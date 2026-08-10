import json
import os
import subprocess
from pathlib import Path

import pytest

from yipilot.application.repo_service import RepoService
from yipilot.application.workspace_service import PreparedWorkspace, WorkspaceService
from yipilot.harness.git_workspace import GitWorkspaceHarness
from yipilot.harness.models import HarnessCheck, HarnessReport
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        check=True,
    )


def _check(report: HarnessReport, check_id: str) -> HarnessCheck:
    return next(check for check in report.checks if check.id == check_id)


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(source, "add", "tracked.py")
    _git(source, "commit", "-m", "initial")
    return source


@pytest.fixture
def prepared_workspace(
    tmp_path: Path, source_repository: Path
) -> PreparedWorkspace:
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    RepoService(project, GitRunner()).add_and_sync(
        "1001", str(source_repository), "sample"
    )
    return WorkspaceService(project, GitRunner()).prepare(
        "1001", "sample", "run-a"
    )


def test_harness_writes_all_git_evidence(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "changed.py").write_text("x = 1\n", encoding="utf-8")

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    assert report.status == "passed"
    for name in [
        "status.txt",
        "diff.patch",
        "diff-stat.txt",
        "changed-files.txt",
        "head.txt",
        "base.txt",
    ]:
        assert (harness.evidence_dir / name).is_file()


def test_harness_reports_no_changes_as_passed(
    prepared_workspace: PreparedWorkspace,
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)

    report = harness.run()

    assert report.status == "passed"
    assert report.summary == "no_changes"
    assert (harness.evidence_dir / "status.txt").read_text(encoding="utf-8") == ""
    assert (harness.evidence_dir / "changed-files.txt").read_text(
        encoding="utf-8"
    ) == ""


def test_changed_files_contains_tracked_and_untracked_paths(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (prepared_workspace.path / "new.py").write_text("new = True\n", encoding="utf-8")

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    assert report.status == "passed"
    assert (harness.evidence_dir / "changed-files.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["new.py", "tracked.py"]
    assert [check.id for check in report.checks] == [
        "GIT-WORKSPACE-001",
        "GIT-WORKSPACE-002",
        "GIT-WORKSPACE-003",
        "GIT-WORKSPACE-004",
    ]


def test_changed_files_contains_staged_paths(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "tracked.py").write_text("value = 3\n", encoding="utf-8")
    _git(prepared_workspace.path, "add", "tracked.py")

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    assert report.status == "passed"
    assert report.summary == "changes_validated"
    assert (harness.evidence_dir / "changed-files.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["tracked.py"]
    assert "value = 3" in (harness.evidence_dir / "diff.patch").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "evidence_name",
    [
        "status.txt",
        "diff.patch",
        "diff-stat.txt",
        "changed-files.txt",
        "head.txt",
        "base.txt",
    ],
)
def test_missing_required_evidence_is_git_workspace_003_failure(
    prepared_workspace: PreparedWorkspace, evidence_name: str
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    (harness.evidence_dir / evidence_name).unlink()

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-003").status == "failed"


@pytest.mark.parametrize(
    ("manifest_field", "foreign_value"),
    [
        ("task_id", "1002"),
        ("repo_alias", "other"),
        ("repo_hash", "0" * 64),
        ("run_id", "run-b"),
    ],
)
def test_foreign_manifest_is_git_workspace_001_failure(
    prepared_workspace: PreparedWorkspace,
    manifest_field: str,
    foreign_value: str,
) -> None:
    manifest_path = prepared_workspace.path.parent / "workspace-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[manifest_field] = foreign_value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = GitWorkspaceHarness(prepared_workspace).run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-001").status == "failed"


def test_invalid_manifest_encoding_is_git_workspace_001_failure(
    prepared_workspace: PreparedWorkspace,
) -> None:
    manifest_path = prepared_workspace.path.parent / "workspace-manifest.json"
    manifest_path.write_bytes(b"\xff")

    report = GitWorkspaceHarness(prepared_workspace).run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-001").status == "failed"


@pytest.mark.parametrize("unsafe_path", ["../outside.py", "C:/outside.py"])
def test_changed_path_outside_workspace_is_git_workspace_002_failure(
    prepared_workspace: PreparedWorkspace, unsafe_path: str
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    (harness.evidence_dir / "changed-files.txt").write_text(
        f"{unsafe_path}\n", encoding="utf-8"
    )

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-002").status == "failed"


def test_invalid_changed_files_encoding_is_git_workspace_002_failure(
    prepared_workspace: PreparedWorkspace,
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    (harness.evidence_dir / "changed-files.txt").write_bytes(b"\xff")

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-002").status == "failed"


def test_evidence_directory_escape_is_git_workspace_003_failure(
    prepared_workspace: PreparedWorkspace,
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    outside = harness.evidence_dir.parent / "outside-evidence"
    harness.evidence_dir.rename(outside)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(harness.evidence_dir), str(outside)],
            shell=False,
            capture_output=True,
            text=True,
            check=True,
        )
    else:
        harness.evidence_dir.symlink_to(outside, target_is_directory=True)

    try:
        report = harness.run()
    finally:
        if os.name == "nt":
            os.rmdir(harness.evidence_dir)
        else:
            harness.evidence_dir.unlink()
        outside.rename(harness.evidence_dir)

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-003").status == "failed"


def test_source_checkout_status_is_unchanged(
    prepared_workspace: PreparedWorkspace, source_repository: Path
) -> None:
    source_status_before = _git(source_repository, "status", "--porcelain").stdout

    report = GitWorkspaceHarness(prepared_workspace).run()

    assert _git(source_repository, "status", "--porcelain").stdout == source_status_before
    assert _check(report, "GIT-WORKSPACE-004").status == "passed"


def test_source_checkout_change_is_git_workspace_004_failure(
    prepared_workspace: PreparedWorkspace, source_repository: Path
) -> None:
    class SourceMutatingGitRunner(GitRunner):
        changed_source = False

        def run(
            self,
            argv: list[str],
            *,
            cwd: Path,
            timeout: float = 60,
        ) -> subprocess.CompletedProcess[str]:
            if cwd == prepared_workspace.path and not self.changed_source:
                (source_repository / "unexpected.py").write_text(
                    "changed = True\n", encoding="utf-8"
                )
                self.changed_source = True
            return super().run(argv, cwd=cwd, timeout=timeout)

    report = GitWorkspaceHarness(
        prepared_workspace, SourceMutatingGitRunner()
    ).run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-004").status == "failed"


def test_invalid_local_repo_metadata_is_git_workspace_004_failure(
    prepared_workspace: PreparedWorkspace,
) -> None:
    metadata_path = prepared_workspace.path.parent / "metadata.json"
    metadata_path.write_bytes(b"\xff")

    report = GitWorkspaceHarness(prepared_workspace).run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-004").status == "failed"


def test_tampered_local_repo_metadata_cannot_disable_git_workspace_004(
    prepared_workspace: PreparedWorkspace,
) -> None:
    metadata_path = prepared_workspace.path.parent / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["normalized_url"] = "https://example.invalid/repo.git"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = GitWorkspaceHarness(prepared_workspace).run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-004").status == "failed"


def test_report_is_persisted_and_evidence_is_repeatable(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    harness = GitWorkspaceHarness(prepared_workspace)

    first = harness.run()
    first_evidence = {
        path.name: path.read_bytes() for path in sorted(harness.evidence_dir.iterdir())
    }
    second = harness.run()
    persisted = HarnessReport.model_validate_json(
        harness.report_path.read_text(encoding="utf-8")
    )

    assert persisted == second
    assert second.input_fingerprint == first.input_fingerprint
    assert second.checks == first.checks
    assert {
        path.name: path.read_bytes() for path in sorted(harness.evidence_dir.iterdir())
    } == first_evidence
