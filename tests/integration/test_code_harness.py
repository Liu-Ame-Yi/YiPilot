import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.execution import (
    ApprovalDecision,
    ApprovalRecord,
    ArgvRule,
    ExecutionPolicy,
    ExecutionTrustLevel,
    ToolKind,
    ToolPolicy,
    ToolRequest,
)
from yipilot.harness.code import CodeHarness
from yipilot.persistence.approval_store import FileApprovalStore
from yipilot.persistence.artifact_store import FileArtifactStore
from yipilot.persistence.event_journal import JsonlEventJournal
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.approval import ApprovalGate, approval_action_type
from yipilot.workspace.executors import ControlledWorkspaceWriter
from yipilot.workspace.tool_registry import ToolRegistry
from yipilot.workspace.workspace_service import PreparedWorkspace


def _git(cwd: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=cwd, shell=False, check=True, capture_output=True)


def test_code_harness_persists_independent_report_and_blocks_stale_git_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Yipilot Tests")
    _git(source, "config", "user.email", "tests@yipilot.invalid")
    (source / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(source, "add", "tracked.py")
    _git(source, "commit", "-m", "initial")

    project = tmp_path / "project"
    project.mkdir()
    store = FileTaskStore(project)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "code_prepare", "code-1001", {})
    run = store.create_run("1001", operation.operation_id, "code_prepare")
    repo_root = project / ".yipilot/tasks/1001/repos/sample"
    repo_root.mkdir(parents=True)
    workspace_path = repo_root / "workspace"
    _git(repo_root, "clone", str(source), "workspace")
    normalized_url = source.as_uri()
    repo_hash = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    store.db.save_record("repo", "1001:sample", "1001", None,
        json.dumps({"task_id": "1001", "alias": "sample", "url_hash": repo_hash, "normalized_url": normalized_url}))
    (repo_root / "workspace-manifest.json").write_text(
        json.dumps({
            "schema_version": 1, "task_id": "1001", "run_id": run.run_id,
            "repo_alias": "sample", "repo_hash": repo_hash, "branch": "main",
            "base_sha": base_sha,
            "workspace_path": ".yipilot/tasks/1001/repos/sample/workspace",
            "created_at": datetime.now(UTC).isoformat(),
        }), encoding="utf-8",
    )
    workspace = PreparedWorkspace(
        path=workspace_path, schema_version=1, task_id="1001", run_id=run.run_id,
        repo_alias="sample", repo_hash=repo_hash, branch="main", base_sha=base_sha,
        created_at=datetime.now(UTC).isoformat(),
    )
    request = ToolRequest(
        task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="write-marker", argv=("yipilot.write-file", "approval-marker.txt", "approved"), timeout_seconds=1,
        output_budget_bytes=1024, input_fingerprint="a" * 64,
    )
    policy = ExecutionPolicy(
        revision="policy-v1",
        tools=(ToolPolicy(
            tool_id="write-marker", kind=ToolKind.WORKSPACE_WRITE,
            executable="yipilot.write-file",
            argv_schema=(ArgvRule(literal="approval-marker.txt"), ArgvRule(literal="approved")), max_output_bytes=1024,
            max_timeout_seconds=1,
        ),),
    )
    journal = JsonlEventJournal(store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id)
    approval = ApprovalRecord(
        approval_id="approval-1", task_id=run.task_id, operation_id=run.operation_id,
        run_id=run.run_id,
        action_type=approval_action_type(policy.tools[0], ExecutionTrustLevel.SANDBOXED),
        target_fingerprint=request.fingerprint, policy_revision=policy.revision,
        decision=ApprovalDecision.APPROVED, decided_at=datetime.now(UTC),
    )
    approvals = FileApprovalStore(store)
    approvals.save(approval)
    writer = ControlledWorkspaceWriter(FileArtifactStore(store), journal)
    with pytest.raises(DomainError, match="^APPROVAL_REQUIRED:"):
        writer.execute(request, policy.tools[0], cwd=workspace.path, project_root=project)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(DomainError, match="^PATH_DENIED:"):
        writer.execute(
            request,
            policy.tools[0],
            cwd=outside,
            project_root=project,
            approval_gate=ApprovalGate(approvals),
            policy_revision=policy.revision,
            approval_id=approval.approval_id,
            confirm_token=request.fingerprint,
        )
    assert not (workspace.path / "approval-marker.txt").exists()
    assert not (outside / "approval-marker.txt").exists()
    assert journal.read_after(0) == []
    result = ToolRegistry(
        policy,
        writer,
        ApprovalGate(approvals),
    ).execute(
        project,
        request,
        approval_id=approval.approval_id,
        confirm_token=request.fingerprint,
    )
    harness = CodeHarness(
        project, run.task_id, run.run_id, workspace, policy, approval_id=approval.approval_id,
        action_type=approval.action_type, target_fingerprint=request.fingerprint,
    )

    report = harness.run()

    assert report.status == "passed"
    assert result.change_summary.changed_file_count == 1
    assert (workspace.path / "approval-marker.txt").read_text(encoding="utf-8") == "approved\n"
    assert (store.event_journal_path(run.run_id).parent / "harness/code-harness-report.json").is_file()
    assert (store.event_journal_path(run.run_id).parent / "harness/task-integrity-report.json").is_file()
    assert harness.require_passed().status == "passed"
    stdout = store.event_journal_path(run.run_id).parent / result.stdout_artifact_ref.relative_path
    stdout.write_bytes(b"tampered")
    with pytest.raises(DomainError, match="^CODE_HARNESS_FAILED:"):
        harness.require_passed()
    stdout.write_bytes(b"")
    (workspace.path / "approval-marker.txt").write_text("stale\n", encoding="utf-8")
    with pytest.raises(DomainError, match="^CODE_HARNESS_FAILED:"):
        harness.require_passed()
