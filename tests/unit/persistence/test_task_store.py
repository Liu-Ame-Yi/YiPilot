from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from yipilot.domain.errors import DomainError
from yipilot.persistence.task_store import (
    FileTaskStore,
    _canonical_request,
)

JSON_SAFE_INTEGER = st.integers(min_value=-(2**63), max_value=2**63 - 1)
JSON_KEY_ALPHABET = st.sampled_from(
    [
        "a",
        "A",
        "0",
        " ",
        "_",
        "-",
        '"',
        "\\",
        "\n",
        "\x00",
        "é",
        "中",
        "🙂",
        "\u0301",
    ]
)


def _symlink_or_skip(link: Path, target: Path, *, is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def test_initialize_creates_runtime_layout_and_persists_project(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)

    project = store.initialize_project()

    assert project.schema_version == 1
    assert (tmp_path / ".yipilot" / "state.db").is_file()
    assert (tmp_path / ".yipilot" / "tasks").is_dir()
    assert (tmp_path / ".yipilot" / "locks").is_dir()
    assert (tmp_path / ".yipilot" / "cache" / "repos").is_dir()
    assert (tmp_path / ".yipilot" / "audit").is_dir()
    assert store.get_project() == project


def test_concurrent_initialization_creates_one_valid_project(tmp_path: Path) -> None:
    def initialize(_: int) -> int:
        return FileTaskStore(tmp_path).initialize_project().schema_version

    with ThreadPoolExecutor(max_workers=8) as executor:
        schema_versions = list(executor.map(initialize, range(16)))

    assert schema_versions == [1] * 16
    assert FileTaskStore(tmp_path).get_project().schema_version == 1


def test_invalid_task_id_leaves_no_task_directory(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()

    with pytest.raises(DomainError, match="INVALID_TASK_ID"):
        store.create_task("001")

    assert not list((tmp_path / ".yipilot" / "tasks").iterdir())


def test_interactive_task_details_and_next_id_persist_without_a_run(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()

    first = store.create_next_task("终端任务", "验证任务向导")
    store.create_next_task("下一任务", "保持顺序 ID")

    assert (first.task_id, first.title, first.development_goal) == (
        "1", "终端任务", "验证任务向导"
    )
    assert [task.task_id for task in store.list_tasks()] == ["1", "2"]
    assert store.list_runs(first.task_id) == []


def test_failed_project_write_leaves_no_authoritative_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FileTaskStore(tmp_path)
    def fail_initialize(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(store.db, "initialize", fail_initialize)
    with pytest.raises(OSError, match="disk full"):
        store.initialize_project()
    assert not store.db.path.exists()


def test_initialize_rejects_a_symlinked_runtime_directory(tmp_path: Path) -> None:
    runtime = tmp_path / ".yipilot"
    runtime.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    _symlink_or_skip(runtime / "cache", outside, is_directory=True)

    with pytest.raises(DomainError, match="PATH_DENIED"):
        FileTaskStore(tmp_path).initialize_project()

    assert not (outside / "repos").exists()


def test_database_rejects_a_symlinked_record(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    original = store.db.path
    outside = tmp_path.parent / f"{tmp_path.name}-outside.db"
    original.replace(outside)
    _symlink_or_skip(original, outside, is_directory=False)
    with pytest.raises(DomainError, match="PATH_DENIED"):
        store.get_project()


def test_operation_key_is_idempotent_but_not_reusable_for_new_payload(
    tmp_path: Path,
) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")

    first = store.create_operation(
        "1001", "task_init", "key-a", {"task_id": "1001", "force": False}
    )

    assert (
        store.create_operation(
            "1001", "task_init", "key-a", {"force": False, "task_id": "1001"}
        )
        == first
    )
    with pytest.raises(DomainError, match="OPERATION_CONFLICT"):
        store.create_operation("1001", "task_init", "key-a", {"task_id": "1002"})


def test_task_cannot_read_another_tasks_operation_or_run(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    store.create_task("1002")
    operation = store.create_operation("1001", "task_init", "key-a", {"task_id": "1001"})
    run = store.create_run("1001", operation.operation_id, "task_init")

    with pytest.raises(DomainError, match="OPERATION_NOT_FOUND"):
        store.get_operation("1002", operation.operation_id)
    with pytest.raises(DomainError, match="RUN_NOT_FOUND"):
        store.get_run("1002", run.run_id)


def test_create_run_persists_a_pending_run(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "task_init", "key-a", {"task_id": "1001"})

    run = store.create_run("1001", operation.operation_id, "task_init")

    assert run.task_id == "1001"
    assert run.operation_id == operation.operation_id
    assert run.status == "pending"
    assert store.get_run("1001", run.run_id) == run


def test_failed_task_transaction_leaves_no_task_record(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    with pytest.raises(OSError), store.db.transaction():
        store.create_task("1001")
        raise OSError("disk full")
    assert store.list_tasks() == []


def test_operation_rejects_an_integer_outside_the_json_persistence_contract(
    tmp_path: Path,
) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operations_directory = tmp_path / ".yipilot" / "tasks" / "1001" / "operations"

    with pytest.raises(DomainError) as error:
        store.create_operation("1001", "task_init", "key-a", {"too_large": 10**5000})

    assert error.value.code == "OPERATION_CONFLICT"
    assert error.value.message == "operation payload must be JSON serializable"
    assert not list(operations_directory.glob("*.json"))
    assert not list(operations_directory.glob(".*.tmp"))


@given(
    st.dictionaries(
        st.text(alphabet=JSON_KEY_ALPHABET, min_size=1, max_size=8),
        JSON_SAFE_INTEGER,
        max_size=20,
    )
)
def test_canonical_request_is_identical_regardless_of_mapping_order(
    payload: dict[str, int]
) -> None:
    canonical = _canonical_request("task_init", payload)
    reordered = _canonical_request("task_init", dict(reversed(payload.items())))

    assert reordered == canonical
