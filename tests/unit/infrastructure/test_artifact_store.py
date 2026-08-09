import hashlib
import json
from pathlib import Path

import pytest

from yipilot.domain.errors import DomainError
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord


def _seed_run(project_root: Path) -> tuple[FileTaskStore, RunRecord, Path]:
    task_store = FileTaskStore(project_root)
    task_store.initialize_project()
    task_store.create_task("1001")
    operation = task_store.create_operation(
        "1001", "task_init", "task-init-1001", {"task_id": "1001"}
    )
    run = task_store.create_run("1001", operation.operation_id, "task_init")
    return task_store, run, task_store.event_journal_path(run.run_id).parent


def test_register_atomically_saves_and_reads_artifact_record(tmp_path: Path) -> None:
    task_store, run, run_root = _seed_run(tmp_path)
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text('{"ok":true}\n', encoding="utf-8")
    expected_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    store = FileArtifactStore(task_store)

    record = store.register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="input-v1",
    )

    record_path = (
        run_root / "artifacts" / "records" / f"{record.artifact_id}.json"
    )
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    assert persisted == record.model_dump(mode="json")
    assert store.get(run.task_id, run.run_id, record.artifact_id) == record
    assert record.operation_id == run.operation_id
    assert record.relative_path == "artifacts/result.json"
    assert record.sha256 == expected_sha256
    assert not list(record_path.parent.glob("*.tmp"))


def test_list_returns_records_in_stable_order(tmp_path: Path) -> None:
    task_store, run, run_root = _seed_run(tmp_path)
    store = FileArtifactStore(task_store)
    registered = []
    for name in ("zeta.txt", "alpha.txt", "middle.txt"):
        path = run_root / "artifacts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(name, encoding="utf-8")
        registered.append(
            store.register(
                run.task_id,
                run.run_id,
                producer="unit-test",
                relative_path=f"artifacts/{name}",
                input_fingerprint=f"input-{name}",
            )
        )

    first = store.list(run.task_id, run.run_id)
    second = store.list(run.task_id, run.run_id)

    assert first == second
    assert first == sorted(registered, key=lambda item: (item.created_at, item.artifact_id))


def test_register_rejects_cross_task_run_and_operation_associations(
    tmp_path: Path,
) -> None:
    task_store, first_run, first_root = _seed_run(tmp_path)
    task_store.create_task("1002")
    second_operation = task_store.create_operation(
        "1002", "task_init", "task-init-1002", {"task_id": "1002"}
    )
    second_run = task_store.create_run(
        "1002", second_operation.operation_id, "task_init"
    )
    second_root = task_store.event_journal_path(second_run.run_id).parent
    for root in (first_root, second_root):
        path = root / "artifacts" / "result.json"
        path.parent.mkdir()
        path.write_text("{}\n", encoding="utf-8")
    store = FileArtifactStore(task_store)

    with pytest.raises(DomainError, match="RUN_NOT_FOUND"):
        store.register(
            "1002",
            first_run.run_id,
            producer="unit-test",
            relative_path="artifacts/result.json",
            input_fingerprint="input-v1",
        )
    with pytest.raises(DomainError, match="ARTIFACT_ASSOCIATION_MISMATCH"):
        store.register(
            second_run.task_id,
            second_run.run_id,
            producer="unit-test",
            relative_path="artifacts/result.json",
            input_fingerprint="input-v1",
            operation_id=first_run.operation_id,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.txt",
        r"..\outside.txt",
        "/outside.txt",
        "C:outside.txt",
        r"C:\outside.txt",
    ],
)
def test_register_rejects_unsafe_relative_paths(
    tmp_path: Path, relative_path: str
) -> None:
    task_store, run, _ = _seed_run(tmp_path)

    with pytest.raises(DomainError, match="PATH_DENIED"):
        FileArtifactStore(task_store).register(
            run.task_id,
            run.run_id,
            producer="unit-test",
            relative_path=relative_path,
            input_fingerprint="input-v1",
        )


def test_register_rejects_an_absolute_path(tmp_path: Path) -> None:
    task_store, run, _ = _seed_run(tmp_path)
    outside = (tmp_path.parent / f"{tmp_path.name}-outside.txt").resolve()
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(DomainError, match="PATH_DENIED"):
        FileArtifactStore(task_store).register(
            run.task_id,
            run.run_id,
            producer="unit-test",
            relative_path=str(outside),
            input_fingerprint="input-v1",
        )


def test_register_rejects_a_symlink_escape(tmp_path: Path) -> None:
    task_store, run, run_root = _seed_run(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-artifact.txt"
    outside.write_text("outside", encoding="utf-8")
    artifacts = run_root / "artifacts"
    artifacts.mkdir()
    try:
        (artifacts / "result.txt").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    with pytest.raises(DomainError, match="PATH_DENIED"):
        FileArtifactStore(task_store).register(
            run.task_id,
            run.run_id,
            producer="unit-test",
            relative_path="artifacts/result.txt",
            input_fingerprint="input-v1",
        )


@pytest.mark.parametrize("association", ["task_id", "run_id", "operation_id"])
def test_get_rejects_schema_valid_but_tampered_associations(
    tmp_path: Path, association: str
) -> None:
    task_store, run, run_root = _seed_run(tmp_path)
    path = run_root / "artifacts" / "result.json"
    path.parent.mkdir()
    path.write_text("{}\n", encoding="utf-8")
    store = FileArtifactStore(task_store)
    record = store.register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="input-v1",
    )
    record_path = (
        run_root / "artifacts" / "records" / f"{record.artifact_id}.json"
    )
    value = json.loads(record_path.read_text(encoding="utf-8"))
    value[association] = {
        "task_id": "1002",
        "run_id": "00000000-0000-0000-0000-000000000000",
        "operation_id": "00000000-0000-0000-0000-000000000000",
    }[association]
    record_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(DomainError, match="ARTIFACT_ASSOCIATION_MISMATCH"):
        store.get(run.task_id, run.run_id, record.artifact_id)


def test_get_rejects_a_schema_valid_but_unsafe_tampered_path(
    tmp_path: Path,
) -> None:
    task_store, run, run_root = _seed_run(tmp_path)
    path = run_root / "artifacts" / "result.json"
    path.parent.mkdir()
    path.write_text("{}\n", encoding="utf-8")
    store = FileArtifactStore(task_store)
    record = store.register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="input-v1",
    )
    record_path = (
        run_root / "artifacts" / "records" / f"{record.artifact_id}.json"
    )
    value = json.loads(record_path.read_text(encoding="utf-8"))
    value["relative_path"] = "C:outside.txt"
    record_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(DomainError, match="PATH_DENIED"):
        store.get(run.task_id, run.run_id, record.artifact_id)
