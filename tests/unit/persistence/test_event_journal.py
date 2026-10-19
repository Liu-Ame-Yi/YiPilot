import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from yipilot.domain.errors import DomainError
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore
from yipilot.workflow.project_service import ProjectService
from yipilot.workflow.recovery_service import RecoveryService


def journal_for(project):
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    op = store.create_operation("1001", "task_init", "events", {})
    run, _ = store.get_or_create_run("1001", op.operation_id, "task_init")
    return JsonlEventJournal(store.event_journal_path(run.run_id), task_id="1001", run_id=run.run_id, stage="task_init")


def test_event_replay_after_cursor(tmp_path: Path) -> None:
    journal = journal_for(tmp_path)

    first = journal.append("run.started", {"message": "start"})
    second = journal.append("run.succeeded", {"message": "done"})

    assert first.sequence == 1
    assert second.sequence == 2
    assert [event.sequence for event in journal.read_after(1)] == [2]


def test_concurrent_append_has_contiguous_sequences_and_valid_json(tmp_path: Path) -> None:
    journal = journal_for(tmp_path)

    def append_events(worker: int) -> None:
        for index in range(25):
            journal.append("stage.progress", {"worker": worker, "index": index})

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(append_events, range(2)))

    lines = [e.model_dump_json() for e in journal.read_after(0)]
    decoded = [json.loads(line) for line in lines]
    assert [event["sequence"] for event in decoded] == list(range(1, 51))
    assert [event.sequence for event in journal.read_after(0)] == list(range(1, 51))


def test_event_payload_is_redacted_before_persistence(tmp_path: Path) -> None:
    journal = journal_for(tmp_path)

    event = journal.append(
        "stage.progress",
        {
            "Authorization": "Bearer top-secret",
            "message": "request used Bearer another-secret",
        },
    )

    persisted = "".join(e.model_dump_json() for e in journal.read_after(0))
    assert "top-secret" not in persisted
    assert "another-secret" not in persisted
    assert event.payload == {
        "Authorization": "[REDACTED]",
        "message": "request used Bearer [REDACTED]",
    }


def test_oversized_event_payload_is_rejected_without_creating_an_event(
    tmp_path: Path,
) -> None:
    journal = journal_for(tmp_path)

    with pytest.raises(DomainError, match="EVENT_PAYLOAD_TOO_LARGE"):
        journal.append("stage.progress", {"message": "x" * 16_385})

    assert not (tmp_path / "events.jsonl").exists()


def test_corrupt_event_line_is_reported_instead_of_silently_skipped(tmp_path: Path) -> None:
    journal = journal_for(tmp_path)
    journal.append("stage.progress", {})
    with FileTaskStore(tmp_path).db.transaction() as connection:
        connection.execute("UPDATE events SET body='not-json'")

    with pytest.raises(DomainError, match="EVENT_JOURNAL_CORRUPT"):
        journal.read_after(0)


@pytest.fixture
def store_with_run(tmp_path: Path) -> tuple[FileTaskStore, str]:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation(
        "1001", "task_init", "task-init-1001", {"task_id": "1001"}
    )
    run = store.create_run("1001", operation.operation_id, "task_init")
    return store, run.run_id


@pytest.mark.parametrize("interrupted_status", ["running", "paused"])
def test_recovery_never_restarts_interrupted_run(
    store_with_run: tuple[FileTaskStore, str],
    interrupted_status: str,
) -> None:
    store, run_id = store_with_run
    store.set_run_status(run_id, "running")
    if interrupted_status == "paused":
        store.set_run_status(run_id, "paused")

    recovered = RecoveryService(store).recover(run_id)

    assert recovered.status == "failed"
    assert recovered.error_code == "EXTERNAL_RESULT_UNCERTAIN"
    events = JsonlEventJournal(store.event_journal_path(run_id)).read_after(0)
    assert events[-1].type == "run.failed"
    assert events[-1].payload["error_code"] == "EXTERNAL_RESULT_UNCERTAIN"


@pytest.mark.parametrize("unchanged_status", ["pending", "succeeded", "failed", "cancelled"])
def test_recovery_leaves_pending_and_terminal_runs_unchanged(
    store_with_run: tuple[FileTaskStore, str],
    unchanged_status: str,
) -> None:
    store, run_id = store_with_run
    if unchanged_status in {"succeeded", "failed"}:
        store.set_run_status(run_id, "running")
    if unchanged_status != "pending":
        store.set_run_status(run_id, unchanged_status)

    recovered = RecoveryService(store).recover(run_id)

    assert recovered.status == unchanged_status
    assert not store.event_journal_path(run_id).exists()


def test_task4_extension_preserves_the_pending_run_json_contract(
    store_with_run: tuple[FileTaskStore, str],
) -> None:
    store, run_id = store_with_run
    persisted = store.find_run(run_id).model_dump(mode="json", exclude_none=True)
    assert "error_code" not in persisted


def test_terminal_event_write_failure_never_marks_run_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ProjectService(tmp_path)
    service.initialize_project()
    original_append = JsonlEventJournal.append

    def fail_terminal_event(
        journal: JsonlEventJournal,
        event_type: str,
        payload: dict[str, object],
        **kwargs: object,
    ) -> object:
        if event_type == "run.succeeded":
            raise OSError("disk full")
        return original_append(journal, event_type, payload, **kwargs)

    monkeypatch.setattr(JsonlEventJournal, "append", fail_terminal_event)

    with pytest.raises(OSError, match="disk full"):
        service.initialize_task("1001")

    runs = FileTaskStore(tmp_path).list_runs("1001")
    assert len(runs) == 1
    assert runs[0].status == "running"
