"""Adapt an offline ModelPort response into the existing restricted AgentPlan."""

import hashlib
import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import AgentPlan, ArtifactRef, PlanStep, ToolRequest
from yipilot.domain.model import (
    ModelCallRecord,
    ModelCallStatus,
    ModelPort,
    ModelRequest,
)
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.security.redaction import SecretRedactor

_RESPONSE_PATH = "artifacts/model-response.json"
_CALL_PATH = "artifacts/model-call.json"


class _ModelStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(pattern=r"^fake-diagnostic$")
    args: dict[str, str]

    def request(self, base: ToolRequest) -> ToolRequest:
        if self.args != {"scope": "workspace"}:
            raise ValueError("model step arguments are not allowed")
        return base.model_copy(update={"tool_id": self.tool_id})


class _ModelPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1, le=1)
    steps: tuple[_ModelStep, ...] = Field(min_length=1)


class ModelPlanner:
    """Persist redacted stream evidence before returning a policy-safe structured plan."""

    def __init__(
        self,
        port: ModelPort,
        request: ModelRequest,
        base_request: ToolRequest,
        artifacts: FileArtifactStore,
        journal: JsonlEventJournal,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._port = port
        self._request = request
        self._base_request = base_request
        self._artifacts = artifacts
        self._journal = journal
        self._redactor = redactor or SecretRedactor()

    def plan(self, *, instruction_fingerprint: str) -> AgentPlan:
        if instruction_fingerprint != hashlib.sha256(
            self._request.user_instruction.encode("utf-8")
        ).hexdigest():
            raise DomainError("MODEL_INPUT_MISMATCH", "model input fingerprint does not match Runtime")
        started_at = datetime.now(UTC)
        response_ref: ArtifactRef | None = None
        try:
            self._validate_input_artifacts()
            content = self._stream_content()
            response_ref = _artifact_ref(
                self._artifacts.write_bytes(
                    self._request.task_id,
                    self._request.run_id,
                    producer="model_port",
                    relative_path=_RESPONSE_PATH,
                    content=json.dumps(
                        {"schema_version": 1, "content": content},
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    input_fingerprint=self._request.input_fingerprint,
                    operation_id=self._request.operation_id,
                )
            )
            self._journal.append(
                "model.message_completed",
                {"response_artifact_ref": response_ref.model_dump(mode="json")},
            )
            plan = self._parse(content, instruction_fingerprint)
        except (DomainError, ValidationError, ValueError, json.JSONDecodeError) as error:
            code = error.code if isinstance(error, DomainError) else "MODEL_PLAN_INVALID"
            self._record(started_at, ModelCallStatus.FAILED, response_ref, code)
            self._journal.append("model.failed", {"error_code": code})
            raise DomainError(code, "model response could not produce an allowed plan") from error
        self._record(started_at, ModelCallStatus.SUCCEEDED, response_ref, "model response stored")
        return plan

    def _stream_content(self) -> str:
        parts: list[str] = []
        size = 0
        for chunk in self._port.stream(self._request):
            content = self._redactor.redact_text(chunk.content)
            chunk_size = len(content.encode("utf-8"))
            size += chunk_size
            if chunk_size > 4096 or size > self._request.output_budget_bytes:
                raise DomainError("MODEL_OUTPUT_LIMIT_EXCEEDED", "model output exceeded its budget")
            self._journal.append("model.delta", {"content": content})
            parts.append(content)
        return "".join(parts)

    def _validate_input_artifacts(self) -> None:
        for reference in self._request.allowed_input_artifact_refs:
            record = self._artifacts.verify(reference.task_id, reference.run_id, reference.artifact_id)
            if _artifact_ref(record) != reference or not record.relative_path.casefold().endswith(".md"):
                raise DomainError("MODEL_INPUT_ARTIFACT_INVALID", "model input artifact is not approved Markdown")

    def _parse(self, content: str, instruction_fingerprint: str) -> AgentPlan:
        parsed = _ModelPlan.model_validate_json(content)
        return AgentPlan(
            task_id=self._request.task_id,
            operation_id=self._request.operation_id,
            run_id=self._request.run_id,
            instruction_fingerprint=instruction_fingerprint,
            steps=tuple(
                PlanStep(
                    step_id=index,
                    summary="execute recorded diagnostic",
                    tool_request=step.request(self._base_request),
                )
                for index, step in enumerate(parsed.steps, start=1)
            ),
        )

    def _record(
        self,
        started_at: datetime,
        status: ModelCallStatus,
        response_ref: ArtifactRef | None,
        summary: str,
    ) -> None:
        record = ModelCallRecord(
            task_id=self._request.task_id,
            operation_id=self._request.operation_id,
            run_id=self._request.run_id,
            provider_profile_name=self._request.provider_profile_name,
            provider_type=self._request.provider_type,
            model=self._request.model,
            profile_revision=self._request.profile_revision,
            timeout_seconds=self._request.timeout_seconds,
            output_budget_bytes=self._request.output_budget_bytes,
            input_fingerprint=self._request.input_fingerprint,
            input_artifact_refs=self._request.allowed_input_artifact_refs,
            status=status,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            response_artifact_ref=response_ref,
            redacted_summary=self._redactor.redact_text(summary),
        )
        self._artifacts.write_bytes(
            record.task_id,
            record.run_id,
            producer="model_port",
            relative_path=_CALL_PATH,
            content=record.model_dump_json(exclude_none=True).encode("utf-8"),
            input_fingerprint=record.input_fingerprint,
            operation_id=record.operation_id,
        )


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )
