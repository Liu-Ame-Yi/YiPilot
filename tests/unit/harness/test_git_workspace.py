import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from yipilot.application.repo_service import RepoService
from yipilot.application.workspace_service import PreparedWorkspace, WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.harness import git_workspace as git_workspace_module
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


def _evidence_bytes(harness: GitWorkspaceHarness) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes() for path in sorted(harness.evidence_dir.iterdir())
    }


def _repository_state(repository: Path) -> tuple[str, str]:
    return (
        _git(repository, "rev-parse", "HEAD").stdout,
        _git(
            repository,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ).stdout,
    )


def _retarget_source(
    workspace: PreparedWorkspace, normalized_url: str
) -> PreparedWorkspace:
    repo_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    metadata_path = workspace.path.parent / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["normalized_url"] = normalized_url
    metadata["url_hash"] = repo_hash
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    manifest_path = workspace.path.parent / "workspace-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["repo_hash"] = repo_hash
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return replace(workspace, repo_hash=repo_hash)


class _RecordingGitRunner(GitRunner):
    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.text_argv: list[list[str]] = []
        self.bytes_argv: list[list[str]] = []
        self.calls: list[tuple[list[str], Path]] = []

    def run(
        self, argv: list[str], *, cwd: Path, timeout: float = 60
    ) -> subprocess.CompletedProcess[str]:
        self.argv.append(argv)
        self.text_argv.append(argv)
        self.calls.append((argv, cwd))
        return super().run(argv, cwd=cwd, timeout=timeout)

    def run_bytes(
        self, argv: list[str], *, cwd: Path, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        self.argv.append(argv)
        self.bytes_argv.append(argv)
        self.calls.append((argv, cwd))
        return super().run_bytes(argv, cwd=cwd, timeout=timeout)


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
        "changed-files.nul",
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
    assert (harness.evidence_dir / "changed-files.nul").read_bytes() == b""


def test_changed_path_nul_round_trip_preserves_embedded_newline() -> None:
    paths = (b"dir/first\nsecond.py",)

    serialized = git_workspace_module._serialize_changed_paths(paths)

    assert serialized == b"dir/first\nsecond.py\0"
    assert git_workspace_module._parse_changed_paths(serialized) == paths
    assert len(git_workspace_module._parse_changed_paths(serialized)) == 1
    assert git_workspace_module._serialize_changed_paths(()) == b""
    assert git_workspace_module._parse_changed_paths(b"") == ()


def test_changed_files_display_escapes_all_control_bytes() -> None:
    path = b"dir/new\ncarriage\rtab\tcontrol\x01delete\x7f.py"

    display = git_workspace_module._display_changed_paths((path,))

    assert display == json.dumps(os.fsdecode(path), ensure_ascii=True) + "\n"
    assert display.count("\n") == 1
    assert "\\n" in display
    assert "\\r" in display
    assert "\\t" in display
    assert "\\u0001" in display
    assert "\\u007f" in display


def test_changed_path_commands_preserve_nul_delimited_bytes(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "new.py").write_text("new = True\n", encoding="utf-8")
    git_runner = _RecordingGitRunner()

    report = GitWorkspaceHarness(prepared_workspace, git_runner).run()

    assert report.status == "passed"
    assert [
        "git",
        "diff",
        "--name-only",
        "-z",
        prepared_workspace.base_sha,
    ] in git_runner.bytes_argv
    assert [
        "git",
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ] in git_runner.bytes_argv


def test_clean_evidence_becomes_stale_after_untracked_file_is_added(
    prepared_workspace: PreparedWorkspace,
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    first = harness.run()
    original_evidence = _evidence_bytes(harness)
    (prepared_workspace.path / "new.py").write_text("new = True\n", encoding="utf-8")
    index_before = _git(
        prepared_workspace.path, "diff", "--cached", "--binary"
    ).stdout

    second = harness.run()

    assert first.status == "passed"
    assert first.summary == "no_changes"
    assert second.status == "failed"
    assert second.summary != "no_changes"
    assert _check(second, "GIT-WORKSPACE-003").status == "failed"
    assert "stale" in _check(second, "GIT-WORKSPACE-003").message.lower()
    assert _evidence_bytes(harness) == original_evidence
    assert (
        _git(prepared_workspace.path, "diff", "--cached", "--binary").stdout
        == index_before
    )


def test_evidence_becomes_stale_when_tracked_file_changes_again_with_same_status(
    prepared_workspace: PreparedWorkspace,
) -> None:
    tracked = prepared_workspace.path / "tracked.py"
    tracked.write_text("value = 2\n", encoding="utf-8")
    harness = GitWorkspaceHarness(prepared_workspace)
    first = harness.run()
    original_evidence = _evidence_bytes(harness)
    status_before = _git(prepared_workspace.path, "status", "--porcelain").stdout
    tracked.write_text("value = 3\n", encoding="utf-8")

    second = harness.run()

    assert first.status == "passed"
    assert _git(prepared_workspace.path, "status", "--porcelain").stdout == status_before
    assert second.status == "failed"
    assert _check(second, "GIT-WORKSPACE-003").status == "failed"
    assert "stale" in _check(second, "GIT-WORKSPACE-003").message.lower()
    assert _evidence_bytes(harness) == original_evidence


def test_precreated_empty_evidence_bundle_is_rejected(
    prepared_workspace: PreparedWorkspace,
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    harness.evidence_dir.mkdir(parents=True)
    for name in [
        "status.txt",
        "diff.patch",
        "diff-stat.txt",
        "changed-files.txt",
        "head.txt",
        "base.txt",
    ]:
        (harness.evidence_dir / name).touch()
    original_evidence = _evidence_bytes(harness)

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-003").status == "failed"
    evidence_message = _check(report, "GIT-WORKSPACE-003").message.lower()
    assert "incomplete" in evidence_message or "legacy" in evidence_message
    assert "changed-files.nul" in evidence_message
    assert _evidence_bytes(harness) == original_evidence


def test_unchanged_clean_workspace_revalidation_is_repeatable(
    prepared_workspace: PreparedWorkspace,
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)

    first = harness.run()
    original_evidence = _evidence_bytes(harness)
    index_before = _git(
        prepared_workspace.path, "diff", "--cached", "--binary"
    ).stdout
    second = harness.run()

    assert first.status == "passed"
    assert first.summary == "no_changes"
    assert second.status == "passed"
    assert second.summary == "no_changes"
    assert second.input_fingerprint == first.input_fingerprint
    assert second.checks == first.checks
    assert _evidence_bytes(harness) == original_evidence
    assert (
        _git(prepared_workspace.path, "diff", "--cached", "--binary").stdout
        == index_before
    )


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
    ).splitlines() == ['"new.py"', '"tracked.py"']
    assert (harness.evidence_dir / "changed-files.nul").read_bytes() == (
        b"new.py\0tracked.py\0"
    )
    assert [check.id for check in report.checks] == [
        "GIT-WORKSPACE-001",
        "GIT-WORKSPACE-002",
        "GIT-WORKSPACE-003",
        "GIT-WORKSPACE-004",
    ]


def test_untracked_text_is_appended_to_patch_without_index_side_effect(
    prepared_workspace: PreparedWorkspace,
) -> None:
    untracked = prepared_workspace.path / "new.py"
    untracked.write_text("new = True\n", encoding="utf-8")
    content_before = untracked.read_bytes()
    status_before = _git(prepared_workspace.path, "status", "--porcelain").stdout
    index_before = _git(prepared_workspace.path, "diff", "--cached", "--binary").stdout

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    patch = (harness.evidence_dir / "diff.patch").read_text(encoding="utf-8")
    stat = (harness.evidence_dir / "diff-stat.txt").read_text(encoding="utf-8")
    assert report.status == "passed"
    assert "new.py" in patch
    assert "+new = True" in patch
    assert "new.py" in stat
    assert _git(prepared_workspace.path, "status", "--porcelain").stdout == status_before
    assert (
        _git(prepared_workspace.path, "diff", "--cached", "--binary").stdout
        == index_before
    )
    assert untracked.read_bytes() == content_before


def test_untracked_binary_is_appended_as_git_binary_patch(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "image.bin").write_bytes(bytes(range(256)))

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    patch = (harness.evidence_dir / "diff.patch").read_text(encoding="utf-8")
    assert report.status == "passed"
    assert "image.bin" in patch
    assert "GIT binary patch" in patch


def test_untracked_empty_file_is_represented_in_patch(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "empty.txt").touch()

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    patch = (harness.evidence_dir / "diff.patch").read_text(encoding="utf-8")
    assert report.status == "passed"
    assert "empty.txt" in patch
    assert "new file mode" in patch


def test_untracked_patch_is_sorted_and_respects_git_ignores(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / ".gitignore").write_text(
        "ignored.log\n", encoding="utf-8"
    )
    (prepared_workspace.path / "z-last.py").write_text("z = 1\n", encoding="utf-8")
    (prepared_workspace.path / "a-first.py").write_text("a = 1\n", encoding="utf-8")
    (prepared_workspace.path / "ignored.log").write_text(
        "secret = True\n", encoding="utf-8"
    )

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    patch = (harness.evidence_dir / "diff.patch").read_text(encoding="utf-8")
    changed_files = (harness.evidence_dir / "changed-files.txt").read_text(
        encoding="utf-8"
    )
    assert report.status == "passed"
    assert patch.index("a-first.py") < patch.index("z-last.py")
    assert "diff --git a/ignored.log b/ignored.log" not in patch
    assert "+secret = True" not in patch
    assert "ignored.log" not in changed_files


def test_untracked_unicode_path_is_captured_without_git_quoting(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "新文件.py").write_text(
        "message = '你好'\n", encoding="utf-8"
    )

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    patch = (harness.evidence_dir / "diff.patch").read_text(encoding="utf-8")
    changed_files = (harness.evidence_dir / "changed-files.txt").read_text(
        encoding="utf-8"
    )
    assert report.status == "passed"
    assert "+message = '你好'" in patch
    assert changed_files == json.dumps("新文件.py", ensure_ascii=True) + "\n"


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows filenames cannot contain an embedded newline",
)
def test_posix_newline_path_is_one_nul_evidence_item(
    prepared_workspace: PreparedWorkspace,
) -> None:
    directory = prepared_workspace.path / "dir"
    directory.mkdir()
    relative_path = "dir/first\nsecond.py"
    (prepared_workspace.path / relative_path).write_text(
        "newline = True\n", encoding="utf-8"
    )
    index_before = _git(
        prepared_workspace.path, "diff", "--cached", "--binary"
    ).stdout

    harness = GitWorkspaceHarness(prepared_workspace)
    report = harness.run()

    changed_nul = (harness.evidence_dir / "changed-files.nul").read_bytes()
    changed_text = (harness.evidence_dir / "changed-files.txt").read_text(
        encoding="utf-8"
    )
    patch = (harness.evidence_dir / "diff.patch").read_bytes()
    assert report.status == "passed"
    assert changed_nul == os.fsencode(relative_path) + b"\0"
    assert git_workspace_module._parse_changed_paths(changed_nul) == (
        os.fsencode(relative_path),
    )
    assert changed_text == json.dumps(relative_path, ensure_ascii=False) + "\n"
    assert "\\n" in changed_text
    assert b"first\\nsecond.py" in patch
    assert b"+newline = True" in patch
    assert (
        _git(prepared_workspace.path, "diff", "--cached", "--binary").stdout
        == index_before
    )


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows filenames cannot contain an embedded newline",
)
def test_posix_newline_path_evidence_is_repeatable(
    prepared_workspace: PreparedWorkspace,
) -> None:
    directory = prepared_workspace.path / "dir"
    directory.mkdir()
    (directory / "first\nsecond.py").write_text("value = 1\n", encoding="utf-8")
    harness = GitWorkspaceHarness(prepared_workspace)

    first = harness.run()
    original_evidence = _evidence_bytes(harness)
    second = harness.run()

    assert first.status == "passed"
    assert second.status == "passed"
    assert second.input_fingerprint == first.input_fingerprint
    assert second.checks == first.checks
    assert _evidence_bytes(harness) == original_evidence


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows filenames cannot contain an embedded newline",
)
def test_posix_newline_path_subsequent_change_is_stale_without_overwrite(
    prepared_workspace: PreparedWorkspace,
) -> None:
    directory = prepared_workspace.path / "dir"
    directory.mkdir()
    changed = directory / "first\nsecond.py"
    changed.write_text("value = 1\n", encoding="utf-8")
    harness = GitWorkspaceHarness(prepared_workspace)
    first = harness.run()
    original_evidence = _evidence_bytes(harness)
    changed.write_text("value = 2\n", encoding="utf-8")

    second = harness.run()

    assert first.status == "passed"
    assert second.status == "failed"
    evidence_check = _check(second, "GIT-WORKSPACE-003")
    assert evidence_check.status == "failed"
    assert "stale" in evidence_check.message.lower()
    assert _evidence_bytes(harness) == original_evidence


def test_patch_collection_disables_external_diff_and_textconv(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "new.py").write_text("new = True\n", encoding="utf-8")
    git_runner = _RecordingGitRunner()

    report = GitWorkspaceHarness(prepared_workspace, git_runner).run()

    patch_commands = [
        argv for argv in git_runner.argv if argv[1] == "diff" and "--binary" in argv
    ]
    assert report.status == "passed"
    assert patch_commands
    assert all("--no-ext-diff" in argv for argv in patch_commands)
    assert all("--no-textconv" in argv for argv in patch_commands)


def test_untracked_patch_passes_git_apply_check(
    prepared_workspace: PreparedWorkspace,
) -> None:
    untracked = prepared_workspace.path / "new.py"
    untracked.write_text("new = True\n", encoding="utf-8")
    harness = GitWorkspaceHarness(prepared_workspace)

    report = harness.run()
    untracked.unlink()
    result = _git(
        prepared_workspace.path,
        "apply",
        "--check",
        str(harness.evidence_dir / "diff.patch"),
    )

    assert report.status == "passed"
    assert result.returncode == 0


def test_untracked_non_utf8_text_is_preserved_in_patch(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "legacy.txt").write_bytes(b"\x80\x81\n")
    harness = GitWorkspaceHarness(prepared_workspace)

    report = harness.run()

    patch = (harness.evidence_dir / "diff.patch").read_bytes()
    assert report.status == "passed"
    assert b"+\x80\x81" in patch


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
    ).splitlines() == ['"tracked.py"']
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
        "changed-files.nul",
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


@pytest.mark.parametrize(
    "unsafe_path",
    [b"../outside.py\0", b"/outside.py\0", b"C:/outside.py\0"],
)
def test_unsafe_nul_changed_path_is_git_workspace_002_failure(
    prepared_workspace: PreparedWorkspace, unsafe_path: bytes
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    (harness.evidence_dir / "changed-files.nul").write_bytes(unsafe_path)

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-002").status == "failed"


@pytest.mark.parametrize(
    "malformed",
    [
        b"safe.py",
        b"safe.py\0\0",
        b"z.py\0a.py\0",
        b"same.py\0same.py\0",
    ],
)
def test_malformed_changed_files_nul_is_git_workspace_002_failure(
    prepared_workspace: PreparedWorkspace, malformed: bytes
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    (harness.evidence_dir / "changed-files.nul").write_bytes(malformed)

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-002").status == "failed"


def test_changed_files_text_is_display_only(
    prepared_workspace: PreparedWorkspace,
) -> None:
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    (harness.evidence_dir / "changed-files.txt").write_bytes(b"\xff")

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-002").status == "passed"
    assert _check(report, "GIT-WORKSPACE-003").status == "failed"


def test_changed_files_nul_tampering_is_detected_by_freshness(
    prepared_workspace: PreparedWorkspace,
) -> None:
    (prepared_workspace.path / "new.py").write_text("new = True\n", encoding="utf-8")
    harness = GitWorkspaceHarness(prepared_workspace)
    assert harness.run().status == "passed"
    original_evidence = _evidence_bytes(harness)
    (harness.evidence_dir / "changed-files.nul").write_bytes(b"other.py\0")

    report = harness.run()

    assert report.status == "failed"
    assert _check(report, "GIT-WORKSPACE-003").status == "failed"
    assert "changed-files.nul" in _check(report, "GIT-WORKSPACE-003").message
    expected_evidence = dict(original_evidence)
    expected_evidence["changed-files.nul"] = b"other.py\0"
    assert _evidence_bytes(harness) == expected_evidence


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


def test_bare_local_source_is_accepted_without_mutating_refs_or_index(
    tmp_path: Path, source_repository: Path
) -> None:
    bare_remote = tmp_path / "source.git"
    _git(tmp_path, "clone", "--bare", str(source_repository), str(bare_remote))
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    git_runner = _RecordingGitRunner()
    RepoService(project, git_runner).add_and_sync(
        "1001", bare_remote.as_uri(), "sample"
    )
    workspace = WorkspaceService(project, git_runner).prepare(
        "1001", "sample", "run-a"
    )
    source_before = _repository_state(bare_remote)
    index_before = _git(workspace.path, "diff", "--cached", "--binary").stdout
    git_runner.calls.clear()

    report = GitWorkspaceHarness(workspace, git_runner).run()

    assert _repository_state(bare_remote) == source_before
    assert _git(workspace.path, "diff", "--cached", "--binary").stdout == index_before
    source_check = _check(report, "GIT-WORKSPACE-004")
    assert source_check.status == "passed", source_check.message
    assert (
        source_check.message
        == "local source repository is bare and has no worktree to mutate"
    )
    source_input = next(item for item in report.inputs if item["kind"] == "source_checkout")
    assert source_input["source_kind"] == "local-bare"
    assert not any(
        argv == ["git", "status", "--porcelain"] and cwd == bare_remote.resolve()
        for argv, cwd in git_runner.calls
    )
    assert report.status == "passed"


def test_linked_worktree_source_is_classified_as_local_worktree(
    tmp_path: Path, source_repository: Path
) -> None:
    linked_source = tmp_path / "linked-source"
    _git(
        source_repository,
        "worktree",
        "add",
        "--detach",
        str(linked_source),
        "HEAD",
    )
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    git_runner = GitRunner()
    RepoService(project, git_runner).add_and_sync(
        "1001", linked_source.as_uri(), "sample"
    )
    workspace = WorkspaceService(project, git_runner).prepare(
        "1001", "sample", "run-a"
    )
    source_status_before = _git(linked_source, "status", "--porcelain").stdout

    report = GitWorkspaceHarness(workspace, git_runner).run()

    assert _git(linked_source, "status", "--porcelain").stdout == source_status_before
    assert _check(report, "GIT-WORKSPACE-004").status == "passed"
    source_input = next(item for item in report.inputs if item["kind"] == "source_checkout")
    assert source_input["source_kind"] == "local-worktree"
    assert report.status == "passed"


def test_remote_source_is_classified_as_remote_only(
    prepared_workspace: PreparedWorkspace,
) -> None:
    workspace = _retarget_source(
        prepared_workspace, "https://example.invalid/repo.git"
    )

    report = GitWorkspaceHarness(workspace).run()

    assert _check(report, "GIT-WORKSPACE-004").status == "passed"
    source_input = next(item for item in report.inputs if item["kind"] == "source_checkout")
    assert source_input["source_kind"] == "remote-only"
    assert report.status == "passed"


def test_non_git_local_source_is_git_workspace_004_failure(
    tmp_path: Path, prepared_workspace: PreparedWorkspace
) -> None:
    ordinary_directory = tmp_path / "ordinary-directory"
    ordinary_directory.mkdir()
    workspace = _retarget_source(prepared_workspace, ordinary_directory.as_uri())

    report = GitWorkspaceHarness(workspace).run()

    source_check = _check(report, "GIT-WORKSPACE-004")
    assert source_check.status == "failed"
    assert "SOURCE_REPOSITORY_INVALID" in source_check.message
    assert report.status == "failed"


def test_source_classification_git_error_is_mapped_to_safe_failure(
    prepared_workspace: PreparedWorkspace, source_repository: Path
) -> None:
    class SourceClassificationFailingGitRunner(GitRunner):
        def run(
            self,
            argv: list[str],
            *,
            cwd: Path,
            timeout: float = 60,
        ) -> subprocess.CompletedProcess[str]:
            if cwd == source_repository and argv == [
                "git",
                "rev-parse",
                "--is-bare-repository",
            ]:
                raise DomainError("GIT_FAILED", "unsafe repository detail")
            return super().run(argv, cwd=cwd, timeout=timeout)

    report = GitWorkspaceHarness(
        prepared_workspace, SourceClassificationFailingGitRunner()
    ).run()

    source_check = _check(report, "GIT-WORKSPACE-004")
    assert source_check.status == "failed"
    assert "SOURCE_REPOSITORY_INVALID" in source_check.message
    assert "unsafe repository detail" not in source_check.message
    assert report.status == "failed"


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
