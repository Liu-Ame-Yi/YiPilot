"""One offline Docker Desktop Linux Worker. No host execution fallback exists."""

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, BinaryIO, Literal
from uuid import uuid4

from filelock import FileLock

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ArtifactRef, ExecutionPolicy, fingerprint_model
from yipilot.domain.sandbox import (
    SandboxBudgets,
    SandboxOutputManifest,
    SandboxRequest,
    SandboxResult,
    SourceManifest,
)
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.filesystem import atomic_write_bytes, project_path
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.paths import require_plain_path
from yipilot.security.redaction import safe_stderr_summary
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.sandbox_sources import (
    PreparedSources,
    SandboxSourceService,
    scan_source_tree,
)

_UID = 10001
_USER = f"{_UID}:{_UID}"
_ENDPOINT = "npipe:////./pipe/docker_engine"
_ENV = {
    "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "MYPY_CACHE_DIR": "/tmp/mypy",
    "RUFF_CACHE_DIR": "/tmp/ruff",
}
_IMAGE_ENV = set(_ENV) | {"LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}
_PREFLIGHT_BUDGETS = SandboxBudgets(
    wall_clock_seconds=15,
    cpu_count=1,
    memory_bytes=512 * 1024**2,
    pid_limit=64,
    stdout_bytes=8192,
    stderr_bytes=8192,
)
_PROBE = r"""
import errno, importlib.util, json, os, pathlib, shutil, socket, sys
assert os.getuid() == 10001 and os.getgid() == 10001
assert os.listdir('/sys/class/net') == ['lo']
assert int(pathlib.Path('/sys/fs/cgroup/memory.max').read_text()) == int(sys.argv[2])
assert int(pathlib.Path('/sys/fs/cgroup/pids.max').read_text()) == int(sys.argv[3])
quota, period = pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().split()
assert abs(int(quota) / int(period) - float(sys.argv[4])) < 0.000001
status = dict(line.split(':', 1) for line in pathlib.Path('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'].strip(), 16) == 0 and status['NoNewPrivs'].strip() == '1'
try:
    pathlib.Path('/yipilot-root-probe').write_text('denied')
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES)
else:
    raise AssertionError('root filesystem is writable')
p = pathlib.Path('/workspace/probe.txt')
p.write_text('probe')
assert p.read_text() == 'probe'
p.unlink()
with socket.socket() as s:
    s.settimeout(1)
    assert s.connect_ex(('198.51.100.1', 53)) != 0
try:
    socket.getaddrinfo('example.invalid', 443)
except socket.gaierror:
    pass
else:
    raise AssertionError('DNS available')
try:
    socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
except PermissionError:
    pass
else:
    raise AssertionError('raw socket available')
assert shutil.which(sys.argv[1])
for tool in ('git', 'python', 'ruff', 'mypy'):
    assert shutil.which(tool), tool
assert importlib.util.find_spec('pytest') is not None
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'network': 'none', 'write_probe': True}))
"""


@dataclass(frozen=True)
class _PreflightRequest:
    """The small, non-Run input shared by the doctor and Worker probe."""

    image_digest: str
    run_id: str
    fingerprint: str
    argv: tuple[str, ...] = ("python",)
    budgets: SandboxBudgets = _PREFLIGHT_BUDGETS


class DockerSandboxWorker:
    """Run one validated request; Control Plane state and Git stay outside Docker."""

    def __init__(
        self,
        project_root: Path,
        policy: ExecutionPolicy,
        prepared: PreparedSources,
        *,
        input_manifest: SourceManifest | None = None,
    ) -> None:
        self._sources = SandboxSourceService(project_root, GitRunner())
        self._root = project_root.resolve(strict=True)
        self._store = FileTaskStore(self._root)
        self._artifacts = FileArtifactStore(self._store)
        self._policy = policy
        self._prepared = prepared
        self._input_manifest = input_manifest
        self._docker = self._docker_path()

    @staticmethod
    def _docker_path() -> str | None:
        executable = (
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Docker/Docker/resources/bin/docker.exe"
        )
        require_plain_path(executable)
        return str(executable) if os.name == "nt" and executable.is_file() else None

    @classmethod
    def doctor(cls, image_digest: str) -> dict[str, Any]:
        """Check a prepared image without building, pulling, or running repository code."""
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
            raise DomainError(
                "SANDBOX_IMAGE_DIGEST_MISMATCH", "image must be an immutable sha256 digest"
            )
        worker = object.__new__(cls)
        worker._docker = cls._docker_path()
        run_id = f"doctor-{uuid4().hex}"
        request = _PreflightRequest(image_digest, run_id, uuid4().hex)
        facts = worker._readiness(request)
        with TemporaryDirectory(prefix="yipilot-doctor-") as temporary:
            workspace = Path(temporary)
            workspace.chmod(0o777)
            worker._probe(request, workspace, f"yipilot-{run_id}", None)
        return facts

    def execute(
        self, request: SandboxRequest, *, cancel_event: threading.Event | None = None
    ) -> SandboxResult:
        try:
            request = SandboxRequest.model_validate_json(
                request.model_dump_json(), context={"policy": self._policy}
            )
        except ValueError as error:
            raise DomainError(
                "SANDBOX_UNAVAILABLE", "sandbox request policy is invalid"
            ) from error
        self._validate_sources(request)
        root = self._store.event_journal_path(request.run_id).parent
        evidence = project_path(root, "docker-worker")
        lock = project_path(root, ".docker-worker.lock")
        require_plain_path(lock)
        with FileLock(str(lock)):
            require_plain_path(evidence)
            if evidence.exists():
                raise DomainError(
                    "SANDBOX_CONFLICT",
                    "Worker attempt already exists; retry requires a new Run",
                )
            evidence.mkdir()
            container: str | None = None
            process: subprocess.Popen[bytes] | None = None
            try:
                self._write(evidence, "request.json", request.model_dump(mode="json"))
                self._write(
                    evidence, "policy.json", self._policy.model_dump(mode="json")
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise DomainError("SANDBOX_CANCELLED", "cancelled before launch")
                facts = self._readiness(request)
                self._write(evidence, "runtime.json", facts)
                name = "yipilot-" + request.run_id
                with TemporaryDirectory(prefix=".docker-probe-", dir=root) as temporary:
                    probe = Path(temporary)
                    probe.chmod(0o777)
                    self._probe(request, probe, name + "-probe", evidence)
                self._validate_sources(request)
                if cancel_event is not None and cancel_event.is_set():
                    raise DomainError(
                        "SANDBOX_CANCELLED", "cancelled before repository launch"
                    )
                argv = self._create_argv(
                    request, name, self._prepared.execution, request.argv
                )
                self._write(evidence, "create-argv.json", argv)
                container = self._create(argv)
                before = self._inspect(container)
                self.check_container(
                    before, request, self._prepared.execution, request.argv
                )
                self._write(evidence, "container-before.json", before)
                started_at = datetime.now(UTC)
                process, stdout, stderr, reason = self._attached(
                    container,
                    request.budgets.wall_clock_seconds,
                    request.budgets.stdout_bytes,
                    request.budgets.stderr_bytes,
                    cancel_event,
                )
                after = self._inspect(container)
                if after["State"]["Running"]:
                    self._kill(container)
                    after = self._inspect(container)
                    reason = "container_error"
                self.check_container(
                    after, request, self._prepared.execution, request.argv
                )
                if after["State"].get("OOMKilled") or after["State"].get("Error"):
                    reason = "container_error"
                if after["State"].get("Status") != "exited" or (
                    process.returncode != 0 and after["State"]["ExitCode"] == 0
                ):
                    reason = "container_error"
                self._write(evidence, "container-after.json", after)
                self._remove(container)
                container = None
                self._write(evidence, "cleanup.json", {"container_removed": True})
                stdout_ref, stdout_size = self._stream(request, "stdout", stdout)
                stderr_ref, stderr_size = self._stream(request, "stderr", stderr)
                self._sources.validate(self._prepared)
                files, _ = scan_source_tree(
                    self._prepared.execution, self._prepared.manifest.files
                )
                manifest = SandboxOutputManifest(
                    task_id=request.task_id,
                    operation_id=request.operation_id,
                    run_id=request.run_id,
                    request_fingerprint=request.fingerprint,
                    baseline_manifest_hash=request.baseline_manifest_hash,
                    files=files,
                )
                atomic_write_bytes(
                    evidence / "output-manifest.json", manifest.canonical_bytes
                )
                result = SandboxResult(
                    task_id=request.task_id,
                    operation_id=request.operation_id,
                    run_id=request.run_id,
                    request_fingerprint=request.fingerprint,
                    container_id=after["Id"],
                    image_digest=request.image_digest,
                    docker_version=facts["server_version"],
                    backend=facts["backend"],
                    enhanced_container_isolation=facts["eci"],
                    network="none",
                    effective_uid=_UID,
                    effective_gid=_UID,
                    resource_limits=request.budgets,
                    reason=reason,
                    exit_code=after["State"]["ExitCode"],
                    started_at=started_at,
                    ended_at=datetime.now(UTC),
                    stdout_artifact_ref=stdout_ref,
                    stderr_artifact_ref=stderr_ref,
                    stdout_bytes=stdout_size,
                    stderr_bytes=stderr_size,
                    output_manifest_hash=manifest.fingerprint,
                    fallback_occurred=False,
                )
                self._write(evidence, "result.json", result.model_dump(mode="json"))
                return result
            except BaseException as error:
                if container is not None:
                    self._remove(container)
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                code = (
                    error.code
                    if isinstance(error, DomainError)
                    else "SANDBOX_UNAVAILABLE"
                )
                self._write(
                    evidence,
                    "failure.json",
                    {"error_code": code, "fallback_occurred": False},
                )
                if isinstance(
                    error,
                    (
                        OSError,
                        ValueError,
                        KeyError,
                        TypeError,
                        subprocess.SubprocessError,
                    ),
                ):
                    raise DomainError(
                        "SANDBOX_UNAVAILABLE",
                        "Worker setup or evidence collection failed",
                    ) from error
                raise

    def _validate_sources(self, request: SandboxRequest) -> None:
        prepared = self._prepared
        manifest = prepared.manifest
        run = self._store.get_run(request.task_id, request.run_id)
        self._store.get_operation(request.task_id, request.operation_id)
        if (
            (manifest.task_id, manifest.operation_id, manifest.run_id)
            != (request.task_id, request.operation_id, request.run_id)
            or run.operation_id != request.operation_id
            or run.status.value not in {"pending", "running"}
            or prepared.manifest_hash != request.baseline_manifest_hash
            or str(prepared.execution) != request.execution_directory
        ):
            raise DomainError(
                "SANDBOX_ASSOCIATION_INVALID", "Worker input does not belong to Run"
            )
        self._sources.validate(prepared)
        expected = manifest
        if request.input_manifest_hash is not None:
            candidate = self._input_manifest
            path = prepared.execution.parent / "input-manifest.json"
            require_plain_path(path)
            if (
                candidate is None
                or candidate.model_dump(exclude={"files"})
                != manifest.model_dump(exclude={"files"})
                or fingerprint_model(candidate) != request.input_manifest_hash
            ):
                raise DomainError(
                    "SANDBOX_MANIFEST_INVALID", "seed manifest is not bound to this Run"
                )
            try:
                if SourceManifest.model_validate_json(path.read_bytes()) != candidate:
                    raise ValueError("seed changed")
            except (OSError, ValueError) as error:
                raise DomainError(
                    "SANDBOX_MANIFEST_INVALID", "seed evidence changed"
                ) from error
            expected = candidate
        elif self._input_manifest is not None:
            raise DomainError("SANDBOX_MANIFEST_INVALID", "unbound seed manifest")
        if scan_source_tree(prepared.execution, expected.files)[0] != expected.files:
            raise DomainError(
                "SANDBOX_MANIFEST_INVALID", "Worker input is not the frozen baseline"
            )

    def _cli(self, *args: str) -> bytes:
        if self._docker is None:
            raise DomainError("DOCKER_CLI_UNAVAILABLE", "Docker CLI is unavailable")
        try:
            result = subprocess.run(
                [self._docker, "--host", _ENDPOINT, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                shell=False,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DomainError(
                "DOCKER_CLI_UNAVAILABLE", "Docker control command failed"
            ) from error
        if result.returncode != 0:
            detail = safe_stderr_summary(
                result.stderr.decode("utf-8", errors="replace")
            )
            lowered = detail.casefold()
            if (
                "the system cannot find the file specified" in lowered
                or "no such file or directory" in lowered
            ):
                code = "DOCKER_DAEMON_UNAVAILABLE"
            elif "access is denied" in lowered or "permission denied" in lowered:
                code = "DOCKER_ACCESS_DENIED"
            elif args[:2] == ("image", "inspect") and (
                "no such image" in lowered or "not found" in lowered
            ):
                code = "SANDBOX_IMAGE_MISSING"
            else:
                code = "DOCKER_DAEMON_UNAVAILABLE"
            raise DomainError(
                code, "Docker control command failed: " + detail
            )
        return result.stdout

    def _readiness(
        self, request: SandboxRequest | _PreflightRequest
    ) -> dict[str, Any]:
        info = json.loads(self._cli("info", "--format", "{{json .}}"))
        if info["OSType"] != "linux" or info["OperatingSystem"] != "Docker Desktop":
            raise DomainError(
                "DOCKER_NOT_LINUX_MODE", "Docker Desktop Linux containers required"
            )
        image = json.loads(self._cli("image", "inspect", request.image_digest))[0]
        config = image["Config"]
        if (
            image["Id"] != request.image_digest
            or image["Os"] != "linux"
            or config.get("Volumes")
            or any(
                entry.split("=", 1)[0] not in _IMAGE_ENV
                for entry in config.get("Env", [])
            )
        ):
            code = (
                "SANDBOX_IMAGE_DIGEST_MISMATCH"
                if image["Id"] != request.image_digest
                else "SANDBOX_POLICY_UNAVAILABLE"
            )
            raise DomainError(
                code, "image digest, volumes or environment denied"
            )
        kernel = info.get("KernelVersion", "").casefold()
        backend = "wsl2" if "wsl2" in kernel else "unknown"
        return {
            "server_version": info["ServerVersion"],
            "backend": backend,
            "eci": None,
            "kernel": info.get("KernelVersion"),
            "security_options": info.get("SecurityOptions"),
            "image_digest": image["Id"],
        }

    @staticmethod
    def _create_argv(
        request: SandboxRequest | _PreflightRequest,
        name: str,
        workspace: Path,
        command: tuple[str, ...],
    ) -> list[str]:
        require_plain_path(workspace)
        if "," in str(workspace):
            raise DomainError(
                "SANDBOX_PATH_DENIED", "Docker mount path contains a separator"
            )
        b = request.budgets
        argv = [
            "create",
            "--pull=never",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            str(b.pid_limit),
            "--memory",
            str(b.memory_bytes),
            "--memory-swap",
            str(b.memory_bytes),
            "--cpus",
            str(b.cpu_count),
            "--user",
            _USER,
            "--workdir",
            "/workspace",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace,readonly=false",
            "--label",
            f"yipilot.run={request.run_id}",
            "--label",
            f"yipilot.request={request.fingerprint}",
            "--entrypoint",
            command[0],
        ]
        for key, value in _ENV.items():
            argv.extend(("--env", f"{key}={value}"))
        return [*argv, request.image_digest, *command[1:]]

    def _create(self, argv: list[str]) -> str:
        name = argv[argv.index("--name") + 1]
        filters = (
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"name=^/{name}$",
            "--format",
            "{{.ID}}",
        )
        if self._cli(*filters).strip():
            raise DomainError("SANDBOX_CONFLICT", "container name already exists")
        try:
            container = self._cli(*argv).decode().strip()
            if re.fullmatch(r"[0-9a-f]{64}", container) is None:
                raise DomainError(
                    "SANDBOX_UNAVAILABLE", "Docker did not return a container ID"
                )
            return container
        except BaseException:
            # A lost create response must not leave an untracked Run container.
            for identifier in self._cli(*filters).decode().splitlines():
                facts = self._inspect(identifier)
                labels = facts["Config"].get("Labels", {})
                expected = next(
                    a.split("=", 1)[1] for a in argv if a.startswith("yipilot.request=")
                )
                if labels.get("yipilot.request") == expected:
                    self._remove(identifier)
            raise

    def _inspect(self, container: str) -> dict[str, Any]:
        return json.loads(self._cli("container", "inspect", container))[0]  # type: ignore[no-any-return]

    @staticmethod
    def check_container(
        facts: dict[str, Any],
        request: SandboxRequest | _PreflightRequest,
        workspace: Path,
        command: tuple[str, ...],
    ) -> None:
        host, config = facts["HostConfig"], facts["Config"]
        mounts = facts["Mounts"]
        if (
            facts["Image"] != request.image_digest
            or config["User"] != _USER
            or config["WorkingDir"] != "/workspace"
            or config["Entrypoint"] != [command[0]]
            or (config.get("Cmd") or []) != list(command[1:])
            or config["Labels"].get("yipilot.request") != request.fingerprint
            or config["Labels"].get("yipilot.run") != request.run_id
            or host["NetworkMode"] != "none"
            or not host["ReadonlyRootfs"]
            or host["Privileged"]
            or host.get("CapAdd")
            or host.get("Devices")
            or host.get("DeviceRequests")
            or host.get("Binds")
            or host.get("PortBindings")
            or host["PidMode"] == "host"
            or host["IpcMode"] == "host"
            or host["CapDrop"] != ["ALL"]
            or "no-new-privileges=true" not in host["SecurityOpt"]
            or host["PidsLimit"] != request.budgets.pid_limit
            or host["Memory"] != request.budgets.memory_bytes
            or host["MemorySwap"] != request.budgets.memory_bytes
            or host["NanoCpus"] != int(request.budgets.cpu_count * 10**9)
            or host["LogConfig"]["Type"] != "none"
            or config.get("Healthcheck", {}).get("Test") != ["NONE"]
            or host.get("CgroupnsMode") == "host"
            or host.get("Tmpfs")
            != {"/tmp": "rw,noexec,nosuid,nodev,size=67108864,mode=1777"}
            or len(mounts) != 1
            or mounts[0]["Type"] != "bind"
            or mounts[0]["Destination"] != "/workspace"
            or not mounts[0]["RW"]
        ):
            raise DomainError(
                "SANDBOX_UNAVAILABLE", "effective container policy differs"
            )
        # Docker Desktop translates Windows paths inside its VM; HostConfig retains input.
        declared = host.get("Mounts", [])
        if (
            len(declared) != 1
            or declared[0].get("Source") != str(workspace)
            or declared[0].get("Target") != "/workspace"
        ):
            raise DomainError("SANDBOX_UNAVAILABLE", "effective mount source differs")

    def _probe(
        self,
        request: SandboxRequest | _PreflightRequest,
        workspace: Path,
        name: str,
        evidence: Path | None,
    ) -> None:
        command = (
            "python",
            "-I",
            "-c",
            _PROBE,
            request.argv[0],
            str(request.budgets.memory_bytes),
            str(request.budgets.pid_limit),
            str(request.budgets.cpu_count),
        )
        container = self._create(self._create_argv(request, name, workspace, command))
        try:
            self.check_container(self._inspect(container), request, workspace, command)
            _, stdout, _, reason = self._attached(container, 15, 8192, 8192, None)
            facts = self._inspect(container)
            if (
                reason != "exited"
                or facts["State"]["Running"]
                or facts["State"]["ExitCode"] != 0
            ):
                raise DomainError(
                    "SANDBOX_POLICY_UNAVAILABLE", "offline runtime probe failed"
                )
            probe = json.loads(stdout)
            if probe != {
                "uid": _UID,
                "gid": _UID,
                "network": "none",
                "write_probe": True,
            }:
                raise DomainError(
                    "SANDBOX_POLICY_UNAVAILABLE", "runtime probe facts differ"
                )
            if evidence is not None:
                self._write(evidence, "preflight.json", probe)
        finally:
            self._remove(container)

    def _attached(
        self,
        container: str,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        cancel: threading.Event | None,
    ) -> tuple[
        subprocess.Popen[bytes],
        bytes,
        bytes,
        Literal["exited", "timed_out", "cancelled", "output_limit", "container_error"],
    ]:
        assert self._docker is not None
        process = subprocess.Popen(
            [self._docker, "--host", _ENDPOINT, "start", "--attach", container],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        exceeded, failed = threading.Event(), threading.Event()
        buffers = [bytearray(), bytearray()]

        def read(stream: BinaryIO, target: bytearray, limit: int) -> None:
            try:
                while chunk := stream.read1(8192):  # type: ignore[attr-defined]
                    remaining = limit - len(target)
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()
            except OSError:
                failed.set()
            finally:
                stream.close()

        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=read, args=args, daemon=True)
            for args in (
                (process.stdout, buffers[0], stdout_limit),
                (process.stderr, buffers[1], stderr_limit),
            )
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        reason: Literal[
            "exited", "timed_out", "cancelled", "output_limit", "container_error"
        ] = "exited"
        try:
            while process.poll() is None:
                if exceeded.is_set():
                    reason = "output_limit"
                elif failed.is_set():
                    reason = "container_error"
                elif cancel is not None and cancel.is_set():
                    reason = "cancelled"
                elif time.monotonic() >= deadline:
                    reason = "timed_out"
                if reason != "exited":
                    self._kill(container)
                    break
                time.sleep(0.02)
            process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
            if any(reader.is_alive() for reader in readers) or failed.is_set():
                raise DomainError(
                    "SANDBOX_UNAVAILABLE", "container output collection incomplete"
                )
            if exceeded.is_set():
                reason = "output_limit"
            return process, bytes(buffers[0]), bytes(buffers[1]), reason
        except BaseException:
            self._kill(container)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
            raise

    def _kill(self, container: str) -> None:
        if self._inspect(container)["State"]["Running"]:
            self._cli("kill", container)
        if self._inspect(container)["State"]["Running"]:
            raise DomainError("SANDBOX_CLEANUP_FAILED", "container is still running")

    def _remove(self, container: str) -> None:
        self._cli("rm", "--force", container)
        if self._cli(
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={container}",
            "--format",
            "{{.ID}}",
        ).strip():
            raise DomainError(
                "SANDBOX_CLEANUP_FAILED", "container removal was not confirmed"
            )

    def _stream(
        self, request: SandboxRequest, stream: str, content: bytes
    ) -> tuple[ArtifactRef, int]:
        record = self._artifacts.write_bytes(
            request.task_id,
            request.run_id,
            producer="docker_worker",
            relative_path=f"artifacts/docker-worker/{stream}.bin",
            content=content,
            input_fingerprint=request.fingerprint,
            operation_id=request.operation_id,
        )
        size = (
            (
                self._store.event_journal_path(request.run_id).parent
                / record.relative_path
            )
            .stat()
            .st_size
        )
        return ArtifactRef(
            artifact_id=record.artifact_id,
            task_id=record.task_id,
            operation_id=record.operation_id,
            run_id=record.run_id,
            relative_path=record.relative_path,
            sha256=record.sha256,
        ), size

    @staticmethod
    def _write(directory: Path, name: str, value: object) -> None:
        atomic_write_bytes(
            directory / name,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        )
