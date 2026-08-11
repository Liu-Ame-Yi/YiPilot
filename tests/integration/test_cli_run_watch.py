import json
from pathlib import Path

from typer.testing import CliRunner

from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.transport.cli.app import app

runner = CliRunner()


def test_run_watch_replays_after_cursor_as_jsonl(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation(
        "1001", "task_init", "task-init-1001", {"task_id": "1001"}
    )
    run = store.create_run("1001", operation.operation_id, "task_init")
    journal = JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id="1001",
        run_id=run.run_id,
        stage="task_init",
    )
    journal.append("run.started", {"message": "start"})
    journal.append("stage.progress", {"message": "middle"})
    journal.append("run.succeeded", {"message": "done"})

    result = runner.invoke(
        app,
        [
            "run",
            "watch",
            "--project",
            str(tmp_path),
            "--run-id",
            run.run_id,
            "--after-sequence",
            "1",
            "--output",
            "jsonl",
        ],
    )

    assert result.exit_code == 0
    assert [json.loads(line)["sequence"] for line in result.stdout.splitlines()] == [2, 3]


def test_run_watch_jsonl_is_one_machine_readable_event_per_line(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation(
        "1001", "task_init", "task-init-1001", {"task_id": "1001"}
    )
    run = store.create_run("1001", operation.operation_id, "task_init")
    journal = JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id="1001",
        run_id=run.run_id,
        stage="task_init",
    )
    journal.append("model.delta", {"content": "hello"})
    journal.append("model.message_completed", {})
    journal.append("run.succeeded", {"message": "done"})

    result = runner.invoke(
        app,
        ["run", "watch", "--project", str(tmp_path), "--run-id", run.run_id, "--output", "jsonl"],
    )

    lines = result.stdout.splitlines()
    assert result.exit_code == 0
    assert len(lines) == 3
    assert "\x1b" not in result.stdout
    assert [json.loads(line)["type"] for line in lines] == [
        "model.delta",
        "model.message_completed",
        "run.succeeded",
    ]


def test_run_watch_human_streams_model_text_and_sends_diagnostics_to_stderr(
    tmp_path: Path,
) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation(
        "1001", "task_init", "task-init-1001", {"task_id": "1001"}
    )
    run = store.create_run("1001", operation.operation_id, "task_init")
    journal = JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id="1001",
        run_id=run.run_id,
        stage="task_init",
    )
    journal.append("model.delta", {"content": "hello"})
    journal.append("model.delta", {"content": " world"})
    journal.append("model.message_completed", {})
    journal.append("run.succeeded", {"message": "done"})

    result = runner.invoke(
        app,
        ["run", "watch", "--project", str(tmp_path), "--run-id", run.run_id],
    )

    assert result.exit_code == 0
    assert result.stdout == "hello world\n"
    assert "run.succeeded" in result.stderr


def test_run_watch_times_out_with_exit_code_four(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation(
        "1001", "task_init", "task-init-1001", {"task_id": "1001"}
    )
    run = store.create_run("1001", operation.operation_id, "task_init")

    result = runner.invoke(
        app,
        [
            "run",
            "watch",
            "--project",
            str(tmp_path),
            "--run-id",
            run.run_id,
            "--follow-timeout-seconds",
            "0.01",
            "--output",
            "jsonl",
        ],
    )

    assert result.exit_code == 4
    assert result.stdout == ""


def test_run_watch_keeps_jsonl_stdout_empty_for_stable_errors(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()

    result = runner.invoke(
        app,
        [
            "run",
            "watch",
            "--project",
            str(tmp_path),
            "--run-id",
            "not-a-uuid",
            "--after-sequence",
            "-1",
            "--output",
            "jsonl",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "EVENT_CURSOR_INVALID"


def test_run_watch_maps_unknown_run_and_corrupt_journal_to_stable_errors(
    tmp_path: Path,
) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation(
        "1001", "task_init", "task-init-1001", {"task_id": "1001"}
    )
    run = store.create_run("1001", operation.operation_id, "task_init")
    store.event_journal_path(run.run_id).write_text("not-json\n", encoding="utf-8")

    corrupt = runner.invoke(
        app,
        ["run", "watch", "--project", str(tmp_path), "--run-id", run.run_id, "--output", "jsonl"],
    )
    unknown = runner.invoke(
        app,
        [
            "run",
            "watch",
            "--project",
            str(tmp_path),
            "--run-id",
            "a2fc37e8-36c1-4d47-b1e3-e2430508dbf4",
            "--output",
            "jsonl",
        ],
    )

    assert corrupt.exit_code == 1
    assert corrupt.stdout == ""
    assert json.loads(corrupt.stderr)["code"] == "EVENT_JOURNAL_CORRUPT"
    assert unknown.exit_code == 1
    assert unknown.stdout == ""
    assert json.loads(unknown.stderr)["code"] == "RUN_NOT_FOUND"
