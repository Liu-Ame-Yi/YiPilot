import json
from pathlib import Path

from typer.testing import CliRunner

from yipilot.transport.cli.app import app

runner = CliRunner()


def _canonical_stdout(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def test_cli_init_and_task_init_are_stable_json(tmp_path: Path) -> None:
    initialized = runner.invoke(
        app, ["init", "--project", str(tmp_path), "--output", "json"]
    )
    assert initialized.exit_code == 0
    assert initialized.stdout == '{"schema_version":1}\n'

    result = runner.invoke(
        app,
        [
            "task",
            "init",
            "--project",
            str(tmp_path),
            "--taskid",
            "1001",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    task = json.loads(result.stdout)
    assert task["task_id"] == "1001"
    assert result.stdout == _canonical_stdout(task)


def test_cli_task_show_and_run_queries_use_persisted_records(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app, ["task", "init", "--project", str(tmp_path), "--taskid", "1001"]
    )
    assert created.exit_code == 0

    shown = runner.invoke(
        app,
        [
            "task",
            "show",
            "--project",
            str(tmp_path),
            "--taskid",
            "1001",
            "--output",
            "json",
        ],
    )
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["task_id"] == "1001"

    listed = runner.invoke(
        app,
        [
            "run",
            "list",
            "--project",
            str(tmp_path),
            "--taskid",
            "1001",
            "--output",
            "json",
        ],
    )
    assert listed.exit_code == 0
    run_list = json.loads(listed.stdout)
    assert run_list["task_id"] == "1001"
    assert len(run_list["runs"]) == 1
    assert run_list["runs"][0]["status"] == "succeeded"
    assert listed.stdout == _canonical_stdout(run_list)

    run_id = run_list["runs"][0]["run_id"]
    shown_run = runner.invoke(
        app,
        [
            "run",
            "show",
            "--project",
            str(tmp_path),
            "--taskid",
            "1001",
            "--run-id",
            run_id,
            "--output",
            "json",
        ],
    )
    assert shown_run.exit_code == 0
    assert json.loads(shown_run.stdout) == run_list["runs"][0]


def test_invalid_task_id_leaves_no_partial_task_or_event(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "task",
            "init",
            "--project",
            str(tmp_path),
            "--taskid",
            "-1",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "INVALID_TASK_ID"
    assert not list((tmp_path / ".yipilot" / "tasks").iterdir())
    assert not list((tmp_path / ".yipilot").rglob("events.jsonl"))


def test_unknown_task_is_a_stable_error_without_traceback(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "task",
            "show",
            "--project",
            str(tmp_path),
            "--taskid",
            "1001",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "TASK_NOT_FOUND"
    assert "Traceback" not in result.stdout
