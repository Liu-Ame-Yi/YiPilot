"""Repository synchronization into project-scoped Git caches."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ValidationError

from yipilot.domain.errors import DomainError
from yipilot.infrastructure.filesystem import atomic_write_json, project_path
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore

_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_URL_WITH_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


class _RepoMetadataRecord(BaseModel):
    schema_version: Literal[1] = 1
    task_id: str
    alias: str
    normalized_url: str
    url_hash: str
    cache_path: str
    head_sha: str
    synced_at: str


@dataclass(frozen=True)
class RepoMetadata:
    schema_version: int
    task_id: str
    alias: str
    normalized_url: str
    url_hash: str
    cache_path: Path
    head_sha: str
    synced_at: str


class RepoService:
    """Add and synchronize Task repositories without touching source checkouts."""

    def __init__(self, project_root: Path, git_runner: GitRunner) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._git = git_runner
        self._task_store = FileTaskStore(self._project_root)

    def add_and_sync(self, task_id: str, url: str, alias: str) -> RepoMetadata:
        self._task_store.get_task(task_id)
        _validate_alias(alias)
        normalized_url = normalize_repo_url(url)
        url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        metadata_path = self._metadata_path(task_id, alias)

        if metadata_path.is_file():
            existing = self._load_metadata(metadata_path)
            if existing.normalized_url != normalized_url:
                raise DomainError(
                    "REPO_ALIAS_CONFLICT",
                    "repository alias is already associated with a different URL",
                )

        cache_path = project_path(
            self._project_root, ".yipilot", "cache", "repos", url_hash
        )
        if cache_path.exists():
            if not (cache_path / ".git").is_dir():
                raise DomainError("GIT_FAILED", "repository cache is invalid")
            self._git.run(["git", "fetch", "--prune", "origin"], cwd=cache_path)
        else:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._git.run(
                ["git", "clone", "--no-checkout", normalized_url, str(cache_path)],
                cwd=self._project_root,
            )

        head_sha = self._git.run(
            ["git", "rev-parse", "refs/remotes/origin/HEAD"], cwd=cache_path
        ).stdout.strip()
        if not head_sha:
            raise DomainError("GIT_FAILED", "remote HEAD could not be resolved")

        record = _RepoMetadataRecord(
            task_id=task_id,
            alias=alias,
            normalized_url=normalized_url,
            url_hash=url_hash,
            cache_path=cache_path.relative_to(self._project_root).as_posix(),
            head_sha=head_sha,
            synced_at=_timestamp(),
        )
        atomic_write_json(
            metadata_path,
            record.model_dump(mode="json"),
            _RepoMetadataRecord.model_validate,
        )
        return self._metadata_from_record(record)

    def get_metadata(self, task_id: str, alias: str) -> RepoMetadata:
        self._task_store.get_task(task_id)
        _validate_alias(alias)
        path = self._metadata_path(task_id, alias)
        if not path.is_file():
            raise DomainError("REPO_NOT_FOUND", "task repository metadata was not found")
        return self._load_metadata(path)

    def list_metadata(self, task_id: str) -> list[RepoMetadata]:
        """Return registered Task repositories in stable alias order."""
        self._task_store.get_task(task_id)
        directory = project_path(
            self._project_root, ".yipilot", "tasks", task_id, "repos"
        )
        if not directory.is_dir():
            return []
        return [
            self.get_metadata(task_id, path.name)
            for path in sorted(directory.iterdir(), key=lambda item: item.name)
            if path.is_dir() and not path.name.startswith(".")
        ]

    def _metadata_path(self, task_id: str, alias: str) -> Path:
        return project_path(
            self._project_root,
            ".yipilot",
            "tasks",
            task_id,
            "repos",
            alias,
            "metadata.json",
        )

    def _load_metadata(self, path: Path) -> RepoMetadata:
        try:
            record = _RepoMetadataRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise DomainError("STORAGE_INVALID", "repository metadata is invalid") from error
        return self._metadata_from_record(record)

    def _metadata_from_record(self, record: _RepoMetadataRecord) -> RepoMetadata:
        cache_path = project_path(self._project_root, *Path(record.cache_path).parts)
        return RepoMetadata(
            schema_version=record.schema_version,
            task_id=record.task_id,
            alias=record.alias,
            normalized_url=record.normalized_url,
            url_hash=record.url_hash,
            cache_path=cache_path,
            head_sha=record.head_sha,
            synced_at=record.synced_at,
        )


def normalize_repo_url(url: str) -> str:
    """Return a deterministic representation for a Git URL or local path."""
    if not isinstance(url, str) or not url.strip() or "\x00" in url:
        raise DomainError("REPO_URL_INVALID", "repository URL must be non-empty and NUL-free")
    value = url.strip()
    path = Path(value).expanduser()
    if path.is_absolute() or not _URL_WITH_SCHEME.match(value):
        if re.match(r"^[^/@\s]+@[^/:\s]+:.+$", value):
            return value.rstrip("/")
        return path.resolve(strict=False).as_uri()

    parsed = urlsplit(value)
    if not parsed.scheme or (parsed.scheme != "file" and not parsed.netloc):
        raise DomainError("REPO_URL_INVALID", "repository URL is invalid")
    if (
        parsed.password is not None
        or parsed.scheme.lower() in {"http", "https"}
        and parsed.username is not None
    ):
        raise DomainError(
            "REPO_URL_INVALID", "repository URL must not contain credentials"
        )
    normalized_path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, parsed.query, "")
    )


def _validate_alias(alias: str) -> None:
    if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
        raise DomainError("REPO_ALIAS_INVALID", "repository alias is invalid")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
