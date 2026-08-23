import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from yipilot.transport.cli.app import app

runner = CliRunner()


def _git(path: Path, *args: str) -> None:
    subprocess.check_call(["git", *args], cwd=path)


def test_replay_model_cli_streams_durable_events(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(source, "add", "tracked.py")
    _git(source, "commit", "-m", "initial")
    project = tmp_path / "project"
    project.mkdir()
    assert runner.invoke(app, ["init", "--project", str(project)]).exit_code == 0
    assert runner.invoke(
        app, ["task", "init", "--project", str(project), "--taskid", "1001"]
    ).exit_code == 0
    assert runner.invoke(
        app,
        [
            "task", "repo", "add", "--project", str(project), "--taskid", "1001",
            "--url", str(source), "--alias", "sample", "--sync",
        ],
    ).exit_code == 0

    generated = runner.invoke(
        app,
        [
            "code", "generate", "--project", str(project), "--taskid", "1001",
            "--alias", "sample", "--instruction", "yipilot replay model diagnostic",
            "--planner", "replay-model", "--output", "json",
        ],
    )

    assert generated.exit_code == 0, generated.stdout
    run_id = json.loads(generated.stdout)["run_id"]
    watched = runner.invoke(
        app, ["run", "watch", "--project", str(project), "--run-id", run_id, "--output", "jsonl"]
    )
    assert watched.exit_code == 0
    assert [json.loads(line)["type"] for line in watched.stdout.splitlines()] == [
        "run.started", "model.delta", "model.message_completed", "plan.created",
        "tool.started", "tool.completed", "harness.completed", "run.succeeded",
    ]
