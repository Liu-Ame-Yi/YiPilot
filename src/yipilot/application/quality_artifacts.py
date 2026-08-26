"""Run-scoped SRS, SDD and test-plan generation plus controlled pytest execution."""

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from yipilot.application.approval import ApprovalGate, approval_action_type
from yipilot.application.tool_registry import ToolRegistry
from yipilot.application.workspace_service import WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ArtifactRef,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    ToolRequest,
)
from yipilot.domain.model import (
    ModelCallRecord,
    ModelCallStatus,
    ModelChunk,
    ModelRequest,
    ModelResponse,
    model_request_fingerprint,
)
from yipilot.domain.models import RunStatus
from yipilot.harness.quality import UnitTestHarness
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import LocalCommandRunner
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.model_port import FakeModelPort, RecordedReplayModelPort
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord


@dataclass(frozen=True)
class QualityGenerationResult:
    task_id: str
    operation_id: str
    run_id: str
    artifact_refs: list[dict[str, str]]

    def value(self) -> dict[str, object]:
        return {"task_id": self.task_id, "operation_id": self.operation_id, "run_id": self.run_id, "artifact_refs": self.artifact_refs}


class QualityArtifactService:
    """Create offline-replayable quality documents from only explicit PRD input."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._store = FileTaskStore(self._project_root)

    def generate(self, task_id: str, *, prd_text: str | None = None, source_run_id: str | None = None, prd_artifact_id: str | None = None, model_mode: str = "fake", profile_revision: int = 1, request_id: str | None = None) -> QualityGenerationResult:
        if model_mode not in {"fake", "replay"}:
            raise DomainError("MODEL_MODE_INVALID", "model mode must be fake or replay")
        if (prd_text is None) == (prd_artifact_id is None):
            raise DomainError("PRD_INPUT_INVALID", "provide exactly one PRD text or PRD Artifact")
        if prd_text is not None and not prd_text.strip():
            raise DomainError("PRD_INPUT_INVALID", "PRD text is required")
        self._store.get_task(task_id)
        source_fingerprint = hashlib.sha256((prd_text or prd_artifact_id or "").encode()).hexdigest()
        operation = self._store.create_operation(task_id, "quality_artifacts", f"quality:{task_id}:{model_mode}:{request_id or source_fingerprint}", {"model_mode": model_mode, "profile_revision": profile_revision, "source_fingerprint": source_fingerprint})
        run, created = self._store.get_or_create_run(task_id, operation.operation_id, "quality_artifacts")
        if not created:
            return QualityGenerationResult(task_id, operation.operation_id, run.run_id, [_value(record) for record in FileArtifactStore(self._store).list(task_id, run.run_id)])
        journal = _journal(self._store, run)
        artifacts = FileArtifactStore(self._store)
        self._store.set_run_status(run.run_id, RunStatus.RUNNING)
        journal.append("run.started", {})
        try:
            prd_content, source_ref = self._prd_content(task_id, prd_text, source_run_id, prd_artifact_id)
            prd = artifacts.write_bytes(task_id, run.run_id, producer="quality_artifacts", relative_path="artifacts/prd.md", content=prd_content.encode(), input_fingerprint=hashlib.sha256(prd_content.encode()).hexdigest(), operation_id=operation.operation_id)
            input_ref = _ref(prd)
            request = ModelRequest(task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id, provider_profile_name="offline-quality", provider_type="openai_compatible", model="quality-fixture", profile_revision=profile_revision, timeout_seconds=1, output_budget_bytes=4096, allowed_input_artifact_refs=(input_ref,), input_fingerprint=model_request_fingerprint("Generate quality artifacts from the approved PRD.", (input_ref,)), user_instruction="Generate quality artifacts from the approved PRD.")
            fixture = ModelResponse(chunks=(ModelChunk(content='{"fixture":"quality-documents-v1"}'),))
            port = FakeModelPort(fixture) if model_mode == "fake" else RecordedReplayModelPort("quality-replay-v1", {request.input_fingerprint: fixture})
            response = "".join(chunk.content for chunk in port.stream(request))
            response_record = artifacts.write_bytes(task_id, run.run_id, producer="model_port", relative_path="artifacts/quality-model-response.json", content=response.encode(), input_fingerprint=request.input_fingerprint, operation_id=operation.operation_id)
            call = ModelCallRecord(task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id, provider_profile_name=request.provider_profile_name, provider_type=request.provider_type, model=request.model, profile_revision=request.profile_revision, timeout_seconds=request.timeout_seconds, output_budget_bytes=request.output_budget_bytes, input_fingerprint=request.input_fingerprint, input_artifact_refs=(input_ref,), status=ModelCallStatus.SUCCEEDED, started_at=datetime.now(UTC), ended_at=datetime.now(UTC), response_artifact_ref=_ref(response_record), redacted_summary="offline quality fixture response stored")
            call_record = artifacts.write_bytes(task_id, run.run_id, producer="model_port", relative_path="artifacts/quality-model-call.json", content=call.model_dump_json(exclude_none=True).encode(), input_fingerprint=request.input_fingerprint, operation_id=operation.operation_id)
            srs = artifacts.write_bytes(task_id, run.run_id, producer="quality_artifacts", relative_path="artifacts/srs.md", content=_srs(prd, source_ref).encode(), input_fingerprint=prd.sha256, operation_id=operation.operation_id)
            sdd = artifacts.write_bytes(task_id, run.run_id, producer="quality_artifacts", relative_path="artifacts/sdd.md", content=_sdd(srs).encode(), input_fingerprint=prd.sha256, operation_id=operation.operation_id)
            plan = artifacts.write_bytes(task_id, run.run_id, producer="quality_artifacts", relative_path="artifacts/test-plan.md", content=_plan(srs, sdd).encode(), input_fingerprint=prd.sha256, operation_id=operation.operation_id)
            metadata = {"schema_version": 1, "generated_at": datetime.now(UTC).isoformat(), "task_id": run.task_id, "operation_id": run.operation_id, "run_id": run.run_id, "input_fingerprint": prd.sha256, "source_fingerprint": source_fingerprint, "provider_profile": {"name": request.provider_profile_name, "model": request.model, "revision": profile_revision}, "upstream_artifact_refs": [_value(prd), *([source_ref] if source_ref else []), _value(response_record), _value(call_record)], "artifacts": {"prd": _value(prd), "srs": _value(srs), "sdd": _value(sdd), "test_plan": _value(plan)}}
            meta = artifacts.write_bytes(task_id, run.run_id, producer="quality_artifacts", relative_path="artifacts/quality-metadata.json", content=json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(), input_fingerprint=prd.sha256, operation_id=operation.operation_id)
            journal.append("stage.progress", {"artifact_ids": [srs.artifact_id, sdd.artifact_id, plan.artifact_id, meta.artifact_id], "model_call_artifact_id": call_record.artifact_id})
            self._store.set_run_status(run.run_id, RunStatus.SUCCEEDED)
            journal.append("run.succeeded", {})
            return QualityGenerationResult(task_id, operation.operation_id, run.run_id, [_value(record) for record in (prd, srs, sdd, plan, meta)])
        except DomainError as error:
            self._store.set_run_status(run.run_id, RunStatus.FAILED, error_code=error.code)
            journal.append("run.failed", {"error_code": error.code})
            raise

    def run_pytest(self, task_id: str, alias: str, *, timeout_seconds: float = 60, request_id: str | None = None) -> dict[str, object]:
        operation = self._store.create_operation(task_id, "unit_test", f"pytest:{task_id}:{alias}:{request_id or timeout_seconds}", {"alias": alias, "timeout_seconds": timeout_seconds})
        run, created = self._store.get_or_create_run(task_id, operation.operation_id, "unit_test")
        if not created:
            return {"task_id": task_id, "operation_id": operation.operation_id, "run_id": run.run_id}
        workspace = WorkspaceService(self._project_root, GitRunner()).prepare(task_id, alias, run.run_id)
        journal, artifacts = _journal(self._store, run), FileArtifactStore(self._store)
        self._store.set_run_status(run.run_id, RunStatus.RUNNING)
        journal.append("run.started", {})
        policy = _pytest_policy(timeout_seconds)
        request = ToolRequest(task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id, workspace_relative_path=f".yipilot/tasks/{task_id}/repos/{alias}/workspaces/{run.run_id}", tool_id="python-pytest", argv=(sys.executable, "-m", "pytest"), timeout_seconds=timeout_seconds, output_budget_bytes=128 * 1024, env_names=("SYSTEMROOT", "WINDIR"), input_fingerprint=hashlib.sha256(f"pytest:{workspace.base_sha}".encode()).hexdigest())
        approval = ApprovalRecord(approval_id="pytest-explicit-command", task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id, action_type=approval_action_type(policy.tools[0], LocalCommandRunner.trust_level), target_fingerprint=request.fingerprint, policy_revision=policy.revision, decision=ApprovalDecision.APPROVED, decided_at=datetime.now(UTC), simulation_only=False)
        approvals = FileApprovalStore(self._store)
        approvals.save(approval)
        try:
            result = ToolRegistry(policy, LocalCommandRunner(artifacts, journal), ApprovalGate(approvals)).execute(self._project_root, request, environment={name: os.environ[name] for name in request.env_names if name in os.environ}, approval_id=approval.approval_id, confirm_token=request.fingerprint)
            report = UnitTestHarness(self._project_root, task_id, run.run_id, workspace, result.tool_run_id).run()
            status = RunStatus.SUCCEEDED if report.status.value == "passed" else RunStatus.FAILED
            self._store.set_run_status(run.run_id, status, error_code=None if status is RunStatus.SUCCEEDED else "UNIT_TEST_HARNESS_FAILED")
            journal.append("harness.completed", {"harness": "unit_test_harness", "status": report.status.value})
            journal.append("run.succeeded" if status is RunStatus.SUCCEEDED else "run.failed", {} if status is RunStatus.SUCCEEDED else {"error_code": "UNIT_TEST_HARNESS_FAILED"})
            return {"task_id": task_id, "operation_id": operation.operation_id, "run_id": run.run_id, "tool_run_id": result.tool_run_id, "status": report.status.value}
        except DomainError as error:
            self._store.set_run_status(run.run_id, RunStatus.FAILED, error_code=error.code)
            journal.append("run.failed", {"error_code": error.code})
            raise

    def _prd_content(self, task_id: str, text: str | None, source_run_id: str | None, artifact_id: str | None) -> tuple[str, dict[str, str] | None]:
        if text is not None:
            return text, None
        if source_run_id is None or artifact_id is None:
            raise DomainError("PRD_INPUT_INVALID", "PRD Artifact requires source run ID")
        record = FileArtifactStore(self._store).verify(task_id, source_run_id, artifact_id)
        if not record.relative_path.endswith(".md"):
            raise DomainError("PRD_INPUT_INVALID", "PRD Artifact must be Markdown")
        root = self._store.event_journal_path(source_run_id).parent
        return (root / record.relative_path).read_text(encoding="utf-8"), _value(record)


def _pytest_policy(timeout: float) -> ExecutionPolicy:
    return ExecutionPolicy(revision="quality-pytest-v1", tools=(ToolPolicy(tool_id="python-pytest", kind=ToolKind.TEST, executable=sys.executable, argv_schema=(ArgvRule(literal="-m"), ArgvRule(literal="pytest")), allowed_env_names=("SYSTEMROOT", "WINDIR"), max_output_bytes=128 * 1024, max_timeout_seconds=min(timeout, 600)),))


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id, stage=run.stage)


def _ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(artifact_id=record.artifact_id, task_id=record.task_id, operation_id=record.operation_id, run_id=record.run_id, relative_path=record.relative_path, sha256=record.sha256)


def _value(record: ArtifactRecord) -> dict[str, str]:
    return {"artifact_id": record.artifact_id, "task_id": record.task_id, "operation_id": record.operation_id, "run_id": record.run_id, "relative_path": record.relative_path, "sha256": record.sha256}


def _srs(prd: ArtifactRecord, source_ref: dict[str, str] | None) -> str:
    source = source_ref["artifact_id"] if source_ref else "explicit-text"
    return f"# SRS\n\nUpstream PRD Artifact: {prd.artifact_id}; source: {source}.\n\n## Goals and scope\nDeliver the approved PRD capability.\n\n## Functional requirements\n- FR-001: Preserve the explicit PRD intent in run-scoped artifacts.\n\n## Non-functional requirements\n- NFR-001: Keep evidence deterministic and redacted.\n\n## Acceptance criteria\n- FR-001 has a tracked test plan.\n\n## Assumptions and constraints\n- Only explicit PRD input is used.\n"


def _sdd(srs: ArtifactRecord) -> str:
    return f"# SDD\n\n## Architecture and components\nQualityArtifactService and deterministic Harnesses.\n\n## Interfaces and data flow\nPRD Artifact -> SRS -> SDD -> Test Plan.\n\n## Constraints\nPython/pytest only; no arbitrary command execution.\n\n## Error handling\nStale evidence blocks downstream stages.\n\n## Test traceability\n- FR-001 -> pytest Harness.\n\n## SRS reference\nSRS Artifact: {srs.artifact_id}.\n"


def _plan(srs: ArtifactRecord, sdd: ArtifactRecord) -> str:
    return f"# Test Plan\n\n## Test scope\nRun deterministic Python unit tests.\n\n## Preconditions\nA prepared task workspace and fresh Approval.\n\n## Test cases and commands\n- `python -m pytest`\n\n## Expected results\nExit status zero and intact ToolRun Artifacts.\n\n## Requirements and design references\n- FR-001; SRS Artifact: {srs.artifact_id}; SDD Artifact: {sdd.artifact_id}.\n"