"""Bounded CLI use cases for the built-in offline code runtime."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import ValidationError

from yipilot.application.agent_runtime import AgentRuntime
from yipilot.application.approval import approval_action_type
from yipilot.application.model_selection_service import ModelSelectionService
from yipilot.application.planner import FakePlanner, PlannerPort, RecordedReplayPlanner
from yipilot.application.provider_service import ProviderService
from yipilot.application.replay_model import (
    replay_model_planner as _replay_model_planner,
)
from yipilot.application.runtime_adapters import RegistryToolGateway
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
from yipilot.domain.model import (
    ModelCallRecord,
    ModelCallStatus,
    ModelPort,
    ModelRequest,
    model_request_fingerprint,
)
from yipilot.domain.models import RunStatus
from yipilot.domain.patch_proposal import (
    MAX_PATCH_DIFF_BYTES,
    MAX_PATCH_FILES,
    PatchProposal,
)
from yipilot.domain.provider import ResolvedModel
from yipilot.harness.code import CodeHarness
from yipilot.harness.models import HarnessReport, HarnessStatus
from yipilot.harness.report_store import (
    CODE_HARNESS_REPORT_NAME,
    SDD_HARNESS_REPORT_NAME,
    SRS_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import FakeExecutor
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.runtime_evidence import FileRuntimeEvidence
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord
from yipilot.infrastructure.tool_run_store import FileToolRunStore

_FAKE_TOOL_ID = "fake-diagnostic"
_FAKE_EXECUTABLE = "yipilot.fake-diagnostic"
_FAKE_ARGV = (_FAKE_EXECUTABLE, "workspace")
_POLICY_REVISION = "code-cli-fake-v1"
_REPLAY_INSTRUCTION = "yipilot replay diagnostic"
_PLAN_PATH = "artifacts/agent-plan.json"
_HANDOFF_PATH = "artifacts/prepare-only-handoff.json"
_VERIFICATION_PATH = "artifacts/prepare-only-verification.json"
_LIVE_RESOLVED_PATH = "artifacts/resolved-model.json"
_LIVE_CONTEXT_PATH = "artifacts/live-context.md"
_LIVE_RESPONSE_PATH = "artifacts/live-model-response.json"
_LIVE_CALL_PATH = "artifacts/live-model-call.json"
_LIVE_PROPOSAL_PATH = "artifacts/patch-proposal.json"
_LIVE_MAX_CONTEXT_FILES = MAX_PATCH_FILES
_LIVE_MAX_CONTEXT_BYTES = 24 * 1024
_LIVE_MAX_FILE_BYTES = 8 * 1024
_LIVE_DOCUMENT_PATHS = (
    "artifacts/srs.md",
    "artifacts/sdd.md",
    "artifacts/test-plan.md",
)


@dataclass(frozen=True)
class CodeGenerateResult:
    task_id: str
    operation_id: str
    run_id: str
    status: str
    error_code: str | None
    plan_artifact_ref: dict[str, object] | None
    patch_proposal_artifact_ref: dict[str, object] | None

    def value(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "operation_id": self.operation_id,
            "plan_artifact_ref": self.plan_artifact_ref,
            "patch_proposal_artifact_ref": self.patch_proposal_artifact_ref,
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

    def __init__(
        self,
        project_root: Path,
        *,
        settings_dir: Path | None = None,
        provider_service: ProviderService | None = None,
        model_selection_service: ModelSelectionService | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._store = FileTaskStore(self._project_root)
        self._settings_dir = settings_dir
        self._legacy_provider_service = provider_service
        self._model_selection = model_selection_service or (
            provider_service.model_selection if provider_service is not None else None
        )

    def generate(
        self,
        task_id: str,
        alias: str,
        instruction: str,
        planner_mode: str,
        request_id: str | None = None,
        provider_profile: str | None = None,
        model_selector: str | None = None,
        context_files: Iterable[str] = (),
    ) -> CodeGenerateResult:
        if not instruction:
            raise DomainError("INSTRUCTION_INVALID", "instruction is required")
        if planner_mode not in {"fake", "replay", "replay-model", "live"}:
            raise DomainError(
                "PLANNER_MODE_INVALID",
                "planner must be fake, replay, replay-model or live",
            )
        self._store.get_task(task_id)
        # Validate the registered repository before creating durable execution state.
        WorkspaceService(self._project_root, GitRunner()).validate_repository(
            task_id, alias
        )
        selected_context = tuple(context_files)
        if planner_mode == "live" and provider_profile is not None and model_selector is not None:
            raise DomainError("MODEL_SELECTION_CONFLICT", "--model and --provider-profile cannot be used together")
        selection: ModelSelectionService | None = None
        if planner_mode == "live" and provider_profile is not None:
            legacy = self._legacy_provider_service or ProviderService(self._settings_dir)
            model_selector = legacy.selector_for_profile(provider_profile)
        if planner_mode == "live":
            selection = self._model_selection or ModelSelectionService(self._settings_dir)
            resolved = selection.resolve(explicit_selector=model_selector)
            port = selection.model_port(resolved)
        else:
            resolved = None
            port = None
        instruction_fingerprint = _sha256(instruction)
        idempotency_key = request_id or instruction_fingerprint
        operation = self._store.create_operation(
            task_id,
            "code_generate",
            f"code-generate:{task_id}:{alias}:{planner_mode}:{resolved.selector if resolved else 'offline'}:{resolved.connection_revision if resolved else 1}:{idempotency_key}",
            {
                "alias": alias,
                "instruction_fingerprint": instruction_fingerprint,
                "planner": planner_mode,
                "provider_profile": resolved.connection_id if resolved else None,
                "provider_revision": resolved.connection_revision if resolved else None,
                "model_selector": resolved.selector if resolved else None,
                "request_id": idempotency_key,
            },
        )
        run, created = self._store.get_or_create_run(
            task_id, operation.operation_id, "code_generate"
        )
        if not created:
            _close_port(port)
            return _generate_result(self._store, run)
        if planner_mode == "live":
            assert resolved is not None and port is not None
            try:
                return self._generate_live(
                    run,
                    alias,
                    instruction,
                    instruction_fingerprint,
                    resolved,
                    port,
                    selected_context,
                )
            finally:
                _close_port(port)
        journal = _journal(self._store, run)
        policy = _fake_policy()
        artifacts = FileArtifactStore(self._store)
        registry = ToolRegistry(
            policy, FakeExecutor(artifacts, journal, stdout=b"fake diagnostic\n")
        )
        evidence = FileRuntimeEvidence(artifacts, journal)

        if planner_mode == "replay" and instruction != _REPLAY_INSTRUCTION:
            planner: PlannerPort = RecordedReplayPlanner("builtin-replay-v1", {})
            workspace = None
        else:
            try:
                workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
                    task_id, alias, run.run_id
                )
            except DomainError as error:
                self._store.set_run_status(run.run_id, RunStatus.RUNNING)
                journal.append("run.started", {})
                self._store.set_run_status(
                    run.run_id, RunStatus.FAILED, error_code=error.code
                )
                journal.append("run.failed", {"error_code": error.code})
                return _generate_result(
                    self._store, self._store.get_run(task_id, run.run_id)
                )
            plan = _plan(run, workspace, instruction_fingerprint)
            planner = (
                _replay_model_planner(
                    run, instruction, plan.steps[0].tool_request, evidence
                )
                if planner_mode == "replay-model"
                else (
                    FakePlanner(plan)
                    if planner_mode == "fake"
                    else RecordedReplayPlanner(
                        "builtin-replay-v1", {instruction_fingerprint: plan}
                    )
                )
            )
            _save_simulation_approval(self._store, policy, plan.steps[0].tool_request)

        runtime = AgentRuntime(
            self._store,
            planner,
            RegistryToolGateway(self._project_root, registry),
            evidence,
        )
        result = runtime.execute(
            task_id,
            operation.operation_id,
            run.run_id,
            instruction,
            harness=(
                (
                    lambda: (
                        _code_harness(
                            self._project_root,
                            workspace,
                            policy,
                            run,
                            plan.steps[0].tool_request,
                        )
                        .run()
                        .status
                        is HarnessStatus.PASSED
                    )
                )
                if workspace is not None
                else lambda: False
            ),
        )
        return _generate_result(
            self._store, self._store.get_run(task_id, result.run.run_id)
        )

    def _generate_live(
        self,
        run: RunRecord,
        alias: str,
        instruction: str,
        instruction_fingerprint: str,
        resolved: ResolvedModel,
        port: ModelPort,
        context_files: tuple[str, ...],
    ) -> CodeGenerateResult:
        """Generate and retain a proposal without constructing an AgentRuntime."""
        journal, artifacts = _journal(self._store, run), FileArtifactStore(self._store)
        try:
            self._store.set_run_status(run.run_id, RunStatus.RUNNING)
            journal.append("run.started", {"mode": "live"})
            workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
                run.task_id, alias, run.run_id
            )
            snapshot = artifacts.write_bytes(
                run.task_id,
                run.run_id,
                producer="live_provider",
                relative_path=_LIVE_RESOLVED_PATH,
                content=json.dumps(
                    resolved.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode(),
                input_fingerprint=instruction_fingerprint,
                operation_id=run.operation_id,
            )
            documents = self._approved_documents(run.task_id)
            files = _context_files(workspace, context_files)
            prompt = _live_prompt(instruction, documents, files)
            context = artifacts.write_bytes(
                run.task_id,
                run.run_id,
                producer="live_provider",
                relative_path=_LIVE_CONTEXT_PATH,
                content=_context_manifest(documents, files).encode(),
                input_fingerprint=_sha256(prompt),
                operation_id=run.operation_id,
            )
            context_ref = _artifact_ref(context)
            journal.append(
                "stage.progress",
                {
                    "context_artifact_id": context.artifact_id,
                "resolved_model_artifact_id": snapshot.artifact_id,
                    "worktree_file_count": len(files),
                },
            )
            request = ModelRequest(
                task_id=run.task_id,
                operation_id=run.operation_id,
                run_id=run.run_id,
                provider_profile_name=resolved.connection_id,
                provider_type=resolved.provider_type,
                model=resolved.model_id,
                profile_revision=resolved.connection_revision,
                timeout_seconds=resolved.timeout_seconds,
                output_budget_bytes=min(resolved.output_limit, MAX_PATCH_DIFF_BYTES),
                request_options=resolved.request_options,
                allowed_input_artifact_refs=(context_ref,),
                input_fingerprint=model_request_fingerprint(prompt, (context_ref,)),
                user_instruction=prompt,
            )
            proposal = self._live_model_proposal(run, artifacts, journal, request, port)
            proposal_record = artifacts.write_bytes(
                run.task_id,
                run.run_id,
                producer="live_provider",
                relative_path=_LIVE_PROPOSAL_PATH,
                content=proposal.model_dump_json().encode(),
                input_fingerprint=request.input_fingerprint,
                operation_id=run.operation_id,
            )
            journal.append(
                "stage.progress",
                {
                    "patch_proposal_artifact_id": proposal_record.artifact_id,
                    "patch_count": len(proposal.patches),
                },
            )
            self._store.set_run_status(run.run_id, RunStatus.SUCCEEDED)
            journal.append("run.succeeded", {})
        except DomainError as error:
            _fail_live_run(self._store, journal, run, error.code)
        except (OSError, RuntimeError, TypeError, ValueError):
            _fail_live_run(self._store, journal, run, "LIVE_PROPOSAL_FAILED")
        return _generate_result(
            self._store, self._store.get_run(run.task_id, run.run_id)
        )

    def _live_model_proposal(
        self,
        run: RunRecord,
        artifacts: FileArtifactStore,
        journal: JsonlEventJournal,
        request: ModelRequest,
        port: ModelPort,
    ) -> PatchProposal:
        started_at = datetime.now(UTC)
        response_ref = None
        try:
            parts: list[str] = []
            size = 0
            for chunk in port.stream(request):
                size += len(chunk.content.encode("utf-8"))
                if size > request.output_budget_bytes:
                    raise DomainError(
                        "MODEL_OUTPUT_LIMIT_EXCEEDED",
                        "model output exceeded its budget",
                    )
                journal.append("model.delta", {"content": chunk.content})
                parts.append(chunk.content)
            content = "".join(parts)
            response = artifacts.write_bytes(
                run.task_id,
                run.run_id,
                producer="model_port",
                relative_path=_LIVE_RESPONSE_PATH,
                content=json.dumps(
                    {"schema_version": 1, "content": content}, separators=(",", ":")
                ).encode(),
                input_fingerprint=request.input_fingerprint,
                operation_id=run.operation_id,
            )
            response_ref = _artifact_ref(response)
            journal.append(
                "model.message_completed",
                {"response_artifact_ref": response_ref.model_dump(mode="json")},
            )
            try:
                proposal = PatchProposal.model_validate_json(content)
            except ValidationError as error:
                raise DomainError(
                    "PATCH_PROPOSAL_INVALID",
                    "model response is not a valid patch proposal",
                ) from error
            _write_live_call(
                artifacts,
                request,
                ModelCallStatus.SUCCEEDED,
                response_ref,
                "patch proposal stored",
                started_at,
            )
            return proposal
        except DomainError as error:
            try:
                _write_live_call(
                    artifacts,
                    request,
                    ModelCallStatus.FAILED,
                    response_ref,
                    error.code,
                    started_at,
                )
                journal.append("model.failed", {"error_code": error.code})
            except DomainError as persistence_error:
                raise persistence_error from error
            raise
        except Exception as error:
            failure = DomainError("MODEL_CALL_FAILED", "model call failed")
            try:
                _write_live_call(
                    artifacts,
                    request,
                    ModelCallStatus.FAILED,
                    response_ref,
                    failure.code,
                    started_at,
                )
                journal.append("model.failed", {"error_code": failure.code})
            except DomainError as persistence_error:
                raise persistence_error from error
            raise failure from error

    def _approved_documents(self, task_id: str) -> list[tuple[ArtifactRecord, str]]:
        artifacts = FileArtifactStore(self._store)
        reports = HarnessReportStore(self._store)
        for run in reversed(self._store.list_runs(task_id)):
            if (
                run.status is not RunStatus.SUCCEEDED
                or run.stage != "quality_artifacts"
                or not _approved_quality_reports(reports, run.run_id)
            ):
                continue
            records = {
                record.relative_path: record
                for record in artifacts.list(task_id, run.run_id)
            }
            if not all(path in records for path in _LIVE_DOCUMENT_PATHS):
                continue
            values: list[tuple[ArtifactRecord, str]] = []
            total = 0
            for path in _LIVE_DOCUMENT_PATHS:
                record = artifacts.verify(
                    task_id, run.run_id, records[path].artifact_id
                )
                raw = (
                    self._store.event_journal_path(run.run_id).parent
                    / record.relative_path
                ).read_bytes()
                total += len(raw)
                if total > _LIVE_MAX_CONTEXT_BYTES:
                    raise DomainError(
                        "MODEL_CONTEXT_LIMIT_EXCEEDED",
                        "approved documents exceed the live context limit",
                    )
                try:
                    values.append((record, raw.decode("utf-8")))
                except UnicodeDecodeError as error:
                    raise DomainError(
                        "MODEL_CONTEXT_INVALID", "approved document is not UTF-8"
                    ) from error
            return values
        return []

    def prepare_only(
        self, task_id: str, source_run_id: str, request_id: str | None = None
    ) -> PrepareOnlyResult:
        source_run = self._store.get_run(task_id, source_run_id)
        if (
            source_run.status is not RunStatus.SUCCEEDED
            or source_run.stage != "code_generate"
        ):
            raise DomainError(
                "CODE_HANDOFF_PRECONDITION_FAILED",
                "run is not a successful code generation",
            )
        idempotency_key = request_id or source_run_id
        operation = self._store.create_operation(
            task_id,
            "code_apply_prepare",
            f"code-apply-prepare:{source_run_id}:{idempotency_key}",
            {"request_id": idempotency_key, "source_run_id": source_run_id},
        )
        run, created = self._store.get_or_create_run(
            task_id, operation.operation_id, "code_apply_prepare"
        )
        if not created:
            if run.status is not RunStatus.SUCCEEDED:
                raise DomainError(
                    "CODE_HANDOFF_PRECONDITION_FAILED",
                    "apply preparation is not complete",
                )
            return _prepare_result(self._store, run)

        journal = _journal(self._store, run)
        self._store.set_run_status(run.run_id, RunStatus.RUNNING)
        journal.append("run.started", {"source_run_id": source_run_id})
        try:
            workspace, report, verification, approval, tool_runs, artifacts = (
                self._source_handoff_evidence(source_run)
            )
            verification_record = FileArtifactStore(self._store).write_bytes(
                task_id,
                run.run_id,
                producer="code_apply_prepare",
                relative_path=_VERIFICATION_PATH,
                content=json.dumps(
                    _verification_value(source_run, report, verification),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                input_fingerprint=verification.input_fingerprint,
                operation_id=run.operation_id,
            )
            handoff = _handoff_value(
                run,
                source_run,
                workspace,
                report,
                verification_record,
                approval,
                tool_runs,
                artifacts,
            )
            handoff_record = FileArtifactStore(self._store).write_bytes(
                task_id,
                run.run_id,
                producer="code_apply_prepare",
                relative_path=_HANDOFF_PATH,
                content=json.dumps(
                    handoff, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                ).encode("utf-8"),
                input_fingerprint=report.input_fingerprint,
                operation_id=run.operation_id,
            )
            journal.append(
                "stage.progress", {"handoff_artifact_id": handoff_record.artifact_id}
            )
            completed = self._store.set_run_status(run.run_id, RunStatus.SUCCEEDED)
            journal.append("run.succeeded", {})
        except DomainError as error:
            self._store.set_run_status(
                run.run_id, RunStatus.FAILED, error_code=error.code
            )
            journal.append("run.failed", {"error_code": error.code})
            raise
        return PrepareOnlyResult(
            task_id=task_id,
            operation_id=completed.operation_id,
            run_id=completed.run_id,
            status=completed.status.value,
            handoff_artifact_ref=_artifact_ref_value(handoff_record),
        )

    def _source_handoff_evidence(
        self, run: RunRecord
    ) -> tuple[
        PreparedWorkspace,
        HarnessReport,
        HarnessReport,
        ApprovalRecord,
        list[ToolResult],
        list[ArtifactRecord],
    ]:
        report = HarnessReportStore(self._store).load(
            run.run_id, CODE_HARNESS_REPORT_NAME
        )
        if (
            report.task_id != run.task_id
            or report.operation_id != run.operation_id
            or report.run_id != run.run_id
            or report.stage != run.stage
            or report.harness_name != "code_harness"
            or report.status is not HarnessStatus.PASSED
        ):
            raise DomainError(
                "CODE_HARNESS_FAILED",
                "code harness report is not a passed Run evidence record",
            )
        plan, _plan_record = self._load_plan(run)
        request = _require_fake_request(plan)
        workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
            run.task_id, _alias_from_request(request), run.run_id
        )
        policy = _fake_policy()
        approval = _simulation_approval(self._store, run, policy, request)
        refreshed = _code_harness(
            self._project_root, workspace, policy, run, request, approval.approval_id
        ).verify()
        if refreshed.status is not HarnessStatus.PASSED:
            raise DomainError(
                "CODE_HARNESS_FAILED", "code harness evidence is stale or has failed"
            )
        artifacts = _verified_artifacts(self._store, run.task_id, run.run_id)
        tool_runs = FileToolRunStore(self._store).list(run.task_id, run.run_id)
        if not tool_runs:
            raise DomainError(
                "CODE_HANDOFF_EVIDENCE_INVALID", "no persisted tool result exists"
            )
        return workspace, report, refreshed, approval, tool_runs, artifacts

    def _load_plan(self, run: RunRecord) -> tuple[AgentPlan, ArtifactRecord]:
        artifacts = FileArtifactStore(self._store)
        records = [
            record
            for record in artifacts.list(run.task_id, run.run_id)
            if record.relative_path == _PLAN_PATH
        ]
        if len(records) != 1:
            raise DomainError(
                "CODE_HANDOFF_EVIDENCE_INVALID",
                "agent plan artifact is missing or ambiguous",
            )
        record = artifacts.verify(run.task_id, run.run_id, records[0].artifact_id)
        path = self._store.event_journal_path(run.run_id).parent / record.relative_path
        try:
            plan = AgentPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DomainError(
                "CODE_HANDOFF_EVIDENCE_INVALID", "agent plan artifact is invalid"
            ) from error
        if (plan.task_id, plan.operation_id, plan.run_id) != (
            run.task_id,
            run.operation_id,
            run.run_id,
        ):
            raise DomainError(
                "CODE_HANDOFF_EVIDENCE_INVALID", "agent plan does not belong to the Run"
            )
        return plan, record


def _operation_run(
    store: FileTaskStore, task_id: str, operation_id: str, stage: str
) -> RunRecord | None:
    runs = [
        run
        for run in store.list_runs(task_id)
        if run.operation_id == operation_id and run.stage == stage
    ]
    if len(runs) > 1:
        raise DomainError(
            "STORAGE_INVALID", "operation has multiple runs for one idempotency key"
        )
    return runs[0] if runs else None


def _generate_result(store: FileTaskStore, run: RunRecord) -> CodeGenerateResult:
    records = [
        record
        for record in FileArtifactStore(store).list(run.task_id, run.run_id)
        if record.relative_path == _PLAN_PATH
    ]
    proposals = [
        record
        for record in FileArtifactStore(store).list(run.task_id, run.run_id)
        if record.relative_path == _LIVE_PROPOSAL_PATH
    ]
    plan_ref = _plan_ref(records[0]) if len(records) == 1 else None
    return CodeGenerateResult(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        status=run.status.value,
        error_code=run.error_code,
        plan_artifact_ref=plan_ref,
        patch_proposal_artifact_ref=_artifact_ref_value(proposals[0])
        if len(proposals) == 1
        else None,
    )


def _prepare_result(store: FileTaskStore, run: RunRecord) -> PrepareOnlyResult:
    records = [
        record
        for record in FileArtifactStore(store).list(run.task_id, run.run_id)
        if record.relative_path == _HANDOFF_PATH
    ]
    if len(records) != 1:
        raise DomainError(
            "CODE_HANDOFF_EVIDENCE_INVALID", "prepared handoff artifact is missing"
        )
    record = FileArtifactStore(store).verify(
        run.task_id, run.run_id, records[0].artifact_id
    )
    return PrepareOnlyResult(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        status=run.status.value,
        handoff_artifact_ref=_artifact_ref_value(record),
    )


def _plan_ref(record: ArtifactRecord) -> dict[str, object]:
    return {
        "artifact_id": record.artifact_id,
        "operation_id": record.operation_id,
        "relative_path": record.relative_path,
        "run_id": record.run_id,
        "schema_version": 1,
        "sha256": record.sha256,
        "task_id": record.task_id,
    }


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


def _plan(
    run: RunRecord, workspace: PreparedWorkspace, instruction_fingerprint: str
) -> AgentPlan:
    request = ToolRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        workspace_relative_path=_workspace_path_value(workspace),
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
        steps=(
            PlanStep(
                step_id=1,
                summary="run deterministic workspace diagnostic",
                tool_request=request,
            ),
        ),
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
        raise DomainError(
            "APPROVAL_REQUIRED", "a current simulation-only fake approval is required"
        )
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
        raise DomainError(
            "CODE_HANDOFF_EVIDENCE_INVALID", "agent plan is not the built-in fake plan"
        )
    request = plan.steps[0].tool_request
    if (
        request.tool_id != _FAKE_TOOL_ID
        or request.argv != _FAKE_ARGV
        or request.timeout_seconds != 1
        or request.output_budget_bytes != 1024
    ):
        raise DomainError(
            "CODE_HANDOFF_EVIDENCE_INVALID", "agent plan is not the built-in fake plan"
        )
    return request


def _alias_from_request(request: ToolRequest) -> str:
    return Path(request.workspace_relative_path).parts[4]


def _verified_artifacts(
    store: FileTaskStore, task_id: str, run_id: str
) -> list[ArtifactRecord]:
    artifacts = FileArtifactStore(store)
    records = artifacts.list(task_id, run_id)
    if not records:
        raise DomainError(
            "CODE_HANDOFF_EVIDENCE_INVALID", "no persisted artifacts exist"
        )
    return [artifacts.verify(task_id, run_id, record.artifact_id) for record in records]


def _handoff_value(
    run: RunRecord,
    source_run: RunRecord,
    workspace: PreparedWorkspace,
    report: HarnessReport,
    verification_record: ArtifactRecord,
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
        "approval_refs": [
            {"approval_id": approval.approval_id, "simulation_only": True}
        ],
        "code_harness_report_ref": _code_harness_report_ref(source_run, report),
        "freshness_verification_artifact_ref": _artifact_ref_value(verification_record),
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
        "source_operation_id": source_run.operation_id,
        "source_run_id": source_run.run_id,
        "task_id": run.task_id,
        "tool_run_refs": [
            {
                "path": (
                    f"tasks/{source_run.task_id}/runs/{source_run.run_id}/"
                    f"tool-runs/{result.tool_run_id}.json"
                ),
                "status": result.status.value,
                "tool_run_id": result.tool_run_id,
            }
            for result in tool_runs
        ],
        "workspace": {
            "base_sha": workspace.base_sha,
            "branch": workspace.branch,
            "path": _workspace_path_value(workspace),
            "repo_alias": workspace.repo_alias,
        },
    }


def _verification_value(
    source_run: RunRecord, source_report: HarnessReport, verification: HarnessReport
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_code_harness_report_ref": _code_harness_report_ref(
            source_run, source_report
        ),
        "verification": verification.model_dump(mode="json"),
    }


def _code_harness_report_ref(
    source_run: RunRecord, report: HarnessReport
) -> dict[str, object]:
    value = report.model_dump(mode="json")
    return {
        "input_fingerprint": value["input_fingerprint"],
        "path": (
            f"tasks/{source_run.task_id}/runs/{source_run.run_id}/"
            f"harness/{CODE_HARNESS_REPORT_NAME}"
        ),
        "report_id": value["report_id"],
    }


def _artifact_ref_value(record: ArtifactRecord) -> dict[str, object]:
    return {
        "artifact_id": record.artifact_id,
        "record_path": f"artifacts/records/{record.artifact_id}.json",
        "relative_path": record.relative_path,
        "sha256": record.sha256,
    }


def _artifact_ref(record: ArtifactRecord):
    from yipilot.domain.execution import ArtifactRef

    return ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )


def _context_files(
    workspace: PreparedWorkspace, values: tuple[str, ...]
) -> list[tuple[str, str, str]]:
    if len(values) > _LIVE_MAX_CONTEXT_FILES or len(set(values)) != len(values):
        raise DomainError(
            "MODEL_CONTEXT_INVALID",
            "context files must be unique and within the file limit",
        )
    selected: list[tuple[str, str, str]] = []
    total = 0
    for value in values:
        posix, windows = PurePosixPath(value), PureWindowsPath(value)
        if (
            not value
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or bool(windows.root)
            or ".." in posix.parts
            or ".." in windows.parts
            or ".git" in {part.casefold() for part in posix.parts}
        ):
            raise DomainError(
                "MODEL_CONTEXT_PATH_DENIED", "context path is not in the task worktree"
            )
        candidate = workspace.path.joinpath(*posix.parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise DomainError(
                "MODEL_CONTEXT_PATH_DENIED",
                "context file must be a regular task-worktree file",
            )
        resolved = candidate.resolve(strict=True)
        if workspace.path != resolved and workspace.path not in resolved.parents:
            raise DomainError(
                "MODEL_CONTEXT_PATH_DENIED", "context file escapes the task worktree"
            )
        raw = candidate.read_bytes()
        total += len(raw)
        if len(raw) > _LIVE_MAX_FILE_BYTES or total > _LIVE_MAX_CONTEXT_BYTES:
            raise DomainError(
                "MODEL_CONTEXT_LIMIT_EXCEEDED",
                "context files exceed the live context limit",
            )
        try:
            selected.append(
                (value.replace("\\", "/"), _sha256_bytes(raw), raw.decode("utf-8"))
            )
        except UnicodeDecodeError as error:
            raise DomainError(
                "MODEL_CONTEXT_INVALID", "context file is not UTF-8"
            ) from error
    return selected


def _approved_quality_reports(reports: HarnessReportStore, run_id: str) -> bool:
    try:
        return all(
            reports.load(run_id, name).status is HarnessStatus.PASSED
            for name in (SRS_HARNESS_REPORT_NAME, SDD_HARNESS_REPORT_NAME)
        )
    except DomainError:
        return False


def _live_prompt(
    instruction: str,
    documents: list[tuple[ArtifactRecord, str]],
    files: list[tuple[str, str, str]],
) -> str:
    value = {
        "approved_documents": [
            {
                "artifact_id": record.artifact_id,
                "path": record.relative_path,
                "content": content,
            }
            for record, content in documents
        ],
        "instruction": instruction,
        "response_contract": {
            "schema_version": 1,
            "summary": "short summary",
            "patches": [
                {
                    "path": "relative/path.py",
                    "unified_diff": "--- a/relative/path.py\\n+++ b/relative/path.py\\n@@ -1 +1 @@\\n-old\\n+new",
                }
            ],
        },
        "worktree_files": [
            {"path": path, "sha256": digest, "content": content}
            for path, digest, content in files
        ],
    }
    prompt = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(prompt.encode("utf-8")) > 32 * 1024:
        raise DomainError(
            "MODEL_CONTEXT_LIMIT_EXCEEDED", "live model prompt exceeds its byte limit"
        )
    return prompt


def _context_manifest(
    documents: list[tuple[ArtifactRecord, str]], files: list[tuple[str, str, str]]
) -> str:
    lines = ["# Live model context", "", "## Approved documents"]
    lines.extend(
        f"- `{record.artifact_id}` `{record.relative_path}` sha256:{record.sha256}"
        for record, _content in documents
    )
    lines.extend(["", "## Selected task worktree files"])
    lines.extend(f"- `{path}` sha256:{digest}" for path, digest, _content in files)
    return "\n".join(lines) + "\n"


def _write_live_call(
    artifacts: FileArtifactStore,
    request: ModelRequest,
    status: ModelCallStatus,
    response_ref: object,
    summary: str,
    started_at: datetime | None = None,
) -> None:
    from yipilot.domain.execution import ArtifactRef

    ref = response_ref if isinstance(response_ref, ArtifactRef) else None
    record = ModelCallRecord(
        task_id=request.task_id,
        operation_id=request.operation_id,
        run_id=request.run_id,
        provider_profile_name=request.provider_profile_name,
        provider_type=request.provider_type,
        model=request.model,
        profile_revision=request.profile_revision,
        timeout_seconds=request.timeout_seconds,
        output_budget_bytes=request.output_budget_bytes,
        input_fingerprint=request.input_fingerprint,
        input_artifact_refs=request.allowed_input_artifact_refs,
        status=status,
        started_at=started_at or datetime.now(UTC),
        ended_at=datetime.now(UTC),
        response_artifact_ref=ref,
        redacted_summary=summary,
    )
    artifacts.write_bytes(
        request.task_id,
        request.run_id,
        producer="model_port",
        relative_path=_LIVE_CALL_PATH,
        content=record.model_dump_json(exclude_none=True).encode(),
        input_fingerprint=request.input_fingerprint,
        operation_id=request.operation_id,
    )


def _fail_live_run(
    store: FileTaskStore, journal: JsonlEventJournal, run: RunRecord, error_code: str
) -> None:
    try:
        store.set_run_status(run.run_id, RunStatus.FAILED, error_code=error_code)
    finally:
        try:
            journal.append("run.failed", {"error_code": error_code})
        except DomainError:
            pass


def _close_port(port: object | None) -> None:
    close = getattr(port, "close", None)
    if callable(close):
        close()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _workspace_path_value(workspace: PreparedWorkspace) -> str:
    return (
        f".yipilot/tasks/{workspace.task_id}/repos/{workspace.repo_alias}/"
        f"workspaces/{workspace.run_id}"
    )


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
