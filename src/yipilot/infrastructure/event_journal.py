"""Locked append-only JSONL persistence for Run events."""

import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, ValidationError

from yipilot.domain.errors import DomainError
from yipilot.security.redaction import SecretRedactor

MAX_EVENT_PAYLOAD_BYTES = 16_384

_EVENT_TYPES = {
    "run.started",
    "plan.created",
    "stage.progress",
    "model.delta",
    "model.message_completed",
    "tool.started",
    "tool.output",
    "tool.completed",
    "harness.completed",
    "approval.required",
    "run.paused",
    "run.succeeded",
    "run.failed",
    "run.cancelled",
}
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s\"']+")
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)


class EventRecord(BaseModel):
    sequence: int
    event_id: str
    task_id: str
    run_id: str
    stage: str
    timestamp: str
    type: str
    visibility: Literal["user", "audit"] = "user"
    payload: dict[str, object]


class JsonlEventJournal:
    """Append and replay one Run's events under a cross-platform file lock."""

    def __init__(
        self,
        path: Path,
        *,
        task_id: str = "",
        run_id: str = "",
        stage: str = "",
    ) -> None:
        self._path = Path(path)
        self._lock = FileLock(str(self._path.with_suffix(f"{self._path.suffix}.lock")))
        self._task_id = task_id
        self._run_id = run_id
        self._stage = stage

    def append(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        visibility: Literal["user", "audit"] = "user",
    ) -> EventRecord:
        if event_type not in _EVENT_TYPES:
            raise DomainError("EVENT_TYPE_INVALID", "event type is not supported")
        redacted_payload = _prepare_payload(payload)
        with self._lock:
            existing = self._read_all_unlocked()
            event = EventRecord(
                sequence=len(existing) + 1,
                event_id=str(uuid4()),
                task_id=self._task_id,
                run_id=self._run_id,
                stage=self._stage,
                timestamp=_timestamp(),
                type=event_type,
                visibility=visibility,
                payload=redacted_payload,
            )
            line = json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{line}\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def read_after(self, after_sequence: int) -> list[EventRecord]:
        if after_sequence < 0:
            raise DomainError("EVENT_CURSOR_INVALID", "event cursor must be non-negative")
        with self._lock:
            return [
                event
                for event in self._read_all_unlocked()
                if event.sequence > after_sequence
            ]

    def _read_all_unlocked(self) -> list[EventRecord]:
        if not self._path.exists():
            return []
        events: list[EventRecord] = []
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                for expected_sequence, line in enumerate(handle, start=1):
                    if not line.endswith("\n"):
                        raise DomainError(
                            "EVENT_JOURNAL_CORRUPT", "event journal has a damaged tail"
                        )
                    event = EventRecord.model_validate_json(line)
                    if event.sequence != expected_sequence:
                        raise DomainError(
                            "EVENT_JOURNAL_CORRUPT",
                            "event sequence is not contiguous",
                        )
                    events.append(event)
        except DomainError:
            raise
        except (OSError, ValidationError, ValueError) as error:
            raise DomainError(
                "EVENT_JOURNAL_CORRUPT", "event journal contains an invalid line"
            ) from error
        return events


def _prepare_payload(payload: Mapping[str, object]) -> dict[str, object]:
    try:
        redacted = _redact_mapping(payload)
        encoded = json.dumps(
            redacted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise DomainError(
            "EVENT_PAYLOAD_INVALID", "event payload must be JSON serializable"
        ) from error
    if len(encoded) > MAX_EVENT_PAYLOAD_BYTES:
        raise DomainError(
            "EVENT_PAYLOAD_TOO_LARGE",
            f"event payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes",
        )
    return redacted


def _redact_mapping(value: Mapping[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("event payload keys must be strings")
        if key.casefold() == "authorization":
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = _redact_value(item)
    return redacted


def _redact_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return SecretRedactor().redact_text(value)
    return value


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
