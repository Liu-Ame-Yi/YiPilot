import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

from yipilot.application.approval import approval_action_type
from yipilot.application.quality_artifacts import QualityArtifactService, _pytest_policy
from yipilot.application.repo_service import RepoService
from yipilot.application.workspace_service import WorkspaceService
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    QUALITY_PYTEST_POLICY_REVISION,
    ApprovalDecision,
    ApprovalRecord,
)
from yipilot.harness.models import HarnessStatus
from yipilot.harness.quality import UnitTestHarness
from yipilot.harness.report_store import (
    UNIT_TEST_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.executors import LocalCommandRunner
from yipilot.infrastructure.git_runner import GitRunner
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.infrastructure.tool_run_store import FileToolRunStore


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _project(
    tmp_path: Path, test_body: str = "def test_ok():\n    assert True\n"
) -> tuple[Path, FileTaskStore]:
    source = tmp_path / "source"
    source.mkdir(parents=True)
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


def _approval(
    service: QualityArtifactService, store: FileTaskStore, run_id: str, approval_id: str
) -> dict[str, str]:
    request, _ = service._pytest_request("1001", run_id)
    run = store.get_run("1001", run_id)
    record = ApprovalRecord(
        approval_id=approval_id,
        task_id="1001",
        operation_id=run.operation_id,
        run_id=run_id,
        action_type=approval_action_type(
            _pytest_policy(request.timeout_seconds).tools[0],
            LocalCommandRunner.trust_level,
        ),
        target_fingerprint=request.fingerprint,
        policy_revision=QUALITY_PYTEST_POLICY_REVISION,
        decision=ApprovalDecision.APPROVED,
        decided_at=datetime.now(UTC),
    )
    FileApprovalStore(store).save(record)
    return {"approval_id": approval_id, "confirmation_token": request.fingerprint}


def _run(
    service: QualityArtifactService,
    store: FileTaskStore,
    *,
    request_id: str,
    timeout: float = 60,
    cancel: Event | None = None,
) -> dict[str, object]:
    prepared = service.prepare_pytest(
        "1001", "sample", timeout_seconds=timeout, request_id=request_id
    )
    approval = _approval(
        service, store, str(prepared["run_id"]), f"approval-{request_id}"
    )
    return service.run_pytest(
        "1001",
        str(prepared["run_id"]),
        approval_id=approval["approval_id"],
        confirmation_token=approval["confirmation_token"],
        cancel_event=cancel,
    )


def test_pytest_consumes_existing_approval_and_report(tmp_path: Path) -> None:
    project, store = _project(tmp_path)
    service = QualityArtifactService(project)
    prepared = service.prepare_pytest("1001", "sample", request_id="approval")
    run_id = str(prepared["run_id"])
    with pytest.raises(DomainError, match="APPROVAL_REQUIRED"):
        service.run_pytest("1001", run_id, approval_id=None, confirmation_token=None)
    assert not (store.event_journal_path(run_id).parent / "tool-runs").exists()
    approval = _approval(service, store, run_id, "reviewed")
    with pytest.raises(DomainError, match="APPROVAL_REQUIRED"):
        service.run_pytest(
            "1001", run_id, approval_id=approval["approval_id"], confirmation_token=None
        )
    assert not (store.event_journal_path(run_id).parent / "tool-runs").exists()
    result = service.run_pytest(
        "1001",
        run_id,
        approval_id=approval["approval_id"],
        confirmation_token=approval["confirmation_token"],
    )
    assert result["status"] == "passed"
    assert not hasattr(service, "approve_pytest")
    report = HarnessReportStore(store).load(run_id, UNIT_TEST_HARNESS_REPORT_NAME)
    assert report.status is HarnessStatus.PASSED
    assert any(check.id == "PYTEST-REPORT-001" for check in report.checks)


def test_pytest_failure_timeout_and_cancel_block(tmp_path: Path) -> None:
    project, store = _project(
        tmp_path, "import time\n\ndef test_ok():\n    time.sleep(1)\n"
    )
    service = QualityArtifactService(project)
    assert (
        _run(service, store, request_id="timeout", timeout=0.05)["status"] == "failed"
    )
    signal = Event()
    signal.set()
    assert (
        _run(service, store, request_id="cancel", cancel=signal)["status"] == "failed"
    )


def test_pytest_failure_blocks(tmp_path: Path) -> None:
    project, store = _project(tmp_path, "def test_ok():\n    assert False\n")
    assert (
        _run(QualityArtifactService(project), store, request_id="failure")["status"]
        == "failed"
    )


def test_pytest_report_toolrun_stream_and_policy_staleness_block(
    tmp_path: Path,
) -> None:
    project, store = _project(tmp_path)
    service = QualityArtifactService(project)
    result = _run(service, store, request_id="tamper")
    run_id, tool_run_id = str(result["run_id"]), str(result["tool_run_id"])
    workspace = WorkspaceService(project, GitRunner()).prepare("1001", "sample", run_id)
    root = store.event_journal_path(run_id).parent
    (root / "artifacts/pytest-report.json").write_text("{}", encoding="utf-8")
    assert (
        UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify().status
        is HarnessStatus.FAILED
    )
    tool_run = FileToolRunStore(store).get("1001", run_id, tool_run_id)
    (root / tool_run.stdout_artifact_ref.relative_path).write_bytes(b"tampered")
    assert (
        UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify().status
        is HarnessStatus.FAILED
    )
    (root / "tool-runs" / f"{tool_run_id}.json").unlink()
    assert (
        UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify().status
        is HarnessStatus.FAILED
    )


def test_stale_policy_approval_blocks_before_process(tmp_path: Path) -> None:
    project, store = _project(tmp_path)
    service = QualityArtifactService(project)
    prepared = service.prepare_pytest("1001", "sample", request_id="stale")
    run_id = str(prepared["run_id"])
    approval = _approval(service, store, run_id, "stale")
    path = store.event_journal_path(run_id).parent / "approvals/stale.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["policy_revision"] = "obsolete"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DomainError, match="APPROVAL_STALE"):
        service.run_pytest(
            "1001",
            run_id,
            approval_id=approval["approval_id"],
            confirmation_token=approval["confirmation_token"],
        )
    assert not (store.event_journal_path(run_id).parent / "tool-runs").exists()


def _check_status(report: object, check_id: str) -> HarnessStatus:
    checks = report.checks
    return next(check.status for check in checks if check.id == check_id)


def test_unit_harness_rechecks_stream_approval_and_git_evidence(tmp_path: Path) -> None:
    project, store = _project(tmp_path)
    service = QualityArtifactService(project)
    result = _run(service, store, request_id="stream")
    run_id, tool_run_id = str(result["run_id"]), str(result["tool_run_id"])
    workspace = WorkspaceService(project, GitRunner()).prepare("1001", "sample", run_id)
    root = store.event_journal_path(run_id).parent
    tool_run = FileToolRunStore(store).get("1001", run_id, tool_run_id)
    (root / tool_run.stdout_artifact_ref.relative_path).write_bytes(b"tampered")
    report = UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify()
    assert _check_status(report, "PYTEST-REPORT-001") is HarnessStatus.FAILED


def test_unit_harness_rechecks_persisted_approval(tmp_path: Path) -> None:
    project, store = _project(tmp_path)
    service = QualityArtifactService(project)
    result = _run(service, store, request_id="approval-verify")
    run_id, tool_run_id = str(result["run_id"]), str(result["tool_run_id"])
    workspace = WorkspaceService(project, GitRunner()).prepare("1001", "sample", run_id)
    path = (
        store.event_journal_path(run_id).parent
        / "approvals/approval-approval-verify.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["policy_revision"] = "obsolete"
    path.write_text(json.dumps(value), encoding="utf-8")
    report = UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify()
    assert _check_status(report, "PYTEST-APPROVAL-001") is HarnessStatus.FAILED


def test_unit_harness_rechecks_workspace_git_evidence(tmp_path: Path) -> None:
    project, store = _project(tmp_path)
    service = QualityArtifactService(project)
    result = _run(service, store, request_id="git-evidence")
    run_id, tool_run_id = str(result["run_id"]), str(result["tool_run_id"])
    workspace = WorkspaceService(project, GitRunner()).prepare("1001", "sample", run_id)
    _git(
        workspace.path,
        "-c",
        "user.name=Yipilot Tests",
        "-c",
        "user.email=tests@yipilot.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "changed after evidence",
    )
    report = UnitTestHarness(project, "1001", run_id, workspace, tool_run_id).verify()
    assert _check_status(report, "PYTEST-GIT-EVIDENCE-001") is HarnessStatus.FAILED
