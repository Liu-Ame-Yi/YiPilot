"""Safe filesystem primitives for project-scoped durable records."""

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TypeAlias

import yaml  # type: ignore[import-untyped]

from yipilot.domain.errors import DomainError

Record: TypeAlias = Mapping[str, object]
RecordValidator: TypeAlias = Callable[[Record], object]


def project_path(root: Path, *parts: str) -> Path:
    """Return a resolved child path only when it remains within ``root``."""
    resolved_root = root.resolve(strict=True)
    for part in parts:
        part_path = Path(part)
        if part_path.is_absolute() or ".." in part_path.parts:
            raise DomainError("PATH_DENIED", "path escapes project root")
    candidate = resolved_root.joinpath(*parts).resolve(strict=False)
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise DomainError("PATH_DENIED", "path escapes project root")
    return candidate


def atomic_write_json(
    path: Path, value: Record, validator: RecordValidator | None = None
) -> None:
    """Atomically write and re-validate one JSON object."""
    _validate_record(value, validator)
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _atomic_write_text(path, f"{content}\n")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    _validate_record(loaded, validator)


def atomic_write_yaml(
    path: Path, value: Record, validator: RecordValidator | None = None
) -> None:
    """Atomically write and re-validate one YAML mapping."""
    _validate_record(value, validator)
    content = yaml.safe_dump(dict(value), allow_unicode=True, sort_keys=True)
    _atomic_write_text(path, content)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    _validate_record(loaded, validator)


def _validate_record(value: object, validator: RecordValidator | None) -> None:
    if not isinstance(value, Mapping):
        raise DomainError("STORAGE_INVALID", "record must be a mapping")
    record = dict(value)
    if validator is not None:
        validator(record)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
