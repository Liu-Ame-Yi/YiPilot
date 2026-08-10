import json
from pathlib import Path

import pytest

from yipilot.domain.errors import DomainError
from yipilot.harness.models import (
    HarnessCheck,
    HarnessGatePolicy,
    HarnessReport,
    aggregate_checks,
)
from yipilot.harness.report_store import HarnessReportStore
from yipilot.harness.task_integrity import TaskIntegrityHarness
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord


def _seed_run(
    project_root: Path,
    *,
    task_id: str = "1001",
    stage: str = "task_init",
) -> tuple[FileTaskStore, RunRecord]:
    store = FileTaskStore(project_root)
    store.initialize_project()
    store.create_task(task_id)
    operation = store.create_operation(
        task_id,
        stage,
        f"{stage}-{task_id}",
        {"task_id": task_id},
    )
    return store, store.create_run(task_id, operation.operation_id, stage)


def _check(report: HarnessReport, check_id: str) -> HarnessCheck:
    return next(check for check in report.checks if check.id == check_id)


def test_missing_task_record_is_blocking_failure(tmp_path: Path) -> None:
    report = TaskIntegrityHarness(tmp_path, "1001", "run-1").run()

    assert report.status == "failed"
    assert _check(report, "TASK-INTEGRITY-001").status == "failed"
    assert _check(report, "TASK-INTEGRITY-001").blocking is True
    assert _check(report, "TASK-INTEGRITY-002").status == "skipped"


def test_invalid_task_record_is_blocking_failure(tmp_path: Path) -> None:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    task_path = tmp_path / ".yipilot" / "tasks" / "1001" / "task.yaml"
    task_path.write_text("schema_version: nope\n", encoding="utf-8")

    report = TaskIntegrityHarness(tmp_path, "1001", "run-1").run()

    assert _check(report, "TASK-INTEGRITY-001").status == "failed"
    assert report.status == "failed"


def test_run_from_another_task_is_blocking_failure(tmp_path: Path) -> None:
    store, _ = _seed_run(tmp_path, task_id="1001")
    store.create_task("1002")
    operation = store.create_operation(
        "1002", "task_init", "task-init-1002", {"task_id": "1002"}
    )
    foreign_run = store.create_run("1002", operation.operation_id, "task_init")

    report = TaskIntegrityHarness(tmp_path, "1001", foreign_run.run_id).run()

    assert _check(report, "TASK-INTEGRITY-001").status == "passed"
    assert _check(report, "TASK-INTEGRITY-002").status == "failed"
    assert report.status == "failed"
    assert not (
        tmp_path
        / ".yipilot"
        / "tasks"
        / "1002"
        / "runs"
        / foreign_run.run_id
        / "harness"
    ).exists()


def test_event_sequence_gap_is_blocking_failure(tmp_path: Path) -> None:
    store, run = _seed_run(tmp_path)
    journal_path = store.event_journal_path(run.run_id)
    journal = JsonlEventJournal(
        journal_path,
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    )
    journal.append("run.started", {"message": "start"})
    journal.append("stage.progress", {"message": "work"})
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["sequence"] = 3
    journal_path.write_text(
        f"{lines[0]}\n{json.dumps(second, separators=(',', ':'))}\n",
        encoding="utf-8",
    )

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    assert _check(report, "TASK-INTEGRITY-003").status == "failed"
    assert _check(report, "TASK-INTEGRITY-004").status == "skipped"
    assert report.status == "failed"


@pytest.mark.parametrize("artifact_ref", ["../outside.txt", "C:/outside.txt"])
def test_artifact_reference_must_be_safe_and_run_relative(
    tmp_path: Path, artifact_ref: str
) -> None:
    store, run = _seed_run(tmp_path)
    JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    ).append("stage.progress", {"artifact_refs": [artifact_ref]})

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    assert _check(report, "TASK-INTEGRITY-003").status == "passed"
    assert _check(report, "TASK-INTEGRITY-004").status == "failed"
    assert report.status == "failed"


def test_registered_artifact_without_event_reference_is_checked(
    tmp_path: Path,
) -> None:
    store, run = _seed_run(tmp_path)
    run_root = store.event_journal_path(run.run_id).parent
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text("{}\n", encoding="utf-8")
    record = FileArtifactStore(store).register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="producer-input-v1",
    )

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    artifact_check = _check(report, "TASK-INTEGRITY-004")
    assert artifact_check.status == "passed"
    assert artifact_check.evidence_refs == [record.relative_path]
    assert report.status == "passed"


def test_registered_artifact_path_is_checked_without_event_artifact_refs(
    tmp_path: Path,
) -> None:
    store, run = _seed_run(tmp_path)
    run_root = store.event_journal_path(run.run_id).parent
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text("{}\n", encoding="utf-8")
    record = FileArtifactStore(store).register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="producer-input-v1",
    )
    record_path = (
        run_root / "artifacts" / "records" / f"{record.artifact_id}.json"
    )
    value = json.loads(record_path.read_text(encoding="utf-8"))
    value["relative_path"] = "../outside.txt"
    record_path.write_text(json.dumps(value), encoding="utf-8")

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    assert _check(report, "TASK-INTEGRITY-004").status == "failed"
    assert report.status == "failed"


def test_invalid_artifact_record_is_reported_and_persisted(
    tmp_path: Path,
) -> None:
    store, run = _seed_run(tmp_path)
    run_root = store.event_journal_path(run.run_id).parent
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text("{}\n", encoding="utf-8")
    record = FileArtifactStore(store).register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="producer-input-v1",
    )
    record_path = (
        run_root / "artifacts" / "records" / f"{record.artifact_id}.json"
    )
    record_path.write_text("{not-json\n", encoding="utf-8")

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    artifact_check = _check(report, "TASK-INTEGRITY-004")
    report_path = run_root / "harness" / "task-integrity-report.json"
    assert artifact_check.status == "failed"
    assert artifact_check.blocking is True
    assert "ARTIFACT_RECORD_INVALID" in artifact_check.message
    assert artifact_check.suggestion == (
        "repair or re-register the ArtifactRecord before continuing"
    )
    assert artifact_check.evidence_refs == ["artifacts/records"]
    assert str(tmp_path) not in artifact_check.message
    assert report.status == "failed"
    assert report.harness_version == "1.1.1"
    assert report_path.is_file()
    assert HarnessReportStore(store).load(run.run_id) == report


@pytest.mark.parametrize(
    ("association", "tampered_value"),
    [
        ("task_id", "1002"),
        ("run_id", "00000000-0000-0000-0000-000000000000"),
        ("operation_id", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_tampered_artifact_association_is_reported_and_persisted(
    tmp_path: Path,
    association: str,
    tampered_value: str,
) -> None:
    store, run = _seed_run(tmp_path)
    run_root = store.event_journal_path(run.run_id).parent
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text("{}\n", encoding="utf-8")
    record = FileArtifactStore(store).register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="producer-input-v1",
    )
    record_path = (
        run_root / "artifacts" / "records" / f"{record.artifact_id}.json"
    )
    value = json.loads(record_path.read_text(encoding="utf-8"))
    value[association] = tampered_value
    record_path.write_text(json.dumps(value), encoding="utf-8")

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    artifact_check = _check(report, "TASK-INTEGRITY-004")
    report_path = run_root / "harness" / "task-integrity-report.json"
    assert artifact_check.status == "failed"
    assert artifact_check.blocking is True
    assert "ARTIFACT_ASSOCIATION_MISMATCH" in artifact_check.message
    assert artifact_check.suggestion == (
        "repair or re-register the ArtifactRecord before continuing"
    )
    assert artifact_check.evidence_refs == ["artifacts/records"]
    assert str(tmp_path) not in artifact_check.message
    assert report.status == "failed"
    assert report_path.is_file()
    assert HarnessReportStore(store).load(run.run_id) == report


def test_unexpected_artifact_domain_error_is_reraised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, run = _seed_run(tmp_path)

    def raise_unexpected_artifact_failure(*_args: object) -> None:
        raise DomainError(
            "UNEXPECTED_ARTIFACT_FAILURE",
            "unexpected artifact failure",
        )

    monkeypatch.setattr(FileArtifactStore, "list", raise_unexpected_artifact_failure)

    with pytest.raises(DomainError) as raised:
        TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    assert raised.value.code == "UNEXPECTED_ARTIFACT_FAILURE"


def test_missing_artifact_record_during_list_is_reported_and_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, run = _seed_run(tmp_path)

    def raise_artifact_not_found(*_args: object) -> None:
        raise DomainError("ARTIFACT_NOT_FOUND", "artifact record not found")

    monkeypatch.setattr(FileArtifactStore, "list", raise_artifact_not_found)

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    artifact_check = _check(report, "TASK-INTEGRITY-004")
    assert artifact_check.status == "failed"
    assert artifact_check.blocking is True
    assert "ARTIFACT_NOT_FOUND" in artifact_check.message
    assert artifact_check.suggestion == (
        "repair or re-register the ArtifactRecord before continuing"
    )
    assert artifact_check.evidence_refs == ["artifacts/records"]
    assert str(tmp_path) not in artifact_check.message
    assert report.status == "failed"
    assert HarnessReportStore(store).load(run.run_id) == report


def test_report_fingerprint_contains_stable_artifact_record_summary(
    tmp_path: Path,
) -> None:
    store, run = _seed_run(tmp_path)
    run_root = store.event_journal_path(run.run_id).parent
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text("{}\n", encoding="utf-8")
    artifact_store = FileArtifactStore(store)
    record = artifact_store.register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="producer-input-v1",
    )

    first = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()
    replay = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()
    artifacts_input = next(
        item for item in first.inputs if item["kind"] == "artifacts"
    )

    assert first.harness_version == "1.1.1"
    assert first.input_fingerprint == replay.input_fingerprint
    assert artifacts_input["records"] == [record.model_dump(mode="json")]
    assert artifacts_input["event_refs"] == []

    second_path = run_root / "git" / "diff.patch"
    second_path.parent.mkdir()
    second_path.write_text("diff --git a/a b/a\n", encoding="utf-8")
    artifact_store.register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="git/diff.patch",
        input_fingerprint="producer-input-v2",
    )
    changed = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    assert changed.input_fingerprint != first.input_fingerprint


def test_legacy_run_without_artifact_records_has_zero_artifacts(
    tmp_path: Path,
) -> None:
    _store, run = _seed_run(tmp_path)

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()
    artifacts_input = next(
        item for item in report.inputs if item["kind"] == "artifacts"
    )

    assert _check(report, "TASK-INTEGRITY-004").status == "passed"
    assert artifacts_input["records"] == []
    assert artifacts_input["event_refs"] == []


def test_task_integrity_004_does_not_add_file_existence_or_hash_semantics(
    tmp_path: Path,
) -> None:
    store, run = _seed_run(tmp_path)
    run_root = store.event_journal_path(run.run_id).parent
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text("original\n", encoding="utf-8")
    FileArtifactStore(store).register(
        run.task_id,
        run.run_id,
        producer="unit-test",
        relative_path="artifacts/result.json",
        input_fingerprint="producer-input-v1",
    )
    artifact_path.unlink()
    JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    ).append("stage.progress", {"artifact_refs": ["artifacts/missing.json"]})

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()

    assert _check(report, "TASK-INTEGRITY-004").status == "passed"
    assert [check.id for check in report.checks] == [
        "TASK-INTEGRITY-001",
        "TASK-INTEGRITY-002",
        "TASK-INTEGRITY-003",
        "TASK-INTEGRITY-004",
    ]


def test_aggregation_uses_failed_skipped_warning_passed_priority() -> None:
    checks = [
        HarnessCheck(id="PASS", status="passed", blocking=True),
        HarnessCheck(id="WARN", status="warning", blocking=True),
        HarnessCheck(id="SKIP", status="skipped", blocking=True),
        HarnessCheck(id="FAIL", status="failed", blocking=True),
    ]

    assert aggregate_checks(checks).status == "failed"
    assert aggregate_checks(checks[:-1]).status == "skipped"
    assert aggregate_checks(checks[:-2]).status == "warning"
    assert aggregate_checks(checks[:1]).status == "passed"


def test_blocking_skipped_is_not_success() -> None:
    result = aggregate_checks(
        [HarnessCheck(id="SKIPPED", status="skipped", blocking=True)]
    )

    assert result.status == "skipped"
    assert HarnessGatePolicy().accepts(result.status) is False
    assert HarnessGatePolicy(allow_warning=True).accepts(result.status) is False


def test_gate_accepts_only_passed_and_explicitly_allowed_warning() -> None:
    strict = HarnessGatePolicy()
    warning_allowed = HarnessGatePolicy(allow_warning=True)

    assert strict.accepts("passed") is True
    assert strict.accepts("warning") is False
    assert warning_allowed.accepts("warning") is True
    assert warning_allowed.accepts("failed") is False
    assert warning_allowed.accepts("skipped") is False


def test_report_is_atomically_saved_and_validates_after_read(
    tmp_path: Path,
) -> None:
    store, run = _seed_run(tmp_path)
    run_root = store.event_journal_path(run.run_id).parent
    artifact_path = run_root / "artifacts" / "result.json"
    artifact_path.parent.mkdir()
    artifact_path.write_text("{}\n", encoding="utf-8")
    JsonlEventJournal(
        store.event_journal_path(run.run_id),
        task_id=run.task_id,
        run_id=run.run_id,
        stage=run.stage,
    ).append("stage.progress", {"artifact_refs": ["artifacts/result.json"]})

    report = TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()
    report_path = (
        run_root / "harness" / "task-integrity-report.json"
    )
    persisted = HarnessReport.model_validate_json(
        report_path.read_text(encoding="utf-8")
    )
    loaded = HarnessReportStore(store).load(run.run_id)

    assert report.status == "passed"
    assert [check.id for check in report.checks] == [
        "TASK-INTEGRITY-001",
        "TASK-INTEGRITY-002",
        "TASK-INTEGRITY-003",
        "TASK-INTEGRITY-004",
    ]
    assert report.operation_id == run.operation_id
    assert report.stage == run.stage
    assert len(report.input_fingerprint) == 64
    assert persisted == report
    assert loaded == report
    assert not list(report_path.parent.glob("*.tmp"))


def test_report_read_rejects_mismatched_run_association(tmp_path: Path) -> None:
    store, run = _seed_run(tmp_path)
    TaskIntegrityHarness(tmp_path, run.task_id, run.run_id).run()
    report_path = HarnessReportStore(store).report_path(run.run_id)
    value = json.loads(report_path.read_text(encoding="utf-8"))
    value["task_id"] = "1002"
    report_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(DomainError, match="HARNESS_REPORT_MISMATCH"):
        HarnessReportStore(store).load(run.run_id)
