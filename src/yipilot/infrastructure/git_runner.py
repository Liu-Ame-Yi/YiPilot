"""Controlled argv-only execution for Git commands."""

import re
import subprocess
from pathlib import Path

from yipilot.domain.errors import DomainError

_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@")
_AUTHORIZATION = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+")
_BEARER = re.compile(r"(?i)(bearer\s+)\S+")
_SECRET_PARAMETER = re.compile(
    r"(?i)(\b(?:access[_-]?token|api[_-]?key|password|secret|token)=)[^\s&]+"
)


class GitRunner:
    """Run only explicit Git argv lists without invoking a shell."""

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
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

        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                timeout=timeout,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DomainError("GIT_TIMEOUT", "git command timed out") from error
        except OSError as error:
            raise DomainError("GIT_FAILED", "git command could not be started") from error

        if completed.returncode != 0:
            summary = _safe_stderr_summary(completed.stderr)
            message = "git command failed"
            if summary:
                message = f"{message}: {summary}"
            raise DomainError("GIT_FAILED", message)
        return completed


def _safe_stderr_summary(stderr: str) -> str:
    summary = " ".join(stderr.strip().split())
    summary = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", summary)
    summary = _AUTHORIZATION.sub(r"\1[REDACTED]", summary)
    summary = _BEARER.sub(r"\1[REDACTED]", summary)
    summary = _SECRET_PARAMETER.sub(r"\1[REDACTED]", summary)
    return summary[:500]
