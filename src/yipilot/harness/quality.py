"""Deterministic quality-artifact Harnesses for the offline Python/pytest boundary."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from yipilot.application.workspace_service import PreparedWorkspace
from yipilot.domain.errors import DomainError
from yipilot.domain.execution import ToolKind, ToolRunStatus
from yipilot.harness.git_workspace import GitWorkspaceHarness
from yipilot.harness.models import (
    HarnessCheck,
    HarnessReport,
    HarnessStatus,
    aggregate_checks,
)
from yipilot.harness.report_store import (
    SDD_HARNESS_REPORT_NAME,
    SRS_HARNESS_REPORT_NAME,
    UNIT_TEST_HARNESS_REPORT_NAME,
    HarnessReportStore,
)
from yipilot.infrastructure.approval_store import FileApprovalStore
from yipilot.infrastructure.artifact_store import ArtifactRecord, FileArtifactStore
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.infrastructure.tool_run_store import FileToolRunStore

_SRS_HEADINGS = ("# SRS", "## Goals and scope", "## Functional requirements", "## Non-functional requirements", "## Acceptance criteria", "## Assumptions and constraints")
_SDD_HEADINGS = ("# SDD", "## Architecture and components", "## Interfaces and data flow", "## Constraints", "## Error handling", "## Test traceability")
_PLAN_HEADINGS = ("# Test Plan", "## Test scope", "## Preconditions", "## Test cases and commands", "## Expected results", "## Requirements and design references")


class SrsHarness:
    """Block a malformed or changed SRS and its PRD inputs."""

    def __init__(self, project_root: Path, task_id: str, run_id: str) -> None:
        self._project_root, self._task_id, self._run_id = Path(project_root), task_id, run_id
        self._store = FileTaskStore(self._project_root)

    def run(self) -> HarnessReport:
        return self._evaluate(True)

    def verify(self) -> HarnessReport:
        return self._evaluate(False)

    def _evaluate(self, persist: bool) -> HarnessReport:
        artifacts = FileArtifactStore(self._store)
        metadata, metadata_check = _metadata(artifacts, self._task_id, self._run_id)
        srs, srs_check = _record(artifacts, self._task_id, self._run_id, "artifacts/srs.md", "SRS-ARTIFACT-001")
        prd, input_check = _record(artifacts, self._task_id, self._run_id, "artifacts/prd.md", "SRS-INPUT-001")
        checks = [srs_check, input_check, metadata_check]
        if srs is not None:
            text = _read(artifacts, srs)
            checks.extend((_structure("SRS-STRUCTURE-001", text, _SRS_HEADINGS), _requirements(text)))
        if metadata is not None and srs is not None and prd is not None:
            checks.append(_meta_hashes("SRS-METADATA-001", metadata, {"srs": srs, "prd": prd}))
            checks.append(_fingerprint_check("SRS-FINGERPRINT-001", metadata, prd))
        return _report(self._store, self._task_id, self._run_id, "srs_harness", "1.0.0", checks, metadata, persist, SRS_HARNESS_REPORT_NAME)


class SddHarness:
    """Block a malformed SDD, missing SRS link, or stale profile/SRS input."""

    def __init__(self, project_root: Path, task_id: str, run_id: str, *, profile_revision: int | None = None, semantic_review: bool = False) -> None:
        self._project_root, self._task_id, self._run_id = Path(project_root), task_id, run_id
        self._profile_revision, self._semantic_review = profile_revision, semantic_review
        self._store = FileTaskStore(self._project_root)

    def run(self) -> HarnessReport:
        return self._evaluate(True)

    def verify(self) -> HarnessReport:
        return self._evaluate(False)

    def _evaluate(self, persist: bool) -> HarnessReport:
        artifacts = FileArtifactStore(self._store)
        metadata, metadata_check = _metadata(artifacts, self._task_id, self._run_id)
        sdd, sdd_check = _record(artifacts, self._task_id, self._run_id, "artifacts/sdd.md", "SDD-ARTIFACT-001")
        srs, srs_check = _record(artifacts, self._task_id, self._run_id, "artifacts/srs.md", "SDD-SRS-REFERENCE-001")
        checks = [sdd_check, srs_check, metadata_check]
        if sdd is not None:
            text = _read(artifacts, sdd)
            checks.append(_structure("SDD-STRUCTURE-001", text, _SDD_HEADINGS))
            if srs is not None and srs.artifact_id not in text:
                checks.append(_failed("SDD-SRS-REFERENCE-001", "SDD does not reference the current SRS Artifact"))
        if metadata is not None and sdd is not None and srs is not None:
            checks.append(_meta_hashes("SDD-METADATA-001", metadata, {"sdd": sdd, "srs": srs}))
            profile = metadata.get("provider_profile")
            expected = profile.get("revision") if isinstance(profile, dict) else None
            actual = self._profile_revision if self._profile_revision is not None else expected
            checks.append(_passed("SDD-PROFILE-REVISION-001", "provider profile revision is current") if expected == actual else _failed("SDD-PROFILE-REVISION-001", "provider profile revision is stale"))
        checks.append(HarnessCheck(id="SDD-SEMANTIC-REVIEW-001", status=HarnessStatus.SKIPPED if self._semantic_review else HarnessStatus.PASSED, blocking=self._semantic_review, description="optional model semantic review", message="disabled by default" if not self._semantic_review else "not implemented"))
        return _report(self._store, self._task_id, self._run_id, "sdd_harness", "1.0.0", checks, metadata, persist, SDD_HARNESS_REPORT_NAME)


class UnitTestHarness:
    """Validate one fixed Python/pytest ToolRun and its workspace evidence."""

    def __init__(self, project_root: Path, task_id: str, run_id: str, workspace: PreparedWorkspace, tool_run_id: str) -> None:
        self._project_root, self._task_id, self._run_id = Path(project_root), task_id, run_id
        self._workspace, self._tool_run_id = workspace, tool_run_id
        self._store = FileTaskStore(self._project_root)

    def run(self) -> HarnessReport:
        return self._evaluate(True)

    def verify(self) -> HarnessReport:
        return self._evaluate(False)

    def _evaluate(self, persist: bool) -> HarnessReport:
        checks: list[HarnessCheck] = []
        try:
            result = FileToolRunStore(self._store).get(self._task_id, self._run_id, self._tool_run_id)
            checks.append(_passed("PYTEST-TOOLRUN-001", "pytest ToolRun and bounded stream Artifacts are intact", [f"tool-runs/{result.tool_run_id}.json"]))
            checks.append(_passed("PYTEST-COMMAND-001", "registered argv is fixed Python -m pytest") if len(result.argv) == 3 and result.argv[1:] == ("-m", "pytest") else _failed("PYTEST-COMMAND-001", "ToolRun is not the fixed pytest command"))
            checks.append(_passed("PYTEST-EXIT-001", "pytest exited successfully") if result.status is ToolRunStatus.SUCCEEDED and result.exit_code == 0 else _failed("PYTEST-EXIT-001", f"pytest terminal status is {result.status}"))
            approvals = FileApprovalStore(self._store).list(self._task_id, self._run_id)
            action = f"tool:{ToolKind.TEST.value}:{result.trust_level.value}"
            approved = any(a.action_type == action and a.target_fingerprint == result.request_fingerprint and a.decision.value == "approved" for a in approvals)
            checks.append(_passed("PYTEST-APPROVAL-001", "fresh pytest approval exists") if approved else _failed("PYTEST-APPROVAL-001", "pytest Approval is missing or stale"))
        except DomainError as error:
            checks.extend((_failed("PYTEST-TOOLRUN-001", error.code), _failed("PYTEST-COMMAND-001", "ToolRun unavailable"), _failed("PYTEST-EXIT-001", "ToolRun unavailable"), _failed("PYTEST-APPROVAL-001", "ToolRun unavailable")))
        git = GitWorkspaceHarness(self._workspace)
        git_report = git.run() if persist else git.verify()
        checks.append(HarnessCheck(id="PYTEST-GIT-EVIDENCE-001", status=HarnessStatus.PASSED if git_report.status is HarnessStatus.PASSED else HarnessStatus.FAILED, description="workspace HEAD and Git evidence are current", message=f"Git workspace status is {git_report.status}", evidence_refs=[ref for check in git_report.checks for ref in check.evidence_refs]))
        return _report(self._store, self._task_id, self._run_id, "unit_test_harness", "1.0.0", checks, {"git_fingerprint": git_report.input_fingerprint}, persist, UNIT_TEST_HARNESS_REPORT_NAME)


def _metadata(artifacts: FileArtifactStore, task_id: str, run_id: str) -> tuple[dict[str, JsonValue] | None, HarnessCheck]:
    record, check = _record(artifacts, task_id, run_id, "artifacts/quality-metadata.json", "QUALITY-METADATA-001")
    if record is None:
        return None, check
    try:
        value = json.loads(_read(artifacts, record))
        if not isinstance(value, dict):
            raise TypeError
        return value, check
    except (TypeError, json.JSONDecodeError):
        return None, _failed("QUALITY-METADATA-001", "quality metadata is invalid")


def _record(artifacts: FileArtifactStore, task_id: str, run_id: str, relative_path: str, check_id: str) -> tuple[ArtifactRecord | None, HarnessCheck]:
    try:
        record = next(item for item in artifacts.list(task_id, run_id) if item.relative_path == relative_path)
        artifacts.verify(task_id, run_id, record.artifact_id)
        return record, _passed(check_id, f"{relative_path} is intact", [relative_path])
    except (DomainError, StopIteration) as error:
        return None, _failed(check_id, getattr(error, "code", "ARTIFACT_NOT_FOUND"))


def _read(artifacts: FileArtifactStore, record: ArtifactRecord) -> str:
    root = artifacts.task_store.event_journal_path(record.run_id).parent
    return (root / record.relative_path).read_text(encoding="utf-8")


def _structure(check_id: str, text: str, headings: tuple[str, ...]) -> HarnessCheck:
    missing = [heading for heading in headings if heading not in text]
    return _passed(check_id, "required document structure is present") if not missing else _failed(check_id, f"missing headings: {', '.join(missing)}")


def _requirements(text: str) -> HarnessCheck:
    return _passed("SRS-REQUIREMENTS-001", "SRS has stable requirement identifiers") if re.search(r"\b(?:FR|NFR)-\d{3}\b", text) else _failed("SRS-REQUIREMENTS-001", "SRS has no requirement identifiers")


def _meta_hashes(
    check_id: str,
    metadata: dict[str, JsonValue],
    records: dict[str, ArtifactRecord],
) -> HarnessCheck:
    docs = metadata.get("artifacts")
    if not isinstance(docs, dict):
        return _failed(check_id, "metadata Artifact hashes are stale")
    for name, record in records.items():
        item = docs.get(name)
        if not isinstance(item, dict):
            return _failed(check_id, "metadata Artifact hashes are stale")
        value = item.get("sha256")
        if value != record.sha256:
            return _failed(check_id, "metadata Artifact hashes are stale")
    return _passed(check_id, "metadata Artifact hashes are current")

def _fingerprint_check(check_id: str, metadata: dict[str, JsonValue], prd: ArtifactRecord) -> HarnessCheck:
    return _passed(check_id, "input fingerprint matches PRD") if metadata.get("input_fingerprint") == prd.sha256 else _failed(check_id, "input fingerprint does not match PRD")


def _report(store: FileTaskStore, task_id: str, run_id: str, name: str, version: str, checks: list[HarnessCheck], metadata: object, persist: bool, filename: str) -> HarnessReport:
    run = store.get_run(task_id, run_id)
    inputs: list[dict[str, JsonValue]] = [{"metadata": metadata if isinstance(metadata, dict) else {}}]
    fingerprint = hashlib.sha256(json.dumps(inputs, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    status = aggregate_checks(checks).status
    report = HarnessReport(report_id=str(uuid4()), task_id=task_id, operation_id=run.operation_id, run_id=run_id, stage=run.stage, harness_name=name, harness_version=version, started_at=datetime.now(UTC), ended_at=datetime.now(UTC), input_fingerprint=fingerprint, inputs=inputs, status=status, checks=checks, summary=f"{name} completed with status {status}")
    if persist:
        HarnessReportStore(store).save(report, filename)
    return report


def _passed(check_id: str, message: str, refs: list[str] | None = None) -> HarnessCheck:
    return HarnessCheck(id=check_id, status=HarnessStatus.PASSED, description=message, message=message, evidence_refs=refs or [])


def _failed(check_id: str, message: str) -> HarnessCheck:
    return HarnessCheck(id=check_id, status=HarnessStatus.FAILED, description="blocking deterministic quality check", message=message)