"""Atomic, run-scoped persistence for terminal tool execution evidence."""

from pathlib import Path

from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ArtifactRef, ToolResult
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.filesystem import (
    atomic_write_json,
    project_path,
    safe_path_component,
)
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord
from yipilot.security.redaction import SecretRedactor


class FileToolRunStore:
    """Persist ToolResults after verifying all referenced artifacts."""

    def __init__(self, task_store: FileTaskStore) -> None:
        self._task_store = task_store
        self._artifacts = FileArtifactStore(task_store)

    def save(self, result: ToolResult) -> Path:
        self._validate_result(result)
        path = self._path(result.run_id, result.tool_run_id, create=True)
        value = SecretRedactor().redact(result.model_dump(mode="json"))
        assert isinstance(value, dict)
        atomic_write_json(path, value, ToolResult.model_validate)
        return path

    def get(self, task_id: str, run_id: str, tool_run_id: str) -> ToolResult:
        safe_path_component(tool_run_id)
        run = self._validate_run(task_id, None, run_id)
        root = self._root(run_id)
        if not root.is_dir():
            raise DomainError("TOOL_RUN_NOT_FOUND", "tool result not found")
        path = self._path(run_id, tool_run_id)
        if not path.is_file():
            raise DomainError("TOOL_RUN_NOT_FOUND", "tool result not found")
        try:
            result = ToolResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise DomainError("TOOL_RUN_INVALID", "tool result is invalid") from error
        if (
            result.task_id != run.task_id
            or result.operation_id != run.operation_id
            or result.run_id != run.run_id
        ):
            raise DomainError("TOOL_RUN_MISMATCH", "tool result does not belong to the persisted run")
        self._validate_result(result)
        return result

    def load(self, task_id: str, run_id: str, tool_run_id: str) -> ToolResult:
        return self.get(task_id, run_id, tool_run_id)

    def list(self, task_id: str, run_id: str) -> list[ToolResult]:
        self._validate_run(task_id, None, run_id)
        root = self._root(run_id)
        if not root.exists():
            return []
        if not root.is_dir():
            raise DomainError("TOOL_RUN_INVALID", "tool results path is not a directory")
        return sorted(
            (self.get(task_id, run_id, path.stem) for path in root.glob("*.json")),
            key=lambda result: (result.started_at, result.tool_run_id),
        )

    def _validate_result(self, result: ToolResult) -> None:
        self._validate_run(result.task_id, result.operation_id, result.run_id)
        refs = [result.stdout_artifact_ref, result.stderr_artifact_ref]
        changed = result.change_summary.changed_paths_artifact_ref
        if changed is not None:
            refs.append(changed)
        for ref in refs:
            self._validate_artifact(result, ref)

    def _validate_artifact(self, result: ToolResult, ref: ArtifactRef) -> None:
        record = self._artifacts.verify(result.task_id, result.run_id, ref.artifact_id)
        if (
            record.operation_id != result.operation_id
            or record.relative_path != ref.relative_path
            or record.sha256 != ref.sha256
        ):
            raise DomainError("TOOL_RUN_ARTIFACT_MISMATCH", "tool result artifact is not intact")

    def _validate_run(
        self, task_id: str, operation_id: str | None, run_id: str
    ) -> RunRecord:
        run = self._task_store.get_run(task_id, run_id)
        if operation_id is not None and run.operation_id != operation_id:
            raise DomainError("TOOL_RUN_MISMATCH", "tool result does not belong to the persisted run")
        return run

    def _root(self, run_id: str) -> Path:
        return project_path(self._task_store.event_journal_path(run_id).parent, "tool-runs")

    def _path(self, run_id: str, tool_run_id: str, *, create: bool = False) -> Path:
        safe_path_component(tool_run_id)
        root = self._root(run_id)
        if create:
            root.mkdir(exist_ok=True)
        return project_path(root, f"{tool_run_id}.json")
