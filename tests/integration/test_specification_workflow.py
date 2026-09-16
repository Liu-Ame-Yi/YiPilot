"""Offline model doubles test the real application/stores/gates, not live semantics."""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from yipilot.app import ProjectApplication
from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelChunk
from yipilot.domain.provider import ModelEntry, ProviderConnection
from yipilot.models.secrets import FakeSecretBackend, SecretStore
from yipilot.models.selection import ModelSelectionService
from yipilot.transport.cli.app import app


class ScriptedModel:
    """Clearly an offline test double; no fixture result proves model understanding."""

    def __init__(self):
        self.requests = []
        self.behavior = None
        self.closed = 0

    def stream(self, request):
        prompt = json.loads(request.user_instruction)
        self.requests.append(prompt)
        value = {"schema_version": 1, **self.response(prompt)}
        if self.behavior:
            value = self.behavior(prompt, value)
        text = value if isinstance(value, str) else json.dumps(value)
        return tuple(ModelChunk(content=text[i:i + 1000]) for i in range(0, len(text), 1000))

    def close(self):
        self.closed += 1

    @staticmethod
    def response(prompt):
        stage = prompt["stage"]
        if stage.endswith("review"):
            return {"document_sha256": prompt["document_sha256"], "verdict": "accept",
                    "summary": "Offline fixture acceptance", "evidence": prompt["required_review_evidence"], "issues": []}
        code = prompt["material"]["code"]
        facts = [{"statement": "The supplied module has value = 1", "evidence": [code[0]["id"]]}] if code else []
        if stage == "srs_generate":
            return {"scope": "Update value", "non_goals": ["No publishing"], "constraints": ["Python"],
                    "criteria": [{"id": "AC-1", "behavior": "Expose value two", "acceptance": "value == 2",
                                  "sources": [prompt["material"]["sources"][0]["id"]]}],
                    "code_facts": facts, "assumptions": [], "open_questions": []}
        return {"srs_sha256": prompt["srs_sha256"], "modules": ["value.py"],
                "interfaces": ["value: int"], "data": ["No persistence change"], "steps": ["Change value to 2"],
                "tests": [{"criterion_id": "AC-1", "test": "assert value == 2"}],
                "code_facts": facts, "assumptions": [], "open_questions": []}


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    model = ScriptedModel()
    reference = SecretStore.keyring_reference("spec-fixture")
    models = ModelSelectionService(tmp_path / "settings",
        secrets=SecretStore(FakeSecretBackend({("yipilot", "spec-fixture"): "offline-test-key"})),
        port_factory=lambda *_: model)
    models.add(ProviderConnection(connection_id="test", base_url="https://example.invalid", timeout_seconds=10, revision=1,
        credential_ref=reference), ModelEntry(connection_id="test", model_id="spec",
        display_name="Offline fixture", output_limit=16000))
    models.set_default("test/spec")
    application = ProjectApplication(root, models=models)
    application.tasks.initialize_project()
    application.tasks.initialize_task("1")
    return application, model, root, models


def start(application, **kwargs):
    return application.start_specification("1", instruction="Update value to 2", **kwargs)


def read(application, record):
    return application.specifications._read(record)


def reject(prompt, value, kind="revise"):
    if prompt["stage"].endswith("review"):
        value.update(verdict={"revise": "revise", "clarify": "clarify", "fatal": "stop"}[kind],
            issues=[{"id": "Q1", "kind": kind, "description": "Specify the value",
                     "evidence": ["input"]}])
    return value


def test_normal_sequence_traceability_and_repeat_after_restart(setup):
    application, model, root, models = setup
    first = start(application, request_id="once")
    assert first.status == "completed"
    assert [p["stage"] for p in model.requests] == ["srs_generate", "srs_review", "sdd_generate", "sdd_review"]
    assert model.closed == 4
    assert set(first.accepted) == {"srs", "sdd"}
    assert read(application, first.accepted["sdd"])["srs_sha256"] == first.accepted["srs"].sha256
    assert read(application, first.reviews["srs"])["document_sha256"] == first.accepted["srs"].sha256
    restarted = ProjectApplication(root, models=models)
    assert start(restarted, request_id="once") == first
    assert restarted.resume_specification("1") == first
    assert len(model.requests) == 4
    with pytest.raises(DomainError, match="OPERATION_CONFLICT"):
        restarted.start_specification("1", instruction="different", request_id="once")


def test_reject_then_revise_and_accept(setup):
    application, model, _, _ = setup
    model.behavior = lambda p, v: reject(p, v) if len(model.requests) == 2 else v
    state = start(application)
    assert state.status == "completed" and state.revisions == {"srs": 1, "sdd": 0}
    assert len(model.requests) == 6
    assert model.requests[2]["revision_review"]["verdict"] == "revise"
    assert model.requests[4]["srs_sha256"] == state.accepted["srs"].sha256


@pytest.mark.parametrize("kind", ["srs", "sdd"])
def test_revision_limit_stops(setup, kind):
    application, model, _, _ = setup
    model.behavior = lambda p, v: reject(p, v) if p["stage"] == f"{kind}_review" else v
    state = start(application, max_revisions=1)
    assert state.status == "stopped" and state.reason == "REVISION_LIMIT_REACHED"
    assert state.revisions[kind] == 1 and kind not in state.accepted
    count = len(model.requests)
    assert application.resume_specification("1") == state
    assert len(model.requests) == count


@pytest.mark.parametrize("stage", ["srs_review", "sdd_review"])
def test_clarify_resets_downstream_and_answer_replays(setup, stage):
    application, model, root, models = setup
    model.behavior = lambda p, v: reject(p, v, "clarify") if p["stage"] == stage else v
    waiting = start(application)
    assert waiting.status == "waiting" and waiting.questions == {"Q1": "Specify the value"}
    assert application.resume_specification("1") == waiting
    with pytest.raises(DomainError, match="SPECIFICATION_ANSWER_STALE"):
        application.clarify_specification("1", waiting.flow_id, {"Q1": "2"}, request_id="answer",
                                          expected_input_sha256="wrong")
    model.behavior = None
    application = ProjectApplication(root, models=models)
    result = application.clarify_specification("1", waiting.flow_id, {"Q1": "2"}, request_id="answer",
                                               expected_input_sha256=waiting.input.sha256)
    assert result.status == "completed" and result.input.sha256 != waiting.input.sha256
    assert result.accepted["srs"] != waiting.accepted.get("srs")
    calls = len(model.requests)
    assert application.clarify_specification("1", waiting.flow_id, {"Q1": "2"}, request_id="answer",
        expected_input_sha256=waiting.input.sha256) == result
    assert len(model.requests) == calls


def test_requirement_change_invalidates_old_flow(setup):
    application, model, _, _ = setup
    old = start(application)
    model.behavior = lambda p, v: reject(p, v, "clarify")
    current = application.start_specification("1", instruction="Update value to 3")
    assert current.status == "waiting" and not current.accepted
    assert application.specification_status("1").flow_id == current.flow_id
    old_state = application.specification_status("1", old.flow_id)
    assert old_state.status == "superseded" and not old_state.accepted
    assert start(application).status == "superseded"
    assert application.specification_status("1").flow_id == current.flow_id


@pytest.mark.parametrize("completed_steps", [1, 2, 3, 4])
def test_restart_at_committed_stage_boundary(setup, monkeypatch, completed_steps):
    application, model, root, models = setup
    step = application.specifications._step

    def interrupt(state):
        step(state)
        if len(model.requests) == completed_steps:
            raise KeyboardInterrupt

    monkeypatch.setattr(application.specifications, "_step", interrupt)
    with pytest.raises(KeyboardInterrupt):
        start(application)
    state = ProjectApplication(root, models=models).resume_specification("1")
    assert state.status == "completed" and len(model.requests) == 4


def test_interrupted_call_is_uncertain_and_not_repeated(setup):
    application, model, root, models = setup

    def interrupt(*_):
        raise KeyboardInterrupt

    model.behavior = interrupt
    with pytest.raises(KeyboardInterrupt):
        start(application)
    state = ProjectApplication(root, models=models).resume_specification("1")
    assert state.status == "stopped" and state.reason == "MODEL_RESULT_UNCERTAIN"
    assert len(model.requests) == 1 and not state.accepted


def test_new_input_preserves_uncertain_run_of_superseded_flow(setup):
    application, model, _, _ = setup

    def interrupt(*_):
        raise KeyboardInterrupt

    model.behavior = interrupt
    with pytest.raises(KeyboardInterrupt):
        start(application)
    previous = application.specification_status("1")
    model.behavior = None
    current = application.start_specification("1", instruction="New input")
    assert current.status == "completed"
    old_run = application.sandbox.store.get_run("1", previous.active_run)
    assert old_run.status.value == "failed" and old_run.error_code == "MODEL_RESULT_UNCERTAIN"
    assert application.specification_status("1", previous.flow_id).status == "superseded"


@pytest.mark.parametrize("bad", ["not json", '{"scope":"incomplete"}'])
def test_invalid_generation_cannot_pass(setup, bad):
    application, model, _, _ = setup
    model.behavior = lambda *_: bad
    state = start(application)
    assert state.status == "stopped" and state.reason == "SPECIFICATION_FORMAT_REPAIR_LIMIT"
    assert not state.accepted and len(model.requests) == 2


@pytest.mark.parametrize("fault", ["hash", "evidence", "self_accept", "unknown_fact", "mapping"])
def test_gate_rejects_wrong_references_or_inconsistent_verdict(setup, fault):
    application, model, _, _ = setup

    def corrupt(prompt, value):
        if fault == "unknown_fact" and prompt["stage"] == "srs_generate":
            value["code_facts"] = [{"statement": "invented", "evidence": ["code:missing.py"]}]
        if fault == "mapping" and prompt["stage"] == "sdd_generate":
            value["tests"][0]["criterion_id"] = "unknown"
        if prompt["stage"] == "srs_review":
            if fault == "hash":
                value["document_sha256"] = "another version"
            elif fault == "evidence":
                value["evidence"] = ["document"]
            elif fault == "self_accept":
                value["issues"] = [{"id": "bad", "kind": "revise", "description": "blocking", "evidence": ["input"]}]
        return value

    model.behavior = corrupt
    state = start(application)
    assert state.status == "stopped" and state.reason == "SPECIFICATION_OUTPUT_INVALID"
    assert "sdd" not in state.accepted


def test_call_failure_receipt_and_fatal_review(setup):
    application, model, _, _ = setup

    def fail(*_):
        raise DomainError("MODEL_READ_TIMEOUT", "fixture timeout")

    model.behavior = fail
    state = start(application)
    assert state.status == "stopped" and state.reason == "MODEL_READ_TIMEOUT"
    assert read(application, state.history[-1])["external_result"] == "uncertain"
    model.behavior = lambda p, v: reject(p, v, "fatal")
    state = start(application, request_id="fatal")
    assert state.status == "stopped" and state.reason == "REVIEW_FATAL"


def test_local_prd_and_repository_evidence(setup, tmp_path):
    application, model, _, _ = setup
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.py").write_text("value = 1\n")
    for args in (["init"], ["add", "value.py"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial"]):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    application.repos.add_and_sync("1", str(source), "repo")
    prd = tmp_path / "prd.md"
    prd.write_text("Update value to two", encoding="utf-8")
    state = application.start_specification("1", prd_path=prd, alias="repo", context_files=["value.py"])
    assert state.status == "completed"
    material = read(application, state.input)
    assert material["repository"]["head_sha"]
    assert material["code"][0]["content"] == "value = 1\n"
    assert material["sources"][0]["source"] == str(prd)
    assert "code:value.py" in model.requests[-1]["required_review_evidence"]
    waiting = application.start_specification("1", alias="repo")
    assert waiting.status == "waiting" and "goal" in waiting.questions


def test_tampered_artifact_blocks_reuse(setup):
    application, _, _, _ = setup
    state = start(application)
    record = state.accepted["srs"]
    path = application.sandbox.store.event_journal_path(record.run_id).parent / record.relative_path
    path.write_text("{}")
    with pytest.raises(DomainError, match="ARTIFACT_STALE"):
        application.resume_specification("1")


def test_cli_uses_public_entry(setup, monkeypatch):
    application, _, root, _ = setup
    monkeypatch.setattr("yipilot.transport.cli.specification.ProjectApplication", lambda *a, **k: application)
    result = CliRunner().invoke(app, ["specification", "start", "--task", "1", "--project", str(root),
                                      "--instruction", "Update value to 2"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "completed"
    result = CliRunner().invoke(app, ["specification", "status", "--task", "1", "--project", str(root)])
    assert result.exit_code == 0 and json.loads(result.output)["accepted"]


def test_failed_commit_does_not_leak_acceptance(setup, monkeypatch):
    application, _, _, _ = setup
    save = application.specifications._save
    failed = False

    def fail_commit(state):
        nonlocal failed
        if state.accepted and not failed:
            failed = True
            raise OSError("injected commit failure")
        save(state)

    monkeypatch.setattr(application.specifications, "_save", fail_commit)
    state = start(application)
    assert state.status == "stopped" and not state.accepted
    persisted = application.specification_status("1")
    assert persisted == state and not persisted.accepted
    assert "srs" not in state.reviews
    assert read(application, state.history[-1])["external_result"] == "received_valid"


def test_unresolved_question_cannot_be_accepted(setup):
    application, model, _, _ = setup

    def question(prompt, value):
        if prompt["stage"] == "srs_generate":
            value["open_questions"] = [{"id": "Q", "question": "Which value?"}]
        return value

    model.behavior = question
    state = start(application)
    assert state.status == "stopped" and not state.accepted
    assert state.reason == "SPECIFICATION_OUTPUT_INVALID"


def test_model_change_between_stages_stops(setup, monkeypatch):
    application, model, root, models = setup
    step = application.specifications._step

    def pause(state):
        step(state)
        raise KeyboardInterrupt

    monkeypatch.setattr(application.specifications, "_step", pause)
    with pytest.raises(KeyboardInterrupt):
        start(application)
    resolve = models.resolve
    monkeypatch.setattr(models, "resolve", lambda **kw: resolve(**kw).model_copy(update={"output_limit": 10000}))
    state = ProjectApplication(root, models=models).resume_specification("1")
    assert state.status == "stopped" and state.reason == "SPECIFICATION_MODEL_CHANGED"
    assert len(model.requests) == 1


@pytest.mark.parametrize("path", ["../outside.md", ".git/config", "missing.py"])
def test_context_paths_do_not_escape_baseline(setup, tmp_path, path):
    application, model, _, _ = setup
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.py").write_text("value = 1\n")
    for args in (["init"], ["add", "value.py"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial"]):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    application.repos.add_and_sync("1", str(source), "repo")
    with pytest.raises(DomainError, match="MODEL_CONTEXT_PATH_DENIED"):
        start(application, alias="repo", context_files=[path])
    assert not model.requests


@pytest.mark.parametrize("stage", ["srs_generate", "srs_review", "sdd_generate", "sdd_review"])
def test_format_repair_is_bounded_and_restartable(setup, monkeypatch, stage):
    application, model, root, models = setup
    model.behavior = lambda p, v: "not json" if p["stage"] == stage and p["format_repair"] is None else v
    step = application.specifications._step

    def interrupt(state):
        step(state)
        if state.format_failure:
            raise KeyboardInterrupt

    monkeypatch.setattr(application.specifications, "_step", interrupt)
    with pytest.raises(KeyboardInterrupt):
        start(application)
    committed = application.specification_status("1")
    assert committed.status == "ready" and committed.format_repairs == {stage: 1}
    failure = read(application, committed.format_failure)
    assert failure["external_result"] == "received_invalid" and failure["diagnostics"]
    assert read(application, failure_record := next(r for r in committed.history if r.artifact_id == failure["response"]["artifact_id"]))["text"] == "not json"
    assert failure_record.sha256 == failure["response"]["sha256"]
    state = ProjectApplication(root, models=models).resume_specification("1")
    assert state.status == "completed" and len(model.requests) == 5
    assert state.format_failure is None
    assert state.format_repairs == {stage: 1}
    assert state.schema_version == 1
    assert all(read(application, r)["schema_version"] == 1 for r in state.accepted.values())


def test_unknown_contract_version_stops_without_repair(setup):
    application, model, _, _ = setup
    model.behavior = lambda p, v: {**v, "schema_version": 99}
    state = start(application)
    assert state.status == "stopped" and not state.format_repairs
    assert len(model.requests) == 1 and not state.accepted


def test_format_repair_failure_does_not_become_acceptance(setup):
    application, model, root, models = setup

    def fail(prompt, value):
        if prompt["format_repair"]:
            raise DomainError("MODEL_READ_TIMEOUT", "uncertain")
        return "invalid"

    model.behavior = fail
    state = start(application)
    assert state.status == "stopped" and state.reason == "MODEL_READ_TIMEOUT"
    assert read(application, state.history[-1])["external_result"] == "uncertain"
    assert ProjectApplication(root, models=models).resume_specification("1") == state
    assert len(model.requests) == 2 and not state.accepted


@pytest.mark.parametrize("reason", ["length", None])
def test_real_adapter_does_not_release_truncated_specification(setup, reason):
    import httpx

    from yipilot.models.openai_compatible import OpenAICompatibleModelPort

    application, _, root, models = setup
    calls = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield calls[-1]

    def handle(request):
        prompt = json.loads(json.loads(request.content)["messages"][0]["content"])
        text = json.dumps({"schema_version": 1, **ScriptedModel.response(prompt)})
        calls.append(("data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": reason}]})
                      + "\n\ndata: [DONE]\n\n").encode())
        return httpx.Response(200, stream=Stream())

    models._port_factory = lambda model, secrets, ref: OpenAICompatibleModelPort(
        model, secrets, credential_ref=ref, transport=httpx.MockTransport(handle))
    state = start(application)
    assert state.status == "stopped" and not state.accepted and not state.format_repairs
    assert state.reason == ("MODEL_OUTPUT_TRUNCATED" if reason else "MODEL_PROTOCOL_ERROR")
    receipt = read(application, state.history[-1])
    assert receipt["completion"]["finish_reason"] == reason
    assert not receipt["completion"]["complete"]
    assert receipt["completion"]["received"] and receipt["external_result"] == "received_invalid"
    assert ProjectApplication(root, models=models).resume_specification("1") == state
    assert len(calls) == 1


def test_legacy_documents_keep_hashes_when_read_by_new_stages(setup, monkeypatch):
    application, model, root, models = setup
    write = application.specifications._write

    def legacy_write(run, name, value):
        if name == "result.json":
            value = {k: v for k, v in value.items() if k != "schema_version"}
        return write(run, name, value)

    monkeypatch.setattr(application.specifications, "_write", legacy_write)
    state = start(application)
    assert state.status == "completed"
    assert "schema_version" not in read(application, state.accepted["srs"])
    assert read(application, state.accepted["sdd"])["srs_sha256"] == state.accepted["srs"].sha256
    assert ProjectApplication(root, models=models).resume_specification("1") == state
    assert len(model.requests) == 4


@pytest.mark.parametrize("version", [None, True, "1", 2])
def test_specification_contract_requires_integer_supported_version(version):
    from pydantic import ValidationError

    from yipilot.domain.specification import SRS

    value = ScriptedModel.response({"stage": "srs_generate", "material": {
        "code": [], "sources": [{"id": "requirement"}]}})
    if version is not None:
        value["schema_version"] = version
    with pytest.raises(ValidationError):
        SRS.model_validate_json(json.dumps(value))
