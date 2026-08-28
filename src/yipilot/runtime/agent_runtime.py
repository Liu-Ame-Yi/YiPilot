"""A deterministic loop from a restricted plan to terminal Run evidence."""

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import AgentPlan, ArtifactRef, ToolResult, ToolRunStatus
from yipilot.domain.models import RunStatus
from yipilot.runtime.planner import PlannerPort
from yipilot.runtime.ports import (
    RunStorePort,
    RuntimeEvidencePort,
    RuntimeRun,
    ToolExecutionPort,
)

_APPROVAL_ERRORS = {"APPROVAL_REQUIRED", "APPROVAL_CONFIRMATION_INVALID", "APPROVAL_STALE"}


@dataclass(frozen=True)
class ApprovalInfo:
    """The exact approval to present when a named plan step reaches the registry."""

    approval_id: str
    confirm_token: str


@dataclass(frozen=True)
class AgentRuntimeResult:
    """A terminal decision for the control plane to persist against its active Run."""

    run: RuntimeRun
    terminal_status: RunStatus
    error_code: str | None
    plan_artifact_ref: ArtifactRef | None
    tool_results: tuple[ToolResult, ...]


class AgentRuntime:
    """Run one control-plane-started Run through injected capability seams."""

    def __init__(
        self,
        store: RunStorePort,
        planner: PlannerPort,
        tools: ToolExecutionPort,
        evidence: RuntimeEvidencePort,
    ) -> None:
        self._store = store
        self._planner = planner
        self._tools = tools
        self._evidence = evidence

    def execute(
        self,
        task_id: str,
        operation_id: str,
        run_id: str,
        instruction: str,
        *,
        approvals: Mapping[int, ApprovalInfo] | None = None,
        harness: Callable[[], bool],
    ) -> AgentRuntimeResult:
        run = self._running_run(task_id, operation_id, run_id)
        fingerprint = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        results: list[ToolResult] = []
        plan_ref: ArtifactRef | None = None
        try:
            plan = self._planner.plan(instruction_fingerprint=fingerprint)
        except Exception as error:  # noqa: BLE001 - faults become an auditable Run failure
            return self._result(run, RunStatus.FAILED, _planner_error_code(error), plan_ref, results)
        try:
            plan = AgentPlan.model_validate(plan.model_dump(mode="json"))
            self._validate_plan(plan, run, fingerprint)
        except (DomainError, ValidationError, AttributeError):
            return self._result(run, RunStatus.FAILED, "PLAN_INVALID", plan_ref, results)
        try:
            plan_ref = self._evidence.write_plan(plan)
        except Exception:  # noqa: BLE001 - persistence faults must not leave a Run running
            return self._result(run, RunStatus.FAILED, "PLAN_PERSIST_FAILED", plan_ref, results)
        self._evidence.append_event("plan.created", {"plan_artifact_ref": plan_ref.model_dump(mode="json")})

        for step in plan.steps:
            try:
                self._tools.validate_request(step.tool_request)
            except DomainError as error:
                if error.code in _APPROVAL_ERRORS:
                    return self._paused(run, plan_ref, results, step.step_id, error.code)
                return self._result(run, RunStatus.FAILED, "TOOL_POLICY_DENIED", plan_ref, results)

        for step in plan.steps:
            approval = (approvals or {}).get(step.step_id)
            try:
                result = self._tools.execute(
                    step.tool_request,
                    approval_id=approval.approval_id if approval else None,
                    confirm_token=approval.confirm_token if approval else None,
                )
            except DomainError as error:
                if error.code in _APPROVAL_ERRORS:
                    return self._paused(run, plan_ref, results, step.step_id, error.code)
                return self._result(run, RunStatus.FAILED, "TOOL_POLICY_DENIED", plan_ref, results)
            results.append(result)
            if result.status is ToolRunStatus.CANCELLED:
                return self._result(run, RunStatus.CANCELLED, None, plan_ref, results)
            if result.status is ToolRunStatus.TIMED_OUT:
                return self._result(run, RunStatus.FAILED, "TOOL_TIMED_OUT", plan_ref, results)
            if result.status is not ToolRunStatus.SUCCEEDED:
                return self._result(run, RunStatus.FAILED, "TOOL_FAILED", plan_ref, results)

        try:
            gate_passed = harness() is True
        except Exception:  # noqa: BLE001 - any callback failure is a failed gate
            gate_passed = False
        self._evidence.append_event("harness.completed", {"gate_passed": gate_passed})
        if not gate_passed:
            return self._result(run, RunStatus.FAILED, "HARNESS_FAILED", plan_ref, results)
        return self._result(run, RunStatus.SUCCEEDED, None, plan_ref, results)

    def _running_run(self, task_id: str, operation_id: str, run_id: str) -> RuntimeRun:
        self._store.get_task(task_id)
        operation = self._store.get_operation(task_id, operation_id)
        run = self._store.get_run(task_id, run_id)
        if operation.task_id != task_id or run.operation_id != operation_id:
            raise DomainError("RUNTIME_ASSOCIATION_MISMATCH", "Task, Operation and Run do not match")
        if run.status is not RunStatus.RUNNING:
            raise DomainError("RUN_NOT_RUNNING", "control plane must start the Run before the agent runtime")
        return run

    @staticmethod
    def _validate_plan(plan: AgentPlan, run: RuntimeRun, fingerprint: str) -> None:
        if (plan.task_id, plan.operation_id, plan.run_id, plan.instruction_fingerprint) != (
            run.task_id,
            run.operation_id,
            run.run_id,
            fingerprint,
        ):
            raise DomainError("PLAN_ASSOCIATION_INVALID", "plan does not belong to this Run")

    def _paused(
        self,
        run: RuntimeRun,
        plan_ref: ArtifactRef | None,
        results: list[ToolResult],
        step_id: int,
        error_code: str,
    ) -> AgentRuntimeResult:
        self._evidence.append_event("approval.required", {"step_id": step_id, "error_code": error_code})
        return self._result(run, RunStatus.PAUSED, error_code, plan_ref, results)

    @staticmethod
    def _result(
        run: RuntimeRun,
        terminal_status: RunStatus,
        error_code: str | None,
        plan_ref: ArtifactRef | None,
        results: list[ToolResult],
    ) -> AgentRuntimeResult:
        return AgentRuntimeResult(run, terminal_status, error_code, plan_ref, tuple(results))


def _planner_error_code(error: Exception) -> str:
    if not isinstance(error, DomainError):
        return "PLANNER_FAILED"
    return {
        "REPLAY_PLAN_NOT_FOUND": "PLANNER_REPLAY_NOT_FOUND",
        "MODEL_REPLAY_NOT_FOUND": "MODEL_REPLAY_NOT_FOUND",
        "MODEL_DISABLED": "MODEL_DISABLED",
        "MODEL_OUTPUT_LIMIT_EXCEEDED": "MODEL_OUTPUT_LIMIT_EXCEEDED",
        "MODEL_PLAN_INVALID": "MODEL_PLAN_INVALID",
        "MODEL_INPUT_MISMATCH": "MODEL_INPUT_MISMATCH",
    }.get(error.code, "PLANNER_FAILED")
