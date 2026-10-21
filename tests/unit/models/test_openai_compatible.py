import gzip
import hashlib
import json

import httpx
import pytest

from yipilot.domain.errors import DomainError
from yipilot.domain.model import ModelRequest, ModelResponse
from yipilot.domain.provider import ProviderProfile, ResolvedModel
from yipilot.models.fixtures import FakeModelPort
from yipilot.models.openai_compatible import OpenAICompatibleModelPort
from yipilot.models.secrets import SecretStore
from yipilot.models.service import ProviderService

_SECRET = "adapter-test-secret"
_PROMPT = "controlled prompt"


class _Stream(httpx.SyncByteStream):
    def __init__(self, content: bytes) -> None:
        self._content = content

    def __iter__(self):
        yield self._content


def _response(
    status: int, *, headers: dict[str, str] | None = None, content: bytes = b""
) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=_Stream(content))



def _profile(**changes: object) -> ProviderProfile:
    values: dict[str, object] = {
        "name": "local",
        "base_url": "https://provider.test/v1",
        "model": "test-model",
        "timeout_seconds": 2,
        "output_budget": 128,
        "revision": 1,
        "api_key_ref": "env:YIPILOT_TEST_KEY",
    }
    values.update(changes)
    return ProviderProfile.model_validate(values)


def _request(profile: ProviderProfile, *, budget: int = 128) -> ModelRequest:
    return ModelRequest(
        task_id="0",
        operation_id="test-operation",
        run_id="test-run",
        provider_profile_name=profile.name,
        provider_type=profile.provider_type,
        model=profile.model,
        profile_revision=profile.revision,
        timeout_seconds=profile.timeout_seconds,
        output_budget_bytes=budget,
        input_fingerprint=hashlib.sha256(_PROMPT.encode()).hexdigest(),
        user_instruction=_PROMPT,
    )


def _port(profile: ProviderProfile, handler, **kwargs: object) -> OpenAICompatibleModelPort:
    return OpenAICompatibleModelPort(
        _resolved(profile),
        SecretStore(environ={"YIPILOT_TEST_KEY": _SECRET}),
        credential_ref="env:YIPILOT_TEST_KEY",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _resolved(profile: ProviderProfile) -> ResolvedModel:
    return ResolvedModel(selector=f"{profile.name}/{profile.model}", source="explicit", connection_id=profile.name, provider_type=profile.provider_type, base_url=profile.base_url, connection_revision=profile.revision, model_id=profile.model, display_name=profile.model, capabilities=profile.capabilities, output_limit=profile.output_budget, timeout_seconds=profile.timeout_seconds)


def test_streams_sse_only_to_fixed_path_and_controlled_body() -> None:
    profile = _profile()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return _response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"hel"}}]}\n'
                b'data: {"choices":[{"delta":{"content":"lo"}}]}\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
                b"data: [DONE]\n"
            ),
        )

    assert "".join(chunk.content for chunk in _port(profile, handler).stream(_request(profile))) == "hello"
    assert seen["url"] == "https://provider.test/v1/chat/completions"
    assert seen["body"] == {
        "model": "test-model",
        "stream": True,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": _PROMPT}],
    }
    assert seen["headers"]["authorization"] == f"Bearer {_SECRET}"


def test_stream_accepts_a_standard_role_chunk_without_text_content() -> None:
    profile = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n'
                b'data: {"choices":[{"delta":{"content":"OK"}}]}\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
                b"data: [DONE]\n"
            ),
        )

    assert "".join(chunk.content for chunk in _port(profile, handler).stream(_request(profile))) == "OK"


def test_provider_probe_uses_a_non_streaming_chat_completion() -> None:
    profile = _profile()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}',
        )

    assert "".join(chunk.content for chunk in _port(profile, handler).probe(_request(profile))) == "OK"
    assert seen["body"] == {
        "model": "test-model",
        "stream": False,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": _PROMPT}],
    }


def test_non_streaming_json_fallback_has_the_same_chunk_semantics() -> None:
    profile = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"choices":[{"message":{"content":"fallback"},"finish_reason":"stop"}]}',
        )

    assert "".join(chunk.content for chunk in _port(profile, handler).stream(_request(profile))) == "fallback"


@pytest.mark.parametrize("streaming", [True, False])
def test_raw_readers_negotiate_identity_encoding(streaming: bool) -> None:
    content = (
        b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n'
        b'data: [DONE]\n'
        if streaming else b'{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("accept-encoding") != "identity":
            return _response(200, headers={"content-encoding": "gzip"}, content=gzip.compress(content))
        return _response(200, content=content)

    port = _port(_profile(), handler)
    assert "".join(c.content for c in (port.stream if streaming else port.probe)(_request(_profile()))) == "OK"


@pytest.mark.parametrize("streaming", [True, False])
def test_unsolicited_compression_is_rejected_before_reading(streaming: bool) -> None:
    class UnreadableStream(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("compressed response must not be consumed")
            yield b""

    port = _port(_profile(), lambda _: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=UnreadableStream()))
    with pytest.raises(DomainError, match="MODEL_PROTOCOL_ERROR"):
        (port.stream if streaming else port.probe)(_request(_profile()))
    assert not port.last_completion.complete and port.last_completion.transport_bytes == 0


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "MODEL_AUTH_FAILED"), (403, "MODEL_AUTH_FAILED"), (429, "MODEL_RATE_LIMITED"), (500, "MODEL_PROVIDER_FAILED")],
)
def test_http_failures_have_stable_safe_codes(status: int, code: str) -> None:
    profile = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(status, content=f"Bearer {_SECRET} {_PROMPT}".encode())

    with pytest.raises(DomainError) as error:
        _port(profile, handler).stream(_request(profile))
    assert error.value.code == code
    assert _SECRET not in str(error.value)
    assert _PROMPT not in str(error.value)


@pytest.mark.parametrize(
    ("failure", "code"),
    [(httpx.ConnectTimeout("private"), "MODEL_CONNECT_TIMEOUT"), (httpx.ReadTimeout("private"), "MODEL_READ_TIMEOUT")],
)
def test_network_timeouts_are_mapped_without_exception_text(failure: Exception, code: str) -> None:
    profile = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    with pytest.raises(DomainError) as error:
        _port(profile, handler).stream(_request(profile))
    assert error.value.code == code
    assert "private" not in str(error.value)


def test_overall_timeout_and_cancellation_do_not_start_or_leave_a_request() -> None:
    profile = _profile()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(200)

    with pytest.raises(DomainError, match="MODEL_OVERALL_TIMEOUT"):
        _port(profile, handler, clock=iter((0.0, 3.0)).__next__).stream(_request(profile))
    with pytest.raises(DomainError, match="MODEL_CANCELLED"):
        _port(profile, handler, is_cancelled=lambda: True).stream(_request(profile))
    assert calls == 0


@pytest.mark.parametrize(
    ("content", "headers", "limit", "code"),
    [
        (b"data: not-json\n", {}, 256, "MODEL_PROTOCOL_ERROR"),
        (b'{"choices": [{"message": {"content": "too long"}}]}', {"content-type": "application/json"}, 8, "MODEL_RESPONSE_LIMIT_EXCEEDED"),
    ],
)
def test_invalid_or_oversized_responses_are_rejected(
    content: bytes, headers: dict[str, str], limit: int, code: str
) -> None:
    profile = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(200, headers=headers, content=content)

    with pytest.raises(DomainError, match=code):
        _port(profile, handler, max_response_bytes=limit).stream(_request(profile))


def test_output_budget_and_response_redaction_apply_before_model_planner() -> None:
    profile = _profile(output_budget=64)

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"Bearer adapter-test-secret"}}]}\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'
                b"data: [DONE]\n"
            ),
        )

    chunks = _port(profile, handler).stream(_request(profile, budget=64))
    assert _SECRET not in "".join(chunk.content for chunk in chunks)
    with pytest.raises(DomainError, match="MODEL_OUTPUT_LIMIT_EXCEEDED"):
        _port(profile, handler).stream(_request(profile, budget=8))


def test_provider_profile_requires_https_except_loopback() -> None:
    with pytest.raises(ValueError, match="loopback"):
        _profile(base_url="http://provider.test/v1")
    assert _profile(base_url="http://localhost:1234/v1").base_url == "http://localhost:1234/v1"


def test_provider_service_only_uses_a_port_for_explicit_request(tmp_path) -> None:

    profile = _profile()
    calls = 0

    def factory(model: ResolvedModel, secrets: SecretStore, reference: str) -> FakeModelPort:
        nonlocal calls
        calls += 1
        return FakeModelPort(ModelResponse(chunks=()))

    service = ProviderService(
        tmp_path, secrets=SecretStore(environ={"YIPILOT_TEST_KEY": _SECRET}), port_factory=factory
    )
    service.add(**profile.model_dump())
    service.model_selection.set_default(service.selector_for_profile(profile.name))
    assert service.test().status == "local_validation_passed"
    assert calls == 0
    assert service.test(request=True).status == "request_passed"
    assert calls == 1


def test_adapter_ignores_ambient_proxy_environment(monkeypatch) -> None:
    profile = _profile()
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    port = _port(profile, lambda request: _response(200))
    assert port._client._trust_env is False


def test_redirect_is_not_followed() -> None:
    profile = _profile()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(302, headers={"location": "https://unapproved.invalid"})

    with pytest.raises(DomainError, match="MODEL_HTTP_ERROR"):
        _port(profile, handler).stream(_request(profile))
    assert calls == 1


def test_cancellation_closes_an_open_response_stream() -> None:
    profile = _profile()

    class CancellableStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ignored"}}]}\n'

        def close(self) -> None:
            self.closed = True

    stream = CancellableStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    with pytest.raises(DomainError, match="MODEL_CANCELLED"):
        _port(profile, handler, is_cancelled=iter((False, False, True)).__next__).stream(
            _request(profile)
        )
    assert stream.closed


def _sse(*events):
    return b"".join(b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"


@pytest.mark.parametrize("reason,code", [
    ("length", "MODEL_OUTPUT_TRUNCATED"), (None, "MODEL_PROTOCOL_ERROR"),
    ("content_filter", "MODEL_PROTOCOL_ERROR"), ("tool_calls", "MODEL_TOOL_CALL_UNSUPPORTED"),
    ("unexpected", "MODEL_PROTOCOL_ERROR"),
])
@pytest.mark.parametrize("streaming", [True, False])
def test_only_normal_completion_can_release_even_valid_json(reason, code, streaming):
    profile = _profile()
    choice = {"delta" if streaming else "message": {"content": "{}"}, "finish_reason": reason}
    content = _sse({"choices": [choice]}) if streaming else json.dumps({"choices": [choice]}).encode()
    port = _port(profile, lambda _: _response(200, content=content))
    with pytest.raises(DomainError, match=code):
        (port.stream if streaming else port.probe)(_request(profile))
    assert not port.last_completion.complete
    assert port.last_completion.text_bytes == 2


def test_reasoning_and_sse_overhead_do_not_spend_text_budget():
    profile = _profile()
    # More than the former 256 KiB transport ceiling, only two bytes of answer.
    content = _sse(*({"choices": [{"delta": {"reasoning_content": "think " * 60}}]} for _ in range(1000)),
                   {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
                   {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 4000, "total_tokens": 4020}})
    port = _port(profile, lambda _: _response(200, content=content))
    assert "".join(c.content for c in port.stream(_request(profile, budget=2))) == "OK"
    assert port.last_completion.transport_bytes > 256 * 1024
    assert port.last_completion.reasoning_bytes == 360000
    assert port.last_completion.text_bytes == 2
    assert port.last_completion.usage["completion_tokens"] == 4000
    assert port.last_completion.complete
    assert "think" not in port.last_completion.model_dump_json()


@pytest.mark.parametrize("content,code", [
    (b"data: " + b"x" * (256 * 1024), "MODEL_EVENT_LIMIT_EXCEEDED"),
    (_sse(*({"choices": [{"delta": {"reasoning_content": "x" * 16384}}]} for _ in range(65))), "MODEL_REASONING_LIMIT_EXCEEDED"),
    (_sse({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "private"}}]}}]}), "MODEL_TOOL_CALL_UNSUPPORTED"),
    (_sse({"choices": [{"delta": {"content": 0}}]}), "MODEL_PROTOCOL_ERROR"),
    (_sse({"choices": [], "usage": {"total_tokens": "bad"}}), "MODEL_PROTOCOL_ERROR"),
], ids=["oversize-event", "oversize-reasoning", "tool-call", "bad-text", "bad-usage"])
def test_stream_guards_are_independent(content, code):
    with pytest.raises(DomainError, match=code):
        _port(_profile(), lambda _: _response(200, content=content)).stream(_request(_profile()))


def test_utf8_and_secrets_split_between_events_are_safe():
    content = _sse({"choices": [{"delta": {"content": "Bearer adapter-"}}]},
                   {"choices": [{"delta": {"content": "test-secret 中文"}, "finish_reason": "stop"}]})

    class Fragments(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(len(content)):
                yield content[index:index + 1]

    port = _port(_profile(), lambda _: httpx.Response(200, stream=Fragments()))
    text = "".join(c.content for c in port.stream(_request(_profile())))
    assert "中文" in text and "test-secret" not in text


def test_token_budget_is_separate_and_context_includes_output_reserve():
    seen = []
    port = _port(_profile(), lambda req: seen.append(json.loads(req.content)) or _response(
        200, content=_sse({"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]})))
    request = _request(_profile(), budget=2).model_copy(update={"output_budget_tokens": 100})
    port.stream(request)
    assert seen[0]["max_tokens"] == 100
    assert port.last_completion.output_budget_tokens == 100
    port._model = port._model.model_copy(update={"context_limit": 200})
    with pytest.raises(DomainError, match="MODEL_CONTEXT_LIMIT_EXCEEDED"):
        port.stream(request)
    assert len(seen) == 1


@pytest.mark.parametrize("streaming", [True, False])
def test_late_eof_cannot_turn_an_expired_request_into_success(streaming):
    now = 0.0

    class LateEOF(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal now
            if streaming:
                yield _sse({"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]})
            else:
                yield b'{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}'
            now = 3.0

    port = _port(_profile(), lambda _: httpx.Response(200, stream=LateEOF()), clock=lambda: now)
    with pytest.raises(DomainError, match="MODEL_OVERALL_TIMEOUT"):
        (port.stream if streaming else port.probe)(_request(_profile()))
    assert not port.last_completion.complete
