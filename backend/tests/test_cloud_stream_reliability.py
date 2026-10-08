"""Cloud stream compatibility and bounded retry regressions."""

from __future__ import annotations

import json

import pytest

import llm_cloud_stream as cloud
from llm_router import LLMRouter
from model_providers import CredentialLease, ProviderRequestError
from session_catalog.service import IPYTHON_PROVIDER_SPEC
from tests.support.model_config import with_test_support


def _router() -> LLMRouter:
    return LLMRouter(
        with_test_support({
            "mode": "cloud",
            "cloud": {"provider": "openai", "openai_model": "test-model"},
        }),
        ".",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "incomplete_eof", "transport_error", "malformed", "oversized_event", "cancelled"])
async def test_openai_stream_retains_structural_end_diagnostics(monkeypatch, kind):
    from model_runtime.openai_sse import OpenAIChatSSEDecoder
    import asyncio
    import httpx
    rows = ['data: {"choices":[{"delta":{"content":"visible fixture"}}]}']
    if kind == "terminal":
        rows.append("data: [DONE]")
    elif kind == "malformed":
        rows.append("data: not JSON")
    elif kind == "oversized_event":
        rows.append("data: " + "x" * 200)
        monkeypatch.setattr(cloud,"OpenAIChatSSEDecoder",lambda:OpenAIChatSSEDecoder(max_frame_chars=100))
    class Response(_StreamResponse):
        async def aiter_lines(self):
            for line in self.lines:
                yield line
            if kind == "transport_error":
                raise httpx.ReadError("fixture broken transport")
            if kind == "cancelled":
                raise asyncio.CancelledError()
    monkeypatch.setattr(cloud.httpx,"AsyncClient",_client_for(Response(rows)))
    router = _router()
    observed = []
    monkeypatch.setattr(router,"_patch_model_request_stream",lambda ref,value:observed.append(value))
    async def consume():
        return [token async for token in cloud.call_openai(router,[{"role":"user","content":"fixture"}],{"max_tokens":32},"dummy-test-key",model="test-model")]
    if kind == "terminal":
        assert await consume() == ["visible fixture"]
    elif kind == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await consume()
    else:
        with pytest.raises(ProviderRequestError):
            await consume()
    assert len(observed) == 1
    assert observed[0]["end_reason"] == ("incomplete_eof" if kind == "malformed" else kind)
    assert observed[0]["saw_terminal"] == (kind == "terminal")
    assert observed[0]["http_status"] == 200
    assert observed[0]["malformed_events"] == (1 if kind == "malformed" else 0)
    assert "visible fixture" not in json.dumps(observed)


def test_openai_decoder_preserves_split_unicode_and_bounds_pending_frames():
    from model_runtime.openai_sse import OpenAIChatSSEDecoder
    decoder = OpenAIChatSSEDecoder()
    encoded = ('data: ' + json.dumps({"choices":[{"delta":{"content":"zażółć"}}]},ensure_ascii=False) + '\n').encode()
    events = []
    for byte in encoded:
        events.extend(decoder.feed_text(bytes([byte])))
    assert events[0].payload["choices"][0]["delta"]["content"] == "zażółć"
    bounded = OpenAIChatSSEDecoder(max_frame_chars=32)
    with pytest.raises(ValueError,match="bounded frame"):
        bounded.feed_text("data: " + "x"*64)
    assert bounded.diagnostics()["end_reason"] == "oversized_event"


class _StreamResponse:
    def __init__(self, lines, *, status_code=200, headers=None, body=b""):
        self.lines = list(lines)
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aread(self):
        return self.body


def _client_for(response):
    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return response

    return _Client


def _sequenced_client_for(responses):
    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return responses.pop(0)

    return _Client


def _capturing_client_for(response, captured):
    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **kwargs):
            captured["payload"] = kwargs.get("json")
            captured["url"] = _args[1]
            captured["headers"] = kwargs.get("headers")
            return response

    return _Client


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,model,path",[
    ("opencode-zen","gpt-6.1-sol","responses"),
    ("opencode-go","grok-4.7","responses"),
    ("opencode-zen","claude-sonnet-4-6","messages"),
    ("opencode-go","qwen3.8-max","messages"),
    ("opencode-zen","qwen3.8-max","chat/completions"),
    ("opencode-go","minimax-m2.7","messages"),
    ("opencode-zen","minimax-m2.7","chat/completions"),
    ("opencode-zen","gemini-3.8-flash","models/gemini-3.8-flash:streamGenerateContent?alt=sse"),
])
async def test_opencode_wire_endpoint_headers_and_session_identity(monkeypatch,provider,model,path):
    from model_runtime.prompt_cache import resolve_prompt_cache_identity
    router = _router()
    profile = router.provider_profile(provider)
    rows = {
        "responses": ['data: {"type":"response.output_text.delta","delta":"done"}',
                      'data: {"type":"response.completed","response":{"status":"completed"}}'],
        "messages": ['data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"done"}}',
                     'data: {"type":"message_stop"}'],
        "chat/completions": ['data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}'],
    }
    if path.startswith("models/"):
        stream=['data: {"candidates":[{"content":{"parts":[{"text":"done"}]},"finishReason":"STOP"}]}']
    else:
        stream=rows[path]
    captured={}
    monkeypatch.setattr(cloud.httpx,"AsyncClient",_capturing_client_for(_StreamResponse(stream),captured))
    identity=resolve_prompt_cache_identity("same-durable-test-chat")
    for _purpose in ("main","auxiliary"):
        output=[token async for token in cloud.call_cloud_once(router,profile,
            CredentialLease(provider,"test","Test","dummy-test-key"),model,[{"role":"user","content":"fixture"}],
            {"max_tokens":32},False,None,None,prompt_cache_identity=identity,tools=[IPYTHON_PROVIDER_SPEC])]
        assert output == ["done"]
        assert captured["url"] == profile.base_url+"/"+path
        assert captured["headers"]["Authorization"] == "Bearer dummy-test-key"
        assert captured["headers"]["User-Agent"] == "VARIANT-1"
        assert captured["headers"]["x-opencode-session"] == identity.key
        assert "dummy-test-key" not in captured["url"]
        if path == "responses":
            assert captured["payload"]["tools"][0]["name"] == "ipython"
        elif path == "messages":
            assert captured["payload"]["tools"][0]["name"] == "ipython"
        elif path == "chat/completions":
            assert captured["payload"]["tools"][0]["function"]["name"] == "ipython"
        else:
            assert captured["payload"]["tools"][0]["functionDeclarations"][0]["name"] == "ipython"


@pytest.mark.asyncio
async def test_compatible_provider_projects_declared_reasoning_effort(monkeypatch):
    response = _StreamResponse([
        "data: " + json.dumps({
            "choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}],
        }),
    ])
    captured = {}
    monkeypatch.setattr(
        cloud.httpx, "AsyncClient", _capturing_client_for(response, captured),
    )
    router = _router()
    monkeypatch.setattr(router, "get_reasoning_effort", lambda *_args: "high")

    output = [token async for token in cloud.call_openai(
        router,
        [{"role": "user", "content": "test"}],
        {"max_tokens": 32},
        "",
        label="hermes",
        model="upstage/solar-pro4:free",
        profile=router.provider_profile("hermes"),
    )]

    assert output == ["done"]
    assert captured["payload"]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_openai_stream_accepts_thinking_and_finish_reason_without_done(
    monkeypatch,
):
    lines = [
        "data: " + json.dumps({
            "choices": [{"delta": {"thinking": "private plan"},
                         "finish_reason": None}],
        }),
        "data: " + json.dumps({
            "choices": [{"delta": {"content": "done"},
                         "finish_reason": "stop"}],
        }),
    ]
    response = _StreamResponse(lines)
    monkeypatch.setattr(cloud.httpx, "AsyncClient", _client_for(response))
    reasoning = []

    output = [token async for token in cloud.call_openai(
        _router(),
        [{"role": "user", "content": "test"}],
        {"max_tokens": 32},
        "key",
        reasoning_sink=reasoning.append,
    )]

    assert output == ["done"]
    assert reasoning == ["private plan"]


@pytest.mark.asyncio
async def test_openai_stream_promotes_embedded_rate_limit_to_typed_error(
    monkeypatch,
):
    response = _StreamResponse([
        "data: " + json.dumps({
            "error": {
                "code": 429,
                "message": "injected stream rate limit",
                "metadata": {"error_type": "rate_limit_exceeded"},
            },
        }),
    ])
    monkeypatch.setattr(cloud.httpx, "AsyncClient", _client_for(response))

    with pytest.raises(ProviderRequestError) as captured:
        _ = [token async for token in cloud.call_openai(
            _router(),
            [{"role": "user", "content": "test"}],
            {"max_tokens": 32},
            "key",
        )]

    assert captured.value.status_code == 429
    assert "injected stream rate limit" in str(captured.value)


@pytest.mark.asyncio
async def test_openai_stream_surfaces_refusal_text(monkeypatch):
    response = _StreamResponse([
        "data: " + json.dumps({
            "choices": [{
                "delta": {"content": None, "refusal": "I cannot help."},
                "finish_reason": "stop",
            }],
        }),
    ])
    monkeypatch.setattr(cloud.httpx, "AsyncClient", _client_for(response))

    output = [token async for token in cloud.call_openai(
        _router(), [{"role": "user", "content": "test"}],
        {"max_tokens": 32}, "key",
    )]

    assert output == ["I cannot help."]


def test_usage_limit_reached_is_typed_but_not_immediately_retried():
    error = {
        "type": "usage_limit_reached",
        "message": "five-hour subscription window exhausted",
    }

    failure = cloud._provider_stream_error("openai-codex", {"error": error})

    assert failure is not None
    assert failure.status_code == 429
    assert cloud._is_transient_provider_error(failure) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_kind", ["http", "sse"])
async def test_cloud_route_replays_identical_step_after_rate_limit(
    monkeypatch, fault_kind,
):
    if fault_kind == "http":
        fault_response = _StreamResponse(
            [],
            status_code=429,
            body=json.dumps({
                "error": {"code": 429, "message": "injected rate limit"},
            }).encode(),
        )
    else:
        fault_response = _StreamResponse([
            "data: " + json.dumps({
                "error": {
                    "code": 429,
                    "message": "injected stream rate limit",
                    "metadata": {"error_type": "rate_limit_exceeded"},
                },
            }),
        ])
    responses = [
        fault_response,
        _StreamResponse([
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "recovered"},
                    "finish_reason": "stop",
                }],
            }),
        ]),
    ]
    monkeypatch.setattr(
        cloud.httpx, "AsyncClient", _sequenced_client_for(responses),
    )
    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(cloud.asyncio, "sleep", no_sleep)
    router = _router()
    router._credential_leases = lambda provider: [CredentialLease(
        provider, "test", "Test", "key", source="environment",
    )]

    output = [token async for token in router._call_cloud(
        [{"role": "user", "content": "test"}], {"max_tokens": 32},
        tools=[IPYTHON_PROVIDER_SPEC],
    )]

    assert output == ["recovered"]
    assert responses == []
    assert delays == [2.0]


def test_retry_classifier_excludes_quota_exhaustion_and_long_server_delays():
    transient = ProviderRequestError("openai", "rate limited", status_code=429)
    exhausted = ProviderRequestError(
        "openai", "insufficient_quota: billing limit", status_code=429,
    )
    too_long = ProviderRequestError(
        "openai", "rate limited", status_code=429, retry_after_seconds=300,
    )

    assert cloud._is_transient_provider_error(transient) is True
    assert cloud._is_transient_provider_error(exhausted) is False
    assert cloud._cloud_retry_delay(transient, 0) == 2.0
    assert cloud._cloud_retry_delay(too_long, 0) is None


@pytest.mark.asyncio
async def test_failed_attempt_reasoning_is_discarded_before_safe_retry(monkeypatch):
    responses = [
        _StreamResponse([
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "discarded reasoning"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "error": {
                    "code": 503,
                    "message": "temporary stream failure",
                },
            }),
        ]),
        _StreamResponse([
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"reasoning_content": "kept reasoning"},
                    "finish_reason": None,
                }],
            }),
            "data: " + json.dumps({
                "choices": [{
                    "delta": {"content": "recovered"},
                    "finish_reason": "stop",
                }],
            }),
        ]),
    ]
    monkeypatch.setattr(
        cloud.httpx, "AsyncClient", _sequenced_client_for(responses),
    )
    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(cloud.asyncio, "sleep", no_sleep)
    router = _router()
    router._credential_leases = lambda provider: [CredentialLease(
        provider, "test", "Test", "key", source="environment",
    )]
    reasoning = []

    output = [token async for token in router._call_cloud(
        [{"role": "user", "content": "test"}],
        {"max_tokens": 32},
        reasoning_sink=reasoning.append,
        tools=[IPYTHON_PROVIDER_SPEC],
    )]

    assert output == ["recovered"]
    assert reasoning == ["kept reasoning"]
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_long_retry_after_surfaces_clean_agent_replay_boundary(monkeypatch):
    response = _StreamResponse(
        [],
        status_code=524,
        headers={"retry-after": "120"},
        body=b"gateway timeout",
    )
    monkeypatch.setattr(cloud.httpx, "AsyncClient", _client_for(response))
    router = _router()
    router._credential_leases = lambda provider: [CredentialLease(
        provider, "test", "Test", "key", source="environment",
    )]

    with pytest.raises(ProviderRequestError) as captured:
        _ = [token async for token in router._call_cloud(
            [{"role": "user", "content": "test"}],
            {"max_tokens": 32},
            tools=[IPYTHON_PROVIDER_SPEC],
        )]

    assert captured.value.status_code == 524
    assert captured.value.retry_after_seconds == 120
    assert captured.value.clean_turn_replay_safe is True
    assert captured.value.model_output_observed is False


@pytest.mark.asyncio
async def test_bound_cloud_route_never_enters_global_provider_fallback(monkeypatch):
    router = _router()
    router.set_fallback_chain(["xai"])
    router._credential_leases = lambda provider: [CredentialLease(
        provider, "test", "Test", "key", source="environment",
    )]
    attempted = []

    async def fail_route(_router, profile, *_args, **_kwargs):
        attempted.append(profile.name)
        raise ProviderRequestError(
            profile.name, "invalid request", status_code=400,
        )
        yield  # pragma: no cover - retain async-generator shape

    monkeypatch.setattr(cloud, "call_cloud_once", fail_route)
    route = {
        "mode": "cloud", "provider": "openai", "model": "test-model",
    }

    with router.bind_model_route(route):
        with pytest.raises(cloud.LocalEngineError):
            _ = [token async for token in router._call_cloud(
                [{"role": "user", "content": "test"}],
                {"max_tokens": 32},
                tools=[IPYTHON_PROVIDER_SPEC],
            )]

    assert attempted == ["openai"]
