"""Consume a ModelPort stream into the Runtime's restricted AgentPlan."""

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
from yipilot.runtime.ports import RuntimeEvidencePort
from yipilot.security.redaction import SecretRedactor


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
    """Persist redacted model evidence before returning a policy-safe plan."""

    def __init__(
        self,
        port: ModelPort,
        request: ModelRequest,
        base_request: ToolRequest,
        evidence: RuntimeEvidencePort,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._port = port
        self._request = request
        self._base_request = base_request
        self._evidence = evidence
        self._redactor = redactor or SecretRedactor()

    def plan(self, *, instruction_fingerprint: str) -> AgentPlan:
        if instruction_fingerprint != hashlib.sha256(
            self._request.user_instruction.encode("utf-8")
        ).hexdigest():
            raise DomainError("MODEL_INPUT_MISMATCH", "model input fingerprint does not match Runtime")
        started_at = datetime.now(UTC)
        response_ref = None
        try:
            self._validate_input_artifacts()
            content = self._stream_content()
            response_ref = self._evidence.write_model_response(self._request, content)
            self._evidence.append_event(
                "model.message_completed",
                {"response_artifact_ref": response_ref.model_dump(mode="json")},
            )
            plan = self._parse(content, instruction_fingerprint)
        except (DomainError, ValidationError, ValueError, json.JSONDecodeError) as error:
            code = error.code if isinstance(error, DomainError) else "MODEL_PLAN_INVALID"
            self._record(started_at, ModelCallStatus.FAILED, response_ref, code)
            self._evidence.append_event("model.failed", {"error_code": code})
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
            self._evidence.append_event("model.delta", {"content": content})
            parts.append(content)
        return "".join(parts)

    def _validate_input_artifacts(self) -> None:
        for reference in self._request.allowed_input_artifact_refs:
            verified = self._evidence.verify_input_artifact(reference)
            if verified != reference or not verified.relative_path.casefold().endswith(".md"):
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
        self._evidence.write_model_call(
            ModelCallRecord(
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
        )
