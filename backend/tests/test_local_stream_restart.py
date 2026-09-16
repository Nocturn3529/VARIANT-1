"""Local stream restarts llama once after connection failures."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import llm_local_stream as lls
from llm_stream_diagnostics import StreamDiagnostics
from model_runtime.llama_server import LlamaServer


class _Engine:
    def __init__(self, ready=True):
        self.ready = ready
        self.model = "m.gguf"
        self.reasoning_budget = 0
        self.base_url = "http://127.0.0.1:8080"
        self.poll_calls = 0

    def poll_process(self):
        self.poll_calls += 1
        return self.ready


class _Inference:
    def start(self, model):
        return "t1", {"model": model}

    def token(self, *a, **k):
        return None

    def complete(self, *a, **k):
        return {"prompt_tokens": 1, "output_tokens": 1}

    def fail(self, *a, **k):
        return {"status": "error"}


class _Router:
    def __init__(self):
        self.engine = _Engine(ready=False)
        self._inference = _Inference()
        self.model_name = "m.gguf"
        self.start_calls = 0
        self._published = []

    async def start_local(self):
        self.start_calls += 1
        self.engine.ready = True

    async def _publish_inference(self, snap, force=False):
        self._published.append(snap)

    def _record_usage(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_ensure_local_ready_starts_cold_engine(monkeypatch):
    router = _Router()

    async def ensure(_router):
        await router.start_local()

    monkeypatch.setattr(
        "model_runtime.engine_manager.ensure_local_engine",
        AsyncMock(side_effect=ensure),
    )
    await lls._ensure_local_ready(router)
    assert router.start_calls == 1
    assert router.engine.ready is True


@pytest.mark.asyncio
async def test_ensure_local_ready_does_not_double_start_after_lifecycle_failure(monkeypatch):
    router = _Router()
    monkeypatch.setattr(
        "model_runtime.engine_manager.ensure_local_engine",
        AsyncMock(side_effect=RuntimeError("candidate failed to load")),
    )

    with pytest.raises(RuntimeError, match="candidate failed to load"):
        await lls._ensure_local_ready(router)

    assert router.start_calls == 0
    assert router.engine.ready is False


@pytest.mark.asyncio
async def test_call_local_retries_after_connect_error(monkeypatch):
    router = _Router()
    router.engine.ready = True

    class _StreamCtx:
        def __init__(self, fail_first):
            self.fail_first = fail_first
            self.calls = 0

        async def __aenter__(self):
            self.calls += 1
            if self.fail_first and self.calls == 1:
                raise httpx.ConnectError("All connection attempts failed")
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

        @property
        def status_code(self):
            return 200

        async def aread(self):
            return b""

    stream_ctx = _StreamCtx(fail_first=True)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return stream_ctx

    monkeypatch.setattr(lls.httpx, "AsyncClient", _Client)

    async def ensure(_router):
        await router.start_local()

    monkeypatch.setattr(
        "model_runtime.engine_manager.ensure_local_engine",
        AsyncMock(side_effect=ensure),
    )

    tokens = []
    diagnostics = StreamDiagnostics()
    async for tok in lls.call_local_inner(
            router, [{"role": "user", "content": "hi"}], {"max_tokens": 8},
            stream_diagnostics=diagnostics):
        tokens.append(tok)
    assert tokens == ["hi"]
    assert diagnostics.finish_reason == "stop"
    assert router.start_calls == 1
    assert stream_ctx.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delta_kind", ["content", "reasoning", "thinking", "tool_call"],
)
async def test_call_local_does_not_retry_after_observable_delta(
        monkeypatch, delta_kind):
    router = _Router()
    router.engine.ready = True

    deltas = {
        "content": {"content": "partial"},
        "reasoning": {"reasoning_content": "thinking"},
        "thinking": {"thinking": "thinking"},
        "tool_call": {"tool_calls": [{"index": 0, "id": "call_1"}]},
    }

    class _StreamCtx:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            self.calls += 1
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield "data: " + json.dumps({
                "choices": [{"delta": deltas[delta_kind]}],
            })
            raise httpx.RemoteProtocolError("stream ended unexpectedly")

        @property
        def status_code(self):
            return 200

        async def aread(self):
            return b""

    stream_ctx = _StreamCtx()

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return stream_ctx

    class _ToolSink:
        def __init__(self):
            self.deltas = []

        def add_openai_delta(self, delta):
            self.deltas.append(delta)

    reasoning = []
    tool_sink = _ToolSink()
    monkeypatch.setattr(lls.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(
        "model_runtime.engine_manager.ensure_local_engine",
        AsyncMock(side_effect=router.start_local),
    )

    tokens = []
    with pytest.raises(lls.LocalEngineError, match="stream ended unexpectedly"):
        async for tok in lls.call_local_inner(
                router,
                [{"role": "user", "content": "hi"}],
                {"max_tokens": 8},
                reasoning_sink=reasoning.append,
                tool_call_sink=tool_sink):
            tokens.append(tok)

    assert stream_ctx.calls == 1
    assert router.start_calls == 0
    assert tokens == (["partial"] if delta_kind == "content" else [])
    assert reasoning == (
        ["thinking"] if delta_kind in {"reasoning", "thinking"} else []
    )
    assert len(tool_sink.deltas) == (1 if delta_kind == "tool_call" else 0)


@pytest.mark.asyncio
async def test_call_local_rejects_clean_eof_without_terminal_event(monkeypatch):
    router = _Router()
    router.engine.ready = True

    class _StreamCtx:
        calls = 0

        async def __aenter__(self):
            self.calls += 1
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'

        status_code = 200

        async def aread(self):
            return b""

    stream = _StreamCtx()

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return stream

    monkeypatch.setattr(lls.httpx, "AsyncClient", _Client)
    tokens = []

    with pytest.raises(lls.LocalEngineError, match="without a terminal event"):
        async for token in lls.call_local_inner(
            router,
            [{"role": "user", "content": "hi"}],
            {"max_tokens": 8},
        ):
            tokens.append(token)

    assert tokens == ["partial"]
    assert stream.calls == 1
    assert router.start_calls == 0


@pytest.mark.asyncio
async def test_local_stream_surfaces_http_200_error_envelope(monkeypatch):
    router = _Router()
    router.engine.ready = True

    class Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            yield 'data: {"error":{"message":"context overflow"}}'

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return Stream()

    monkeypatch.setattr(lls.httpx, "AsyncClient", Client)

    with pytest.raises(lls.LocalEngineError, match="context overflow"):
        _ = [token async for token in lls.call_local_inner(
            router, [{"role": "user", "content": "test"}],
            {"max_tokens": 8},
        )]

    assert router.start_calls == 0


def test_is_unreachable_local_detects_connect_phrase():
    assert lls._is_unreachable_local(httpx.ConnectError("All connection attempts failed"))
    assert lls._is_unreachable_local(RuntimeError("connection refused"))
    assert not lls._is_unreachable_local(httpx.ReadTimeout("slow prefill"))
    assert not lls._is_unreachable_local(RuntimeError("rate limited"))


@pytest.mark.asyncio
async def test_llama_start_settles_existing_managed_owner_before_attach():
    server = LlamaServer({"autostart": False}, ".")
    server.proc = object()
    server._proc_job = object()
    server.stop = AsyncMock()
    server._wait_healthy = AsyncMock(return_value=True)

    await server.start(ready_timeout_s=1)

    server.stop.assert_awaited_once()
    assert server.ready is True
