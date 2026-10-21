"""Conservative recovery for interrupted local Runs."""

from yipilot.domain.errors import DomainError
from yipilot.domain.models import RunStatus
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.ownership import project_lock
from yipilot.persistence.task_store import FileTaskStore, RunRecord
from yipilot.workspace.runner_client import RunnerClient
from yipilot.workspace.runner_worker import cancel_remote_job, read_remote_request


class RecoveryService:
    """Fail interrupted side-effect runs instead of replaying unknown work."""

    def __init__(self, store: FileTaskStore, runner: RunnerClient | None = None) -> None:
        self._store = store
        self._runner = runner or RunnerClient()

    def recover(self, run_id: str) -> RunRecord:
        with project_lock(self._store.db.project, "run-" + run_id):
            root = self._store.event_journal_path(run_id).parent
            if (root / "remote-job/submission.json").exists() or self._store.find_run(run_id).stage == "sandbox_verification":
                return self._recover(run_id)
            with project_lock(self._store.db.project, "executor"):
                return self._recover(run_id)

    def _recover(self, run_id: str) -> RunRecord:
        run = self._store.find_run(run_id)
        if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
            return run

        if run.stage == "sandbox_verification":
            for child in self._store.list_runs(run.task_id):
                if child.operation_id == run.operation_id and child.run_id != run_id:
                    if child.status == RunStatus.PENDING:
                        self._store.set_run_status(child.run_id, RunStatus.CANCELLED, error_code="EXECUTION_INTERRUPTED")
                    elif (child.status in {RunStatus.RUNNING, RunStatus.PAUSED}
                          and self.recover(child.run_id).status == RunStatus.PAUSED):
                        if run.status == RunStatus.RUNNING:
                            return self._store.set_run_status(run_id, RunStatus.PAUSED, error_code="REMOTE_RESULT_PENDING")
                        return run

        root = self._store.event_journal_path(run_id).parent
        if (root / "remote-job/submission.json").exists():
            try:
                request = read_remote_request(root)
                if (request.task_id, request.operation_id, request.run_id) != (run.task_id, run.operation_id, run.run_id):
                    raise ValueError("Remote request belongs to another Run")
                cancel_remote_job(root, request, self._runner)
            except (DomainError, OSError, ValueError):
                if run.status == RunStatus.RUNNING:
                    return self._store.set_run_status(run_id, RunStatus.PAUSED, error_code="REMOTE_RESULT_PENDING")
                return self._store.find_run(run_id)

        error_code = "EXECUTION_INTERRUPTED" if run.stage in {"code_generate", "code_repair"} else "EXTERNAL_RESULT_UNCERTAIN"
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
