"""OpenAI-compatible loopback gateway for VARIANT-1's active local runtime."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
import time
import uuid

import anyio
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from model_runtime.openai_sse import OpenAIChatSSEDecoder


MAX_GATEWAY_REQUEST_BYTES = 64 * 1024 * 1024


async def _request_json(request: Request):
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_GATEWAY_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="inference request exceeds 64 MiB")
        payload.extend(chunk)
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="valid JSON object required") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return body


class _StreamResources:
    """Own the scheduler and HTTP handles across response setup and streaming."""

    def __init__(self, lease):
        self.lease = lease
        self.client = None
        self.upstream = None
        self._close_task = None

    async def _close(self):
        try:
            if self.upstream is not None:
                await self.upstream.aclose()
        finally:
            try:
                if self.client is not None:
                    await self.client.aclose()
            finally:
                await self.lease.__aexit__(None, None, None)

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancelled = False
        # Starlette disconnects use an AnyIO cancellation scope. Shield both
        # that scope and direct asyncio cancellation until ownership is settled.
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.shield(self._close_task)
                    break
                except asyncio.CancelledError:
                    if self._close_task.done():
                        raise
                    cancelled = True
        if cancelled:
            raise asyncio.CancelledError


class _GatewayStreamingResponse(StreamingResponse):
    def __init__(self, *args, resources, **kwargs):
        super().__init__(*args, **kwargs)
        self._resources = resources

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # The response-start send can fail before the generator is entered.
            await self._resources.close()


@asynccontextmanager
async def _null_slot():
    yield


def _admission_slot(host):
    factory = getattr(host.router, "_local_gate", None)
    return factory() if callable(factory) else _null_slot()


async def _acquire_ready_engine(host):
    """Acquire the scheduler only after readiness, then fence engine identity."""

    while True:
        expected = await _ensure_engine(host)
        lease = _admission_slot(host)
        await lease.__aenter__()
        if host.router.engine is expected and host.router.engine_ready:
            return lease, expected
        await lease.__aexit__(None, None, None)


@asynccontextmanager
async def _admitted_engine(host):
    lease, engine = await _acquire_ready_engine(host)
    try:
        yield engine
    finally:
        await lease.__aexit__(None, None, None)


async def _ensure_engine(host) -> object:
    from model_runtime import engine_manager
    await engine_manager.ensure_active_runtime(host.router)
    return host.router.engine


def _headers(engine) -> dict:
    value = getattr(engine, "request_headers", None)
    return dict(value) if isinstance(value, dict) else {}


def _model(host, engine=None) -> str:
    selected = engine if engine is not None else host.router.engine
    return str(
        getattr(selected, "api_model", "")
        or host.router.model_name
        or "variant1-local"
    )


def _model_error(host, engine, requested: str = "") -> JSONResponse | None:
    value = str(requested or "").strip()
    selected = _model(host, engine)
    if not value or value in {"default", "variant1-local", selected}:
        return None
    return JSONResponse(
        {
            "error": {
                "message": (
                    f"model {value!r} is not attached to VARIANT-1's active "
                    f"local runtime (selected: {selected!r})"
                ),
                "type": "invalid_request_error",
                "code": "model_not_attached",
            }
        },
        status_code=400,
    )


async def models(host) -> JSONResponse:
    async with _admitted_engine(host) as engine:
        model = _model(host, engine)
    return JSONResponse({
        "object": "list",
        "data": [{
            "id": model, "object": "model", "created": int(time.time()),
            "owned_by": "variant1", "runtime": str(getattr(engine, "runtime_id", "llamacpp")),
        }],
    })


async def status(host) -> JSONResponse:
    engine = host.router.engine
    return JSONResponse({
        "ready": bool(host.router.engine_ready),
        "model": _model(host),
        "runtime": engine.runtime_status(),
        "recipes": host.runtime_recipes.process_status() if getattr(host, "runtime_recipes", None) else {},
    })


async def tokenize(host, request: Request) -> Response:
    body = await _request_json(request)
    async with _admitted_engine(host) as engine:
        text = str(body.get("content") or body.get("text") or "") if isinstance(body, dict) else ""
        count = await engine.count_tokens(text)
        if count is not None:
            return JSONResponse({"count": int(count)})
        upstream = f"{str(engine.base_url).rstrip('/')}/tokenize"
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            response = await client.post(upstream, json=body, headers=_headers(engine))
    return Response(response.content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))


async def chat_completions(host, request: Request) -> Response:
    body = await _request_json(request)
    body = dict(body)
    request_id = f"GW-{uuid.uuid4().hex[:12].upper()}"
    started_wall = time.time()
    started = time.perf_counter()
    engine = host.router.engine
    runtime_id = str(getattr(engine, "runtime_id", "llamacpp"))
    runtime_name = str(getattr(engine, "display_name", runtime_id))
    requested_model = str(body.get("model") or "")
    body["model"] = _model(host, engine)

    if not bool(body.get("stream")):
        try:
            async with _admitted_engine(host) as engine:
                invalid = _model_error(host, engine, requested_model)
                if invalid is not None:
                    return invalid
                body["model"] = _model(host, engine)
                endpoint = f"{str(engine.base_url).rstrip('/')}/v1/chat/completions"
                runtime_id = str(getattr(engine, "runtime_id", "llamacpp"))
                runtime_name = str(getattr(engine, "display_name", runtime_id))
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0), trust_env=False) as client:
                    response = await client.post(endpoint, json=body, headers=_headers(engine))
            try:
                payload = response.json() if response.content else {}
            except Exception:
                payload = {}
            usage = payload.get("usage") if isinstance(payload, dict) and isinstance(payload.get("usage"), dict) else {}
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            host.inference_observability.record_request({
                "request_id": request_id, "started_at": started_wall, "finished_at": time.time(),
                "status": "complete" if response.is_success else "error", "runtime_id": runtime_id,
                "runtime_name": runtime_name, "model": body["model"],
                "prompt_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
                "latency_ms": elapsed_ms, "ttft_ms": elapsed_ms,
                "error": "" if response.is_success else (response.text or str(payload))[:500], "source": "gateway",
            })
            return Response(response.content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))
        except Exception as exc:
            host.inference_observability.record_request({
                "request_id": request_id, "started_at": started_wall, "finished_at": time.time(),
                "status": "error", "runtime_id": runtime_id, "runtime_name": runtime_name,
                "model": body["model"], "latency_ms": (time.perf_counter() - started) * 1000,
                "error": str(exc), "source": "gateway",
            })
            raise

    lease, engine = await _acquire_ready_engine(host)
    resources = _StreamResources(lease)
    try:
        invalid = _model_error(host, engine, requested_model)
        if invalid is not None:
            await resources.close()
            return invalid
        body["model"] = _model(host, engine)
        endpoint = f"{str(engine.base_url).rstrip('/')}/v1/chat/completions"
        runtime_id = str(getattr(engine, "runtime_id", "llamacpp"))
        runtime_name = str(getattr(engine, "display_name", runtime_id))
        client = resources.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=600.0), trust_env=False)
        upstream_request = client.build_request("POST", endpoint, json=body, headers=_headers(engine))
        upstream = resources.upstream = await client.send(upstream_request, stream=True)
        if not upstream.is_success:
            content = await upstream.aread()
            await resources.close()
            host.inference_observability.record_request({
                "request_id": request_id, "started_at": started_wall, "finished_at": time.time(),
                "status": "error", "runtime_id": runtime_id, "runtime_name": runtime_name,
                "model": body["model"], "latency_ms": (time.perf_counter() - started) * 1000,
                "error": content.decode("utf-8", errors="replace")[:500], "source": "gateway",
            })
            return Response(content, status_code=upstream.status_code, media_type=upstream.headers.get("content-type", "application/json"))
    except BaseException as exc:
        await resources.close()
        host.inference_observability.record_request({
            "request_id": request_id, "started_at": started_wall, "finished_at": time.time(),
            "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
            "runtime_id": runtime_id, "runtime_name": runtime_name,
            "model": body["model"], "latency_ms": (time.perf_counter() - started) * 1000,
            "error": str(exc), "source": "gateway",
        })
        raise

    async def stream():
        first = None
        chunks = 0
        usage = {}
        error = ""
        status_value = "complete"
        decoder = OpenAIChatSSEDecoder()
        try:
            async for raw in upstream.aiter_bytes():
                if raw:
                    for decoded in decoder.feed_text(raw):
                        if decoded.done:
                            continue
                        event = decoded.payload or {}
                        content = (((event.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
                        if content:
                            first = first or time.perf_counter()
                            chunks += 1
                        if isinstance(event.get("usage"), dict):
                            usage = event["usage"]
                    yield raw
            decoder.finish()
            decoder.require_terminal("loopback gateway stream")
        except asyncio.CancelledError:
            status_value = "cancelled"
            error = "client disconnected"
            raise
        except Exception as exc:
            status_value = "error"
            error = str(exc)
            raise
        finally:
            finished = time.perf_counter()
            await resources.close()
            output_tokens = int(usage.get("completion_tokens") or chunks)
            decode_s = max(0.0, finished - (first or finished))
            host.inference_observability.record_request({
                "request_id": request_id, "started_at": started_wall, "finished_at": time.time(),
                "status": status_value, "runtime_id": runtime_id, "runtime_name": runtime_name,
                "model": body["model"], "prompt_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": output_tokens, "ttft_ms": ((first or finished) - started) * 1000,
                "latency_ms": (finished - started) * 1000,
                "decode_tps": max(0, output_tokens - 1) / decode_s if decode_s else 0,
                "error": error, "source": "gateway",
            })

    try:
        return _GatewayStreamingResponse(
            stream(), resources=resources, status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )
    except BaseException:
        await resources.close()
        raise


__all__ = ["chat_completions", "models", "status", "tokenize"]
