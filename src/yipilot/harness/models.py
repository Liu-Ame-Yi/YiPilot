"""Versioned Harness report models, aggregation and downstream gate policy."""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class HarnessStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


class HarnessCheck(BaseModel):
    """One stable, auditable deterministic check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    status: HarnessStatus
    blocking: bool = True
    description: str = ""
    message: str = ""
    suggestion: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class HarnessAggregate(BaseModel):
    """The versioned outcome of applying the Harness aggregation policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Literal[1] = 1
    status: HarnessStatus


def aggregate_checks(checks: Sequence[HarnessCheck]) -> HarnessAggregate:
    """Aggregate checks without treating non-blocking findings as terminal."""
    if not checks:
        return HarnessAggregate(status=HarnessStatus.SKIPPED)

    blocking = [check for check in checks if check.blocking]
    if any(check.status is HarnessStatus.FAILED for check in blocking):
        status = HarnessStatus.FAILED
    elif any(check.status is HarnessStatus.SKIPPED for check in blocking):
        status = HarnessStatus.SKIPPED
    elif any(check.status is not HarnessStatus.PASSED for check in checks):
        status = HarnessStatus.WARNING
    else:
        status = HarnessStatus.PASSED
    return HarnessAggregate(status=status)


class HarnessReport(BaseModel):
    """A versioned report tied to one Task, Operation, Run and Stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    aggregation_policy_version: Literal[1] = 1
    report_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    harness_name: str = Field(min_length=1)
    harness_version: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    input_fingerprint: str = Field(min_length=1)
    inputs: list[dict[str, JsonValue]]
    status: HarnessStatus
    checks: list[HarnessCheck] = Field(min_length=1)
    summary: str

    @model_validator(mode="after")
    def validate_report_outcome(self) -> "HarnessReport":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        expected = aggregate_checks(self.checks).status
        if self.status is not expected:
            raise ValueError("report status does not match its checks")
        return self


class HarnessGatePolicy(BaseModel):
    """Explicit downstream policy for accepting a Harness outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    allow_warning: bool = False

    def accepts(self, status: HarnessStatus | str) -> bool:
        try:
            outcome = HarnessStatus(status)
        except ValueError:
            return False
        return outcome is HarnessStatus.PASSED or (
            self.allow_warning and outcome is HarnessStatus.WARNING
        )
