import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from yipilot.domain.execution import ArtifactRef
from yipilot.domain.model import ModelChunk, ModelRequest, ModelResponse
from yipilot.domain.models import RunStatus
from yipilot.domain.patch_proposal import PatchProposal
from yipilot.infrastructure.model_port import FakeModelPort
from yipilot.runtime.agent_runtime import AgentRuntime
from yipilot.runtime.model_planner import ModelPlanner


@dataclass(frozen=True)
class Run:
    task_id: str = "1001"
    operation_id: str = "operation-1"
    run_id: str = "run-1"
    stage: str = "code_generate"
    status: RunStatus = RunStatus.RUNNING
    error_code: str | None = None


class Runs:
    def __init__(self) -> None:
        self.run = Run()

    def get_task(self, task_id: str) -> object:
        assert task_id == self.run.task_id
        return object()

    def get_operation(self, task_id: str, operation_id: str) -> Run:
        assert (task_id, operation_id) == (self.run.task_id, self.run.operation_id)
        return self.run

    def get_run(self, task_id: str, run_id: str) -> Run:
        assert (task_id, run_id) == (self.run.task_id, self.run.run_id)
        return self.run


class Evidence:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.proposal: PatchProposal | None = None

    def append_event(self, event_type: str, payload: dict[str, object]) -> None:
        del payload
        self.events.append(event_type)

    def write_plan(self, plan: object) -> ArtifactRef:
        return ref(plan)

    def write_model_response(self, request: object, content: str) -> ArtifactRef:
        del content
        return ref(request)

    def write_model_call(self, record: object) -> None:
        del record

    def write_patch_proposal(self, request: object, proposal: PatchProposal) -> ArtifactRef:
        self.proposal = proposal
        return ref(request)

    def verify_input_artifact(self, reference: ArtifactRef) -> ArtifactRef:
        return reference


class Tools:
    def validate_request(self, request: object) -> None:
        raise AssertionError(f"proposal-only runtime must not execute {request}")

    def execute(self, request: object, **kwargs: object) -> object:
        raise AssertionError(f"proposal-only runtime must not execute {request}")


def ref(value: object) -> ArtifactRef:
    return ArtifactRef.model_construct(artifact_id="artifact-1", task_id=value.task_id, operation_id=value.operation_id, run_id=value.run_id, relative_path="artifacts/runtime.md", sha256="a" * 64)


def test_fixed_model_response_becomes_a_proposal_only_runtime_plan() -> None:
    run = Run()
    instruction = "offline model proposal"
    request = ModelRequest(task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id, provider_profile_name="fixture", provider_type="openai_compatible", model="fixture", profile_revision=1, timeout_seconds=1, output_budget_bytes=1024, input_fingerprint=hashlib.sha256(instruction.encode()).hexdigest(), user_instruction=instruction)
    response = ModelResponse(chunks=(ModelChunk(content=json.dumps({"schema_version": 1, "summary": "Update.", "patches": [{"path": "a.py", "unified_diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"}]})),))
    evidence = Evidence()

    result = AgentRuntime(Runs(), ModelPlanner(FakeModelPort(response), request, evidence), Tools(), evidence).execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)

    assert result.terminal_status is RunStatus.SUCCEEDED
    assert result.tool_results == () and evidence.proposal is not None
    assert evidence.events == ["turn.started", "model.delta", "model.message_completed", "proposal.created", "plan.created", "harness.completed", "turn.completed"]


def test_runtime_module_has_no_forbidden_adapter_imports() -> None:
    runtime_root = Path(__file__).parents[3] / "src" / "yipilot" / "runtime"
    forbidden = ("subprocess", "yipilot.infrastructure.git_runner", "yipilot.infrastructure.openai_compatible", "yipilot.infrastructure.secret_store", "yipilot.transport")
    imports = {alias.name for path in runtime_root.glob("*.py") for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")] if isinstance(node, ast.ImportFrom) else [])}
    assert not {name for name in imports if name.startswith(forbidden)}
