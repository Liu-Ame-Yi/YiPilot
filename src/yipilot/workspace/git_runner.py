"""Controlled argv-only execution for Git commands."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar

from yipilot.domain.errors import DomainError
from yipilot.security.redaction import safe_stderr_summary

_REPOSITORY_ENVIRONMENT_VARIABLES = frozenset(
    {
        "GIT_INDEX_FILE",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    }
)
_CONTROLLED_NO_INDEX_OPTIONS = frozenset(
    {
        "--no-index",
        "--binary",
        "--stat",
        "--no-ext-diff",
        "--no-textconv",
        "--no-prefix",
    }
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
        env: Mapping[str, str | None] | None = None,
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
                env=_child_environment(env),
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
        env: Mapping[str, str | None] | None = None,
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
                env=_child_environment(env),
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


def _child_environment(env: Mapping[str, str | None] | None) -> dict[str, str]:
    merged = os.environ.copy()
    for name in _REPOSITORY_ENVIRONMENT_VARIABLES:
        merged.pop(name, None)
    if env is not None:
        for name, value in env.items():
            if value is None:
                merged.pop(name, None)
            else:
                merged[name] = value
    return merged


def _check_returncode(
    argv: list[str], completed: subprocess.CompletedProcess[_Output]
) -> None:
    no_index_difference = (
        completed.returncode == 1
        and _is_controlled_no_index_evidence(argv)
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
    summary = safe_stderr_summary(stderr_text)
    message = "git command failed"
    if summary:
        message = f"{message}: {summary}"
    raise DomainError("GIT_FAILED", message)


def _is_controlled_no_index_evidence(argv: list[str]) -> bool:
    command_index = _git_subcommand_index(argv)
    if command_index is None or argv[command_index] != "diff":
        return False
    try:
        separator = argv.index("--", command_index + 1)
    except ValueError:
        return False
    options = argv[command_index + 1 : separator]
    return (
        len(argv[separator + 1 :]) == 2
        and options.count("--no-index") == 1
        and all(option in _CONTROLLED_NO_INDEX_OPTIONS for option in options)
    )


def _git_subcommand_index(argv: list[str]) -> int | None:
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "--no-optional-locks":
            index += 1
        elif argument == "-c":
            index += 2
        elif argument.startswith("-c") and len(argument) > 2:
            index += 1
        else:
            return index
    return None
