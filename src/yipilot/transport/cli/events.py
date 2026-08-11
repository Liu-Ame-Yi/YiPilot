"""Render durable Run events for the command-line transport."""

import json
import sys
from typing import TextIO

from yipilot.infrastructure.event_journal import EventRecord

_TERMINAL_EVENT_TYPES = frozenset({"run.succeeded", "run.failed", "run.cancelled"})


class JsonlEventRenderer:
    """Write only canonical event JSON objects to stdout, one per line."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def render(self, event: EventRecord) -> None:
        self._stream.write(
            json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        self._stream.flush()


class HumanEventRenderer:
    """Stream model text on stdout and keep operational diagnostics on stderr."""

    def __init__(
        self,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self._text_open = False

    def render(self, event: EventRecord) -> None:
        if event.type == "model.delta":
            content = event.payload.get("content", "")
            if isinstance(content, str):
                self._stdout.write(content)
                self._stdout.flush()
                self._text_open = True
            return
        if event.type == "model.message_completed":
            if self._text_open:
                self._stdout.write("\n")
                self._stdout.flush()
                self._text_open = False
            return
        self._stderr.write(f"{event.type} (sequence {event.sequence})\n")
        self._stderr.flush()


def is_terminal_event(event: EventRecord) -> bool:
    return event.type in _TERMINAL_EVENT_TYPES
