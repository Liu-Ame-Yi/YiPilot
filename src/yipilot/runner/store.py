"""One durable Job record per request key; execution attempts are never replayed."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from yipilot.domain.errors import DomainError
from yipilot.domain.remote_execution import JobRequest, JobState, JobStatus
from yipilot.security.paths import require_plain_path

TERMINAL = {"succeeded", "failed", "cancelled", "uncertain"}


class JobStore:
    def __init__(self, root: Path) -> None:
        require_plain_path(root)
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = root / "jobs.db"
        require_plain_path(self.path)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL)")
            if version in (0, 1):
                db.execute("ALTER TABLE jobs ADD COLUMN finished REAL")
                db.execute("ALTER TABLE jobs ADD COLUMN gc_state TEXT NOT NULL DEFAULT 'retained'")
                for job_id, encoded in db.execute("SELECT id,status FROM jobs").fetchall():
                    if JobStatus.model_validate_json(encoded).state in TERMINAL:
                        db.execute("UPDATE jobs SET finished=? WHERE id=?", (time.time(), job_id))
                db.execute("CREATE INDEX jobs_retained ON jobs(finished) WHERE request<>''")
                db.execute("PRAGMA user_version=2")
            elif version != 2:
                raise DomainError("STORAGE_VERSION", "Unsupported Runner database")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db

    def lookup(self, request: JobRequest) -> JobStatus | None:
        with self.transaction() as db:
            row = db.execute("SELECT fingerprint,status FROM jobs WHERE request_key=?", (request.request_key,)).fetchone()
        if row is None:
            return None
        if row[0] != request.fingerprint:
            raise DomainError("REQUEST_CONFLICT", "Request key already binds different content")
        return JobStatus.model_validate_json(row[1])

    def enqueue(self, job_id: str, request: JobRequest) -> JobStatus:
        status = JobStatus(job_id=job_id, request_fingerprint=request.fingerprint, state="queued")
        with self.transaction() as db:
            db.execute("INSERT INTO jobs(id,request_key,fingerprint,request,status) VALUES (?,?,?,?,?)", (job_id, request.request_key,
                       request.fingerprint, request.model_dump_json(), status.model_dump_json()))
        return status

    def get(self, job_id: str) -> tuple[JobRequest, JobStatus]:
        with self.transaction() as db:
            row = db.execute("SELECT request,status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise DomainError("JOB_NOT_FOUND", "Job not found")
        if not row[0]:
            raise DomainError("RESULT_EXPIRED", "Job payload has expired; request key remains reserved")
        return JobRequest.model_validate_json(row[0]), JobStatus.model_validate_json(row[1])

    def status(self, job_id: str) -> JobStatus:
        with self.transaction() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise DomainError("JOB_NOT_FOUND", "Job not found")
        return JobStatus.model_validate_json(row[0])

    def list(self) -> list[tuple[JobRequest, JobStatus]]:
        with self.transaction() as db:
            rows = db.execute("SELECT request,status FROM jobs WHERE request<>'' ORDER BY rowid").fetchall()
        return [(JobRequest.model_validate_json(r), JobStatus.model_validate_json(s)) for r, s in rows]

    def finished(self) -> Sequence[tuple[str, float]]:
        with self.transaction() as db:
            return db.execute("SELECT id,finished FROM jobs WHERE request<>'' AND finished IS NOT NULL ORDER BY finished,rowid").fetchall()

    def expire(self, job_id: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT status,finished FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row[1] is None:
                raise ValueError("Only completed Jobs may expire")
            values = JobStatus.model_validate_json(row[0]).model_dump()
            values["result_expired"] = True
            status = JobStatus.model_validate(values)
            db.execute("UPDATE jobs SET request='',status=?,gc_state='pending' WHERE id=?", (status.model_dump_json(), job_id))

    def pending_deletions(self) -> Sequence[str]:
        with self.transaction() as db:
            return [row[0] for row in db.execute("SELECT id FROM jobs WHERE gc_state='pending'")]

    def deleted(self, job_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE jobs SET gc_state='removed' WHERE id=? AND gc_state='pending'", (job_id,))

    def update(self, job_id: str, *, state: JobState | None = None, sandbox_id: str | None = None,
               error_code: str | None = None, cancel: bool = False) -> JobStatus:
        with self.transaction() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise DomainError("JOB_NOT_FOUND", "Job not found")
            values = JobStatus.model_validate_json(row[0]).model_dump()
            if state is not None:
                values["state"] = state
            if sandbox_id is not None:
                values["sandbox_id"] = sandbox_id
            if error_code is not None:
                values["error_code"] = error_code
            values["cancel_requested"] |= cancel
            status = JobStatus.model_validate(values)
            db.execute("UPDATE jobs SET status=?,finished=CASE WHEN ? THEN COALESCE(finished,?) ELSE finished END WHERE id=?",
                       (status.model_dump_json(), status.state in TERMINAL, time.time(), job_id))
        return status
