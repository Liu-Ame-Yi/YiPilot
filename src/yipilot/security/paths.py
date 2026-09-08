"""Shared no-link path validation and bounded evidence reads."""

import stat
from pathlib import Path

from yipilot.domain.errors import DomainError
from yipilot.domain.sandbox import SandboxErrorCode as Code


def require_plain_path(path: Path) -> None:
    """Check every existing ancestor without resolving away links or junctions."""
    for candidate in reversed((path, *path.parents)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode))
            or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1)
        ):
            raise DomainError(
                Code.PATH_DENIED, "links, reparse points and special files denied"
            )


def read_evidence(path: Path) -> bytes:
    require_plain_path(path)
    if path.stat().st_size > 16 * 1024**2:
        raise DomainError("SANDBOX_EVIDENCE_FAILED", "oversized verification record")
    return path.read_bytes()
