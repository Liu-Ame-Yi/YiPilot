"""Atomic, task-scoped persistence for Yipilot durable records."""

import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar
from uuid import UUID, uuid4

import yaml  # type: ignore[import-untyped]
from filelock import FileLock
from pydantic import BaseModel, ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.models import validate_task_id
from yipilot.infrastructure.filesystem import (
    atomic_write_json,
    atomic_write_yaml,
    project_path,
)


class ProjectRecord(BaseModel):
    schema_version: Literal[1] = 1


class TaskRecord(BaseModel):
    schema_version: Literal[1] = 1
    task_id: str
    created_at: str


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
    status: Literal["pending"] = "pending"
    created_at: str


RecordModel = TypeVar("RecordModel", bound=BaseModel)


class FileTaskStore:
    """Persist facts exclusively beneath one project's ``.yipilot`` directory."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = Path(project_root)

    def initialize_project(self) -> ProjectRecord:
        """Create the runtime layout and its authoritative project configuration."""
        runtime_root = self._runtime_root()
        created: list[Path] = []
        runtime_was_missing = not runtime_root.exists()
        bootstrap_lock_path: Path | None = None
        try:
            self._create_directory(runtime_root, created)
            bootstrap_lock_path = self._runtime_path(".bootstrap.lock")
            with FileLock(str(bootstrap_lock_path)):
                config_path = self._runtime_path("config.yaml")
                if config_path.is_file():
                    return self.get_project()
                for path in (
                    self._runtime_path("locks"),
                    self._runtime_path("tasks"),
                    self._runtime_path("cache"),
                    self._runtime_path("cache", "repos"),
                    self._runtime_path("audit"),
                ):
                    self._create_directory(path, created)
                project = ProjectRecord()
                atomic_write_yaml(config_path, project.model_dump(mode="json"), _validate_project)
            return self.get_project()
        except Exception:
            if runtime_was_missing:
                if bootstrap_lock_path is not None:
                    bootstrap_lock_path.unlink(missing_ok=True)
                for path in reversed(created):
                    path.rmdir()
            raise

    def get_project(self) -> ProjectRecord:
        return self._load_yaml(self._runtime_path("config.yaml"), ProjectRecord, "PROJECT_NOT_INITIALIZED")

    def create_task(self, task_id: str) -> TaskRecord:
        validate_task_id(task_id)
        self._require_initialized()
        task_directory = self._task_directory(task_id)
        with self._lock(f"task-{task_id}"):
            if task_directory.is_dir():
                return self.get_task(task_id)
            staging = self._tasks_directory() / f".{task_id}.{uuid4()}.tmp"
            staging.mkdir()
            try:
                for directory in (staging / "operations", staging / "runs"):
                    directory.mkdir()
                task = TaskRecord(task_id=task_id, created_at=_timestamp())
                atomic_write_yaml(
                    staging / "task.yaml", task.model_dump(mode="json"), _validate_task
                )
                staging.replace(task_directory)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        validate_task_id(task_id)
        task_path = self._task_directory(task_id) / "task.yaml"
        return self._load_yaml(task_path, TaskRecord, "TASK_NOT_FOUND", task_id)

    def create_operation(
        self,
        task_id: str,
        operation_type: str,
        idempotency_key: str,
        payload: Mapping[str, object],
    ) -> OperationRecord:
        self.get_task(task_id)
        if not operation_type or not idempotency_key:
            raise DomainError("OPERATION_CONFLICT", "operation type and idempotency key are required")
        request_json = _canonical_request(operation_type, payload)
        with self._lock(f"task-{task_id}"):
            for operation_path in self._operations_directory(task_id).glob("*.json"):
                operation_id = operation_path.stem
                _validate_uuid(operation_id, "OPERATION_NOT_FOUND")
                operation = self._load_json(
                    self._operation_path(task_id, operation_id),
                    OperationRecord,
                    "OPERATION_NOT_FOUND",
                )
                if operation.idempotency_key == idempotency_key:
                    if operation.request_json == request_json:
                        return operation
                    raise DomainError(
                        "OPERATION_CONFLICT",
                        "idempotency key is already associated with a different request",
                    )
            operation = OperationRecord(
                operation_id=str(uuid4()),
                task_id=task_id,
                operation_type=operation_type,
                idempotency_key=idempotency_key,
                request_json=request_json,
                created_at=_timestamp(),
            )
            atomic_write_json(
                self._operation_path(task_id, operation.operation_id),
                operation.model_dump(mode="json"),
                _validate_operation,
            )
            return operation

    def get_operation(self, task_id: str, operation_id: str) -> OperationRecord:
        self.get_task(task_id)
        _validate_uuid(operation_id, "OPERATION_NOT_FOUND")
        operation = self._load_json(
            self._operation_path(task_id, operation_id), OperationRecord, "OPERATION_NOT_FOUND"
        )
        if operation.task_id != task_id:
            raise DomainError("OPERATION_NOT_FOUND", "operation does not belong to task")
        return operation

    def create_run(self, task_id: str, operation_id: str, stage: str) -> RunRecord:
        self.get_operation(task_id, operation_id)
        if not stage:
            raise DomainError("PRECONDITION_FAILED", "stage is required")
        with self._lock(f"task-{task_id}"):
            run = RunRecord(
                run_id=str(uuid4()),
                task_id=task_id,
                operation_id=operation_id,
                stage=stage,
                created_at=_timestamp(),
            )
            run_directory = self._runs_directory(task_id) / run.run_id
            staging = self._runs_directory(task_id) / f".{run.run_id}.tmp"
            staging.mkdir()
            try:
                atomic_write_json(
                    staging / "run.json", run.model_dump(mode="json"), _validate_run
                )
                staging.replace(run_directory)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return self.get_run(task_id, run.run_id)

    def get_run(self, task_id: str, run_id: str) -> RunRecord:
        self.get_task(task_id)
        _validate_uuid(run_id, "RUN_NOT_FOUND")
        run = self._load_json(self._run_path(task_id, run_id), RunRecord, "RUN_NOT_FOUND")
        if run.task_id != task_id:
            raise DomainError("RUN_NOT_FOUND", "run does not belong to task")
        return run

    def _runtime_root(self) -> Path:
        return project_path(self._project_root, ".yipilot")

    def _runtime_path(self, *parts: str) -> Path:
        return project_path(self._project_root, ".yipilot", *parts)

    def _tasks_directory(self) -> Path:
        return self._runtime_path("tasks")

    def _task_directory(self, task_id: str) -> Path:
        validate_task_id(task_id)
        return self._runtime_path("tasks", task_id)

    def _operations_directory(self, task_id: str) -> Path:
        return self._runtime_path("tasks", task_id, "operations")

    def _operation_path(self, task_id: str, operation_id: str) -> Path:
        _validate_uuid(operation_id, "OPERATION_NOT_FOUND")
        return self._runtime_path("tasks", task_id, "operations", f"{operation_id}.json")

    def _runs_directory(self, task_id: str) -> Path:
        return self._runtime_path("tasks", task_id, "runs")

    def _run_path(self, task_id: str, run_id: str) -> Path:
        _validate_uuid(run_id, "RUN_NOT_FOUND")
        return self._runtime_path("tasks", task_id, "runs", run_id, "run.json")

    def _require_initialized(self) -> None:
        if not self._runtime_path("config.yaml").is_file():
            raise DomainError("PROJECT_NOT_INITIALIZED", "project has not been initialized")

    @staticmethod
    def _create_directory(path: Path, created: list[Path]) -> None:
        if path.exists():
            return
        try:
            path.mkdir()
        except FileExistsError:
            return
        created.append(path)

    def _lock(self, name: str) -> FileLock:
        lock_path = self._runtime_path("locks", f"{name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(lock_path))

    def _load_yaml(
        self, path: Path, model: type[RecordModel], missing_code: str, identifier: str = ""
    ) -> RecordModel:
        if not path.is_file():
            raise DomainError(missing_code, f"record not found: {identifier}".rstrip(": "))
        try:
            return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValidationError, yaml.YAMLError) as error:
            raise DomainError("STORAGE_INVALID", f"cannot read {path.name}") from error

    def _load_json(
        self, path: Path, model: type[RecordModel], missing_code: str
    ) -> RecordModel:
        if not path.is_file():
            raise DomainError(missing_code, "record not found")
        try:
            return model.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise DomainError("STORAGE_INVALID", f"cannot read {path.name}") from error


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


def _validate_project(value: Mapping[str, object]) -> None:
    ProjectRecord.model_validate(value)


def _validate_task(value: Mapping[str, object]) -> None:
    TaskRecord.model_validate(value)


def _validate_operation(value: Mapping[str, object]) -> None:
    OperationRecord.model_validate(value)


def _validate_run(value: Mapping[str, object]) -> None:
    RunRecord.model_validate(value)
