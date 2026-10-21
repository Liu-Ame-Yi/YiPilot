"""The one explicitly supported live ModelPort: OpenAI Chat Completions."""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from yipilot.domain.errors import DomainError
from yipilot.domain.model import (
    MAX_MODEL_CHUNK_BYTES,
    ModelChunk,
    ModelCompletion,
    ModelRequest,
)
from yipilot.domain.provider import ALLOWED_MODEL_REQUEST_OPTIONS, ResolvedModel
from yipilot.models.secrets import SecretStore
from yipilot.security.redaction import SecretRedactor

MAX_MODEL_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_MODEL_EVENT_BYTES = 256 * 1024
MAX_MODEL_REASONING_BYTES = 1024 * 1024
_PROBE_INSTRUCTION = "Reply with OK."


class OpenAICompatibleModelPort:
    """Stream a safe, bounded Chat Completions request without retries or redirects."""

    def __init__(
        self,
        model: ResolvedModel,
        secrets: SecretStore,
        *,
        credential_ref: str,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = MAX_MODEL_RESPONSE_BYTES,
        is_cancelled: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._model = model
        self._credential_ref = credential_ref
        self._secrets = secrets
        self._max_response_bytes = max_response_bytes
        self._is_cancelled = is_cancelled or (lambda: False)
        self._clock = clock
        self.last_completion = ModelCompletion()
        self._client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def stream(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        return self._request(request, streaming=True)

    def probe(self, request: ModelRequest) -> tuple[ModelChunk, ...]:
        """Verify credentials, endpoint and model without requiring SSE compatibility."""
        return self._request(request, streaming=False)

    def _request(self, request: ModelRequest, *, streaming: bool) -> tuple[ModelChunk, ...]:
        tokens = min(request.output_budget_tokens or self._model.max_output_tokens, self._model.max_output_tokens)
        self.last_completion = ModelCompletion(output_budget_tokens=tokens)
        self._validate_request(request)
        # Conservative UTF-8 upper bound, not a tokenizer. Reserve completion and framing.
        if self._model.context_limit and len(request.user_instruction.encode()) + tokens + 256 > self._model.context_limit:
            raise DomainError("MODEL_CONTEXT_LIMIT_EXCEEDED", "conservative input estimate plus output reserve exceeds context")
        secret = self._secrets.read(self._credential_ref)
        redactor = SecretRedactor([secret])
        budget = min(request.output_budget_bytes, self._model.output_limit)
        timeout = min(request.timeout_seconds, self._model.timeout_seconds)
        deadline = self._clock() + timeout
        self._check_stop(deadline)
        body = {
            "model": self._model.model_id,
            "stream": streaming,
            "max_tokens": tokens,
            "messages": [{"role": "user", "content": request.user_instruction}],
        }
        if not set(request.request_options) <= ALLOWED_MODEL_REQUEST_OPTIONS:
            raise DomainError("MODEL_REQUEST_OPTIONS_INVALID", "model request contains unsupported options")
        body.update({key: request.request_options[key] for key in ALLOWED_MODEL_REQUEST_OPTIONS & request.request_options.keys()})
        try:
            with self._client.stream(
                "POST",
                f"{self._model.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {secret}", "Accept-Encoding": "identity"},
                json=body,
                timeout=httpx.Timeout(timeout),
            ) as response:
                self._check_stop(deadline)
                self._raise_for_status(response)
                # Raw reads keep wire and parser budgets identical.
                if response.headers.get("content-encoding", "identity").strip().casefold() != "identity":
                    raise DomainError("MODEL_PROTOCOL_ERROR", "compressed model responses are unsupported")
                content_type = response.headers.get("content-type", "").casefold()
                if not streaming or "application/json" in content_type:
                    content = self._json_content(self._bounded_bytes(response, deadline))
                    if self.last_completion.text_bytes > budget:
                        raise DomainError("MODEL_OUTPUT_LIMIT_EXCEEDED", "model output exceeded its budget")
                    chunks = _chunks(content, budget, redactor)
                    self.last_completion.complete = True
                    return chunks
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
        if "chat" not in self._model.capabilities:
            raise DomainError("PROVIDER_CAPABILITY_UNAVAILABLE", "provider does not support chat")
        if (
            request.provider_profile_name != self._model.connection_id
            or request.model != self._model.model_id
            or request.profile_revision != self._model.connection_revision
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
            self.last_completion.transport_bytes = total
            if total > self._max_response_bytes:
                raise DomainError("MODEL_RESPONSE_LIMIT_EXCEEDED", "model response exceeded its limit")
            parts.append(part)
        self._check_stop(deadline)
        return b"".join(parts)

    def _sse_chunks(
        self,
        response: httpx.Response,
        deadline: float,
        budget: int,
        redactor: SecretRedactor,
    ) -> tuple[ModelChunk, ...]:
        buffer = b""
        parts: list[str] = []
        done = False
        for part in response.iter_raw():
            self._check_stop(deadline)
            self.last_completion.transport_bytes += len(part)
            if self.last_completion.transport_bytes > self._max_response_bytes:
                raise DomainError("MODEL_RESPONSE_LIMIT_EXCEEDED", "model response exceeded its limit")
            buffer += part
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if done and line.strip():
                    raise DomainError("MODEL_PROTOCOL_ERROR", "data after stream completion")
                done = self._sse_line(line.rstrip(b"\r"), parts, budget) or done
            if len(buffer) > MAX_MODEL_EVENT_BYTES:
                raise DomainError("MODEL_EVENT_LIMIT_EXCEEDED", "model event exceeded its limit")
        if buffer:
            if done and buffer.strip():
                raise DomainError("MODEL_PROTOCOL_ERROR", "data after stream completion")
            done = self._sse_line(buffer.rstrip(b"\r"), parts, budget) or done
        self._check_stop(deadline)
        if not done:
            raise DomainError("MODEL_PROTOCOL_ERROR", "model stream ended without completion")
        self.last_completion.received = True
        self._require_stop()
        chunks = _chunks("".join(parts), budget, redactor)
        self.last_completion.complete = True
        return chunks

    def _sse_line(
        self, line: bytes, parts: list[str], budget: int
    ) -> bool:
        if len(line) > MAX_MODEL_EVENT_BYTES:
            raise DomainError("MODEL_EVENT_LIMIT_EXCEEDED", "model event exceeded its limit")
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
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise DomainError("MODEL_PROTOCOL_ERROR", "model stream chunk is invalid") from error
        content = self._payload_content(payload, streaming=True)
        if content:
            if self.last_completion.text_bytes > budget:
                raise DomainError("MODEL_OUTPUT_LIMIT_EXCEEDED", "model output exceeded its budget")
            parts.append(content)
        return False

    def _json_content(self, raw: bytes) -> str:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DomainError("MODEL_PROTOCOL_ERROR", "model response is not valid JSON") from error
        self.last_completion.received = True
        content = self._payload_content(payload, streaming=False)
        self._require_stop()
        return content

    def _payload_content(self, payload: Any, *, streaming: bool) -> str:
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            raise DomainError("MODEL_PROTOCOL_ERROR", "model choices are invalid")
        usage = payload.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise DomainError("MODEL_PROTOCOL_ERROR", "model usage is invalid")
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if value is not None:
                    if type(value) is not int or value < 0:
                        raise DomainError("MODEL_PROTOCOL_ERROR", "model usage is invalid")
                    self.last_completion.usage[key] = value
        choices = payload["choices"]
        if not choices and streaming and usage is not None:
            return ""
        if len(choices) != 1 or not isinstance(choices[0], dict) or choices[0].get("index", 0) != 0:
            raise DomainError("MODEL_PROTOCOL_ERROR", "expected one model choice")
        choice = choices[0]
        message = choice.get("delta" if streaming else "message")
        if not isinstance(message, dict):
            raise DomainError("MODEL_PROTOCOL_ERROR", "model message is invalid")
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise DomainError("MODEL_PROTOCOL_ERROR", "model text is invalid")
        if self.last_completion.finish_reason is not None:
            raise DomainError("MODEL_PROTOCOL_ERROR", "choice after finish reason")
        self.last_completion.text_bytes += len(content.encode())
        for key in ("reasoning_content", "reasoning", "reasoning_text"):
            reasoning = message.get(key)
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise DomainError("MODEL_PROTOCOL_ERROR", "model reasoning is invalid")
                self.last_completion.reasoning_bytes += len(reasoning.encode())
        if self.last_completion.reasoning_bytes > MAX_MODEL_REASONING_BYTES:
            raise DomainError("MODEL_REASONING_LIMIT_EXCEEDED", "model reasoning exceeded its limit")
        if message.get("tool_calls") or message.get("function_call"):
            self.last_completion.finish_reason = "tool_calls"
            raise DomainError("MODEL_TOOL_CALL_UNSUPPORTED", "text-only model request returned a tool call")
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise DomainError("MODEL_PROTOCOL_ERROR", "model finish reason is invalid")
            match reason:
                case "stop" | "length" | "tool_calls" | "content_filter":
                    self.last_completion.finish_reason = reason
                case _:
                    self.last_completion.finish_reason = "unknown"
        return content

    def _require_stop(self) -> None:
        reason = self.last_completion.finish_reason
        if reason == "length":
            raise DomainError("MODEL_OUTPUT_TRUNCATED", "model stopped at its token limit")
        if reason == "tool_calls":
            raise DomainError("MODEL_TOOL_CALL_UNSUPPORTED", "text-only model request returned a tool call")
        if reason != "stop":
            raise DomainError("MODEL_PROTOCOL_ERROR", "model did not confirm a normal stop")

    def _check_stop(self, deadline: float) -> None:
        if self._is_cancelled():
            raise DomainError("MODEL_CANCELLED", "model request was cancelled")
        if self._clock() > deadline:
            raise DomainError("MODEL_OVERALL_TIMEOUT", "model request exceeded its overall timeout")


def provider_probe_request(model: ResolvedModel) -> ModelRequest:
    """A fixed minimal request for explicit provider connectivity validation."""
    from yipilot.domain.model import model_request_fingerprint

    budget = min(model.output_limit, 32)
    return ModelRequest(
        task_id="0",
        operation_id="provider-test",
        run_id="provider-test",
        provider_profile_name=model.connection_id,
        provider_type=model.provider_type,
        model=model.model_id,
        profile_revision=model.connection_revision,
        timeout_seconds=min(model.timeout_seconds, 10.0),
        output_budget_bytes=budget,
        output_budget_tokens=min(model.max_output_tokens, 256),
        input_fingerprint=model_request_fingerprint(_PROBE_INSTRUCTION),
        user_instruction=_PROBE_INSTRUCTION,
    )
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
