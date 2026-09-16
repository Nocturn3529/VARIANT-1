"""Local OpenAI-compatible inference streaming helpers for LLMRouter.

Keeps OpenAI-compatible /v1/chat/completions stream parsing and inference
telemetry out of the router facade so llm_router.py stays orchestration.
"""

from __future__ import annotations

from contextlib import aclosing

import asyncio
from contextlib import asynccontextmanager

import httpx

from model_runtime.llama_server import LocalEngineError
from llm_cloud_stream import STREAM_TIMEOUT
from model_runtime.message_graph import build_message_graph, render_openai_chat
from model_runtime.request_manifest import (
    model_request_event_hooks,
    patch_provider_response_identity,
    provider_response_identity,
)
from model_runtime.prompt_cache import apply_prompt_cache_identity
from model_runtime.openai_sse import OpenAIChatSSEDecoder


@asynccontextmanager
async def null_gate():
    """No-op gate used when no scheduler is wired (tests, cloud-only)."""
    yield


async def call_local(router, messages: list, sampling: dict, json_mode: bool = False,
                     image_b64=None, reasoning_budget: int = None,
                     reasoning_sink=None, tools: list = None, tool_call_sink=None,
                     stream_diagnostics=None, prompt_cache_identity=None):
    """Acquire the optional local scheduler gate, then stream from the engine."""
    try:
        from model_runtime.engine_manager import _local_lifecycle_slot
    except ImportError:
        gate_factory = getattr(router, "_local_gate", None)
        gate = gate_factory() if gate_factory else null_gate()
    else:
        gate = _local_lifecycle_slot(router)
    async with gate:
        async with aclosing(call_local_inner(
                router, messages, sampling, json_mode, image_b64, reasoning_budget,
                reasoning_sink=reasoning_sink, tools=tools, tool_call_sink=tool_call_sink,
                stream_diagnostics=stream_diagnostics,
                prompt_cache_identity=prompt_cache_identity)) as owned_stream:
            async for tok in owned_stream:
                yield tok


def _is_unreachable_local(exc: BaseException) -> bool:
    """True when the selected local endpoint is not accepting connections."""
    if isinstance(exc, (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.RemoteProtocolError,
        httpx.WriteError,
        httpx.NetworkError,
    )):
        return True
    msg = str(exc or "").lower()
    return any(
        needle in msg
        for needle in (
            "all connection attempts failed",
            "connection refused",
            "connect error",
            "actively refused",
            "winerror 10061",
            "errno 111",
            "name or service not known",
        )
    )


async def _ensure_local_ready(router) -> None:
    """Start the bundled engine or attach to a configured external endpoint."""
    poll = getattr(router.engine, "poll_process", None)
    if callable(poll):
        poll()
    if router.engine.ready:
        return
    try:
        from model_runtime import engine_manager
    except ImportError:
        # Narrow fallback for stripped/unit-test environments only. A real
        # lifecycle failure must propagate instead of immediately spawning a
        # second process after the managed start already failed.
        await router.start_local()
    else:
        await engine_manager.ensure_local_engine(router)
    if not router.engine.ready:
        raise LocalEngineError("local engine not ready")


def render_local_messages(messages: list, image_b64=None) -> list[dict]:
    """Project VARIANT-1's message interchange into the OpenAI chat shape.

    Prompt budgeting calls this same function as generation so tool-call IDs,
    multimodal content blocks, and provider replay normalization cannot drift
    between the preflight count and the request that follows it.
    """
    return render_openai_chat(build_message_graph(messages, image_b64))


def build_local_template_payload(router, messages: list,
                                 reasoning_budget: int | None,
                                 tools: list | None) -> dict:
    """Return the chat-template-relevant subset of a local request.

    llama.cpp's native input-token endpoint (and the legacy
    ``/apply-template`` fallback) accepts the OpenAI messages and provider tools
    used by ``/v1/chat/completions``. Keeping their construction here gives
    token preflight the same schema serialization and thinking-template knobs
    as inference without sending sampling or streaming-only fields.
    """
    payload = {"messages": messages}
    if tools:
        import tool_calling
        payload["tools"] = tool_calling.to_openai_tools(tools)
        payload["tool_choice"] = "auto"
    if getattr(router.engine, "supports_llama_extensions", True):
        if reasoning_budget is not None:
            payload["reasoning_budget"] = int(reasoning_budget)
        else:
            payload["reasoning_budget"] = router.engine.reasoning_budget
        if payload["reasoning_budget"] == 0:
            # Some templates use this kwarg even when they ignore the llama.cpp
            # reasoning_budget extension.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _build_local_payload(router, messages: list, sampling: dict, json_mode: bool,
                          reasoning_budget: int | None, tools: list | None) -> dict:
    payload = {
        **build_local_template_payload(
            router, messages, reasoning_budget, tools),
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": sampling.get("temperature", 0.7),
        "top_p": sampling.get("top_p", 0.95),
        "max_tokens": sampling.get("max_tokens", 512),
    }
    api_model = str(getattr(router.engine, "api_model", "") or "").strip()
    if api_model:
        payload["model"] = api_model
    # Provider tools (OpenAI tools array) are preferred over JSON grammar.
    if not tools and json_mode:
        # Force valid JSON output (llama.cpp applies a JSON grammar).
        payload["response_format"] = {"type": "json_object"}
    return payload


def _record_local_terminal(router, telemetry, status: str) -> None:
    """Persist one local attempt outcome without affecting the inference path."""
    if not isinstance(telemetry, dict):
        return
    try:
        router._record_usage_outcome(
            "local",
            router.model_name or telemetry.get("model") or "local model",
            status=status,
            latency_ms=float(telemetry.get("time_to_last_token_s") or 0) * 1000.0,
        )
    except Exception:
        pass


async def call_local_inner(router, messages: list, sampling: dict, json_mode: bool = False,
                           image_b64=None, reasoning_budget: int = None,
                           reasoning_sink=None, tools: list = None, tool_call_sink=None,
                           stream_diagnostics=None, prompt_cache_identity=None):
    """Stream tokens from the selected local OpenAI-compatible endpoint.

    If llama-server has died since boot, restart once and retry the request.
    """
    await _ensure_local_ready(router)
    source_messages = messages
    messages = render_local_messages(source_messages, image_b64)
    payload = _build_local_payload(
        router, messages, sampling, json_mode, reasoning_budget, tools)
    request_headers = dict(getattr(router.engine, "request_headers", {}) or {})
    raw_cache_enabled = getattr(router.engine, "cache_prompt", None)
    prompt_cache = apply_prompt_cache_identity(
        prompt_cache_identity,
        payload=payload,
        headers=request_headers,
        body_field=str(
            getattr(router.engine, "prompt_cache_body_field", "") or ""
        ),
        header_name=str(
            getattr(router.engine, "prompt_cache_header", "") or ""
        ),
        fallback_application="local_server_prefix_cache",
        cache_enabled=(
            bool(raw_cache_enabled) if raw_cache_enabled is not None else None
        ),
    )

    last_error: BaseException | None = None
    observable_delta_delivered = False
    managed = bool(getattr(router.engine, "managed", True))
    attempts = 2 if managed else 1
    for attempt in range(attempts):
        url = f"{router.engine.base_url}/v1/chat/completions"
        telemetry_id, telemetry = router._inference.start(router.model_name)
        await router._publish_inference(telemetry, force=True)
        telemetry_terminal = False
        final_timings = {}
        final_usage = {}
        response_identity = {}
        try:
            event_hooks = model_request_event_hooks(
                router,
                provider="local",
                api_style="openai",
                transport="chat_completions",
                adapter=str(getattr(
                    router.engine, "request_adapter", "llama_cpp.chat_completions")),
                adapter_version="2",
                model=router.model_name or "",
                payload=payload,
                source_messages=source_messages,
                source_tools=tools,
                requested_images=image_b64,
                endpoint_path="/v1/chat/completions",
                retry_kind="local_restart_retry" if attempt else "",
                prompt_cache=prompt_cache,
                context_limit_tokens=int(
                    getattr(router.engine, "ctx_size", 0) or 0),
            )
            async with httpx.AsyncClient(
                    timeout=STREAM_TIMEOUT, trust_env=False,
                    event_hooks=event_hooks) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=request_headers,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise LocalEngineError(
                            f"{getattr(router.engine, 'display_name', 'local runtime')} "
                            f"{resp.status_code}: {body[:200]!r}"
                        )
                    had_content = False
                    decoder = OpenAIChatSSEDecoder()
                    reasoning = []
                    async for line in resp.aiter_lines():
                        event = decoder.decode_line(line)
                        if event is None:
                            continue
                        if event.done:
                            break
                        obj = event.payload or {}
                        embedded_error = obj.get("error")
                        if embedded_error:
                            detail = (
                                embedded_error.get("message")
                                if isinstance(embedded_error, dict)
                                else str(embedded_error)
                            )
                            raise LocalEngineError(
                                f"local runtime stream error: {detail}"
                            )
                        response_identity.update(provider_response_identity(obj))
                        if isinstance(obj.get("timings"), dict):
                            final_timings = obj["timings"]
                        if isinstance(obj.get("usage"), dict):
                            final_usage = obj["usage"]
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason")
                        if stream_diagnostics is not None:
                            stream_diagnostics.note_finish_reason(
                                finish_reason)
                        delta = choice.get("delta") or {}
                        tok = delta.get("content")
                        rtok = (
                            delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or delta.get("thinking")
                        )
                        if tool_call_sink is not None and delta.get("tool_calls"):
                            # A sink may mutate before raising, so crossing this
                            # boundary makes replay unsafe even if it then fails.
                            observable_delta_delivered = True
                            try:
                                tool_call_sink.add_openai_delta(delta.get("tool_calls"))
                            except Exception:
                                pass
                        if rtok:
                            reasoning.append(rtok)
                            if reasoning_sink is not None:
                                observable_delta_delivered = True
                                try:
                                    reasoning_sink(rtok)
                                except Exception:
                                    pass
                        if rtok or tok:
                            token_snapshot = router._inference.token(telemetry_id, rtok or tok)
                            await router._publish_inference(
                                token_snapshot,
                                force=bool(token_snapshot and token_snapshot.get("output_tokens") == 1),
                            )
                        if tok:
                            had_content = True
                            observable_delta_delivered = True
                            yield tok
                    decoder.require_terminal(
                        "local stream", error_factory=LocalEngineError,
                    )
                    if not had_content and reasoning and reasoning_sink is None:
                        # Legacy: a thinking model spent the turn reasoning and emitted
                        # no content — surface the reasoning so the reply isn't empty.
                        # With a reasoning_sink the caller handles reasoning itself, so
                        # we keep it OUT of the visible reply.
                        observable_delta_delivered = True
                        yield "".join(reasoning)
            telemetry = router._inference.complete(
                telemetry_id, timings=final_timings, usage=final_usage)
            telemetry_terminal = True
            await router._publish_inference(telemetry, force=True)
            patch_provider_response_identity(
                router, event_hooks.request_ref, response_identity,
            )
            if telemetry:
                router._record_usage(
                    "local",
                    telemetry.get("prompt_tokens", 0),
                    telemetry.get("output_tokens", 0),
                    model=router.model_name or telemetry.get("model", "local model"),
                    raw_usage=final_usage,
                    inference_time_s=telemetry.get("time_to_last_token_s"),
                    latency_ms=float(telemetry.get("time_to_last_token_s") or 0) * 1000.0,
                    ttft_ms=telemetry.get("ttft_ms"),
                    prefill_tps=telemetry.get("prompt_tps"),
                    generation_tps=telemetry.get("decode_tps"),
                    manifest_ref=event_hooks.request_ref,
                )
            return
        except asyncio.CancelledError:
            telemetry = router._inference.fail(telemetry_id, status="cancelled")
            telemetry_terminal = True
            await router._publish_inference(telemetry, force=True)
            _record_local_terminal(router, telemetry, "cancelled")
            raise
        except httpx.HTTPError as e:
            last_error = e
            telemetry = router._inference.fail(telemetry_id, status="error", error=str(e))
            telemetry_terminal = True
            await router._publish_inference(telemetry, force=True)
            _record_local_terminal(router, telemetry, "error")
            poll = getattr(router.engine, "poll_process", None)
            if callable(poll):
                poll()
            if _is_unreachable_local(e):
                router.engine.ready = False
            # Port dead while ready flag was stale — force restart once.
            if (
                _is_unreachable_local(e)
                and attempt == 0
                and managed
                and not observable_delta_delivered
            ):
                print(
                    f"[local-inference] request unreachable ({type(e).__name__}: {e}); "
                    "restarting local engine once",
                    flush=True,
                )
                try:
                    await _ensure_local_ready(router)
                    continue
                except Exception as restart_err:
                    raise LocalEngineError(
                        f"local request failed: {e}; restart also failed: {restart_err}"
                    ) from e
            raise LocalEngineError(f"local request failed: {e}") from e
        except LocalEngineError as e:
            if not telemetry_terminal:
                telemetry = router._inference.fail(
                    telemetry_id, status="error", error=str(e))
                telemetry_terminal = True
                await router._publish_inference(telemetry, force=True)
                _record_local_terminal(router, telemetry, "error")
            raise
        except Exception as e:
            telemetry = router._inference.fail(telemetry_id, status="error", error=str(e))
            telemetry_terminal = True
            await router._publish_inference(telemetry, force=True)
            _record_local_terminal(router, telemetry, "error")
            raise
        finally:
            if not telemetry_terminal:
                telemetry = router._inference.fail(telemetry_id, status="cancelled")
                await router._publish_inference(telemetry, force=True)
                _record_local_terminal(router, telemetry, "cancelled")

    if last_error is not None:
        raise LocalEngineError(f"local request failed: {last_error}") from last_error
    raise LocalEngineError("local engine not ready")
