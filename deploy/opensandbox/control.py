"""Host-only control channel preparation for the fixed Linux Docker profile."""

import io
import json
import os
import re
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

CONTROL_PORT = 443
TASK_UID = 10001
JOBS_ROOT = Path("/srv/yipilot-runner/jobs")
MAX_COMMAND_BYTES = 64 * 1024
WORKER_PREFIX = "v1/filesystem/10001/10001/"
FILESYSTEM_ROUTES = {
    "files": {"DELETE"}, "files/info": {"GET"}, "files/mv": {"POST"},
    "files/permissions": {"POST"}, "files/search": {"GET"}, "files/replace": {"POST"},
    "files/upload": {"POST"}, "files/download": {"GET"},
    "directories": {"POST", "DELETE"}, "directories/list": {"GET"},
}
CONTROL_RULES = """add table inet yipilot_control
add chain inet yipilot_control output { type filter hook output priority -300; policy accept; }
add rule inet yipilot_control output meta skuid 10001 tcp dport { 53, 443, 8080, 18080, 44772 } reject with tcp reset
add rule inet yipilot_control output meta skuid 10001 udp dport { 53, 443, 8080, 18080, 44772 } reject
"""


@contextmanager
def creation_guard(job_id):
    import fcntl

    root = JOBS_ROOT / job_id
    if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
        raise ValueError("Invalid Job identity")
    # The validated work directory has a root-private parent; task code cannot reach this lease.
    with (root / "runtime.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = root / "runtime.json"
            metadata = path.lstat()
            if (not path.is_file() or path.is_symlink() or metadata.st_uid != 0
                    or metadata.st_mode & 0o077 or metadata.st_nlink != 1 or metadata.st_size > 4096):
                raise ValueError("Private host creation lease required")
            expected = {"job_id": job_id, "sandbox_id": str(UUID(job_id)), "state": "authorized"}
            if json.loads(path.read_text()) != expected:
                raise ValueError("Creation lease is consumed or revoked")
            expected["state"] = "consumed"
            publish_lease(root, expected)
            try:
                yield
            finally:
                expected["state"] = "settled"
                publish_lease(root, expected)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def publish_lease(root, value):
    temporary = root / "runtime.partial"
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, root / "runtime.json")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def protect_namespace(container, expected_network):
    container.reload()
    if (not container.attrs["State"]["Running"]
            or container.attrs["HostConfig"]["NetworkMode"] != expected_network):
        raise ValueError("A running dedicated control namespace is required")
    namespace = container.attrs["NetworkSettings"]["SandboxKey"]
    descriptor = os.open(namespace, os.O_RDONLY | os.O_CLOEXEC)
    try:
        if os.fstat(descriptor).st_ino == os.stat("/proc/self/ns/net").st_ino:
            raise ValueError("Refusing to install task rules in the host namespace")
        command = ["/usr/bin/nsenter", f"--net=/proc/self/fd/{descriptor}", "--"]
        result = subprocess.run(command + ["/usr/sbin/sysctl", "-n", "net.ipv4.ip_unprivileged_port_start"],
                                pass_fds=(descriptor,), check=True, capture_output=True, timeout=10)
        if result.stdout.strip() != b"1024":
            raise ValueError("Privileged control ports are not protected in the task namespace")
        subprocess.run(command + [
                        "/usr/sbin/nft", "-f", "-"], input=CONTROL_RULES.encode(),
                       pass_fds=(descriptor,), check=True, capture_output=True, timeout=10)
    finally:
        os.close(descriptor)


def install_control_launcher(container):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as bundle:
        for name, mode, data in [
            ("yipilot-execd", 0o500, b'#!/bin/sh\nexec /opt/opensandbox/execd --port 443 "$@"\n'),
            (".env", 0o600, b""),
        ]:
            info = tarfile.TarInfo(name)
            info.mode, info.size = mode, len(data)
            bundle.addfile(info, io.BytesIO(data))
        directory = tarfile.TarInfo("control")
        directory.type, directory.mode = tarfile.DIRTYPE, 0o700
        bundle.addfile(directory)
    if not container.put_archive("/opt/opensandbox", content.getvalue()):
        raise ValueError("Control launcher installation failed")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON keys are not permitted")
        result[key] = value
    return result


async def prepare_proxy_request(request, port, path, *, internal=False):
    """Only host-authenticated argv and low-UID file workers reach root execd."""
    method = request.method
    if internal:
        if port == 18080 and method == "GET" and path == "policy":
            return path, None
        raise ValueError("Runtime policy changes are unavailable in this profile")
    if port != 44772:
        raise ValueError("Only the execd server proxy is available")
    if path in ("ping", "ready") and method == "GET":
        return path, None
    local_path = path.removeprefix(WORKER_PREFIX)
    if method in FILESYSTEM_ROUTES.get(local_path, set()):
        return WORKER_PREFIX + local_path, None
    if (method == "DELETE" and path == "command") or (
            method == "GET" and re.fullmatch(r"command/(status/[a-zA-Z0-9_-]{1,128}|[a-zA-Z0-9_-]{1,128}/logs)", path)):
        return path, None
    if method != "POST" or path != "command":
        raise ValueError("The requested control interface is unavailable")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise ValueError("Encoded command bodies are unavailable")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_COMMAND_BYTES:
            raise ValueError("Command body is too large")
        content.extend(chunk)
    try:
        command = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid command JSON") from exc
    fields = {"argv", "command", "background", "timeout", "cwd", "uid", "gid", "envs"}
    if not isinstance(command, dict) or set(command) - fields or command.get("command"):
        raise ValueError("Only an argv command with canonical fields is permitted")
    argv = command.get("argv")
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]):
        raise ValueError("A nonempty argv without NUL is required")
    for key in ("uid", "gid"):
        if command.get(key) is not None and (type(command[key]) is not int or command[key] != TASK_UID):
            raise ValueError("The task identity is fixed at 10001:10001")
        command[key] = TASK_UID
    timeout = command.get("timeout")
    if timeout is None:
        command["timeout"] = 600_000
    elif type(timeout) is not int or not 1 <= timeout <= 600_000:
        raise ValueError("Command timeout must be within 1..600000 milliseconds")
    env = command.get("envs") or {}
    if (not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str)
            or key.startswith(("EXECD", "OPENSANDBOX", "JUPYTER")) for key, value in env.items())):
        raise ValueError("Controller environment overrides are unavailable")
    command["envs"] = {**env, "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"}
    if command.get("background") is not None and type(command["background"]) is not bool:
        raise ValueError("background must be a boolean")
    if command.get("cwd") is not None and (not isinstance(command["cwd"], str) or "\0" in command["cwd"]):
        raise ValueError("cwd must be a path without NUL")
    if not command.get("cwd"):
        command["cwd"] = "/workspace"
    return path, json.dumps(command, separators=(",", ":"), ensure_ascii=False).encode()
