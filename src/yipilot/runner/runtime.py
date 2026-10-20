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

from yipilot.domain.remote_execution import IMAGE, PROFILE, JobRequest
from yipilot.runner.store import JobStore
from yipilot.security.paths import require_plain_path


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
        self._groups: dict[str, Path] = {}

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
            box = await Sandbox.create(IMAGE, timeout=timedelta(seconds=min(900, request.timeout_seconds + 120)),
                skip_health_check=True,
                resource={"cpu": str(request.cpu_millis) + "m", "memory": str(request.memory_bytes)},
                metadata={"yipilot.job_id": job_id, "yipilot.profile": PROFILE},
                entrypoint=["tail", "-f", "/dev/null"],
                network_policy=NetworkPolicy.model_validate({"defaultAction": "deny", "egress": [{"action": "deny", "target": "*"}]}),
                volumes=[Volume(name="work", host=Host(path=str(work)), mountPath="/workspace")],
                connection_config=self.connection)
            store.update(job_id, sandbox_id=box.id)
            await box.check_ready(timeout=timedelta(seconds=30), polling_interval=timedelta(milliseconds=200))
            facts = docker("inspect", "sandbox-" + box.id)[0]
            if facts["Config"]["Labels"].get("yipilot.job_id") != job_id or not facts["State"]["Running"]:
                raise ValueError("Runtime ownership differs")
            self._groups[job_id] = writer_group(facts["State"]["Pid"])
            if store.get(job_id)[1].cancel_requested:
                return "cancelled", None, b"", b""
            handlers = ExecutionHandlers(on_stdout=lambda msg: output("stdout", msg),
                on_stderr=lambda msg: output("stderr", msg), skip_accumulation=True)
            task = asyncio.create_task(box.commands.run(["python", "-B", "./" + request.script],
                opts=RunCommandOpts(working_directory="/workspace", uid=10001, gid=10001,
                    timeout=timedelta(seconds=request.timeout_seconds)), handlers=handlers))
            deadline = time.monotonic() + request.timeout_seconds
            while not task.done():
                if store.get(job_id)[1].cancel_requested:
                    reason = "cancelled"
                    break
                if time.monotonic() >= deadline:
                    reason = "timed_out"
                    break
                if shutil.disk_usage(work).free < self.disk_reserve:
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
        names = subprocess.run(["docker", "ps", "-aq", "--filter", "label=yipilot.job_id=" + job_id],
                               check=True, capture_output=True, timeout=15).stdout.decode().split()
        if names:
            facts = docker("inspect", *names)
            if len(facts) != 1 or facts[0]["Name"] != "/sandbox-" + sandbox_id:
                raise ValueError("Unexpected containers for Job")
            if facts[0]["State"]["Running"]:
                self._groups[job_id] = writer_group(facts[0]["State"]["Pid"])
        try:
            asyncio.run(self._kill(sandbox_id))
        except Exception:  # noqa: BLE001, S110 -- Verify host absence before accepting an earlier deletion.
            pass
        remaining = subprocess.run(["docker", "ps", "-aq", "--filter", "name=sandbox-" + sandbox_id,
                                    "--filter", "name=sandbox-egress-" + sandbox_id],
                                   check=True, capture_output=True, timeout=15).stdout.strip()
        volumes = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "name=opensandbox-runtime-" + sandbox_id],
                                 check=True, capture_output=True, timeout=15).stdout.strip()
        group = self._groups.pop(job_id, None)
        if remaining or volumes or (group is not None and group.exists()
                                   and "populated 1" in (group / "cgroup.events").read_text()):
            raise ValueError("Writers or resources remain")

    async def _kill(self, sandbox_id: str) -> None:
        from opensandbox import SandboxManager  # type: ignore[import-not-found]
        manager = await SandboxManager.create(self.connection)
        try:
            await asyncio.wait_for(manager.kill_sandbox(sandbox_id), timeout=30)
        finally:
            await manager.close()
