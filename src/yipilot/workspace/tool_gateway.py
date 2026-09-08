"""Application adapters that bind Runtime tool requests to the existing registry."""

from pathlib import Path

from yipilot.domain.execution import ToolRequest, ToolResult
from yipilot.workspace.tool_registry import ToolRegistry


class RegistryToolGateway:
    """Bind one project root while preserving ToolRegistry validation and approval gates."""

    def __init__(self, project_root: Path, registry: ToolRegistry) -> None:
        self._project_root = Path(project_root)
        self._registry = registry

    def validate_request(self, request: ToolRequest) -> None:
        self._registry.validate_request(request)

    def execute(
        self,
        request: ToolRequest,
        *,
        approval_id: str | None = None,
        confirm_token: str | None = None,
    ) -> ToolResult:
        return self._registry.execute(
            self._project_root, request, approval_id=approval_id, confirm_token=confirm_token
        )
