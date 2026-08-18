"""Composite evidence gate for prepare-only code handoff."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from yipilot.application.workspace_service import PreparedWorkspace
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ExecutionPolicy
from yipilot.harness.git_workspace import GitWorkspaceHarness
from yipilot.harness.models import (
    HarnessCheck,
    HarnessGatePolicy,
    HarnessReport,
    HarnessStatus,
    aggregate_checks,
)
from yipilot.harness.report_store import CODE_HARNESS_REPORT_NAME, HarnessReportStore
from yipilot.harness.task_integrity import TaskIntegrityHarness
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.infrastructure.tool_run_store import FileToolRunStore

_HARNESS_NAME = "code_harness"
_HARNESS_VERSION = "1.0.0"


class CodeHarness:
    """Require fresh approval, complete tool evidence and current Git evidence."""

    def __init__(
        self,
        project_root: Path,
        task_id: str,
        run_id: str,
        workspace: PreparedWorkspace,
        policy: ExecutionPolicy,
        *,
        approval_id: str,
        action_type: str,
        target_fingerprint: str,
        gate_policy: HarnessGatePolicy | None = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._task_id = task_id
        self._run_id = run_id
        self._workspace = workspace
        self._policy = policy
        self._approval_id = approval_id
        self._action_type = action_type
        self._target_fingerprint = target_fingerprint
        self._gate_policy = gate_policy or HarnessGatePolicy()
        self._store = FileTaskStore(self._project_root)
        self._reports = HarnessReportStore(self._store)

    def run(self) -> HarnessReport:
        started_at = datetime.now(UTC)
        run = self._store.get_run(self._task_id, self._run_id)
        approval_check = self._check_approval(run.operation_id)
        tool_runs_check = self._check_tool_runs()
        git_report = GitWorkspaceHarness(self._workspace).run()
        workspace_check = _derived_check(
            "CODE-WORKSPACE-001", git_report, "GIT-WORKSPACE-001", "workspace manifest matches Task and Run"
        )
        git_check = _git_check(git_report)
        integrity_report = TaskIntegrityHarness(
            self._project_root, self._task_id, self._run_id
        ).run()
        integrity_check = HarnessCheck(
            id="CODE-TASK-INTEGRITY-001",
            status=(HarnessStatus.PASSED if integrity_report.status is HarnessStatus.PASSED else HarnessStatus.FAILED),
            description="TaskIntegrityHarness has passed",
            message=f"task integrity status is {integrity_report.status}",
            evidence_refs=["harness/task-integrity-report.json"],
        )
        checks = [approval_check, tool_runs_check, workspace_check, git_check, integrity_check]
        inputs: list[dict[str, JsonValue]] = [
            {"kind": "approval", "approval_id": self._approval_id, "target_fingerprint": self._target_fingerprint},
            {"kind": "tool_runs", "check_status": tool_runs_check.status.value},
            {"kind": "git", "input_fingerprint": git_report.input_fingerprint},
            {"kind": "task_integrity", "input_fingerprint": integrity_report.input_fingerprint},
        ]
        status = aggregate_checks(checks).status
        report = HarnessReport(
            report_id=str(uuid4()), task_id=self._task_id, operation_id=run.operation_id,
            run_id=self._run_id, stage=run.stage, harness_name=_HARNESS_NAME,
            harness_version=_HARNESS_VERSION, started_at=started_at, ended_at=datetime.now(UTC),
            input_fingerprint=_fingerprint(inputs), inputs=inputs, status=status, checks=checks,
            summary=f"code harness completed with status {status}",
        )
        self._reports.save(report, CODE_HARNESS_REPORT_NAME)
        return report

    def require_passed(self) -> HarnessReport:
        report = self.run()
        if not self._gate_policy.accepts(report.status):
            raise DomainError("CODE_HARNESS_FAILED", "code harness has not passed")
        return report

    def _check_approval(self, operation_id: str) -> HarnessCheck:
        try:
            approval = FileApprovalStore(self._store).get(
                self._task_id, self._run_id, self._approval_id
            )
            approval.require_fresh(
                task_id=self._task_id, operation_id=operation_id, run_id=self._run_id,
                action_type=self._action_type, target_fingerprint=self._target_fingerprint,
                policy_revision=self._policy.revision,
            )
        except DomainError as error:
            return HarnessCheck(
                id="CODE-APPROVAL-001", status=HarnessStatus.FAILED,
                description="Approval exists, is approved and fresh", message=error.code,
                evidence_refs=[f"approvals/{self._approval_id}.json"],
            )
        return HarnessCheck(
            id="CODE-APPROVAL-001", status=HarnessStatus.PASSED,
            description="Approval exists, is approved and fresh", message="approval is current",
            evidence_refs=[f"approvals/{self._approval_id}.json"],
        )

    def _check_tool_runs(self) -> HarnessCheck:
        try:
            results = FileToolRunStore(self._store).list(self._task_id, self._run_id)
        except DomainError as error:
            return HarnessCheck(
                id="CODE-TOOLRUN-001", status=HarnessStatus.FAILED,
                description="ToolResult has complete Run and Artifact associations", message=error.code,
                evidence_refs=["tool-runs"],
            )
        if not results:
            return HarnessCheck(
                id="CODE-TOOLRUN-001", status=HarnessStatus.FAILED,
                description="ToolResult has complete Run and Artifact associations", message="no persisted ToolResult exists",
                evidence_refs=["tool-runs"],
            )
        if not any(
            result.request_fingerprint == self._target_fingerprint
            and self._action_type.endswith(f":{result.trust_level.value}")
            for result in results
        ):
            return HarnessCheck(
                id="CODE-TOOLRUN-001", status=HarnessStatus.FAILED,
                description="ToolResult has complete Run and Artifact associations",
                message="no ToolResult matches the approved target and trust level",
                evidence_refs=[f"tool-runs/{result.tool_run_id}.json" for result in results],
            )
        return HarnessCheck(
            id="CODE-TOOLRUN-001", status=HarnessStatus.PASSED,
            description="ToolResult has complete Run and Artifact associations", message="all persisted ToolResults are intact",
            evidence_refs=[f"tool-runs/{result.tool_run_id}.json" for result in results],
        )


def _derived_check(
    check_id: str, report: HarnessReport, source_id: str, description: str
) -> HarnessCheck:
    source = next(check for check in report.checks if check.id == source_id)
    return HarnessCheck(
        id=check_id,
        status=(HarnessStatus.PASSED if source.status is HarnessStatus.PASSED else HarnessStatus.FAILED),
        description=description, message=source.message, evidence_refs=source.evidence_refs,
    )


def _fingerprint(inputs: list[dict[str, JsonValue]]) -> str:
    encoded = json.dumps(inputs, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _git_check(report: HarnessReport) -> HarnessCheck:
    return HarnessCheck(
        id="CODE-GIT-EVIDENCE-001",
        status=(HarnessStatus.PASSED if report.status is HarnessStatus.PASSED else HarnessStatus.FAILED),
        description="Git evidence is complete, current and reviewable",
        message=f"Git workspace status is {report.status}",
        evidence_refs=[ref for check in report.checks for ref in check.evidence_refs],
    )
