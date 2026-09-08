import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from yipilot.harness.git_workspace import GitWorkspaceHarness
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore
from yipilot.transport.cli.app import app
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.workspace_service import WorkspaceService

runner = CliRunner()


def _git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        check=True,
    )


def _source_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(source, "add", "tracked.py")
    _git(source, "commit", "-m", "initial")
    return source


def test_m0_m2_acceptance_uses_local_cli_and_isolated_git_workspace(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = _source_repository(tmp_path)

    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["task", "init", "--project", str(project), "--taskid", "1001"],
        ).exit_code
        == 0
    )
    store = FileTaskStore(project)
    created_runs = store.list_runs("1001")
    assert len(created_runs) == 1
    initialized_run = created_runs[0]
    assert store.get_operation("1001", initialized_run.operation_id).operation_type == "task_init"
    assert [event.type for event in JsonlEventJournal(
        store.event_journal_path(initialized_run.run_id)
    ).read_after(0)] == ["run.started", "run.succeeded"]

    replay = runner.invoke(
        app,
        [
            "run",
            "watch",
            "--project",
            str(project),
            "--run-id",
            initialized_run.run_id,
            "--after-sequence",
            "1",
            "--output",
            "jsonl",
        ],
    )
    assert replay.exit_code == 0
    assert [json.loads(line)["sequence"] for line in replay.stdout.splitlines()] == [2]
    synced = runner.invoke(
        app,
        [
            "task",
            "repo",
            "add",
            "--project",
            str(project),
            "--taskid",
            "1001",
            "--url",
            str(source),
            "--alias",
            "sample",
            "--sync",
            "--output",
            "json",
        ],
    )
    assert synced.exit_code == 0
    assert json.loads(synced.stdout)["head_sha"] == _git(
        source, "rev-parse", "HEAD"
    ).stdout.strip()

    prepared = runner.invoke(
        app,
        [
            "task",
            "workspace",
            "prepare",
            "--project",
            str(project),
            "--taskid",
            "1001",
            "--alias",
            "sample",
            "--run-id",
            "acceptance-run",
            "--output",
            "json",
        ],
    )
    assert prepared.exit_code == 0
    workspace = Path(json.loads(prepared.stdout)["path"])
    (workspace / "tracked.py").write_text("value = 2\n", encoding="utf-8")

    persisted_workspace = WorkspaceService(project, GitRunner()).prepare(
        "1001", "sample", "acceptance-run"
    )
    report = GitWorkspaceHarness(persisted_workspace).run()

    assert report.status == "passed"
    assert _git(source, "branch", "--show-current").stdout.strip() == "main"
    assert _git(source, "status", "--porcelain").stdout == ""
