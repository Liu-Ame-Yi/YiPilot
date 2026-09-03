import hashlib
import json
from pathlib import Path

from yipilot.application.agent_runtime import AgentRuntime
from yipilot.application.model_planner import ModelPlanner
from yipilot.application.runtime_adapters import RegistryToolGateway
from yipilot.application.tool_registry import ToolRegistry
from yipilot.domain.execution import ExecutionPolicy
from yipilot.domain.model import ModelChunk, ModelRequest, ModelResponse
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.model_port import FakeModelPort
from yipilot.infrastructure.runtime_evidence import FileRuntimeEvidence
from yipilot.infrastructure.task_store import FileTaskStore


def _proposal() -> str:
    return json.dumps({"schema_version": 1, "summary": "Update.", "patches": [{"path": "a.py", "unified_diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"}]})


def test_runtime_persists_single_turn_model_plan_and_proposal(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1")
    operation = store.create_operation("1", "code_generate", "test", {})
    run = store.create_run("1", operation.operation_id, "code_generate")
    instruction = "change one file"
    request = ModelRequest(task_id="1", operation_id=operation.operation_id, run_id=run.run_id, provider_profile_name="fixture", provider_type="openai_compatible", model="fixture", profile_revision=1, timeout_seconds=1, output_budget_bytes=1024, input_fingerprint=hashlib.sha256(instruction.encode()).hexdigest(), user_instruction=instruction)
    artifacts = FileArtifactStore(store)
    evidence = FileRuntimeEvidence(artifacts, JsonlEventJournal(store.event_journal_path(run.run_id), task_id="1", run_id=run.run_id, stage="code_generate"))
    runtime = AgentRuntime(store, ModelPlanner(FakeModelPort(ModelResponse(chunks=(ModelChunk(content=_proposal()),))), request, evidence), RegistryToolGateway(tmp_path, ToolRegistry(ExecutionPolicy(revision="proposal-v1"))), evidence)

    result = runtime.execute("1", operation.operation_id, run.run_id, instruction, harness=lambda: True)

    assert result.run.status.value == "succeeded"
    assert {item.relative_path for item in artifacts.list("1", run.run_id)} >= {"artifacts/model-response.json", "artifacts/model-call.json", "artifacts/agent-plan.json", "artifacts/patch-proposal.json"}
    assert [item.type for item in JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)] == ["run.started", "turn.started", "model.delta", "model.message_completed", "proposal.created", "plan.created", "harness.completed", "turn.completed", "run.succeeded"]


def test_tool_registry_has_no_proposal_write_or_shell_tools() -> None:
    registry = ToolRegistry(ExecutionPolicy(revision="proposal-v1"))
    from yipilot.domain.errors import DomainError
    from yipilot.domain.execution import ToolRequest

    request = ToolRequest(task_id="1", operation_id="operation", run_id="run", workspace_relative_path=".yipilot/tasks/1/repos/sample/workspaces/run", tool_id="shell", argv=("cmd.exe", "/c", "write"), timeout_seconds=1, output_budget_bytes=1, input_fingerprint="a" * 64)
    try:
        registry.validate_request(request)
    except DomainError as error:
        assert error.code == "TOOL_DENIED"
    else:
        raise AssertionError("unregistered command was accepted")
