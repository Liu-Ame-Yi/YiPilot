from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from yipilot.domain.errors import DomainError
from yipilot.infrastructure.task_store import (
    FileTaskStore,
    OperationRecord,
    _canonical_request,
)

JSON_SAFE_INTEGER = st.integers(min_value=-(2**63), max_value=2**63 - 1)


def _symlink_or_skip(link: Path, target: Path, *, is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")


def test_initialize_creates_runtime_layout_and_persists_project(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)

    project = store.initialize_project()

    assert project.schema_version == 1
    assert (tmp_path / ".yipilot" / "config.yaml").is_file()
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


def test_failed_project_write_leaves_no_partial_runtime_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileTaskStore(tmp_path)

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("yipilot.infrastructure.task_store.atomic_write_yaml", fail_write)

    with pytest.raises(OSError, match="disk full"):
        store.initialize_project()

    assert not (tmp_path / ".yipilot").exists()


def test_initialize_rejects_a_symlinked_runtime_directory(tmp_path: Path) -> None:
    runtime = tmp_path / ".yipilot"
    runtime.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    _symlink_or_skip(runtime / "cache", outside, is_directory=True)

    with pytest.raises(DomainError, match="PATH_DENIED"):
        FileTaskStore(tmp_path).initialize_project()

    assert not (outside / "repos").exists()


def test_operation_scan_rejects_a_symlinked_record(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    foreign_operation_id = str(uuid4())
    outside = tmp_path.parent / f"{tmp_path.name}-outside-operation.json"
    foreign = OperationRecord(
        operation_id=foreign_operation_id,
        task_id="1001",
        operation_type="task_init",
        idempotency_key="key-a",
        request_json='{"operation_type":"task_init","payload":{}}',
        created_at="2026-08-08T00:00:00+00:00",
    )
    outside.write_text(foreign.model_dump_json(), encoding="utf-8")
    _symlink_or_skip(
        tmp_path / ".yipilot" / "tasks" / "1001" / "operations" / f"{foreign_operation_id}.json",
        outside,
        is_directory=False,
    )

    with pytest.raises(DomainError, match="PATH_DENIED"):
        store.create_operation("1001", "task_init", "key-a", {})


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


def test_failed_task_write_leaves_no_task_directory_or_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("yipilot.infrastructure.task_store.atomic_write_yaml", fail_write)

    with pytest.raises(OSError, match="disk full"):
        store.create_task("1001")

    assert not (tmp_path / ".yipilot" / "tasks" / "1001").exists()
    assert not list((tmp_path / ".yipilot" / "tasks").glob("*.tmp"))


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
        st.text(min_size=1, max_size=8), JSON_SAFE_INTEGER, max_size=20
    )
)
def test_canonical_request_is_identical_regardless_of_mapping_order(
    payload: dict[str, int]
) -> None:
    canonical = _canonical_request("task_init", payload)
    reordered = _canonical_request("task_init", dict(reversed(payload.items())))

    assert reordered == canonical
