"""SQLite task state; the historical FileTaskStore name remains source compatible."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar
from uuid import UUID, uuid4

from filelock import FileLock
from pydantic import BaseModel

from yipilot.domain.errors import DomainError
from yipilot.domain.models import RunStatus, transition_run, validate_task_id
from yipilot.domain.sandbox import ImageDigest
from yipilot.persistence.database import Database
from yipilot.persistence.filesystem import project_path


class RepoMetadataRecord(BaseModel):
    schema_version: Literal[1] = 1
    task_id: str
    alias: str
    normalized_url: str
    url_hash: str
    cache_path: str
    head_sha: str
    synced_at: str


class ProjectRecord(BaseModel):
    schema_version: Literal[1] = 1
    sandbox_image_digest: ImageDigest | None = None


class TaskRecord(BaseModel):
    schema_version: Literal[1] = 1
    task_id: str
    created_at: str
    title: str = ""
    development_goal: str = ""


class OperationRecord(BaseModel):
    schema_version: Literal[1] = 1
    operation_id: str
    task_id: str
    operation_type: str
    idempotency_key: str
    request_json: str
    created_at: str


class RunRecord(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    task_id: str
    operation_id: str
    stage: str
    status: RunStatus = RunStatus.PENDING
    error_code: str | None = None
    legacy: bool = False
    result_artifact_ids: tuple[str, ...] = ()
    created_at: str


RecordModel = TypeVar("RecordModel", bound=BaseModel)


class FileTaskStore:
    """SQLite is the only metadata authority. Files contain execution evidence."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = Path(project_root)
        self.db = Database(self._project_root)

    def initialize_project(self) -> ProjectRecord:
        runtime = self._runtime_root()
        runtime.mkdir(exist_ok=True)
        with FileLock(str(self._runtime_path(".bootstrap.lock"))):
            if not self.db.path.exists() and self._runtime_path("config.yaml").exists():
                raise DomainError("MIGRATION_REQUIRED", "run explicit storage migration first")
            for parts in (("locks",), ("tasks",), ("cache", "repos"), ("audit",)):
                self._runtime_path(*parts).mkdir(parents=True, exist_ok=True)
            self.db.initialize()
            with self.db.transaction() as connection:
                connection.execute("INSERT OR IGNORE INTO project VALUES ('project',?)", (ProjectRecord().model_dump_json(),))
        return self.get_project()

    def get_project(self) -> ProjectRecord:
        return self._get("project", "project", ProjectRecord, "PROJECT_NOT_INITIALIZED")

    def set_sandbox_image_digest(self, image_digest: str) -> ProjectRecord:
        with self.db.transaction() as connection:
            updated = ProjectRecord(sandbox_image_digest=image_digest)
            connection.execute("UPDATE project SET body=? WHERE id='project'", (updated.model_dump_json(),))
        return updated

    def create_task(self, task_id: str, *, title: str = "", development_goal: str = "") -> TaskRecord:
        validate_task_id(task_id)
        with self.db.transaction() as connection:
            task = TaskRecord(task_id=task_id, created_at=_timestamp(), title=title, development_goal=development_goal)
            connection.execute("INSERT OR IGNORE INTO tasks VALUES (?,?)", (task_id, task.model_dump_json()))
            for part in ("operations", "runs"):
                self._runtime_path("tasks", task_id, part).mkdir(parents=True, exist_ok=True)
        return self.get_task(task_id)

    def create_next_task(self, title: str, development_goal: str) -> TaskRecord:
        with self.db.transaction():
            next_id = max((int(t.task_id) for t in self.list_tasks()), default=0) + 1
            return self.create_task(str(next_id), title=title, development_goal=development_goal)

    def get_task(self, task_id: str) -> TaskRecord:
        validate_task_id(task_id)
        return self._get("tasks", task_id, TaskRecord, "TASK_NOT_FOUND")

    def list_tasks(self) -> list[TaskRecord]:
        with self.db.transaction() as connection:
            tasks = [TaskRecord.model_validate_json(row[0]) for row in connection.execute("SELECT body FROM tasks")]
        return sorted(tasks, key=lambda t: (t.created_at, int(t.task_id)))

    def find_operation(self, task_id: str, key: str) -> OperationRecord | None:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT body FROM operations WHERE task_id=? AND request_key=?", (task_id, key)).fetchone()
            return OperationRecord.model_validate_json(row[0]) if row else None

    def create_operation(self, task_id: str, operation_type: str, idempotency_key: str,
                         payload: Mapping[str, object]) -> OperationRecord:
        if not operation_type or not idempotency_key:
            raise DomainError("OPERATION_CONFLICT", "operation type and idempotency key are required")
        request_json = _canonical_request(operation_type, payload)
        with self.db.transaction() as connection:
            self.get_task(task_id)
            previous = self.find_operation(task_id, idempotency_key)
            if previous:
                if previous.request_json != request_json:
                    raise DomainError("OPERATION_CONFLICT", "idempotency key is already associated with a different request")
                return previous
            operation = OperationRecord(operation_id=str(uuid4()), task_id=task_id,
                operation_type=operation_type, idempotency_key=idempotency_key,
                request_json=request_json, created_at=_timestamp())
            connection.execute("INSERT INTO operations VALUES (?,?,?,?,?)",
                (operation.operation_id, task_id, idempotency_key, hashlib.sha256(request_json.encode()).hexdigest(), operation.model_dump_json()))
            return operation

    def get_operation(self, task_id: str, operation_id: str) -> OperationRecord:
        _validate_uuid(operation_id, "OPERATION_NOT_FOUND")
        operation = self._get("operations", operation_id, OperationRecord, "OPERATION_NOT_FOUND")
        if operation.task_id != task_id:
            raise DomainError("OPERATION_NOT_FOUND", "operation does not belong to task")
        return operation

    def create_run(self, task_id: str, operation_id: str, stage: str) -> RunRecord:
        with self.db.transaction() as connection:
            self.get_operation(task_id, operation_id)
            if not stage:
                raise DomainError("PRECONDITION_FAILED", "stage is required")
            attempt = connection.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM runs WHERE operation_id=? AND stage=?", (operation_id, stage)).fetchone()[0]
            run = RunRecord(run_id=str(uuid4()), task_id=task_id, operation_id=operation_id, stage=stage, created_at=_timestamp())
            connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (run.run_id, task_id, operation_id, stage, attempt, run.model_dump_json()))
            self._runtime_path("tasks", task_id, "runs", run.run_id).mkdir(parents=True, exist_ok=True)
            return run

    def get_or_create_run(self, task_id: str, operation_id: str, stage: str) -> tuple[RunRecord, bool]:
        with self.db.transaction() as connection:
            self.get_operation(task_id, operation_id)
            rows = connection.execute("SELECT body FROM runs WHERE operation_id=? AND stage=? ORDER BY attempt", (operation_id, stage)).fetchall()
            if len(rows) > 1:
                raise DomainError("STORAGE_INVALID", "operation has multiple runs for one stage")
            if rows:
                return RunRecord.model_validate_json(rows[0][0]), False
            return self.create_run(task_id, operation_id, stage), True

    def get_run(self, task_id: str, run_id: str) -> RunRecord:
        run = self.find_run(run_id)
        if run.task_id != task_id:
            raise DomainError("RUN_NOT_FOUND", "run does not belong to task")
        return run

    def find_run(self, run_id: str) -> RunRecord:
        _validate_uuid(run_id, "RUN_NOT_FOUND")
        return self._get("runs", run_id, RunRecord, "RUN_NOT_FOUND")

    def list_runs(self, task_id: str) -> list[RunRecord]:
        self.get_task(task_id)
        with self.db.transaction() as connection:
            runs = [RunRecord.model_validate_json(row[0]) for row in connection.execute("SELECT body FROM runs WHERE task_id=?", (task_id,))]
        return sorted(runs, key=lambda r: (r.created_at, r.run_id))

    def set_run_status(self, run_id: str, status: RunStatus | str, *, error_code: str | None = None, event_payload: Mapping[str, object] | None = None) -> RunRecord:
        # State and lifecycle event commit together; no external execution in this transaction.
        from yipilot.persistence.artifact_store import FileArtifactStore
        from yipilot.persistence.event_journal import JsonlEventJournal

        try:
            target = RunStatus(status)
        except ValueError as error:
            raise DomainError("RUN_TRANSITION_INVALID", "unknown run status") from error
        with self.db.transaction() as connection:
            run = self.find_run(run_id)
            if run.status == target:
                return run
            transition_run(run.status, target)
            result_ids = run.result_artifact_ids
            if target == RunStatus.SUCCEEDED:
                artifacts = FileArtifactStore(self)
                records = artifacts.list(run.task_id, run_id)
                required = {
                    "code_generate": {"artifacts/agent-plan.json", "artifacts/patch-proposal.json"},
                    "code_repair": {"artifacts/patch-proposal.json", "artifacts/repair-chain.json"},
                    "code_apply": {"artifacts/apply-evidence.json"},
                    "sandbox_apply": {"sandbox-apply/result.json", "sandbox-output/evidence.json"},
                    "sandbox_verification": {"artifacts/change-handoff.json"},
                    "change_verification": {"artifacts/change-handoff.json"},
                    "quality_artifacts": {"artifacts/quality-metadata.json"},
                    "unit_test": {"artifacts/pytest-report.json"},
                }.get(run.stage, set())
                if required - {record.relative_path for record in records}:
                    raise DomainError("RUN_EVIDENCE_MISSING", "required result artifacts are not registered")
                result_ids = tuple(record.artifact_id for record in records)
                for record in records:
                    artifacts.verify(run.task_id, run_id, record.artifact_id)
            updated = run.model_copy(update={"status": target, "error_code": error_code, "result_artifact_ids": result_ids})
            connection.execute("UPDATE runs SET body=? WHERE id=?", (updated.model_dump_json(), run_id))
            event_type = "run.started" if target == RunStatus.RUNNING else f"run.{target.value}"
            JsonlEventJournal(self.event_journal_path(run_id), task_id=run.task_id, run_id=run_id, stage=run.stage).append(
                event_type, event_payload if event_payload is not None else ({"error_code": error_code} if error_code else {}))
            return updated

    def event_journal_path(self, run_id: str) -> Path:
        """Compatibility locator for run files; events themselves live in SQLite."""
        run = self.find_run(run_id)
        return self._runtime_path("tasks", run.task_id, "runs", run.run_id, "events.jsonl")

    def _runtime_root(self) -> Path:
        return self._runtime_path()

    def _runtime_path(self, *parts: str) -> Path:
        return project_path(self._project_root, ".yipilot", *parts)

    def _get(self, table: str, identifier: str, model: type[RecordModel], missing: str) -> RecordModel:
        with self.db.transaction() as connection:
            row = connection.execute(f"SELECT body FROM {table} WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise DomainError(missing, "record not found")
            try:
                return model.model_validate_json(row[0])
            except ValueError as error:
                raise DomainError("STORAGE_INVALID", "record is invalid") from error


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_request(operation_type: str, payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            {"operation_type": operation_type, "payload": dict(payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise DomainError("OPERATION_CONFLICT", "operation payload must be JSON serializable") from error


def _validate_uuid(value: str, error_code: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as error:
        raise DomainError(error_code, "invalid record identifier") from error
