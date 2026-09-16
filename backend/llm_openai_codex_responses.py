"""ChatGPT subscription inference through the OpenAI Codex Responses wire.

Authentication is resolved by :mod:`openai_codex_oauth`; this module owns only
the final request projection and SSE normalization.  VARIANT-1 remains the agent
loop and tool executor.
"""

from __future__ import annotations

from reasoning_summaries import ResponsesSummary

import time
import json
from typing import Any

import httpx

import openai_codex_oauth
from model_providers import ProviderRequestError
from model_runtime.context import context_limit_tokens as _context_limit_tokens
from model_runtime.message_graph import build_message_graph, render_openai_responses
from model_runtime.request_manifest import (
    model_request_event_hooks,
    patch_provider_response_identity,
    provider_response_identity,
)
from model_runtime.prompt_cache import apply_prompt_cache_identity
from model_runtime.request_policy import project_reasoning_policy
from model_runtime.responses_protocol import (
    ResponsesSSEDecoder,
    push_function_call,
    responses_event_type,
    responses_tools,
)


STREAM_TIMEOUT = httpx.Timeout(None, connect=20.0, read=300.0)
# Transport protection is independent of a model's token target. The private
# consumer endpoint rejects max_output_tokens; bytes are not a token counter.
MAX_RESPONSE_OUTPUT_BYTES = 16 * 1024 * 1024


def _reasoning_replay_item(item: dict) -> dict | None:
    """Retain only the opaque Responses reasoning fields needed after tools."""

    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return None
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        return None
    replay: dict[str, Any] = {
        "type": "reasoning",
        "encrypted_content": encrypted,
    }
    if item.get("id"):
        replay["id"] = str(item["id"])
    for field in ("content", "summary"):
        if isinstance(item.get(field), list):
            replay[field] = item[field]
    return replay


async def call_openai_codex_responses(
    router,
    messages: list,
    sampling: dict,
    key: str,
    *,
    base: str = openai_codex_oauth.CODEX_RESPONSES_BASE,
    model: str | None = None,
    json_mode: bool = False,
    image_b64=None,
    reasoning_sink=None,
    tools: list | None = None,
    tool_call_sink=None,
    profile=None,
    stream_diagnostics=None,
    prompt_cache_identity=None,
    reasoning_budget: int | None = None,
):
    """Stream one VARIANT-1-controlled turn through ChatGPT Codex OAuth."""

    from llm_cloud_stream import (
        _embedded_error_status,
        _embedded_retry_after_seconds,
        _response_retry_after_seconds,
    )

    profile = profile or router.provider_profile("openai-codex")
    model = model or router.get_cloud_model("openai-codex") or (
        profile.default_model if profile else "gpt-5.3-codex-spark"
    )
    graph = build_message_graph(messages, image_b64)
    instructions, input_items, _ = render_openai_responses(graph)

    payload: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "store": False,
        "stream": True,
    }
    public_summary = ResponsesSummary()
    if instructions:
        payload["instructions"] = instructions
    effort = project_reasoning_policy(
        router, profile, model, payload, reasoning_budget,
        effort_field="reasoning.effort", default_effort="medium",
    )
    if effort and effort not in {"none", "off"} and (reasoning_budget != 0 or tools):
        payload["reasoning"]["summary"] = "auto"
        # Codex reasoning models require the opaque reasoning item to be
        # replayed with a later function_call_output. It remains in VARIANT-1's
        # bounded in-memory provider-replay registry and is never persisted in
        # the transcript or request manifest.
        payload["include"] = ["reasoning.encrypted_content"]

    response_tools = responses_tools(tools)
    if response_tools:
        payload["tools"] = response_tools
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = True
    elif json_mode:
        payload["text"] = {"format": {"type": "json_object"}}

    # The consumer Codex endpoint rejects max_output_tokens even though the
    # public Responses API accepts it. Sampling.max_tokens remains a VARIANT-1
    # local/API-provider control and is deliberately absent on this wire.
    prepare_payload = getattr(router, "prepare_cloud_payload", None)
    if callable(prepare_payload):
        payload = prepare_payload(
            payload, provider="openai-codex", model=model
        )

    pinned_base = openai_codex_oauth.validate_codex_base_url(base)
    url = f"{pinned_base}/responses"
    account_id_getter = getattr(router, "oauth_account_id", None)
    account_id = (
        str(account_id_getter("openai-codex") or "")
        if callable(account_id_getter) else ""
    )
    headers = openai_codex_oauth.request_headers(
        key, account_id=account_id
    )
    prompt_cache = apply_prompt_cache_identity(
        prompt_cache_identity,
        payload=payload,
        headers=headers,
        body_field=str(getattr(profile, "prompt_cache_body_field", "") or ""),
        header_name=str(getattr(profile, "prompt_cache_header", "") or ""),
    )
    request_started = time.perf_counter()
    print(
        f"[cloud] openai-codex transport=responses model={model} "
        f"reasoning_effort={effort}",
        flush=True,
    )

    usage: dict[str, Any] = {}
    response_identity: dict[str, Any] = {}
    event_hooks = model_request_event_hooks(
        router,
        provider="openai-codex",
        api_style="openai",
        transport="responses",
        adapter="openai_codex.responses",
        adapter_version="2",
        model=model,
        payload=payload,
        source_messages=messages,
        source_tools=tools,
        requested_images=image_b64,
        endpoint_path="/backend-api/codex/responses",
        prompt_cache=prompt_cache,
        context_limit_tokens=_context_limit_tokens(
            router,
            {"mode": "cloud", "provider": "openai-codex", "model": model},
        ),
        output_budget={"application": "advisory_unapplied",
                       "requested_tokens": max(1, int(sampling.get("max_tokens", 512))),
                       "resource_limit_bytes": MAX_RESPONSE_OUTPUT_BYTES},
    )

    try:
        got_content = False
        output_chars = 0
        output_bytes = 0
        function_index = 0
        argument_bytes: dict[str, int] = {}
        function_aliases: dict[str, str] = {}
        emitted_functions: set[str] = set()

        def charge_output(size: int) -> None:
            nonlocal output_bytes
            if output_bytes + size > MAX_RESPONSE_OUTPUT_BYTES:
                if stream_diagnostics is not None:
                    stream_diagnostics.note_finish_reason("error:response_resource_limit")
                raise ProviderRequestError("openai-codex", "Codex response exceeded the transport output resource limit")
            output_bytes += size

        def charge_function(item: dict, event: dict, *, delta: str | None = None) -> None:
            aliases = [str(value) for value in (
                event.get("item_id"), item.get("id"), item.get("call_id"),
                event.get("call_id"),
                f"index:{event['output_index']}" if "output_index" in event else None,
            ) if value is not None and str(value)]
            identity = next((function_aliases[a] for a in aliases if a in function_aliases),
                            aliases[0] if aliases else "unidentified")
            for alias in aliases:
                function_aliases[alias] = identity
            prior = argument_bytes.get(identity, 0)
            if delta is not None:
                size = prior + len(str(delta).encode("utf-8"))
            else:
                arguments = item.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                size = max(prior, len(arguments.encode("utf-8")))
            additional = size - prior
            charge_output(additional)
            argument_bytes[identity] = size

        def emit_function(item: dict, event: dict) -> None:
            nonlocal function_index
            charge_function(item, event)
            identity = str(item.get("call_id") or item.get("id") or "")
            if not identity or not item.get("name") or identity in emitted_functions:
                return
            arguments = item.get("arguments")
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                if not isinstance(parsed, dict):
                    raise ValueError("arguments must be an object")
            except (ValueError, TypeError) as exc:
                raise ProviderRequestError("openai-codex", "Codex returned incomplete function arguments", status_code=502) from exc
            push_function_call(tool_call_sink, item, function_index,
                               responses_replay={"reasoning_items": list(reasoning_replay_items)})
            emitted_functions.add(identity)
            function_index += 1
        decoder = ResponsesSSEDecoder()
        reasoning_replay_items: list[dict] = []
        async with httpx.AsyncClient(
            timeout=STREAM_TIMEOUT, trust_env=False, event_hooks=event_hooks
        ) as client:
            async with client.stream(
                "POST", url, headers=headers, json=payload
            ) as response:
                try:
                    router._observe_cloud_response(
                        "openai-codex", model, response
                    )
                except Exception:
                    pass
                if response.status_code != 200:
                    body = await response.aread()
                    detail = body[:400].decode("utf-8", errors="replace")
                    raise ProviderRequestError(
                        "openai-codex",
                        f"OpenAI Codex Responses {response.status_code}: {detail}",
                        status_code=response.status_code,
                        retry_after_seconds=_response_retry_after_seconds(
                            response.headers
                        ),
                    )

                async for line in response.aiter_lines():
                    framed = decoder.decode_line(line)
                    if framed is None:
                        continue
                    if framed.done:
                        break
                    event = framed.payload or {}
                    response_identity.update(provider_response_identity(event))
                    event_type = responses_event_type(event)
                    charge_output(public_summary.observe(event))
                    await public_summary.progress(reasoning_sink)
                    if event_type.startswith("response.reasoning_summary_"):
                        continue

                    if event_type == "error":
                        raw = event.get("error") or event
                        error = raw if isinstance(raw, dict) else {
                            "message": str(raw)
                        }
                        raise ProviderRequestError(
                            "openai-codex",
                            f"OpenAI Codex Responses error: {error!r}",
                            status_code=_embedded_error_status(error) or 400,
                            retry_after_seconds=_embedded_retry_after_seconds(
                                error
                            ),
                        )

                    if "output_text.delta" in event_type:
                        delta = event.get("delta")
                        if delta is None and isinstance(event.get("text"), str):
                            delta = event.get("text")
                        if delta:
                            text = str(delta)
                            charge_output(len(text.encode('utf-8')))
                            got_content = True
                            output_chars += len(text)
                            yield text
                        continue

                    if (
                        "reasoning" in event_type
                        and "delta" in event_type
                    ):
                        delta = event.get("delta") or ""
                        if delta:
                            text = str(delta)
                            charge_output(len(text.encode('utf-8')))
                            try:
                                if text and reasoning_sink is not None:
                                    reasoning_sink(text)
                            except Exception:
                                pass
                        continue

                    if event_type.endswith("output_item.added"):
                        item = event.get("item") or {}
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            charge_function(item, event)
                        continue

                    if "function_call_arguments.delta" in event_type:
                        charge_function({}, event, delta=str(event.get("delta") or ""))
                        continue

                    if event_type.endswith("output_item.done"):
                        item = event.get("item") or {}
                        replay_item = _reasoning_replay_item(item)
                        if replay_item is not None:
                            reasoning_replay_items.append(replay_item)
                        elif (
                            isinstance(item, dict)
                            and str(item.get("type") or "") == "function_call"
                        ):
                            emit_function(item, event)
                        continue

                    if "function_call" in event_type and "delta" not in event_type:
                        item = event.get("item") or event
                        if isinstance(item, dict):
                            charge_function(item, event)
                        continue

                    if event_type in {
                        "response.completed",
                        "response.incomplete",
                        "response.failed",
                    }:
                        response_obj = event.get("response") or {}
                        response_identity.update(
                            provider_response_identity(response_obj)
                        )
                        if isinstance(response_obj, dict) and isinstance(
                            response_obj.get("usage"), dict
                        ):
                            usage = response_obj["usage"]
                        if event_type == "response.completed" and isinstance(response_obj, dict):
                            for index, item in enumerate(response_obj.get("output") or ()):
                                if isinstance(item, dict) and item.get("type") == "function_call":
                                    emit_function(item, {"output_index": index})
                        if stream_diagnostics is not None:
                            reason = event_type.removeprefix("response.")
                            if event_type == "response.incomplete":
                                details = (
                                    response_obj.get("incomplete_details")
                                    if isinstance(response_obj, dict)
                                    else None
                                ) or {}
                                if isinstance(details, dict) and details.get(
                                    "reason"
                                ):
                                    reason = f"{reason}:{details['reason']}"
                            stream_diagnostics.note_finish_reason(reason)
                        if event_type == "response.failed":
                            raw = (
                                response_obj.get("error")
                                if isinstance(response_obj, dict)
                                else None
                            ) or event.get("error")
                            error = raw if isinstance(raw, dict) else {
                                "message": str(raw or "response failed")
                            }
                            raise ProviderRequestError(
                                "openai-codex",
                                f"OpenAI Codex response failed: {error!r}",
                                status_code=_embedded_error_status(error) or 400,
                                retry_after_seconds=(
                                    _embedded_retry_after_seconds(error)
                                ),
                            )
                        break

                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]

                if not decoder.saw_terminal:
                    raise ProviderRequestError(
                        "openai-codex",
                        "OpenAI Codex stream ended without a terminal event",
                        status_code=503,
                    )
        # A tool-only turn legitimately has no visible text.
        public_summary.publish(reasoning_sink)
        if not got_content and function_index == 0:
            pass

        try:
            patch_provider_response_identity(
                router, event_hooks.request_ref, response_identity
            )
            prompt_tokens = usage.get("input_tokens") or usage.get(
                "prompt_tokens"
            ) or 0
            output_tokens = usage.get("output_tokens") or usage.get(
                "completion_tokens"
            ) or 0
            if not output_tokens and output_chars:
                output_tokens = output_chars // 4
            router._record_usage(
                "openai-codex",
                prompt_tokens,
                output_tokens,
                usage.get("total_tokens"),
                model=model,
                raw_usage=dict(usage) if usage else {},
                inference_time_s=time.perf_counter() - request_started,
                manifest_ref=event_hooks.request_ref,
            )
        except Exception:
            pass
    except ProviderRequestError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ProviderRequestError(
            "openai-codex",
            f"OpenAI Codex transport failed: {exc}",
            status_code=503,
        ) from exc
    except Exception as exc:
        raise ProviderRequestError(
            "openai-codex", f"OpenAI Codex request failed: {exc}"
        ) from exc


__all__ = ["call_openai_codex_responses"]
