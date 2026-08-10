"""Controlled argv-only execution for Git commands."""

import re
import subprocess
from pathlib import Path
from typing import TypeVar

from yipilot.domain.errors import DomainError

_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@")
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+")
_BEARER = re.compile(r"(?i)(bearer\s+)\S+")
_SECRET_PARAMETER = re.compile(
    r"(?i)(\b(?:access[_-]?token|api[_-]?key|password|secret|token)=)[^\s&]+"
)
_Output = TypeVar("_Output", str, bytes)


class GitRunner:
    """Run only explicit Git argv lists without invoking a shell."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        _validate_argv(argv)

        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                timeout=timeout,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DomainError("GIT_TIMEOUT", "git command timed out") from error
        except OSError as error:
            raise DomainError("GIT_FAILED", "git command could not be started") from error

        _check_returncode(argv, completed)
        return completed

    def run_bytes(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run Git while preserving stdout and stderr as exact bytes."""
        _validate_argv(argv)
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                timeout=timeout,
                shell=False,
                capture_output=True,
                text=False,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DomainError("GIT_TIMEOUT", "git command timed out") from error
        except OSError as error:
            raise DomainError("GIT_FAILED", "git command could not be started") from error

        _check_returncode(argv, completed)
        return completed


def _validate_argv(argv: list[str]) -> None:
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != "git"
        or any(not isinstance(argument, str) or "\x00" in argument for argument in argv)
    ):
        raise DomainError(
            "GIT_ARGV_INVALID",
            "git commands must be NUL-free argv lists beginning with git",
        )


def _check_returncode(
    argv: list[str], completed: subprocess.CompletedProcess[_Output]
) -> None:
    try:
        separator = argv.index("--")
    except ValueError:
        separator = -1
    no_index_difference = (
        completed.returncode == 1
        and argv[1:3] == ["diff", "--no-index"]
        and separator >= 3
        and len(argv[separator + 1 :]) == 2
        and bool(completed.stdout)
    )
    if completed.returncode == 0 or no_index_difference:
        return
    raw_stderr = completed.stderr
    stderr_text = (
        raw_stderr.decode("utf-8", errors="replace")
        if isinstance(raw_stderr, bytes)
        else raw_stderr
    )
    summary = _safe_stderr_summary(stderr_text)
    message = "git command failed"
    if summary:
        message = f"{message}: {summary}"
    raise DomainError("GIT_FAILED", message)


def _safe_stderr_summary(stderr: str) -> str:
    summary = " ".join(stderr.strip().split())
    summary = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", summary)
    summary = _AUTHORIZATION.sub(r"\1[REDACTED]", summary)
    summary = _BEARER.sub(r"\1[REDACTED]", summary)
    summary = _SECRET_PARAMETER.sub(r"\1[REDACTED]", summary)
    return summary[:500]
