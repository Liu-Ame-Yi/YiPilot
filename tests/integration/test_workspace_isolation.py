import json
import subprocess
from pathlib import Path
from typing import Self

import pytest

from yipilot.application import workspace_service as workspace_service_module
from yipilot.application.repo_service import RepoService
from yipilot.application.workspace_service import WorkspaceService
from yipilot.domain.errors import DomainError
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
def prepared_project(tmp_path: Path, source_repository: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    store.create_task("1002")
    service = RepoService(project, GitRunner())
    service.add_and_sync("1001", str(source_repository), "sample")
    service.add_and_sync("1002", str(source_repository), "sample")
    return project


def test_task_worktrees_are_distinct_and_do_not_change_source_checkout(
    prepared_project: Path, source_repository: Path
) -> None:
    service = WorkspaceService(prepared_project, GitRunner())

    first = service.prepare("1001", "sample", "run-a")
    second = service.prepare("1002", "sample", "run-b")
    (first.path / "changed.py").write_text("x = 1\n", encoding="utf-8")

    assert first.path != second.path
    assert first.path == (
        prepared_project
        / ".yipilot"
        / "tasks"
        / "1001"
        / "repos"
        / "sample"
        / "workspace"
    )
    assert second.path == (
        prepared_project
        / ".yipilot"
        / "tasks"
        / "1002"
        / "repos"
        / "sample"
        / "workspace"
    )
    assert _git(first.path, "branch", "--show-current").stdout.strip() == (
        "yipilot/task-1001/run-a"
    )
    assert _git(second.path, "branch", "--show-current").stdout.strip() == (
        "yipilot/task-1002/run-b"
    )
    assert _git(source_repository, "branch", "--show-current").stdout.strip() == "main"
    assert _git(source_repository, "status", "--porcelain").stdout == ""


def test_workspace_manifest_records_portable_identity_and_base(
    prepared_project: Path,
) -> None:
    workspace = WorkspaceService(prepared_project, GitRunner()).prepare(
        "1001", "sample", "run-a"
    )
    manifest_path = (
        prepared_project
        / ".yipilot"
        / "tasks"
        / "1001"
        / "repos"
        / "sample"
        / "workspace-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest == {
        "base_sha": workspace.base_sha,
        "branch": "yipilot/task-1001/run-a",
        "created_at": workspace.created_at,
        "repo_alias": "sample",
        "repo_hash": workspace.repo_hash,
        "run_id": "run-a",
        "schema_version": 1,
        "task_id": "1001",
        "workspace_path": ".yipilot/tasks/1001/repos/sample/workspace",
    }
    assert not Path(manifest["workspace_path"]).is_absolute()
    assert workspace.base_sha == _git(workspace.path, "rev-parse", "HEAD").stdout.strip()


def test_workspace_lock_is_held_before_git_mutation(
    prepared_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"locked": False, "git_mutations": 0}
    real_file_lock = workspace_service_module.FileLock

    class ObservedFileLock:
        def __init__(self, path: str) -> None:
            self._delegate = real_file_lock(path)

        def __enter__(self) -> Self:
            self._delegate.acquire()
            state["locked"] = True
            return self

        def __exit__(self, *exc_info: object) -> None:
            state["locked"] = False
            self._delegate.release()

    class GuardedGitRunner(GitRunner):
        def run(
            self,
            argv: list[str],
            *,
            cwd: Path,
            timeout: float = 60,
        ) -> subprocess.CompletedProcess[str]:
            if argv[1] in {"branch", "worktree"}:
                assert state["locked"] is True
                state["git_mutations"] += 1
            return super().run(argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(workspace_service_module, "FileLock", ObservedFileLock)

    WorkspaceService(prepared_project, GuardedGitRunner()).prepare(
        "1001", "sample", "run-a"
    )

    assert state == {"locked": False, "git_mutations": 2}


def test_matching_workspace_manifest_is_reused_idempotently(
    prepared_project: Path,
) -> None:
    service = WorkspaceService(prepared_project, GitRunner())
    first = service.prepare("1001", "sample", "run-a")

    second = service.prepare("1001", "sample", "run-a")

    assert second == first


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "1002"),
        ("repo_alias", "other"),
        ("repo_hash", "0" * 64),
        ("run_id", "run-b"),
    ],
)
def test_workspace_manifest_for_another_owner_is_never_reused(
    prepared_project: Path, field: str, value: str
) -> None:
    service = WorkspaceService(prepared_project, GitRunner())
    service.prepare("1001", "sample", "run-a")
    manifest_path = (
        prepared_project
        / ".yipilot"
        / "tasks"
        / "1001"
        / "repos"
        / "sample"
        / "workspace-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DomainError, match="WORKSPACE_CONFLICT"):
        service.prepare("1001", "sample", "run-a")
