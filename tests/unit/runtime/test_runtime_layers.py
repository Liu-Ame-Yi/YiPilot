"""Offline seam and dependency tests for the Agent Runtime Module."""

import ast
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from yipilot.domain.execution import ArtifactRef, ToolRequest, ToolResult, ToolRunStatus
from yipilot.domain.model import ModelChunk, ModelRequest, ModelResponse
from yipilot.domain.models import RunStatus
from yipilot.infrastructure.model_port import FakeModelPort
from yipilot.runtime.agent_runtime import AgentRuntime
from yipilot.runtime.model_planner import ModelPlanner


@dataclass(frozen=True)
class _Run:
    task_id: str = "1001"
    operation_id: str = "operation-1"
    run_id: str = "run-1"
    stage: str = "code_generate"
    status: RunStatus = RunStatus.PENDING
    error_code: str | None = None


class _Runs:
    def __init__(self) -> None:
        self.run = _Run()

    def get_task(self, task_id: str) -> object:
        assert task_id == self.run.task_id
        return object()

    def get_operation(self, task_id: str, operation_id: str) -> _Run:
        assert (task_id, operation_id) == (self.run.task_id, self.run.operation_id)
        return self.run

    def get_run(self, task_id: str, run_id: str) -> _Run:
        assert (task_id, run_id) == (self.run.task_id, self.run.run_id)
        return self.run

    def set_run_status(
        self, run_id: str, status: RunStatus, *, error_code: str | None = None
    ) -> _Run:
        assert run_id == self.run.run_id
        self.run = replace(self.run, status=status, error_code=error_code)
        return self.run


class _Evidence:
    def __init__(self) -> None:
        self.events: list[str] = []

    def append_event(self, event_type: str, payload: dict[str, object]) -> None:
        del payload
        self.events.append(event_type)

    def write_plan(self, plan: object) -> ArtifactRef:
        return _ref(plan)

    def write_model_response(self, request: object, content: str) -> ArtifactRef:
        del content
        return _ref(request)

    def write_model_call(self, record: object) -> None:
        del record

    def verify_input_artifact(self, reference: ArtifactRef) -> ArtifactRef:
        return reference


class _Tools:
    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []

    def validate_request(self, request: ToolRequest) -> None:
        self.requests.append(request)

    def execute(
        self, request: ToolRequest, *, approval_id: str | None = None, confirm_token: str | None = None
    ) -> ToolResult:
        del approval_id, confirm_token
        self.requests.append(request)
        return ToolResult.model_construct(status=ToolRunStatus.SUCCEEDED)


def _ref(value: object) -> ArtifactRef:
    return ArtifactRef.model_construct(
        artifact_id="artifact-1",
        task_id=value.task_id,
        operation_id=value.operation_id,
        run_id=value.run_id,
        relative_path="artifacts/runtime.json",
        sha256="a" * 64,
    )


def test_runtime_replaces_model_and_tool_adapters_offline() -> None:
    run = _Run()
    instruction = "offline model plan"
    fingerprint = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    request = ModelRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        provider_profile_name="offline",
        provider_type="openai_compatible",
        model="fixture",
        profile_revision=1,
        timeout_seconds=1,
        output_budget_bytes=1024,
        input_fingerprint=fingerprint,
        user_instruction=instruction,
    )
    tool = ToolRequest(
        task_id=run.task_id,
        operation_id=run.operation_id,
        run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="fake-diagnostic",
        argv=("yipilot.fake-diagnostic", "workspace"),
        timeout_seconds=1,
        output_budget_bytes=1024,
        input_fingerprint=fingerprint,
    )
    response = ModelResponse(
        chunks=(
            ModelChunk(
                content=json.dumps(
                    {"schema_version": 1, "steps": [{"tool_id": "fake-diagnostic", "args": {"scope": "workspace"}}]}
                )
            ),
        )
    )
    evidence = _Evidence()
    tools = _Tools()
    runs = _Runs()
    runs.set_run_status(run.run_id, RunStatus.RUNNING)
    evidence.append_event("run.started", {})
    result = AgentRuntime(
        runs, ModelPlanner(FakeModelPort(response), request, tool, evidence), tools, evidence
    ).execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)

    assert result.terminal_status is RunStatus.SUCCEEDED
    assert len(tools.requests) == 2
    assert evidence.events == [
        "run.started",
        "model.delta",
        "model.message_completed",
        "plan.created",
        "harness.completed",
    ]


def test_runtime_module_has_no_forbidden_adapter_imports() -> None:
    runtime_root = Path(__file__).parents[3] / "src" / "yipilot" / "runtime"
    forbidden = (
        "subprocess",
        "yipilot.infrastructure.git_runner",
        "yipilot.infrastructure.openai_compatible",
        "yipilot.infrastructure.secret_store",
        "yipilot.transport",
    )
    imports = {
        alias.name
        for path in runtime_root.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(name=node.module or "")]
            if isinstance(node, ast.ImportFrom)
            else []
        )
    }

    assert not {name for name in imports if name.startswith(forbidden)}
