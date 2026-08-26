"""Policy-gated entry point for Yipilot's built-in local tools."""

from collections.abc import Mapping
from pathlib import Path
from threading import Event
from typing import Protocol

from yipilot.application.approval import ApprovalGate, requires_approval
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ExecutionPolicy,
    ExecutionTrustLevel,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolResult,
    is_generic_python_executable,
)


class ExecutorPort(Protocol):
    """The narrow executor boundary used by the registry."""

    trust_level: ExecutionTrustLevel

    def execute(
        self,
        request: ToolRequest,
        tool: ToolPolicy,
        *,
        cwd: Path,
        project_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
        cancel_event: Event | None = None,
        approval_gate: ApprovalGate | None = None,
        policy_revision: str | None = None,
        approval_id: str | None = None,
        confirm_token: str | None = None,
    ) -> ToolResult: ...


class ToolRegistry:
    """Validate registered, read-only tools before an executor can run them."""

    def __init__(
        self,
        policy: ExecutionPolicy,
        executor: ExecutorPort | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> None:
        self._policy = policy
        self._executor = executor
        self._approval_gate = approval_gate

    def validate_request(self, request: ToolRequest) -> ToolPolicy:
        tool = self._policy.validate_request(request)
        if tool.kind is not ToolKind.READ_ONLY and self._approval_gate is None:
            raise DomainError(
                "APPROVAL_REQUIRED", "high-risk tool execution requires approval"
            )
        if is_generic_python_executable(tool.executable) and tool.kind is not ToolKind.TEST:
            raise DomainError(
                "TOOL_EXECUTABLE_DENIED",
                "generic Python interpreters are only allowed for registered test tools",
            )
        return tool

    def resolve_cwd(self, project_root: Path, request: ToolRequest) -> Path:
        self.validate_request(request)
        return self._policy.resolve_cwd(project_root, request)

    def execute(
        self,
        project_root: Path,
        request: ToolRequest,
        *,
        environment: Mapping[str, str] | None = None,
        cancel_event: Event | None = None,
        approval_id: str | None = None,
        confirm_token: str | None = None,
    ) -> ToolResult:
        if self._executor is None:
            raise DomainError("EXECUTOR_UNAVAILABLE", "no executor is configured")
        tool = self.validate_request(request)
        trust_level = self._executor.trust_level
        if requires_approval(tool, trust_level):
            if self._approval_gate is None:
                raise DomainError("APPROVAL_REQUIRED", "high-risk tool execution requires approval")
            self._approval_gate.require_approved(
                request,
                tool,
                trust_level,
                approval_id=approval_id,
                confirm_token=confirm_token,
                policy_revision=self._policy.revision,
            )
        return self._executor.execute(
            request,
            tool,
            project_root=project_root,
            cwd=self._policy.resolve_cwd(project_root, request),
            environment=environment,
            cancel_event=cancel_event,
            approval_gate=self._approval_gate,
            policy_revision=self._policy.revision,
            approval_id=approval_id,
            confirm_token=confirm_token,
        )
