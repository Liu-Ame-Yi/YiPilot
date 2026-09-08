"""Immutable execution-domain contracts for Yipilot's built-in runtime."""

import hashlib
import json
import re
import sys
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import DomainError

QUALITY_PYTEST_TOOL_ID = "python-pytest"
QUALITY_PYTEST_POLICY_REVISION = "quality-pytest-v1"
QUALITY_PYTEST_MAX_OUTPUT_BYTES = 128 * 1024
QUALITY_PYTEST_MAX_TIMEOUT_SECONDS = 600.0
QUALITY_PYTEST_ALLOWED_ENV_NAMES = ("SYSTEMROOT", "WINDIR")
VERIFICATION_POLICY_REVISION = "change-verification-v1"
VERIFICATION_PYTEST_TOOL_ID = "verification-pytest"

MAX_TOOL_TIMEOUT_SECONDS = 3600.0
MAX_TOOL_OUTPUT_BYTES = 16 * 1024 * 1024

NonEmptyString = Annotated[str, Field(min_length=1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
TaskId = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$")]


class ToolKind(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    TEST = "test"


class ExecutionTrustLevel(StrEnum):
    FAKE = "fake"
    SANDBOXED = "sandboxed"
    HOST_TRUSTED = "host_trusted"


class ToolRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalRecord(BaseModel):
    """A human decision bound to one exact action, target, policy and Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    approval_id: NonEmptyString
    task_id: TaskId
    operation_id: NonEmptyString
    run_id: NonEmptyString
    action_type: NonEmptyString
    target_fingerprint: Sha256
    policy_revision: NonEmptyString
    decision: ApprovalDecision
    decided_at: datetime
    simulation_only: bool = False

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("decided_at must include a timezone")
        return value

    def require_fresh(
        self,
        *,
        task_id: str,
        operation_id: str,
        action_type: str,
        target_fingerprint: str,
        policy_revision: str,
        run_id: str,
    ) -> Self:
        expected = (
            task_id,
            operation_id,
            action_type,
            target_fingerprint,
            policy_revision,
            run_id,
        )
        actual = (
            self.task_id,
            self.operation_id,
            self.action_type,
            self.target_fingerprint,
            self.policy_revision,
            self.run_id,
        )
        if expected != actual:
            raise DomainError(
                "APPROVAL_STALE",
                "approval target, policy, action or Run has changed",
            )
        if self.decision is not ApprovalDecision.APPROVED:
            raise DomainError("APPROVAL_DENIED", "approval decision is not approved")
        return self


class ArtifactRef(BaseModel):
    """A run-scoped reference to persisted artifact content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifact_id: NonEmptyString
    task_id: TaskId
    operation_id: NonEmptyString
    run_id: NonEmptyString
    relative_path: NonEmptyString
    sha256: Sha256

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return _relative_path(value, label="artifact path")


class ToolRequest(BaseModel):
    """One immutable request for a policy-controlled local tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: TaskId
    operation_id: NonEmptyString
    run_id: NonEmptyString
    workspace_relative_path: NonEmptyString
    tool_id: NonEmptyString
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(
        gt=0,
        le=MAX_TOOL_TIMEOUT_SECONDS,
        allow_inf_nan=False,
    )
    output_budget_bytes: int = Field(gt=0, le=MAX_TOOL_OUTPUT_BYTES)
    env_names: tuple[str, ...] = ()
    input_artifact_refs: tuple[ArtifactRef, ...] = ()
    input_fingerprint: Sha256

    @field_validator("workspace_relative_path")
    @classmethod
    def validate_workspace_relative_path(cls, value: str) -> str:
        return _relative_path(value, label="workspace path")

    @field_validator("argv")
    @classmethod
    def validate_argv_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not token or _has_control_character(token) for token in value):
            raise ValueError("argv must contain non-empty argument tokens")
        return value

    @field_validator("env_names")
    @classmethod
    def validate_env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _environment_names(value)

    @model_validator(mode="after")
    def validate_artifact_associations(self) -> Self:
        workspace_parts = PurePosixPath(self.workspace_relative_path).parts
        expected_workspace_prefix = (
            ".yipilot",
            "tasks",
            self.task_id,
            "repos",
        )
        legacy_workspace = (
            len(workspace_parts) == 6
            and workspace_parts[:4] == expected_workspace_prefix
            and bool(workspace_parts[4])
            and workspace_parts[5] == "workspace"
        )
        run_workspace = (
            len(workspace_parts) == 7
            and workspace_parts[:4] == expected_workspace_prefix
            and bool(workspace_parts[4])
            and workspace_parts[5] == "workspaces"
            and workspace_parts[6] == self.run_id
        )
        if not legacy_workspace and not run_workspace:
            raise ValueError("workspace path must identify its Task workspace")
        association = (self.task_id, self.operation_id, self.run_id)
        if any(
            (ref.task_id, ref.operation_id, ref.run_id) != association
            for ref in self.input_artifact_refs
        ):
            raise ValueError(
                "input artifacts must belong to the same Task, Operation and Run"
            )
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint_model(self)


class PlanStep(BaseModel):
    """One ordered, already-structured action in an Agent plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: int = Field(ge=1)
    summary: Annotated[str, Field(min_length=1, max_length=1024)]
    tool_request: ToolRequest


class AgentPlan(BaseModel):
    """An immutable run-scoped plan; it deliberately contains no raw instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: TaskId
    operation_id: NonEmptyString
    run_id: NonEmptyString
    instruction_fingerprint: Sha256
    steps: tuple[PlanStep, ...]

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        association = (self.task_id, self.operation_id, self.run_id)
        if any(
            (
                step.tool_request.task_id,
                step.tool_request.operation_id,
                step.tool_request.run_id,
            )
            != association
            for step in self.steps
        ):
            raise ValueError(
                "plan requests must belong to the same Task, Operation and Run"
            )
        if [step.step_id for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("plan step IDs must be unique and ordered from one")
        return self


class ToolChangeSummary(BaseModel):
    """Bounded metadata about workspace changes; paths remain an Artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    changed_file_count: int = Field(ge=0)
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    changed_paths_artifact_ref: ArtifactRef | None = None
    summary: str = Field(default="", max_length=1024)


class ToolResult(BaseModel):
    """Bounded, immutable evidence for one terminal ToolRun."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tool_run_id: NonEmptyString
    task_id: TaskId
    operation_id: NonEmptyString
    run_id: NonEmptyString
    request_fingerprint: Sha256
    argv: tuple[str, ...] = Field(min_length=1)
    status: ToolRunStatus
    started_at: datetime
    ended_at: datetime
    exit_code: int | None
    stdout_artifact_ref: ArtifactRef
    stderr_artifact_ref: ArtifactRef
    change_summary: ToolChangeSummary
    trust_level: ExecutionTrustLevel

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.started_at.tzinfo is None or self.ended_at.tzinfo is None:
            raise ValueError("tool result timestamps must include a timezone")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        terminal = {
            ToolRunStatus.SUCCEEDED,
            ToolRunStatus.FAILED,
            ToolRunStatus.TIMED_OUT,
            ToolRunStatus.CANCELLED,
        }
        if self.status not in terminal:
            raise ValueError("tool result status must be terminal")
        if self.status is ToolRunStatus.SUCCEEDED and self.exit_code != 0:
            raise ValueError("successful tool result requires exit code zero")
        association = (self.task_id, self.operation_id, self.run_id)
        refs = (self.stdout_artifact_ref, self.stderr_artifact_ref)
        if any(
            (ref.task_id, ref.operation_id, ref.run_id) != association for ref in refs
        ):
            raise ValueError(
                "result artifacts must belong to the same Task, Operation and Run"
            )
        changed_paths_ref = self.change_summary.changed_paths_artifact_ref
        if (
            changed_paths_ref is not None
            and (
                changed_paths_ref.task_id,
                changed_paths_ref.operation_id,
                changed_paths_ref.run_id,
            )
            != association
        ):
            raise ValueError(
                "result artifacts must belong to the same Task, Operation and Run"
            )
        return self


class ArgvRule(BaseModel):
    """One exact or regex-constrained argv position."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    literal: str | None = None
    pattern: str | None = None
    max_length: int = Field(default=4096, gt=0, le=4096)

    @model_validator(mode="after")
    def validate_rule(self) -> Self:
        if (self.literal is None) == (self.pattern is None):
            raise ValueError("argv rule requires exactly one of literal or pattern")
        candidate = self.literal if self.literal is not None else self.pattern
        assert candidate is not None
        if not candidate or _has_control_character(candidate):
            raise ValueError("argv rule may not contain control characters")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as error:
                raise ValueError("argv rule pattern must be valid") from error
        return self

    def accepts(self, token: str) -> bool:
        if len(token) > self.max_length:
            return False
        if self.literal is not None:
            return token == self.literal
        assert self.pattern is not None
        return re.fullmatch(self.pattern, token) is not None


class ToolPolicy(BaseModel):
    """The allowlist entry for one stable tool ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tool_id: NonEmptyString
    kind: ToolKind
    executable: NonEmptyString
    argv_schema: tuple[ArgvRule, ...]
    allowed_env_names: tuple[str, ...] = ()
    max_output_bytes: int = Field(gt=0, le=MAX_TOOL_OUTPUT_BYTES)
    max_timeout_seconds: float = Field(
        gt=0,
        le=MAX_TOOL_TIMEOUT_SECONDS,
        allow_inf_nan=False,
    )

    @field_validator("executable")
    @classmethod
    def validate_executable(cls, value: str) -> str:
        shell_names = {
            "bash",
            "bash.exe",
            "cmd",
            "cmd.exe",
            "fish",
            "fish.exe",
            "powershell",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
            "sh",
            "sh.exe",
            "wsl",
            "wsl.exe",
            "zsh",
            "zsh.exe",
        }
        if (
            _has_control_character(value)
            or PureWindowsPath(value).name.casefold() in shell_names
        ):
            raise ValueError("executable must be one controlled non-shell token")
        return value

    @field_validator("allowed_env_names")
    @classmethod
    def validate_allowed_env_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _environment_names(value)

    def validate_request(self, request: ToolRequest) -> Self:
        if request.argv[0] != self.executable:
            raise DomainError(
                "TOOL_EXECUTABLE_DENIED",
                "argv executable does not match tool policy",
            )
        arguments = request.argv[1:]
        if len(arguments) != len(self.argv_schema) or any(
            not rule.accepts(token)
            for rule, token in zip(self.argv_schema, arguments, strict=True)
        ):
            raise DomainError("TOOL_ARGV_DENIED", "argv does not match tool schema")
        if not set(request.env_names).issubset(self.allowed_env_names):
            raise DomainError(
                "TOOL_ENV_DENIED", "environment name is not allowed by tool policy"
            )
        if request.output_budget_bytes > self.max_output_bytes:
            raise DomainError("TOOL_BUDGET_DENIED", "output budget exceeds tool policy")
        if request.timeout_seconds > self.max_timeout_seconds:
            raise DomainError("TOOL_TIMEOUT_DENIED", "timeout exceeds tool policy")
        return self


def is_quality_pytest_request(
    tool: ToolPolicy, request: ToolRequest, policy_revision: str | None
) -> bool:
    return (
        policy_revision == QUALITY_PYTEST_POLICY_REVISION
        and tool.tool_id == QUALITY_PYTEST_TOOL_ID
        and tool.kind is ToolKind.TEST
        and tool.executable == sys.executable
        and tool.argv_schema == (ArgvRule(literal="-m"), ArgvRule(literal="pytest"))
        and tool.allowed_env_names == QUALITY_PYTEST_ALLOWED_ENV_NAMES
        and tool.max_output_bytes == QUALITY_PYTEST_MAX_OUTPUT_BYTES
        and tool.max_timeout_seconds == request.timeout_seconds
        and request.tool_id == QUALITY_PYTEST_TOOL_ID
        and request.argv == (sys.executable, "-m", "pytest")
        and request.output_budget_bytes == QUALITY_PYTEST_MAX_OUTPUT_BYTES
        and request.timeout_seconds <= QUALITY_PYTEST_MAX_TIMEOUT_SECONDS
    )


def is_registered_python_test_request(
    tool: ToolPolicy, request: ToolRequest, policy_revision: str | None
) -> bool:
    """Permit only the two versioned Python test policies."""
    return is_quality_pytest_request(tool, request, policy_revision) or (
        policy_revision == VERIFICATION_POLICY_REVISION
        and tool.tool_id == VERIFICATION_PYTEST_TOOL_ID
        and tool.kind is ToolKind.TEST
        and tool.executable == sys.executable
        and tool.argv_schema == (ArgvRule(literal="-m"), ArgvRule(literal="pytest"))
        and tool.allowed_env_names == ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")
        and request.tool_id == VERIFICATION_PYTEST_TOOL_ID
        and request.argv == (sys.executable, "-m", "pytest")
        and set(request.env_names).issubset(("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT"))
    )


class ExecutionPolicy(BaseModel):
    """Versioned deny-by-default constraints for local execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    revision: NonEmptyString
    tools: tuple[ToolPolicy, ...] = ()

    @model_validator(mode="after")
    def validate_unique_tool_ids(self) -> Self:
        tool_ids = [tool.tool_id for tool in self.tools]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("tool IDs must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint_model(self)

    def resolve_cwd(self, project_root: Path, request: ToolRequest) -> Path:
        self.validate_request(request)
        root = project_root.resolve(strict=True)
        workspace = root.joinpath(request.workspace_relative_path).resolve(strict=False)
        if workspace != root and root not in workspace.parents:
            raise DomainError("PATH_DENIED", "workspace escapes project root")
        return workspace

    def validate_request(self, request: ToolRequest) -> ToolPolicy:
        for tool in self.tools:
            if tool.tool_id == request.tool_id:
                break
        else:
            raise DomainError(
                "TOOL_DENIED", "tool ID is not allowed by execution policy"
            )
        return tool.validate_request(request)


def _relative_path(value: str, *, label: str) -> str:
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in posix.parts
        or ".." in windows.parts
    ):
        raise ValueError(f"{label} must be workspace-relative")
    return value


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _environment_names(value: tuple[str, ...]) -> tuple[str, ...]:
    if len({name.casefold() for name in value}) != len(value) or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in value
    ):
        raise ValueError("environment names must be unique identifiers")
    return value


def is_generic_python_executable(executable: str) -> bool:
    """Identify interpreter entry points that Task 10 must not execute."""
    name = PureWindowsPath(executable).name.casefold()
    return (
        "python" in name
        or "pypy" in name
        or re.fullmatch(r"py[0-9.]*(?:\.exe)?", name) is not None
    )


def fingerprint_model(model: BaseModel) -> str:
    canonical = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_ALLOWED_TOOL_RUN_TRANSITIONS = {
    ToolRunStatus.PENDING: {ToolRunStatus.RUNNING, ToolRunStatus.CANCELLED},
    ToolRunStatus.RUNNING: {
        ToolRunStatus.SUCCEEDED,
        ToolRunStatus.FAILED,
        ToolRunStatus.TIMED_OUT,
        ToolRunStatus.CANCELLED,
    },
}


def transition_tool_run(current: ToolRunStatus, target: ToolRunStatus) -> ToolRunStatus:
    if current not in _ALLOWED_TOOL_RUN_TRANSITIONS:
        raise DomainError(
            "TOOL_RUN_TERMINAL",
            f"cannot transition terminal tool run {current}",
        )
    if target not in _ALLOWED_TOOL_RUN_TRANSITIONS[current]:
        raise DomainError(
            "TOOL_RUN_TRANSITION_INVALID",
            f"cannot transition tool run {current} to {target}",
        )
    return target
