"""Atomic, run-scoped persistence for execution approvals."""

from pathlib import Path

from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ApprovalRecord
from yipilot.persistence.filesystem import (
    project_path,
    safe_path_component,
)
from yipilot.persistence.task_store import FileTaskStore, RunRecord


class FileApprovalStore:
    """Save approvals only beneath the persisted Run they authorize."""

    def __init__(self, task_store: FileTaskStore) -> None:
        self._task_store = task_store

    def save(self, approval: ApprovalRecord) -> Path:
        self._validate_run(approval.task_id, approval.operation_id, approval.run_id)
        path = self._path(approval.run_id, approval.approval_id, create=True)
        self._task_store.db.save_record("approval", approval.approval_id, approval.task_id, approval.run_id, approval.model_dump_json())
        return path

    def get(self, task_id: str, run_id: str, approval_id: str) -> ApprovalRecord:
        safe_path_component(approval_id)
        run = self._validate_run(task_id, None, run_id)
        try:
            approval = ApprovalRecord.model_validate_json(self._task_store.db.record("approval", approval_id, "APPROVAL_NOT_FOUND"))
        except ValidationError as error:
            raise DomainError("APPROVAL_INVALID", "invalid record") from error
        if (
            approval.task_id != run.task_id
            or approval.operation_id != run.operation_id
            or approval.run_id != run.run_id
        ):
            raise DomainError("APPROVAL_MISMATCH", "approval does not belong to the persisted run")
        return approval

    def load(self, task_id: str, run_id: str, approval_id: str) -> ApprovalRecord:
        return self.get(task_id, run_id, approval_id)

    def list(self, task_id: str, run_id: str) -> list[ApprovalRecord]:
        self._validate_run(task_id, None, run_id)
        records = [ApprovalRecord.model_validate_json(body) for body in self._task_store.db.records("approval", task_id, run_id)]
        return sorted((self.get(task_id, run_id, r.approval_id) for r in records), key=lambda approval: (approval.decided_at, approval.approval_id))

    def _validate_run(
        self, task_id: str, operation_id: str | None, run_id: str
    ) -> RunRecord:
        run = self._task_store.get_run(task_id, run_id)
        if operation_id is not None and run.operation_id != operation_id:
            raise DomainError("APPROVAL_MISMATCH", "approval does not belong to the persisted run")
        return run

    def _root(self, run_id: str) -> Path:
        return project_path(self._task_store.event_journal_path(run_id).parent, "approvals")

    def _path(self, run_id: str, approval_id: str, *, create: bool = False) -> Path:
        safe_path_component(approval_id)
        root = self._root(run_id)
        if create:
            root.mkdir(exist_ok=True)
        return project_path(root, f"{approval_id}.json")
