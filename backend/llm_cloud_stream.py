"""Cloud provider streaming + image attachment helpers for LLMRouter.

Per-provider HTTP stream parsing and multi-provider fallback orchestration live
here. ``LLMRouter.stream`` stays a thin mode-selection facade.
"""

from __future__ import annotations

from contextlib import aclosing
from reasoning_summaries import ReasoningBuffer

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import time

import httpx

from model_runtime.llama_server import LocalEngineError
from model_providers import CredentialLease, ProviderRequestError
from model_runtime.message_graph import (
    build_message_graph,
    render_anthropic_messages,
    render_gemini_generate_content,
    render_openai_chat,
)
from model_runtime.request_manifest import (
    model_request_event_hooks,
    patch_provider_response_identity,
    provider_response_identity,
)
from model_runtime.prompt_cache import apply_prompt_cache_identity
from model_runtime.openai_sse import OpenAIChatSSEDecoder
from model_runtime.context import context_limit_tokens as _context_limit_tokens
from model_runtime.request_policy import (
    effective_reasoning_budget,
    project_reasoning_policy,
    resolve_cloud_request_policy,
)
from llm_stream_diagnostics import StreamDiagnostics

STREAM_TIMEOUT = httpx.Timeout(None, connect=15.0, read=180.0)
CLOUD_ROUTE_MAX_ATTEMPTS = 4
CLOUD_RETRY_BASE_SECONDS = 2.0
CLOUD_MAX_SERVER_RETRY_SECONDS = 60.0
NON_RETRYABLE_LIMIT_MARKERS = (
    "usage_limit_reached",
    "gousagelimiterror",
    "freeusagelimiterror",
    "monthly usage limit reached",
    "available balance",
    "insufficient_quota",
    "out of budget",
    "quota exceeded",
    "billing",
)


class BufferedToolCallSink:
    """Hold one provider attempt's native-call fragments until it succeeds.

    Cloud provider and credential failover must never share a mutable tool-call
    accumulator.  Replaying these exact sink operations after a successful
    attempt preserves provider-native IDs and replay metadata without exposing
    a failed attempt to the agent loop.
    """

    def __init__(self):
        self._events: list[tuple[str, tuple, dict]] = []
        self._responses_seen_call_ids: set[str] = set()

    def _record(self, method: str, *args, **kwargs) -> None:
        self._events.append((method, deepcopy(args), deepcopy(kwargs)))

    def add_openai_delta(self, tool_calls) -> None:
        self._record("add_openai_delta", tool_calls)

    def anthropic_block_start(self, index: int, block: dict) -> None:
        self._record("anthropic_block_start", index, block)

    def anthropic_input_json_delta(self, index: int, partial_json: str) -> None:
        self._record("anthropic_input_json_delta", index, partial_json)

    def anthropic_block_stop(self, index: int) -> None:
        self._record("anthropic_block_stop", index)

    def add_gemini_function_call(self, call_or_name, args=None, **kwargs) -> None:
        self._record("add_gemini_function_call", call_or_name, args, **kwargs)

    def commit(self, sink) -> None:
        if sink is None:
            return
        for method, args, kwargs in self._events:
            getattr(sink, method)(*args, **kwargs)


def _cached_prompt_tokens(usage: dict) -> int:
    """Extract provider-reported cached prompt tokens from an OpenAI-style usage dict."""
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        try:
            return max(0, int(details.get("cached_tokens") or 0))
        except (TypeError, ValueError):
            pass
    try:
        return max(0, int(usage.get("cached_tokens") or 0))
    except (TypeError, ValueError):
        return 0


def _response_retry_after_seconds(headers) -> float | None:
    """Parse HTTP Retry-After seconds/date without permitting unbounded waits."""

    try:
        raw = headers.get("retry-after")
    except Exception:
        raw = None
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _embedded_retry_after_seconds(error: dict) -> float | None:
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    raw = (
        error.get("retry_after")
        or error.get("retry_after_seconds")
        or metadata.get("retry_after")
        or metadata.get("retry_after_seconds")
    )
    try:
        return None if raw is None else max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _embedded_error_status(error: dict) -> int:
    raw = error.get("status_code") or error.get("status") or error.get("code")
    try:
        code = int(raw or 0)
    except (TypeError, ValueError):
        code = 0
    if code:
        return code
    kind = " ".join((
        str(error.get("type") or ""),
        str(error.get("code") or ""),
        str((error.get("metadata") or {}).get("error_type") or "")
        if isinstance(error.get("metadata"), dict) else "",
    )).casefold()
    if (
        "rate_limit" in kind
        or "too_many_requests" in kind
        or "usage_limit_reached" in kind
    ):
        return 429
    if "service_unavailable" in kind or "temporarily_unavailable" in kind:
        return 503
    return 0


def _provider_stream_error(label: str, obj) -> ProviderRequestError | None:
    """Convert an HTTP-200 provider error envelope into a typed failure."""

    if not isinstance(obj, dict) or not obj.get("error"):
        return None
    raw = obj.get("error")
    error = raw if isinstance(raw, dict) else {"message": str(raw)}
    status = _embedded_error_status(error)
    message = str(error.get("message") or error.get("type") or raw)[:300]
    failure = ProviderRequestError(
        label,
        f"{label} stream error{f' {status}' if status else ''}: {message}",
        status_code=status,
        retry_after_seconds=_embedded_retry_after_seconds(error),
    )
    metadata = (
        error.get("metadata")
        if isinstance(error.get("metadata"), dict)
        else {}
    )
    failure.provider_error_type = str(
        error.get("type")
        or error.get("code")
        or metadata.get("error_type")
        or ""
    )
    return failure


def _cloud_retry_delay(
    exc: LocalEngineError,
    failed_attempt: int,
) -> float | None:
    explicit = getattr(exc, "retry_after_seconds", None)
    if explicit is not None:
        requested = max(0.0, float(explicit))
        if requested > CLOUD_MAX_SERVER_RETRY_SECONDS:
            return None
        return requested
    return CLOUD_RETRY_BASE_SECONDS * (2 ** max(0, int(failed_attempt)))


def _credential_failure_detail(exc: LocalEngineError) -> str:
    detail = str(exc)
    retry_after = getattr(exc, "retry_after_seconds", None)
    if retry_after is not None:
        detail += f"; retry-after: {max(1, math.ceil(float(retry_after)))}"
    return detail


def _is_transient_provider_error(exc: LocalEngineError) -> bool:
    detail = " ".join((
        str(exc),
        str(getattr(exc, "provider_error_type", "") or ""),
    )).casefold()
    if any(marker in detail for marker in NON_RETRYABLE_LIMIT_MARKERS):
        return False
    status = int(getattr(exc, "status_code", 0) or 0)
    return status in {408, 409, 429} or status >= 500


async def call_anthropic(router, messages, sampling, key, image_b64=None,
                         json_mode: bool = False, reasoning_sink=None,
                         profile=None, model: str = None, base: str = None,
                         tools: list = None, tool_call_sink=None,
                         stream_diagnostics=None, prompt_cache_identity=None,
                         reasoning_budget: int | None = None):
    profile = profile or router.provider_profile("anthropic")
    label = profile.name if profile else "anthropic"
    model = model or router.get_cloud_model(label) or "claude-sonnet-4-6"
    graph = build_message_graph(messages, image_b64)
    system, conv = render_anthropic_messages(graph)
    request_policy = resolve_cloud_request_policy(profile, model, sampling)
    payload = {
        "model": model,
        "max_tokens": int(sampling.get("max_tokens", 512)),
        "messages": conv,
        "stream": True,
    }
    payload.update(request_policy.sampling)
    if tools:
        import tool_calling
        payload["tools"] = tool_calling.to_anthropic_tools(tools)
    if json_mode:
        if request_policy.structured_output_style != "anthropic_json_schema":
            raise ProviderRequestError(
                label,
                f"{label} does not declare a structured JSON output wire",
                status_code=400,
            )
        payload["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "additionalProperties": True,
                },
            }
        }
    if profile:
        payload.update(dict(profile.request_defaults or {}))
    if system:
        payload["system"] = system
    project_reasoning_policy(router, profile, model, payload, reasoning_budget)
    prepare_payload = getattr(router, "prepare_cloud_payload", None)
    if callable(prepare_payload):
        payload = prepare_payload(payload, provider=label, model=model)
    headers = router._provider_headers(profile, key) if profile else {"x-api-key": key}
    headers.update({"anthropic-version": "2023-06-01", "content-type": "application/json"})
    prompt_cache = apply_prompt_cache_identity(
        prompt_cache_identity,
        payload=payload,
        headers=headers,
        body_field=str(getattr(profile, "prompt_cache_body_field", "") or ""),
        header_name=str(getattr(profile, "prompt_cache_header", "") or ""),
    )
    base = base or (profile.base_url if profile else "https://api.anthropic.com")
    url = base.rstrip("/") + "/v1/messages"
    request_started = time.perf_counter()
    try:
        usage = {}
        saw_terminal = False
        block_index = -1
        event_hooks = model_request_event_hooks(
            router,
            provider=label,
            api_style="anthropic",
            transport="messages",
            adapter="anthropic.messages",
            adapter_version="2",
            model=model,
            payload=payload,
            source_messages=messages,
            source_tools=tools,
            requested_images=image_b64,
            endpoint_path="/v1/messages",
            prompt_cache=prompt_cache,
            context_limit_tokens=_context_limit_tokens(router, {
                "mode": "cloud", "provider": label, "model": model,
            }),
        )
        async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT, trust_env=False, event_hooks=event_hooks) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                router._observe_cloud_response(label, model, resp)
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise ProviderRequestError(
                        label, f"{label} {resp.status_code}: {body[:200]!r}",
                        status_code=resp.status_code,
                        retry_after_seconds=_response_retry_after_seconds(resp.headers),
                    )
                saw_terminal = False
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    if data == "[DONE]":
                        saw_terminal = True
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    stream_error = _provider_stream_error(label, obj)
                    if stream_error is not None:
                        raise stream_error
                    etype = obj.get("type")
                    if etype == "message_start":
                        u = obj.get("message", {}).get("usage") or {}
                        if isinstance(u, dict):
                            usage.update(u)
                    elif etype == "message_delta":
                        if stream_diagnostics is not None:
                            stream_diagnostics.note_finish_reason(
                                (obj.get("delta") or {}).get("stop_reason"))
                        if (obj.get("delta") or {}).get("stop_reason"):
                            saw_terminal = True
                        u = obj.get("usage") or {}
                        if isinstance(u, dict):
                            usage.update(u)
                    elif etype == "content_block_start":
                        block_index = int(obj.get("index", block_index + 1))
                        block = obj.get("content_block") or {}
                        if tool_call_sink is not None and block.get("type") == "tool_use":
                            try:
                                tool_call_sink.anthropic_block_start(block_index, block)
                            except Exception:
                                pass
                    elif etype == "content_block_delta":
                        d = obj.get("delta") or {}
                        idx = int(obj.get("index", block_index))
                        if d.get("type") == "text_delta" and d.get("text"):
                            yield d["text"]
                        elif (d.get("type") == "thinking_delta" and d.get("thinking")
                              and reasoning_sink is not None):
                            try:
                                reasoning_sink(d["thinking"])
                            except Exception:
                                pass
                        elif (d.get("type") == "input_json_delta"
                              and tool_call_sink is not None):
                            try:
                                tool_call_sink.anthropic_input_json_delta(
                                    idx, d.get("partial_json") or "")
                            except Exception:
                                pass
                    elif etype == "content_block_stop" and tool_call_sink is not None:
                        try:
                            tool_call_sink.anthropic_block_stop(
                                int(obj.get("index", block_index)))
                        except Exception:
                            pass
                    elif etype == "message_stop":
                        saw_terminal = True
                if not saw_terminal:
                    raise ProviderRequestError(
                        label,
                        f"{label} stream ended without a terminal event",
                        status_code=503,
                    )
        router._record_usage(
            label,
            usage.get("input_tokens") or 0,
            usage.get("output_tokens") or 0,
            model=model,
            raw_usage=usage,
            inference_time_s=time.perf_counter() - request_started,
            manifest_ref=event_hooks.request_ref,
        )
    except LocalEngineError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderRequestError(
            label, f"{label} transport failed: {e}", status_code=503,
        ) from e
    except Exception as e:
        raise ProviderRequestError(label, f"{label} request failed: {e}") from e

async def call_openai(router, messages, sampling, key, json_mode=False, image_b64=None,
                      base: str = None, model_key: str = "openai_model", label: str = "openai",
                      extra_body: dict = None, reasoning_sink=None,
                      profile=None, model: str = None,
                      tools: list = None, tool_call_sink=None,
                      stream_diagnostics=None, prompt_cache_identity=None,
                      reasoning_budget: int | None = None):
    """OpenAI-compatible caller (used by OpenAI, xAI, and NVIDIA NIM).

    The router supplies one provider-neutral durable-chat cache identity.
    Profiles declaratively project it when this compatible endpoint exposes a
    native request field/header; otherwise native prefix caching remains in use.

    When ``tools`` (VARIANT-1 specs) is provided, they are converted to OpenAI
    function tools and streamed tool_call deltas are pushed to ``tool_call_sink``.
    """
    profile = profile or router.provider_profile(label)
    default_model = {"xai_model": "grok-4.3",
                     "nvidia_model": "nvidia/nemotron-3-ultra-550b-a55b"}.get(model_key, "gpt-4o")
    model = model or (router.cfg.get("cloud", {}) or {}).get(model_key) or \
        (profile.default_model if profile else default_model)
    source_messages = messages
    messages = render_openai_chat(
        build_message_graph(source_messages, image_b64))
    request_policy = resolve_cloud_request_policy(profile, model, sampling)
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    payload.update(request_policy.sampling)
    payload[request_policy.completion_token_field] = int(
        sampling.get("max_tokens", 512)
    )
    if label != "nvidia":
        payload["stream_options"] = {"include_usage": True}
    # Provider tools and json_object response_format conflict on many providers.
    if tools:
        import tool_calling
        payload["tools"] = tool_calling.to_openai_tools(tools)
        payload["tool_choice"] = "auto"
    elif json_mode:
        payload["response_format"] = {"type": "json_object"}
    if extra_body:
        payload.update(extra_body)   # provider-specific knobs (e.g. NVIDIA thinking)
    if profile:
        payload.update(dict(profile.request_defaults or {}))
    project_reasoning_policy(router, profile, model, payload, reasoning_budget)
    prepare_payload = getattr(router, "prepare_cloud_payload", None)
    if callable(prepare_payload):
        payload = prepare_payload(payload, provider=label, model=model)
    headers = router._provider_headers(profile, key) if profile else {
        "Authorization": f"Bearer {key}"}
    headers["content-type"] = "application/json"
    prompt_cache = apply_prompt_cache_identity(
        prompt_cache_identity,
        payload=payload,
        headers=headers,
        body_field=str(getattr(profile, "prompt_cache_body_field", "") or ""),
        header_name=str(getattr(profile, "prompt_cache_header", "") or ""),
    )
    # Be /v1-aware: a base that already ends in /v1 (xAI, NVIDIA) must not get a
    # second /v1 appended (which would 404).
    b = (base or (profile.base_url if profile else "https://api.openai.com/v1")).rstrip("/")
    url = router._openai_url(b, "chat/completions")
    request_started = time.perf_counter()
    try:
        usage = {}
        response_identity = {}
        event_hooks = model_request_event_hooks(
            router,
            provider=label,
            api_style="openai",
            transport="chat_completions",
            adapter="openai.chat_completions",
            adapter_version="2",
            model=model,
            payload=payload,
            source_messages=source_messages,
            source_tools=tools,
            requested_images=image_b64,
            endpoint_path="/v1/chat/completions",
            prompt_cache=prompt_cache,
            context_limit_tokens=_context_limit_tokens(router, {
                "mode": "cloud", "provider": label, "model": model,
            }),
        )
        async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT, trust_env=False, event_hooks=event_hooks) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                router._observe_cloud_response(label, model, resp)
                if resp.status_code != 200:
                    body = await resp.aread()
                    # Surface the provider's reason in the log (not just the bubble)
                    # so failures like a bad model id / unsupported field are visible.
                    print(f"[cloud] {label} {resp.status_code}: {body[:300]!r}", flush=True)
                    raise ProviderRequestError(
                        label, f"{label} {resp.status_code}: {body[:200]!r}",
                        status_code=resp.status_code,
                        retry_after_seconds=_response_retry_after_seconds(resp.headers),
                    )
                got_content = False
                reasoning = []
                out_chars = 0
                decoder = OpenAIChatSSEDecoder()
                async for line in resp.aiter_lines():
                    event = decoder.decode_line(line)
                    if event is None:
                        continue
                    if event.done:
                        break
                    obj = event.payload or {}
                    stream_error = _provider_stream_error(label, obj)
                    if stream_error is not None:
                        raise stream_error
                    response_identity.update(provider_response_identity(obj))
                    if isinstance(obj.get("usage"), dict):
                        usage = obj["usage"]
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason")
                    if stream_diagnostics is not None:
                        stream_diagnostics.note_finish_reason(finish_reason)
                    delta = choice.get("delta") or {}
                    tok = delta.get("content")
                    if not tok and isinstance(delta.get("refusal"), str):
                        tok = delta.get("refusal")
                    if tok:
                        got_content = True
                        out_chars += len(tok)
                        yield tok
                    # Native tool-call deltas (OpenAI / xAI / compatible local).
                    if tool_call_sink is not None and delta.get("tool_calls"):
                        try:
                            tool_call_sink.add_openai_delta(delta.get("tool_calls"))
                        except Exception:
                            pass
                    # Compatible thinking models use `reasoning_content`,
                    # `reasoning`, or `thinking`. A sink keeps that channel
                    # separate from the visible reply.
                    rc = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or delta.get("thinking")
                    )
                    if rc:
                        if reasoning_sink is not None:
                            try:
                                reasoning_sink(rc)
                            except Exception:
                                pass
                        else:
                            reasoning.append(rc)
                decoder.require_terminal(
                    f"{label} stream",
                    error_factory=lambda message: ProviderRequestError(
                        label, message, status_code=503,
                    ),
                )
                if not got_content and reasoning and reasoning_sink is None:
                    joined = "".join(reasoning)
                    out_chars += len(joined)
                    yield joined
        # Some OpenAI-compatible servers (e.g. NVIDIA NIM) don't return a usage
        # chunk, so fall back to a rough completion-token estimate rather than
        # recording the call as 0 tokens.
        comp = usage.get("completion_tokens") or usage.get("output_tokens")
        if not comp and out_chars:
            comp = out_chars // 4
        raw_usage = dict(usage) if isinstance(usage, dict) else {}
        cached = _cached_prompt_tokens(raw_usage)
        if cached:
            raw_usage["cached_tokens"] = cached
        patch_provider_response_identity(
            router, event_hooks.request_ref, response_identity,
        )
        router._record_usage(
            label,
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
            comp or 0,
            usage.get("total_tokens"),
            model=model,
            raw_usage=raw_usage,
            inference_time_s=time.perf_counter() - request_started,
            manifest_ref=event_hooks.request_ref,
        )
    except LocalEngineError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderRequestError(
            label, f"{label} transport failed: {e}", status_code=503,
        ) from e
    except Exception as e:
        raise ProviderRequestError(label, f"{label} request failed: {e}") from e

def gemini_extract_text(obj: dict):
    """Pull any text deltas out of one parsed Gemini stream chunk."""
    out = []
    for candidate in (obj or {}).get("candidates", []):
        content = candidate.get("content", {}) or {}
        for part in content.get("parts", []) or []:
            if part.get("text") and not part.get("thought"):
                out.append(part["text"])
    return out


def gemini_extract_thoughts(obj: dict):
    """Pull provider reasoning text without exposing it as assistant content."""
    out = []
    for candidate in (obj or {}).get("candidates", []):
        content = candidate.get("content", {}) or {}
        for part in content.get("parts", []) or []:
            if part.get("text") and part.get("thought"):
                out.append(part["text"])
    return out


def gemini_extract_function_calls(obj: dict) -> list[dict]:
    """Pull function calls plus exact multi-turn replay metadata."""
    out = []
    for candidate_index, candidate in enumerate(
        (obj or {}).get("candidates", [])
    ):
        content = candidate.get("content", {}) or {}
        for part_index, part in enumerate(content.get("parts", []) or []):
            fc = part.get("functionCall")
            if isinstance(fc, dict) and fc.get("name"):
                args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                gemini_replay: dict = {
                    "candidate_index": candidate_index,
                    "part_index": part_index,
                }
                if fc.get("id"):
                    gemini_replay["id"] = str(fc["id"])
                signature = part.get("thoughtSignature") or part.get(
                    "thought_signature")
                if signature:
                    gemini_replay["thought_signature"] = str(signature)
                out.append({
                    "id": str(fc.get("id") or ""),
                    "name": str(fc["name"]),
                    "args": args,
                    "provider_replay": {"gemini": gemini_replay},
                })
    return out


async def call_gemini(router, messages, sampling, key, json_mode=False, image_b64=None,
                      reasoning_sink=None, profile=None, model: str = None,
                      base: str = None, tools: list = None, tool_call_sink=None,
                      stream_diagnostics=None, prompt_cache_identity=None,
                      reasoning_budget: int | None = None):
    """Google Gemini (via Generative Language API)."""
    profile = profile or router.provider_profile("gemini")
    label = profile.name if profile else "gemini"
    model = model or router.get_cloud_model(label) or "gemini-2.0-flash"
    graph = build_message_graph(messages, image_b64)
    system_text, contents = render_gemini_generate_content(graph)

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": sampling.get("temperature", 0.7),
            "topP": sampling.get("top_p", 0.95),
            "maxOutputTokens": int(sampling.get("max_tokens", 512)),
        }
    }
    if system_text:
        payload["systemInstruction"] = {"parts": [{"text": system_text}]}
    if tools:
        import tool_calling
        payload["tools"] = tool_calling.to_gemini_tools(tools)
    elif json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    project_reasoning_policy(router, profile, model, payload, reasoning_budget)
    prepare_payload = getattr(router, "prepare_cloud_payload", None)
    if callable(prepare_payload):
        payload = prepare_payload(payload, provider=label, model=model)

    headers = {"Content-Type": "application/json"}
    prompt_cache = apply_prompt_cache_identity(
        prompt_cache_identity,
        payload=payload,
        headers=headers,
        body_field=str(getattr(profile, "prompt_cache_body_field", "") or ""),
        header_name=str(getattr(profile, "prompt_cache_header", "") or ""),
    )
    base = base or (profile.base_url if profile else
                    "https://generativelanguage.googleapis.com/v1beta")
    # alt=sse makes Gemini stream proper `data: {json}` SSE lines; without it
    # the endpoint returns a single chunked JSON *array*, which can't be parsed
    # line-by-line and yields no tokens.
    url = f"{base}/models/{model}:streamGenerateContent?alt=sse&key={key}"
    subscription = label == 'google-antigravity'
    if subscription:
        from model_runtime.google_ai import prepare_request
        url, headers, payload = prepare_request(payload,
            project_id=str(router._oauth_rec(label).get('project_id') or ''),
            model=model, access_token=key, cache_identity=prompt_cache_identity)

    request_started = time.perf_counter()
    try:
        usage = {}
        saw_terminal = False
        event_hooks = model_request_event_hooks(
            router,
            provider=label,
            api_style="gemini",
            transport="cloud_code_assist" if subscription else "generate_content",
            adapter="google_ai.generate_content" if subscription else "gemini.generate_content",
            adapter_version="2",
            model=model,
            payload=payload,
            source_messages=messages,
            source_tools=tools,
            requested_images=image_b64,
            endpoint_path="/v1internal:streamGenerateContent" if subscription else "/models/:model:streamGenerateContent",
            prompt_cache=prompt_cache,
            context_limit_tokens=_context_limit_tokens(router, {
                "mode": "cloud", "provider": label, "model": model,
            }),
        )
        async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT, trust_env=False, event_hooks=event_hooks) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                router._observe_cloud_response(label, model, resp)
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise ProviderRequestError(
                        label, f"{label} {resp.status_code}: {body[:200]!r}",
                        status_code=resp.status_code,
                        retry_after_seconds=_response_retry_after_seconds(resp.headers),
                    )
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if subscription:
                        from model_runtime.google_ai import unwrap_response
                        obj = unwrap_response(obj)
                    stream_error = _provider_stream_error(label, obj)
                    if stream_error is not None:
                        raise stream_error
                    if isinstance(obj.get("usageMetadata"), dict):
                        usage = obj["usageMetadata"]
                    if stream_diagnostics is not None:
                        for candidate in obj.get("candidates") or []:
                            if isinstance(candidate, dict):
                                finish_reason = candidate.get("finishReason")
                                stream_diagnostics.note_finish_reason(finish_reason)
                                if finish_reason:
                                    saw_terminal = True
                    else:
                        for candidate in obj.get("candidates") or []:
                            if (
                                isinstance(candidate, dict)
                                and candidate.get("finishReason")
                            ):
                                saw_terminal = True
                    for tok in gemini_extract_text(obj):
                        yield tok
                    if reasoning_sink is not None:
                        for thought in gemini_extract_thoughts(obj):
                            try:
                                reasoning_sink(thought)
                            except Exception:
                                pass
                    if tool_call_sink is not None:
                        for call in gemini_extract_function_calls(obj):
                            try:
                                tool_call_sink.add_gemini_function_call(call)
                            except Exception:
                                pass
                if not saw_terminal:
                    raise ProviderRequestError(
                        label,
                        f"{label} stream ended without a terminal candidate",
                        status_code=503,
                    )
        router._record_usage(
            label,
            usage.get("promptTokenCount") or 0,
            (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0),
            usage.get("totalTokenCount"),
            model=model,
            raw_usage=usage,
            inference_time_s=time.perf_counter() - request_started,
            manifest_ref=event_hooks.request_ref,
        )
    except LocalEngineError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderRequestError(
            label, f"{label} transport failed: {e}", status_code=503,
        ) from e
    except Exception as e:
        raise ProviderRequestError(label, f"{label} request failed: {e}") from e


# ── Multi-provider cloud orchestration (fallback chain + credentials) ─────────


async def call_cloud(router, messages: list, sampling: dict, json_mode: bool = False,
                     image_b64=None, reasoning_budget: int = None,
                     reasoning_sink=None, tools: list = None, tool_call_sink=None,
                     stream_diagnostics=None, internal_projection: bool = False,
                     prompt_cache_identity=None):
    """Walk provider routes with bounded, pre-output transient retries."""
    providers = [router.cloud_provider]
    bound_route = (
        router.bound_model_route()
        if callable(getattr(router, "bound_model_route", None))
        else None
    )
    # A durable chat route is an execution identity. Global Settings fallback
    # may help unbound auxiliary calls, but it must never silently replace a
    # chat-pinned provider/model without updating that durable route.
    route_is_bound = bool(
        isinstance(bound_route, dict)
        and str(bound_route.get("mode") or "") == "cloud"
        and str(bound_route.get("provider") or "").strip()
    )
    if not route_is_bound:
        providers.extend(
            p for p in router.get_fallback_chain() if p not in providers
        )
    failures = []
    retries_remaining = CLOUD_ROUTE_MAX_ATTEMPTS - 1
    retry_index = 0
    last_error: LocalEngineError | None = None
    for provider in providers:
        profile = router.provider_profile(provider)
        if not profile:
            failures.append(f"{provider}: unknown provider")
            continue
        # Internal vision-description calls need a route already known to be
        # multimodal.  User-facing turns are native-first instead: every shared
        # adapter projects the typed image and lets the selected model accept
        # it, because provider-level flags cannot accurately describe every
        # model served by OpenAI-compatible gateways.  A bounded text fallback
        # is applied by the chat loop only after a native request is rejected.
        if image_b64 and internal_projection and not profile.supports_vision:
            failures.append(f"{provider}: provider does not support vision")
            continue
        model = router.get_cloud_model(provider) or profile.default_model
        validate_request = getattr(router, "validate_model_request", None)
        if callable(validate_request):
            from model_runtime.context import model_route_support_coordinates

            support_adapter = model_route_support_coordinates(
                router,
                {"mode": "cloud", "provider": provider, "model": model},
            )["adapter"]
            # Qualification is a pre-request gate.  It must run before even an
            # OAuth refresh so an unsupported route produces no provider I/O.
            validate_request(
                provider=provider,
                model=model,
                adapter=support_adapter,
                tools=tools,
                internal_projection=internal_projection,
            )
        if provider == "ollama":
            from model_runtime.ollama_cloud import OllamaCloudError, ensure_cloud_model

            try:
                await ensure_cloud_model(model)
            except OllamaCloudError as exc:
                failures.append(f"{provider}: {exc}")
                continue
        if provider == "hermes":
            from model_runtime.hermes_proxy import HermesProxyError, ensure_proxy

            try:
                await ensure_proxy(model)
            except HermesProxyError as exc:
                failures.append(f"{provider}: {exc}")
                continue
        oauth_fresh = await router.ensure_oauth_fresh(provider)
        if router.oauth_required_for_route(provider) and not oauth_fresh:
            failures.append(f"{provider}: OAuth refresh did not produce a usable token")
            continue
        leases = router._credential_leases(provider)
        if not leases:
            failures.append(f"{provider}: no usable credential")
            continue
        for lease in leases:
            for route_attempt in range(CLOUD_ROUTE_MAX_ATTEMPTS):
                try:
                    from run_context import current_run_context

                    attempt_context = current_run_context()
                    if attempt_context is not None:
                        attempt_context.metadata["_provider_attempts"] = int(
                            attempt_context.metadata.get("_provider_attempts") or 0
                        ) + 1
                except Exception:
                    pass
                attempt_started = time.perf_counter()
                first_output_at = None
                print(
                    f"[cloud] using provider={provider} model={model} "
                    f"credential={lease.label} attempt={route_attempt + 1}",
                    flush=True,
                )
                yielded = False
                attempt_reasoning = ReasoningBuffer(summary_sink=reasoning_sink)
                summary_status = "discarded"
                buffered_tools = (
                    BufferedToolCallSink() if tool_call_sink is not None else None
                )
                attempt_diagnostics = StreamDiagnostics()

                if stream_diagnostics is not None:
                    stream_diagnostics.note_model(provider, model)
                try:
                    async with aclosing(call_cloud_once(
                            router, profile, lease, model, messages, sampling,
                            json_mode, image_b64, reasoning_budget,
                            attempt_reasoning if reasoning_sink is not None else None,
                            tools=tools, tool_call_sink=buffered_tools,
                            stream_diagnostics=attempt_diagnostics,
                            prompt_cache_identity=prompt_cache_identity)) as owned_stream:
                        async for token in owned_stream:
                            if first_output_at is None and token:
                                first_output_at = time.perf_counter()
                            yielded = True
                            yield token
                    if buffered_tools is not None:
                        buffered_tools.commit(tool_call_sink)
                    if reasoning_sink is not None:
                        attempt_reasoning.replay(reasoning_sink)
                    if stream_diagnostics is not None:
                        stream_diagnostics.note_model(provider, model)
                        stream_diagnostics.note_finish_reason(
                            attempt_diagnostics.finish_reason)
                    if first_output_at is not None:
                        try:
                            router._observe_usage_performance(
                                provider,
                                model,
                                ttft_ms=(first_output_at - attempt_started) * 1000.0,
                            )
                        except Exception:
                            pass
                    router.credential_pools.mark_success(lease)
                    summary_status = "done"
                    await attempt_reasoning.finish("done")
                    return
                except asyncio.CancelledError:
                    summary_status = "cancelled"
                    try:
                        router._record_usage_outcome(
                            provider,
                            model,
                            status="cancelled",
                            latency_ms=(time.perf_counter() - attempt_started) * 1000.0,
                        )
                    except Exception:
                        pass
                    raise
                except GeneratorExit:
                    summary_status = "cancelled"
                    raise
                except LocalEngineError as exc:
                    await attempt_reasoning.finish("discarded")
                    last_error = exc
                    from model_runtime.image_fallback import looks_like_image_rejection
                    if looks_like_image_rejection(exc):
                        # A rejected input modality is model/request evidence.
                        # Rotating a healthy credential cannot make it support pixels.
                        if isinstance(exc, ProviderRequestError):
                            exc.failure_kind = "unsupported_modality"
                            exc.model_output_observed = yielded
                        raise
                    try:
                        router._record_usage_outcome(
                            provider,
                            model,
                            status="error",
                            latency_ms=(time.perf_counter() - attempt_started) * 1000.0,
                        )
                    except Exception:
                        pass
                    # Visible text is streamed to the owning client and cannot be
                    # rolled back. Private reasoning and tool fragments remain
                    # attempt-local until success, so they are safe to discard.
                    if yielded:
                        if isinstance(exc, ProviderRequestError):
                            exc.model_output_observed = True
                        router.credential_pools.mark_failure(
                            lease,
                            status_code=getattr(exc, "status_code", 0),
                            detail=_credential_failure_detail(exc),
                        )
                        if (
                            getattr(lease, "source", "") in {"oauth", "codex_cli_oauth"}
                            and getattr(exc, "status_code", 0) in {401, 403}
                        ):
                            router.invalidate_oauth_access_token(provider)
                        raise
                    retry_delay = None
                    can_retry = (
                        route_attempt + 1 < CLOUD_ROUTE_MAX_ATTEMPTS
                        and retries_remaining > 0
                        and _is_transient_provider_error(exc)
                    )
                    if can_retry:
                        retry_delay = _cloud_retry_delay(exc, retry_index)
                    if retry_delay is not None:
                        retries_remaining -= 1
                        retry_index += 1
                        print(
                            f"[cloud] transient provider failure; retrying same "
                            f"route in {retry_delay:.2f}s: {exc}",
                            flush=True,
                        )
                        await asyncio.sleep(retry_delay)
                        continue
                    router.credential_pools.mark_failure(
                        lease,
                        status_code=getattr(exc, "status_code", 0),
                        detail=_credential_failure_detail(exc),
                    )
                    if (
                        getattr(lease, "source", "") in {"oauth", "codex_cli_oauth"}
                        and getattr(exc, "status_code", 0) in {401, 403}
                    ):
                        router.invalidate_oauth_access_token(provider)
                    failures.append(f"{provider}/{lease.label}: {exc}")
                    break
                finally:
                    if summary_status != "done":
                        await attempt_reasoning.finish(summary_status)
    detail = "; ".join(failures[-6:]) or "no configured provider could run"
    if (
        isinstance(last_error, ProviderRequestError)
        and _is_transient_provider_error(last_error)
        and not bool(last_error.model_output_observed)
    ):
        last_error.clean_turn_replay_safe = True
        raise last_error
    raise LocalEngineError(f"cloud fallback chain exhausted: {detail}")


async def call_cloud_once(router, profile, lease: CredentialLease, model: str,
                          messages: list, sampling: dict, json_mode: bool,
                          image_b64, reasoning_budget: int,
                          reasoning_sink=None, tools: list = None,
                          tool_call_sink=None, stream_diagnostics=None,
                          prompt_cache_identity=None):
    """One provider/credential attempt; dispatches by api_style."""
    base = router.provider_base_url(profile.name, lease)
    if not base:
        raise ProviderRequestError(
            profile.name, f"no base URL configured for {profile.name}")
    extra = None
    if profile.name == "nvidia" and router._thinking_enabled(reasoning_budget):
        thinking_cap = effective_reasoning_budget(
            router, sampling, reasoning_budget,
        )
        extra = {
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning_budget": thinking_cap,
        }
    if profile.api_style == "anthropic":
        generator = call_anthropic(
            router, messages, sampling, lease.secret, image_b64=image_b64,
            json_mode=json_mode, reasoning_sink=reasoning_sink,
            profile=profile, model=model, base=base,
            tools=tools, tool_call_sink=tool_call_sink,
            stream_diagnostics=stream_diagnostics,
            prompt_cache_identity=prompt_cache_identity,
            reasoning_budget=reasoning_budget)
    elif profile.api_style == "gemini":
        generator = call_gemini(
            router, messages, sampling, lease.secret, json_mode=json_mode,
            image_b64=image_b64, reasoning_sink=reasoning_sink,
            profile=profile, model=model, base=base,
            tools=tools, tool_call_sink=tool_call_sink,
            stream_diagnostics=stream_diagnostics,
            prompt_cache_identity=prompt_cache_identity,
            reasoning_budget=reasoning_budget)
    elif profile.api_style == "openai":
        if profile.name == "openai-codex":
            from llm_openai_codex_responses import (
                call_openai_codex_responses,
            )

            generator = call_openai_codex_responses(
                router, messages, sampling, lease.secret,
                base=base, model=model, json_mode=json_mode,
                image_b64=image_b64, reasoning_sink=reasoning_sink,
                tools=tools, tool_call_sink=tool_call_sink, profile=profile,
                stream_diagnostics=stream_diagnostics,
                prompt_cache_identity=prompt_cache_identity,
                reasoning_budget=reasoning_budget)
        # Personal SuperGrok OAuth → Hermes-aligned /v1/responses.
        elif profile.name == "xai" and getattr(lease, "source", "") == "oauth":
            from llm_xai_responses import call_xai_responses
            generator = call_xai_responses(
                router, messages, sampling, lease.secret,
                base=base, model=model, json_mode=json_mode,
                image_b64=image_b64, reasoning_sink=reasoning_sink,
                tools=tools, tool_call_sink=tool_call_sink, profile=profile,
                stream_diagnostics=stream_diagnostics,
                prompt_cache_identity=prompt_cache_identity,
                reasoning_budget=reasoning_budget)
        else:
            generator = call_openai(
                router, messages, sampling, lease.secret, json_mode=json_mode,
                image_b64=image_b64, base=base, model=model, label=profile.name,
                extra_body=extra, reasoning_sink=reasoning_sink, profile=profile,
                tools=tools, tool_call_sink=tool_call_sink,
                stream_diagnostics=stream_diagnostics,
                prompt_cache_identity=prompt_cache_identity,
                reasoning_budget=reasoning_budget)
    else:
        raise ProviderRequestError(
            profile.name, f"unsupported API style: {profile.api_style}")
    async with aclosing(generator) as owned_stream:
        async for token in owned_stream:
            yield token
