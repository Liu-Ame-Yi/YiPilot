"""The deployment profile is deliberately independent of the Windows client."""

import copy
import importlib.util
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

ASSETS = Path(__file__).parents[3] / "deploy/opensandbox"
spec = importlib.util.spec_from_file_location("yipilot_profile", ASSETS / "policy.py")
assert spec and spec.loader
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


@pytest.fixture
def request_document(monkeypatch):
    monkeypatch.setattr(profile, "validate_work_directory", lambda path: None)
    job = "a" * 32
    return {"image": {"uri": profile.IMAGE}, "timeout": 180,
            "metadata": {"yipilot.job_id": job, "yipilot.profile": profile.PROFILE},
            "entrypoint": ["tail", "-f", "/dev/null"], "networkPolicy": copy.deepcopy(profile.DENY),
            "volumes": [{"name": "work", "host": {"path": f"/srv/yipilot-runner/jobs/{job}/work"},
                         "mountPath": "/workspace", "readOnly": False}]}


def test_minimal_request_and_explicit_limits(request_document):
    profile.validate_request(request_document)
    profile.validate_resources((256 * 1024**2, 250_000_000, None))
    profile.validate_resources((1024**3, 1_000_000_000, None))


def test_tools_image_and_profile_are_an_indivisible_pair(request_document):
    request_document["metadata"]["yipilot.profile"] = profile.TOOLS_PROFILE
    with pytest.raises(ValueError):
        profile.validate_request(request_document)
    request_document["image"]["uri"] = profile.TOOLS_IMAGE
    profile.validate_request(request_document)
    request_document["metadata"]["yipilot.profile"] = profile.PROFILE
    with pytest.raises(ValueError):
        profile.validate_request(request_document)


@pytest.mark.parametrize("field,value", [
    ("image", {"uri": "python:latest"}), ("image", {"uri": profile.IMAGE, "auth": {"username": "x"}}),
    ("timeout", None), ("timeout", 901), ("timeout", True),
    ("metadata", {"yipilot.job_id": "../other"}),
    ("env", {"OPENSANDBOX_EGRESS_MODE": "dns"}),
    ("extensions", {"bootstrap.execd.isolation": "enable"}),
    ("networkPolicy", None), ("networkPolicy", {"defaultAction": "allow"}),
    ("entrypoint", ["sh", "-c", "sleep infinity"]), ("platform", {"os": "windows", "arch": "amd64"}),
    ("credentialProxy", {"enabled": True}), ("snapshotId", "old-snapshot"),
    ("volumes", []), ("secureAccess", True),
])
def test_profile_bypasses_are_rejected(request_document, field, value):
    request_document[field] = value
    with pytest.raises(ValueError):
        profile.validate_request(request_document)


@pytest.mark.parametrize("change", [
    {"host": {"path": "/etc"}}, {"mountPath": "/opt/opensandbox"},
    {"subPath": ".."}, {"pvc": {"claimName": "other-job"}},
    {"host": {"path": "/srv/yipilot-runner/jobs/" + "b" * 32 + "/work"}},
])
def test_other_host_paths_and_mounts_are_rejected(request_document, change):
    request_document["volumes"][0].update(change)
    with pytest.raises(ValueError):
        profile.validate_request(request_document)


@pytest.mark.parametrize("limits", [
    (None, 250_000_000, None), (256 * 1024**2, None, None),
    (64 * 1024**2, 250_000_000, None), (2 * 1024**3, 250_000_000, None),
    (256 * 1024**2, 2_000_000_000, None), (256 * 1024**2, 250_000_000, 1),
])
def test_missing_or_excessive_limits_are_rejected(limits):
    with pytest.raises(ValueError):
        profile.validate_resources(limits)


def test_network_must_be_internal_private_and_without_peer_communication():
    network = {"Name": profile.NETWORK, "Driver": "bridge", "Internal": True, "EnableIPv6": False,
               "Options": {"com.docker.network.bridge.name": profile.BRIDGE,
                           "com.docker.network.bridge.enable_icc": "false"}}
    profile.validate_network(network)
    for field, value in [("Internal", False), ("EnableIPv6", True), ("Name", "bridge"), ("Options", {})]:
        changed = {**network, field: value}
        with pytest.raises(ValueError):
            profile.validate_network(changed)


def test_deployment_template_has_fixed_runtime_and_no_extra_binds():
    config = tomllib.loads((ASSETS / "config.toml.example").read_text(encoding="utf-8"))
    profile.validate_config(config)
    for field, value in [("network_mode", "bridge"), ("publish_host", "0.0.0.0"),
                         ("sandbox_binds", ["/:/host:rw"]), ("seccomp_profile", "unconfined")]:
        changed = copy.deepcopy(config)
        changed["docker"][field] = value
        with pytest.raises(ValueError):
            profile.validate_config(changed)


@pytest.mark.parametrize("component,mode,uid,gid", [
    ("work", stat.S_IFDIR | 0o700, 10001, 10001),
    ("work", stat.S_IFDIR | 0o777, 10001, 10001),
    ("work", stat.S_IFDIR | 0o700, 0, 10001),
    ("work", stat.S_IFDIR | 0o700, 10001, 0),
    ("work", stat.S_IFLNK | 0o700, 10001, 10001),
    ("ancestor", stat.S_IFDIR | 0o777, 0, 0),
    ("ancestor", stat.S_IFDIR | 0o755, 10001, 10001),
    ("ancestor", stat.S_IFLNK | 0o755, 0, 0),
])
def test_host_mount_ownership_and_link_boundary(monkeypatch, component, mode, uid, gid):
    work = profile.JOBS_ROOT / ("a" * 32) / "work"
    nodes = {p: SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0) for p in work.parents}
    nodes[work] = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=10001, st_gid=10001)
    target = work if component == "work" else work.parent
    nodes[target] = SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
    original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path: nodes[path] if path in nodes else original(path))
    if component == "work" and mode == stat.S_IFDIR | 0o700 and uid == gid == 10001:
        profile.validate_work_directory(work)
    else:
        with pytest.raises(ValueError):
            profile.validate_work_directory(work)
