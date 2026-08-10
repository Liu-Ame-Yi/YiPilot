from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from yipilot.domain.errors import DomainError


@contextmanager
def _passthrough_context() -> Iterator[None]:
    yield


def test_domain_error_crosses_context_manager_without_masking() -> None:
    error = DomainError("EXPECTED_FAILURE", "expected failure")

    with pytest.raises(DomainError) as exc_info, _passthrough_context():
        raise error

    assert exc_info.value is error
    assert exc_info.value.code == "EXPECTED_FAILURE"
    assert exc_info.value.message == "expected failure"
    assert str(exc_info.value) == "EXPECTED_FAILURE: expected failure"
    assert exc_info.value.__traceback__ is not None


def test_domain_error_preserves_string_format() -> None:
    error = DomainError("INVALID_TASK_ID", "task ID must be canonical")

    assert str(error) == "INVALID_TASK_ID: task ID must be canonical"


def test_domain_error_supports_ordinary_raise_and_catch() -> None:
    error = DomainError("EXPECTED_FAILURE", "expected failure")

    try:
        raise error
    except DomainError as caught:
        assert caught is error
        assert caught.code == "EXPECTED_FAILURE"
        assert caught.message == "expected failure"
        assert caught.__traceback__ is not None


def test_domain_error_preserves_explicit_cause() -> None:
    cause = ValueError("invalid input")
    error = DomainError("EXPECTED_FAILURE", "expected failure")

    with pytest.raises(DomainError) as exc_info:
        try:
            raise cause
        except ValueError as caught:
            raise error from caught

    assert exc_info.value is error
    assert exc_info.value.__cause__ is cause
    assert exc_info.value.__context__ is cause
    assert exc_info.value.__traceback__ is not None
