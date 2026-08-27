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
from yipilot.domain.execution import (
    QUALITY_PYTEST_POLICY_REVISION,
    ToolKind,
    ToolResult,
    ToolRunStatus,
)
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
from yipilot.infrastructure.provider_store import FileProviderStore
from yipilot.infrastructure.task_store import FileTaskStore
from yipilot.infrastructure.tool_run_store import FileToolRunStore

_SRS_HEADINGS = (
    "# SRS",
    "## Goals and scope",
    "## Functional requirements",
    "## Non-functional requirements",
    "## Acceptance criteria",
    "## Assumptions and constraints",
)
_SDD_HEADINGS = (
    "# SDD",
    "## Architecture and components",
    "## Interfaces and data flow",
    "## Constraints",
    "## Error handling",
    "## Test traceability",
)
_PLAN_HEADINGS = (
    "# Test Plan",
    "## Test scope",
    "## Preconditions",
    "## Test cases and commands",
    "## Expected results",
    "## Requirements and design references",
)


class SrsHarness:
    """Block a malformed or changed SRS and its PRD inputs."""

    def __init__(self, project_root: Path, task_id: str, run_id: str) -> None:
        self._project_root, self._task_id, self._run_id = (
            Path(project_root),
            task_id,
            run_id,
        )
        self._store = FileTaskStore(self._project_root)

    def run(self) -> HarnessReport:
        return self._evaluate(True)

    def verify(self) -> HarnessReport:
        return self._evaluate(False)

    def _evaluate(self, persist: bool) -> HarnessReport:
        artifacts = FileArtifactStore(self._store)
        metadata, metadata_check = _metadata(artifacts, self._task_id, self._run_id)
        srs, srs_check = _record(
            artifacts,
            self._task_id,
            self._run_id,
            "artifacts/srs.md",
            "SRS-ARTIFACT-001",
        )
        prd, input_check = _record(
            artifacts, self._task_id, self._run_id, "artifacts/prd.md", "SRS-INPUT-001"
        )
        plan, plan_check = _record(
            artifacts,
            self._task_id,
            self._run_id,
            "artifacts/test-plan.md",
            "TEST-PLAN-ARTIFACT-001",
        )
        checks = [srs_check, input_check, plan_check, metadata_check]
        if srs is not None:
            text = _read(artifacts, srs)
            checks.extend(
                (
                    _structure("SRS-STRUCTURE-001", text, _SRS_HEADINGS),
                    _requirements(text),
                )
            )
        if plan is not None:
            plan_text = _read(artifacts, plan)
            checks.append(
                _structure("TEST-PLAN-STRUCTURE-001", plan_text, _PLAN_HEADINGS)
            )
            checks.append(
                _passed(
                    "TEST-PLAN-REFERENCES-001",
                    "test plan has requirement and design references",
                    [plan.relative_path],
                )
                if re.search(r"\bFR-\d{3}\b", plan_text)
                and "SRS Artifact:" in plan_text
                and "SDD Artifact:" in plan_text
                else _failed(
                    "TEST-PLAN-REFERENCES-001",
                    "test plan lacks requirement or design references",
                )
            )
        if metadata is not None and srs is not None and prd is not None:
            records = {"srs": srs, "prd": prd}
            if plan is not None:
                records["test_plan"] = plan
            checks.append(_meta_hashes("SRS-METADATA-001", metadata, records))
            checks.append(_fingerprint_check("SRS-FINGERPRINT-001", metadata, prd))
            checks.append(_upstream_prd_check(artifacts, metadata, self._run_id))
        return _report(
            self._store,
            self._task_id,
            self._run_id,
            "srs_harness",
            "1.1.0",
            checks,
            metadata,
            persist,
            SRS_HARNESS_REPORT_NAME,
        )


class SddHarness:
    """Block a malformed SDD, stale upstream input, or stale provider profile."""

    def __init__(
        self,
        project_root: Path,
        task_id: str,
        run_id: str,
        *,
        profile_revision: int | None = None,
        settings_dir: Path | None = None,
        semantic_review: bool = False,
    ) -> None:
        self._project_root, self._task_id, self._run_id = (
            Path(project_root),
            task_id,
            run_id,
        )
        self._profile_revision, self._semantic_review = (
            profile_revision,
            semantic_review,
        )
        self._providers = FileProviderStore(settings_dir)
        self._store = FileTaskStore(self._project_root)

    def run(self) -> HarnessReport:
        return self._evaluate(True)

    def verify(self) -> HarnessReport:
        return self._evaluate(False)

    def _evaluate(self, persist: bool) -> HarnessReport:
        artifacts = FileArtifactStore(self._store)
        metadata, metadata_check = _metadata(artifacts, self._task_id, self._run_id)
        sdd, sdd_check = _record(
            artifacts,
            self._task_id,
            self._run_id,
            "artifacts/sdd.md",
            "SDD-ARTIFACT-001",
        )
        srs, srs_check = _record(
            artifacts,
            self._task_id,
            self._run_id,
            "artifacts/srs.md",
            "SDD-SRS-ARTIFACT-001",
        )
        prd, prd_check = _record(
            artifacts,
            self._task_id,
            self._run_id,
            "artifacts/prd.md",
            "SDD-PRD-ARTIFACT-001",
        )
        checks = [sdd_check, srs_check, prd_check, metadata_check]
        if sdd is not None:
            text = _read(artifacts, sdd)
            checks.append(_structure("SDD-STRUCTURE-001", text, _SDD_HEADINGS))
            if srs is not None:
                checks.append(
                    _passed(
                        "SDD-SRS-REFERENCE-001",
                        "SDD references the current SRS Artifact",
                        [srs.relative_path],
                    )
                    if srs.artifact_id in text
                    else _failed(
                        "SDD-SRS-REFERENCE-001",
                        "SDD does not reference the current SRS Artifact",
                    )
                )
        if (
            metadata is not None
            and sdd is not None
            and srs is not None
            and prd is not None
        ):
            checks.extend(
                (
                    _meta_hashes(
                        "SDD-METADATA-001",
                        metadata,
                        {"prd": prd, "srs": srs, "sdd": sdd},
                    ),
                    _fingerprint_check("SDD-FINGERPRINT-001", metadata, prd),
                    _upstream_prd_check(
                        artifacts,
                        metadata,
                        self._run_id,
                        "SDD-UPSTREAM-001",
                    ),
                    _provider_check(
                        self._providers,
                        metadata,
                        self._profile_revision,
                    ),
                )
            )
        checks.append(_semantic_check(self._semantic_review))
        return _report(
            self._store,
            self._task_id,
            self._run_id,
            "sdd_harness",
            "1.1.0",
            checks,
            metadata,
            persist,
            SDD_HARNESS_REPORT_NAME,
        )


class UnitTestHarness:
    """Validate one fixed Python/pytest ToolRun and its workspace evidence."""

    def __init__(
        self,
        project_root: Path,
        task_id: str,
        run_id: str,
        workspace: PreparedWorkspace,
        tool_run_id: str,
    ) -> None:
        self._project_root, self._task_id, self._run_id = (
            Path(project_root),
            task_id,
            run_id,
        )
        self._workspace, self._tool_run_id = workspace, tool_run_id
        self._store = FileTaskStore(self._project_root)

    def run(self) -> HarnessReport:
        return self._evaluate(True)

    def verify(self) -> HarnessReport:
        return self._evaluate(False)

    def _evaluate(self, persist: bool) -> HarnessReport:
        checks: list[HarnessCheck] = []
        artifacts = FileArtifactStore(self._store)
        try:
            result = FileToolRunStore(self._store).get(
                self._task_id, self._run_id, self._tool_run_id
            )
            checks.append(
                _pytest_report_check(artifacts, self._task_id, self._run_id, result)
            )
            checks.append(
                _passed(
                    "PYTEST-TOOLRUN-001",
                    "pytest ToolRun and bounded stream Artifacts are intact",
                    [f"tool-runs/{result.tool_run_id}.json"],
                )
            )
            checks.append(
                _passed(
                    "PYTEST-COMMAND-001", "registered argv is fixed Python -m pytest"
                )
                if len(result.argv) == 3 and result.argv[1:] == ("-m", "pytest")
                else _failed(
                    "PYTEST-COMMAND-001", "ToolRun is not the fixed pytest command"
                )
            )
            checks.append(
                _passed("PYTEST-EXIT-001", "pytest exited successfully")
                if result.status is ToolRunStatus.SUCCEEDED and result.exit_code == 0
                else _failed(
                    "PYTEST-EXIT-001", f"pytest terminal status is {result.status}"
                )
            )
            approvals = FileApprovalStore(self._store).list(self._task_id, self._run_id)
            action = f"tool:{ToolKind.TEST.value}:{result.trust_level.value}"
            approved = any(
                a.task_id == result.task_id
                and a.operation_id == result.operation_id
                and a.run_id == result.run_id
                and a.action_type == action
                and a.target_fingerprint == result.request_fingerprint
                and a.policy_revision == QUALITY_PYTEST_POLICY_REVISION
                and a.decision.value == "approved"
                for a in approvals
            )
            checks.append(
                _passed("PYTEST-APPROVAL-001", "fresh pytest approval exists")
                if approved
                else _failed(
                    "PYTEST-APPROVAL-001", "pytest Approval is missing or stale"
                )
            )
        except DomainError as error:
            checks.extend(
                (
                    _failed("PYTEST-REPORT-001", error.code),
                    _failed("PYTEST-TOOLRUN-001", error.code),
                    _failed("PYTEST-COMMAND-001", "ToolRun unavailable"),
                    _failed("PYTEST-EXIT-001", "ToolRun unavailable"),
                    _failed("PYTEST-APPROVAL-001", "ToolRun unavailable"),
                )
            )
        git = GitWorkspaceHarness(self._workspace)
        git_report = git.run() if persist else git.verify()
        checks.append(
            HarnessCheck(
                id="PYTEST-GIT-EVIDENCE-001",
                status=HarnessStatus.PASSED
                if git_report.status is HarnessStatus.PASSED
                else HarnessStatus.FAILED,
                description="workspace HEAD and Git evidence are current",
                message=f"Git workspace status is {git_report.status}",
                evidence_refs=[
                    ref for check in git_report.checks for ref in check.evidence_refs
                ],
            )
        )
        return _report(
            self._store,
            self._task_id,
            self._run_id,
            "unit_test_harness",
            "1.1.0",
            checks,
            {"git_fingerprint": git_report.input_fingerprint},
            persist,
            UNIT_TEST_HARNESS_REPORT_NAME,
        )


def _pytest_report_check(
    artifacts: FileArtifactStore, task_id: str, run_id: str, result: ToolResult
) -> HarnessCheck:
    record, check = _record(
        artifacts, task_id, run_id, "artifacts/pytest-report.json", "PYTEST-REPORT-001"
    )
    if record is None:
        return check
    try:
        artifacts.verify(task_id, run_id, result.stdout_artifact_ref.artifact_id)
        artifacts.verify(task_id, run_id, result.stderr_artifact_ref.artifact_id)
        value = json.loads(_read(artifacts, record))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise TypeError
        valid = (
            value.get("tool_run_id") == result.tool_run_id
            and value.get("status") == result.status.value
            and value.get("exit_code") == result.exit_code
            and value.get("stdout_artifact_ref")
            == result.stdout_artifact_ref.model_dump(mode="json")
            and value.get("stderr_artifact_ref")
            == result.stderr_artifact_ref.model_dump(mode="json")
        )
        return (
            _passed(
                "PYTEST-REPORT-001",
                "pytest report matches intact ToolRun and stream Artifacts",
                [record.relative_path],
            )
            if valid
            else _failed(
                "PYTEST-REPORT-001", "pytest report does not match ToolRun evidence"
            )
        )
    except (DomainError, TypeError, json.JSONDecodeError):
        return _failed("PYTEST-REPORT-001", "pytest report is invalid or stale")


def _metadata(
    artifacts: FileArtifactStore, task_id: str, run_id: str
) -> tuple[dict[str, JsonValue] | None, HarnessCheck]:
    record, check = _record(
        artifacts,
        task_id,
        run_id,
        "artifacts/quality-metadata.json",
        "QUALITY-METADATA-001",
    )
    if record is None:
        return None, check
    try:
        value = json.loads(_read(artifacts, record))
        if not isinstance(value, dict):
            raise TypeError
        return value, check
    except (TypeError, json.JSONDecodeError):
        return None, _failed("QUALITY-METADATA-001", "quality metadata is invalid")


def _record(
    artifacts: FileArtifactStore,
    task_id: str,
    run_id: str,
    relative_path: str,
    check_id: str,
) -> tuple[ArtifactRecord | None, HarnessCheck]:
    try:
        record = next(
            item
            for item in artifacts.list(task_id, run_id)
            if item.relative_path == relative_path
        )
        artifacts.verify(task_id, run_id, record.artifact_id)
        return record, _passed(check_id, f"{relative_path} is intact", [relative_path])
    except (DomainError, StopIteration) as error:
        return None, _failed(check_id, getattr(error, "code", "ARTIFACT_NOT_FOUND"))


def _read(artifacts: FileArtifactStore, record: ArtifactRecord) -> str:
    root = artifacts.task_store.event_journal_path(record.run_id).parent
    return (root / record.relative_path).read_text(encoding="utf-8")


def _structure(check_id: str, text: str, headings: tuple[str, ...]) -> HarnessCheck:
    missing = [heading for heading in headings if heading not in text]
    return (
        _passed(check_id, "required document structure is present")
        if not missing
        else _failed(check_id, f"missing headings: {', '.join(missing)}")
    )


def _requirements(text: str) -> HarnessCheck:
    return (
        _passed("SRS-REQUIREMENTS-001", "SRS has stable requirement identifiers")
        if re.search(r"\b(?:FR|NFR)-\d{3}\b", text)
        else _failed("SRS-REQUIREMENTS-001", "SRS has no requirement identifiers")
    )


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


def _fingerprint_check(
    check_id: str, metadata: dict[str, JsonValue], prd: ArtifactRecord
) -> HarnessCheck:
    return (
        _passed(check_id, "input fingerprint matches PRD")
        if metadata.get("input_fingerprint") == prd.sha256
        else _failed(check_id, "input fingerprint does not match PRD")
    )


def _upstream_prd_check(
    artifacts: FileArtifactStore,
    metadata: dict[str, JsonValue],
    run_id: str,
    check_id: str = "SRS-UPSTREAM-001",
) -> HarnessCheck:
    refs = metadata.get("upstream_artifact_refs")
    if not isinstance(refs, list):
        return _failed(check_id, "upstream Artifact refs are invalid")
    source_refs = [
        ref
        for ref in refs
        if isinstance(ref, dict)
        and ref.get("run_id") != run_id
        and isinstance(ref.get("relative_path"), str)
        and str(ref["relative_path"]).endswith(".md")
    ]
    if not source_refs:
        return _passed(check_id, "explicit PRD input has no upstream Artifact")
    for ref in source_refs:
        try:
            source_task, source_run, artifact_id = (
                ref["task_id"],
                ref["run_id"],
                ref["artifact_id"],
            )
            if (
                not isinstance(source_task, str)
                or not isinstance(source_run, str)
                or not isinstance(artifact_id, str)
            ):
                raise TypeError
            record = artifacts.verify(source_task, source_run, artifact_id)
            if record.relative_path != ref.get(
                "relative_path"
            ) or record.sha256 != ref.get("sha256"):
                return _failed(check_id, "upstream PRD Artifact is stale")
        except (DomainError, TypeError):
            return _failed(check_id, "upstream PRD Artifact is missing or invalid")
    return _passed(
        check_id,
        "upstream PRD Artifact is intact",
        ["upstream_artifact_refs"],
    )


def _provider_check(
    providers: FileProviderStore,
    metadata: dict[str, JsonValue],
    offline_revision: int | None,
) -> HarnessCheck:
    profile = metadata.get("provider_profile")
    if not isinstance(profile, dict):
        return _failed(
            "SDD-PROFILE-REVISION-001", "provider profile metadata is invalid"
        )
    name, revision, model = (
        profile.get("name"),
        profile.get("revision"),
        profile.get("model"),
    )
    if (
        not isinstance(name, str)
        or not isinstance(revision, int)
        or not isinstance(model, str)
    ):
        return _failed(
            "SDD-PROFILE-REVISION-001", "provider profile metadata is invalid"
        )
    if name == "offline-quality":
        if offline_revision is None:
            return _failed(
                "SDD-PROFILE-REVISION-001", "OFFLINE_PROFILE_REVISION_REQUIRED"
            )
        current_revision, current_model = offline_revision, "quality-fixture"
    else:
        try:
            current = providers.get(name)
        except DomainError as error:
            return _failed("SDD-PROFILE-REVISION-001", error.code)
        current_revision, current_model = current.revision, current.model
    return (
        _passed("SDD-PROFILE-REVISION-001", "provider profile is current")
        if (revision, model) == (current_revision, current_model)
        else _failed("SDD-PROFILE-REVISION-001", "provider profile is stale")
    )


def _semantic_check(enabled: bool) -> HarnessCheck:
    if enabled:
        return _failed("SDD-SEMANTIC-REVIEW-001", "SEMANTIC_REVIEW_NOT_IMPLEMENTED")
    return HarnessCheck(
        id="SDD-SEMANTIC-REVIEW-001",
        status=HarnessStatus.PASSED,
        blocking=False,
        description="optional model semantic review",
        message="disabled by default; no network request was made",
    )


def _report(
    store: FileTaskStore,
    task_id: str,
    run_id: str,
    name: str,
    version: str,
    checks: list[HarnessCheck],
    metadata: object,
    persist: bool,
    filename: str,
) -> HarnessReport:
    run = store.get_run(task_id, run_id)
    inputs: list[dict[str, JsonValue]] = [
        {"metadata": metadata if isinstance(metadata, dict) else {}}
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            inputs, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    status = aggregate_checks(checks).status
    report = HarnessReport(
        report_id=str(uuid4()),
        task_id=task_id,
        operation_id=run.operation_id,
        run_id=run_id,
        stage=run.stage,
        harness_name=name,
        harness_version=version,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
        input_fingerprint=fingerprint,
        inputs=inputs,
        status=status,
        checks=checks,
        summary=f"{name} completed with status {status}",
    )
    if persist:
        HarnessReportStore(store).save(report, filename)
    return report


def _passed(check_id: str, message: str, refs: list[str] | None = None) -> HarnessCheck:
    return HarnessCheck(
        id=check_id,
        status=HarnessStatus.PASSED,
        description=message,
        message=message,
        evidence_refs=refs or [],
    )


def _failed(check_id: str, message: str) -> HarnessCheck:
    return HarnessCheck(
        id=check_id,
        status=HarnessStatus.FAILED,
        description="blocking deterministic quality check",
        message=message,
    )
