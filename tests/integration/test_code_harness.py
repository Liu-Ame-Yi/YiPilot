import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from yipilot.application.approval import approval_action_type
from yipilot.application.tool_registry import ToolRegistry
from yipilot.application.workspace_service import PreparedWorkspace
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
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import FakeExecutor
from yipilot.infrastructure.task_store import FileTaskStore


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
    (repo_root / "metadata.json").write_text(
        json.dumps({"task_id": "1001", "alias": "sample", "url_hash": repo_hash, "normalized_url": normalized_url}),
        encoding="utf-8",
    )
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
    (workspace.path / "created.txt").write_text("evidence\n", encoding="utf-8")

    request = ToolRequest(
        task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="diagnostic", argv=("diagnostic", "--version"), timeout_seconds=1,
        output_budget_bytes=1024, input_fingerprint="a" * 64,
    )
    policy = ExecutionPolicy(
        revision="policy-v1",
        tools=(ToolPolicy(
            tool_id="diagnostic", kind=ToolKind.READ_ONLY, executable="diagnostic",
            argv_schema=(ArgvRule(literal="--version"),), max_output_bytes=1024,
            max_timeout_seconds=1,
        ),),
    )
    journal = JsonlEventJournal(store.event_journal_path(run.run_id), task_id=run.task_id, run_id=run.run_id)
    ToolRegistry(policy, FakeExecutor(FileArtifactStore(store), journal)).execute(project, request)
    approval = ApprovalRecord(
        approval_id="approval-1", task_id=run.task_id, operation_id=run.operation_id,
        run_id=run.run_id,
        action_type=approval_action_type(policy.tools[0], ExecutionTrustLevel.FAKE),
        target_fingerprint=request.fingerprint, policy_revision=policy.revision,
        decision=ApprovalDecision.APPROVED, decided_at=datetime.now(UTC),
    )
    FileApprovalStore(store).save(approval)
    harness = CodeHarness(
        project, run.task_id, run.run_id, workspace, policy, approval_id=approval.approval_id,
        action_type=approval.action_type, target_fingerprint=request.fingerprint,
    )

    report = harness.run()

    assert report.status == "passed"
    assert (store.event_journal_path(run.run_id).parent / "harness/code-harness-report.json").is_file()
    assert (store.event_journal_path(run.run_id).parent / "harness/task-integrity-report.json").is_file()
    assert harness.require_passed() == report
    (workspace.path / "created.txt").write_text("stale\n", encoding="utf-8")
    assert harness.run().status == "failed"
