from enum import StrEnum

from .errors import DomainError


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED_TRANSITIONS = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.PAUSED,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
}


def validate_task_id(value: str) -> str:
    if value == "0" or (value.isdecimal() and not value.startswith("0")):
        return value
    raise DomainError(
        "INVALID_TASK_ID",
        "task id must be a non-negative decimal without leading zeros",
    )


def transition_run(current: RunStatus, target: RunStatus) -> RunStatus:
    if current not in _ALLOWED_TRANSITIONS:
        raise DomainError("RUN_TERMINAL", f"cannot transition terminal run {current}")
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise DomainError(
            "RUN_TRANSITION_INVALID", f"cannot transition {current} to {target}"
        )
    return target
