"""Control traffic must stay separate from untrusted task code and identity."""

import asyncio
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ASSETS = Path(__file__).parents[3] / "deploy/opensandbox"
spec = importlib.util.spec_from_file_location("profile_control", ASSETS / "control.py")
assert spec and spec.loader
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class Request:
    def __init__(self, method="POST", body=None, *, raw=None, headers=None):
        self.method = method
        self.headers = headers or {}
        self.raw = raw if raw is not None else json.dumps(body or {"argv": ["python", "-V"]}).encode()
        self.read = 0

    async def stream(self):
        for index in range(0, len(self.raw), 127):
            self.read += 1
            yield self.raw[index:index + 127]


def prepare(request, path="command", port=44772, **options):
    return asyncio.run(control.prepare_proxy_request(request, port, path, **options))


def test_command_defaults_to_low_identity_and_private_work():
    path, body = prepare(Request(body={"argv": ["python", "-V"], "envs": {"HOME": "/root", "NAME": "值"}}))
    command = json.loads(body)
    assert path == "command" and command["uid"] == command["gid"] == 10001
    assert command["timeout"] == 600_000 and command["cwd"] == "/workspace"
    assert command["envs"] == {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "NAME": "值"}


def test_explicit_approved_limits_and_task_identity_survive():
    _, body = prepare(Request(body={"argv": ["python", "-V"], "uid": 10001, "gid": 10001,
                                   "timeout": 600_000, "background": True, "cwd": "/tmp"}))
    command = json.loads(body)
    assert command["timeout"] == 600_000 and command["background"] is True and command["cwd"] == "/tmp"


@pytest.mark.parametrize("change", [
    {"uid": 0}, {"gid": 0}, {"uid": "10001"}, {"uid": True}, {"UID": 0},
    {"command": "sh -c whoami"}, {"argv": []}, {"argv": "python"}, {"argv": ["python", "\0"]},
    {"timeout": 0}, {"timeout": 600_001}, {"timeout": True},
    {"background": 1}, {"cwd": True}, {"cwd": "\0"},
    {"envs": {"EXECD_ACCESS_TOKEN": "fixture"}}, {"envs": {"OPENSANDBOX_LIFECYCLE": "{}"}},
    {"envs": {"JUPYTER_HOST": "http://localhost"}}, {"envs": {"NAME": 1}},
])
def test_command_cannot_gain_control_identity_or_configuration(change):
    with pytest.raises(ValueError):
        prepare(Request(body={"argv": ["python", "-V"], **change}))


@pytest.mark.parametrize("raw", [b'{"argv":["python"],"uid":0,"uid":10001}', b'[]', b'null', b'\xff', b'{'])
def test_ambiguous_command_json_is_rejected(raw):
    with pytest.raises(ValueError):
        prepare(Request(raw=raw))


def test_chunked_commands_are_bounded_before_json_decode():
    request = Request(raw=b" " * (control.MAX_COMMAND_BYTES + 1000))
    with pytest.raises(ValueError, match="too large"):
        prepare(request)
    assert request.read * 127 <= control.MAX_COMMAND_BYTES + 127


def test_encoded_command_body_is_rejected_without_reading():
    request = Request(headers={"content-encoding": "gzip"})
    with pytest.raises(ValueError):
        prepare(request)
    assert request.read == 0


@pytest.mark.parametrize("path,method", [
    ("files/upload", "POST"), ("files/download", "GET"), ("files", "DELETE"),
    ("directories", "POST"), ("directories/list", "GET"),
    (control.WORKER_PREFIX + "files/info", "GET"),
])
def test_file_operations_use_low_uid_worker_without_buffering_uploads(path, method):
    request = Request(method=method)
    forwarded, body = prepare(request, path)
    assert forwarded == control.WORKER_PREFIX + path.removeprefix(control.WORKER_PREFIX)
    assert body is None and request.read == 0


@pytest.mark.parametrize("path,method,port", [
    ("internal/init", "POST", 44772), ("session", "POST", 44772), ("pty/x/ws", "GET", 44772),
    ("code", "POST", 44772), ("proxy/443/internal/init", "POST", 44772),
    ("v1/filesystem/0/0/files/upload", "POST", 44772),
    ("v1/filesystem/010001/10001/files/upload", "POST", 44772),
    ("files/%2e%2e/internal/init", "POST", 44772), ("files//upload", "POST", 44772),
    ("files/upload", "GET", 44772), ("ping", "GET", 443), ("policy", "POST", 18080),
])
def test_unused_and_alternate_control_paths_are_rejected(path, method, port):
    with pytest.raises(ValueError):
        prepare(Request(method=method), path, port)


def test_control_health_status_and_internal_policy_read_are_allowed():
    for path in ("ping", "ready", "command/status/abc", "command/abc/logs"):
        assert prepare(Request(method="GET"), path) == (path, None)
    assert prepare(Request(method="DELETE")) == ("command", None)
    assert prepare(Request(method="GET"), "policy", 18080, internal=True) == ("policy", None)
    with pytest.raises(ValueError):
        prepare(Request(), "policy", 18080, internal=True)


def test_control_launcher_is_root_owned_private_and_uses_reserved_port():
    container = Mock()
    container.put_archive.return_value = True
    control.install_control_launcher(container)
    destination, data = container.put_archive.call_args.args
    assert destination == "/opt/opensandbox"
    with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
        assert {m.name for m in bundle} == {"yipilot-execd", ".env", "control"}
        for name, mode in [("yipilot-execd", 0o500), (".env", 0o600), ("control", 0o700)]:
            member = bundle.getmember(name)
            assert member.uid == member.gid == 0 and member.mode == mode
        assert b"--port 443" in bundle.extractfile("yipilot-execd").read()
    container.put_archive.return_value = False
    with pytest.raises(ValueError):
        control.install_control_launcher(container)


def namespace_fixture(monkeypatch, *, host=False):
    fake_os = SimpleNamespace(O_RDONLY=0, O_CLOEXEC=1, open=Mock(return_value=42), close=Mock(),
        fstat=Mock(return_value=SimpleNamespace(st_ino=1 if host else 2)),
        stat=Mock(return_value=SimpleNamespace(st_ino=1)))
    runner = Mock(return_value=SimpleNamespace(stdout=b"1024\n"))
    monkeypatch.setattr(control, "os", fake_os)
    monkeypatch.setattr(control, "subprocess", SimpleNamespace(run=runner))
    container = Mock(attrs={"State": {"Running": True}, "HostConfig": {"NetworkMode": "dedicated"},
                            "NetworkSettings": {"SandboxKey": "/run/docker/netns/owned"}})
    return container, fake_os, runner


def test_namespace_rules_use_pinned_descriptor_and_close_it(monkeypatch):
    container, fake_os, runner = namespace_fixture(monkeypatch)
    control.protect_namespace(container, "dedicated")
    args = runner.call_args
    assert "--net=/proc/self/fd/42" in args.args[0] and args.kwargs["pass_fds"] == (42,)
    assert args.kwargs["check"] is True and args.kwargs["timeout"] == 10
    assert "meta skuid 10001" in args.kwargs["input"].decode()
    fake_os.close.assert_called_once_with(42)


def test_host_namespace_is_never_modified(monkeypatch):
    container, fake_os, runner = namespace_fixture(monkeypatch, host=True)
    with pytest.raises(ValueError, match="host namespace"):
        control.protect_namespace(container, "dedicated")
    runner.assert_not_called()
    fake_os.close.assert_called_once_with(42)


def test_unprotected_privileged_ports_refuse_creation(monkeypatch):
    container, fake_os, runner = namespace_fixture(monkeypatch)
    runner.return_value.stdout = b"0\n"
    with pytest.raises(ValueError, match="not protected"):
        control.protect_namespace(container, "dedicated")
    assert runner.call_count == 1 and "nft" not in runner.call_args.args[0]
    fake_os.close.assert_called_once_with(42)


def test_failed_namespace_setup_has_no_unrestricted_fallback(monkeypatch):
    container, fake_os, runner = namespace_fixture(monkeypatch)
    runner.side_effect = subprocess.CalledProcessError(1, ["nft"])
    with pytest.raises(subprocess.CalledProcessError):
        control.protect_namespace(container, "dedicated")
    fake_os.close.assert_called_once_with(42)
