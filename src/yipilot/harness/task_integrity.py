"""Deterministic integrity checks for one persisted Task and Run."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from yipilot.domain.errors import DomainError
from yipilot.harness.models import (
    HarnessCheck,
    HarnessReport,
    HarnessStatus,
    aggregate_checks,
)
from yipilot.harness.report_store import HarnessReportStore
from yipilot.infrastructure.event_journal import EventRecord, JsonlEventJournal
from yipilot.infrastructure.filesystem import project_path
from yipilot.infrastructure.task_store import FileTaskStore, RunRecord, TaskRecord

_HARNESS_NAME = "task_integrity"
_HARNESS_VERSION = "1.0.0"


class TaskIntegrityHarness:
    """Validate task/run ownership, events and run-scoped artifact references."""

    def __init__(self, project_root: Path, task_id: str, run_id: str) -> None:
        self._project_root = Path(project_root)
        self._task_id = task_id
        self._run_id = run_id
        self._store = FileTaskStore(self._project_root)

    def run(self) -> HarnessReport:
        started_at = datetime.now(UTC)
        task, task_check = self._check_task()
        run, run_check = self._check_run(task)
        events, events_check = self._check_events(run)
        artifact_refs, artifacts_check = self._check_artifacts(run, events)
        checks = [task_check, run_check, events_check, artifacts_check]
        inputs = self._input_summary(run, events, artifact_refs, checks)
        status = aggregate_checks(checks).status
        report = HarnessReport(
            report_id=str(uuid4()),
            task_id=self._task_id,
            operation_id=run.operation_id if run is not None else "unknown",
            run_id=self._run_id,
            stage=run.stage if run is not None else "unknown",
            harness_name=_HARNESS_NAME,
            harness_version=_HARNESS_VERSION,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            input_fingerprint=_fingerprint(inputs),
            inputs=inputs,
            status=status,
            checks=checks,
            summary=f"task integrity checks completed with status {status}",
        )
        if run is not None:
            HarnessReportStore(self._store).save(report)
        return report

    def _check_task(self) -> tuple[TaskRecord | None, HarnessCheck]:
        evidence = [f"tasks/{self._task_id}/task.yaml"]
        try:
            task = self._store.get_task(self._task_id)
        except DomainError as error:
            return None, HarnessCheck(
                id="TASK-INTEGRITY-001",
                status=HarnessStatus.FAILED,
                blocking=True,
                description="task.yaml exists and validates against the Task schema",
                message=f"task record is missing or invalid ({error.code})",
                suggestion="initialize or repair the task record before continuing",
                evidence_refs=evidence,
            )
        return task, HarnessCheck(
            id="TASK-INTEGRITY-001",
            status=HarnessStatus.PASSED,
            blocking=True,
            description="task.yaml exists and validates against the Task schema",
            message="task record is present and valid",
            evidence_refs=evidence,
        )

    def _check_run(
        self, task: TaskRecord | None
    ) -> tuple[RunRecord | None, HarnessCheck]:
        evidence = [f"tasks/{self._task_id}/runs/{self._run_id}/run.json"]
        if task is None:
            return None, HarnessCheck(
                id="TASK-INTEGRITY-002",
                status=HarnessStatus.SKIPPED,
                blocking=True,
                description="run belongs to the requested task",
                message="run ownership cannot be checked without a valid task",
                suggestion="resolve TASK-INTEGRITY-001 and run the harness again",
                evidence_refs=evidence,
            )
        try:
            run = self._store.get_run(task.task_id, self._run_id)
        except DomainError as error:
            return None, HarnessCheck(
                id="TASK-INTEGRITY-002",
                status=HarnessStatus.FAILED,
                blocking=True,
                description="run belongs to the requested task",
                message=f"run is missing, invalid, or belongs to another task ({error.code})",
                suggestion="use a run created for this task",
                evidence_refs=evidence,
            )
        return run, HarnessCheck(
            id="TASK-INTEGRITY-002",
            status=HarnessStatus.PASSED,
            blocking=True,
            description="run belongs to the requested task",
            message="run record belongs to the requested task",
            evidence_refs=evidence,
        )

    def _check_events(
        self, run: RunRecord | None
    ) -> tuple[list[EventRecord] | None, HarnessCheck]:
        evidence = [f"tasks/{self._task_id}/runs/{self._run_id}/events.jsonl"]
        if run is None:
            return None, HarnessCheck(
                id="TASK-INTEGRITY-003",
                status=HarnessStatus.SKIPPED,
                blocking=True,
                description="events.jsonl sequence is contiguous",
                message="event sequence cannot be checked without a valid run",
                suggestion="resolve TASK-INTEGRITY-002 and run the harness again",
                evidence_refs=evidence,
            )
        journal = JsonlEventJournal(
            self._store.event_journal_path(run.run_id),
            task_id=run.task_id,
            run_id=run.run_id,
            stage=run.stage,
        )
        try:
            events = journal.read_after(0)
        except DomainError as error:
            return None, HarnessCheck(
                id="TASK-INTEGRITY-003",
                status=HarnessStatus.FAILED,
                blocking=True,
                description="events.jsonl sequence is contiguous",
                message=f"event journal is invalid ({error.code})",
                suggestion="preserve the journal and repair its sequence before continuing",
                evidence_refs=evidence,
            )
        return events, HarnessCheck(
            id="TASK-INTEGRITY-003",
            status=HarnessStatus.PASSED,
            blocking=True,
            description="events.jsonl sequence is contiguous",
            message=f"event sequence is contiguous across {len(events)} event(s)",
            evidence_refs=evidence,
        )

    def _check_artifacts(
        self,
        run: RunRecord | None,
        events: list[EventRecord] | None,
    ) -> tuple[list[str], HarnessCheck]:
        if run is None or events is None:
            return [], HarnessCheck(
                id="TASK-INTEGRITY-004",
                status=HarnessStatus.SKIPPED,
                blocking=True,
                description="artifact references are safe paths relative to the run",
                message="artifact references cannot be checked without valid run events",
                suggestion="resolve the preceding integrity check and run the harness again",
            )

        artifact_refs: list[str] = []
        for event in events:
            value = event.payload.get("artifact_refs")
            if value is None:
                continue
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                return artifact_refs, _artifact_failure(
                    artifact_refs,
                    "artifact_refs must be a list of relative path strings",
                )
            artifact_refs.extend(value)

        run_root = self._store.event_journal_path(run.run_id).parent
        for reference in artifact_refs:
            path = Path(reference)
            try:
                if (
                    not reference
                    or path == Path(".")
                    or path.is_absolute()
                    or bool(path.drive)
                ):
                    raise DomainError("PATH_DENIED", "artifact path is not relative")
                project_path(run_root, reference)
            except (DomainError, OSError):
                return artifact_refs, _artifact_failure(
                    artifact_refs,
                    f"artifact reference is outside the run: {reference}",
                )

        return artifact_refs, HarnessCheck(
            id="TASK-INTEGRITY-004",
            status=HarnessStatus.PASSED,
            blocking=True,
            description="artifact references are safe paths relative to the run",
            message=f"validated {len(artifact_refs)} artifact reference(s)",
            evidence_refs=artifact_refs,
        )

    def _input_summary(
        self,
        run: RunRecord | None,
        events: list[EventRecord] | None,
        artifact_refs: list[str],
        checks: list[HarnessCheck],
    ) -> list[dict[str, JsonValue]]:
        serialized_artifact_refs: list[JsonValue] = list(artifact_refs)
        return [
            {
                "kind": "task",
                "task_id": self._task_id,
                "check_status": checks[0].status.value,
            },
            {
                "kind": "run",
                "run_id": self._run_id,
                "operation_id": run.operation_id if run is not None else "unknown",
                "stage": run.stage if run is not None else "unknown",
                "check_status": checks[1].status.value,
            },
            {
                "kind": "events",
                "event_count": len(events) if events is not None else 0,
                "check_status": checks[2].status.value,
            },
            {
                "kind": "artifacts",
                "refs": serialized_artifact_refs,
                "check_status": checks[3].status.value,
            },
        ]


def _artifact_failure(evidence_refs: list[str], message: str) -> HarnessCheck:
    return HarnessCheck(
        id="TASK-INTEGRITY-004",
        status=HarnessStatus.FAILED,
        blocking=True,
        description="artifact references are safe paths relative to the run",
        message=message,
        suggestion="store artifact references as safe paths beneath the run directory",
        evidence_refs=evidence_refs,
    )


def _fingerprint(inputs: list[dict[str, JsonValue]]) -> str:
    encoded = json.dumps(
        inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
