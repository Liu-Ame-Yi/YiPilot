import hashlib
import json
from pathlib import Path

import pytest

from tests.helpers.fake_executor import FakeExecutor
from yipilot.agent.model_planner import ModelPlanner
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ArgvRule,
    ExecutionPolicy,
    ToolKind,
    ToolPolicy,
    ToolRequest,
)
from yipilot.domain.model import ModelChunk, ModelRequest, ModelResponse
from yipilot.models.fixtures import (
    DisabledModelPort,
    FakeModelPort,
    RecordedReplayModelPort,
)
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.runtime_evidence import FileRuntimeEvidence
from yipilot.persistence.task_store import FileTaskStore, RunRecord
from yipilot.security.redaction import SecretRedactor
from yipilot.workflow.run_executor import RunExecutor
from yipilot.workspace.tool_gateway import RegistryToolGateway
from yipilot.workspace.tool_registry import ToolRegistry


def _context(tmp_path: Path) -> tuple[FileTaskStore, RunRecord, JsonlEventJournal, ToolRegistry]:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "agent", "model-port", {})
    run = store.create_run("1001", operation.operation_id, "code_generate")
    (tmp_path / ".yipilot/tasks/1001/repos/sample/workspace").mkdir(parents=True)
    journal = JsonlEventJournal(
        store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id, stage=run.stage
    )
    policy = ExecutionPolicy(
        revision="model-port-v1",
        tools=(ToolPolicy(
            tool_id="fake-diagnostic", kind=ToolKind.READ_ONLY,
            executable="yipilot.fake-diagnostic", argv_schema=(ArgvRule(literal="workspace"),),
            max_output_bytes=1024, max_timeout_seconds=1,
        ),),
    )
    registry = ToolRegistry(
        policy, FakeExecutor(FileArtifactStore(store), journal, stdout=b"diagnostic\n")
    )
    return store, run, journal, registry


def _request(run: RunRecord, instruction: str, budget: int = 1024) -> tuple[ModelRequest, ToolRequest]:
    fingerprint = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    request = ModelRequest(
        task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
        provider_profile_name="offline", provider_type="openai_compatible", model="fixture",
        profile_revision=1, timeout_seconds=1, output_budget_bytes=budget,
        input_fingerprint=fingerprint, user_instruction=instruction,
    )
    tool = ToolRequest(
        task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="fake-diagnostic", argv=("yipilot.fake-diagnostic", "workspace"),
        timeout_seconds=1, output_budget_bytes=1024, input_fingerprint=fingerprint,
    )
    return request, tool


def _response(content: str) -> ModelResponse:
    return ModelResponse(chunks=(ModelChunk(content=content),))


def _valid_response() -> ModelResponse:
    return _response(
        json.dumps(
            {
                "schema_version": 1,
                "summary": "Fixture proposal.",
                "patches": [
                    {
                        "path": "tracked.py",
                        "unified_diff": "--- a/tracked.py\n+++ b/tracked.py\n@@ -1 +1 @@\n-old\n+new\n",
                    }
                ],
            }
        )
    )


def test_fake_replay_and_disabled_ports_have_stable_stream_or_error_contract(tmp_path: Path) -> None:
    _store, run, _journal, _registry = _context(tmp_path)
    request, _tool = _request(run, "fixed")
    response = _valid_response()

    assert FakeModelPort(response).stream(request) == response.chunks
    assert RecordedReplayModelPort("fixture-v1", {request.input_fingerprint: response}).stream(request) == response.chunks
    with pytest.raises(DomainError, match="^MODEL_REPLAY_NOT_FOUND:"):
        RecordedReplayModelPort("fixture-v1", {}).stream(request)
    with pytest.raises(DomainError, match="^MODEL_DISABLED:"):
        DisabledModelPort().stream(request)


def test_replay_model_streams_events_persists_artifacts_and_executes_only_valid_plan(tmp_path: Path) -> None:
    store, run, journal, registry = _context(tmp_path)
    instruction = "recorded diagnostic"
    request, _ = _request(run, instruction)
    planner = ModelPlanner(
        RecordedReplayModelPort("fixture-v1", {request.input_fingerprint: _valid_response()}),
        request, FileRuntimeEvidence(FileArtifactStore(store), journal),
    )

    result = RunExecutor(store, planner, RegistryToolGateway(tmp_path, registry), FileRuntimeEvidence(FileArtifactStore(store), journal)).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True
    )

    assert result.run.status == "succeeded"
    events = journal.read_after(0)
    assert [event.type for event in events] == [
        "run.started", "turn.started", "model.delta", "model.message_completed",
        "proposal.created", "plan.created", "harness.completed", "turn.completed",
        "run.succeeded",
    ]
    paths = {record.relative_path for record in FileArtifactStore(store).list(run.task_id, run.run_id)}
    assert {"artifacts/model-response.json", "artifacts/model-call.json", "artifacts/agent-plan.json"} <= paths


@pytest.mark.parametrize("response,code", [
    (None, "MODEL_REPLAY_NOT_FOUND"),
    (_response('{"schema_version":1,"steps":[{"tool_id":"other","args":{}}]}'), "PATCH_PROPOSAL_INVALID"),
    (_response("x" * 32), "MODEL_OUTPUT_LIMIT_EXCEEDED"),
])
def test_model_failures_write_redacted_event_and_never_start_tools(
    tmp_path: Path, response: ModelResponse | None, code: str
) -> None:
    store, run, journal, registry = _context(tmp_path)
    instruction = "private instruction"
    request, _ = _request(run, instruction, budget=16 if code == "MODEL_OUTPUT_LIMIT_EXCEEDED" else 1024)
    planner = ModelPlanner(
        RecordedReplayModelPort("fixture-v1", {request.input_fingerprint: response} if response else {}),
        request, FileRuntimeEvidence(FileArtifactStore(store), journal),
    )

    result = RunExecutor(store, planner, RegistryToolGateway(tmp_path, registry), FileRuntimeEvidence(FileArtifactStore(store), journal)).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True
    )

    assert result.run.error_code == code
    assert [event.type for event in journal.read_after(0)][-3:] == ["model.failed", "turn.failed", "run.failed"]
    assert "tool.started" not in [event.type for event in journal.read_after(0)]


def test_model_output_is_redacted_before_events_and_artifacts(tmp_path: Path) -> None:
    store, run, journal, registry = _context(tmp_path)
    instruction = "private instruction"
    request, _ = _request(run, instruction)
    secret = "environment-value"
    content = '{"schema_version":1,"steps":[],"note":"Bearer token https://user:password@example.test ' + secret + '"}'
    planner = ModelPlanner(
        FakeModelPort(_response(content)), request, FileRuntimeEvidence(FileArtifactStore(store), journal),
        redactor=SecretRedactor([secret]),
    )

    RunExecutor(store, planner, RegistryToolGateway(tmp_path, registry), FileRuntimeEvidence(FileArtifactStore(store), journal)).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True
    )

    persisted = "".join(
        path.read_text(encoding="utf-8")
        for path in store.event_journal_path(run.run_id).parent.rglob("*") if path.is_file()
    )
    assert secret not in persisted
    assert "password" not in persisted
    assert "token" not in persisted


def test_verified_markdown_artifacts_contribute_to_model_fingerprint(tmp_path: Path) -> None:
    from yipilot.domain.execution import ArtifactRef
    from yipilot.domain.model import model_request_fingerprint

    store, run, journal, registry = _context(tmp_path)
    instruction = "recorded markdown diagnostic"
    artifact_store = FileArtifactStore(store)
    record = artifact_store.write_bytes(
        run.task_id,
        run.run_id,
        producer="test",
        relative_path="artifacts/approved-input.md",
        content=b"# approved\n",
        input_fingerprint="input-v1",
        operation_id=run.operation_id,
    )
    reference = ArtifactRef(
        artifact_id=record.artifact_id,
        task_id=record.task_id,
        operation_id=record.operation_id,
        run_id=record.run_id,
        relative_path=record.relative_path,
        sha256=record.sha256,
    )
    request, _ = _request(run, instruction)
    request = request.model_copy(
        update={
            "allowed_input_artifact_refs": (reference,),
            "input_fingerprint": model_request_fingerprint(instruction, (reference,)),
        }
    )
    planner = ModelPlanner(
        RecordedReplayModelPort("fixture-v1", {request.input_fingerprint: _valid_response()}),
        request,
        FileRuntimeEvidence(artifact_store, journal),
    )

    result = RunExecutor(store, planner, RegistryToolGateway(tmp_path, registry), FileRuntimeEvidence(FileArtifactStore(store), journal)).execute(
        run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True
    )

    assert result.run.status == "succeeded"
    call = next(record for record in artifact_store.list(run.task_id, run.run_id) if record.relative_path == "artifacts/model-call.json")
    call_value = json.loads((store.event_journal_path(run.run_id).parent / call.relative_path).read_text(encoding="utf-8"))
    assert call_value["input_artifact_refs"] == [reference.model_dump(mode="json")]


@pytest.mark.parametrize("port_kind", ["fake", "replay"])
def test_fake_and_replay_ports_share_success_event_contract(tmp_path: Path, port_kind: str) -> None:
    store, run, journal, registry = _context(tmp_path)
    instruction = "fixed"
    request, _ = _request(run, instruction)
    response = _valid_response()
    port = (
        FakeModelPort(response)
        if port_kind == "fake"
        else RecordedReplayModelPort("fixture-v1", {request.input_fingerprint: response})
    )
    result = RunExecutor(
        store, ModelPlanner(port, request, FileRuntimeEvidence(FileArtifactStore(store), journal)), RegistryToolGateway(tmp_path, registry), FileRuntimeEvidence(FileArtifactStore(store), journal)
    ).execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)

    assert result.run.status == "succeeded"
    assert [event.type for event in journal.read_after(0)][:4] == [
        "run.started", "turn.started", "model.delta", "model.message_completed"
    ]


def test_disabled_port_writes_model_failure_and_call_record(tmp_path: Path) -> None:
    store, run, journal, registry = _context(tmp_path)
    instruction = "fixed"
    request, _ = _request(run, instruction)
    artifacts = FileArtifactStore(store)
    result = RunExecutor(
        store, ModelPlanner(DisabledModelPort(), request, FileRuntimeEvidence(artifacts, journal)), RegistryToolGateway(tmp_path, registry), FileRuntimeEvidence(FileArtifactStore(store), journal)
    ).execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)

    assert result.run.error_code == "MODEL_DISABLED"
    assert [event.type for event in journal.read_after(0)] == [
        "run.started", "turn.started", "model.failed", "turn.failed", "run.failed"
    ]
    assert any(record.relative_path == "artifacts/model-call.json" for record in artifacts.list(run.task_id, run.run_id))


def test_default_model_redaction_hides_environment_secret_assignment(tmp_path: Path) -> None:
    store, run, journal, registry = _context(tmp_path)
    instruction = "private instruction"
    request, _ = _request(run, instruction)
    secret = "environment-value"
    content = '{"schema_version":1,"steps":[],"note":"YIPILOT_TEST_SECRET=' + secret + '"}'
    RunExecutor(
        store,
        ModelPlanner(FakeModelPort(_response(content)), request, FileRuntimeEvidence(FileArtifactStore(store), journal)),
        RegistryToolGateway(tmp_path, registry),
        FileRuntimeEvidence(FileArtifactStore(store), journal),
    ).execute(run.task_id, run.operation_id, run.run_id, instruction, harness=lambda: True)

    persisted = store.event_journal_path(run.run_id).parent.joinpath("events.jsonl").read_text(encoding="utf-8")
    assert secret not in persisted
    assert "YIPILOT_TEST_SECRET=[REDACTED]" in persisted
