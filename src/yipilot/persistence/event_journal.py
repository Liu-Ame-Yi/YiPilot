"""SQLite events; JSONL is a renderer/export, never a project write store."""

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from yipilot.domain.errors import DomainError
from yipilot.persistence.database import Database
from yipilot.security.redaction import SecretRedactor

MAX_EVENT_PAYLOAD_BYTES = 16_384

EVENT_TYPES = {
    "run.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "plan.created",
    "proposal.created",
    "stage.progress",
    "model.delta",
    "model.message_completed",
    "model.failed",
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
    cursor: int | None = None
    event_id: str
    task_id: str
    run_id: str
    stage: str
    timestamp: str
    type: str
    visibility: Literal["user", "audit"] = "user"
    payload: dict[str, object]


class JsonlEventJournal:
    """Append and replay SQLite events under the historical source-level name."""

    def __init__(
        self,
        path: Path,
        *,
        task_id: str = "",
        run_id: str = "",
        stage: str = "",
    ) -> None:
        self._path = Path(path)
        self._task_id = task_id
        self._run_id = run_id
        self._stage = stage
        runtime = next((p for p in self._path.parents if p.name == ".yipilot"), None)
        if runtime is None:
            raise DomainError("EVENT_PATH_INVALID", "event locator must belong to a project Run")
        self._db = Database(runtime.parent)
        self._run_id = run_id or self._path.parent.name
        self._task_id = task_id or self._path.parents[2].name

    def append(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        visibility: Literal["user", "audit"] = "user",
    ) -> EventRecord:
        if event_type not in EVENT_TYPES:
            raise DomainError("EVENT_TYPE_INVALID", "event type is not supported")
        redacted_payload = _prepare_payload(payload)
        return self._append_sqlite(event_type, redacted_payload, visibility)

    def read_after(self, after_sequence: int) -> list[EventRecord]:
        if after_sequence < 0:
            raise DomainError("EVENT_CURSOR_INVALID", "event cursor must be non-negative")
        with self._db.transaction() as connection:
            try:
                events = [EventRecord.model_validate_json(row[1]) for row in connection.execute(
                    "SELECT sequence,body FROM events WHERE run_id=? ORDER BY sequence", (self._run_id,))]
                if any(event.sequence != index for index, event in enumerate(events, 1)):
                    raise ValueError("non-contiguous sequence")
                return [event for event in events if event.sequence > after_sequence]
            except ValueError as error:
                raise DomainError("EVENT_JOURNAL_CORRUPT", "invalid persisted event") from error

    def _append_sqlite(self, event_type: str, payload: dict[str, object],
                       visibility: Literal["user", "audit"]) -> EventRecord:
        with self._db.transaction() as connection:
            if event_type in {"run.started", "run.paused", "run.succeeded", "run.failed", "run.cancelled"}:
                from yipilot.persistence.task_store import FileTaskStore
                store = FileTaskStore(self._db.project)
                target = "running" if event_type == "run.started" else event_type[4:]
                if store.find_run(self._run_id).status.value != target:
                    store.set_run_status(self._run_id, target, error_code=str(payload["error_code"]) if payload.get("error_code") else None, event_payload=payload)
            if event_type in {"run.started", "run.succeeded", "run.failed", "run.cancelled"}:
                row = connection.execute("SELECT body FROM events WHERE run_id=? AND type=? ORDER BY sequence DESC LIMIT 1", (self._run_id, event_type)).fetchone()
                if row:
                    return EventRecord.model_validate_json(row[0])
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (self._run_id,)).fetchone()[0]
            event = EventRecord(sequence=sequence, event_id=str(uuid4()), task_id=self._task_id,
                run_id=self._run_id, stage=self._stage, timestamp=_timestamp(), type=event_type,
                visibility=visibility, payload=payload)
            cursor = connection.execute("INSERT INTO events(event_id,run_id,sequence,type,body) VALUES (?,?,?,?,?)",
                (event.event_id, self._run_id, sequence, event_type, event.model_dump_json())).lastrowid
            event = event.model_copy(update={"cursor": cursor})
            connection.execute("UPDATE events SET body=? WHERE cursor=?", (event.model_dump_json(), cursor))
            return event


def _prepare_payload(payload: Mapping[str, object]) -> dict[str, object]:
    try:
        if any(not isinstance(key, str) for key in payload):
            raise TypeError("event payload keys must be strings")
        redacted = SecretRedactor().redact(payload)
        assert isinstance(redacted, dict)
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


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
