"""The one explicitly supported live ModelPort: OpenAI Chat Completions."""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from yipilot.domain.errors import DomainError
from yipilot.domain.model import MAX_MODEL_CHUNK_BYTES, ModelChunk, ModelRequest
from yipilot.domain.provider import ProviderProfile
from yipilot.infrastructure.secret_store import SecretStore
from yipilot.security.redaction import SecretRedactor

MAX_MODEL_RESPONSE_BYTES = 256 * 1024
_PROBE_INSTRUCTION = "Reply with OK."


class OpenAICompatibleModelPort:
    """Stream a safe, bounded Chat Completions request without retries or redirects."""

    def __init__(
        self,
        profile: ProviderProfile,
        secrets: SecretStore,
        *,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = MAX_MODEL_RESPONSE_BYTES,
        is_cancelled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._profile = profile
        self._secrets = secrets
        self._max_response_bytes = max_response_bytes
        self._is_cancelled = is_cancelled or (lambda: False)
        self._clock = clock
        self._client = httpx.Client(transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        self._validate_request(request)
        if self._profile.api_key_ref is None:
            raise DomainError("SECRET_REF_MISSING", "provider profile has no secret reference")
        secret = self._secrets.read(self._profile.api_key_ref)
        redactor = SecretRedactor([secret])
        budget = min(request.output_budget_bytes, self._profile.output_budget)
        timeout = min(request.timeout_seconds, self._profile.timeout_seconds)
        deadline = self._clock() + timeout
        self._check_stop(deadline)
        body = {
            "model": self._profile.model,
            "stream": True,
            "max_tokens": budget,
            "messages": [{"role": "user", "content": request.user_instruction}],
        }
        try:
            with self._client.stream(
                "POST",
                f"{self._profile.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {secret}"},
                json=body,
                timeout=httpx.Timeout(timeout),
            ) as response:
                self._check_stop(deadline)
                self._raise_for_status(response)
                content_type = response.headers.get("content-type", "").casefold()
                if "application/json" in content_type:
                    content = self._json_content(self._bounded_bytes(response, deadline))
                    return _chunks(content, budget, redactor)
                return self._sse_chunks(response, deadline, budget, redactor)
        except DomainError:
            raise
        except httpx.ConnectTimeout as error:
            raise DomainError("MODEL_CONNECT_TIMEOUT", "model connection timed out") from error
        except httpx.ReadTimeout as error:
            raise DomainError("MODEL_READ_TIMEOUT", "model response timed out") from error
        except httpx.TimeoutException as error:
            raise DomainError("MODEL_NETWORK_TIMEOUT", "model request timed out") from error
        except httpx.HTTPError as error:
            raise DomainError("MODEL_NETWORK_FAILED", "model network request failed") from error

    def _validate_request(self, request: ModelRequest) -> None:
        if not self._profile.enabled:
            raise DomainError("PROVIDER_DISABLED", "provider profile is disabled")
        if "chat" not in self._profile.capabilities:
            raise DomainError("PROVIDER_CAPABILITY_UNAVAILABLE", "provider does not support chat")
        if (
            request.provider_profile_name != self._profile.name
            or request.model != self._profile.model
            or request.profile_revision != self._profile.revision
        ):
            raise DomainError("MODEL_PROFILE_MISMATCH", "model request does not match provider profile")

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise DomainError("MODEL_AUTH_FAILED", "model provider rejected credentials")
        if response.status_code == 429:
            raise DomainError("MODEL_RATE_LIMITED", "model provider rate limited the request")
        if response.status_code >= 500:
            raise DomainError("MODEL_PROVIDER_FAILED", "model provider failed")
        if response.status_code >= 300:
            raise DomainError("MODEL_HTTP_ERROR", "model provider returned an unexpected status")

    def _bounded_bytes(self, response: httpx.Response, deadline: float) -> bytes:
        total = 0
        parts: list[bytes] = []
        for part in response.iter_raw():
            self._check_stop(deadline)
            total += len(part)
            if total > self._max_response_bytes:
                raise DomainError("MODEL_RESPONSE_LIMIT_EXCEEDED", "model response exceeded its limit")
            parts.append(part)
        return b"".join(parts)

    def _sse_chunks(
        self,
        response: httpx.Response,
        deadline: float,
        budget: int,
        redactor: SecretRedactor,
    ) -> tuple[ModelChunk, ...]:
        total = 0
        buffer = b""
        chunks: list[ModelChunk] = []
        done = False
        for part in response.iter_raw():
            self._check_stop(deadline)
            total += len(part)
            if total > self._max_response_bytes:
                raise DomainError("MODEL_RESPONSE_LIMIT_EXCEEDED", "model response exceeded its limit")
            buffer += part
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                done = self._sse_line(line.rstrip(b"\r"), chunks, budget, redactor) or done
        if buffer:
            done = self._sse_line(buffer.rstrip(b"\r"), chunks, budget, redactor) or done
        if not done:
            raise DomainError("MODEL_PROTOCOL_ERROR", "model stream ended without completion")
        return tuple(chunks)

    @staticmethod
    def _sse_line(
        line: bytes, chunks: list[ModelChunk], budget: int, redactor: SecretRedactor
    ) -> bool:
        if not line or line.startswith(b":"):
            return False
        if not line.startswith(b"data:"):
            raise DomainError("MODEL_PROTOCOL_ERROR", "model stream is not valid SSE")
        try:
            data = line[5:].lstrip().decode("utf-8")
        except UnicodeDecodeError as error:
            raise DomainError("MODEL_PROTOCOL_ERROR", "model stream is not valid UTF-8") from error
        if data == "[DONE]":
            return True
        content = _delta_content(data)
        if content:
            chunks.extend(_chunks(content, budget - _chunk_bytes(chunks), redactor))
        return False

    @staticmethod
    def _json_content(raw: bytes) -> str:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DomainError("MODEL_PROTOCOL_ERROR", "model response is not valid JSON") from error
        return _message_content(payload)

    def _check_stop(self, deadline: float) -> None:
        if self._is_cancelled():
            raise DomainError("MODEL_CANCELLED", "model request was cancelled")
        if self._clock() > deadline:
            raise DomainError("MODEL_OVERALL_TIMEOUT", "model request exceeded its overall timeout")


def provider_probe_request(profile: ProviderProfile) -> ModelRequest:
    """A fixed minimal request for explicit provider connectivity validation."""
    from yipilot.domain.model import model_request_fingerprint

    budget = min(profile.output_budget, 32)
    return ModelRequest(
        task_id="0",
        operation_id="provider-test",
        run_id="provider-test",
        provider_profile_name=profile.name,
        provider_type=profile.provider_type,
        model=profile.model,
        profile_revision=profile.revision,
        timeout_seconds=min(profile.timeout_seconds, 10.0),
        output_budget_bytes=budget,
        input_fingerprint=model_request_fingerprint(_PROBE_INSTRUCTION),
        user_instruction=_PROBE_INSTRUCTION,
    )


def _delta_content(data: str) -> str:
    try:
        payload = json.loads(data)
        content = payload["choices"][0]["delta"].get("content", "")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise DomainError("MODEL_PROTOCOL_ERROR", "model stream chunk is invalid") from error
    if not isinstance(content, str):
        raise DomainError("MODEL_PROTOCOL_ERROR", "model stream chunk is invalid")
    return content


def _message_content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise DomainError("MODEL_PROTOCOL_ERROR", "model JSON response is invalid") from error
    if not isinstance(content, str):
        raise DomainError("MODEL_PROTOCOL_ERROR", "model JSON response is invalid")
    return content


def _chunk_bytes(chunks: list[ModelChunk]) -> int:
    return sum(len(chunk.content.encode("utf-8")) for chunk in chunks)


def _chunks(content: str, remaining: int, redactor: SecretRedactor) -> tuple[ModelChunk, ...]:
    safe = redactor.redact_text(content)
    if len(safe.encode("utf-8")) > remaining:
        raise DomainError("MODEL_OUTPUT_LIMIT_EXCEEDED", "model output exceeded its budget")
    values: list[ModelChunk] = []
    current = ""
    current_bytes = 0
    for character in safe:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > MAX_MODEL_CHUNK_BYTES:
            values.append(ModelChunk(content=current))
            current, current_bytes = "", 0
        current += character
        current_bytes += character_bytes
    if current:
        values.append(ModelChunk(content=current))
    return tuple(values)
