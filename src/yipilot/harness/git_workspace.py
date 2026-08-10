"""Git workspace evidence collection and deterministic Harness checks."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import cast
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4

from pydantic import JsonValue

from yipilot.application.workspace_service import PreparedWorkspace
from yipilot.domain.errors import DomainError
from yipilot.harness.models import (
    HarnessCheck,
    HarnessReport,
    HarnessStatus,
    aggregate_checks,
)
from yipilot.infrastructure.filesystem import atomic_write_json, project_path
from yipilot.infrastructure.git_runner import GitRunner

_EVIDENCE_NAMES = (
    "status.txt",
    "diff.patch",
    "diff-stat.txt",
    "changed-files.txt",
    "head.txt",
    "base.txt",
)
_HARNESS_NAME = "git_workspace"
_HARNESS_VERSION = "1.2.0"


class GitWorkspaceHarness:
    """Collect and validate the Git evidence for one prepared workspace."""

    def __init__(
        self,
        workspace: PreparedWorkspace,
        git_runner: GitRunner | None = None,
    ) -> None:
        self._workspace = workspace
        self._git = git_runner or GitRunner()
        self._project_root = workspace.path.parents[5].resolve(strict=True)
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
        started_at = datetime.now(UTC)
        source_checkout, source_resolution_error = self._source_checkout()
        source_status_before, source_error_before = self._source_status(source_checkout)

        current_evidence = self._build_evidence()
        if not self.evidence_dir.exists():
            self._persist_evidence(current_evidence)

        manifest, manifest_check = self._check_manifest()
        changed_paths, paths_check = self._check_changed_paths()
        evidence, evidence_check = self._check_evidence(current_evidence)
        source_status_after, source_error_after = self._source_status(source_checkout)
        source_check = self._check_source_status(
            source_checkout,
            source_resolution_error,
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
            source_checkout is not None,
            source_resolution_error,
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
        atomic_write_json(
            self.report_path,
            report.model_dump(mode="json"),
            HarnessReport.model_validate,
        )
        return report

    def _build_evidence(self) -> dict[str, str | bytes]:
        tracked_paths = _split_nul(
            self._run("diff", "--name-only", "-z", self._workspace.base_sha)
        )
        untracked_paths = _split_nul(
            self._run("ls-files", "--others", "--exclude-standard", "-z")
        )
        changed_paths = sorted(
            {path for path in tracked_paths + untracked_paths if path}
        )
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
                os.devnull,
                path,
            )
            for path in sorted(untracked_paths)
            if path
        ]
        untracked_stats = [
            self._run(
                "diff",
                "--no-index",
                "--stat",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                os.devnull,
                path,
            )
            for path in sorted(untracked_paths)
            if path
        ]
        return {
            "status.txt": self._run("status", "--porcelain"),
            "diff.patch": _join_git_bytes(tracked_patch, *untracked_patches),
            "diff-stat.txt": _join_git_output(tracked_stat, *untracked_stats),
            "changed-files.txt": (
                "" if not changed_paths else "\n".join(changed_paths) + "\n"
            ),
            "head.txt": self._run("rev-parse", "HEAD"),
            "base.txt": self._run("rev-parse", self._workspace.base_sha),
        }

    def _persist_evidence(self, evidence: dict[str, str | bytes]) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=False)
        for name in _EVIDENCE_NAMES:
            value = evidence[name]
            if isinstance(value, bytes):
                (self.evidence_dir / name).write_bytes(value)
            else:
                (self.evidence_dir / name).write_text(value, encoding="utf-8")

    def _check_manifest(self) -> tuple[dict[str, JsonValue] | None, HarnessCheck]:
        path = self._workspace.path.parent / "workspace-manifest.json"
        evidence_ref = (
            f"tasks/{self._workspace.task_id}/repos/"
            f"{self._workspace.repo_alias}/workspace-manifest.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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

    def _check_changed_paths(self) -> tuple[list[str], HarnessCheck]:
        path = self.evidence_dir / "changed-files.txt"
        evidence_ref = self._evidence_ref("changed-files.txt")
        if not self._safe_evidence_file(path):
            return [], HarnessCheck(
                id="GIT-WORKSPACE-002",
                status=HarnessStatus.FAILED,
                description="all changed paths remain inside the workspace",
                message="changed paths evidence is missing or outside the evidence directory",
                suggestion="restore the original evidence bundle and run the harness again",
                evidence_refs=[evidence_ref],
            )
        try:
            changed_paths = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return [], HarnessCheck(
                id="GIT-WORKSPACE-002",
                status=HarnessStatus.FAILED,
                description="all changed paths remain inside the workspace",
                message="changed paths cannot be validated because their evidence is missing",
                suggestion="restore the original evidence bundle and run the harness again",
                evidence_refs=[evidence_ref],
            )

        workspace_root = self._workspace.path.resolve(strict=True)
        for changed_path in changed_paths:
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
                    message=f"changed path is outside the workspace: {changed_path}",
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
                message=f"required Git evidence is missing: {', '.join(missing)}",
                suggestion="preserve the incomplete bundle and collect evidence in a new run",
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
                    f"{', '.join(stale)}"
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
            message="all six required Git evidence files match the current workspace",
            evidence_refs=evidence_refs,
        )

    def _source_checkout(self) -> tuple[Path | None, str | None]:
        metadata_path = self._workspace.path.parent / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, "SOURCE_METADATA_INVALID"
        if not isinstance(metadata, dict):
            return None, "SOURCE_METADATA_INVALID"
        normalized_url = metadata.get("normalized_url")
        if not isinstance(normalized_url, str):
            return None, "SOURCE_METADATA_INVALID"
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
            return None, "SOURCE_METADATA_MISMATCH"
        parsed = urlsplit(normalized_url)
        if parsed.scheme.lower() != "file":
            return None, None
        converted = url2pathname(parsed.path)
        if os.name == "nt" and converted.startswith("/"):
            converted = converted[1:]
        if parsed.netloc:
            converted = f"//{parsed.netloc}/{converted.lstrip('/')}"
        candidate = Path(converted)
        if not candidate.is_dir() or not (candidate / ".git").exists():
            return None, "SOURCE_CHECKOUT_MISSING"
        try:
            return candidate.resolve(strict=True), None
        except OSError:
            return None, "SOURCE_CHECKOUT_MISSING"

    def _source_status(
        self, source_checkout: Path | None
    ) -> tuple[str | None, str | None]:
        if source_checkout is None:
            return None, None
        try:
            status = self._git.run(
                ["git", "status", "--porcelain"], cwd=source_checkout
            ).stdout
        except DomainError as error:
            return None, error.code
        return status, None

    @staticmethod
    def _check_source_status(
        source_checkout: Path | None,
        resolution_error: str | None,
        before: str | None,
        after: str | None,
        error_before: str | None,
        error_after: str | None,
    ) -> HarnessCheck:
        if resolution_error is not None:
            return HarnessCheck(
                id="GIT-WORKSPACE-004",
                status=HarnessStatus.FAILED,
                description="the source checkout status remains unchanged",
                message=f"local source checkout could not be resolved ({resolution_error})",
                suggestion="repair repository metadata before running the harness again",
            )
        if source_checkout is None:
            return HarnessCheck(
                id="GIT-WORKSPACE-004",
                status=HarnessStatus.PASSED,
                description="the source checkout status remains unchanged",
                message="no local source checkout is registered",
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
        changed_paths: list[str],
        evidence: list[dict[str, JsonValue]],
        has_local_source_checkout: bool,
        source_resolution_error: str | None,
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
                "paths": cast(list[JsonValue], changed_paths),
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
                "local": has_local_source_checkout,
                "resolution_error": source_resolution_error,
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
            ["git", *arguments], cwd=self._workspace.path
        ).stdout

    def _run_bytes(self, *arguments: str) -> bytes:
        return self._git.run_bytes(
            ["git", *arguments], cwd=self._workspace.path
        ).stdout

    def _evidence_ref(self, name: str) -> str:
        return (
            f"tasks/{self._workspace.task_id}/runs/"
            f"{self._workspace.run_id}/git/{name}"
        )


def _json_mapping(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        return None
    return value


def _fingerprint(inputs: list[dict[str, JsonValue]]) -> str:
    encoded = json.dumps(
        inputs,
        ensure_ascii=False,
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


def _split_nul(value: str) -> list[str]:
    return [part for part in value.split("\0") if part]
