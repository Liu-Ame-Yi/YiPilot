"""Atomic, run-scoped persistence for Harness reports."""

from pathlib import Path

from pydantic import ValidationError

from yipilot.domain.errors import DomainError
from yipilot.harness.models import HarnessReport
from yipilot.persistence.filesystem import atomic_write_json, project_path
from yipilot.persistence.task_store import FileTaskStore
from yipilot.security.redaction import SecretRedactor

TASK_INTEGRITY_REPORT_NAME = "task-integrity-report.json"
CODE_HARNESS_REPORT_NAME = "code-harness-report.json"
SRS_HARNESS_REPORT_NAME = "srs-harness-report.json"
SDD_HARNESS_REPORT_NAME = "sdd-harness-report.json"
UNIT_TEST_HARNESS_REPORT_NAME = "unit-test-harness-report.json"


class HarnessReportStore:
    """Save and load reports beneath an existing Run using Store-owned paths."""

    def __init__(self, task_store: FileTaskStore) -> None:
        self._task_store = task_store

    def report_path(
        self, run_id: str, report_name: str = TASK_INTEGRITY_REPORT_NAME
    ) -> Path:
        run_root = self._task_store.event_journal_path(run_id).parent
        return project_path(
            run_root,
            "harness",
            report_name,
        )

    def save(
        self, report: HarnessReport, report_name: str = TASK_INTEGRITY_REPORT_NAME
    ) -> Path:
        self._validate_run_association(report)
        path = self.report_path(report.run_id, report_name)
        value = SecretRedactor().redact(report.model_dump(mode="json"))
        assert isinstance(value, dict)
        atomic_write_json(path, value, HarnessReport.model_validate)
        return path

    def load(
        self, run_id: str, report_name: str = TASK_INTEGRITY_REPORT_NAME
    ) -> HarnessReport:
        path = self.report_path(run_id, report_name)
        if not path.is_file():
            raise DomainError("HARNESS_REPORT_NOT_FOUND", "report not found")
        try:
            report = HarnessReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise DomainError(
                "HARNESS_REPORT_INVALID",
                "task integrity report is invalid",
            ) from error
        self._validate_run_association(report)
        return report

    def _validate_run_association(self, report: HarnessReport) -> None:
        run = self._task_store.find_run(report.run_id)
        if (
            run.task_id != report.task_id
            or run.operation_id != report.operation_id
            or run.stage != report.stage
        ):
            raise DomainError(
                "HARNESS_REPORT_MISMATCH",
                "report does not belong to the persisted run",
            )
