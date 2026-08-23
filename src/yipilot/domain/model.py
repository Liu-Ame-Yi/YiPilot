"""Versioned, offline-safe contracts for model inference."""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from yipilot.domain.execution import ArtifactRef, NonEmptyString, Sha256, TaskId

MAX_MODEL_CHUNK_BYTES = 4096
MAX_MODEL_OUTPUT_BYTES = 128 * 1024


class ModelCallStatus(StrEnum):
    """Terminal outcomes retained without request or response bodies."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ModelRequest(BaseModel):
    """One bounded model input; only instruction text and Markdown refs are allowed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: TaskId
    operation_id: NonEmptyString
    run_id: NonEmptyString
    provider_profile_name: NonEmptyString
    provider_type: Literal["openai_compatible"]
    model: NonEmptyString
    profile_revision: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0, le=600, allow_inf_nan=False)
    output_budget_bytes: int = Field(gt=0, le=MAX_MODEL_OUTPUT_BYTES)
    allowed_input_artifact_refs: tuple[ArtifactRef, ...] = ()
    input_fingerprint: Sha256
    user_instruction: Annotated[str, Field(min_length=1, max_length=32 * 1024)]

    @model_validator(mode="after")
    def validate_artifacts(self) -> "ModelRequest":
        association = (self.task_id, self.operation_id, self.run_id)
        if any(
            (ref.task_id, ref.operation_id, ref.run_id) != association
            or not ref.relative_path.casefold().endswith(".md")
            for ref in self.allowed_input_artifact_refs
        ):
            raise ValueError("model inputs must be same-run Markdown artifacts")
        return self


class ModelChunk(BaseModel):
    """A bounded text delta emitted by a synchronous ModelPort."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value or len(value.encode("utf-8")) > MAX_MODEL_CHUNK_BYTES:
            raise ValueError("model chunk must be non-empty and within its byte budget")
        return value


class ModelResponse(BaseModel):
    """A recorded, deterministic response represented by its stream chunks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    chunks: tuple[ModelChunk, ...]

    @property
    def content(self) -> str:
        return "".join(chunk.content for chunk in self.chunks)


class ModelCallRecord(BaseModel):
    """Safe persisted call metadata; prompt and response bodies are artifacts, never fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: TaskId
    operation_id: NonEmptyString
    run_id: NonEmptyString
    provider_profile_name: NonEmptyString
    provider_type: Literal["openai_compatible"]
    model: NonEmptyString
    profile_revision: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    output_budget_bytes: int = Field(gt=0)
    input_fingerprint: Sha256
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    status: ModelCallStatus
    started_at: datetime
    ended_at: datetime
    response_artifact_ref: ArtifactRef | None = None
    redacted_summary: Annotated[str, Field(max_length=1024)]

    @model_validator(mode="after")
    def validate_associations(self) -> "ModelCallRecord":
        association = (self.task_id, self.operation_id, self.run_id)
        refs = (*self.input_artifact_refs, self.response_artifact_ref)
        if any(
            ref is not None and (ref.task_id, ref.operation_id, ref.run_id) != association
            for ref in refs
        ):
            raise ValueError("model artifacts must belong to the same Task, Operation and Run")
        if self.ended_at < self.started_at:
            raise ValueError("model call end time must not precede start time")
        return self


class ModelPort(Protocol):
    """Minimal synchronous stream boundary; Task 16 may add a real adapter."""

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]: ...


def model_request_fingerprint(
    instruction: str, artifact_refs: tuple[ArtifactRef, ...] = ()
) -> str:
    """Fingerprint only the explicitly permitted model inputs."""
    value = json.dumps(
        {"artifact_refs": [ref.model_dump(mode="json") for ref in artifact_refs], "instruction": instruction},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
