import json
import threading
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import yipilot.transport.cli.app as cli_app_module
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


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "Infinity", "-inf"])
def test_run_watch_rejects_non_finite_follow_timeout_as_stable_jsonl_error(
    tmp_path: Path, value: str
) -> None:
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
            "a2fc37e8-36c1-4d47-b1e3-e2430508dbf4",
            "--follow-timeout-seconds",
            value,
            "--output",
            "jsonl",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["code"] == "FOLLOW_TIMEOUT_INVALID"


def test_run_watch_follows_events_appended_after_its_first_empty_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    first_empty_poll = threading.Event()
    original_read_after = JsonlEventJournal.read_after

    def signal_first_empty_poll(
        watched_journal: JsonlEventJournal, after_sequence: int
    ) -> list[object]:
        events = original_read_after(watched_journal, after_sequence)
        if watched_journal is not journal and not events:
            first_empty_poll.set()
        return events  # type: ignore[return-value]

    monkeypatch.setattr(JsonlEventJournal, "read_after", signal_first_empty_poll)
    result_holder: list[object] = []
    watcher = threading.Thread(
        target=lambda: result_holder.append(
            runner.invoke(
                app,
                [
                    "run",
                    "watch",
                    "--project",
                    str(tmp_path),
                    "--run-id",
                    run.run_id,
                    "--follow-timeout-seconds",
                    "1",
                    "--output",
                    "jsonl",
                ],
            )
        )
    )
    watcher.start()
    assert first_empty_poll.wait(timeout=1)

    journal.append("stage.progress", {"message": "followed"})
    journal.append("run.succeeded", {"message": "done"})
    watcher.join(timeout=1)

    assert not watcher.is_alive()
    result = result_holder[0]
    assert result.exit_code == 0  # type: ignore[union-attr]
    assert [json.loads(line)["sequence"] for line in result.stdout.splitlines()] == [  # type: ignore[union-attr]
        1,
        2,
    ]


def test_run_watch_sleep_is_limited_by_remaining_follow_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyJournal:
        def read_after(self, after_sequence: int) -> list[object]:
            return []

    class IgnoredRenderer:
        def render(self, event: object) -> None:
            return

    clock = iter([0.0, 0.0, 0.01])
    sleeps: list[float] = []
    monkeypatch.setattr(cli_app_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cli_app_module.time, "sleep", sleeps.append)

    with pytest.raises(typer.Exit) as raised:
        cli_app_module._watch_journal(EmptyJournal(), 0, 0.01, IgnoredRenderer())

    assert raised.value.exit_code == 4
    assert sleeps == [0.01]


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
