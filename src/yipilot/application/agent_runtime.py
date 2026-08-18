"""A small deterministic loop from a structured plan to terminal Run evidence."""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from yipilot.application.planner import PlannerPort
from yipilot.application.tool_registry import ToolRegistry
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import AgentPlan, ArtifactRef, ToolResult, ToolRunStatus
from yipilot.domain.models import RunStatus
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord

_APPROVAL_ERRORS = {
    "APPROVAL_REQUIRED",
    "APPROVAL_CONFIRMATION_INVALID",
    "APPROVAL_STALE",
}


@dataclass(frozen=True)
class ApprovalInfo:
    """The exact approval to present when a named plan step reaches the registry."""

    approval_id: str
    confirm_token: str


@dataclass(frozen=True)
class AgentRuntimeResult:
    """The bounded audit result returned to the caller; artifacts retain large evidence."""

    run: RunRecord
    plan_artifact_ref: ArtifactRef | None
    tool_results: tuple[ToolResult, ...]


class AgentRuntime:
    """Execute exactly one pending Run without retries, resume, models, or networking."""

    def __init__(
        self,
        project_root: Path,
        store: FileTaskStore,
        planner: PlannerPort,
        tools: ToolRegistry,
    ) -> None:
        self._project_root = Path(project_root)
        self._store = store
        self._planner = planner
        self._tools = tools
        self._artifacts = FileArtifactStore(store)

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
        run = self._pending_run(task_id, operation_id, run_id)
        journal = self._journal(run)
        run = self._store.set_run_status(run_id, RunStatus.RUNNING)
        journal.append("run.started", {})
        fingerprint = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
        results: list[ToolResult] = []
        plan_ref: ArtifactRef | None = None
        try:
            plan = self._planner.plan(instruction_fingerprint=fingerprint)
        except Exception as error:  # noqa: BLE001 - planner faults become an auditable Run failure
            error_code = (
                "PLANNER_REPLAY_NOT_FOUND"
                if isinstance(error, DomainError) and error.code == "REPLAY_PLAN_NOT_FOUND"
                else "PLANNER_FAILED"
            )
            return self._failed(run, journal, plan_ref, results, error_code)
        try:
            plan = AgentPlan.model_validate(plan.model_dump(mode="json"))
            self._validate_plan(plan, run, fingerprint)
        except (DomainError, ValidationError, AttributeError):
            return self._failed(run, journal, plan_ref, results, "PLAN_INVALID")
        try:
            plan_ref = self._write_plan(plan)
        except Exception:  # noqa: BLE001 - persistence faults must not leave a Run running
            return self._failed(run, journal, plan_ref, results, "PLAN_PERSIST_FAILED")
        journal.append("plan.created", {"plan_artifact_ref": plan_ref.model_dump(mode="json")})

        for step in plan.steps:
            try:
                self._tools.validate_request(step.tool_request)
            except DomainError as error:
                if error.code in _APPROVAL_ERRORS:
                    return self._paused(run, journal, plan_ref, results, step.step_id, error.code)
                return self._failed(run, journal, plan_ref, results, "TOOL_POLICY_DENIED")

        for step in plan.steps:
            approval = (approvals or {}).get(step.step_id)
            try:
                result = self._tools.execute(
                    self._project_root,
                    step.tool_request,
                    approval_id=approval.approval_id if approval else None,
                    confirm_token=approval.confirm_token if approval else None,
                )
            except DomainError as error:
                if error.code in _APPROVAL_ERRORS:
                    return self._paused(run, journal, plan_ref, results, step.step_id, error.code)
                return self._failed(run, journal, plan_ref, results, "TOOL_POLICY_DENIED")
            results.append(result)
            if result.status is ToolRunStatus.CANCELLED:
                return self._cancelled(run, journal, plan_ref, results)
            if result.status is ToolRunStatus.TIMED_OUT:
                return self._failed(run, journal, plan_ref, results, "TOOL_TIMED_OUT")
            if result.status is not ToolRunStatus.SUCCEEDED:
                return self._failed(run, journal, plan_ref, results, "TOOL_FAILED")

        try:
            gate_passed = harness() is True
        except Exception:  # noqa: BLE001 - any callback failure is a failed gate
            gate_passed = False
        journal.append("harness.completed", {"gate_passed": gate_passed})
        if not gate_passed:
            return self._failed(run, journal, plan_ref, results, "HARNESS_FAILED")
        completed = self._store.set_run_status(run_id, RunStatus.SUCCEEDED)
        journal.append("run.succeeded", {})
        return AgentRuntimeResult(completed, plan_ref, tuple(results))

    def _pending_run(self, task_id: str, operation_id: str, run_id: str) -> RunRecord:
        self._store.get_task(task_id)
        operation = self._store.get_operation(task_id, operation_id)
        run = self._store.get_run(task_id, run_id)
        if operation.task_id != task_id or run.operation_id != operation_id:
            raise DomainError("RUNTIME_ASSOCIATION_MISMATCH", "Task, Operation and Run do not match")
        if run.status is not RunStatus.PENDING:
            raise DomainError("RUN_NOT_PENDING", "agent runtime only starts pending runs")
        return run

    @staticmethod
    def _validate_plan(plan: AgentPlan, run: RunRecord, fingerprint: str) -> None:
        if (
            plan.task_id,
            plan.operation_id,
            plan.run_id,
            plan.instruction_fingerprint,
        ) != (run.task_id, run.operation_id, run.run_id, fingerprint):
            raise DomainError("PLAN_ASSOCIATION_INVALID", "plan does not belong to this Run")

    def _write_plan(self, plan: AgentPlan) -> ArtifactRef:
        content = json.dumps(
            plan.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        record = self._artifacts.write_bytes(
            plan.task_id,
            plan.run_id,
            producer="agent_runtime",
            relative_path="artifacts/agent-plan.json",
            content=content,
            input_fingerprint=plan.instruction_fingerprint,
            operation_id=plan.operation_id,
        )
        return _artifact_ref(record)

    def _journal(self, run: RunRecord) -> JsonlEventJournal:
        return JsonlEventJournal(
            self._store.event_journal_path(run.run_id),
            task_id=run.task_id,
            run_id=run.run_id,
            stage=run.stage,
        )

    def _paused(
        self,
        run: RunRecord,
        journal: JsonlEventJournal,
        plan_ref: ArtifactRef | None,
        results: list[ToolResult],
        step_id: int,
        error_code: str,
    ) -> AgentRuntimeResult:
        journal.append("approval.required", {"step_id": step_id, "error_code": error_code})
        paused = self._store.set_run_status(run.run_id, RunStatus.PAUSED, error_code=error_code)
        journal.append("run.paused", {"error_code": error_code})
        return AgentRuntimeResult(paused, plan_ref, tuple(results))

    def _failed(
        self,
        run: RunRecord,
        journal: JsonlEventJournal,
        plan_ref: ArtifactRef | None,
        results: list[ToolResult],
        error_code: str,
    ) -> AgentRuntimeResult:
        failed = self._store.set_run_status(run.run_id, RunStatus.FAILED, error_code=error_code)
        journal.append("run.failed", {"error_code": error_code})
        return AgentRuntimeResult(failed, plan_ref, tuple(results))

    def _cancelled(
        self,
        run: RunRecord,
        journal: JsonlEventJournal,
        plan_ref: ArtifactRef | None,
        results: list[ToolResult],
    ) -> AgentRuntimeResult:
        cancelled = self._store.set_run_status(run.run_id, RunStatus.CANCELLED)
        journal.append("run.cancelled", {})
        return AgentRuntimeResult(cancelled, plan_ref, tuple(results))


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )
