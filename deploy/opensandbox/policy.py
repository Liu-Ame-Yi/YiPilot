"""YiPilot's single Linux execution profile for OpenSandbox Server 1.1.1."""

import re
import stat
from pathlib import Path, PurePosixPath

PROFILE = "yipilot-python-v1"
NETWORK = "yipilot-isolated"
BRIDGE = "yipilot0"
JOBS_ROOT = Path("/srv/yipilot-runner/jobs")
IMAGE = "public.ecr.aws/docker/library/python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258"
EXECD = "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/execd@sha256:cda2bc35409bbf57561599b316cafb899598f7dce671332e7ceda14eb1cfeded"
EGRESS = "sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/egress@sha256:bce2cc2e4dc3a3594678b91ac29efb0669fccebfdfc137727018d3b087fbb13a"
DENY = {"defaultAction": "deny", "egress": [{"action": "deny", "target": "*"}]}


def validate_work_directory(path):
    """Only the trusted host may choose and replace a Job's mount point."""
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Job ancestors must be root-owned plain protected directories")
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 10001
            or info.st_gid != 10001 or info.st_mode & 0o077):
        raise ValueError("Job work must be a plain private directory owned by 10001:10001")


def validate_request(request):
    """Reject unsupported shapes before image resolution or Docker side effects."""
    metadata = request.get("metadata") or {}
    job = metadata.get("yipilot.job_id", "")
    if (re.fullmatch(r"[0-9a-f]{32}", job) is None
            or metadata != {"yipilot.job_id": job, "yipilot.profile": PROFILE}):
        raise ValueError("Exactly one canonical Job ID and profile label are required")
    if request.get("image") != {"uri": IMAGE}:
        raise ValueError("Only the cached pinned Python image is permitted")
    for field in ("snapshotId", "templateId", "extensions", "env", "lifecycle",
                  "credentialProxy", "secureAccess", "resourceRequests"):
        if request.get(field):
            raise ValueError(f"{field} is unavailable in this execution profile")
    if request.get("platform") not in (None, {"os": "linux", "arch": "amd64"}):
        raise ValueError("This profile requires Linux amd64")
    if request.get("entrypoint") != ["tail", "-f", "/dev/null"]:
        raise ValueError("The supervisor entrypoint is fixed; use the command API for work")
    timeout = request.get("timeout")
    if type(timeout) is not int or not 60 <= timeout <= 900:
        raise ValueError("A bounded sandbox lifetime of 60..900 seconds is required")
    if request.get("networkPolicy") != DENY:
        raise ValueError("The explicit deny-all network policy is mandatory")
    expected = JOBS_ROOT / job / "work"
    volumes = request.get("volumes") or []
    if len(volumes) != 1:
        raise ValueError("Exactly one Job work mount is required")
    volume = volumes[0]
    if (set(volume) - {"name", "host", "mountPath", "readOnly"}
            or volume.get("name") != "work" or volume.get("mountPath") != "/workspace"
            or volume.get("readOnly", False) is not False
            or volume.get("host") != {"path": str(PurePosixPath(expected.as_posix()))}):
        raise ValueError("Only this Job's work directory may be mounted at /workspace")
    validate_work_directory(expected)


def validate_resources(limits):
    memory, cpu, gpu = limits
    if (memory is None or not 128 * 1024**2 <= memory <= 1024**3
            or cpu is None or not 100_000_000 <= cpu <= 1_000_000_000 or gpu is not None):
        raise ValueError("Explicit 128Mi..1Gi memory and 100m..1 CPU limits, without GPU, are required")


def validate_config(config):
    docker = config["docker"]
    runtime = config["runtime"]
    egress = config.get("egress") or {}
    if (docker["network_mode"] != NETWORK or docker["publish_host"] != "127.0.0.1"
            or docker.get("sandbox_binds") or docker.get("apparmor_profile")
            or docker.get("seccomp_profile") or config.get("secure_runtime")
            or runtime["type"] != "docker" or runtime["execd_image"] != EXECD
            or runtime.get("execd_run_as_init")
            or config["server"]["host"] != "127.0.0.1"
            or not config["server"].get("api_key")
            or egress.get("image") != EGRESS or egress.get("mode") != "dns+nft"
            or not egress.get("disable_ipv6") or egress.get("upstream_proxy")
            or docker.get("sandbox_env") != {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"}):
        raise ValueError("Server configuration does not match the fixed YiPilot profile")


def validate_network(network):
    options = network.get("Options") or {}
    if (network.get("Name") != NETWORK or network.get("Driver") != "bridge"
            or not network.get("Internal") or network.get("EnableIPv6")
            or options.get("com.docker.network.bridge.name") != BRIDGE
            or options.get("com.docker.network.bridge.enable_icc") != "false"):
        raise ValueError("The isolated, IPv4-only, no-ICC YiPilot network is required")
