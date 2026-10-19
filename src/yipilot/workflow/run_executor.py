"""Own Run lifecycle around the model/tool execution protocol."""

from collections.abc import Callable, Mapping
from dataclasses import replace

from yipilot.agent.agent_runtime import AgentRuntime, AgentRuntimeResult, ApprovalInfo
from yipilot.agent.planner import PlannerPort
from yipilot.agent.ports import RuntimeEvidencePort, ToolExecutionPort
from yipilot.domain.errors import DomainError
from yipilot.domain.models import RunStatus
from yipilot.persistence.ownership import exclusive_execution
from yipilot.persistence.task_store import FileTaskStore


class RunExecutor:
    def __init__(self, store: FileTaskStore, planner: PlannerPort,
                 tools: ToolExecutionPort, evidence: RuntimeEvidencePort) -> None:
        self._store = store
        self._evidence = evidence
        self._runtime = AgentRuntime(store, planner, tools, evidence)

    @exclusive_execution
    def execute(self, task_id: str, operation_id: str, run_id: str, instruction: str,
                *, approvals: Mapping[int, ApprovalInfo] | None = None,
                harness: Callable[[], bool]) -> AgentRuntimeResult:
        run = self._store.get_run(task_id, run_id)
        if run.operation_id != operation_id:
            raise DomainError("RUN_OPERATION_MISMATCH", "Run belongs to another operation")
        if run.status is not RunStatus.PENDING:
            raise DomainError("RUN_NOT_PENDING", "only pending Runs can start")
        self._store.set_run_status(run_id, RunStatus.RUNNING)
        self._evidence.append_event("run.started", {})
        try:
            result = self._runtime.execute(task_id, operation_id, run_id, instruction,
                                           approvals=approvals, harness=harness)
        except BaseException as error:
            status = RunStatus.CANCELLED if isinstance(error, KeyboardInterrupt) else RunStatus.FAILED
            self._store.set_run_status(run_id, status, error_code="RUNTIME_INTERRUPTED")
            self._evidence.append_event(f"run.{status.value}", {"error_code": "RUNTIME_INTERRUPTED"})
            raise
        completed = self._store.set_run_status(
            run_id, result.terminal_status, error_code=result.error_code)
        payload = {"error_code": result.error_code} if result.error_code else {}
        self._evidence.append_event(f"run.{result.terminal_status.value}", payload)
        return replace(result, run=completed)
