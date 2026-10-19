"""Run-scoped SRS, SDD and test-plan generation plus controlled pytest execution."""

import hashlib
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    MAX_TOOL_TIMEOUT_SECONDS,
    QUALITY_PYTEST_MAX_OUTPUT_BYTES,
    QUALITY_PYTEST_MAX_TIMEOUT_SECONDS,
    QUALITY_PYTEST_POLICY_REVISION,
    QUALITY_PYTEST_TOOL_ID,
    ArgvRule,
    ArtifactRef,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolResult,
)
from yipilot.domain.model import (
    ModelCallRecord,
    ModelCallStatus,
    ModelChunk,
    ModelRequest,
    ModelResponse,
)
from yipilot.domain.models import RunStatus
from yipilot.harness.quality import UnitTestHarness
from yipilot.models.fixtures import FakeModelPort, RecordedReplayModelPort
from yipilot.models.store import FileProviderStore
from yipilot.persistence.approval_store import FileApprovalStore
from yipilot.persistence.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.ownership import exclusive_execution
from yipilot.persistence.task_store import FileTaskStore, RunRecord
from yipilot.security.approval import ApprovalGate
from yipilot.workspace.executors import LocalCommandRunner
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.tool_registry import ToolRegistry
from yipilot.workspace.workspace_service import PreparedWorkspace, WorkspaceService


class _QualityDocuments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1, le=1)
    srs: str = Field(min_length=1)
    sdd: str = Field(min_length=1)
    test_plan: str = Field(min_length=1)


def _fixture_fingerprint(prd: str) -> str:
    return hashlib.sha256(prd.encode()).hexdigest()


def _fixture_response(payload: str) -> ModelResponse:
    return ModelResponse(chunks=(ModelChunk(content=payload),))


_DEFAULT_QUALITY_FIXTURES: dict[str, ModelResponse] = {
    _fixture_fingerprint(
        "Build a redacted offline quality workflow."
    ): _fixture_response(
        '{"schema_version":1,"srs":"# SRS\\n\\n## Goals and scope\\nOffline audit report.\\n\\n## Functional requirements\\n- FR-101: Produce redacted audit evidence.\\n\\n## Non-functional requirements\\n- NFR-101: Store bounded artifacts.\\n\\n## Acceptance criteria\\n- FR-101 has a passing report.\\n\\n## Assumptions and constraints\\n- Explicit PRD only.\\n","sdd":"# SDD\\n\\n## Architecture and components\\nQuality artifacts.\\n\\n## Interfaces and data flow\\nPRD -> SRS -> SDD.\\n\\n## Constraints\\nPython only.\\n\\n## Error handling\\nStale evidence blocks.\\n\\n## Test traceability\\nFR-101.\\n\\n## SRS reference\\nSRS Artifact: {SRS_ARTIFACT}.\\n","test_plan":"# Test Plan\\n\\n## Test scope\\nFR-101.\\n\\n## Preconditions\\nFresh evidence.\\n\\n## Test cases and commands\\n`python -m pytest`\\n\\n## Expected results\\nExit zero.\\n\\n## Requirements and design references\\nFR-101; SRS Artifact: {SRS_ARTIFACT}; SDD Artifact: {SDD_ARTIFACT}.\\n"}'
    ),
    _fixture_fingerprint("Build alpha capability."): _fixture_response(
        '{"schema_version":1,"srs":"# SRS\\n\\n## Goals and scope\\nAlpha capability.\\n\\n## Functional requirements\\n- FR-201: Deliver alpha.\\n\\n## Non-functional requirements\\n- NFR-201: Keep artifacts redacted.\\n\\n## Acceptance criteria\\n- FR-201 is verified.\\n\\n## Assumptions and constraints\\n- Explicit PRD only.\\n","sdd":"# SDD\\n\\n## Architecture and components\\nAlpha component.\\n\\n## Interfaces and data flow\\nPRD -> SRS -> SDD.\\n\\n## Constraints\\nPython only.\\n\\n## Error handling\\nStale evidence blocks.\\n\\n## Test traceability\\nFR-201.\\n\\n## SRS reference\\nSRS Artifact: {SRS_ARTIFACT}.\\n","test_plan":"# Test Plan\\n\\n## Test scope\\nFR-201.\\n\\n## Preconditions\\nFresh evidence.\\n\\n## Test cases and commands\\n`python -m pytest`\\n\\n## Expected results\\nExit zero.\\n\\n## Requirements and design references\\nFR-201; SRS Artifact: {SRS_ARTIFACT}; SDD Artifact: {SDD_ARTIFACT}.\\n"}'
    ),
    _fixture_fingerprint("Build beta capability."): _fixture_response(
        '{"schema_version":1,"srs":"# SRS\\n\\n## Goals and scope\\nBeta capability.\\n\\n## Functional requirements\\n- FR-301: Deliver beta.\\n\\n## Non-functional requirements\\n- NFR-301: Keep artifacts redacted.\\n\\n## Acceptance criteria\\n- FR-301 is verified.\\n\\n## Assumptions and constraints\\n- Explicit PRD only.\\n","sdd":"# SDD\\n\\n## Architecture and components\\nBeta component.\\n\\n## Interfaces and data flow\\nPRD -> SRS -> SDD.\\n\\n## Constraints\\nPython only.\\n\\n## Error handling\\nStale evidence blocks.\\n\\n## Test traceability\\nFR-301.\\n\\n## SRS reference\\nSRS Artifact: {SRS_ARTIFACT}.\\n","test_plan":"# Test Plan\\n\\n## Test scope\\nFR-301.\\n\\n## Preconditions\\nFresh evidence.\\n\\n## Test cases and commands\\n`python -m pytest`\\n\\n## Expected results\\nExit zero.\\n\\n## Requirements and design references\\nFR-301; SRS Artifact: {SRS_ARTIFACT}; SDD Artifact: {SDD_ARTIFACT}.\\n"}'
    ),
}




@dataclass(frozen=True)
class QualityGenerationResult:
    task_id: str
    operation_id: str
    run_id: str
    artifact_refs: list[dict[str, str]]

    def value(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "artifact_refs": self.artifact_refs,
        }


class QualityArtifactService:
    """Create offline-replayable quality documents from only explicit PRD input."""

    def __init__(
        self,
        project_root: Path,
        *,
        settings_dir: Path | None = None,
        fixture_responses: Mapping[str, ModelResponse] | None = None,
    ) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._providers = FileProviderStore(settings_dir)
        self._fixtures = dict(fixture_responses or _DEFAULT_QUALITY_FIXTURES)
        self._store = FileTaskStore(self._project_root)

    @exclusive_execution
    def generate(
        self,
        task_id: str,
        *,
        prd_text: str | None = None,
        source_run_id: str | None = None,
        prd_artifact_id: str | None = None,
        model_mode: str = "fake",
        profile_revision: int = 1,
        provider_profile_name: str = "offline-quality",
        request_id: str | None = None,
    ) -> QualityGenerationResult:
        _validate_quality_profile_revision(profile_revision)
        if model_mode not in {"fake", "replay"}:
            raise DomainError("MODEL_MODE_INVALID", "model mode must be fake or replay")
        if (prd_text is None) == (prd_artifact_id is None) or (
            prd_text is not None and not prd_text.strip()
        ):
            raise DomainError(
                "PRD_INPUT_INVALID", "provide one non-empty PRD text or PRD Artifact"
            )
        self._store.get_task(task_id)
        prd_content, source_ref = self._prd_content(
            task_id, prd_text, source_run_id, prd_artifact_id
        )
        source_fingerprint = _source_fingerprint(prd_content, source_ref)
        operation = self._store.create_operation(
            task_id,
            "quality_artifacts",
            f"quality:{task_id}:{model_mode}:{request_id or source_fingerprint}",
            {
                "model_mode": model_mode,
                "profile_revision": profile_revision,
                "source_fingerprint": source_fingerprint,
            },
        )
        run, created = self._store.get_or_create_run(
            task_id, operation.operation_id, "quality_artifacts"
        )
        if not created:
            return QualityGenerationResult(
                task_id,
                operation.operation_id,
                run.run_id,
                [
                    _value(record)
                    for record in FileArtifactStore(self._store).list(
                        task_id, run.run_id
                    )
                ],
            )
        journal, artifacts = _journal(self._store, run), FileArtifactStore(self._store)
        self._store.set_run_status(run.run_id, RunStatus.RUNNING)
        journal.append("run.started", {})
        try:
            profile_model, current_revision = "quality-fixture", profile_revision
            if provider_profile_name != "offline-quality":
                profile = self._providers.get(provider_profile_name)
                profile_model, current_revision = profile.model, profile.revision
            prd = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="quality_artifacts",
                relative_path="artifacts/prd.md",
                content=prd_content.encode(),
                input_fingerprint=_fixture_fingerprint(prd_content),
                operation_id=operation.operation_id,
            )
            input_ref = _ref(prd)
            request = ModelRequest(
                task_id=run.task_id,
                operation_id=run.operation_id,
                run_id=run.run_id,
                provider_profile_name=provider_profile_name,
                provider_type="openai_compatible",
                model=profile_model,
                profile_revision=current_revision,
                timeout_seconds=1,
                output_budget_bytes=4096,
                allowed_input_artifact_refs=(input_ref,),
                input_fingerprint=prd.sha256,
                user_instruction="Generate quality artifacts from the approved PRD.",
            )
            port = self._model_port(model_mode, request.input_fingerprint)
            response = "".join(chunk.content for chunk in port.stream(request))
            try:
                payload = _QualityDocuments.model_validate_json(response)
            except ValidationError as error:
                raise DomainError(
                    "QUALITY_MODEL_PAYLOAD_INVALID",
                    "model response is not a quality document payload",
                ) from error
            response_record = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="model_port",
                relative_path="artifacts/quality-model-response.json",
                content=response.encode(),
                input_fingerprint=request.input_fingerprint,
                operation_id=operation.operation_id,
            )
            call = ModelCallRecord(
                task_id=run.task_id,
                operation_id=run.operation_id,
                run_id=run.run_id,
                provider_profile_name=request.provider_profile_name,
                provider_type=request.provider_type,
                model=request.model,
                profile_revision=request.profile_revision,
                timeout_seconds=request.timeout_seconds,
                output_budget_bytes=request.output_budget_bytes,
                input_fingerprint=request.input_fingerprint,
                input_artifact_refs=(input_ref,),
                status=ModelCallStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                response_artifact_ref=_ref(response_record),
                redacted_summary="offline quality fixture response stored",
            )
            call_record = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="model_port",
                relative_path="artifacts/quality-model-call.json",
                content=call.model_dump_json(exclude_none=True).encode(),
                input_fingerprint=request.input_fingerprint,
                operation_id=operation.operation_id,
            )
            srs = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="quality_artifacts",
                relative_path="artifacts/srs.md",
                content=payload.srs.encode(),
                input_fingerprint=prd.sha256,
                operation_id=operation.operation_id,
            )
            sdd = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="quality_artifacts",
                relative_path="artifacts/sdd.md",
                content=payload.sdd.replace("{SRS_ARTIFACT}", srs.artifact_id).encode(),
                input_fingerprint=prd.sha256,
                operation_id=operation.operation_id,
            )
            plan = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="quality_artifacts",
                relative_path="artifacts/test-plan.md",
                content=payload.test_plan.replace("{SRS_ARTIFACT}", srs.artifact_id)
                .replace("{SDD_ARTIFACT}", sdd.artifact_id)
                .encode(),
                input_fingerprint=prd.sha256,
                operation_id=operation.operation_id,
            )
            metadata = {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "task_id": run.task_id,
                "operation_id": run.operation_id,
                "run_id": run.run_id,
                "input_fingerprint": prd.sha256,
                "source_fingerprint": source_fingerprint,
                "provider_profile": {
                    "name": request.provider_profile_name,
                    "model": request.model,
                    "revision": current_revision,
                },
                "upstream_artifact_refs": [
                    _value(prd),
                    *([source_ref] if source_ref else []),
                    _value(response_record),
                    _value(call_record),
                ],
                "artifacts": {
                    "prd": _value(prd),
                    "srs": _value(srs),
                    "sdd": _value(sdd),
                    "test_plan": _value(plan),
                },
            }
            meta = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="quality_artifacts",
                relative_path="artifacts/quality-metadata.json",
                content=json.dumps(
                    metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")
                ).encode(),
                input_fingerprint=prd.sha256,
                operation_id=operation.operation_id,
            )
            journal.append(
                "stage.progress",
                {
                    "artifact_ids": [
                        srs.artifact_id,
                        sdd.artifact_id,
                        plan.artifact_id,
                        meta.artifact_id,
                    ],
                    "model_call_artifact_id": call_record.artifact_id,
                },
            )
            self._store.set_run_status(run.run_id, RunStatus.SUCCEEDED)
            journal.append("run.succeeded", {})
            return QualityGenerationResult(
                task_id,
                operation.operation_id,
                run.run_id,
                [_value(record) for record in (prd, srs, sdd, plan, meta)],
            )
        except DomainError as error:
            self._store.set_run_status(
                run.run_id, RunStatus.FAILED, error_code=error.code
            )
            journal.append("run.failed", {"error_code": error.code})
            raise

    def _model_port(
        self, model_mode: str, fingerprint: str
    ) -> FakeModelPort | RecordedReplayModelPort:
        if model_mode == "fake":
            try:
                return FakeModelPort(self._fixtures[fingerprint])
            except KeyError as error:
                raise DomainError(
                    "QUALITY_FIXTURE_NOT_FOUND",
                    "no fake quality fixture matches this input",
                ) from error
        return RecordedReplayModelPort("quality-replay-v1", self._fixtures)

    @exclusive_execution
    def prepare_pytest(
        self,
        task_id: str,
        alias: str,
        *,
        timeout_seconds: float = 60,
        request_id: str | None = None,
    ) -> dict[str, object]:
        _validate_pytest_timeout(timeout_seconds)
        self._store.get_task(task_id)
        operation = self._store.create_operation(
            task_id,
            "unit_test",
            f"pytest:{task_id}:{alias}:{request_id or timeout_seconds}",
            {"alias": alias, "timeout_seconds": timeout_seconds},
        )
        run, created = self._store.get_or_create_run(
            task_id, operation.operation_id, "unit_test"
        )
        artifacts = FileArtifactStore(self._store)
        if not created:
            request, _ = self._pytest_request(task_id, run.run_id)
            return {
                "task_id": task_id,
                "operation_id": operation.operation_id,
                "run_id": run.run_id,
                "approval_target_fingerprint": request.fingerprint,
            }
        workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
            task_id, alias, run.run_id
        )
        request = _pytest_tool_request(run, workspace, timeout_seconds)
        artifacts.write_bytes(
            task_id,
            run.run_id,
            producer="quality_artifacts",
            relative_path="artifacts/pytest-request.json",
            content=json.dumps(
                {
                    "schema_version": 1,
                    "alias": alias,
                    "request": request.model_dump(mode="json"),
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            input_fingerprint=request.fingerprint,
            operation_id=operation.operation_id,
        )
        return {
            "task_id": task_id,
            "operation_id": operation.operation_id,
            "run_id": run.run_id,
            "approval_target_fingerprint": request.fingerprint,
        }

    @exclusive_execution
    def run_pytest(
        self,
        task_id: str,
        run_id: str,
        *,
        approval_id: str | None,
        confirmation_token: str | None,
        cancel_event: Event | None = None,
    ) -> dict[str, object]:
        request, alias = self._pytest_request(task_id, run_id)
        run = self._store.get_run(task_id, run_id)
        workspace = WorkspaceService(self._project_root, GitRunner()).prepare(
            task_id, alias, run.run_id
        )
        policy = _pytest_policy(request.timeout_seconds)
        if approval_id is None or confirmation_token is None:
            raise DomainError(
                "APPROVAL_REQUIRED",
                "pytest execution requires approval and confirmation",
            )
        approvals = FileApprovalStore(self._store)
        gate = ApprovalGate(approvals)
        # This is deliberately before changing Run state or constructing a process.
        gate.require_approved(
            request,
            policy.tools[0],
            LocalCommandRunner.trust_level,
            approval_id=approval_id,
            confirm_token=confirmation_token,
            policy_revision=policy.revision,
        )
        journal, artifacts = _journal(self._store, run), FileArtifactStore(self._store)
        self._store.set_run_status(run.run_id, RunStatus.RUNNING)
        journal.append("run.started", {})
        try:
            result = ToolRegistry(
                policy, LocalCommandRunner(artifacts, journal), gate
            ).execute(
                self._project_root,
                request,
                environment={
                    name: os.environ[name]
                    for name in request.env_names
                    if name in os.environ
                },
                approval_id=approval_id,
                confirm_token=confirmation_token,
                cancel_event=cancel_event,
            )
            pytest_report = artifacts.write_bytes(
                task_id,
                run.run_id,
                producer="quality_artifacts",
                relative_path="artifacts/pytest-report.json",
                content=json.dumps(
                    _pytest_report_value(result),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
                input_fingerprint=result.request_fingerprint,
                operation_id=run.operation_id,
            )
            report = UnitTestHarness(
                self._project_root, task_id, run.run_id, workspace, result.tool_run_id
            ).run()
            status = (
                RunStatus.SUCCEEDED
                if report.status.value == "passed"
                else RunStatus.FAILED
            )
            journal.append(
                "harness.completed",
                {
                    "harness": "unit_test_harness",
                    "status": report.status.value,
                    "pytest_report_artifact_id": pytest_report.artifact_id,
                },
            )
            self._store.set_run_status(
                run.run_id,
                status,
                error_code=None
                if status is RunStatus.SUCCEEDED
                else "UNIT_TEST_HARNESS_FAILED",
            )
            journal.append(
                "run.succeeded" if status is RunStatus.SUCCEEDED else "run.failed",
                {}
                if status is RunStatus.SUCCEEDED
                else {"error_code": "UNIT_TEST_HARNESS_FAILED"},
            )
            return {
                "task_id": task_id,
                "operation_id": run.operation_id,
                "run_id": run.run_id,
                "tool_run_id": result.tool_run_id,
                "pytest_report_artifact_id": pytest_report.artifact_id,
                "status": report.status.value,
            }
        except DomainError as error:
            self._store.set_run_status(
                run.run_id, RunStatus.FAILED, error_code=error.code
            )
            journal.append("run.failed", {"error_code": error.code})
            raise

    def _pytest_request(self, task_id: str, run_id: str) -> tuple[ToolRequest, str]:
        artifacts = FileArtifactStore(self._store)
        record, _ = next(
            (
                (item, None)
                for item in artifacts.list(task_id, run_id)
                if item.relative_path == "artifacts/pytest-request.json"
            ),
            (None, None),
        )
        if record is None:
            raise DomainError(
                "PYTEST_REQUEST_NOT_FOUND", "prepared pytest request is missing"
            )
        artifacts.verify(task_id, run_id, record.artifact_id)
        try:
            value = json.loads(
                (
                    self._store.event_journal_path(run_id).parent / record.relative_path
                ).read_text(encoding="utf-8")
            )
            if not isinstance(value, dict) or not isinstance(value.get("alias"), str):
                raise TypeError("prepared pytest request is invalid")
            return ToolRequest.model_validate(value["request"]), value["alias"]
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise DomainError(
                "PYTEST_REQUEST_INVALID", "prepared pytest request is invalid"
            ) from error

    def _prd_content(
        self,
        task_id: str,
        text: str | None,
        source_run_id: str | None,
        artifact_id: str | None,
    ) -> tuple[str, dict[str, str] | None]:
        if text is not None:
            return text, None
        if source_run_id is None or artifact_id is None:
            raise DomainError(
                "PRD_INPUT_INVALID", "PRD Artifact requires source run ID"
            )
        try:
            record = FileArtifactStore(self._store).verify(
                task_id, source_run_id, artifact_id
            )
        except DomainError as error:
            raise DomainError("PRD_ARTIFACT_INVALID", "PRD Artifact is invalid") from error
        if record.relative_path != "artifacts/prd.md":
            raise DomainError(
                "PRD_ARTIFACT_INVALID", "PRD Artifact must be artifacts/prd.md"
            )
        root = self._store.event_journal_path(source_run_id).parent
        return (root / record.relative_path).read_text(encoding="utf-8"), _value(record)


def _pytest_tool_request(
    run: RunRecord, workspace: PreparedWorkspace, timeout: float
) -> ToolRequest:
    base_sha = workspace.base_sha
    env_names = tuple(name for name in ("SYSTEMROOT", "WINDIR") if name in os.environ)
    return ToolRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        workspace_relative_path=f".yipilot/tasks/{run.task_id}/repos/{workspace.repo_alias}/workspaces/{run.run_id}",
        tool_id=QUALITY_PYTEST_TOOL_ID,
        argv=(sys.executable, "-m", "pytest"),
        timeout_seconds=timeout,
        output_budget_bytes=QUALITY_PYTEST_MAX_OUTPUT_BYTES,
        env_names=env_names,
        input_fingerprint=hashlib.sha256(f"pytest:{base_sha}".encode()).hexdigest(),
    )


def _validate_quality_profile_revision(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DomainError("VALIDATION_ERROR", "invalid quality generation input")


def _validate_pytest_timeout(value: float) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not 0 < value <= min(
            MAX_TOOL_TIMEOUT_SECONDS, QUALITY_PYTEST_MAX_TIMEOUT_SECONDS
        )
    ):
        raise DomainError("VALIDATION_ERROR", "invalid pytest preparation input")


def _pytest_report_value(result: ToolResult) -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool_run_id": result.tool_run_id,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "stdout_artifact_ref": result.stdout_artifact_ref.model_dump(mode="json"),
        "stderr_artifact_ref": result.stderr_artifact_ref.model_dump(mode="json"),
    }


def _pytest_policy(timeout: float) -> ExecutionPolicy:
    return ExecutionPolicy(
        revision=QUALITY_PYTEST_POLICY_REVISION,
        tools=(
            ToolPolicy(
                tool_id=QUALITY_PYTEST_TOOL_ID,
                kind=ToolKind.TEST,
                executable=sys.executable,
                argv_schema=(ArgvRule(literal="-m"), ArgvRule(literal="pytest")),
                allowed_env_names=("SYSTEMROOT", "WINDIR"),
                max_output_bytes=QUALITY_PYTEST_MAX_OUTPUT_BYTES,
                max_timeout_seconds=timeout,
            ),
        ),
    )


def _journal(store: FileTaskStore, run: RunRecord) -> JsonlEventJournal:
    return JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    )


def _ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )


def _value(record: ArtifactRecord) -> dict[str, str]:
    return {
        "artifact_id": record.artifact_id,
        "task_id": record.task_id,
        "operation_id": record.operation_id,
        "run_id": record.run_id,
        "relative_path": record.relative_path,
        "sha256": record.sha256,
    }


def _source_fingerprint(prd: str, source_ref: dict[str, str] | None) -> str:
    value = {
        "content_sha256": _fixture_fingerprint(prd),
        "source_ref": source_ref or {},
    }
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
