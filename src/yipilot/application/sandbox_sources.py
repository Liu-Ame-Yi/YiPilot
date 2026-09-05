"""Trusted, Git-object-only source export. This service executes no repository code."""

import hashlib
import json
import re
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from filelock import FileLock
from pydantic import ValidationError

from yipilot.application.repo_service import RepoService
from yipilot.domain.errors import DomainError
from yipilot.domain.models import RunStatus
from yipilot.domain.sandbox import SandboxErrorCode as Code
from yipilot.domain.sandbox import SourceFile, SourceManifest, source_path
from yipilot.infrastructure.filesystem import atomic_write_json, project_path
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord, TaskRecord

_EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    ".yipilot",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
    ".docker",
    ".kube",
    ".gnupg",
    ".netrc",
    "_netrc",
    ".gitconfig",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".vscode",
    ".idea",
    ".codex",
    ".claude",
}
_MAX_FILE_BYTES = 16 * 1024**2
_MAX_TREE_BYTES = 128 * 1024**2
_MAX_FILES = 10000
_OBJECT_ENV = {"GIT_NO_REPLACE_OBJECTS": "1"}


@dataclass(frozen=True)
class PreparedSources:
    baseline: Path
    execution: Path
    manifest_path: Path
    manifest_hash: str
    manifest: SourceManifest


def _plain_path(path: Path) -> None:
    """Check every existing ancestor without resolving away links or junctions."""
    for candidate in reversed((path, *path.parents)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode))
            or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1)
        ):
            raise DomainError(
                Code.PATH_DENIED, "links, reparse points and special files denied"
            )


def _excluded(path: str) -> bool:
    return any(
        p.casefold() in _EXCLUDED or p.casefold().startswith(".env")
        for p in path.split("/")
    )


def _text(content: bytes) -> None:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DomainError(
            Code.SOURCE_DENIED, "source must be UTF-8 patchable text"
        ) from error
    if b"\x00" in content or len(content) > _MAX_FILE_BYTES:
        raise DomainError(Code.SOURCE_DENIED, "binary or oversized source denied")


class SandboxSourceService:
    """One completed pair per persisted Run; retained until trusted evidence cleanup."""

    def __init__(self, project_root: Path, git_runner: GitRunner) -> None:
        _plain_path(project_root.absolute())
        self._root = project_root.resolve(strict=True)
        self._git = git_runner
        self._store = FileTaskStore(self._root)
        self._repos = RepoService(self._root, git_runner)

    def prepare(
        self,
        task: TaskRecord,
        repository_alias: str,
        run: RunRecord,
        frozen_base_sha: str,
    ) -> PreparedSources:
        # Re-read mutable storage records rather than trusting caller-owned objects.
        _plain_path(self._root / ".yipilot")
        if (
            task != self._store.get_task(task.task_id)
            or run.task_id != task.task_id
            or run != self._store.get_run(task.task_id, run.run_id)
        ):
            raise DomainError(
                Code.ASSOCIATION_INVALID, "Task/Operation/Run association changed"
            )
        self._store.get_operation(task.task_id, run.operation_id)
        if run.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
            raise DomainError(
                Code.ASSOCIATION_INVALID, "Run must be pending or running"
            )
        if not isinstance(frozen_base_sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", frozen_base_sha
        ):
            raise DomainError(
                Code.BASE_INVALID, "base must be a full frozen commit SHA"
            )
        metadata = self._repos.get_metadata(task.task_id, repository_alias)
        if (metadata.task_id, metadata.alias) != (task.task_id, repository_alias):
            raise DomainError(
                Code.ASSOCIATION_INVALID, "repository association changed"
            )
        _plain_path(metadata.cache_path)
        root = self._store.event_journal_path(run.run_id).parent
        _plain_path(root)
        baseline = project_path(root, "baseline")
        execution = project_path(root, "execution")
        manifest_path = project_path(root, "source-manifest.json")
        lock = project_path(root, ".source-copy.lock")
        _plain_path(lock)
        with FileLock(str(lock)):
            for path in (baseline, execution, manifest_path):
                _plain_path(path)
                if path.exists():
                    raise DomainError(
                        Code.CONFLICT, "Run source copies must never be reused"
                    )
            created: list[Path] = []
            try:
                try:
                    resolved = self._git.run(
                        [
                            "git",
                            "rev-parse",
                            "--verify",
                            f"{frozen_base_sha}^{{commit}}",
                        ],
                        cwd=metadata.cache_path,
                        env=_OBJECT_ENV,
                    ).stdout.strip()
                except DomainError as error:
                    raise DomainError(
                        Code.BASE_INVALID, "frozen commit is unavailable"
                    ) from error
                if resolved != frozen_base_sha:
                    raise DomainError(
                        Code.BASE_INVALID, "base must identify a commit directly"
                    )
                baseline.mkdir()
                created.append(baseline)
                execution.mkdir()
                created.append(execution)
                files = self._export(
                    metadata.cache_path, frozen_base_sha, baseline, execution
                )
                files = self._remove_ignored(
                    metadata.cache_path, baseline, execution, files
                )
                manifest = SourceManifest(
                    task_id=task.task_id,
                    operation_id=run.operation_id,
                    run_id=run.run_id,
                    repository_alias=repository_alias,
                    base_sha=frozen_base_sha,
                    created_at=datetime.now(UTC),
                    files=files,
                )
                # Mark ownership before writing: an interrupted/failed atomic write is cleaned too.
                created.append(manifest_path)
                atomic_write_json(
                    manifest_path,
                    manifest.model_dump(mode="json"),
                    lambda value: SourceManifest.model_validate_json(
                        json.dumps(dict(value))
                    ),
                )
                prepared = PreparedSources(
                    baseline,
                    execution,
                    manifest_path,
                    hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    manifest,
                )
                self.validate(prepared)
                return prepared
            except BaseException as error:
                for path in reversed(created):
                    _plain_path(path)
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                if isinstance(error, (OSError, ValueError)):
                    raise DomainError(
                        Code.COPY_FAILED, "source copy preparation failed"
                    ) from error
                raise

    def _export(
        self, cache: Path, base: str, baseline: Path, execution: Path
    ) -> tuple[SourceFile, ...]:
        tree = self._git.run_bytes(
            ["git", "ls-tree", "-rz", "--full-tree", base], cwd=cache, env=_OBJECT_ENV
        ).stdout
        entries = tree.split(b"\x00")
        if entries[-1] != b"" or len(entries) > _MAX_FILES + 1:
            raise DomainError(Code.SOURCE_DENIED, "invalid or oversized Git tree")
        files = []
        seen: dict[str, str] = {}
        total = 0
        for entry in entries[:-1]:
            header, name = entry.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split(" ")
            name_text = source_path(name.decode("utf-8"))
            if mode not in {"100644", "100755"} or kind != "blob":
                raise DomainError(
                    Code.SOURCE_DENIED, "Git links and special entries denied"
                )
            if not re.fullmatch(r"[0-9a-f]{40}", oid):
                raise DomainError(Code.SOURCE_DENIED, "invalid Git object id")
            # Include directories in collision detection (Foo/a versus foo/b on Windows).
            parts = name_text.split("/")
            for i in range(1, len(parts) + 1):
                prefix = "/".join(parts[:i])
                prior = seen.setdefault(prefix.casefold(), prefix)
                if prior != prefix:
                    raise DomainError(
                        Code.PATH_DENIED, "case-alias source paths denied"
                    )
            if _excluded(name_text):
                continue
            size = int(
                self._git.run(
                    ["git", "cat-file", "-s", oid], cwd=cache, env=_OBJECT_ENV
                ).stdout
            )
            total += size
            if size > _MAX_FILE_BYTES or total > _MAX_TREE_BYTES:
                raise DomainError(Code.SOURCE_DENIED, "source copy budget exceeded")
            content = self._git.run_bytes(
                ["git", "cat-file", "blob", oid], cwd=cache, env=_OBJECT_ENV
            ).stdout
            _text(content)
            if size != len(content):
                raise DomainError(Code.SOURCE_DENIED, "Git blob size changed")
            for root in (baseline, execution):
                path = project_path(root, name_text)
                _plain_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as handle:
                    handle.write(content)
                if mode == "100755":
                    path.chmod(0o755)
            files.append(
                SourceFile(
                    path=name_text,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=size,
                    git_mode="100755" if mode == "100755" else "100644",
                )
            )
        return tuple(sorted(files, key=lambda file: file.path))

    def _remove_ignored(
        self,
        cache: Path,
        baseline: Path,
        execution: Path,
        files: tuple[SourceFile, ...],
    ) -> tuple[SourceFile, ...]:
        # An absent private index treats all exported files as candidates. Only the
        # frozen tree's .gitignore files participate, never user/global excludes.
        with TemporaryDirectory(
            prefix=".source-index-", dir=baseline.parent
        ) as directory:
            ignored_output = self._git.run_bytes(
                [
                    "git",
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-per-directory=.gitignore",
                    "-z",
                ],
                cwd=cache,
                env={
                    **_OBJECT_ENV,
                    "GIT_WORK_TREE": str(baseline),
                    "GIT_INDEX_FILE": str(Path(directory) / "index"),
                },
            ).stdout
        ignored = {
            source_path(p.decode("utf-8")) for p in ignored_output.split(b"\0") if p
        }
        if not ignored.issubset({f.path for f in files}):
            raise DomainError(Code.SOURCE_DENIED, "unexpected ignored source paths")
        for name in ignored:
            for tree in (baseline, execution):
                path = project_path(tree, name)
                _plain_path(path)
                path.unlink()
                parent = path.parent
                while parent != tree and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        return tuple(f for f in files if f.path not in ignored)

    def validate(self, prepared: PreparedSources) -> None:
        """Check frozen baseline and both trees' safety; execution content may change."""
        manifest = prepared.manifest
        root = project_path(
            self._root, ".yipilot", "tasks", manifest.task_id, "runs", manifest.run_id
        )
        if (prepared.baseline, prepared.execution, prepared.manifest_path) != (
            root / "baseline",
            root / "execution",
            root / "source-manifest.json",
        ):
            raise DomainError(
                Code.ASSOCIATION_INVALID, "source paths do not belong to Run"
            )
        for path in (prepared.baseline, prepared.execution, prepared.manifest_path):
            _plain_path(path)
        try:
            content = prepared.manifest_path.read_bytes()
            if (
                hashlib.sha256(content).hexdigest() != prepared.manifest_hash
                or SourceManifest.model_validate_json(content) != manifest
            ):
                raise ValueError("manifest changed")
            for tree in (prepared.baseline, prepared.execution):
                if not tree.is_dir():
                    raise ValueError("source tree missing")
                actual = {}
                pending = [tree]
                seen: set[str] = set()
                total = 0
                while pending:
                    for path in pending.pop().iterdir():
                        _plain_path(path)
                        relative = source_path(path.relative_to(tree).as_posix())
                        if relative.casefold() in seen or len(seen) >= _MAX_FILES * 2:
                            raise ValueError("case alias or oversized tree")
                        seen.add(relative.casefold())
                        if _excluded(relative):
                            raise ValueError("excluded source entry")
                        if path.is_dir():
                            pending.append(path)
                        else:
                            if path.stat().st_size > _MAX_FILE_BYTES:
                                raise ValueError("oversized source")
                            total += path.stat().st_size
                            if total > _MAX_TREE_BYTES:
                                raise ValueError("source tree budget exceeded")
                            data = path.read_bytes()
                            _text(data)
                            actual[relative] = (
                                hashlib.sha256(data).hexdigest(),
                                len(data),
                            )
                if tree == prepared.baseline and actual != {
                    f.path: (f.sha256, f.size_bytes) for f in manifest.files
                }:
                    raise ValueError("baseline changed")
        except (OSError, ValueError, ValidationError) as error:
            raise DomainError(
                Code.MANIFEST_INVALID, "source evidence is invalid"
            ) from error
