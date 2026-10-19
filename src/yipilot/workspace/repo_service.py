"""Repository synchronization into project-scoped Git caches."""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from yipilot.domain.errors import DomainError
from yipilot.persistence.filesystem import project_path
from yipilot.persistence.ownership import exclusive_execution
from yipilot.persistence.task_store import FileTaskStore, RepoMetadataRecord
from yipilot.workspace.git_runner import GitRunner

_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_URL_WITH_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


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

    @exclusive_execution
    def add_and_sync(self, task_id: str, url: str, alias: str) -> RepoMetadata:
        self._task_store.get_task(task_id)
        _validate_alias(alias)
        normalized_url = normalize_repo_url(url)
        url_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()

        existing_records = self._task_store.db.records("repo", task_id)
        existing_match = next((r for r in existing_records if RepoMetadataRecord.model_validate_json(r).alias == alias), None)
        if existing_match:
            existing = self._metadata_from_record(RepoMetadataRecord.model_validate_json(existing_match))
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

        record = RepoMetadataRecord(
            task_id=task_id,
            alias=alias,
            normalized_url=normalized_url,
            url_hash=url_hash,
            cache_path=cache_path.relative_to(self._project_root).as_posix(),
            head_sha=head_sha,
            synced_at=_timestamp(),
        )
        self._metadata_path(task_id, alias).parent.mkdir(parents=True, exist_ok=True)
        self._task_store.db.save_record("repo", f"{task_id}:{alias}", task_id, None, record.model_dump_json())
        return self._metadata_from_record(record)

    def get_metadata(self, task_id: str, alias: str) -> RepoMetadata:
        self._task_store.get_task(task_id)
        _validate_alias(alias)
        record = RepoMetadataRecord.model_validate_json(self._task_store.db.record("repo", f"{task_id}:{alias}", "REPO_NOT_FOUND"))
        return self._metadata_from_record(record)

    def list_metadata(self, task_id: str) -> list[RepoMetadata]:
        self._task_store.get_task(task_id)
        records = [RepoMetadataRecord.model_validate_json(body) for body in self._task_store.db.records("repo", task_id)]
        return [self._metadata_from_record(r) for r in sorted(records, key=lambda r: r.alias)]

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

    def _metadata_from_record(self, record: RepoMetadataRecord) -> RepoMetadata:
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
