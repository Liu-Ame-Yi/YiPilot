import hashlib
import json
from pathlib import Path

import pytest

from yipilot.agent.model_planner import ModelPlanner
from yipilot.domain.execution import ExecutionPolicy
from yipilot.domain.model import ModelChunk, ModelRequest, ModelResponse
from yipilot.models.fixtures import FakeModelPort
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.runtime_evidence import FileRuntimeEvidence
from yipilot.persistence.task_store import FileTaskStore
from yipilot.workflow.run_executor import RunExecutor
from yipilot.workspace.tool_gateway import RegistryToolGateway
from yipilot.workspace.tool_registry import ToolRegistry


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
    runtime = RunExecutor(store, ModelPlanner(FakeModelPort(ModelResponse(chunks=(ModelChunk(content=_proposal()),))), request, evidence), RegistryToolGateway(tmp_path, ToolRegistry(ExecutionPolicy(revision="proposal-v1"))), evidence)

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


def test_executor_rejects_wrong_operation_and_persists_interrupt(tmp_path):
    from yipilot.domain.errors import DomainError

    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1")
    operation = store.create_operation("1", "code_generate", "interrupt", {})
    run = store.create_run("1", operation.operation_id, "code_generate")
    journal = JsonlEventJournal(store.event_journal_path(run.run_id),
        task_id="1", run_id=run.run_id, stage=run.stage)
    evidence = FileRuntimeEvidence(FileArtifactStore(store), journal)

    class InterruptedPlanner:
        def plan(self, *, instruction_fingerprint):
            raise KeyboardInterrupt

    executor = RunExecutor(store, InterruptedPlanner(),
        RegistryToolGateway(tmp_path, ToolRegistry(ExecutionPolicy(revision="interrupt"))), evidence)
    with pytest.raises(DomainError, match="RUN_OPERATION_MISMATCH"):
        executor.execute("1", "another-operation", run.run_id, "instruction", harness=lambda: True)
    assert store.get_run("1", run.run_id).status.value == "pending"
    assert not store.event_journal_path(run.run_id).exists()
    with pytest.raises(KeyboardInterrupt):
        executor.execute("1", operation.operation_id, run.run_id, "instruction", harness=lambda: True)
    assert store.get_run("1", run.run_id).status.value == "cancelled"
    assert [event.type for event in journal.read_after(0)] == ["run.started", "turn.started", "run.cancelled"]
