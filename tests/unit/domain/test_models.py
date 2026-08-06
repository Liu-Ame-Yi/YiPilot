import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.models import RunStatus, transition_run, validate_task_id


@pytest.mark.parametrize("task_id", ["0", "1001"])
def test_validate_task_id_accepts_canonical_non_negative_decimal_ids(task_id: str) -> None:
    assert validate_task_id(task_id) == task_id


@pytest.mark.parametrize("task_id", ["-1", "01", "x", ""])
def test_validate_task_id_rejects_non_canonical_ids(task_id: str) -> None:
    with pytest.raises(DomainError, match="^INVALID_TASK_ID:") as exc_info:
        validate_task_id(task_id)

    assert exc_info.value.code == "INVALID_TASK_ID"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.PENDING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.PAUSED),
        (RunStatus.PAUSED, RunStatus.FAILED),
    ],
)
def test_validate_run_transition_allows_permitted_transitions(
    current: RunStatus, target: RunStatus
) -> None:
    transition_run(current, target)


def test_validate_run_transition_rejects_non_terminal_target_from_pending() -> None:
    with pytest.raises(DomainError, match="^RUN_TRANSITION_INVALID:") as exc_info:
        transition_run(RunStatus.PENDING, RunStatus.SUCCEEDED)

    assert exc_info.value.code == "RUN_TRANSITION_INVALID"


@pytest.mark.parametrize(
    "current", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED]
)
def test_validate_run_transition_rejects_any_transition_from_terminal_run(
    current: RunStatus,
) -> None:
    with pytest.raises(DomainError, match="^RUN_TERMINAL:") as exc_info:
        transition_run(current, RunStatus.RUNNING)

    assert exc_info.value.code == "RUN_TERMINAL"


def test_domain_error_stringifies_code_and_message() -> None:
    error = DomainError(code="INVALID_TASK_ID", message="task ID must be canonical")

    assert str(error) == "INVALID_TASK_ID: task ID must be canonical"
