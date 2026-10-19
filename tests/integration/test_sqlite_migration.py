"""Real sqlite3 connections and orchestration, with deterministic crash boundaries."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ApprovalRecord, ToolChangeSummary, ToolResult
from yipilot.persistence.approval_store import FileApprovalStore
from yipilot.persistence.artifact_store import (
    FileArtifactStore,
    artifact_reference,
    audit_artifacts,
)
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.migration import migrate, scan
from yipilot.persistence.ownership import project_lock
from yipilot.persistence.task_store import FileTaskStore, RepoMetadataRecord
from yipilot.persistence.tool_run_store import FileToolRunStore
from yipilot.workflow.recovery_service import RecoveryService


def seed(root):
    root.mkdir(exist_ok=True)
    store = FileTaskStore(root)
    store.initialize_project()
    store.create_task("1")
    operation = store.create_operation("1", "check", "key", {"input": "fixed"})
    run = store.create_run("1", operation.operation_id, "check")
    return store, run


def legacy(root):
    store, run = seed(root)
    store.set_run_status(run.run_id, "running")
    artifact = FileArtifactStore(store).write_bytes("1", run.run_id, producer="fixture",
        relative_path="artifacts/report.txt", content=b"verified\n", input_fingerprint="fixed")
    FileApprovalStore(store).save(ApprovalRecord(approval_id="approved", task_id="1",
        operation_id=run.operation_id, run_id=run.run_id, action_type="sandbox_execution",
        target_fingerprint="a" * 64, policy_revision="v1", decision="approved", decided_at=datetime.now(UTC)))
    store.set_run_status(run.run_id, "succeeded")
    runtime = root / ".yipilot"
    run_root = store.event_journal_path(run.run_id).parent
    with store.db.transaction() as connection:
        for table, path in (("project", runtime / "config.yaml"), ("tasks", runtime / "tasks/1/task.yaml"),
                            ("operations", runtime / f"tasks/1/operations/{run.operation_id}.json"),
                            ("runs", run_root / "run.json")):
            value = json.loads(connection.execute(f"SELECT body FROM {table}").fetchone()[0])
            value.pop("legacy", None)
            value.pop("result_artifact_ids", None)
            path.write_text(json.dumps(value), encoding="utf-8")
        for kind, identifier, body in connection.execute("SELECT kind,id,body FROM records"):
            value = json.loads(body)
            value.pop("revoked", None)
            path = run_root / ("artifacts/records" if kind == "artifact" else "approvals") / f"{identifier}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")
        events = [json.loads(row[0]) for row in connection.execute("SELECT body FROM events ORDER BY cursor")]
        for event in events:
            event.pop("cursor", None)
        (run_root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    store.db.path.unlink()
    return run, artifact


def test_independent_connections_reserve_one_request_and_conflict(tmp_path):
    seed(tmp_path)
    barrier = Barrier(2)
    def reserve(_):
        store = FileTaskStore(tmp_path)
        barrier.wait()
        return store.create_operation("1", "check", "race", {"input": "same"})
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(reserve, range(2))
    assert a == b
    with pytest.raises(DomainError, match="OPERATION_CONFLICT"):
        FileTaskStore(tmp_path).create_operation("1", "check", "race", {"input": "changed"})
    with FileTaskStore(tmp_path).db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM operations WHERE request_key='race'").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_state_event_and_result_reference_rollback(tmp_path, monkeypatch):
    store, run = seed(tmp_path)
    store.set_run_status(run.run_id, "running")
    original = JsonlEventJournal.append
    def fail(journal, event_type, *args, **kwargs):
        result = original(journal, event_type, *args, **kwargs)
        if event_type == "run.succeeded":
            raise OSError("before commit")
        return result
    monkeypatch.setattr(JsonlEventJournal, "append", fail)
    with pytest.raises(OSError, match="before commit"):
        store.set_run_status(run.run_id, "succeeded")
    assert store.find_run(run.run_id).status == "running"
    assert [e.type for e in JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)] == ["run.started"]


def test_published_file_is_an_orphan_until_database_commit(tmp_path, monkeypatch):
    store, run = seed(tmp_path)
    artifacts = FileArtifactStore(store)
    def fail(*args, **kwargs):
        raise OSError("published before record")
    monkeypatch.setattr(store.db, "save_record", fail)
    with pytest.raises(OSError):
        artifacts.write_bytes("1", run.run_id, producer="test", relative_path="artifacts/result.txt",
            content=b"complete file", input_fingerprint="fixed")
    assert (store.event_journal_path(run.run_id).parent / "artifacts/result.txt").read_bytes() == b"complete file"
    assert artifacts.list("1", run.run_id) == []
    assert store.find_run(run.run_id).status == "pending"
    assert audit_artifacts(store)["orphans"] == [f"{run.run_id}/artifacts/result.txt"]


@pytest.mark.parametrize("damage", ["missing", "hash"])
def test_damaged_evidence_cannot_commit_success(tmp_path, damage):
    store, run = seed(tmp_path)
    record = FileArtifactStore(store).write_bytes("1", run.run_id, producer="test",
        relative_path="artifacts/result.txt", content=b"ok", input_fingerprint="fixed")
    path = store.event_journal_path(run.run_id).parent / record.relative_path
    path.unlink() if damage == "missing" else path.write_bytes(b"tampered")
    store.set_run_status(run.run_id, "running")
    with pytest.raises(DomainError, match="ARTIFACT_STALE"):
        store.set_run_status(run.run_id, "succeeded")
    assert store.find_run(run.run_id).status == "running"


def test_restart_and_repeated_terminal_subscription(tmp_path):
    store, run = seed(tmp_path)
    store.set_run_status(run.run_id, "running")
    store.set_run_status(run.run_id, "succeeded")
    restarted = FileTaskStore(tmp_path)
    journal = JsonlEventJournal(restarted.event_journal_path(run.run_id))
    events = journal.read_after(0)
    assert [e.type for e in events] == ["run.started", "run.succeeded"]
    assert [e.cursor for e in events] == sorted(e.cursor for e in events)
    assert journal.append("run.succeeded", {}) == events[-1]
    assert journal.read_after(events[-1].sequence) == []


def test_import_backup_replay_and_repeat_do_not_overwrite_new_state(tmp_path):
    run, artifact = legacy(tmp_path)
    before = scan(tmp_path)
    assert before["errors"] == []
    assert before["counts"] == {"project": 1, "task": 1, "operation": 1, "run": 1, "artifact": 1, "approval": 1, "event": 2}
    report = migrate(tmp_path, executors_stopped=True)
    store = FileTaskStore(tmp_path)
    assert store.find_run(run.run_id).legacy
    assert FileArtifactStore(store).verify("1", run.run_id, artifact.artifact_id) == artifact
    approval = FileApprovalStore(store).get("1", run.run_id, "approved")
    with pytest.raises(DomainError, match="APPROVAL_STALE"):
        approval.require_fresh(task_id="1", operation_id=run.operation_id, run_id=run.run_id,
            action_type=approval.action_type, target_fingerprint=approval.target_fingerprint, policy_revision=approval.policy_revision)
    assert Path(report["backup"]).is_dir()
    store.create_task("2")
    assert migrate(tmp_path, executors_stopped=True)["already_migrated"]
    assert store.get_task("2")
    assert (tmp_path / ".yipilot/config.yaml").is_file()


@pytest.mark.parametrize("damage", ["unknown", "missing", "hash", "events", "terminal", "request"])
def test_read_only_scan_reports_corruption_and_never_switches(tmp_path, damage):
    run, artifact = legacy(tmp_path)
    root = tmp_path / f".yipilot/tasks/1/runs/{run.run_id}"
    if damage == "unknown":
        path = root / "run.json"
        value = json.loads(path.read_text())
        value["unknown"] = True
        path.write_text(json.dumps(value))
    elif damage == "events":
        (root / "events.jsonl").write_text("bad\n")
    elif damage == "terminal":
        path = root / "events.jsonl"
        path.write_text(path.read_text().replace("run.succeeded", "run.failed"))
    elif damage == "request":
        path = root.parent.parent / f"operations/{run.operation_id}.json"
        value = json.loads(path.read_text())
        value["request_json"] = "null"
        path.write_text(json.dumps(value))
    elif damage == "missing":
        (root / artifact.relative_path).unlink()
    else:
        (root / artifact.relative_path).write_bytes(b"changed")
    assert scan(tmp_path)["errors"]
    with pytest.raises(DomainError, match="MIGRATION_INVALID"):
        migrate(tmp_path, executors_stopped=True)
    assert not (tmp_path / ".yipilot/state.db").exists()


def test_switch_failure_preserves_source_and_can_retry(tmp_path, monkeypatch):
    legacy(tmp_path)
    from yipilot.persistence import migration
    original = migration._switch
    def fail(*args):
        raise OSError("switch denied")
    monkeypatch.setattr(migration, "_switch", fail)
    with pytest.raises(DomainError, match="MIGRATION_FAILED"):
        migrate(tmp_path, executors_stopped=True)
    assert not (tmp_path / ".yipilot/state.db").exists()
    assert scan(tmp_path)["errors"] == []
    monkeypatch.setattr(migration, "_switch", original)
    migrate(tmp_path, executors_stopped=True)


def test_live_owner_blocks_recovery_without_time_based_takeover(tmp_path):
    store, run = seed(tmp_path)
    store.set_run_status(run.run_id, "running")
    with project_lock(tmp_path, "executor"), ThreadPoolExecutor(1) as pool, pytest.raises(DomainError, match="PROJECT_BUSY"):
        pool.submit(RecoveryService(FileTaskStore(tmp_path)).recover, run.run_id).result()
    recovered = RecoveryService(store).recover(run.run_id)
    assert recovered.error_code == "EXTERNAL_RESULT_UNCERTAIN"
    assert recovered.status == "failed"


def test_legacy_project_requires_explicit_migration(tmp_path):
    legacy(tmp_path)
    with pytest.raises(DomainError, match="MIGRATION_REQUIRED"):
        FileTaskStore(tmp_path).initialize_project()
    with pytest.raises(DomainError, match="MIGRATION_STOP_REQUIRED"):
        migrate(tmp_path)


def test_import_includes_repo_tool_result_and_all_references(tmp_path):
    run, artifact = legacy(tmp_path)
    root = tmp_path / ".yipilot"
    repo = RepoMetadataRecord(task_id="1", alias="repo", normalized_url="file:///fixture",
        url_hash="a" * 64, cache_path=".yipilot/cache/repos/fixture", head_sha="b" * 40,
        synced_at=datetime.now(UTC).isoformat())
    path = root / "tasks/1/repos/repo/metadata.json"
    path.parent.mkdir(parents=True)
    path.write_text(repo.model_dump_json(), encoding="utf-8")
    now = datetime.now(UTC)
    result = ToolResult(tool_run_id="tool-1", task_id="1", operation_id=run.operation_id,
        run_id=run.run_id, request_fingerprint="a" * 64, argv=("fixture",), status="succeeded",
        started_at=now, ended_at=now, exit_code=0, stdout_artifact_ref=artifact_reference(artifact),
        stderr_artifact_ref=artifact_reference(artifact), change_summary=ToolChangeSummary(changed_file_count=0),
        trust_level="sandboxed")
    path = root / f"tasks/1/runs/{run.run_id}/tool-runs/tool-1.json"
    path.parent.mkdir()
    path.write_text(result.model_dump_json(), encoding="utf-8")
    assert scan(tmp_path)["errors"] == []
    migrate(tmp_path, executors_stopped=True)
    store = FileTaskStore(tmp_path)
    assert FileToolRunStore(store).get("1", run.run_id, "tool-1") == result
    assert RepoMetadataRecord.model_validate_json(store.db.record("repo", "1:repo", "missing")) == repo


def test_migration_rejects_linked_legacy_lock_before_opening_it(tmp_path):
    legacy(tmp_path)
    original = tmp_path / "preserved.txt"
    original.write_bytes(b"must not truncate")
    os.link(original, tmp_path / ".yipilot/old.lock")
    with pytest.raises(DomainError, match="PATH_DENIED"):
        migrate(tmp_path, executors_stopped=True)
    assert original.read_bytes() == b"must not truncate"
    assert not (tmp_path / ".yipilot/state.db").exists()


def test_pending_legacy_run_gets_terminal_replay_without_execution(tmp_path):
    run, _ = legacy(tmp_path)
    root = tmp_path / f".yipilot/tasks/1/runs/{run.run_id}"
    value = json.loads((root / "run.json").read_text())
    value["status"] = "pending"
    (root / "run.json").write_text(json.dumps(value))
    (root / "events.jsonl").write_text("")
    migrate(tmp_path, executors_stopped=True)
    store = FileTaskStore(tmp_path)
    recovered = store.find_run(run.run_id)
    assert recovered.status == "cancelled" and recovered.error_code == "LEGACY_INTERRUPTED"
    assert JsonlEventJournal(store.event_journal_path(run.run_id)).read_after(0)[-1].type == "run.cancelled"


def test_success_requires_registered_evidence_even_when_file_exists(tmp_path):
    store, run = seed(tmp_path)
    proposal = store.create_run("1", run.operation_id, "code_generate")
    path = store.event_journal_path(proposal.run_id).parent / "artifacts/patch-proposal.json"
    path.parent.mkdir()
    path.write_bytes(b"{}")
    store.set_run_status(proposal.run_id, "running")
    with pytest.raises(DomainError, match="RUN_EVIDENCE_MISSING"):
        store.set_run_status(proposal.run_id, "succeeded")


def test_nested_transaction_failure_can_be_caught_without_partial_commit(tmp_path):
    store, _ = seed(tmp_path)
    with store.db.transaction():
        store.create_task("2")
        with pytest.raises(RuntimeError), store.db.transaction():
            store.create_task("3")
            raise RuntimeError("abort nested unit")
    assert [task.task_id for task in store.list_tasks()] == ["1", "2"]


def test_schema_upgrade_rolls_back_and_unknown_version_is_rejected(tmp_path, monkeypatch):
    from yipilot.persistence import database
    (tmp_path / ".yipilot").mkdir()
    original = database._SCHEMA
    monkeypatch.setattr(database, "_SCHEMA", (*original, "INVALID SQL"))
    with pytest.raises(Exception, match="syntax"):
        database.Database(tmp_path).initialize()
    monkeypatch.setattr(database, "_SCHEMA", original)
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    with store.db.transaction() as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(DomainError, match="STORAGE_VERSION"):
        store.get_project()


def test_migration_refuses_an_active_executor_and_dry_run_does_not_write(tmp_path):
    legacy(tmp_path)
    before = sorted((p.relative_to(tmp_path), p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())
    assert scan(tmp_path)["errors"] == []
    after = sorted((p.relative_to(tmp_path), p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())
    assert before == after
    with project_lock(tmp_path, "executor"), ThreadPoolExecutor(1) as pool, pytest.raises(DomainError, match="PROJECT_BUSY"):
        pool.submit(migrate, tmp_path, executors_stopped=True).result()
    assert not (tmp_path / ".yipilot/state.db").exists()
