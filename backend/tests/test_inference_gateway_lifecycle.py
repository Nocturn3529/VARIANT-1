"""Gateway ownership under transport failure and cancellation, without a server."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from automation.scheduler import LLMScheduler, principal_for
from model_runtime import openai_gateway


class Upstream:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self):
        self.read_error = None
        self.stream_error = None
        self.close_error = None
        self.closed = 0
        self.stream_started = asyncio.Event()
        self.stream_gate = None
        self.close_started = asyncio.Event()
        self.close_gate = None

    @property
    def is_success(self):
        return self.status_code < 400

    async def aread(self):
        if self.read_error:
            raise self.read_error
        return b'{"error":"unavailable"}'

    async def aiter_bytes(self):
        self.stream_started.set()
        if self.stream_gate is not None:
            await self.stream_gate.wait()
        if self.stream_error:
            raise self.stream_error
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed += 1
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error:
            raise self.close_error


@pytest.fixture
def gateway(monkeypatch):
    scheduler = LLMScheduler()
    upstream = Upstream()
    records = []
    engine = SimpleNamespace(base_url="http://unused.test", api_model="test-model")
    host = SimpleNamespace(
        router=SimpleNamespace(
            engine=engine, engine_ready=True, model_name="test-model",
            _local_gate=lambda: scheduler.slot(principal_for("chat")),
        ),
        inference_observability=SimpleNamespace(record_request=records.append),
    )

    class Client:
        closed = 0
        send_error = None
        send_gate = None
        close_error = None

        def __init__(self):
            self.send_started = asyncio.Event()

        def build_request(self, *_args, **_kwargs):
            return object()

        async def send(self, _request, *, stream):
            assert stream
            self.send_started.set()
            if self.send_gate is not None:
                await self.send_gate.wait()
            if self.send_error:
                raise self.send_error
            return upstream

        async def aclose(self):
            self.closed += 1
            if self.close_error:
                raise self.close_error

    async def ready(_host):
        return engine

    async def request_stream():
        yield b'{"stream": true, "messages": [], "model": "test-model"}'

    client = Client()
    monkeypatch.setattr(openai_gateway, "_ensure_engine", ready)
    monkeypatch.setattr(openai_gateway.httpx, "AsyncClient", lambda **_kwargs: client)
    return SimpleNamespace(
        host=host, client=client, upstream=upstream, scheduler=scheduler,
        request=SimpleNamespace(stream=request_stream), records=records,
    )


async def start(gateway):
    return await openai_gateway.chat_completions(gateway.host, gateway.request)


async def assert_released(gateway, *, upstream_closed=1):
    assert gateway.client.closed == 1
    assert gateway.upstream.closed == upstream_closed
    assert not gateway.scheduler.status()["busy"]
    # Verify the next local model request can actually acquire the real gate.
    async with asyncio.timeout(1):
        async with gateway.scheduler.slot(principal_for("chat")):
            pass


async def test_cancel_before_response_headers_releases_client_and_scheduler(gateway):
    gateway.client.send_gate = asyncio.Event()
    request = asyncio.create_task(start(gateway))
    await gateway.client.send_started.wait()
    assert gateway.scheduler.status()["busy"]
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await assert_released(gateway, upstream_closed=0)
    assert gateway.records[-1]["status"] == "cancelled"


async def test_header_transport_failure_releases_client_and_scheduler(gateway):
    gateway.client.send_error = httpx.ReadError("headers lost")
    with pytest.raises(httpx.ReadError, match="headers lost"):
        await start(gateway)
    await assert_released(gateway, upstream_closed=0)


async def test_error_body_disconnect_releases_every_resource(gateway):
    gateway.upstream.status_code = 503
    gateway.upstream.read_error = httpx.ReadError("error body lost")
    with pytest.raises(httpx.ReadError, match="error body lost"):
        await start(gateway)
    await assert_released(gateway)


async def test_error_response_releases_resources_before_return(gateway):
    gateway.upstream.status_code = 503
    response = await start(gateway)
    assert response.status_code == 503
    assert b"unavailable" in response.body
    await assert_released(gateway)


async def test_success_holds_scheduler_until_stream_finishes(gateway):
    response = await start(gateway)
    assert gateway.scheduler.status()["busy"]
    chunks = [chunk async for chunk in response.body_iterator]
    assert b"[DONE]" in b"".join(chunks)
    await assert_released(gateway)
    assert gateway.records[-1]["status"] == "complete"


async def test_stream_read_failure_releases_every_resource(gateway):
    gateway.upstream.stream_error = httpx.ReadError("stream lost")
    response = await start(gateway)
    with pytest.raises(httpx.ReadError, match="stream lost"):
        async for _chunk in response.body_iterator:
            pass
    await assert_released(gateway)


@pytest.mark.parametrize("failing_resource", ["upstream", "client"])
async def test_close_failure_still_releases_other_resources(gateway, failing_resource):
    getattr(gateway, failing_resource).close_error = OSError("close failed")
    response = await start(gateway)
    with pytest.raises(OSError, match="close failed"):
        async for _chunk in response.body_iterator:
            pass
    await assert_released(gateway)


async def test_response_start_failure_closes_unstarted_generator_resources(gateway):
    response = await start(gateway)

    async def send(_message):
        raise OSError("downstream closed")

    async def receive():
        await asyncio.Future()

    with pytest.raises(Exception):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert not gateway.upstream.stream_started.is_set()
    await assert_released(gateway)


async def test_asgi_disconnect_releases_resources_inside_cancel_scope(gateway):
    gateway.upstream.stream_gate = asyncio.Event()
    response = await start(gateway)

    async def receive():
        await gateway.upstream.stream_started.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    async with asyncio.timeout(1):
        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    await assert_released(gateway)


async def test_repeated_cancellation_waits_for_cleanup_and_releases_scheduler(gateway):
    gateway.upstream.status_code = 503
    gateway.upstream.close_gate = asyncio.Event()
    request = asyncio.create_task(start(gateway))
    await gateway.upstream.close_started.wait()
    request.cancel()
    await asyncio.sleep(0)
    request.cancel()
    await asyncio.sleep(0)
    assert gateway.scheduler.status()["busy"]
    gateway.upstream.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    await assert_released(gateway)
