"""Atomic, run-scoped persistence for registered artifacts."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ArtifactRef
from yipilot.persistence.filesystem import (
    atomic_write_bytes,
    atomic_write_json,
    project_path,
)
from yipilot.persistence.task_store import FileTaskStore, RunRecord
from yipilot.security.redaction import SecretRedactor


class ArtifactRecord(BaseModel):
    """Versioned metadata for one file produced by a persisted Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifact_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_fingerprint: str = Field(min_length=1)
    created_at: datetime


class FileArtifactStore:
    """Register and load artifact metadata beneath an existing Run."""

    def __init__(self, task_store: FileTaskStore) -> None:
        self._task_store = task_store

    @property
    def task_store(self) -> FileTaskStore:
        return self._task_store

    def register(
        self,
        task_id: str,
        run_id: str,
        *,
        producer: str,
        relative_path: str,
        input_fingerprint: str,
        operation_id: str | None = None,
    ) -> ArtifactRecord:
        run, run_root = self._run_context(task_id, run_id)
        self._require_operation(run, operation_id)
        artifact_path = _artifact_path(run_root, relative_path)
        unresolved_path = run_root.joinpath(relative_path)
        if not artifact_path.is_file() or unresolved_path.is_symlink():
            raise DomainError("ARTIFACT_NOT_FOUND", "artifact must be a regular file")
        record = ArtifactRecord(
            artifact_id=str(uuid4()),
            task_id=run.task_id,
            operation_id=run.operation_id,
            run_id=run.run_id,
            producer=producer,
            relative_path=relative_path,
            sha256=_sha256(artifact_path),
            input_fingerprint=input_fingerprint,
            created_at=datetime.now(UTC),
        )
        path = self._record_path(run_root, record.artifact_id)
        atomic_write_json(
            path,
            record.model_dump(mode="json"),
            ArtifactRecord.model_validate,
        )
        return self.get(task_id, run_id, record.artifact_id)

    def write_bytes(
        self,
        task_id: str,
        run_id: str,
        *,
        producer: str,
        relative_path: str,
        content: bytes,
        input_fingerprint: str,
        operation_id: str | None = None,
    ) -> ArtifactRecord:
        """Atomically persist and register one run-scoped binary artifact."""
        run, run_root = self._run_context(task_id, run_id)
        self._require_operation(run, operation_id)
        atomic_write_bytes(_artifact_path(run_root, relative_path), _redact_bytes(content))
        return self.register(
            task_id,
            run_id,
            producer=producer,
            relative_path=relative_path,
            input_fingerprint=input_fingerprint,
            operation_id=operation_id,
        )

    def get(self, task_id: str, run_id: str, artifact_id: str) -> ArtifactRecord:
        run, run_root = self._run_context(task_id, run_id)
        path = self._record_path(run_root, artifact_id)
        if not path.is_file():
            raise DomainError("ARTIFACT_NOT_FOUND", "artifact record not found")
        try:
            record = ArtifactRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise DomainError(
                "ARTIFACT_RECORD_INVALID", "artifact record is invalid"
            ) from error
        if (
            record.task_id != run.task_id
            or record.operation_id != run.operation_id
            or record.run_id != run.run_id
        ):
            raise DomainError(
                "ARTIFACT_ASSOCIATION_MISMATCH",
                "artifact record does not belong to the persisted run",
            )
        _artifact_path(run_root, record.relative_path)
        return record

    def list(self, task_id: str, run_id: str) -> list[ArtifactRecord]:
        _, run_root = self._run_context(task_id, run_id)
        records_root = project_path(run_root, "artifacts", "records")
        if not records_root.exists():
            return []
        if not records_root.is_dir():
            raise DomainError(
                "ARTIFACT_RECORD_INVALID", "artifact records path is not a directory"
            )
        records = [
            self.get(task_id, run_id, path.stem)
            for path in records_root.glob("*.json")
        ]
        return sorted(records, key=lambda record: (record.created_at, record.artifact_id))

    def verify(self, task_id: str, run_id: str, artifact_id: str) -> ArtifactRecord:
        """Load one record and verify its registered content remains intact."""
        record = self.get(task_id, run_id, artifact_id)
        _, run_root = self._run_context(task_id, run_id)
        path = _artifact_path(run_root, record.relative_path)
        if not path.is_file() or path.is_symlink() or _sha256(path) != record.sha256:
            raise DomainError("ARTIFACT_STALE", "artifact content is missing or has changed")
        return record

    def _run_context(self, task_id: str, run_id: str) -> tuple[RunRecord, Path]:
        run = self._task_store.get_run(task_id, run_id)
        self._task_store.get_operation(task_id, run.operation_id)
        run_root = self._task_store.event_journal_path(run.run_id).parent
        return run, run_root

    @staticmethod
    def _require_operation(run: RunRecord, operation_id: str | None) -> None:
        if operation_id is not None and operation_id != run.operation_id:
            raise DomainError(
                "ARTIFACT_ASSOCIATION_MISMATCH",
                "artifact operation does not belong to the persisted run",
            )

    @staticmethod
    def _record_path(run_root: Path, artifact_id: str) -> Path:
        return project_path(
            run_root,
            "artifacts",
            "records",
            f"{artifact_id}.json",
        )


def _redact_bytes(content: bytes) -> bytes:
    try:
        return SecretRedactor().redact_text(content.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        return content

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_path(run_root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    windows_path = PureWindowsPath(relative_path)
    if (
        not relative_path
        or path == Path(".")
        or path.is_absolute()
        or bool(path.drive)
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or ".." in windows_path.parts
    ):
        raise DomainError("PATH_DENIED", "artifact path is not run-relative")
    return project_path(run_root, relative_path)


def artifact_reference(record: ArtifactRecord) -> ArtifactRef:
    """Project persisted metadata onto the public evidence reference contract."""
    return ArtifactRef(artifact_id=record.artifact_id, task_id=record.task_id,
        operation_id=record.operation_id, run_id=record.run_id,
        relative_path=record.relative_path, sha256=record.sha256)
