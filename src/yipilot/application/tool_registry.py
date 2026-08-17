"""Policy-gated entry point for Yipilot's built-in local tools."""

from collections.abc import Mapping
from pathlib import Path
from threading import Event
from typing import Protocol

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    ToolRequest,
    ToolResult,
    is_generic_python_executable,
)


class ExecutorPort(Protocol):
    """The narrow executor boundary used by the registry."""

    def execute(
        self,
        request: ToolRequest,
        tool: ToolPolicy,
        *,
        cwd: Path,
        environment: Mapping[str, str] | None = None,
        cancel_event: Event | None = None,
    ) -> ToolResult: ...


class ToolRegistry:
    """Validate registered, read-only tools before an executor can run them."""

    def __init__(self, policy: ExecutionPolicy, executor: ExecutorPort | None = None) -> None:
        self._policy = policy
        self._executor = executor

    def validate_request(self, request: ToolRequest) -> ToolPolicy:
        tool = self._policy.validate_request(request)
        if tool.kind is not ToolKind.READ_ONLY:
            raise DomainError(
                "TOOL_KIND_DENIED", "only read-only tools may be executed in Task 10"
            )
        if is_generic_python_executable(tool.executable):
            raise DomainError(
                "TOOL_EXECUTABLE_DENIED",
                "generic Python interpreters are not Task 10 tools",
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
    ) -> ToolResult:
        if self._executor is None:
            raise DomainError("EXECUTOR_UNAVAILABLE", "no executor is configured")
        tool = self.validate_request(request)
        return self._executor.execute(
            request,
            tool,
            cwd=self._policy.resolve_cwd(project_root, request),
            environment=environment,
            cancel_event=cancel_event,
        )
