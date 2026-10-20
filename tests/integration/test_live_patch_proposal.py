import json
import subprocess
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from tests.helpers.sqlite_records import event_text
from yipilot.domain.model import ModelChunk, ModelRequest
from yipilot.domain.provider import ModelEntry, ProviderConnection
from yipilot.models.secrets import SecretStore
from yipilot.models.selection import ModelSelectionService
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore
from yipilot.transport.cli.app import app
from yipilot.workflow.code_service import CodeService

runner = CliRunner()


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=path, text=True).strip()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (source / "unselected.py").write_text("private = 2\n", encoding="utf-8")
    _git(source, "add", "tracked.py", "unselected.py")
    _git(source, "commit", "-m", "initial")
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(app, ["task", "init", "--project", str(project), "--taskid", "1001"]).exit_code == 0
    assert runner.invoke(app, ["task", "repo", "add", "--project", str(project), "--taskid", "1001", "--url", str(source), "--alias", "sample", "--sync"]).exit_code == 0
    return project, source


class FixedPort:
    def __init__(self, content: str | Exception) -> None:
        self.content = content
        self.requests: list[ModelRequest] = []

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        self.requests.append(request)
        if isinstance(self.content, Exception):
            raise self.content
        return tuple(ModelChunk(content=self.content[index:index + 4096]) for index in range(0, len(self.content), 4096))


def _models(tmp_path: Path, port: FixedPort) -> ModelSelectionService:
    models = ModelSelectionService(
        tmp_path / "settings",
        secrets=SecretStore(environ={"KEY": "unit-secret"}),
        port_factory=lambda *_: port,
    )
    models.add(
        ProviderConnection(connection_id="local", base_url="https://example.test/v1", timeout_seconds=10, credential_ref="env:KEY", revision=2),
        ModelEntry(connection_id="local", model_id="coder", display_name="Coder", output_limit=64 * 1024),
    )
    models.set_default("local/coder")
    return models


def _proposal(path: str = "tracked.py") -> str:
    return json.dumps({"schema_version": 1, "summary": "Update tracked value.", "patches": [{"path": path, "unified_diff": f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"}]})


def test_fixed_port_uses_the_runtime_and_preserves_source(tmp_path: Path, monkeypatch) -> None:
    from yipilot.workspace.workspace_service import WorkspaceService

    monkeypatch.setattr(WorkspaceService, "prepare", lambda *_a, **_k: pytest.fail("host checkout invoked"))
    project, source = _project(tmp_path)
    port = FixedPort(_proposal())
    result = CodeService(project, model_selection_service=_models(tmp_path, port)).generate("1001", "sample", "change tracked", context_files=("tracked.py",))

    assert result.status == "succeeded"
    assert result.plan_artifact_ref and result.patch_proposal_artifact_ref
    assert _git(source, "status", "--porcelain") == ""
    assert (source / "tracked.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "tracked.py" in port.requests[0].user_instruction
    assert "unselected.py" not in port.requests[0].user_instruction
    store = FileTaskStore(project)
    run_root = store.event_journal_path(result.run_id).parent
    assert (run_root / "baseline/tracked.py").read_bytes() == b"value = 1\n"
    assert not list((project / ".yipilot/tasks/1001/repos/sample").glob("workspaces/*"))
    assert CodeService(project).proposals("1001")[0]["base_sha"] == _git(source, "rev-parse", "HEAD")
    records = {item.relative_path for item in FileArtifactStore(store).list("1001", result.run_id)}
    assert {"artifacts/resolved-model.json", "artifacts/live-context.md", "artifacts/model-response.json", "artifacts/model-call.json", "artifacts/agent-plan.json", "artifacts/patch-proposal.json"} <= records
    assert [event.type for event in JsonlEventJournal(store.event_journal_path(result.run_id)).read_after(0)] == ["run.started", "turn.started", "model.delta", "model.message_completed", "proposal.created", "plan.created", "harness.completed", "turn.completed", "run.succeeded"]


@pytest.mark.parametrize("content, code", [("not json", "PATCH_PROPOSAL_INVALID"), (_proposal("../escape.py"), "PATCH_PROPOSAL_INVALID"), (_proposal("/absolute.py"), "PATCH_PROPOSAL_INVALID")])
def test_invalid_proposals_fail_safely(tmp_path: Path, content: str, code: str) -> None:
    project, source = _project(tmp_path)
    result = CodeService(project, model_selection_service=_models(tmp_path, FixedPort(content))).generate("1001", "sample", "invalid", context_files=("tracked.py",))

    assert (result.status, result.error_code) == ("failed", code)
    assert _git(source, "status", "--porcelain") == ""
    events = JsonlEventJournal(FileTaskStore(project).event_journal_path(result.run_id)).read_after(0)
    assert [event.type for event in events][-2:] == ["turn.failed", "run.failed"]


def test_provider_failure_is_a_failed_turn_without_secret_leak(tmp_path: Path) -> None:
    project, source = _project(tmp_path)
    result = CodeService(project, model_selection_service=_models(tmp_path, FixedPort(RuntimeError("Authorization: Bearer unit-secret")))).generate("1001", "sample", "private Authorization: Bearer instruction-secret", context_files=("tracked.py",))

    assert result.status == "failed"
    assert _git(source, "status", "--porcelain") == ""
    text = event_text(FileTaskStore(project).event_journal_path(result.run_id))
    assert "unit-secret" not in text and "instruction-secret" not in text and "Authorization" not in text


def test_cli_uses_model_and_rejects_old_planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _source = _project(tmp_path)
    models = _models(tmp_path, FixedPort(_proposal()))
    monkeypatch.setattr("yipilot.app.ModelSelectionService", lambda *_: models)

    result = runner.invoke(app, ["code", "generate", "--project", str(project), "--taskid", "1001", "--alias", "sample", "--instruction", "change", "--context-file", "tracked.py", "--output", "json"])
    old = runner.invoke(app, ["code", "generate", "--taskid", "1001", "--alias", "sample", "--instruction", "change", "--planner", "fake"])

    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "succeeded"
    assert old.exit_code != 0 and "--planner" in Text.from_ansi(old.output).plain
