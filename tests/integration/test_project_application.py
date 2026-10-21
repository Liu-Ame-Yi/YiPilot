"""Offline composition checks: only model and host runtime execution are replaced.

The receipt fixture is not a claim that OpenSandbox or the target checks actually ran.
Source export, approvals, Git worktrees, output validation, stores and repair
services execute their production code.
"""

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from uuid import UUID

import httpx
import pytest
from typer.testing import CliRunner

from yipilot.app import ProjectApplication
from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelChunk
from yipilot.domain.provider import ModelEntry, ProviderConnection
from yipilot.models.selection import ModelSelectionService
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore
from yipilot.runner.http import make_server
from yipilot.runner.service import Runner
from yipilot.transport.cli.app import app
from yipilot.transport.cli.interactive import InteractiveSession
from yipilot.workspace.runner_client import RunnerClient


class Model:
    def __init__(self):
        self.requests = []
        self.closed = 0

    def stream(self, request):
        self.requests.append(request)
        value = 3 if "Bounded OpenSandbox repair" in request.user_instruction else 2
        return (ModelChunk(content=json.dumps({"summary": "Update value", "patches": [{
            "path": "value.py", "unified_diff":
            f"--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n-value = 1\n+value = {value}\n",
        }]})),)

    def close(self):
        self.closed += 1


class FixtureRuntime:
    """Only host runtime effects are faked; HTTP, bundles and evidence are real."""
    def __init__(self, project):
        self.project = project
        self.calls = []
        self.fail_tool = None
        self.entered = threading.Event()
        self.barrier = None

    def doctor(self):
        pass

    def prepare_work(self, job_id, work, store):
        work.mkdir()

    def release_work(self, job_id, work):
        pass

    def cleanup(self, job_id, sandbox_id):
        pass

    def execute(self, job_id, request, work, store):
        self.calls.append(request)
        stage = FileTaskStore(self.project).find_run(request.run_id).stage
        tool = "sandbox-apply-patch" if stage == "sandbox_apply" else stage
        failed = tool == self.fail_tool
        store.update(job_id, sandbox_id=str(UUID(job_id)))
        self.entered.set()
        if self.barrier is not None and tool == "sandbox-apply-patch":
            self.barrier.wait(timeout=10)
        (work / request.script).unlink()
        if tool == "sandbox-apply-patch":
            payload = work / "yipilot-approved-patch.diff"
            added = next(line[1:] for line in payload.read_text().splitlines() if line.startswith("+value = "))
            (work / "value.py").write_text(added + "\n", encoding="utf-8")
            payload.unlink()
        output = b"failed check\n" if failed else b"fixture check\n"
        return "exited", 1 if failed else 0, output, output


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for args in [("init", "--initial-branch=main"), ("config", "user.name", "Tests"),
                 ("config", "user.email", "test@example.invalid")]:
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    (source / "value.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=source, check=True, capture_output=True)
    root = tmp_path / "project"
    root.mkdir()
    models = ModelSelectionService(tmp_path / "settings")
    monkeypatch.setenv("YIPILOT_FIXTURE_KEY", "offline-secret")
    models.add(ProviderConnection(connection_id="local", base_url="https://example.invalid/v1",
        timeout_seconds=10, credential_ref="env:YIPILOT_FIXTURE_KEY", revision=1),
        ModelEntry(connection_id="local", model_id="coder", display_name="Fixture", output_limit=4096))
    models.set_default("local/coder")
    model = Model()
    monkeypatch.setattr(ModelSelectionService, "model_port", lambda *_: model)
    runtime = FixtureRuntime(root)
    runner = Runner(tmp_path / "runner", runtime, disk_reserve=0)
    runtime.backend = runner
    runner.start()
    server = make_server(runner, "t" * 32, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False,
                        headers={"Authorization": "Bearer " + "t" * 32})
    application = ProjectApplication(root, models=models, runner=RunnerClient(http))
    application.tasks.initialize_project()
    application.tasks.create_interactive_task("change", "Update value")
    application.repos.add_and_sync("1", str(source), "repo")
    try:
        yield application, model, runtime, source
    finally:
        http.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        runner.close()



def generate(application):
    result = application.generate("1", "repo", "Update value", context_files=("value.py",))
    assert result.status == "succeeded"
    return next(p for p in application.proposals("1") if p["proposal_run_id"] == result.run_id)


def apply(application, proposal, confirm=lambda _: True):
    return application.apply("1", proposal["proposal_run_id"],
                             proposal_hash=proposal["proposal_hash"], confirm=confirm)


def test_explicit_requests_replay_without_new_model_docker_or_approval(project):
    application, model, docker, _ = project
    first = application.generate("1", "repo", "Update value", request_id="generate-once", context_files=("value.py",))
    repeated = application.generate("1", "repo", "Update value", request_id="generate-once", context_files=("value.py",))
    assert repeated == first and len(model.requests) == 1
    with pytest.raises(DomainError, match="OPERATION_CONFLICT"):
        application.generate("1", "repo", "Different input", request_id="generate-once", context_files=("value.py",))
    proposal = application.proposals("1")[0]
    arguments = {"proposal_hash": proposal["proposal_hash"], "request_id": "apply-once"}
    run_id = application.apply("1", first.run_id, confirm=lambda _: True, **arguments)
    count = len(docker.calls)
    assert application.apply("1", first.run_id, confirm=lambda _: pytest.fail("replayed approval"), **arguments) == run_id
    assert len(docker.calls) == count
    verified = application.verify("1", run_id, confirm=lambda _: True, request_id="verify-once")
    assert verified.handoff is not None
    count = len(docker.calls)
    replay = application.verify("1", run_id, confirm=lambda _: pytest.fail("replayed approval"), request_id="verify-once")
    assert replay == verified and len(docker.calls) == count


def test_generate_apply_verify_failure_repair_apply_verify(project):
    application, model, docker, source = project
    proposal = generate(application)
    run_id = apply(application, proposal)
    docker.fail_tool = "verification-pytest"
    outcome = application.verify("1", run_id, confirm=lambda _: True)
    assert outcome.error is not None and outcome.handoff is None
    assert outcome.repair is not None and outcome.repair.status == "succeeded"
    assert outcome.repair_error is None
    assert len(model.requests) == 2 and model.closed == 2
    verification = next(r for r in application.sandbox.store.list_runs("1") if r.stage == "sandbox_verification")
    replayed_repair = application.repairs.repair("1", verification.run_id)
    assert replayed_repair == outcome.repair and len(model.requests) == 2
    chain_path = application.sandbox.store.event_journal_path(outcome.repair.run_id).parent / "artifacts/repair-chain.json"
    assert json.loads(chain_path.read_text(encoding="utf-8"))["attempt"] == 1
    assert "failed check" in model.requests[-1].user_instruction
    repair = next(p for p in application.proposals("1") if p["proposal_run_id"] == outcome.repair.run_id)
    assert repair["root_proposal_run_id"] == proposal["proposal_run_id"]
    repaired_run = apply(application, repair)
    docker.fail_tool = None
    verified = application.verify("1", repaired_run, confirm=lambda _: True)
    assert verified.handoff is not None, verified
    assert verified.error is None and verified.repair is None
    assert source.joinpath("value.py").read_text() == "value = 1\n"
    worktree = application.sandbox.root / ".yipilot/tasks/1/repos/repo/workspaces" / repaired_run
    assert (worktree / "value.py").read_text() == "value = 3\n"
    assert not any(r.status.value in {"pending", "running"} for r in application.sandbox.store.list_runs("1"))
    # Existing schema/IDs and hash verification remain readable through fresh stores.
    fresh = ProjectApplication(application.sandbox.root, models=application.models, runner=application.runner)
    fresh.sandbox.accepted("1", repaired_run)
    FileArtifactStore(fresh.sandbox.store).verify("1", verified.handoff.run_id, verified.handoff.artifact_id)
    root = fresh.sandbox.store.event_journal_path(proposal["proposal_run_id"]).parent
    frozen = json.loads((root / "artifacts/resolved-model.json").read_text())
    assert frozen["selector"] == "local/coder" and frozen["connection_revision"] == 1


@pytest.mark.parametrize("action", ["sandbox_execution", "sandbox_output", "sandbox_verification"])
def test_rejected_approval_is_persisted_and_cancels_pending_work(project, action):
    application, _, docker, _ = project
    proposal = generate(application)
    seen = []

    def confirm(prompt):
        seen.append(prompt)
        return prompt.action != action

    run_id = apply(application, proposal, confirm)
    if action == "sandbox_verification":
        outcome = application.verify("1", run_id, confirm=confirm)
        assert outcome.cancelled and outcome.repair is None
    else:
        assert run_id is None
    assert len(docker.calls) == (0 if action == "sandbox_execution" else 1)
    rejected = next(p for p in seen if p.action == action)
    root = application.sandbox.store.event_journal_path(rejected.run_id).parent
    records = [json.loads(body) for body in application.sandbox.store.db.records("approval", "1", rejected.run_id)]
    assert any(r["decision"] == "denied" and r["target_fingerprint"] == rejected.fingerprint for r in records)
    run = application.sandbox.store.get_run("1", rejected.run_id)
    events = JsonlEventJournal(root / "events.jsonl", task_id="1", run_id=run.run_id, stage=run.stage).read_after(0)
    assert any(e.type == "stage.progress" and e.payload.get("message") == "approval decided" for e in events)
    assert not any(r.status.value in {"pending", "running"} for r in application.sandbox.store.list_runs("1"))


def test_execution_failure_and_stale_or_non_boolean_intent_fail_closed(project):
    application, _, docker, _ = project
    proposal = generate(application)
    with pytest.raises(DomainError, match="APPROVAL_STALE"):
        apply(application, {**proposal, "proposal_hash": "0" * 64})
    with pytest.raises(DomainError, match="APPROVAL_CONFIRMATION_INVALID"):
        apply(application, proposal, lambda _: "yes")
    assert docker.calls == []
    docker.fail_tool = "sandbox-apply-patch"
    with pytest.raises(DomainError, match="SANDBOX_OUTPUT_INVALID"):
        apply(application, proposal)
    assert not application.applied_runs("1")
    assert not list(application.sandbox.root.glob(".yipilot/tasks/1/repos/repo/workspaces/*/.git"))


def test_output_changed_during_review_cannot_be_written_to_worktree(project):
    application, _, _, source = project
    proposal = generate(application)

    def confirm(prompt):
        if prompt.action == "sandbox_output":
            root = application.sandbox.store.event_journal_path(prompt.run_id).parent
            (root / "execution/value.py").write_text("value = 999\n", encoding="utf-8")
        return True

    with pytest.raises(DomainError, match="SANDBOX_OUTPUT_UNAPPROVED"):
        apply(application, proposal, confirm)
    assert application.applied_runs("1") == []
    assert source.joinpath("value.py").read_text() == "value = 1\n"
    assert not list(application.sandbox.root.glob(".yipilot/tasks/1/repos/repo/workspaces/*/.git"))


def test_cli_and_interactive_generate_use_the_same_application_path(project):
    application, model, _, _ = project
    root = application.sandbox.root
    settings = root.parent / "settings"
    response = CliRunner().invoke(app, ["code", "generate", "--project", str(root),
        "--settings-dir", str(settings), "--taskid", "1", "--alias", "repo",
        "--instruction", "CLI input", "--context-file", "value.py", "--output", "json"])
    assert response.exit_code == 0, response.output
    output = StringIO()
    session = InteractiveSession(project=root, settings_dir=settings,
        input=StringIO("1\nvalue.py\ny\n"), output=output)
    session.session_task_id = "1"
    session._run_live_proposal()
    assert "PatchProposal evidence is ready" in output.getvalue()
    assert len(application.proposals("1")) == 2 and len(model.requests) == 2


@pytest.mark.parametrize("accepted", [False, True])
def test_lost_submit_response_is_reconciled_without_resubmitting(project, monkeypatch, accepted):
    application, _, runtime, source = project
    proposal = generate(application)
    original = application.runner.submit
    submissions = []

    def lost(bundle):
        submissions.append(bundle)
        if accepted:
            original(bundle)
            assert runtime.entered.wait(timeout=5)
        raise httpx.ReadTimeout("fixture lost response")

    monkeypatch.setattr(application.runner, "submit", lost)
    with pytest.raises(DomainError, match="REMOTE_RESULT_PENDING"):
        apply(application, proposal)
    run = next(r for r in application.sandbox.store.list_runs("1") if r.stage == "sandbox_apply")
    assert run.status.value == "paused"
    recovered = application.recover(run.run_id)
    assert recovered.status.value == ("failed" if accepted else "paused")
    assert len(submissions) == 1 and len(runtime.calls) == int(accepted)
    assert not application.applied_runs("1") and source.joinpath("value.py").read_text() == "value = 1\n"


def test_two_runs_wait_remotely_without_holding_the_project_lock(project):
    application, _, runtime, _ = project
    proposal = generate(application)
    # No host workloads run: this fixture tests local ownership with two admitted Jobs.
    runtime.backend.memory_budget, runtime.backend.cpu_budget = 2 * 1024**3, 3000
    runtime.barrier = threading.Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(apply, application, proposal)
        assert runtime.entered.wait(timeout=10)
        second = pool.submit(apply, application, proposal)
        runs = (first.result(timeout=40), second.result(timeout=40))
    assert len(set(runs)) == 2 and len(runtime.calls) == 2


@pytest.mark.parametrize("relative", ["remote-job/result.json", "remote-job/status.json", "remote-job/input.zip",
                                     "remote-job/result.zip", "sandbox-output/patch.diff", "execution/value.py"])
def test_tampered_remote_evidence_cannot_be_verified_or_handed_off(project, relative):
    application, _, _, source = project
    run_id = apply(application, generate(application))
    root = application.sandbox.store.event_journal_path(run_id).parent
    path = root / relative
    path.write_bytes(path.read_bytes() + b"tampered")
    outcome = application.verify("1", run_id, confirm=lambda _: True)
    assert outcome.error is not None and outcome.handoff is None
    assert source.joinpath("value.py").read_text() == "value = 1\n"
