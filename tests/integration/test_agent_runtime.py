import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from yipilot.application.agent_runtime import AgentRuntime
from yipilot.application.approval import ApprovalGate
from yipilot.application.planner import FakePlanner, RecordedReplayPlanner
from yipilot.application.recovery_service import RecoveryService
from yipilot.application.tool_registry import ToolRegistry
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    AgentPlan,
    ArgvRule,
    ExecutionPolicy,
    PlanStep,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolRunStatus,
)
from yipilot.domain.models import RunStatus
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import FakeExecutor
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord


class _FailingPlanner:
    def plan(self, *, instruction_fingerprint: str) -> AgentPlan:
        del instruction_fingerprint
        raise OSError("fixture read failed")


def _context(
    tmp_path: Path,
    *,
    kind: ToolKind = ToolKind.READ_ONLY,
    executor_status: ToolRunStatus | None = None,
) -> tuple[FileTaskStore, RunRecord, ToolRegistry, Callable[[AgentPlan], AgentRuntime]]:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "agent", "agent-1001", {})
    run = store.create_run("1001", operation.operation_id, "agent")
    (tmp_path / ".yipilot/tasks/1001/repos/sample/workspace").mkdir(parents=True)
    policy = ExecutionPolicy(
        revision="policy-v1",
        tools=(
            ToolPolicy(
                tool_id="diagnostic",
                kind=kind,
                executable="where.exe",
                argv_schema=(ArgvRule(literal="where.exe"),),
                max_output_bytes=1024,
                max_timeout_seconds=1,
            ),
        ),
    )
    journal = JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    )
    registry = ToolRegistry(
        policy,
        FakeExecutor(
            FileArtifactStore(store), journal, stdout=b"safe output", status=executor_status
        ),
        ApprovalGate(FileApprovalStore(store)) if kind is not ToolKind.READ_ONLY else None,
    )

    def runtime(plan: AgentPlan) -> AgentRuntime:
        return AgentRuntime(tmp_path, store, FakePlanner(plan), registry)

    return store, run, registry, runtime


def _request(run: RunRecord, *, argv: tuple[str, ...] = ("where.exe", "where.exe")) -> ToolRequest:
    return ToolRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="diagnostic",
        argv=argv,
        timeout_seconds=1,
        output_budget_bytes=1024,
        input_fingerprint="a" * 64,
    )


def _plan(run: RunRecord, instruction: str, *requests: ToolRequest) -> AgentPlan:
    return AgentPlan(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        instruction_fingerprint=hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        steps=tuple(
            PlanStep(step_id=index, summary=f"step {index}", tool_request=request)
            for index, request in enumerate(requests, start=1)
        ),
    )


def test_fake_plan_persists_evidence_and_succeeds(tmp_path: Path) -> None:
    store, run, _registry, runtime = _context(tmp_path)
    instruction = "run the fixed diagnostic"
    result = runtime(_plan(run, instruction, _request(run))).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True
    )

    assert result.run.status is RunStatus.SUCCEEDED
    assert len(result.tool_results) == 1
    assert result.plan_artifact_ref is not None
    artifact = store.event_journal_path(run.run_id).parent / result.plan_artifact_ref.relative_path
    assert artifact.is_file()
    assert [event.type for event in JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)] == [
        "run.started", "plan.created", "tool.started", "tool.completed", "harness.completed", "run.succeeded"
    ]


def test_replay_requires_an_exact_fingerprint_without_executing_tools(tmp_path: Path) -> None:
    store, run, registry, _runtime = _context(tmp_path)
    instruction = "recorded instruction"
    plan = _plan(run, instruction, _request(run))
    runtime = AgentRuntime(
        tmp_path,
        store,
        RecordedReplayPlanner("fixtures-v1", {plan.instruction_fingerprint: plan}),
        registry,
    )
    result = runtime.execute(run.task_id, run.operation_id, run.run_id, "unknown", harness=lambda: True)

    assert result.run.status is RunStatus.FAILED
    assert result.run.error_code == "PLANNER_REPLAY_NOT_FOUND"
    assert [event.type for event in JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)] == [
        "run.started", "run.failed"
    ]


def test_replay_returns_the_recorded_structure_and_runs_it(tmp_path: Path) -> None:
    store, run, registry, _runtime = _context(tmp_path)
    instruction = "recorded instruction"
    plan = _plan(run, instruction, _request(run))
    planner = RecordedReplayPlanner("fixtures-v1", {plan.instruction_fingerprint: plan})
    runtime = AgentRuntime(tmp_path, store, planner, registry)

    assert planner.plan(instruction_fingerprint=plan.instruction_fingerprint) == plan
    assert planner.plan(instruction_fingerprint=plan.instruction_fingerprint) == plan
    result = runtime.execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)
    assert result.run.status is RunStatus.SUCCEEDED


def test_planner_fault_is_a_stable_terminal_run_failure(tmp_path: Path) -> None:
    store, run, registry, _runtime = _context(tmp_path)
    runtime = AgentRuntime(tmp_path, store, _FailingPlanner(), registry)
    result = runtime.execute(run.task_id, run.operation_id, run.run_id, "private instruction", harness=lambda: True)

    assert result.run.status is RunStatus.FAILED
    assert result.run.error_code == "PLANNER_FAILED"
    assert [event.type for event in JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)] == [
        "run.started", "run.failed"
    ]


@pytest.mark.parametrize("invalid", ["cross_run", "duplicate", "bad_argv", "unknown_tool"])
def test_invalid_plan_never_starts_a_tool(tmp_path: Path, invalid: str) -> None:
    store, run, _registry, runtime = _context(tmp_path)
    instruction = "fixed instruction"
    request = _request(run)
    if invalid == "cross_run":
        request = request.model_copy(update={"run_id": "00000000-0000-0000-0000-000000000000"})
        plan = AgentPlan.model_construct(
            task_id=run.task_id,
            operation_id=run.operation_id,
            run_id=run.run_id,
            instruction_fingerprint=hashlib.sha256(instruction.encode()).hexdigest(),
            steps=(PlanStep.model_construct(step_id=1, summary="step", tool_request=request),),
        )
    elif invalid == "duplicate":
        plan = AgentPlan.model_construct(
            task_id=run.task_id,
            operation_id=run.operation_id,
            run_id=run.run_id,
            instruction_fingerprint=hashlib.sha256(instruction.encode()).hexdigest(),
            steps=(
                PlanStep.model_construct(step_id=1, summary="one", tool_request=request),
                PlanStep.model_construct(step_id=1, summary="two", tool_request=request),
            ),
        )
    else:
        changes = {"argv": ("other.exe", "where.exe")} if invalid == "bad_argv" else {"tool_id": "missing"}
        plan = _plan(run, instruction, request.model_copy(update=changes))
    result = runtime(plan).execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)

    assert result.run.status is RunStatus.FAILED
    assert not result.tool_results
    assert "tool.started" not in [event.type for event in JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)]


def test_unapproved_high_risk_step_pauses_without_running_later_steps(tmp_path: Path) -> None:
    store, run, _registry, runtime = _context(tmp_path, kind=ToolKind.WORKSPACE_WRITE)
    instruction = "write only when approved"
    request = _request(run)
    result = runtime(_plan(run, instruction, request, request)).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True
    )

    assert result.run.status is RunStatus.PAUSED
    assert not result.tool_results
    assert [event.type for event in JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)][-2:] == [
        "approval.required", "run.paused"
    ]


@pytest.mark.parametrize(
    ("tool_status", "run_status", "error_code"),
    [
        (ToolRunStatus.FAILED, RunStatus.FAILED, "TOOL_FAILED"),
        (ToolRunStatus.TIMED_OUT, RunStatus.FAILED, "TOOL_TIMED_OUT"),
        (ToolRunStatus.CANCELLED, RunStatus.CANCELLED, None),
    ],
)
def test_terminal_tool_statuses_choose_the_run_terminal_state(
    tmp_path: Path, tool_status: ToolRunStatus, run_status: RunStatus, error_code: str | None
) -> None:
    _store, run, _registry, runtime = _context(tmp_path, executor_status=tool_status)
    instruction = "execute once"
    result = runtime(_plan(run, instruction, _request(run))).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True
    )

    assert result.run.status is run_status
    assert result.run.error_code == error_code


def test_failed_harness_blocks_success_and_events_hide_instruction_and_argv(tmp_path: Path) -> None:
    store, run, _registry, runtime = _context(tmp_path)
    instruction = "Authorization: Bearer instruction-secret"
    request = _request(run, argv=("where.exe", "where.exe"))
    result = runtime(_plan(run, instruction, request)).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: False
    )

    assert result.run.status is RunStatus.FAILED
    assert result.run.error_code == "HARNESS_FAILED"
    event_text = store.event_journal_path(run.run_id).read_text(encoding="utf-8")
    assert instruction not in event_text
    assert '"argv"' not in event_text
    assert "Authorization" not in event_text


def test_recovery_marks_interrupted_run_failed_and_runtime_does_not_resume(tmp_path: Path) -> None:
    store, run, _registry, runtime = _context(tmp_path)
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    recovered = RecoveryService(store).recover(run.run_id)

    assert recovered.status is RunStatus.FAILED
    assert recovered.error_code == "EXECUTION_INTERRUPTED"
    with pytest.raises(DomainError, match="RUN_NOT_PENDING"):
        runtime(_plan(run, "again", _request(run))).execute(
            run.task_id, run.operation_id, run.run_id, "again", harness=lambda: True
        )
