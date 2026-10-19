"""Explicit, fail-closed import of legacy metadata; never invoked by startup."""

import hashlib
import json
import os
import shutil
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any
from uuid import UUID

import yaml  # type: ignore[import-untyped]
from filelock import FileLock, Timeout
from pydantic import BaseModel

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ApprovalRecord, ToolResult
from yipilot.domain.models import validate_task_id
from yipilot.persistence.artifact_store import (
    ArtifactRecord,
    FileArtifactStore,
    artifact_path,
)
from yipilot.persistence.database import Database
from yipilot.persistence.event_journal import (
    EVENT_TYPES,
    EventRecord,
    JsonlEventJournal,
)
from yipilot.persistence.filesystem import atomic_write_bytes, project_path
from yipilot.persistence.ownership import project_lock
from yipilot.persistence.task_store import (
    FileTaskStore,
    OperationRecord,
    ProjectRecord,
    RepoMetadataRecord,
    RunRecord,
    TaskRecord,
)
from yipilot.persistence.tool_run_store import FileToolRunStore
from yipilot.security.paths import require_plain_path


def _manifest(root: Path) -> dict[str, str]:
    files = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        require_plain_path(directory)
        for path in sorted(directory.iterdir()):
            require_plain_path(path)
            if path.is_dir():
                pending.append(path)
            elif path.is_file() and not path.name.endswith(".lock"):
                with path.open("rb") as handle:
                    files[path.relative_to(root).as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    return files


def _kind(path: str) -> tuple[str, type[BaseModel]] | None:
    parts = Path(path).parts
    if path == "config.yaml":
        return "project", ProjectRecord
    if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "task.yaml":
        return "task", TaskRecord
    if len(parts) == 4 and parts[2] == "operations" and path.endswith(".json"):
        return "operation", OperationRecord
    if len(parts) == 5 and parts[2] == "repos" and parts[4] == "metadata.json":
        return "repo", RepoMetadataRecord
    if len(parts) >= 5 and parts[2] == "runs":
        if len(parts) == 5 and parts[4] == "run.json":
            return "run", RunRecord
        if len(parts) == 5 and parts[4] == "events.jsonl":
            return "event", EventRecord
        if len(parts) == 6 and parts[4] in {"approvals", "tool-runs"} and path.endswith(".json"):
            return ("approval", ApprovalRecord) if parts[4] == "approvals" else ("tool", ToolResult)
        if len(parts) == 7 and parts[4:6] == ("artifacts", "records") and path.endswith(".json"):
            return "artifact", ArtifactRecord
    return None


def scan(project: Path) -> dict[str, Any]:
    """Read-only scan, including foreign keys, schema, event order and content hashes."""
    root = project_path(project, ".yipilot")
    report: dict[str, Any] = {"format": "legacy-v1", "counts": {}, "errors": [], "warnings": [], "records": []}
    if (root / "state.db").exists():
        report["format"] = "sqlite"
        return report
    try:
        manifest = _manifest(root)
    except (DomainError, OSError) as error:
        report["errors"].append(str(error))
        return report
    report["manifest"] = manifest
    report["source_hash"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if "config.yaml" not in manifest:
        report["errors"].append("config.yaml: missing project record")
    for relative in manifest:
        classified = _kind(relative)
        if classified is None:
            continue  # Opaque execution resources and artifact bodies are retained byte-for-byte.
        kind, model = classified
        path = project_path(root, relative)
        try:
            text = path.read_text(encoding="utf-8")
            values = [json.loads(line) for line in text.splitlines()] if kind == "event" else [yaml.safe_load(text) if relative.endswith(".yaml") else json.loads(text)]
            if kind == "event" and text and not text.endswith("\n"):
                raise ValueError("damaged event tail")
            for sequence, value in enumerate(values, 1):
                unknown = set(value) - model.model_fields.keys()
                if unknown:
                    raise ValueError(f"unknown fields: {sorted(unknown)}")
                record = model.model_validate(value)
                body = record.model_dump(mode="json")
                if kind == "operation":
                    request = json.loads(body["request_json"])
                    if not isinstance(request, dict) or set(request) != {"operation_type", "payload"} or request["operation_type"] != body["operation_type"] or not isinstance(request["payload"], dict):
                        raise ValueError("invalid frozen request")
                if kind != "project":
                    validate_task_id(body["task_id"])
                for field in ("run_id", "operation_id"):
                    if field in body:
                        UUID(body[field])
                for field in ("created_at", "synced_at", "timestamp"):
                    if field in body:
                        datetime.fromisoformat(body[field])
                parts = Path(relative).parts
                identifier_field = {"operation": "operation_id", "approval": "approval_id", "tool": "tool_run_id", "artifact": "artifact_id"}.get(kind)
                if identifier_field and body[identifier_field] != path.stem:
                    raise ValueError("record ID differs from filename")
                if kind == "repo" and body["alias"] != parts[3]:
                    raise ValueError("repository alias differs from path")
                if kind != "project" and body.get("task_id") != parts[1]:
                    raise ValueError("task association differs from path")
                if kind in {"run", "event", "artifact", "approval", "tool"} and body["run_id"] != parts[3]:
                    raise ValueError("run association differs from path")
                if kind == "event" and (body["sequence"] != sequence or body["type"] not in EVENT_TYPES):
                    raise ValueError("invalid event type or sequence")
                if kind == "artifact":
                    artifact = artifact_path(root / "tasks" / body["task_id"] / "runs" / body["run_id"], body["relative_path"])
                    if hashlib.sha256(artifact.read_bytes()).hexdigest() != body["sha256"]:
                        raise ValueError("artifact hash mismatch")
                report["records"].append((kind, relative, body))
        except (ValueError, TypeError, OSError, DomainError, yaml.YAMLError) as error:
            report["errors"].append(f"{relative}: {error}")
    records = report["records"]
    tasks = {v["task_id"] for k, _, v in records if k == "task"}
    operations = {v["operation_id"]: v for k, _, v in records if k == "operation"}
    runs = {v["run_id"]: v for k, _, v in records if k == "run"}
    for kind, relative, value in records:
        if kind != "project" and value["task_id"] not in tasks:
            report["errors"].append(f"{relative}: missing task")
        if kind in {"run", "approval", "tool", "artifact"}:
            operation = operations.get(value["operation_id"], {})
            if operation.get("task_id") != value["task_id"]:
                report["errors"].append(f"{relative}: missing or mismatched operation")
        if kind in {"event", "approval", "tool", "artifact"}:
            run = runs.get(value["run_id"], {})
            if run.get("task_id") != value["task_id"] or kind != "event" and run.get("operation_id") != value["operation_id"]:
                report["errors"].append(f"{relative}: missing or mismatched run")
        if kind == "run" and value["status"] in {"pending", "running", "paused"}:
            report["warnings"].append(f"{relative}: import as cancelled/LEGACY_INTERRUPTED; never resume")
        if kind == "approval":
            report["warnings"].append(f"{relative}: retained for history, revoked for execution")
    seen: set[tuple[str, str]] = set()
    requests: set[tuple[str, str]] = set()
    for kind, relative, value in records:
        identity_field = {"task": "task_id", "operation": "operation_id", "run": "run_id", "event": "event_id", "approval": "approval_id", "tool": "tool_run_id", "artifact": "artifact_id"}.get(kind)
        if identity_field:
            identity = (kind, value[identity_field])
            if identity in seen:
                report["errors"].append(f"{relative}: duplicate {identity_field}")
            seen.add(identity)
        if kind == "operation":
            request = (value["task_id"], value["idempotency_key"])
            if request in requests:
                report["errors"].append(f"{relative}: duplicate request key")
            requests.add(request)
    artifacts = {v["artifact_id"]: v for k, _, v in records if k == "artifact"}
    terminals: set[str] = set()
    for kind, relative, value in records:
        if kind == "event" and value["type"] in {"run.succeeded", "run.failed", "run.cancelled"}:
            if value["run_id"] in terminals or value["type"] != f"run.{runs.get(value['run_id'], {}).get('status')}":
                report["errors"].append(f"{relative}: duplicate or inconsistent terminal event")
            terminals.add(value["run_id"])
        if kind == "tool":
            refs = [value["stdout_artifact_ref"], value["stderr_artifact_ref"]]
            changed = value["change_summary"].get("changed_paths_artifact_ref")
            if changed:
                refs.append(changed)
            for ref in refs:
                artifact = artifacts.get(ref["artifact_id"], {})
                if any(artifact.get(key) != ref[key] for key in ("task_id", "run_id", "operation_id", "sha256", "relative_path")):
                    report["errors"].append(f"{relative}: invalid tool artifact reference {ref['artifact_id']}")
    report["counts"] = dict(Counter(kind for kind, _, _ in records))
    report["retained_files"] = len(manifest)
    return report


def public_report(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report.items() if k not in {"records", "manifest"}}


def migrate(project: Path, *, executors_stopped: bool = False) -> dict[str, Any]:
    """Backup, convert in isolation, verify, then atomically publish state.db."""
    if not executors_stopped:
        raise DomainError("MIGRATION_STOP_REQUIRED", "stop old processes and containers; pass --executors-stopped")
    root = project_path(project, ".yipilot")
    with project_lock(project, "executor"), project_lock(project, "maintenance"), ExitStack() as locks:
        if (root / "state.db").exists():
            with Database(project).transaction() as connection:
                rows = connection.execute("SELECT report FROM migration").fetchall()
                if rows:
                    return {**json.loads(rows[0][0]), "already_migrated": True}
            raise DomainError("MIGRATION_CONFLICT", "target database already exists")
        try:
            for path in root.rglob("*.lock"):
                require_plain_path(path)
                if path.name not in {"executor.lock", "maintenance.lock"}:
                    locks.enter_context(FileLock(path).acquire(timeout=0))
        except Timeout as error:
            raise DomainError("PROJECT_BUSY", "legacy writer is active") from error
        report = scan(project)
        if report["errors"]:
            raise DomainError("MIGRATION_INVALID", json.dumps(public_report(report), ensure_ascii=False))
        backup = project_path(project, f".yipilot-backup-{report['source_hash'][:16]}")
        if not backup.exists():
            shutil.copytree(root, backup, ignore=shutil.ignore_patterns("*.lock"))
        if _manifest(backup) != report["manifest"]:
            raise DomainError("MIGRATION_BACKUP_INVALID", "backup hashes differ")
        temporary = Path(mkdtemp(prefix=".yipilot-migration-", dir=project))
        shutil.copytree(backup, temporary / ".yipilot")
        database = Database(temporary)
        database.initialize()
        try:
            _import(database, report)
            with database.transaction() as connection:
                for kind, table in (("project", "project"), ("task", "tasks"), ("operation", "operations"), ("run", "runs"), ("event", "events")):
                    if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != report["counts"].get(kind, 0):
                        raise DomainError("MIGRATION_INVALID", f"count mismatch: {kind}")
                for kind in ("repo", "artifact", "approval", "tool"):
                    if connection.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()[0] != report["counts"].get(kind, 0):
                        raise DomainError("MIGRATION_INVALID", f"count mismatch: {kind}")
            store = FileTaskStore(temporary)
            for task in store.list_tasks():
                for run in store.list_runs(task.task_id):
                    artifacts = FileArtifactStore(store)
                    for artifact in artifacts.list(task.task_id, run.run_id):
                        artifacts.verify(task.task_id, run.run_id, artifact.artifact_id)
                    FileToolRunStore(store).list(task.task_id, run.run_id)
                    journal = JsonlEventJournal(store.event_journal_path(run.run_id), run_id=run.run_id, task_id=run.task_id, stage=run.stage)
                    events = journal.read_after(0)
                    terminal = {"run.succeeded", "run.failed", "run.cancelled"}
                    if not any(event.type in terminal for event in events) and run.status.value in {"succeeded", "failed", "cancelled"}:
                        journal.append(f"run.{run.status.value}", {"legacy": True, "error_code": run.error_code})
            with database.transaction() as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise DomainError("MIGRATION_INVALID", "database validation failed")
            if _manifest(root) != report["manifest"]:
                raise DomainError("MIGRATION_CONFLICT", "source changed during conversion")
            summary = {**public_report(report), "backup": str(backup), "converted_project": str(temporary)}
            with database.transaction() as connection:
                connection.execute("INSERT INTO migration VALUES (?,?)", (report["source_hash"], json.dumps(summary)))
            atomic_write_bytes(backup.with_suffix(".manifest.json"), json.dumps(report["manifest"], sort_keys=True).encode())
            _switch(database.path, root / "state.db")
            return summary
        except Exception as error:
            raise DomainError("MIGRATION_FAILED", f"source retained; inspect {temporary}: {error}") from error


def _switch(source: Path, target: Path) -> None:
    if target.exists():
        raise DomainError("MIGRATION_CONFLICT", "target appeared during migration")
    os.replace(source, target)


def _import(database: Database, report: dict[str, Any]) -> None:
    with database.transaction() as connection:
        order = {kind: i for i, kind in enumerate(("project", "task", "operation", "run", "repo", "artifact", "approval", "tool", "event"))}
        for kind, _, original in sorted(report["records"], key=lambda r: order[r[0]]):
            value = dict(original)
            body = json.dumps(value)
            if kind == "project":
                connection.execute("INSERT INTO project VALUES ('project',?)", (body,))
            elif kind == "task":
                connection.execute("INSERT INTO tasks VALUES (?,?)", (value["task_id"], body))
            elif kind == "operation":
                connection.execute("INSERT INTO operations VALUES (?,?,?,?,?)", (value["operation_id"], value["task_id"], value["idempotency_key"], hashlib.sha256(value["request_json"].encode()).hexdigest(), body))
            elif kind == "run":
                if value["status"] in {"pending", "running", "paused"}:
                    value.update(status="cancelled", error_code="LEGACY_INTERRUPTED")
                value["legacy"] = True
                attempt = connection.execute("SELECT COUNT(*)+1 FROM runs WHERE operation_id=? AND stage=?", (value["operation_id"], value["stage"])).fetchone()[0]
                connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (value["run_id"], value["task_id"], value["operation_id"], value["stage"], attempt, json.dumps(value)))
            elif kind == "event":
                cursor = connection.execute("INSERT INTO events(event_id,run_id,sequence,type,body) VALUES (?,?,?,?,?)", (value["event_id"], value["run_id"], value["sequence"], value["type"], body)).lastrowid
                value["cursor"] = cursor
                connection.execute("UPDATE events SET body=? WHERE cursor=?", (json.dumps(value), cursor))
            else:
                if kind == "approval":
                    value["revoked"] = True
                identifier = f"{value['task_id']}:{value['alias']}" if kind == "repo" else value[{"artifact": "artifact_id", "approval": "approval_id", "tool": "tool_run_id"}[kind]]
                database.save_record(kind, identifier, value["task_id"], value.get("run_id"), json.dumps(value))
