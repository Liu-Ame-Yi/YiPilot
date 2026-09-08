"""Offline composition checks: only model and Docker execution are replaced.

The receipt fixture is not a claim that Docker or the target checks actually ran.
Source export, approvals, Git worktrees, output validation, stores and repair
services execute their production code.
"""

import json
import subprocess
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yipilot.app import ProjectApplication
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ArtifactRef
from yipilot.domain.model import ModelChunk
from yipilot.domain.provider import ModelEntry, ProviderConnection
from yipilot.domain.sandbox import SandboxResult
from yipilot.models.selection import ModelSelectionService
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.filesystem import atomic_write_json
from yipilot.transport.cli.app import app
from yipilot.transport.cli.interactive import InteractiveSession
from yipilot.workspace.docker_worker import DockerSandboxWorker
from yipilot.workspace.sandbox_output import SandboxOutputService


class Model:
    def __init__(self):
        self.requests = []
        self.closed = 0

    def stream(self, request):
        self.requests.append(request)
        value = 3 if "Bounded Docker repair" in request.user_instruction else 2
        return (ModelChunk(content=json.dumps({"summary": "Update value", "patches": [{
            "path": "value.py", "unified_diff":
            f"--- a/value.py\n+++ b/value.py\n@@ -1 +1 @@\n-value = 1\n+value = {value}\n",
        }]})),)

    def close(self):
        self.closed += 1


class DockerReceipts:
    def __init__(self):
        self.calls = []
        self.fail_tool = None

    def execute(self, worker, request, *, cancel_event=None):
        self.calls.append(request)
        root = Path(request.execution_directory).parent
        failed = request.tool_id == self.fail_tool
        if request.tool_id == "sandbox-apply-patch":
            payload = Path(request.execution_directory) / "yipilot-approved-patch.diff"
            # Fixture-only edit, never execute a model-supplied command on the host.
            added = next(line[1:] for line in payload.read_text().splitlines()
                         if line.startswith("+value = "))
            (payload.parent / "value.py").write_text(added + "\n", encoding="utf-8")
            payload.unlink()
        artifacts = FileArtifactStore(worker._store)
        refs = []
        for stream in ("stdout", "stderr"):
            record = artifacts.write_bytes(request.task_id, request.run_id,
                producer="offline-docker-fixture", relative_path=f"artifacts/{stream}.txt",
                content=b"failed check\n" if failed else b"fixture check\n",
                input_fingerprint=request.fingerprint, operation_id=request.operation_id)
            refs.append(ArtifactRef.model_validate(record.model_dump(include={
                "artifact_id", "task_id", "operation_id", "run_id", "relative_path", "sha256"})))
        output = SandboxOutputService(worker._root, worker._sources._git).inspect(
            worker._prepared, request, worker._policy)
        now = datetime.now(UTC)
        result = SandboxResult(task_id=request.task_id, operation_id=request.operation_id,
            run_id=request.run_id, request_fingerprint=request.fingerprint,
            container_id="c" * 64, image_digest=request.image_digest,
            docker_version="offline-fixture", backend="unknown",
            enhanced_container_isolation=None, network="none", effective_uid=10001,
            effective_gid=10001, resource_limits=request.budgets, reason="exited",
            exit_code=1 if failed else 0, started_at=now, ended_at=now,
            stdout_artifact_ref=refs[0], stderr_artifact_ref=refs[1],
            stdout_bytes=13 if failed else 14, stderr_bytes=13 if failed else 14,
            output_manifest_hash=output.manifest.fingerprint, fallback_occurred=False)
        facts = {
            "Id": result.container_id, "Image": request.image_digest,
            "State": {"Running": False, "Status": "exited", "ExitCode": result.exit_code},
            "Config": {"User": "10001:10001", "WorkingDir": "/workspace",
                "Entrypoint": [request.argv[0]], "Cmd": list(request.argv[1:]),
                "Labels": {"yipilot.request": request.fingerprint, "yipilot.run": request.run_id},
                "Healthcheck": {"Test": ["NONE"]}},
            "Mounts": [{"Type": "bind", "Destination": "/workspace", "RW": True}],
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True,
                "Privileged": False, "PidMode": "", "IpcMode": "private", "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges=true"], "PidsLimit": request.budgets.pid_limit,
                "Memory": request.budgets.memory_bytes, "MemorySwap": request.budgets.memory_bytes,
                "NanoCpus": int(request.budgets.cpu_count * 10**9), "LogConfig": {"Type": "none"},
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=67108864,mode=1777"},
                "Mounts": [{"Source": request.execution_directory, "Target": "/workspace"}]},
        }
        atomic_write_json(root / "docker-worker/result.json", result.model_dump(mode="json"))
        atomic_write_json(root / "docker-worker/container-after.json", facts)
        atomic_write_json(root / "docker-worker/cleanup.json", {"container_removed": True})
        return result


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
    application = ProjectApplication(root, models=models)
    application.tasks.initialize_project()
    application.tasks.create_interactive_task("change", "Update value")
    application.repos.add_and_sync("1", str(source), "repo")
    application.sandbox.store.set_sandbox_image_digest("sha256:" + "b" * 64)
    docker = DockerReceipts()
    monkeypatch.setattr(DockerSandboxWorker, "execute",
        lambda worker, request, **kw: docker.execute(worker, request, **kw))
    return application, model, docker, source


def generate(application):
    result = application.generate("1", "repo", "Update value", context_files=("value.py",))
    assert result.status == "succeeded"
    return next(p for p in application.proposals("1") if p["proposal_run_id"] == result.run_id)


def apply(application, proposal, confirm=lambda _: True):
    return application.apply("1", proposal["proposal_run_id"],
                             proposal_hash=proposal["proposal_hash"], confirm=confirm)


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
    fresh = ProjectApplication(application.sandbox.root, models=application.models)
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
    records = [json.loads(p.read_text()) for p in (root / "approvals").glob("*.json")]
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
