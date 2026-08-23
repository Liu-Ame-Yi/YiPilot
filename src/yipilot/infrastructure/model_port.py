"""Offline ModelPort implementations; none performs network I/O."""

from collections.abc import Mapping

from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelChunk, ModelRequest, ModelResponse


class FakeModelPort:
    """Return exactly the supplied deterministic recorded response."""

    def __init__(self, response: ModelResponse) -> None:
        self._response = response

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        del request
        return self._response.chunks


class RecordedReplayModelPort:
    """Replay only a fixture whose version and input fingerprint both match."""

    def __init__(self, version: str, responses: Mapping[str, ModelResponse]) -> None:
        if not version:
            raise ValueError("replay version is required")
        self.version = version
        self._responses = dict(responses)

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        try:
            return self._responses[request.input_fingerprint].chunks
        except KeyError as error:
            raise DomainError(
                "MODEL_REPLAY_NOT_FOUND", "no recorded model response matches this input"
            ) from error


class DisabledModelPort:
    """Stable offline failure used until a real adapter is explicitly added."""

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        del request
        raise DomainError("MODEL_DISABLED", "model calls are disabled before Task 16")
