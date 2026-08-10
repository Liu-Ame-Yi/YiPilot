import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from yipilot.application.repo_service import RepoService
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


def _create_local_repository(path: Path, filename: str = "tracked.py") -> Path:
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Yipilot Tests")
    _git(path, "config", "user.email", "tests@yipilot.invalid")
    (path / filename).write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    store = FileTaskStore(root)
    store.initialize_project()
    store.create_task("1001")
    store.create_task("1002")
    return root


@pytest.fixture
def local_remote(tmp_path: Path) -> Path:
    return _create_local_repository(tmp_path / "source")


def test_git_runner_uses_argv_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitRunner().run(["git", "status", "--porcelain"], cwd=tmp_path)

    assert result.stdout == "ok\n"
    assert observed["argv"] == ["git", "status", "--porcelain"]
    assert observed["shell"] is False
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["encoding"] == "utf-8"
    assert observed["cwd"] == tmp_path
    assert observed["timeout"] == 60


@pytest.mark.parametrize(
    "argv",
    [
        "git status",
        ["echo", "not-git"],
        ["git", "status\x00--porcelain"],
    ],
)
def test_git_runner_rejects_non_argv_git_commands(
    argv: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DomainError, match="GIT_ARGV_INVALID"):
        GitRunner().run(argv, cwd=tmp_path)  # type: ignore[arg-type]

    assert called is False


def test_git_runner_reports_a_stable_redacted_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "top-secret-token"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            128,
            stdout="",
            stderr=(
                f"fatal: https://user:{secret}@example.invalid/repo.git "
                f"Authorization: Bearer {secret}"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DomainError) as raised:
        GitRunner().run(["git", "fetch", "origin"], cwd=tmp_path)

    assert raised.value.code == "GIT_FAILED"
    assert "fatal" in raised.value.message
    assert secret not in str(raised.value)


def test_git_runner_accepts_no_index_difference_exit_code(tmp_path: Path) -> None:
    untracked = tmp_path / "untracked.py"
    untracked.write_text("value = 1\n", encoding="utf-8")

    result = GitRunner().run(
        ["git", "diff", "--no-index", "--binary", "--", os.devnull, str(untracked)],
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "+value = 1" in result.stdout


def test_git_runner_does_not_accept_exit_code_one_for_other_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="failed")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DomainError, match="GIT_FAILED"):
        GitRunner().run(["git", "diff", "HEAD"], cwd=tmp_path)


def test_git_runner_rejects_no_index_exit_one_without_diff_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="error: Could not access 'missing.py'",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DomainError, match="GIT_FAILED"):
        GitRunner().run(
            ["git", "diff", "--no-index", "--", os.devnull, "missing.py"],
            cwd=tmp_path,
        )


def test_git_runner_bytes_preserves_non_utf8_diff(tmp_path: Path) -> None:
    non_utf8 = tmp_path / "legacy.txt"
    non_utf8.write_bytes(b"\x80\x81\n")

    result = GitRunner().run_bytes(
        ["git", "diff", "--no-index", "--binary", "--", os.devnull, str(non_utf8)],
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert b"+\x80\x81" in result.stdout


def test_sync_uses_sha256_cache_and_records_remote_head(
    project: Path, local_remote: Path
) -> None:
    metadata = RepoService(project, GitRunner()).add_and_sync(
        "1001", str(local_remote), "sample"
    )
    expected_url = local_remote.resolve().as_uri()
    expected_head = _git(local_remote, "rev-parse", "HEAD").stdout.strip()

    assert metadata.normalized_url == expected_url
    assert metadata.url_hash == hashlib.sha256(expected_url.encode("utf-8")).hexdigest()
    assert len(metadata.url_hash) == 64
    assert metadata.cache_path == (
        project / ".yipilot" / "cache" / "repos" / metadata.url_hash
    )
    assert metadata.cache_path.is_dir()
    assert metadata.head_sha == expected_head

    metadata_path = (
        project
        / ".yipilot"
        / "tasks"
        / "1001"
        / "repos"
        / "sample"
        / "metadata.json"
    )
    record = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert record == {
        "alias": "sample",
        "cache_path": f".yipilot/cache/repos/{metadata.url_hash}",
        "head_sha": expected_head,
        "normalized_url": expected_url,
        "schema_version": 1,
        "synced_at": metadata.synced_at,
        "task_id": "1001",
        "url_hash": metadata.url_hash,
    }


def test_same_repository_can_be_fetched_again_and_alias_is_idempotent(
    project: Path, local_remote: Path
) -> None:
    service = RepoService(project, GitRunner())
    first = service.add_and_sync("1001", str(local_remote), "sample")
    (local_remote / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    _git(local_remote, "add", "tracked.py")
    _git(local_remote, "commit", "-m", "second")

    second = service.add_and_sync("1001", str(local_remote.resolve()), "sample")

    assert second.normalized_url == first.normalized_url
    assert second.url_hash == first.url_hash
    assert second.cache_path == first.cache_path
    assert second.head_sha == _git(local_remote, "rev-parse", "HEAD").stdout.strip()
    assert second.head_sha != first.head_sha
    assert not (first.cache_path / first.cache_path.name).exists()


def test_alias_bound_to_another_url_is_rejected(
    project: Path, local_remote: Path, tmp_path: Path
) -> None:
    service = RepoService(project, GitRunner())
    service.add_and_sync("1001", str(local_remote), "sample")
    another_remote = _create_local_repository(tmp_path / "other-source", "other.py")

    with pytest.raises(DomainError, match="REPO_ALIAS_CONFLICT"):
        service.add_and_sync("1001", str(another_remote), "sample")


def test_nul_url_is_rejected_before_git_is_invoked(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DomainError, match="REPO_URL_INVALID"):
        RepoService(project, GitRunner()).add_and_sync(
            "1001", "file:///repo\x00secret", "sample"
        )

    assert called is False


def test_credentialed_http_url_is_rejected_without_persisting_secret(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "top-secret-token"
    called = False

    def fail_if_called(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("Git must not be invoked for a credentialed URL")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    with pytest.raises(DomainError, match="REPO_URL_INVALID") as raised:
        RepoService(project, GitRunner()).add_and_sync(
            "1001", f"https://user:{secret}@example.invalid/repo.git", "sample"
        )

    assert secret not in str(raised.value)
    assert called is False
    assert not (
        project
        / ".yipilot"
        / "tasks"
        / "1001"
        / "repos"
        / "sample"
        / "metadata.json"
    ).exists()
