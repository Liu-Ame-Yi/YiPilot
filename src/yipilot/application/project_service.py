"""Project, Task and Run application use cases."""

from pathlib import Path

from yipilot.domain.errors import DomainError
from yipilot.domain.models import RunStatus
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.task_store import (
    FileTaskStore,
    ProjectRecord,
    RunRecord,
    TaskRecord,
)


class ProjectService:
    """Coordinate project mutations without exposing persistence to transports."""

    def __init__(self, project_root: Path) -> None:
        self._store = FileTaskStore(project_root)

    def initialize_project(self) -> ProjectRecord:
        return self._store.initialize_project()

    def initialize_task(self, task_id: str) -> TaskRecord:
        task = self._store.create_task(task_id)
        operation = self._store.create_operation(
            task_id,
            "task_init",
            f"task-init-{task_id}",
            {"task_id": task_id},
        )
        matching_runs = [
            run
            for run in self._store.list_runs(task_id)
            if run.operation_id == operation.operation_id
        ]
        if matching_runs:
            return task

        run = self._store.create_run(task_id, operation.operation_id, "task_init")
        run = self._store.set_run_status(run.run_id, RunStatus.RUNNING)
        journal = self._journal(run)
        journal.append("run.started", {"message": "task initialization started"})
        journal.append("run.succeeded", {"message": "task initialization completed"})
        self._store.set_run_status(run.run_id, RunStatus.SUCCEEDED)
        return task

    def create_interactive_task(self, title: str, development_goal: str) -> TaskRecord:
        if not title.strip() or not development_goal.strip():
            raise DomainError("TASK_DETAILS_REQUIRED", "task title and development goal are required")
        self._store.initialize_project()
        return self._store.create_next_task(title.strip(), development_goal.strip())

    def show_task(self, task_id: str) -> TaskRecord:
        return self._store.get_task(task_id)

    def list_tasks(self) -> list[TaskRecord]:
        return self._store.list_tasks()

    def list_runs(self, task_id: str) -> list[RunRecord]:
        return self._store.list_runs(task_id)

    def show_run(self, task_id: str, run_id: str) -> RunRecord:
        return self._store.get_run(task_id, run_id)

    def _journal(self, run: RunRecord) -> JsonlEventJournal:
        return JsonlEventJournal(
            self._store.event_journal_path(run.run_id),
            task_id=run.task_id,
            run_id=run.run_id,
            stage=run.stage,
        )
