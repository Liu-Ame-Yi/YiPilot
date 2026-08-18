import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yipilot.application.approval import ApprovalGate, approval_action_type
from yipilot.application.tool_registry import ToolRegistry
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
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import FileArtifactStore
from yipilot.infrastructure.event_journal import JsonlEventJournal
from yipilot.infrastructure.executors import FakeExecutor, LocalCommandRunner
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord
from yipilot.infrastructure.tool_run_store import FileToolRunStore


def _context(
    tmp_path: Path, kind: ToolKind = ToolKind.WORKSPACE_WRITE
) -> tuple[FileTaskStore, RunRecord, ToolRequest, ExecutionPolicy]:
    store = FileTaskStore(tmp_path)
    store.initialize_project()
    store.create_task("1001")
    operation = store.create_operation("1001", "tool", "tool-1001", {})
    run = store.create_run("1001", operation.operation_id, "tool")
    (tmp_path / ".yipilot/tasks/1001/repos/sample/workspace").mkdir(parents=True)
    request = ToolRequest(
        task_id=run.task_id, operation_id=run.operation_id, run_id=run.run_id,
        workspace_relative_path=".yipilot/tasks/1001/repos/sample/workspace",
        tool_id="controlled", argv=("where.exe", "where.exe"), timeout_seconds=1,
        output_budget_bytes=1024, input_fingerprint="a" * 64,
    )
    policy = ExecutionPolicy(
        revision="policy-v1",
        tools=(ToolPolicy(
            tool_id="controlled", kind=kind,
            executable="where.exe", argv_schema=(ArgvRule(literal="where.exe"),),
            max_output_bytes=1024, max_timeout_seconds=1,
        ),),
    )
    return store, run, request, policy


def _approval(
    request: ToolRequest, policy: ExecutionPolicy, *, decision: ApprovalDecision = ApprovalDecision.APPROVED,
    **changes: str,
) -> ApprovalRecord:
    tool = policy.validate_request(request)
    values: dict[str, object] = {
        "approval_id": "approval-1", "task_id": request.task_id,
        "operation_id": request.operation_id, "run_id": request.run_id,
        "action_type": approval_action_type(tool, ExecutionTrustLevel.HOST_TRUSTED),
        "target_fingerprint": request.fingerprint, "policy_revision": policy.revision,
        "decision": decision, "decided_at": datetime.now(UTC),
    }
    values.update(changes)
    return ApprovalRecord.model_validate(values)


@pytest.mark.parametrize(
    ("approval_change", "decision", "token", "expected"),
    [
        (None, ApprovalDecision.APPROVED, None, "APPROVAL_REQUIRED"),
        ({}, ApprovalDecision.DENIED, "request", "APPROVAL_DENIED"),
        ({"policy_revision": "policy-v0"}, ApprovalDecision.APPROVED, "request", "APPROVAL_STALE"),
        ({"target_fingerprint": "b" * 64}, ApprovalDecision.APPROVED, "request", "APPROVAL_CONFIRMATION_INVALID"),
    ],
)
def test_high_risk_execution_rejects_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approval_change: dict[str, str] | None,
    decision: ApprovalDecision, token: str | None, expected: str,
) -> None:
    store, _run, request, policy = _context(tmp_path)
    approvals = FileApprovalStore(store)
    if approval_change is not None:
        approvals.save(_approval(request, policy, decision=decision, **approval_change))
    journal = JsonlEventJournal(store.event_journal_path(request.run_id), task_id=request.task_id, run_id=request.run_id)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Popen"))
    registry = ToolRegistry(policy, LocalCommandRunner(FileArtifactStore(store), journal), ApprovalGate(approvals))

    with pytest.raises(DomainError, match=rf"^{expected}:"):
        registry.execute(
            tmp_path, request, approval_id="approval-1",
            confirm_token=request.fingerprint if token == "request" else token,
        )
    assert journal.read_after(0) == []


def test_tool_result_store_round_trips_and_detects_stale_artifact(tmp_path: Path) -> None:
    store, _run, request, policy = _context(tmp_path, ToolKind.READ_ONLY)
    journal = JsonlEventJournal(store.event_journal_path(request.run_id), task_id=request.task_id, run_id=request.run_id)
    result = ToolRegistry(policy, FakeExecutor(FileArtifactStore(store), journal)).execute(tmp_path, request)
    runs = FileToolRunStore(store)

    assert runs.get(request.task_id, request.run_id, result.tool_run_id) == result
    artifact_path = store.event_journal_path(request.run_id).parent / result.stdout_artifact_ref.relative_path
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(DomainError, match="^ARTIFACT_STALE:"):
        runs.get(request.task_id, request.run_id, result.tool_run_id)


def test_approval_store_rejects_unsafe_identifier(tmp_path: Path) -> None:
    store, _run, request, policy = _context(tmp_path)
    approval = _approval(request, policy, approval_id="../outside")

    with pytest.raises(DomainError, match="^PATH_DENIED:"):
        FileApprovalStore(store).save(approval)
