"""Input acceptance, budget admission and trusted post-stop collection in one owner."""

import hashlib
import os
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from uuid import uuid4

from yipilot.domain.errors import DomainError
from yipilot.domain.remote_execution import (
    JobRequest,
    JobResult,
    JobStatus,
    manifest_hash,
)
from yipilot.runner.store import JobStore
from yipilot.workspace.remote_bundle import publish_result, unpack_input
from yipilot.workspace.sandbox_sources import scan_source_tree


class Runtime(Protocol):
    def execute(self, job_id: str, request: JobRequest, work: Path, store: JobStore) -> tuple[str, int | None, bytes, bytes]: ...
    def cleanup(self, job_id: str, sandbox_id: str | None) -> None: ...


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Runner:
    def __init__(self, root: Path, runtime: Runtime, *, max_slots: int = 4,
                 memory_budget: int = 1280 * 1024**2, cpu_budget: int = 1500,
                 disk_reserve: int = 4 * 1024**3) -> None:
        if not 1 <= max_slots <= 4 or memory_budget <= 0 or cpu_budget <= 0 or disk_reserve < 0:
            raise ValueError("Invalid admission limits")
        self.root, self.runtime = root, runtime
        self.store = JobStore(root)
        self.jobs = root / "jobs"
        self.staging = root / "staging"
        self.jobs.mkdir(mode=0o700, exist_ok=True)
        self.staging.mkdir(mode=0o700, exist_ok=True)
        self.max_slots, self.memory_budget, self.cpu_budget = max_slots, memory_budget, cpu_budget
        self.disk_reserve = disk_reserve
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def submit(self, bundle: Path) -> JobStatus:
        with self._lock, TemporaryDirectory(dir=self.staging, prefix="input-") as temporary:
            staged = Path(temporary) / "job"
            staged.mkdir(mode=0o700)
            request = unpack_input(bundle, staged / "input")
            previous = self.store.lookup(request)
            if previous is not None:
                return previous
            # shortcut: retain at most 32 Jobs; add owned retention GC before production use.
            if len(self.store.list()) >= 32:
                raise DomainError("RUNNER_FULL", "Retained Job limit reached")
            if request.memory_bytes + 64 * 1024**2 > self.memory_budget or request.cpu_millis + 100 > self.cpu_budget:
                raise DomainError("BUDGET_UNAVAILABLE", "Job and sidecar exceed admission budget")
            if shutil.disk_usage(self.root).free < self.disk_reserve + 4 * 128 * 1024**2:
                raise DomainError("DISK_PRESSURE", "Insufficient reserved disk")
            for directory in sorted((staged / "input").rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if directory.is_file():
                    directory.chmod(0o400)
                else:
                    sync_directory(directory)
            sync_directory(staged / "input")
            sync_directory(staged)
            job_id = uuid4().hex
            os.replace(staged, self.jobs / job_id)
            sync_directory(self.jobs)
            status = self.store.enqueue(job_id, request)
            self._wake.set()
            return status

    def cancel(self, job_id: str) -> JobStatus:
        with self._lock:
            _, status = self.store.get(job_id)
            if status.state == "queued":
                return self.store.update(job_id, state="cancelled", cancel=True)
            if status.state == "running":
                return self.store.update(job_id, cancel=True)
            return status

    def result_path(self, job_id: str) -> Path:
        _, status = self.store.get(job_id)
        if status.state not in {"succeeded", "failed", "cancelled"}:
            raise DomainError("RESULT_UNAVAILABLE", "No complete trusted result")
        path = self.jobs / job_id / "artifacts" / "result.zip"
        if not path.is_file():
            raise DomainError("RESULT_UNAVAILABLE", "No complete trusted result")
        return path

    def start(self) -> None:
        if self._thread is not None:
            raise ValueError("Runner already started")
        for _, status in self.store.list():
            if status.state == "running":
                try:
                    self.runtime.cleanup(status.job_id, status.sandbox_id)
                except Exception:  # noqa: BLE001 -- Unknown runtime failures retain admission budget.
                    self.store.update(status.job_id, state="cleanup_failed", error_code="RECONCILIATION_FAILED")
                else:
                    self.store.update(status.job_id, state="uncertain", error_code="INTERRUPTED_ATTEMPT")
        self._thread = threading.Thread(target=self._loop, name="runner-dispatch")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()

    @property
    def healthy(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _loop(self) -> None:
        futures: dict[str, Future[None]] = {}
        with ThreadPoolExecutor(max_workers=self.max_slots) as pool:
            while not self._stop.is_set():
                with self._lock:
                    records = self.store.list()
                    active = [(r, s) for r, s in records if s.state in {"running", "cleanup_failed"}]
                    used_memory = sum(r.memory_bytes + 64 * 1024**2 for r, _ in active)
                    used_cpu = sum(r.cpu_millis + 100 for r, _ in active)
                    for request, status in records:
                        if (status.state != "queued" or len(active) >= self.max_slots
                                or any(s.state == "cleanup_failed" for _, s in active)
                                or used_memory + request.memory_bytes + 64 * 1024**2 > self.memory_budget
                                or used_cpu + request.cpu_millis + 100 > self.cpu_budget
                                or shutil.disk_usage(self.root).free < self.disk_reserve):
                            continue
                        self.store.update(status.job_id, state="running")
                        futures[status.job_id] = pool.submit(self._execute, status.job_id, request)
                        active.append((request, status))
                        used_memory += request.memory_bytes + 64 * 1024**2
                        used_cpu += request.cpu_millis + 100
                for job_id, future in list(futures.items()):
                    if future.done():
                        future.result()
                        del futures[job_id]
                self._wake.wait(.2)
                self._wake.clear()
            for _, status in self.store.list():
                if status.state == "running":
                    self.store.update(status.job_id, cancel=True)

    def _execute(self, job_id: str, request: JobRequest) -> None:
        root = self.jobs / job_id
        work = root / "work"
        started = datetime.now(UTC)
        outcome: tuple[str, int | None, bytes, bytes] | None = None
        try:
            shutil.copytree(root / "input", work)
            executable = {f.path for f in request.files if f.git_mode == "100755"}
            for path in [work, *work.rglob("*")]:
                path.chmod(0o700 if path.is_dir() else
                           (0o755 if path.relative_to(work).as_posix() in executable else 0o644))
            outcome = self.runtime.execute(job_id, request, work, self.store)
        except Exception:  # noqa: BLE001 -- Reconcile every failed external execution before releasing budget.
            self.store.update(job_id, error_code="EXECUTION_FAILED")
        try:
            self.runtime.cleanup(job_id, self.store.get(job_id)[1].sandbox_id)
        except Exception:  # noqa: BLE001 -- Unknown cleanup failures must quarantine the Job.
            self.store.update(job_id, state="cleanup_failed", error_code="CLEANUP_FAILED")
            return
        if outcome is None:
            self.store.update(job_id, state="uncertain", error_code="EXECUTION_UNCERTAIN")
            return
        try:
            reason, exit_code, stdout, stderr = outcome
            files, contents = scan_source_tree(work, request.files)
            old, new = {f.path: f for f in request.files}, {f.path: f for f in files}
            changed = tuple(sorted(p for p in new if old.get(p) != new[p]))
            status = self.store.get(job_id)[1]
            result = JobResult(job_id=job_id, request_fingerprint=request.fingerprint,
                input_manifest_hash=manifest_hash(request.files), output_manifest_hash=manifest_hash(files),
                sandbox_id=status.sandbox_id or "", reason=reason, exit_code=exit_code,  # type: ignore[arg-type]
                started_at=started, ended_at=datetime.now(UTC), files=files,
                deleted=tuple(sorted(old.keys() - new.keys())), changed=changed,
                stdout_sha256=hashlib.sha256(stdout).hexdigest(), stderr_sha256=hashlib.sha256(stderr).hexdigest(),
                stdout_bytes=len(stdout), stderr_bytes=len(stderr))
            artifacts = root / "artifacts"
            artifacts.mkdir(mode=0o700)
            publish_result(artifacts / "result.zip", result, {p: contents[p] for p in changed}, stdout, stderr)
            sync_directory(artifacts)
            self.store.update(job_id, state="cancelled" if reason == "cancelled" else
                              ("succeeded" if reason == "exited" and exit_code == 0 else "failed"))
        except Exception:  # noqa: BLE001 -- Never publish partial or unverifiable output.
            self.store.update(job_id, state="failed", error_code="OUTPUT_INVALID")
        finally:
            self._wake.set()
