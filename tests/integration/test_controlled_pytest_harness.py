import subprocess
from pathlib import Path
from threading import Event

import pytest

from yipilot.application.quality_artifacts import QualityArtifactService
from yipilot.application.repo_service import RepoService
from yipilot.application.workspace_service import WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.harness.models import HarnessStatus
from yipilot.harness.quality import UnitTestHarness
from yipilot.harness.report_store import (
    UNIT_TEST_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.infrastructure.tool_run_store import FileToolRunStore


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _project(
    tmp_path: Path, test_body: str = "def test_ok():\n    assert True\n"
) -> tuple[Path, FileTaskStore]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "test_ok.py").write_text(test_body, encoding="utf-8")
    _git(source, "add", "test_ok.py")
    _git(source, "commit", "-m", "initial")
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    RepoService(project, GitRunner()).add_and_sync("1001", str(source), "sample")
    return project, store


def test_controlled_pytest_requires_explicit_approval_and_report_is_verified(
    tmp_path: Path,
) -> None:
    project, store = _project(tmp_path)
    service = QualityArtifactService(project)
    prepared = service.prepare_pytest("1001", "sample", request_id="tamper")
    with pytest.raises(DomainError, match="APPROVAL_REQUIRED"):
        service.run_pytest(
            "1001", str(prepared["run_id"]), approval_id=None, confirmation_token=None
        )
    run_root = project / ".yipilot/tasks/1001/runs" / str(prepared["run_id"])
    assert not (run_root / "tool-runs").exists()
    approval = service.approve_pytest(
        "1001", str(prepared["run_id"]), "reviewed-pytest"
    )
    with pytest.raises(DomainError, match="APPROVAL_REQUIRED"):
        service.run_pytest(
            "1001",
            str(prepared["run_id"]),
            approval_id=approval["approval_id"],
            confirmation_token=None,
        )
    assert not (run_root / "tool-runs").exists()
    result = service.run_pytest(
        "1001",
        str(prepared["run_id"]),
        approval_id=approval["approval_id"],
        confirmation_token=approval["confirmation_token"],
    )
    assert result["status"] == "passed"
    report = HarnessReportStore(store).load(
        str(result["run_id"]), UNIT_TEST_HARNESS_REPORT_NAME
    )
    assert report.status is HarnessStatus.PASSED
    assert any(check.id == "PYTEST-REPORT-001" for check in report.checks)
    pytest_report = run_root / "artifacts/pytest-report.json"
    pytest_report.write_text("{}", encoding="utf-8")
    workspace = WorkspaceService(project, GitRunner()).prepare(
        "1001", "sample", str(result["run_id"])
    )
    verified = UnitTestHarness(
        project, "1001", str(result["run_id"]), workspace, str(result["tool_run_id"])
    ).verify()
    assert verified.status is HarnessStatus.FAILED
    assert (
        next(
            check for check in verified.checks if check.id == "PYTEST-REPORT-001"
        ).status
        is HarnessStatus.FAILED
    )


def test_failed_pytest_blocks_the_harness(tmp_path: Path) -> None:
    project, _ = _project(tmp_path, "def test_ok():\n    assert False\n")
    service = QualityArtifactService(project)
    prepared = service.prepare_pytest("1001", "sample", request_id="tamper")
    approval = service.approve_pytest(
        "1001", str(prepared["run_id"]), "reviewed-failure"
    )
    result = service.run_pytest(
        "1001",
        str(prepared["run_id"]),
        approval_id=approval["approval_id"],
        confirmation_token=approval["confirmation_token"],
    )
    assert result["status"] == "failed"


def test_cancelled_pytest_blocks_the_harness(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    service = QualityArtifactService(project)
    prepared = service.prepare_pytest("1001", "sample", request_id="cancelled")
    approval = service.approve_pytest(
        "1001", str(prepared["run_id"]), "reviewed-cancel"
    )
    cancelled = Event()
    cancelled.set()
    result = service.run_pytest(
        "1001",
        str(prepared["run_id"]),
        approval_id=str(approval["approval_id"]),
        confirmation_token=str(approval["confirmation_token"]),
        cancel_event=cancelled,
    )
    assert result["status"] == "failed"


def test_timeout_missing_toolrun_and_tampered_stdout_block(tmp_path: Path) -> None:
    project, store = _project(
        tmp_path, "import time\n\ndef test_ok():\n    time.sleep(1)\n"
    )
    service = QualityArtifactService(project)
    timed = service.prepare_pytest(
        "1001", "sample", timeout_seconds=0.05, request_id="timed"
    )
    approval = service.approve_pytest("1001", str(timed["run_id"]), "reviewed-timeout")
    timed_result = service.run_pytest(
        "1001",
        str(timed["run_id"]),
        approval_id=str(approval["approval_id"]),
        confirmation_token=str(approval["confirmation_token"]),
    )
    assert timed_result["status"] == "failed"

    prepared = service.prepare_pytest("1001", "sample", request_id="tamper")
    approval = service.approve_pytest(
        "1001", str(prepared["run_id"]), "reviewed-tamper"
    )
    result = service.run_pytest(
        "1001",
        str(prepared["run_id"]),
        approval_id=str(approval["approval_id"]),
        confirmation_token=str(approval["confirmation_token"]),
    )
    run_id = str(result["run_id"])
    tool_run_id = str(result["tool_run_id"])
    tool_run = FileToolRunStore(store).get("1001", run_id, tool_run_id)
    run_root = store.event_journal_path(run_id).parent
    (run_root / tool_run.stdout_artifact_ref.relative_path).write_bytes(b"tampered")
    workspace = WorkspaceService(project, GitRunner()).prepare("1001", "sample", run_id)
    assert (
        UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify().status
        is HarnessStatus.FAILED
    )
    (run_root / "tool-runs" / f"{tool_run_id}.json").unlink()
    assert (
        UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify().status
        is HarnessStatus.FAILED
    )
