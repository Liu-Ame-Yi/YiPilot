"""Strict, proposal-only patches returned by a live model."""

import re
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PATCH_FILES = 16
MAX_PATCH_DIFF_BYTES = 64 * 1024
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$"
)


class PatchProposalPatch(BaseModel):
    """One unified diff for exactly one task-worktree-relative path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=1024)
    unified_diff: str = Field(min_length=1, max_length=MAX_PATCH_DIFF_BYTES)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        posix, windows = PurePosixPath(value), PureWindowsPath(value)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or bool(windows.root)
            or ".." in posix.parts
            or ".." in windows.parts
            or ".git" in {part.casefold() for part in posix.parts}
        ):
            raise ValueError("patch path must be a task-worktree-relative path")
        return value.replace("\\", "/")

    @model_validator(mode="after")
    def validate_diff(self) -> "PatchProposalPatch":
        if len(self.unified_diff.encode("utf-8")) > MAX_PATCH_DIFF_BYTES:
            raise ValueError("patch diff exceeds its byte limit")
        lines = self.unified_diff.splitlines()
        if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith(
            "+++ "
        ):
            raise ValueError("patch must contain a unified diff header and hunk")
        old, new = lines[0][4:], lines[1][4:]
        expected = self.path
        valid_old = old in {"/dev/null", f"a/{expected}"}
        valid_new = new in {"/dev/null", f"b/{expected}"}
        if not valid_old or not valid_new or (old == new == "/dev/null"):
            raise ValueError("patch headers must match its relative path")
        _validate_hunks(lines[2:])
        return self


class PatchProposal(BaseModel):
    """A bounded, reviewable live-model output; it has no apply capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    summary: str = Field(min_length=1, max_length=1024)
    patches: tuple[PatchProposalPatch, ...] = Field(
        min_length=1, max_length=MAX_PATCH_FILES
    )

    @model_validator(mode="after")
    def validate_unique_paths(self) -> "PatchProposal":
        paths = [patch.path.casefold() for patch in self.patches]
        if len(paths) != len(set(paths)):
            raise ValueError("patch paths must be unique")
        if (
            sum(len(patch.unified_diff.encode("utf-8")) for patch in self.patches)
            > MAX_PATCH_DIFF_BYTES
        ):
            raise ValueError("proposal diffs exceed their total byte limit")
        return self


def _validate_hunks(lines: list[str]) -> None:
    old_remaining = new_remaining = None
    saw_content = False
    for line in lines:
        header = _HUNK_HEADER.fullmatch(line)
        if header is not None:
            if old_remaining not in (None, 0) or new_remaining not in (None, 0):
                raise ValueError("patch hunk line counts are invalid")
            old_remaining = _hunk_count(header["old_start"], header["old_count"])
            new_remaining = _hunk_count(header["new_start"], header["new_count"])
            continue
        if old_remaining is None or new_remaining is None:
            raise ValueError("patch must contain a valid unified diff hunk")
        if line.startswith(" "):
            old_remaining -= 1
            new_remaining -= 1
            saw_content = True
        elif line.startswith("-"):
            old_remaining -= 1
            saw_content = True
        elif line.startswith("+"):
            new_remaining -= 1
            saw_content = True
        elif line == r"\ No newline at end of file":
            continue
        else:
            raise ValueError("patch hunk contains an invalid line")
        if old_remaining < 0 or new_remaining < 0:
            raise ValueError("patch hunk line counts are invalid")
    if not saw_content or old_remaining != 0 or new_remaining != 0:
        raise ValueError("patch hunk line counts are invalid")


def _hunk_count(start: str, count: str | None) -> int:
    parsed_start = int(start)
    parsed_count = int(count) if count is not None else 1
    if parsed_start == 0 and parsed_count != 0:
        raise ValueError("patch hunk range is invalid")
    return parsed_count
