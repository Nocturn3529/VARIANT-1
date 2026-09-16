"""xAI Subscription inference via OpenAI-compatible Responses API.

Personal SuperGrok / OAuth path (Hermes-aligned). API-key xAI traffic still
uses chat/completions in ``llm_cloud_stream.call_openai``.

POST ``{base}/responses`` with streamed SSE; map text + function_call events
into VARIANT-1's existing sinks.
"""

from __future__ import annotations

from reasoning_summaries import ResponsesSummary

import time
from typing import Any

import httpx

from model_runtime.message_graph import (
    build_message_graph,
    render_openai_responses,
)
from model_providers import ProviderRequestError
from model_runtime.request_manifest import (
    model_request_event_hooks,
    patch_provider_response_identity,
    provider_response_identity,
)
from model_runtime.prompt_cache import apply_prompt_cache_identity
from model_runtime.request_policy import project_reasoning_policy
from model_runtime.context import context_limit_tokens as _context_limit_tokens
from model_runtime.responses_protocol import (
    ResponsesSSEDecoder,
    push_function_call,
    responses_event_type,
    responses_tools,
)

STREAM_TIMEOUT = httpx.Timeout(None, connect=15.0, read=180.0)


async def call_xai_responses(
    router,
    messages: list,
    sampling: dict,
    key: str,
    *,
    base: str = "https://api.x.ai/v1",
    model: str = None,
    json_mode: bool = False,
    image_b64=None,
    reasoning_sink=None,
    tools: list = None,
    tool_call_sink=None,
    profile=None,
    stream_diagnostics=None,
    prompt_cache_identity=None,
    reasoning_budget: int | None = None,
):
    """Stream assistant text from xAI ``/v1/responses`` (OAuth subscription)."""
    from llm_cloud_stream import (
        _embedded_error_status,
        _embedded_retry_after_seconds,
        _response_retry_after_seconds,
    )

    requested_images = image_b64
    profile = profile or router.provider_profile("xai")
    model = model or router.get_cloud_model("xai") or (
        profile.default_model if profile else "grok-4.5")
    graph = build_message_graph(messages, image_b64)
    instructions, input_items, has_images = render_openai_responses(graph)

    payload: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": True,
        "max_output_tokens": int(sampling.get("max_tokens", 512)),
    }
    public_summary = ResponsesSummary()
    if has_images:
        # xAI documents image requests as non-storable. This also keeps image
        # bytes out of provider-side conversational state.
        payload["store"] = False
    if instructions:
        payload["instructions"] = instructions
    # Grok 4.5 Responses: reasoning.effort low|medium|high (default high at xAI;
    # VARIANT-1 defaults to low for snappier agent loops).
    effort = project_reasoning_policy(
        router, profile, model, payload, reasoning_budget,
        effort_field="reasoning.effort", default_effort="low",
    )
    temp = sampling.get("temperature")
    if temp is not None and not (profile and profile.omit_temperature):
        payload["temperature"] = float(temp)
    rtools = responses_tools(tools)
    if rtools:
        payload["tools"] = rtools
        payload["tool_choice"] = "auto"
    # json_mode is best-effort; Responses may ignore text.format on some models.
    if json_mode and not rtools:
        payload["text"] = {"format": {"type": "json_object"}}
    prepare_payload = getattr(router, "prepare_cloud_payload", None)
    if callable(prepare_payload):
        payload = prepare_payload(payload, provider="xai", model=model)

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    prompt_cache = apply_prompt_cache_identity(
        prompt_cache_identity,
        payload=payload,
        headers=headers,
        body_field=str(getattr(profile, "prompt_cache_body_field", "") or ""),
        header_name=str(getattr(profile, "prompt_cache_header", "") or ""),
    )

    b = (base or "https://api.x.ai/v1").rstrip("/")
    url = f"{b}/responses" if b.endswith("/v1") else f"{b}/v1/responses"
    request_started = time.perf_counter()
    print(f"[cloud] xai transport=responses model={model} reasoning_effort={effort}",
          flush=True)

    try:
        usage = {}
        response_identity = {}
        event_hooks = model_request_event_hooks(
            router,
            provider="xai",
            api_style="openai",
            transport="responses",
            adapter="xai.responses",
            adapter_version="2",
            model=model,
            payload=payload,
            source_messages=messages,
            source_tools=tools,
            requested_images=requested_images,
            endpoint_path="/v1/responses",
            prompt_cache=prompt_cache,
            context_limit_tokens=_context_limit_tokens(router, {
                "mode": "cloud", "provider": "xai", "model": model,
            }),
        )
        async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT, trust_env=False, event_hooks=event_hooks) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                try:
                    router._observe_cloud_response("xai", model, resp)
                except Exception:
                    pass
                if resp.status_code != 200:
                    body = await resp.aread()
                    print(f"[cloud] xai responses {resp.status_code}: {body[:300]!r}",
                          flush=True)
                    raise ProviderRequestError(
                        "xai",
                        f"xai responses {resp.status_code}: {body[:200]!r}",
                        status_code=resp.status_code,
                        retry_after_seconds=_response_retry_after_seconds(resp.headers),
                    )
                got_content = False
                out_chars = 0
                fc_index = 0
                decoder = ResponsesSSEDecoder()
                async for line in resp.aiter_lines():
                    framed = decoder.decode_line(line)
                    if framed is None:
                        continue
                    if framed.done:
                        break
                    obj = framed.payload or {}
                    response_identity.update(provider_response_identity(obj))
                    et = responses_event_type(obj)
                    if et == "error":
                        raw = obj.get("error") or obj
                        err = raw if isinstance(raw, dict) else {"message": str(raw)}
                        raise ProviderRequestError(
                            "xai",
                            f"xai responses error: {err!r}",
                            status_code=_embedded_error_status(err) or 400,
                            retry_after_seconds=_embedded_retry_after_seconds(err),
                        )

                    public_summary.observe(obj)
                    await public_summary.progress(reasoning_sink)
                    if et.startswith("response.reasoning_summary_"):
                        continue

                    # Text deltas
                    if "output_text.delta" in et or et.endswith("output_text.delta"):
                        delta = obj.get("delta")
                        if delta is None and isinstance(obj.get("text"), str):
                            delta = obj.get("text")
                        if delta:
                            got_content = True
                            out_chars += len(str(delta))
                            yield str(delta)
                        continue

                    # Reasoning deltas (optional)
                    if reasoning_sink is not None and "reasoning" in et and "delta" in et:
                        rd = obj.get("delta") or ""
                        if rd:
                            try:
                                reasoning_sink(str(rd))
                            except Exception:
                                pass
                        continue

                    # Completed output items (function calls)
                    if et == "response.output_item.done" or et.endswith("output_item.done"):
                        item = obj.get("item") or {}
                        if isinstance(item, dict) and str(item.get("type") or "") in (
                                "function_call", "function_call_output"):
                            if str(item.get("type")) == "function_call":
                                push_function_call(tool_call_sink, item, fc_index)
                                fc_index += 1
                        continue

                    # Some gateways embed function_call directly
                    if "function_call" in et and "delta" not in et:
                        item = obj.get("item") or obj
                        if isinstance(item, dict) and item.get("name"):
                            push_function_call(tool_call_sink, item, fc_index)
                            fc_index += 1
                        continue

                    if et in ("response.completed", "response.incomplete", "response.failed"):
                        resp_obj = obj.get("response") or {}
                        response_identity.update(
                            provider_response_identity(resp_obj)
                        )
                        if isinstance(resp_obj, dict) and isinstance(resp_obj.get("usage"), dict):
                            usage = resp_obj["usage"]
                        if stream_diagnostics is not None:
                            reason = et.removeprefix("response.")
                            if et == "response.incomplete" and isinstance(resp_obj, dict):
                                details = resp_obj.get("incomplete_details") or {}
                                if isinstance(details, dict) and details.get("reason"):
                                    reason = f"{reason}:{details['reason']}"
                            stream_diagnostics.note_finish_reason(reason)
                        if et == "response.failed":
                            raw = (resp_obj.get("error") if isinstance(resp_obj, dict)
                                   else None) or obj.get("error")
                            err = raw if isinstance(raw, dict) else {
                                "message": str(raw or "response failed")
                            }
                            raise ProviderRequestError(
                                "xai",
                                f"xai responses failed: {err!r}",
                                status_code=_embedded_error_status(err) or 400,
                                retry_after_seconds=_embedded_retry_after_seconds(err),
                            )
                        break

                    # Usage-only events
                    if isinstance(obj.get("usage"), dict):
                        usage = obj["usage"]

                if not decoder.saw_terminal:
                    raise ProviderRequestError(
                        "xai",
                        "xai responses stream ended without a terminal event",
                        status_code=503,
                    )
                if not got_content and fc_index == 0:
                    # Empty stream with 200 — surface softly
                    pass

        public_summary.publish(reasoning_sink)
        try:
            patch_provider_response_identity(
                router, event_hooks.request_ref, response_identity,
            )
            prompt = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
            comp = usage.get("output_tokens") or usage.get("completion_tokens") or 0
            if not comp and out_chars:
                comp = out_chars // 4
            router._record_usage(
                "xai", prompt, comp, usage.get("total_tokens"),
                model=model, raw_usage=dict(usage) if usage else {},
                inference_time_s=time.perf_counter() - request_started,
                manifest_ref=event_hooks.request_ref,
            )
        except Exception:
            pass
    except ProviderRequestError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderRequestError(
            "xai", f"xai responses transport failed: {e}", status_code=503,
        ) from e
    except Exception as e:
        raise ProviderRequestError("xai", f"xai responses request failed: {e}") from e
