"""Input acceptance, budget admission and trusted post-stop collection in one owner."""

import hashlib
import os
import re
import shutil
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, Protocol
from uuid import uuid4

from yipilot.domain.errors import DomainError
from yipilot.domain.remote_execution import (
    MAX_DIAGNOSTIC_BYTES,
    JobDiagnostics,
    JobRequest,
    JobResult,
    JobStatus,
    manifest_hash,
)
from yipilot.persistence.filesystem import atomic_write_bytes
from yipilot.runner.store import JobStore
from yipilot.security.paths import require_plain_path
from yipilot.security.redaction import SecretRedactor
from yipilot.workspace.remote_bundle import publish_result, unpack_input
from yipilot.workspace.sandbox_sources import scan_source_tree


class Runtime(Protocol):
    def doctor(self) -> None: ...
    def prepare_work(self, job_id: str, work: Path, store: JobStore) -> None: ...
    def execute(self, job_id: str, request: JobRequest, work: Path, store: JobStore) -> tuple[str, int | None, bytes, bytes]: ...
    def cleanup(self, job_id: str, sandbox_id: str | None) -> None: ...
    def release_work(self, job_id: str, work: Path) -> None: ...


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Runner:
    def __init__(self, root: Path, runtime: Runtime, *, max_slots: int = 4,
                 memory_budget: int = 768 * 1024**2, cpu_budget: int = 1500,
                 disk_reserve: int = 4 * 1024**3, retained_jobs: int = 32,
                 retention_seconds: int = 86400) -> None:
        if (not 1 <= max_slots <= 4 or memory_budget <= 0 or cpu_budget <= 0 or disk_reserve < 0
                or retained_jobs < 1 or retention_seconds < 1):
            raise ValueError("Invalid admission limits")
        self.root, self.runtime = root, runtime
        self.store = JobStore(root)
        self.jobs = root / "jobs"
        self.staging = root / "staging"
        self.jobs.mkdir(mode=0o700, exist_ok=True)
        self.staging.mkdir(mode=0o700, exist_ok=True)
        self.max_slots, self.memory_budget, self.cpu_budget = max_slots, memory_budget, cpu_budget
        self.disk_reserve = disk_reserve
        self.retained_jobs, self.retention_seconds = retained_jobs, retention_seconds
        self._lock = threading.RLock()
        self._collection_lock = threading.Lock()
        self._downloads: dict[str, int] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def submit(self, bundle: Path) -> JobStatus:
        with self._lock, TemporaryDirectory(dir=self.staging, prefix="input-") as temporary:
            staged = Path(temporary) / "job"
            staged.mkdir(mode=0o700)
            with self._collection_lock:
                request = unpack_input(bundle, staged / "input")
            previous = self.store.lookup(request)
            if previous is not None:
                return previous
            self._collect_expired(time.time(), make_room=True)
            if len(self.store.list()) >= self.retained_jobs:
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
            status = self.store.status(job_id)
            if status.state == "queued":
                return self.store.update(job_id, state="cancelled", cancel=True)
            if status.state == "running":
                return self.store.update(job_id, cancel=True)
            return status

    def result_path(self, job_id: str) -> Path:
        _, status = self.store.get(job_id)
        if status.state not in {"succeeded", "failed", "cancelled"} or status.error_code == "OUTPUT_INVALID":
            raise DomainError("RESULT_UNAVAILABLE", "No complete trusted result")
        path = self.jobs / job_id / "artifacts" / "result.zip"
        if not path.is_file():
            raise DomainError("RESULT_UNAVAILABLE", "No complete trusted result")
        return path

    def diagnostics(self, job_id: str) -> JobDiagnostics:
        with self._lock:
            request, status = self.store.get(job_id)
            path = self.jobs / job_id / "diagnostics.json"
            require_plain_path(path)
            if status.state != "failed" or status.error_code != "OUTPUT_INVALID" or not path.is_file():
                raise DomainError("DIAGNOSTICS_UNAVAILABLE", "No retained output-failure diagnostics")
            with path.open("rb") as source:
                raw = source.read(MAX_DIAGNOSTIC_BYTES + 1)
            if len(raw) > MAX_DIAGNOSTIC_BYTES:
                raise ValueError("Diagnostic document exceeds budget")
            diagnostic = JobDiagnostics.model_validate_json(raw)
            if (diagnostic.job_id != job_id or diagnostic.request_fingerprint != request.fingerprint
                    or diagnostic.sandbox_id != status.sandbox_id
                    or len(diagnostic.stdout.encode()) > request.stdout_bytes
                    or len(diagnostic.stderr.encode()) > request.stderr_bytes):
                raise ValueError("Diagnostic Job binding differs")
            return diagnostic

    @contextmanager
    def open_result(self, job_id: str) -> Iterator[BinaryIO]:
        with self._lock:
            stream = self.result_path(job_id).open("rb")
            self._downloads[job_id] = self._downloads.get(job_id, 0) + 1
        try:
            with stream:
                yield stream
        finally:
            with self._lock:
                self._downloads[job_id] -= 1
                if not self._downloads[job_id]:
                    del self._downloads[job_id]

    def collect_expired(self, *, now: float | None = None) -> None:
        with self._lock:
            self._collect_expired(time.time() if now is None else now)

    def _collect_expired(self, now: float, *, make_room: bool = False) -> None:
        retained = len(self.store.list())
        for job_id, finished in self.store.finished():
            if (job_id in self._downloads or (finished + self.retention_seconds > now
                    and not (make_room and retained >= self.retained_jobs))):
                continue
            work = self.jobs / job_id / "work"
            if os.path.ismount(work):
                raise ValueError("Refusing retention of a mounted Job")
            self.store.expire(job_id)
            retained -= 1
        for job_id in self.store.pending_deletions():
            if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
                raise ValueError("Invalid owned Job directory")
            target = self.jobs / job_id
            require_plain_path(target)
            if os.path.ismount(target / "work"):
                raise ValueError("Refusing deletion of a mounted Job")
            if target.exists():
                if os.name == "nt":
                    for path in (target / "input").rglob("*"):
                        require_plain_path(path)
                        if path.is_file():
                            path.chmod(0o600)
                shutil.rmtree(target)
            sync_directory(self.jobs)
            self.store.deleted(job_id)

    def start(self) -> None:
        if self._thread is not None:
            raise ValueError("Runner already started")
        for _, status in self.store.list():
            if status.state in {"running", "cleanup_failed"}:
                try:
                    self.runtime.cleanup(status.job_id, status.sandbox_id)
                    self.runtime.release_work(status.job_id, self.jobs / status.job_id / "work")
                except Exception:  # noqa: BLE001 -- Unknown runtime failures retain admission budget.
                    self.store.update(status.job_id, state="cleanup_failed", error_code="RECONCILIATION_FAILED")
                else:
                    self.store.update(status.job_id, state="uncertain", error_code="INTERRUPTED_ATTEMPT")
        self.collect_expired()
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
        next_collection = time.monotonic() + 60
        with ThreadPoolExecutor(max_workers=self.max_slots) as pool:
            while not self._stop.is_set():
                with self._lock:
                    if time.monotonic() >= next_collection:
                        self._collect_expired(time.time())
                        next_collection = time.monotonic() + 60
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
            self.runtime.prepare_work(job_id, work, self.store)
            shutil.copytree(root / "input", work, dirs_exist_ok=True)
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
        state = "uncertain"
        try:
            if outcome is None:
                self.store.update(job_id, error_code="EXECUTION_UNCERTAIN")
                return
            with self._collection_lock:
                self._publish(job_id, request, work, started, outcome)
            reason, exit_code, _, _ = outcome
            state = "cancelled" if reason == "cancelled" else (
                "succeeded" if reason == "exited" and exit_code == 0 else "failed")
        except Exception:  # noqa: BLE001 -- Never publish partial or unverifiable output.
            state = "failed"
            self.store.update(job_id, error_code="OUTPUT_INVALID")
            if outcome is not None:
                try:
                    self._save_diagnostics(job_id, request, started, outcome)
                except (OSError, ValueError, DomainError):
                    pass  # Diagnostic persistence must not bypass cleanup or kill dispatch.
        finally:
            try:
                self.runtime.release_work(job_id, work)
            except Exception:  # noqa: BLE001 -- Mounted storage retains the entire reservation.
                self.store.update(job_id, state="cleanup_failed", error_code="WORK_RELEASE_FAILED")
            else:
                self.store.update(job_id, state=state)  # type: ignore[arg-type]
            self._wake.set()

    def _save_diagnostics(self, job_id: str, request: JobRequest, started: datetime,
                          outcome: tuple[str, int | None, bytes, bytes]) -> None:
        reason, exit_code, stdout, stderr = outcome
        logs = []
        for name, data in (("stdout", stdout), ("stderr", stderr)):
            limit = getattr(request, name + "_bytes")
            text = SecretRedactor().redact_text(data[:limit].decode("utf-8", errors="replace"))
            logs.append(text.encode("utf-8")[:limit].decode("utf-8", errors="ignore"))
        diagnostic = JobDiagnostics(job_id=job_id, request_fingerprint=request.fingerprint,
            sandbox_id=self.store.status(job_id).sandbox_id, reason=reason, exit_code=exit_code,  # type: ignore[arg-type]
            started_at=started, ended_at=datetime.now(UTC), stdout=logs[0], stderr=logs[1])
        atomic_write_bytes(self.jobs / job_id / "diagnostics.json", diagnostic.model_dump_json().encode())

    def _publish(self, job_id: str, request: JobRequest, work: Path, started: datetime,
                 outcome: tuple[str, int | None, bytes, bytes]) -> None:
        reason, exit_code, stdout, stderr = outcome
        files, contents = scan_source_tree(work, request.files)
        old, new = {f.path: f for f in request.files}, {f.path: f for f in files}
        changed = tuple(sorted(p for p in new if old.get(p) != new[p]))
        status = self.store.get(job_id)[1]
        result = JobResult(job_id=job_id, request_fingerprint=request.fingerprint,
            profile=request.profile, image=request.image,
            input_manifest_hash=manifest_hash(request.files), output_manifest_hash=manifest_hash(files),
            sandbox_id=status.sandbox_id or "", reason=reason, exit_code=exit_code,  # type: ignore[arg-type]
            started_at=started, ended_at=datetime.now(UTC), files=files,
            deleted=tuple(sorted(old.keys() - new.keys())), changed=changed,
            stdout_sha256=hashlib.sha256(stdout).hexdigest(), stderr_sha256=hashlib.sha256(stderr).hexdigest(),
            stdout_bytes=len(stdout), stderr_bytes=len(stderr))
        artifacts = work.parent / "artifacts"
        artifacts.mkdir(mode=0o700)
        publish_result(artifacts / "result.zip", result, {p: contents[p] for p in changed}, stdout, stderr)
        sync_directory(artifacts)
