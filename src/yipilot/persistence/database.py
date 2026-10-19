"""Local SQLite metadata. Connections and explicit transactions are thread-local."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from threading import local
from uuid import uuid4

from yipilot.domain.errors import DomainError
from yipilot.persistence.filesystem import project_path
from yipilot.persistence.ownership import project_lock
from yipilot.security.paths import require_plain_path

_active = local()
SCHEMA_VERSION = 1
_SCHEMA = (
    'CREATE TABLE project (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
    'CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
    'CREATE TABLE operations (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(task_id, request_key), UNIQUE(id,task_id))',
    'CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), operation_id TEXT NOT NULL REFERENCES operations(id), stage TEXT NOT NULL, attempt INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(operation_id, stage, attempt), UNIQUE(id,task_id), FOREIGN KEY(operation_id,task_id) REFERENCES operations(id,task_id))',
    'CREATE TABLE records (kind TEXT NOT NULL, id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT REFERENCES runs(id), body TEXT NOT NULL, PRIMARY KEY(kind,id), FOREIGN KEY(run_id,task_id) REFERENCES runs(id,task_id))',
    'CREATE TABLE events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL, type TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(run_id,sequence))',
    "CREATE UNIQUE INDEX terminal_event ON events(run_id) WHERE type IN ('run.succeeded','run.failed','run.cancelled')",
    'CREATE TABLE migration (source_hash TEXT PRIMARY KEY, report TEXT NOT NULL)',
)


class Database:
    def __init__(self, project: Path, *, filename: str = "state.db") -> None:
        self.project = project
        self.filename = filename

    @property
    def path(self) -> Path:
        path = project_path(self.project, ".yipilot", self.filename)
        require_plain_path(path)
        require_plain_path(path.with_name(path.name + "-journal"))
        return path

    def initialize(self) -> None:
        with self.transaction(create=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise DomainError("STORAGE_VERSION", "unsupported database schema")

    @contextmanager
    def transaction(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        key = str(self.path.resolve())
        connections = getattr(_active, "connections", None)
        if connections is None:
            connections = _active.connections = {}
        if key in connections:
            connection = connections[key]
            savepoint = "nested_" + uuid4().hex
            connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield connection
                connection.execute(f"RELEASE {savepoint}")
            except BaseException:
                connection.execute(f"ROLLBACK TO {savepoint}")
                connection.execute(f"RELEASE {savepoint}")
                raise
            return
        if not create and not self.path.is_file():
            code = "MIGRATION_REQUIRED" if project_path(self.project, ".yipilot", "config.yaml").exists() else "PROJECT_NOT_INITIALIZED"
            raise DomainError(code, "initialize a new project or explicitly migrate legacy storage")
        with project_lock(self.project, "maintenance", timeout=5), closing(
            sqlite3.connect(self.path, timeout=5, isolation_level=None)
        ) as connection:
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                if not create and connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise DomainError("STORAGE_VERSION", "unsupported database schema")
                connections[key] = connection
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connections.pop(key, None)

    def save_record(self, kind: str, identifier: str, task_id: str, run_id: str | None, body: str) -> None:
        with self.transaction() as connection:
            previous = connection.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            if previous and previous[0] != body and kind != "repo":
                raise DomainError("STORAGE_CONFLICT", "immutable record already exists")
            connection.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (kind, identifier, task_id, run_id, body))

    def record(self, kind: str, identifier: str, missing: str) -> str:
        with self.transaction() as connection:
            row = connection.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            if row is None:
                raise DomainError(missing, "record not found")
            return str(row[0])

    def records(self, kind: str, task_id: str, run_id: str | None = None) -> list[str]:
        with self.transaction() as connection:
            return [row[0] for row in connection.execute("SELECT body FROM records WHERE kind=? AND task_id=? AND run_id IS ? ORDER BY id", (kind, task_id, run_id))]
