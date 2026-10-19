"""Git workspace evidence collection and deterministic Harness checks."""

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Literal, cast
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4

from pydantic import JsonValue

from yipilot.domain.errors import DomainError
from yipilot.harness.models import (
    HarnessCheck,
    HarnessReport,
    HarnessStatus,
    aggregate_checks,
)
from yipilot.persistence.filesystem import atomic_write_json, project_path
from yipilot.workspace.git_runner import GitRunner
from yipilot.workspace.workspace_service import PreparedWorkspace

_EVIDENCE_NAMES = (
    "status.txt",
    "diff.patch",
    "diff-stat.txt",
    "changed-files.txt",
    "changed-files.nul",
    "head.txt",
    "base.txt",
)
_HARNESS_NAME = "git_workspace"
_HARNESS_VERSION = "1.5.0"
_SourceKind = Literal["remote-only", "local-bare", "local-worktree"]


@dataclass(frozen=True)
class _SourceRepository:
    kind: _SourceKind | None
    path: Path | None
    resolution_error: str | None


class GitWorkspaceHarness:
    """Collect and validate the Git evidence for one prepared workspace."""

    def __init__(
        self,
        workspace: PreparedWorkspace,
        git_runner: GitRunner | None = None,
    ) -> None:
        self._workspace = workspace
        self._git = git_runner or GitRunner()
        self._workspace_git_env: dict[str, str] | None = None
        self._project_root = _project_root(workspace.path)
        self.evidence_dir = project_path(
            self._project_root,
            ".yipilot",
            "tasks",
            workspace.task_id,
            "runs",
            workspace.run_id,
            "git",
        )
        self.report_path = project_path(
            self._project_root,
            ".yipilot",
            "tasks",
            workspace.task_id,
            "runs",
            workspace.run_id,
            "harness",
            "git-workspace-report.json",
        )

    def run(self) -> HarnessReport:
        """Build current evidence, preserving and validating the first bundle."""
        return self._evaluate(persist=True)

    def verify(self) -> HarnessReport:
        """Validate the existing evidence bundle without changing durable evidence."""
        return self._evaluate(persist=False)

    def _evaluate(self, *, persist: bool) -> HarnessReport:
        started_at = datetime.now(UTC)
        source = self._source_repository()
        source_status_before, source_error_before = self._source_status(source)

        current_evidence = self._build_evidence()
        if persist and not self.evidence_dir.exists():
            self._persist_evidence(current_evidence)

        manifest, manifest_check = self._check_manifest()
        changed_paths, paths_check = self._check_changed_paths()
        evidence, evidence_check = self._check_evidence(current_evidence)
        source_status_after, source_error_after = self._source_status(source)
        source_check = self._check_source_status(
            source,
            source_status_before,
            source_status_after,
            source_error_before,
            source_error_after,
        )
        checks = [manifest_check, paths_check, evidence_check, source_check]
        inputs = self._input_summary(
            manifest,
            changed_paths,
            evidence,
            source,
            source_status_before,
            source_status_after,
            source_error_before,
            source_error_after,
            checks,
        )
        status = aggregate_checks(checks).status
        if status is HarnessStatus.PASSED:
            summary = "no_changes" if not changed_paths else "changes_validated"
        else:
            summary = "git_workspace_failed"
        report = HarnessReport(
            report_id=str(uuid4()),
            task_id=self._workspace.task_id,
            operation_id="unknown",
            run_id=self._workspace.run_id,
            stage="code_prepare",
            harness_name=_HARNESS_NAME,
            harness_version=_HARNESS_VERSION,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            input_fingerprint=_fingerprint(inputs),
            inputs=inputs,
            status=status,
            checks=checks,
            summary=summary,
        )
        if persist:
            atomic_write_json(
                self.report_path,
                report.model_dump(mode="json"),
                HarnessReport.model_validate,
            )
        return report

    def _build_evidence(self) -> dict[str, str | bytes]:
        with self._workspace_index_snapshot():
            return self._build_evidence_from_index_snapshot()

    def _build_evidence_from_index_snapshot(self) -> dict[str, str | bytes]:
        tracked_paths = _parse_git_changed_paths(
            self._run_bytes(
                "diff", "--name-only", "-z", self._workspace.base_sha
            )
        )
        untracked_paths = _parse_git_changed_paths(
            self._run_bytes(
                "ls-files", "--others", "--exclude-standard", "-z"
            )
        )
        changed_paths = tuple(sorted(set(tracked_paths + untracked_paths)))
        tracked_patch = self._run_bytes(
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            self._workspace.base_sha,
        )
        tracked_stat = self._run(
            "diff",
            "--stat",
            "--no-ext-diff",
            "--no-textconv",
            self._workspace.base_sha,
        )
        untracked_patches = [
            self._run_bytes(
                "diff",
                "--no-index",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                "/dev/null",
                os.fsdecode(path),
            )
            for path in sorted(untracked_paths)
        ]
        untracked_stats = [
            self._run(
                "diff",
                "--no-index",
                "--stat",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                "/dev/null",
                os.fsdecode(path),
            )
            for path in sorted(untracked_paths)
        ]
        return {
            "status.txt": self._run("status", "--porcelain"),
            "diff.patch": _join_git_bytes(tracked_patch, *untracked_patches),
            "diff-stat.txt": _join_git_output(tracked_stat, *untracked_stats),
            "changed-files.txt": _display_changed_paths(changed_paths),
            "changed-files.nul": _serialize_changed_paths(changed_paths),
            "head.txt": self._run("rev-parse", "HEAD"),
            "base.txt": self._run("rev-parse", self._workspace.base_sha),
        }

    @contextmanager
    def _workspace_index_snapshot(self) -> Iterator[None]:
        """Run workspace reads against a disposable index copy."""
        index_value = self._git.run(
            _git_read_argv("rev-parse", "--git-path", "index"),
            cwd=self._workspace.path,
        ).stdout.strip()
        index_path = Path(index_value)
        if not index_path.is_absolute():
            index_path = self._workspace.path / index_path
        try:
            with tempfile.TemporaryDirectory(prefix="yipilot-git-index-") as temporary:
                snapshot = Path(temporary) / "index"
                shutil.copy2(index_path, snapshot)
                self._workspace_git_env = {"GIT_INDEX_FILE": str(snapshot)}
                try:
                    yield
                finally:
                    self._workspace_git_env = None
        except OSError as error:
            raise DomainError(
                "GIT_FAILED", "workspace index could not be safely read"
            ) from error

    def _persist_evidence(self, evidence: dict[str, str | bytes]) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=False)
        for name in _EVIDENCE_NAMES:
            value = evidence[name]
            if isinstance(value, bytes):
                (self.evidence_dir / name).write_bytes(value)
            else:
                (self.evidence_dir / name).write_text(value, encoding="utf-8")

    def _check_manifest(self) -> tuple[dict[str, JsonValue] | None, HarnessCheck]:
        path = _manifest_path(self._workspace)
        evidence_ref = (
            f"tasks/{self._workspace.task_id}/repos/"
            f"{self._workspace.repo_alias}/{path.relative_to(self._project_root / '.yipilot' / 'tasks' / self._workspace.task_id / 'repos' / self._workspace.repo_alias).as_posix()}"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (DomainError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            value = None
        expected = {
            "task_id": self._workspace.task_id,
            "run_id": self._workspace.run_id,
            "repo_alias": self._workspace.repo_alias,
            "repo_hash": self._workspace.repo_hash,
        }
        actual = (
            {key: value.get(key) for key in expected}
            if isinstance(value, dict)
            else None
        )
        if actual != expected:
            return _json_mapping(value), HarnessCheck(
                id="GIT-WORKSPACE-001",
                status=HarnessStatus.FAILED,
                description=(
                    "workspace manifest belongs to the requested task, repository, and run"
                ),
                message="workspace manifest is invalid or belongs to another owner",
                suggestion="prepare a workspace whose manifest matches this task, repository, and run",
                evidence_refs=[evidence_ref],
            )
        return _json_mapping(value), HarnessCheck(
            id="GIT-WORKSPACE-001",
            status=HarnessStatus.PASSED,
            description=(
                "workspace manifest belongs to the requested task, repository, and run"
            ),
            message="workspace manifest ownership matches",
            evidence_refs=[evidence_ref],
        )

    def _check_changed_paths(self) -> tuple[tuple[bytes, ...], HarnessCheck]:
        path = self.evidence_dir / "changed-files.nul"
        evidence_ref = self._evidence_ref("changed-files.nul")
        if not self._safe_evidence_file(path):
            return (), HarnessCheck(
                id="GIT-WORKSPACE-002",
                status=HarnessStatus.FAILED,
                description="all changed paths remain inside the workspace",
                message=(
                    "authoritative changed-files.nul evidence is missing or outside "
                    "the evidence directory"
                ),
                suggestion="restore the original evidence bundle and run the harness again",
                evidence_refs=[evidence_ref],
            )
        try:
            changed_paths = _parse_changed_paths(path.read_bytes())
        except (OSError, UnicodeError, ValueError):
            return (), HarnessCheck(
                id="GIT-WORKSPACE-002",
                status=HarnessStatus.FAILED,
                description="all changed paths remain inside the workspace",
                message=(
                    "changed paths cannot be validated because changed-files.nul is "
                    "missing or malformed"
                ),
                suggestion="restore the original evidence bundle and run the harness again",
                evidence_refs=[evidence_ref],
            )

        workspace_root = self._workspace.path.resolve(strict=True)
        for changed_path_bytes in changed_paths:
            changed_path = os.fsdecode(changed_path_bytes)
            path_value = Path(changed_path)
            windows_path = PureWindowsPath(changed_path)
            try:
                if (
                    not changed_path
                    or path_value == Path(".")
                    or path_value.is_absolute()
                    or bool(path_value.drive)
                    or bool(windows_path.drive)
                    or bool(windows_path.root)
                    or ".." in windows_path.parts
                ):
                    raise ValueError
                resolved = workspace_root.joinpath(path_value).resolve(strict=False)
                if resolved == workspace_root or workspace_root not in resolved.parents:
                    raise ValueError
            except (OSError, ValueError):
                return changed_paths, HarnessCheck(
                    id="GIT-WORKSPACE-002",
                    status=HarnessStatus.FAILED,
                    description="all changed paths remain inside the workspace",
                    message=(
                        "changed path is outside the workspace: "
                        f"{_json_safe_path(changed_path)}"
                    ),
                    suggestion="remove unsafe changed paths and recollect the evidence bundle",
                    evidence_refs=[evidence_ref],
                )

        return changed_paths, HarnessCheck(
            id="GIT-WORKSPACE-002",
            status=HarnessStatus.PASSED,
            description="all changed paths remain inside the workspace",
            message=f"validated {len(changed_paths)} changed path(s)",
            evidence_refs=[evidence_ref],
        )

    def _check_evidence(
        self, current_evidence: dict[str, str | bytes]
    ) -> tuple[list[dict[str, JsonValue]], HarnessCheck]:
        evidence: list[dict[str, JsonValue]] = []
        missing: list[str] = []
        stale: list[str] = []
        try:
            evidence_root = self.evidence_dir.resolve(strict=True)
        except OSError:
            evidence_root = None
        if evidence_root != self.evidence_dir:
            evidence = [
                {"name": name, "present": False, "sha256": None}
                for name in _EVIDENCE_NAMES
            ]
            return evidence, HarnessCheck(
                id="GIT-WORKSPACE-003",
                status=HarnessStatus.FAILED,
                description="the complete Git evidence bundle exists",
                message="Git evidence directory is missing or resolves outside its run",
                suggestion="preserve the unsafe bundle and collect evidence in a new run",
                evidence_refs=[self._evidence_ref(name) for name in _EVIDENCE_NAMES],
            )
        for name in _EVIDENCE_NAMES:
            path = self.evidence_dir / name
            current_bytes = _evidence_value_bytes(current_evidence[name])
            current_digest = hashlib.sha256(current_bytes).hexdigest()
            if not self._safe_evidence_file(path):
                missing.append(name)
                evidence.append(
                    {
                        "name": name,
                        "present": False,
                        "sha256": None,
                        "current_sha256": current_digest,
                        "matches_current": False,
                    }
                )
                continue
            saved_bytes = path.read_bytes()
            digest = hashlib.sha256(saved_bytes).hexdigest()
            matches_current = saved_bytes == current_bytes
            if not matches_current:
                stale.append(name)
            evidence.append(
                {
                    "name": name,
                    "present": True,
                    "sha256": digest,
                    "current_sha256": current_digest,
                    "matches_current": matches_current,
                }
            )

        evidence_refs = [self._evidence_ref(name) for name in _EVIDENCE_NAMES]
        if missing:
            return evidence, HarnessCheck(
                id="GIT-WORKSPACE-003",
                status=HarnessStatus.FAILED,
                description="the complete Git evidence bundle exists",
                message=(
                    "Git evidence bundle is incomplete or uses a legacy format; "
                    f"required Git evidence is missing: {', '.join(missing)}; "
                    "the original evidence was not overwritten"
                ),
                suggestion=(
                    "preserve the incomplete bundle and collect evidence in a new run"
                ),
                evidence_refs=evidence_refs,
            )
        if stale:
            return evidence, HarnessCheck(
                id="GIT-WORKSPACE-003",
                status=HarnessStatus.FAILED,
                description=(
                    "the complete Git evidence bundle matches the current workspace"
                ),
                message=(
                    "Git evidence is stale and does not match the current workspace: "
                    f"{', '.join(stale)}; the original evidence was not overwritten"
                ),
                suggestion=(
                    "preserve the original evidence bundle for audit and collect evidence "
                    "in a new run"
                ),
                evidence_refs=evidence_refs,
            )
        return evidence, HarnessCheck(
            id="GIT-WORKSPACE-003",
            status=HarnessStatus.PASSED,
            description="the complete Git evidence bundle matches the current workspace",
            message="all seven required Git evidence files match the current workspace",
            evidence_refs=evidence_refs,
        )

    def _source_repository(self) -> _SourceRepository:
        from yipilot.persistence.database import Database
        runtime = next(p for p in self._workspace.path.parents if p.name == ".yipilot")
        try:
            metadata = json.loads(Database(runtime.parent).record("repo", f"{self._workspace.task_id}:{self._workspace.repo_alias}", "SOURCE_METADATA_INVALID"))
        except (DomainError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _SourceRepository(None, None, "SOURCE_METADATA_INVALID")
        if not isinstance(metadata, dict):
            return _SourceRepository(None, None, "SOURCE_METADATA_INVALID")
        normalized_url = metadata.get("normalized_url")
        if not isinstance(normalized_url, str):
            return _SourceRepository(None, None, "SOURCE_METADATA_INVALID")
        expected_identity = (
            self._workspace.task_id,
            self._workspace.repo_alias,
            self._workspace.repo_hash,
        )
        actual_identity = (
            metadata.get("task_id"),
            metadata.get("alias"),
            metadata.get("url_hash"),
        )
        normalized_url_hash = hashlib.sha256(
            normalized_url.encode("utf-8")
        ).hexdigest()
        if (
            actual_identity != expected_identity
            or normalized_url_hash != self._workspace.repo_hash
        ):
            return _SourceRepository(None, None, "SOURCE_METADATA_MISMATCH")
        parsed = urlsplit(normalized_url)
        if parsed.scheme.lower() != "file":
            return _SourceRepository("remote-only", None, None)
        converted = url2pathname(parsed.path)
        if os.name == "nt" and converted.startswith("/"):
            converted = converted[1:]
        if parsed.netloc:
            converted = f"//{parsed.netloc}/{converted.lstrip('/')}"
        candidate = Path(converted)
        if not candidate.is_dir():
            return _SourceRepository(None, None, "SOURCE_CHECKOUT_MISSING")
        try:
            candidate = candidate.resolve(strict=True)
        except OSError:
            return _SourceRepository(None, None, "SOURCE_CHECKOUT_MISSING")

        try:
            is_bare = self._git.run(
                _git_read_argv("rev-parse", "--is-bare-repository"), cwd=candidate
            ).stdout.strip()
            is_inside_worktree = self._git.run(
                _git_read_argv("rev-parse", "--is-inside-work-tree"), cwd=candidate
            ).stdout.strip()
            absolute_git_dir = self._git.run(
                _git_read_argv("rev-parse", "--absolute-git-dir"), cwd=candidate
            ).stdout.strip()
        except DomainError:
            return _SourceRepository(None, None, "SOURCE_REPOSITORY_INVALID")

        if not absolute_git_dir:
            return _SourceRepository(None, None, "SOURCE_REPOSITORY_INVALID")
        try:
            Path(absolute_git_dir).resolve(strict=True)
        except OSError:
            return _SourceRepository(None, None, "SOURCE_REPOSITORY_INVALID")
        if is_bare == "true" and is_inside_worktree == "false":
            return _SourceRepository("local-bare", candidate, None)
        if is_bare == "false" and is_inside_worktree == "true":
            return _SourceRepository("local-worktree", candidate, None)
        return _SourceRepository(None, None, "SOURCE_REPOSITORY_INVALID")

    def _source_status(
        self, source: _SourceRepository
    ) -> tuple[str | None, str | None]:
        if source.kind != "local-worktree" or source.path is None:
            return None, None
        try:
            status = self._git.run(
                _git_read_argv("status", "--porcelain"), cwd=source.path
            ).stdout
        except DomainError as error:
            return None, error.code
        return status, None

    @staticmethod
    def _check_source_status(
        source: _SourceRepository,
        before: str | None,
        after: str | None,
        error_before: str | None,
        error_after: str | None,
    ) -> HarnessCheck:
        if source.resolution_error is not None:
            return HarnessCheck(
                id="GIT-WORKSPACE-004",
                status=HarnessStatus.FAILED,
                description="the source checkout status remains unchanged",
                message=(
                    "local source checkout could not be resolved "
                    f"({source.resolution_error})"
                ),
                suggestion="repair repository metadata before running the harness again",
            )
        if source.kind == "remote-only":
            return HarnessCheck(
                id="GIT-WORKSPACE-004",
                status=HarnessStatus.PASSED,
                description="the source checkout status remains unchanged",
                message="no local source checkout is registered",
            )
        if source.kind == "local-bare":
            return HarnessCheck(
                id="GIT-WORKSPACE-004",
                status=HarnessStatus.PASSED,
                description="the source checkout status remains unchanged",
                message=(
                    "local source repository is bare and has no worktree to mutate"
                ),
            )
        if error_before is not None or error_after is not None:
            return HarnessCheck(
                id="GIT-WORKSPACE-004",
                status=HarnessStatus.FAILED,
                description="the source checkout status remains unchanged",
                message="source checkout status could not be captured",
                suggestion="verify the local source checkout before running the harness again",
            )
        if before != after:
            return HarnessCheck(
                id="GIT-WORKSPACE-004",
                status=HarnessStatus.FAILED,
                description="the source checkout status remains unchanged",
                message="source checkout status changed during evidence collection",
                suggestion="preserve both checkouts and inspect the unexpected source changes",
            )
        return HarnessCheck(
            id="GIT-WORKSPACE-004",
            status=HarnessStatus.PASSED,
            description="the source checkout status remains unchanged",
            message="source checkout status is unchanged",
        )

    def _input_summary(
        self,
        manifest: dict[str, JsonValue] | None,
        changed_paths: tuple[bytes, ...],
        evidence: list[dict[str, JsonValue]],
        source: _SourceRepository,
        source_status_before: str | None,
        source_status_after: str | None,
        source_error_before: str | None,
        source_error_after: str | None,
        checks: list[HarnessCheck],
    ) -> list[dict[str, JsonValue]]:
        return [
            {
                "kind": "workspace_manifest",
                "manifest": manifest,
                "check_status": checks[0].status.value,
            },
            {
                "kind": "changed_paths",
                "paths": cast(
                    list[JsonValue],
                    [_json_safe_path(path) for path in changed_paths],
                ),
                "check_status": checks[1].status.value,
            },
            {
                "kind": "git_evidence",
                "files": cast(list[JsonValue], evidence),
                "base_sha": self._workspace.base_sha,
                "check_status": checks[2].status.value,
            },
            {
                "kind": "source_checkout",
                "source_kind": source.kind,
                "local": source.kind in {"local-bare", "local-worktree"},
                "resolution_error": source.resolution_error,
                "status_before": source_status_before,
                "status_after": source_status_after,
                "error_before": source_error_before,
                "error_after": source_error_after,
                "check_status": checks[3].status.value,
            },
        ]

    def _safe_evidence_file(self, path: Path) -> bool:
        try:
            return (
                self.evidence_dir.resolve(strict=True) == self.evidence_dir
                and path.is_file()
                and not path.is_symlink()
                and path.resolve(strict=True).parent == self.evidence_dir
            )
        except OSError:
            return False

    def _run(self, *arguments: str) -> str:
        return self._git.run(
            _git_read_argv(*arguments),
            cwd=self._workspace.path,
            env=self._workspace_git_env,
        ).stdout

    def _run_bytes(self, *arguments: str) -> bytes:
        return self._git.run_bytes(
            _git_read_argv(*arguments),
            cwd=self._workspace.path,
            env=self._workspace_git_env,
        ).stdout

    def _evidence_ref(self, name: str) -> str:
        return (
            f"tasks/{self._workspace.task_id}/runs/"
            f"{self._workspace.run_id}/git/{name}"
        )


def _project_root(workspace_path: Path) -> Path:
    for ancestor in workspace_path.parents:
        if ancestor.name == ".yipilot":
            return ancestor.parent.resolve(strict=True)
    raise DomainError("PATH_DENIED", "workspace is outside a Yipilot project")


def _repo_root(workspace_path: Path) -> Path:
    if workspace_path.parent.name == "workspaces":
        return workspace_path.parents[1]
    return workspace_path.parent


def _manifest_path(workspace: PreparedWorkspace) -> Path:
    if workspace.path.parent.name == "workspaces":
        return workspace.path.parent / f"{workspace.run_id}.json"
    return workspace.path.parent / "workspace-manifest.json"


def _json_mapping(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        return None
    return value


def _fingerprint(inputs: list[dict[str, JsonValue]]) -> str:
    encoded = json.dumps(
        inputs,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _join_git_output(*parts: str) -> str:
    return "".join(part if not part or part.endswith("\n") else f"{part}\n" for part in parts)


def _join_git_bytes(*parts: bytes) -> bytes:
    return b"".join(part if not part or part.endswith(b"\n") else part + b"\n" for part in parts)


def _evidence_value_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    return value.replace("\n", os.linesep).encode("utf-8")


def _parse_git_changed_paths(value: bytes) -> tuple[bytes, ...]:
    try:
        return _parse_nul_paths(value, require_canonical=False)
    except ValueError as error:
        raise DomainError(
            "GIT_FAILED", "git returned malformed NUL-delimited changed paths"
        ) from error


def _serialize_changed_paths(paths: tuple[bytes, ...]) -> bytes:
    if not paths:
        return b""
    if (
        any(not path or b"\0" in path for path in paths)
        or paths != tuple(sorted(set(paths)))
    ):
        raise ValueError("changed paths are not canonical")
    return b"\0".join(paths) + b"\0"


def _parse_changed_paths(value: bytes) -> tuple[bytes, ...]:
    return _parse_nul_paths(value, require_canonical=True)


def _parse_nul_paths(
    value: bytes, *, require_canonical: bool
) -> tuple[bytes, ...]:
    if not value:
        return ()
    if not value.endswith(b"\0"):
        raise ValueError("non-empty changed paths must end with NUL")
    paths = tuple(value[:-1].split(b"\0"))
    if any(not path for path in paths):
        raise ValueError("changed paths cannot contain an empty item")
    if require_canonical and paths != tuple(sorted(set(paths))):
        raise ValueError("changed paths are not sorted and unique")
    return paths


def _display_changed_paths(paths: tuple[bytes, ...]) -> str:
    return "".join(_json_safe_path(path) + "\n" for path in paths)


def _json_safe_path(path: bytes | str) -> str:
    """Return an injective JSON-safe display form without retaining surrogates."""
    decoded = os.fsdecode(path) if isinstance(path, bytes) else path
    return json.dumps(decoded, ensure_ascii=True)


def _git_read_argv(*arguments: str) -> list[str]:
    """Build a Git read command that cannot acquire optional write locks."""
    return [
        "git",
        "--no-optional-locks",
        "-c",
        "core.quotePath=true",
        *arguments,
    ]
