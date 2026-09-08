"""Conservative recovery for interrupted local Runs."""

from yipilot.domain.models import RunStatus
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore, RunRecord


class RecoveryService:
    """Fail interrupted side-effect runs instead of replaying unknown work."""

    def __init__(self, store: FileTaskStore) -> None:
        self._store = store

    def recover(self, run_id: str) -> RunRecord:
        run = self._store.find_run(run_id)
        if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
            return run

        error_code = "EXECUTION_INTERRUPTED"
        journal = JsonlEventJournal(
            self._store.event_journal_path(run_id),
            task_id=run.task_id,
            run_id=run.run_id,
            stage=run.stage,
        )
        journal.append(
            "run.failed",
            {
                "error_code": error_code,
                "message": "interrupted execution was not restarted",
            },
        )
        return self._store.set_run_status(
            run_id, RunStatus.FAILED, error_code=error_code
        )
