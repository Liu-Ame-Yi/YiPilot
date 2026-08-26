import subprocess
from pathlib import Path

from yipilot.application.quality_artifacts import QualityArtifactService
from yipilot.application.repo_service import RepoService
from yipilot.harness.models import HarnessStatus
from yipilot.harness.report_store import (
    UNIT_TEST_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def test_controlled_pytest_uses_toolrun_approval_and_git_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(source, "add", "test_ok.py")
    _git(source, "commit", "-m", "initial")
    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    RepoService(project, GitRunner()).add_and_sync("1001", str(source), "sample")
    result = QualityArtifactService(project).run_pytest("1001", "sample")
    assert result["status"] == "passed"
    report = HarnessReportStore(store).load(result["run_id"], UNIT_TEST_HARNESS_REPORT_NAME)
    assert report.status is HarnessStatus.PASSED
    assert any(check.id == "PYTEST-APPROVAL-001" for check in report.checks)
    assert (project / ".yipilot/tasks/1001/runs" / result["run_id"] / "tool-runs").is_dir()