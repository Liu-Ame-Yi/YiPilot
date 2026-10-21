"""Fixed OpenSandbox SDK execution and host-observed writer/resource removal."""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from filelock import FileLock

from yipilot.domain.remote_execution import IMAGES, TOOLS_PROFILE, JobRequest
from yipilot.persistence.filesystem import atomic_write_bytes
from yipilot.runner import disk
from yipilot.runner.service import sync_directory
from yipilot.runner.store import JobStore
from yipilot.security.paths import require_plain_path

JOBS_ROOT = Path("/srv/yipilot-runner/jobs")


def lease(job_id: str, state: str) -> None:
    root = JOBS_ROOT / job_id
    require_plain_path(root)
    path = root / "runtime.json"
    atomic_write_bytes(path, json.dumps({"job_id": job_id, "sandbox_id": str(UUID(job_id)), "state": state}).encode())
    path.chmod(0o600)
    sync_directory(root)


def docker(*arguments: str) -> Any:
    completed = subprocess.run(["docker", *arguments], check=True, capture_output=True, timeout=15)
    return json.loads(completed.stdout) if completed.stdout.strip() else None


def writer_group(pid: int) -> Path:
    group = (Path("/proc") / str(pid) / "cgroup").read_text().strip().split("::", 1)[1]
    if not group.startswith("/") or ".." in Path(group).parts:
        raise ValueError("Invalid cgroup path")
    return Path("/sys/fs/cgroup") / group.lstrip("/")


class OpenSandboxRuntime:
    def __init__(self, connection: Any, *, disk_reserve: int = 4 * 1024**3) -> None:
        self.connection, self.disk_reserve = connection, disk_reserve
        self._groups: dict[str, set[Path]] = {}

    def doctor(self) -> None:
        import httpx
        with httpx.Client(trust_env=False, timeout=3) as client:
            response = client.get("http://127.0.0.1:8081/health")
            response.raise_for_status()
            if response.json().get("status") != "healthy":
                raise ValueError("OpenSandbox is unavailable")
        for image in IMAGES.values():
            if not docker("image", "inspect", image):
                raise ValueError("Pinned image is not cached")

    def prepare_work(self, job_id: str, work: Path, store: JobStore) -> None:
        if work != JOBS_ROOT / job_id / "work":
            raise ValueError("Only the owned Job work directory may be prepared")
        store.update(job_id, sandbox_id=str(UUID(job_id)))
        with FileLock(work.parent / "runtime.lock", timeout=0):
            lease(job_id, "authorized")
        tools = store.get(job_id)[0].profile == TOOLS_PROFILE
        # Serialize allocation so concurrent Jobs cannot spend the same disk reserve.
        with FileLock(JOBS_ROOT.parent / "disk.lock", timeout=30):
            required = disk.WORK_BYTES + (disk.TEMP_BYTES if tools else 0)
            if shutil.disk_usage(work.parent).free < self.disk_reserve + required:
                raise ValueError("Insufficient reserved Job disk")
            disk.prepare(job_id, work)
            if tools:
                temporary = work.parent / "tmp"
                disk.prepare(job_id, temporary)
                temporary.chmod(0o700)
                os.chown(temporary, 10001, 10001)  # type: ignore[attr-defined]

    def release_work(self, job_id: str, work: Path) -> None:
        disk.release(job_id, work.parent / "tmp")
        disk.release(job_id, work)

    def execute(self, job_id: str, request: JobRequest, work: Path, store: JobStore) -> tuple[str, int | None, bytes, bytes]:
        return asyncio.run(self._execute(job_id, request, work, store))

    async def _execute(self, job_id: str, request: JobRequest, work: Path, store: JobStore) -> tuple[str, int | None, bytes, bytes]:
        from opensandbox import Sandbox  # type: ignore[import-not-found]
        from opensandbox.models import (  # type: ignore[import-not-found]
            Host,
            NetworkPolicy,
            Volume,
        )
        from opensandbox.models.execd import (  # type: ignore[import-not-found]
            ExecutionHandlers,
            RunCommandOpts,
        )

        streams = {"stdout": bytearray(), "stderr": bytearray()}
        reason = "exited"
        task: asyncio.Task[Any] | None = None
        box = None
        tools = request.profile == TOOLS_PROFILE
        volumes = [Volume(name="work", host=Host(path=str(work)), mountPath="/workspace")]
        envs = {"TMPDIR": "/job-tmp"} if tools else {}
        if tools:
            volumes.append(Volume(name="tmp", host=Host(path=str(work.parent / "tmp")), mountPath="/job-tmp"))
        if request.policy_revision == "python-isolated-script-v2":
            envs["HYPOTHESIS_STORAGE_DIRECTORY"] = ("/job-tmp" if tools else "/tmp") + "/yipilot-hypothesis"

        for path in [work, *work.rglob("*")]:
            require_plain_path(path)
            os.chown(path, 10001, 10001)  # type: ignore[attr-defined]

        async def output(stream: str, message: Any) -> None:
            nonlocal reason
            data = message.text.encode("utf-8")
            available = getattr(request, stream + "_bytes") - len(streams[stream])
            streams[stream].extend(data[:available])
            if len(data) > available:
                reason = "output_limit"
                raise ValueError("Output budget reached")

        try:
            box = await Sandbox.create(request.image, timeout=timedelta(seconds=min(900, request.timeout_seconds + 120)),
                skip_health_check=True,
                resource={"cpu": str(request.cpu_millis) + "m", "memory": str(request.memory_bytes)},
                metadata={"yipilot.job_id": job_id, "yipilot.profile": request.profile},
                entrypoint=["tail", "-f", "/dev/null"],
                network_policy=NetworkPolicy.model_validate({"defaultAction": "deny", "egress": [{"action": "deny", "target": "*"}]}),
                volumes=volumes,
                connection_config=self.connection)
            if box.id != store.get(job_id)[1].sandbox_id:
                raise ValueError("Server creation identity differs from reserved identity")
            await box.check_ready(timeout=timedelta(seconds=30), polling_interval=timedelta(milliseconds=200))
            facts = docker("inspect", "sandbox-" + box.id)[0]
            if (facts["Config"]["Labels"].get("yipilot.job_id") != job_id
                    or facts["Config"]["Labels"].get("yipilot.profile") != request.profile
                    or facts["Config"]["Image"] != request.image or not facts["State"]["Running"]):
                raise ValueError("Runtime ownership differs")
            self._groups[job_id] = {writer_group(facts["State"]["Pid"])}
            if store.get(job_id)[1].cancel_requested:
                return "cancelled", None, b"", b""
            handlers = ExecutionHandlers(on_stdout=lambda msg: output("stdout", msg),
                on_stderr=lambda msg: output("stderr", msg), skip_accumulation=True)
            argv = ["python", *(["-I"] if request.policy_revision == "python-isolated-script-v2" else []), "-B", "./" + request.script]
            task = asyncio.create_task(box.commands.run(argv,
                opts=RunCommandOpts(working_directory="/workspace", uid=10001, gid=10001,
                    envs=envs or None,
                    timeout=timedelta(seconds=request.timeout_seconds)), handlers=handlers))
            deadline = time.monotonic() + request.timeout_seconds
            while not task.done():
                if store.get(job_id)[1].cancel_requested:
                    reason = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    reason = "timed_out"
                    break
                if shutil.disk_usage(work.parent).free < self.disk_reserve:
                    reason = "runtime_error"
                    break
                await asyncio.sleep(.05)
            if reason != "exited":
                return reason, None, bytes(streams["stdout"]), bytes(streams["stderr"])
            execution = await task
            error = getattr(execution, "error", None)
            if error and any(word in str(error.name).lower().replace(" ", "") for word in ("timeout", "timedout")):
                reason = "timed_out"
            elif execution.exit_code is None:
                reason = "runtime_error"
            return reason, execution.exit_code, bytes(streams["stdout"]), bytes(streams["stderr"])
        except Exception:
            if box is None:
                raise
            return reason if reason != "exited" else "runtime_error", None, bytes(streams["stdout"]), bytes(streams["stderr"])
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if box is not None:
                await box.close()

    def cleanup(self, job_id: str, sandbox_id: str | None) -> None:
        if sandbox_id is None:
            raise ValueError("Creation outcome unknown; retain budget for reconciliation")
        if re.fullmatch(r"[0-9a-f-]{36}", sandbox_id) is None:
            raise ValueError("Invalid sandbox identity")
        with FileLock(JOBS_ROOT / job_id / "runtime.lock", timeout=45):
            record = JOBS_ROOT / job_id / "runtime.json"
            require_plain_path(record)
            state = None
            if record.exists():
                value = json.loads(record.read_text())
                state = value.get("state")
                if (value != {"job_id": job_id, "sandbox_id": str(UUID(job_id)), "state": state}
                        or state not in {"authorized", "consumed", "settled", "revoked", "revoked_unsettled"}):
                    raise ValueError("Invalid creation ownership record")
            unsettled = state in {"consumed", "revoked_unsettled"}
            lease(job_id, "revoked_unsettled" if unsettled else "revoked")
            self._remove(job_id, sandbox_id)
            if unsettled:
                raise ValueError("Server creation lost its completion proof; retain reservation")

    def _remove(self, job_id: str, sandbox_id: str) -> None:
        names = subprocess.run(["docker", "ps", "-aq", "--filter", "label=yipilot.job_id=" + job_id],
                               check=True, capture_output=True, timeout=15).stdout.decode().split()
        if names:
            facts = docker("inspect", *names)
            if len(facts) != 1 or facts[0]["Name"] != "/sandbox-" + sandbox_id:
                raise ValueError("Unexpected containers for Job")
            if facts[0]["State"]["Running"]:
                self._groups.setdefault(job_id, set()).add(writer_group(facts[0]["State"]["Pid"]))
        owned = subprocess.run(["docker", "ps", "-aq", "--filter", "name=^/sandbox-" + sandbox_id + "$",
                                "--filter", "name=^/sandbox-egress-" + sandbox_id + "$"],
                               check=True, capture_output=True, timeout=15).stdout.decode().split()
        facts = docker("inspect", *owned) if owned else []
        for container in facts:
            labels = container["Config"]["Labels"]
            primary = (container["Name"] == "/sandbox-" + sandbox_id and labels.get("yipilot.job_id") == job_id
                       and labels.get("yipilot.profile") in IMAGES
                       and container["Config"]["Image"] == IMAGES[labels["yipilot.profile"]])
            sidecar = (container["Name"] == "/sandbox-egress-" + sandbox_id
                       and labels.get("opensandbox.io/egress-sidecar-for") == sandbox_id)
            if not (primary or sidecar):
                raise ValueError("Container ownership differs")
            if container["State"]["Running"]:
                self._groups.setdefault(job_id, set()).add(writer_group(container["State"]["Pid"]))
        volume_name = "opensandbox-runtime-" + sandbox_id
        volumes = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "name=^" + volume_name + "$"],
                                 check=True, capture_output=True, timeout=15).stdout.decode().split()
        if volumes and (volumes != [volume_name] or
                        docker("volume", "inspect", volume_name)[0].get("Labels", {}).get("opensandbox.io/volume-managed-by") != "server"):
            raise ValueError("Volume ownership differs")
        try:
            asyncio.run(self._kill(sandbox_id))
        except Exception:  # noqa: BLE001, S110 -- Verify host absence before accepting an earlier deletion.
            pass
        # The API cannot delete a sidecar-only creation; verified IDs remain safe during a server outage.
        for container in facts:
            if subprocess.run(["docker", "ps", "-aq", "--filter", "id=" + container["Id"]],
                              check=True, capture_output=True, timeout=15).stdout.strip():
                subprocess.run(["docker", "rm", "-f", container["Id"]], check=True, capture_output=True, timeout=30)
        if volumes and subprocess.run(["docker", "volume", "ls", "-q", "--filter", "name=^" + volume_name + "$"],
                                      check=True, capture_output=True, timeout=15).stdout.strip():
            subprocess.run(["docker", "volume", "rm", volume_name], check=True, capture_output=True, timeout=15)
        remaining = subprocess.run(["docker", "ps", "-aq", "--filter", "name=sandbox-" + sandbox_id,
                                    "--filter", "name=sandbox-egress-" + sandbox_id],
                                   check=True, capture_output=True, timeout=15).stdout.strip()
        volume_residue = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "name=opensandbox-runtime-" + sandbox_id],
                                 check=True, capture_output=True, timeout=15).stdout.strip()
        groups = self._groups.get(job_id, set())
        if remaining or volume_residue or any(group.exists() and "populated 1" in (group / "cgroup.events").read_text() for group in groups):
            raise ValueError("Writers or resources remain")
        self._groups.pop(job_id, None)

    async def _kill(self, sandbox_id: str) -> None:
        from opensandbox import SandboxManager  # type: ignore[import-not-found]
        from opensandbox.transport import RetryPolicy  # type: ignore[import-not-found]
        # A paused API must reach host cleanup before SDK retries consume the cancellation budget.
        connection = self.connection.model_copy(update={"request_timeout": timedelta(seconds=5),
                                                       "retry_policy": RetryPolicy.disabled()})
        manager = await SandboxManager.create(connection)
        try:
            await asyncio.wait_for(manager.kill_sandbox(sandbox_id), timeout=10)
        finally:
            await asyncio.wait_for(manager.close(), timeout=5)
