"""Run-bound inputs and evidence contracts; no execution backend is provided."""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from yipilot.domain.execution import (
    ArtifactRef,
    ExecutionPolicy,
    Sha256,
    TaskId,
    _fingerprint_model,
)

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
CommitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class SandboxMode(StrEnum):
    OFFLINE_WORKSPACE_WRITE = "offline_workspace_write"


class SandboxErrorCode(StrEnum):
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    ASSOCIATION_INVALID = "SANDBOX_ASSOCIATION_INVALID"
    BASE_INVALID = "SANDBOX_BASE_INVALID"
    PATH_DENIED = "SANDBOX_PATH_DENIED"
    SOURCE_DENIED = "SANDBOX_SOURCE_DENIED"
    CONFLICT = "SANDBOX_CONFLICT"
    COPY_FAILED = "SANDBOX_COPY_FAILED"
    MANIFEST_INVALID = "SANDBOX_MANIFEST_INVALID"
    OUTPUT_INVALID = "SANDBOX_OUTPUT_INVALID"
    OUTPUT_DRIFT = "SANDBOX_OUTPUT_DRIFT"
    OUTPUT_UNAPPROVED = "SANDBOX_OUTPUT_UNAPPROVED"
    EVIDENCE_FAILED = "SANDBOX_EVIDENCE_FAILED"


class SandboxBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    wall_clock_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    cpu_count: float = Field(gt=0, le=64, allow_inf_nan=False)
    memory_bytes: int = Field(gt=0, le=64 * 1024**3)
    pid_limit: int = Field(gt=0, le=4096)
    stdout_bytes: int = Field(gt=0, le=16 * 1024**2)
    stderr_bytes: int = Field(gt=0, le=16 * 1024**2)


class SandboxAssociation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    task_id: TaskId
    operation_id: Identifier
    run_id: Identifier

    @field_validator("operation_id", "run_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return source_path(value)


class SandboxRequest(SandboxAssociation):
    """Validate with context={'policy': trusted_execution_policy} before use."""

    policy_revision: Identifier
    mode: SandboxMode
    execution_directory: str
    baseline_manifest_hash: Sha256
    image_digest: ImageDigest
    tool_id: Identifier
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    budgets: SandboxBudgets

    @property
    def fingerprint(self) -> str:
        return _fingerprint_model(self)

    @model_validator(mode="after")
    def validate_request(self, info: ValidationInfo) -> Self:
        path = Path(self.execution_directory)
        if (
            not path.is_absolute()
            or str(path) != self.execution_directory
            or ".." in path.parts
            or any(ord(c) < 32 for c in self.execution_directory)
            or path.parts[-6:]
            != (".yipilot", "tasks", self.task_id, "runs", self.run_id, "execution")
        ):
            raise ValueError("execution directory must be canonical and Run-bound")
        source_path("/".join(path.parts[1:]))
        policy = (info.context or {}).get("policy")
        if (
            not isinstance(policy, ExecutionPolicy)
            or policy.revision != self.policy_revision
        ):
            raise ValueError("trusted registered policy revision is required")
        tool = next((t for t in policy.tools if t.tool_id == self.tool_id), None)
        if (
            tool is None
            or any(
                not t or len(t) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in t)
                for t in self.argv
            )
            or self.argv[0] != tool.executable
            or len(self.argv) != len(tool.argv_schema) + 1
            or any(
                not rule.accepts(token)
                for rule, token in zip(tool.argv_schema, self.argv[1:], strict=True)
            )
        ):
            raise ValueError("argv must match a registered non-shell tool")
        if (
            self.budgets.wall_clock_seconds > tool.max_timeout_seconds
            or max(self.budgets.stdout_bytes, self.budgets.stderr_bytes)
            > tool.max_output_bytes
        ):
            raise ValueError("budgets exceed registered policy")
        return self


class SandboxResult(SandboxAssociation):
    """Required facts from a future Worker; never substitutes for trusted validation."""

    request_fingerprint: Sha256
    container_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image_digest: ImageDigest
    docker_version: Identifier
    backend: Literal["wsl2", "hyper-v", "unknown"]
    enhanced_container_isolation: bool | None
    network: Literal["none"]
    effective_uid: int = Field(gt=0)
    effective_gid: int = Field(ge=0)
    resource_limits: SandboxBudgets
    reason: Literal[
        "exited", "cancelled", "timed_out", "output_limit", "container_error"
    ]
    exit_code: int | None
    started_at: datetime
    ended_at: datetime
    stdout_artifact_ref: ArtifactRef
    stderr_artifact_ref: ArtifactRef
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    output_manifest_hash: Sha256
    fallback_occurred: Literal[False]

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if (
            self.started_at.tzinfo is None
            or self.ended_at.tzinfo is None
            or self.ended_at < self.started_at
        ):
            raise ValueError("timestamps must be timezone-aware and ordered")
        if self.reason == "exited" and self.exit_code is None:
            raise ValueError("exited outcome requires exit code")
        if (
            self.stdout_bytes > self.resource_limits.stdout_bytes
            or self.stderr_bytes > self.resource_limits.stderr_bytes
        ):
            raise ValueError("stream artifacts exceed budgets")
        for ref in (self.stdout_artifact_ref, self.stderr_artifact_ref):
            if (ref.task_id, ref.operation_id, ref.run_id) != (
                self.task_id,
                self.operation_id,
                self.run_id,
            ):
                raise ValueError(
                    "artifact must belong to the same Task, Operation and Run"
                )
            source_path(ref.relative_path)
        return self


def source_path(value: str) -> str:
    """Portable regular-patch paths, including Windows aliases and ADS denial."""
    parts = value.split("/")
    if (
        not value
        or len(value) > 1024
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(c in value for c in ':*?"<>|')
        or any(
            not p
            or p in {".", ".."}
            or p.endswith((".", " "))
            or PureWindowsPath(p).is_reserved()
            for p in parts
        )
    ):
        raise ValueError("unsafe source path")
    return value


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    git_mode: Literal["100644", "100755"]

    _path = field_validator("path")(source_path)


class SourceManifest(SandboxAssociation):
    repository_alias: Identifier
    base_sha: CommitSha
    created_at: datetime
    files: tuple[SourceFile, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include timezone")
        paths = [f.path for f in self.files]
        if paths != sorted(paths) or len({p.casefold() for p in paths}) != len(paths):
            raise ValueError("manifest paths must be sorted and unique")
        return self


class SandboxOutputManifest(SandboxAssociation):
    """Canonical content facts; hash covers canonical_bytes, without a clock value."""

    request_fingerprint: Sha256
    baseline_manifest_hash: Sha256
    files: tuple[SourceFile, ...]

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        paths = [file.path for file in self.files]
        if paths != sorted(paths) or len({p.casefold() for p in paths}) != len(paths):
            raise ValueError("output files must be sorted and unique")
        return self

    @property
    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


class SandboxDiffEvidence(SandboxAssociation):
    """Trusted output evidence, not a Change Handoff or execution attestation."""

    request_fingerprint: Sha256
    result_fingerprint: Sha256
    policy_revision: Identifier
    baseline_manifest_hash: Sha256
    output_manifest_hash: Sha256
    patch_sha256: Sha256
    approval_id: Identifier
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include timezone")
        return value
