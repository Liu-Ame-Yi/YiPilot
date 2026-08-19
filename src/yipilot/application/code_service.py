"""Bounded CLI use cases for the built-in offline code runtime."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from yipilot.application.agent_runtime import AgentRuntime
from yipilot.application.approval import approval_action_type
from yipilot.application.planner import FakePlanner, PlannerPort, RecordedReplayPlanner
from yipilot.application.tool_registry import ToolRegistry
from yipilot.application.workspace_service import PreparedWorkspace, WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    AgentPlan,
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ExecutionPolicy,
    ExecutionTrustLevel,
    PlanStep,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolResult,
)
from yipilot.domain.models import RunStatus
from yipilot.harness.code import CodeHarness
from yipilot.harness.models import HarnessReport, HarnessStatus
from yipilot.harness.report_store import CODE_HARNESS_REPORT_NAME, HarnessReportStore
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import FakeExecutor
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord
from yipilot.infrastructure.tool_run_store import FileToolRunStore

_FAKE_TOOL_ID = "fake-diagnostic"
_FAKE_EXECUTABLE = "yipilot.fake-diagnostic"
_FAKE_ARGV = (_FAKE_EXECUTABLE, "workspace")
_POLICY_REVISION = "code-cli-fake-v1"
_REPLAY_INSTRUCTION = "yipilot replay diagnostic"
_PLAN_PATH = "artifacts/agent-plan.json"
_HANDOFF_PATH = "artifacts/prepare-only-handoff.json"


@dataclass(frozen=True)
class CodeGenerateResult:
    task_id: str
    operation_id: str
    run_id: str
    status: str
    error_code: str | None
    plan_artifact_ref: dict[str, object] | None

    def value(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "operation_id": self.operation_id,
            "plan_artifact_ref": self.plan_artifact_ref,
            "run_id": self.run_id,
            "status": self.status,
            "task_id": self.task_id,
        }


@dataclass(frozen=True)
class PrepareOnlyResult:
    task_id: str
    operation_id: str
    run_id: str
    status: str
    handoff_artifact_ref: dict[str, object]

    def value(self) -> dict[str, object]:
        return {
            "handoff_artifact_ref": self.handoff_artifact_ref,
            "operation_id": self.operation_id,
            "prepare_only": True,
            "run_id": self.run_id,
            "status": self.status,
            "task_id": self.task_id,
        }


class CodeService:
    """Create one auditable fake/replay Run and a no-side-effect handoff."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._store = FileTaskStore(self._project_root)

    def generate(
        self, task_id: str, alias: str, instruction: str, planner_mode: str
    ) -> CodeGenerateResult:
        if not instruction:
            raise DomainError("INSTRUCTION_INVALID", "instruction is required")
        if planner_mode not in {"fake", "replay"}:
            raise DomainError("PLANNER_MODE_INVALID", "planner must be fake or replay")
        self._store.get_task(task_id)
        instruction_fingerprint = _sha256(instruction)
        operation = self._store.create_operation(
            task_id,
            "code_generate",
            f"code-generate:{task_id}:{alias}:{planner_mode}:{instruction_fingerprint}",
            {
                "alias": alias,
                "instruction_fingerprint": instruction_fingerprint,
                "planner": planner_mode,
            },
        )
        run = self._store.create_run(task_id, operation.operation_id, "code_generate")
        journal = _journal(self._store, run)
        policy = _fake_policy()
        artifacts = FileArtifactStore(self._store)
        registry = ToolRegistry(policy, FakeExecutor(artifacts, journal, stdout=b"fake diagnostic\n"))

        if planner_mode == "replay" and instruction != _REPLAY_INSTRUCTION:
            planner: PlannerPort = RecordedReplayPlanner("builtin-replay-v1", {})
            workspace = None
        else:
            workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
                task_id, alias, run.run_id
            )
            plan = _plan(run, workspace, instruction_fingerprint)
            planner = (
                FakePlanner(plan)
                if planner_mode == "fake"
                else RecordedReplayPlanner("builtin-replay-v1", {instruction_fingerprint: plan})
            )
            _save_simulation_approval(self._store, policy, plan.steps[0].tool_request)

        runtime = AgentRuntime(self._project_root, self._store, planner, registry)
        result = runtime.execute(
            task_id,
            operation.operation_id,
            run.run_id,
            instruction,
            harness=(
                (lambda: _code_harness(self._project_root, workspace, policy, run, plan.steps[0].tool_request).run().status is HarnessStatus.PASSED)
                if workspace is not None
                else lambda: False
            ),
        )
        return CodeGenerateResult(
            task_id=task_id,
            operation_id=operation.operation_id,
            run_id=run.run_id,
            status=result.run.status.value,
            error_code=result.run.error_code,
            plan_artifact_ref=(
                result.plan_artifact_ref.model_dump(mode="json")
                if result.plan_artifact_ref is not None
                else None
            ),
        )

    def prepare_only(self, task_id: str, run_id: str) -> PrepareOnlyResult:
        run = self._store.get_run(task_id, run_id)
        if run.status is not RunStatus.SUCCEEDED or run.stage != "code_generate":
            raise DomainError("CODE_HANDOFF_PRECONDITION_FAILED", "run is not a successful code generation")
        report = HarnessReportStore(self._store).load(run_id, CODE_HARNESS_REPORT_NAME)
        if (
            report.task_id != task_id
            or report.operation_id != run.operation_id
            or report.run_id != run_id
            or report.stage != run.stage
            or report.harness_name != "code_harness"
            or report.status is not HarnessStatus.PASSED
        ):
            raise DomainError("CODE_HARNESS_FAILED", "code harness report is not a passed Run evidence record")
        plan, _plan_record = self._load_plan(run)
        request = _require_fake_request(plan)
        workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
            task_id, _alias_from_request(request), run_id
        )
        policy = _fake_policy()
        approval = _simulation_approval(self._store, run, policy, request)
        refreshed = _code_harness(
            self._project_root, workspace, policy, run, request, approval.approval_id
        ).run()
        if refreshed.status is not HarnessStatus.PASSED:
            raise DomainError("CODE_HARNESS_FAILED", "code harness evidence is stale or has failed")
        artifacts = _verified_artifacts(self._store, task_id, run_id)
        tool_runs = FileToolRunStore(self._store).list(task_id, run_id)
        if not tool_runs:
            raise DomainError("CODE_HANDOFF_EVIDENCE_INVALID", "no persisted tool result exists")
        handoff = _handoff_value(run, workspace, refreshed, approval, tool_runs, artifacts)
        handoff_record = FileArtifactStore(self._store).write_bytes(
            task_id,
            run_id,
            producer="code_apply_prepare",
            relative_path=_HANDOFF_PATH,
            content=json.dumps(handoff, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            input_fingerprint=refreshed.input_fingerprint,
            operation_id=run.operation_id,
        )
        return PrepareOnlyResult(
            task_id=task_id,
            operation_id=run.operation_id,
            run_id=run_id,
            status=run.status.value,
            handoff_artifact_ref=_artifact_ref_value(handoff_record),
        )

    def _load_plan(self, run: RunRecord) -> tuple[AgentPlan, ArtifactRecord]:
        artifacts = FileArtifactStore(self._store)
        records = [record for record in artifacts.list(run.task_id, run.run_id) if record.relative_path == _PLAN_PATH]
        if len(records) != 1:
            raise DomainError("CODE_HANDOFF_EVIDENCE_INVALID", "agent plan artifact is missing or ambiguous")
        record = artifacts.verify(run.task_id, run.run_id, records[0].artifact_id)
        path = self._store.event_journal_path(run.run_id).parent / record.relative_path
        try:
            plan = AgentPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DomainError("CODE_HANDOFF_EVIDENCE_INVALID", "agent plan artifact is invalid") from error
        if (plan.task_id, plan.operation_id, plan.run_id) != (run.task_id, run.operation_id, run.run_id):
            raise DomainError("CODE_HANDOFF_EVIDENCE_INVALID", "agent plan does not belong to the Run")
        return plan, record


def _fake_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        revision=_POLICY_REVISION,
        tools=(
            ToolPolicy(
                tool_id=_FAKE_TOOL_ID,
                kind=ToolKind.READ_ONLY,
                executable=_FAKE_EXECUTABLE,
                argv_schema=(ArgvRule(literal="workspace"),),
                max_output_bytes=1024,
                max_timeout_seconds=1,
            ),
        ),
    )


def _plan(run: RunRecord, workspace: PreparedWorkspace, instruction_fingerprint: str) -> AgentPlan:
    request = ToolRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        workspace_relative_path=(
            f".yipilot/tasks/{run.task_id}/repos/{workspace.repo_alias}/workspace"
        ),
        tool_id=_FAKE_TOOL_ID,
        argv=_FAKE_ARGV,
        timeout_seconds=1,
        output_budget_bytes=1024,
        input_fingerprint=instruction_fingerprint,
    )
    return AgentPlan(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        instruction_fingerprint=instruction_fingerprint,
        steps=(PlanStep(step_id=1, summary="run deterministic workspace diagnostic", tool_request=request),),
    )


def _save_simulation_approval(
    store: FileTaskStore, policy: ExecutionPolicy, request: ToolRequest
) -> None:
    tool = policy.validate_request(request)
    approval = ApprovalRecord(
        approval_id="fake-read-only-simulation",
        task_id=request.task_id,
        operation_id=request.operation_id,
        run_id=request.run_id,
        action_type=approval_action_type(tool, ExecutionTrustLevel.FAKE),
        target_fingerprint=request.fingerprint,
        policy_revision=policy.revision,
        decision=ApprovalDecision.APPROVED,
        decided_at=datetime.now(UTC),
        simulation_only=True,
    )
    FileApprovalStore(store).save(approval)


def _simulation_approval(
    store: FileTaskStore, run: RunRecord, policy: ExecutionPolicy, request: ToolRequest
) -> ApprovalRecord:
    tool = policy.validate_request(request)
    approvals = [
        approval
        for approval in FileApprovalStore(store).list(run.task_id, run.run_id)
        if approval.simulation_only
        and approval.action_type == approval_action_type(tool, ExecutionTrustLevel.FAKE)
        and approval.target_fingerprint == request.fingerprint
    ]
    if len(approvals) != 1:
        raise DomainError("APPROVAL_REQUIRED", "a current simulation-only fake approval is required")
    approval = approvals[0]
    approval.require_fresh(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        action_type=approval_action_type(tool, ExecutionTrustLevel.FAKE),
        target_fingerprint=request.fingerprint,
        policy_revision=policy.revision,
    )
    return approval


def _code_harness(
    project_root: Path,
    workspace: PreparedWorkspace,
    policy: ExecutionPolicy,
    run: RunRecord,
    request: ToolRequest,
    approval_id: str = "fake-read-only-simulation",
) -> CodeHarness:
    return CodeHarness(
        project_root,
        run.task_id,
        run.run_id,
        workspace,
        policy,
        approval_id=approval_id,
        action_type=approval_action_type(policy.tools[0], ExecutionTrustLevel.FAKE),
        target_fingerprint=request.fingerprint,
    )


def _require_fake_request(plan: AgentPlan) -> ToolRequest:
    if len(plan.steps) != 1:
        raise DomainError("CODE_HANDOFF_EVIDENCE_INVALID", "agent plan is not the built-in fake plan")
    request = plan.steps[0].tool_request
    if (
        request.tool_id != _FAKE_TOOL_ID
        or request.argv != _FAKE_ARGV
        or request.timeout_seconds != 1
        or request.output_budget_bytes != 1024
    ):
        raise DomainError("CODE_HANDOFF_EVIDENCE_INVALID", "agent plan is not the built-in fake plan")
    return request


def _alias_from_request(request: ToolRequest) -> str:
    return Path(request.workspace_relative_path).parts[4]


def _verified_artifacts(store: FileTaskStore, task_id: str, run_id: str) -> list[ArtifactRecord]:
    artifacts = FileArtifactStore(store)
    records = artifacts.list(task_id, run_id)
    if not records:
        raise DomainError("CODE_HANDOFF_EVIDENCE_INVALID", "no persisted artifacts exist")
    return [artifacts.verify(task_id, run_id, record.artifact_id) for record in records]


def _handoff_value(
    run: RunRecord,
    workspace: PreparedWorkspace,
    report: HarnessReport,
    approval: ApprovalRecord,
    tool_runs: list[ToolResult],
    artifacts: list[ArtifactRecord],
) -> dict[str, object]:
    report_value = report.model_dump(mode="json")
    git_evidence_refs = sorted(
        {
            ref
            for check in report_value["checks"]
            for ref in check["evidence_refs"]
            if "/git/" in ref
        }
    )
    return {
        "artifact_refs": [_artifact_ref_value(record) for record in artifacts],
        "approval_refs": [{"approval_id": approval.approval_id, "simulation_only": True}],
        "code_harness_report_ref": {
            "input_fingerprint": report_value["input_fingerprint"],
            "path": f"harness/{CODE_HARNESS_REPORT_NAME}",
            "report_id": report_value["report_id"],
        },
        "git_evidence_refs": git_evidence_refs,
        "operation_id": run.operation_id,
        "prepare_only": True,
        "recommended_checks": [
            "git -C <workspace> diff --check",
            "Review the Git diff and all referenced evidence before any manual apply.",
            "Simulation-only fake evidence is not a human approval for a real write.",
        ],
        "run_id": run.run_id,
        "schema_version": 1,
        "task_id": run.task_id,
        "tool_run_refs": [
            {
                "path": f"tool-runs/{result.tool_run_id}.json",
                "status": result.status.value,
                "tool_run_id": result.tool_run_id,
            }
            for result in tool_runs
        ],
        "workspace": {
            "base_sha": workspace.base_sha,
            "branch": workspace.branch,
            "path": f".yipilot/tasks/{workspace.task_id}/repos/{workspace.repo_alias}/workspace",
            "repo_alias": workspace.repo_alias,
        },
    }


def _artifact_ref_value(record: ArtifactRecord) -> dict[str, object]:
    return {
        "artifact_id": record.artifact_id,
        "record_path": f"artifacts/records/{record.artifact_id}.json",
        "relative_path": record.relative_path,
        "sha256": record.sha256,
    }


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
