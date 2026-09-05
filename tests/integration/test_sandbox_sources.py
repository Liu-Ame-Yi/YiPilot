import hashlib
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from yipilot.application import sandbox_sources as module
from yipilot.application.repo_service import RepoService
from yipilot.application.sandbox_sources import SandboxSourceService
from yipilot.domain.errors import DomainError
from yipilot.domain.sandbox import source_path
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore


def git(root: Path, *argv: str) -> str:
    return GitRunner().run(["git", *argv], cwd=root).stdout.strip()


@pytest.fixture
def setup(tmp_path: Path):
    source = tmp_path / "repo"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "app.py").write_bytes(b"value = 1\n")
    (source / ".gitignore").write_bytes(b".env\n.venv/\nignored.txt\n")
    for name in (".env", "ignored.txt", "untracked.txt"):
        (source / name).write_text("local secret", encoding="utf-8")
    (source / ".venv").mkdir()
    (source / ".venv/secret").write_text("secret", encoding="utf-8")
    (source / ".yipilot").mkdir()
    (source / ".yipilot/settings.json").write_text("secret", encoding="utf-8")
    git(source, "add", "app.py", ".gitignore", ".yipilot")
    git(source, "commit", "-m", "base")
    base = git(source, "rev-parse", "HEAD")
    (source / "app.py").write_bytes(b"value = 2\n")
    git(source, "add", "app.py")
    git(source, "commit", "-m", "later")
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    task = store.create_task("1")
    op = store.create_operation("1", "sandbox", "key", {})
    run = store.create_run("1", op.operation_id, "sandbox")
    metadata = RepoService(project, GitRunner()).add_and_sync("1", str(source), "repo")
    return SimpleNamespace(
        source=source,
        project=project,
        store=store,
        task=task,
        run=run,
        base=base,
        cache=metadata.cache_path,
        root=store.event_journal_path(run.run_id).parent,
        service=SandboxSourceService(project, GitRunner()),
    )


def prepare(s):
    return s.service.prepare(s.task, "repo", s.run, s.base)


def assert_clean(s) -> None:
    assert not (s.root / "baseline").exists()
    assert not (s.root / "execution").exists()
    assert not (s.root / "source-manifest.json").exists()


def test_frozen_commit_export_independent_copies_and_manifest(setup) -> None:
    s = setup
    copies = prepare(s)
    manifest_bytes = copies.manifest_path.read_bytes()
    assert copies.baseline == s.root / "baseline"
    assert copies.execution == s.root / "execution"
    for tree in (copies.baseline, copies.execution):
        assert sorted(p.name for p in tree.iterdir()) == [".gitignore", "app.py"]
        assert (tree / "app.py").read_bytes() == b"value = 1\n"
    assert not os.path.samefile(copies.baseline / "app.py", copies.execution / "app.py")
    assert copies.manifest.base_sha == s.base
    assert copies.manifest.operation_id == s.run.operation_id
    assert copies.manifest.repository_alias == "repo"
    assert copies.manifest_hash == hashlib.sha256(manifest_bytes).hexdigest()
    (copies.execution / "app.py").write_bytes(b"value = 3\n")
    s.service.validate(copies)
    assert (copies.baseline / "app.py").read_bytes() == b"value = 1\n"
    assert copies.manifest_path.read_bytes() == manifest_bytes
    assert (s.source / "app.py").read_bytes() == b"value = 2\n"


@pytest.mark.parametrize(
    "base", ["HEAD", "../main", "a" * 39, "f" * 40, "a" * 40 + "\n"]
)
def test_invalid_commit_leaves_no_copies(setup, base: str) -> None:
    with pytest.raises(DomainError, match="SANDBOX_BASE_INVALID"):
        setup.service.prepare(setup.task, "repo", setup.run, base)
    assert_clean(setup)


@pytest.mark.parametrize("name", ["baseline", "execution", "source-manifest.json"])
def test_directory_conflicts_are_not_overwritten(setup, name: str) -> None:
    conflict = setup.root / name
    conflict.mkdir()
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        prepare(setup)
    assert conflict.is_dir()
    assert list(conflict.iterdir()) == []
    if name != "execution":
        assert not (setup.root / "execution").exists()


def test_completed_run_copies_are_never_reused(setup) -> None:
    copies = prepare(setup)
    before = copies.manifest_path.read_bytes()
    with pytest.raises(DomainError, match="SANDBOX_CONFLICT"):
        prepare(setup)
    assert copies.manifest_path.read_bytes() == before


def test_manifest_write_failure_cleans_both_copies(setup, monkeypatch) -> None:
    def fail(path, *args):
        path.write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(module, "atomic_write_json", fail)
    with pytest.raises(DomainError, match="SANDBOX_COPY_FAILED"):
        prepare(setup)
    assert_clean(setup)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "C:/absolute",
        "C:relative",
        "a/../b",
        "a\\b",
        "a//b",
        "a/./b",
        "a\nname",
        "name:stream",
        "NUL.txt",
        "aux",
        "a.",
        "a /b",
    ],
)
def test_unsafe_export_paths_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        source_path(path)


@pytest.mark.parametrize(
    ("mode", "path"),
    [
        ("120000", "link"),
        ("160000", "submodule"),
        ("100644", "../escape"),
        ("100644", "/absolute"),
        ("100644", "NUL.txt"),
    ],
)
def test_unsafe_git_entries_fail_before_materialization(
    setup, monkeypatch, mode, path
) -> None:
    real = setup.service._git.run_bytes

    def injected(argv, **kwargs):
        if argv[1] == "ls-tree":
            return subprocess.CompletedProcess(
                argv, 0, f"{mode} blob {'a' * 40}\t{path}\0".encode(), b""
            )
        return real(argv, **kwargs)

    monkeypatch.setattr(setup.service._git, "run_bytes", injected)
    with pytest.raises(DomainError):
        prepare(setup)
    assert_clean(setup)


def test_git_committed_symlink_is_rejected_without_host_symlink_permission(
    setup,
) -> None:
    # Git index fixture creates an actual committed symlink, never a host link.
    oid = git(setup.source, "hash-object", "-w", "app.py")
    git(setup.source, "update-index", "--add", "--cacheinfo", f"120000,{oid},link")
    git(setup.source, "commit", "-m", "link")
    git(setup.cache, "fetch", "origin")
    setup.base = git(setup.source, "rev-parse", "HEAD")
    with pytest.raises(DomainError, match="SANDBOX_SOURCE_DENIED"):
        prepare(setup)
    assert_clean(setup)


def test_reparse_point_rejected_during_validation(setup, monkeypatch) -> None:
    copies = prepare(setup)
    target = copies.execution / "app.py"
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_nlink=1,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        setup.service.validate(copies)


def test_execution_symlink_is_rejected(setup) -> None:
    copies = prepare(setup)
    link = copies.execution / "link"
    try:
        link.symlink_to(copies.baseline, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"host symlink privilege unavailable: {error}")
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        setup.service.validate(copies)


@pytest.mark.parametrize("target", ["baseline", "manifest_path"])
def test_frozen_evidence_drift_is_rejected(setup, target) -> None:
    copies = prepare(setup)
    path = copies.baseline / "app.py" if target == "baseline" else copies.manifest_path
    path.write_bytes(b"tampered\n")
    with pytest.raises(DomainError, match="SANDBOX_MANIFEST_INVALID"):
        setup.service.validate(copies)


def test_no_docker_or_repository_execution_during_preparation(
    setup, monkeypatch
) -> None:
    actual = subprocess.run
    calls = []

    def guarded(argv, **kwargs):
        assert argv[:2] in (
            ["git", "rev-parse"],
            ["git", "ls-tree"],
            ["git", "cat-file"],
            ["git", "ls-files"],
        )
        assert kwargs["shell"] is False
        calls.append(argv)
        return actual(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    prepare(setup)
    assert calls


def test_cross_run_association_is_rejected(setup) -> None:
    wrong = setup.run.model_copy(update={"operation_id": "other"})
    with pytest.raises(DomainError, match="SANDBOX_ASSOCIATION_INVALID"):
        setup.service.prepare(setup.task, "repo", wrong, setup.base)
    assert_clean(setup)


@pytest.mark.parametrize("content", [b"binary\x00file", b"\xff\xfe"])
def test_unpatchable_blob_rejected_and_cleaned(setup, content) -> None:
    (setup.source / "bad.bin").write_bytes(content)
    git(setup.source, "add", "bad.bin")
    git(setup.source, "commit", "-m", "binary")
    git(setup.cache, "fetch", "origin")
    setup.base = git(setup.source, "rev-parse", "HEAD")
    with pytest.raises(DomainError, match="SANDBOX_SOURCE_DENIED"):
        prepare(setup)
    assert_clean(setup)


def test_force_tracked_private_configuration_is_excluded(setup) -> None:
    git(setup.source, "add", "-f", ".env", ".venv", "ignored.txt")
    git(setup.source, "commit", "-m", "private files")
    git(setup.cache, "fetch", "origin")
    setup.base = git(setup.source, "rev-parse", "HEAD")
    copies = prepare(setup)
    for tree in (copies.baseline, copies.execution):
        assert sorted(p.name for p in tree.iterdir()) == [".gitignore", "app.py"]


def test_replace_refs_cannot_change_frozen_source(setup) -> None:
    later = git(setup.cache, "rev-parse", "HEAD")
    git(setup.cache, "replace", setup.base, later)
    copies = prepare(setup)
    assert (copies.baseline / "app.py").read_bytes() == b"value = 1\n"


def test_reparse_point_rejected_before_creating_copies(setup, monkeypatch) -> None:
    target = setup.root / "execution"
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_nlink=1,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        prepare(setup)
    assert_clean(setup)


def test_hardlink_cannot_alias_baseline(setup) -> None:
    copies = prepare(setup)
    os.link(copies.baseline / "app.py", copies.execution / "alias.py")
    with pytest.raises(DomainError, match="SANDBOX_PATH_DENIED"):
        setup.service.validate(copies)
