"""Interfaces at the Runtime seams; implementations live above or below Runtime."""

from collections.abc import Mapping
from typing import Protocol

from yipilot.domain.execution import AgentPlan, ArtifactRef, ToolRequest, ToolResult
from yipilot.domain.model import ModelCallRecord, ModelRequest
from yipilot.domain.models import RunStatus


class RuntimeOperation(Protocol):
    task_id: str


class RuntimeRun(Protocol):
    task_id: str
    operation_id: str
    run_id: str
    stage: str
    status: RunStatus
    error_code: str | None


class RunStorePort(Protocol):
    """Control-plane Run state seam; Runtime neither creates nor selects Runs."""

    def get_task(self, task_id: str) -> object: ...

    def get_operation(self, task_id: str, operation_id: str) -> RuntimeOperation: ...

    def get_run(self, task_id: str, run_id: str) -> RuntimeRun: ...



class RuntimeEvidencePort(Protocol):
    """Append audit events and persist/verify only Runtime-owned evidence."""

    def append_event(self, event_type: str, payload: Mapping[str, object]) -> None: ...

    def write_plan(self, plan: AgentPlan) -> ArtifactRef: ...

    def write_model_response(self, request: ModelRequest, content: str) -> ArtifactRef: ...

    def write_model_call(self, record: ModelCallRecord) -> None: ...

    def verify_input_artifact(self, reference: ArtifactRef) -> ArtifactRef: ...


class ToolExecutionPort(Protocol):
    """ToolRegistry seam; every Runtime request is validated before execution."""

    def validate_request(self, request: ToolRequest) -> None: ...

    def execute(
        self,
        request: ToolRequest,
        *,
        approval_id: str | None = None,
        confirm_token: str | None = None,
    ) -> ToolResult: ...
