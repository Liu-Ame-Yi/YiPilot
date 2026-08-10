"""Task-owned Git worktree preparation."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ValidationError

from yipilot.application.repo_service import RepoService
from yipilot.domain.errors import DomainError
from yipilot.infrastructure.filesystem import atomic_write_json, project_path
from yipilot.infrastructure.git_runner import GitRunner

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class _WorkspaceManifestRecord(BaseModel):
    schema_version: Literal[1] = 1
    task_id: str
    run_id: str
    repo_alias: str
    repo_hash: str
    branch: str
    base_sha: str
    workspace_path: str
    created_at: str


@dataclass(frozen=True)
class PreparedWorkspace:
    path: Path
    schema_version: int
    task_id: str
    run_id: str
    repo_alias: str
    repo_hash: str
    branch: str
    base_sha: str
    created_at: str


class WorkspaceService:
    """Create or safely reuse a Task and Run-specific Git worktree."""

    def __init__(self, project_root: Path, git_runner: GitRunner) -> None:
        self._project_root = Path(project_root).resolve(strict=True)
        self._git = git_runner
        self._repos = RepoService(self._project_root, git_runner)

    def prepare(self, task_id: str, repo_alias: str, run_id: str) -> PreparedWorkspace:
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise DomainError("WORKSPACE_RUN_INVALID", "workspace run id is invalid")
        metadata = self._repos.get_metadata(task_id, repo_alias)
        branch = f"yipilot/task-{task_id}/{run_id}"
        repo_root = project_path(
            self._project_root,
            ".yipilot",
            "tasks",
            task_id,
            "repos",
            repo_alias,
        )
        workspace_path = project_path(repo_root, "workspace")
        manifest_path = project_path(repo_root, "workspace-manifest.json")
        relative_workspace = workspace_path.relative_to(self._project_root).as_posix()
        lock_path = project_path(
            self._project_root,
            ".yipilot",
            "locks",
            f"workspace-{task_id}-{repo_alias}.lock",
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with FileLock(str(lock_path)):
            if manifest_path.is_file():
                manifest = self._load_manifest(manifest_path)
                self._require_matching_manifest(
                    manifest,
                    task_id=task_id,
                    run_id=run_id,
                    repo_alias=repo_alias,
                    repo_hash=metadata.url_hash,
                    branch=branch,
                    workspace_path=relative_workspace,
                )
                if not workspace_path.is_dir() or not (workspace_path / ".git").is_file():
                    raise DomainError("WORKSPACE_CONFLICT", "recorded workspace is missing")
                return self._prepared_workspace(workspace_path, manifest)

            if workspace_path.exists():
                raise DomainError(
                    "WORKSPACE_CONFLICT", "workspace exists without a matching manifest"
                )

            self._git.run(
                ["git", "branch", branch, metadata.head_sha], cwd=metadata.cache_path
            )
            self._git.run(
                ["git", "worktree", "add", str(workspace_path), branch],
                cwd=metadata.cache_path,
            )
            manifest = _WorkspaceManifestRecord(
                task_id=task_id,
                run_id=run_id,
                repo_alias=repo_alias,
                repo_hash=metadata.url_hash,
                branch=branch,
                base_sha=metadata.head_sha,
                workspace_path=relative_workspace,
                created_at=_timestamp(),
            )
            atomic_write_json(
                manifest_path,
                manifest.model_dump(mode="json"),
                _WorkspaceManifestRecord.model_validate,
            )
            return self._prepared_workspace(workspace_path, manifest)

    @staticmethod
    def _load_manifest(path: Path) -> _WorkspaceManifestRecord:
        try:
            return _WorkspaceManifestRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise DomainError("WORKSPACE_CONFLICT", "workspace manifest is invalid") from error

    @staticmethod
    def _require_matching_manifest(
        manifest: _WorkspaceManifestRecord,
        *,
        task_id: str,
        run_id: str,
        repo_alias: str,
        repo_hash: str,
        branch: str,
        workspace_path: str,
    ) -> None:
        expected = (
            task_id,
            run_id,
            repo_alias,
            repo_hash,
            branch,
            workspace_path,
        )
        actual = (
            manifest.task_id,
            manifest.run_id,
            manifest.repo_alias,
            manifest.repo_hash,
            manifest.branch,
            manifest.workspace_path,
        )
        if actual != expected:
            raise DomainError(
                "WORKSPACE_CONFLICT",
                "workspace manifest belongs to another task, repository, or run",
            )

    @staticmethod
    def _prepared_workspace(
        path: Path, manifest: _WorkspaceManifestRecord
    ) -> PreparedWorkspace:
        return PreparedWorkspace(
            path=path,
            schema_version=manifest.schema_version,
            task_id=manifest.task_id,
            run_id=manifest.run_id,
            repo_alias=manifest.repo_alias,
            repo_hash=manifest.repo_hash,
            branch=manifest.branch,
            base_sha=manifest.base_sha,
            created_at=manifest.created_at,
        )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
