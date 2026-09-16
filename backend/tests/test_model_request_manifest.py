"""Structural and privacy contract for post-adapter model request receipts."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from model_runtime.request_manifest import (
    begin_model_call,
    build_model_request_manifest,
    end_model_call,
    model_request_event_hooks,
    provider_response_identity,
)
from llm_router import LLMRouter
from run_context import Variant1RunContext, bind_run_context


SECRET = "SENTINEL-secret-prompt-4f8c9b"
SECRET_ARGUMENT = "SENTINEL-tool-argument-90210"
SECRET_RESULT = "SENTINEL-tool-result-77119"
SECRET_DESCRIPTION = "SENTINEL-schema-description-33557"


def _png_b64(width: int = 2, height: int = 3) -> str:
    # The manifest reads only the PNG signature/IHDR dimensions.
    header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    return base64.b64encode(header).decode("ascii")


def _tools() -> list[dict]:
    return [{
        "name": "read_record",
        "description": SECRET_DESCRIPTION,
        "params": {
            "record_id": {
                "type": "string",
                "required": True,
                "desc": SECRET_DESCRIPTION,
            },
            "limit": {"type": "int", "required": False},
        },
    }]


def _source_messages() -> list[dict]:
    return [
        {"role": "system", "content": f"system {SECRET}"},
        {"role": "user", "content": f"user {SECRET}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_record",
                    "arguments": json.dumps({"record_id": SECRET_ARGUMENT}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": SECRET_RESULT,
        },
    ]


def _openai_payload(image_b64: str) -> dict:
    return {
        "model": "local-test",
        "messages": [
            {"role": "system", "content": f"system {SECRET}"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"user {SECRET}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_record",
                        "arguments": json.dumps({"record_id": SECRET_ARGUMENT}),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": SECRET_RESULT,
            },
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "read_record",
                "description": SECRET_DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "record_id": {
                            "type": "string",
                            "description": SECRET_DESCRIPTION,
                        },
                        "limit": {"type": "integer"},
                    },
                    "required": ["record_id"],
                },
            },
        }],
        "tool_choice": "auto",
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 256,
        "stream": True,
    }


def _manifest(**overrides) -> dict:
    image = overrides.pop("requested_images", _png_b64())
    payload = overrides.pop("payload", _openai_payload(image))
    args = {
        "provider": "local",
        "api_style": "openai",
        "transport": "chat_completions",
        "adapter": "llama_cpp.chat_completions",
        "adapter_version": "1",
        "model": "local-test",
        "payload": payload,
        "source_messages": _source_messages(),
        "source_tools": _tools(),
        "requested_images": image,
        "endpoint_path": "/v1/chat/completions",
        "context_limit_tokens": 8192,
        "wire_body_bytes": len(json.dumps(payload).encode()),
        **overrides,
    }
    return build_model_request_manifest(**args)


def test_manifest_is_metadata_only_and_counts_final_openai_payload():
    image = _png_b64()
    manifest = _manifest(requested_images=image)
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True)

    for secret in (
        SECRET,
        SECRET_ARGUMENT,
        SECRET_RESULT,
        SECRET_DESCRIPTION,
        image,
        "data:image/png;base64,",
    ):
        assert secret not in encoded

    assert manifest["messages"]["source"]["count"] == 4
    assert manifest["messages"]["rendered"]["count"] == 4
    assert manifest["messages"]["rendered"]["role_counts"] == {
        "system": 1, "user": 1, "assistant": 1, "tool": 1,
    }
    assert manifest["messages"]["rendered"]["tool_argument_chars"] > 0
    assert manifest["messages"]["rendered"]["tool_result_chars"] == len(SECRET_RESULT)
    assert manifest["messages"]["ordered_rendered"][2]["tool_names"] == ["read_record"]
    assert manifest["messages"]["ordered_rendered"][3]["tool_names"] == ["read_record"]
    assert manifest["tools"]["rendered_count"] == 1
    assert manifest["tools"]["rendered"][0] == {
        "name": "read_record",
        "argument_key_count": 2,
        "argument_keys": ["limit", "record_id"],
        "argument_keys_truncated_count": 0,
        "required_argument_count": 1,
        "required_arguments": ["record_id"],
        "required_arguments_truncated_count": 0,
    }
    assert manifest["prompt_cache"] == {
        "identity_available": False,
        "key_id": None,
        "scope": None,
        "source": None,
        "application": "unavailable",
        "native_key_sent": False,
        "cache_enabled": None,
    }
    assert len(manifest["tools"]["rendered_schema_sha256"]) == 64
    assert manifest["tools"]["rendered_schema_metrics"][0]["estimated_schema_tokens"] > 0
    assert manifest["images"]["requested_count"] == 1
    assert manifest["images"]["rendered_count"] == 1
    assert manifest["images"]["rendered"][0]["width"] == 2
    assert manifest["images"]["rendered"][0]["height"] == 3
    assert manifest["images"]["rendered"][0]["anchor"] == 1
    assert manifest["tool_protocol"]["valid"] is True
    assert manifest["budget"]["context_limit_tokens"] == 8192
    assert manifest["budget"]["remaining_margin_tokens"] > 0
    assert manifest["privacy"]["exact_payload_ref"] is None


def test_schema_hash_is_canonical_but_prompt_and_payload_hashes_are_omitted():
    first = _manifest()
    payload = _openai_payload(_png_b64())
    parameters = payload["tools"][0]["function"]["parameters"]
    payload["tools"][0]["function"]["parameters"] = {
        "required": parameters["required"],
        "properties": {
            "limit": parameters["properties"]["limit"],
            "record_id": parameters["properties"]["record_id"],
        },
        "type": parameters["type"],
    }
    second = _manifest(payload=payload)

    assert (
        first["tools"]["rendered_schema_sha256"]
        == second["tools"]["rendered_schema_sha256"]
    )
    encoded = json.dumps(first, sort_keys=True)
    assert "prompt_sha" not in encoded
    assert "payload_sha" not in encoded
    assert first["privacy"]["payload_hash_stored"] is False


def test_request_category_is_available_before_response_usage_arrives():
    from llm_usage import observe_usage_category

    with observe_usage_category("compaction"):
        summary = _manifest()
    agent = _manifest()
    assert summary["call_category"] == "compaction"
    assert agent["call_category"] == "agent"


def test_implicit_request_route_resolves_to_selected_local_route():
    token = begin_model_call(requested_route=None, selected_mode="local")
    try:
        local_attempt = _manifest(
            provider="local",
            api_style="openai",
            transport="chat_completions",
            adapter="llama_cpp.chat_completions",
            requested_images=None,
        )
    finally:
        end_model_call(token)
    assert local_attempt["route"]["requested"] == "local"
    assert local_attempt["route"]["selected_mode"] == "local"
    assert local_attempt["route"]["physical_mode"] == "local"
    assert local_attempt["route"]["model_revision"] == "revision_unavailable"
    assert local_attempt["route"]["system_fingerprint"] == "revision_unavailable"


def test_provider_response_identity_is_strictly_allowlisted():
    result = provider_response_identity({
        "model": "served-model-1",
        "model_version": "revision-7",
        "system_fingerprint": "fp_abc",
        "output": SECRET,
        "error": SECRET_RESULT,
    })

    assert result == {
        "provider_returned_model_id": "served-model-1",
        "model_revision": "revision-7",
        "system_fingerprint": "fp_abc",
    }
    assert SECRET not in json.dumps(result)
    assert SECRET_RESULT not in json.dumps(result)


def test_gemini_manifest_exposes_protocol_and_image_projection_loss():
    image = _png_b64()
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": f"user {SECRET}"}]},
            # This is the current lossy Gemini projection: tool history vanished.
            {"role": "model", "parts": [{"text": ""}]},
            {"role": "user", "parts": [{"text": SECRET_RESULT}]},
        ],
        "tools": [{
            "functionDeclarations": [{
                "name": "read_record",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"record_id": {"type": "STRING"}},
                    "required": ["record_id"],
                },
            }],
        }],
        "generationConfig": {"maxOutputTokens": 128},
    }
    manifest = _manifest(
        provider="gemini",
        api_style="gemini",
        transport="generate_content",
        adapter="gemini.generate_content",
        model="gemini-test",
        payload=payload,
        requested_images=image,
        endpoint_path="/models/:model:streamGenerateContent?key=TOP-SECRET",
        context_limit_tokens=0,
    )

    assert manifest["tool_protocol"]["source"]["calls"] == 1
    assert manifest["tool_protocol"]["source"]["results"] == 1
    assert manifest["tool_protocol"]["rendered"]["calls"] == 0
    assert manifest["tool_protocol"]["rendered"]["results"] == 0
    assert manifest["tool_protocol"]["call_loss"] is True
    assert manifest["tool_protocol"]["result_loss"] is True
    assert manifest["tool_protocol"]["valid"] is False
    assert manifest["images"]["requested_count"] == 1
    assert manifest["images"]["rendered_count"] == 0
    assert manifest["images"]["loss"] is True
    assert manifest["route"]["endpoint_path"] == "/models/:model:streamGenerateContent"
    assert "TOP-SECRET" not in json.dumps(manifest)
    assert any(t["kind"] == "tool_protocol_loss" for t in manifest["transforms"])


def test_xai_responses_manifest_exposes_synthetic_text_and_dropped_image():
    image = _png_b64()
    manifest = _manifest(
        provider="xai",
        api_style="openai",
        transport="responses",
        adapter="xai.responses",
        model="grok-test",
        payload={
            "model": "grok-test",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello"}],
            }],
            "stream": True,
            "max_output_tokens": 64,
        },
        source_messages=[],
        source_tools=[],
        requested_images=image,
        endpoint_path="/v1/responses",
    )

    assert manifest["messages"]["source"]["text_chars"] == 0
    assert manifest["messages"]["rendered"]["text_chars"] == 5
    assert manifest["images"]["loss"] is True
    assert {
        "kind": "content_size_changed",
        "char_delta": 5,
    } in manifest["transforms"]
    assert "Hello" not in json.dumps(manifest)


def test_source_embedded_image_loss_is_detected_without_screenshot_argument():
    image = _png_b64()
    source = [{
        "role": "user",
        "content": [
            {"type": "text", "text": SECRET},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image}"},
            },
        ],
    }]
    manifest = _manifest(
        payload={"messages": [{"role": "user", "content": SECRET}]},
        source_messages=source,
        source_tools=[],
        requested_images=None,
    )
    assert manifest["messages"]["source"]["images"] == 1
    assert manifest["images"]["requested_count"] == 1
    assert manifest["images"]["rendered_count"] == 0
    assert manifest["images"]["loss"] is True


def test_receipt_caps_detail_arrays_but_preserves_aggregate_counts():
    image = _png_b64()
    messages = [
        {"role": "user", "content": f"message {index}"}
        for index in range(140)
    ]
    messages[0] = {
        "role": "user",
        "content": [
            {"type": "text", "text": "images"},
            *[
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image}"},
                }
                for _ in range(20)
            ],
        ],
    }
    tools = [
        {
            "name": f"tool_{index}",
            "params": {"value": {"type": "string"}},
        }
        for index in range(140)
    ]
    payload_tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            },
        }
        for index in range(140)
    ]
    manifest = _manifest(
        payload={"messages": messages, "tools": payload_tools},
        source_messages=messages,
        source_tools=tools,
        requested_images=None,
    )

    assert manifest["messages"]["rendered"]["count"] == 140
    assert len(manifest["messages"]["ordered_rendered"]) == 128
    assert manifest["messages"]["ordered_rendered_truncated_count"] == 12
    window = manifest["messages"]["ordered_rendered_window"]
    assert window == {
        "policy": "head_tail",
        "head_count": 16,
        "tail_count": 112,
        "omitted_count": 12,
        "first_omitted_position": 16,
        "last_omitted_position": 27,
    }
    assert manifest["messages"]["ordered_rendered"][16]["position"] == 28
    assert manifest["messages"]["ordered_rendered"][-1]["position"] == 139
    assert manifest["tools"]["rendered_count"] == 140
    assert len(manifest["tools"]["rendered"]) == 128
    assert manifest["tools"]["rendered_truncated_count"] == 12
    assert manifest["images"]["rendered_count"] == 20
    assert len(manifest["images"]["rendered"]) == 16
    assert manifest["images"]["rendered_truncated_count"] == 4


def test_requested_required_arguments_match_converter_default():
    tools = [{
        "name": "optional_tool",
        "params": {
            "implicit_optional": {"type": "string"},
            "explicit_required": {"type": "string", "required": True},
        },
    }]
    payload = {
        "messages": [{"role": "user", "content": "test"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "optional_tool",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "implicit_optional": {"type": "string"},
                        "explicit_required": {"type": "string"},
                    },
                    "required": ["explicit_required"],
                },
            },
        }],
    }
    manifest = _manifest(
        payload=payload,
        source_messages=payload["messages"],
        source_tools=tools,
        requested_images=None,
    )
    assert manifest["tools"]["requested"][0]["required_arguments"] == [
        "explicit_required"]
    assert manifest["tools"]["requested"] == manifest["tools"]["rendered"]


def test_anthropic_blocks_preserve_tool_causality_and_image_anchor():
    image = _png_b64(7, 9)
    payload = {
        "model": "claude-test",
        "system": f"system {SECRET}",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"user {SECRET}"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image,
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_record",
                    "input": {"record_id": SECRET_ARGUMENT},
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": SECRET_RESULT,
                }],
            },
        ],
        "tools": [{
            "name": "read_record",
            "description": SECRET_DESCRIPTION,
            "input_schema": {
                "type": "object",
                "properties": {"record_id": {"type": "string"}},
                "required": ["record_id"],
            },
        }],
        "stream": True,
        "max_tokens": 128,
    }
    manifest = _manifest(
        provider="anthropic",
        api_style="anthropic",
        transport="messages",
        adapter="anthropic.messages",
        model="claude-test",
        payload=payload,
        requested_images=image,
        endpoint_path="/v1/messages",
    )

    assert manifest["tool_protocol"]["rendered"]["calls"] == 1
    assert manifest["tool_protocol"]["rendered"]["results"] == 1
    assert manifest["tool_protocol"]["valid"] is True
    assert manifest["images"]["rendered"][0]["width"] == 7
    assert manifest["images"]["rendered"][0]["height"] == 9


def test_manifest_records_only_opaque_prompt_cache_identity():
    owner = "raw-durable-chat-must-not-appear"
    from model_runtime.prompt_cache import (
        apply_prompt_cache_identity,
        resolve_prompt_cache_identity,
    )

    identity = resolve_prompt_cache_identity(owner)
    receipt = apply_prompt_cache_identity(
        identity,
        payload={},
        headers={},
        fallback_application="provider_prefix_cache",
    )
    manifest = _manifest(prompt_cache=receipt)
    encoded = json.dumps(manifest, sort_keys=True)

    assert owner not in encoded
    assert manifest["prompt_cache"]["key_id"] == identity.key
    assert manifest["prompt_cache"]["scope"] == "explicit"
    assert manifest["prompt_cache"]["native_key_sent"] is False


@pytest.mark.asyncio
async def test_httpx_hook_is_read_only_and_excludes_url_headers_and_credentials():
    payload = _openai_payload(_png_b64())
    captured_manifests = []
    captured_bodies = []

    class Router:
        async def _record_model_request_manifest(self, manifest):
            captured_manifests.append(manifest)

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(bytes(request.content))
        return httpx.Response(200, json={"ok": True})

    hooks = model_request_event_hooks(
        Router(),
        provider="gemini",
        api_style="openai",
        transport="chat_completions",
        adapter="test",
        adapter_version="1",
        model="test-model",
        payload=payload,
        source_messages=_source_messages(),
        source_tools=_tools(),
        requested_images=_png_b64(),
        endpoint_path="/safe?key=must-not-survive",
    )
    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(
        transport=transport, event_hooks=hooks,
    ) as client:
        await client.post(
            "https://example.invalid/safe?key=credential-secret",
            headers={"Authorization": "Bearer credential-secret"},
            json=payload,
        )

    assert len(captured_manifests) == 1
    assert json.loads(captured_bodies[0]) == payload
    assert captured_manifests[0]["request"]["wire_body_bytes"] == len(captured_bodies[0])
    encoded = json.dumps(captured_manifests[0])
    assert "credential-secret" not in encoded
    assert "must-not-survive" not in encoded
    assert "Authorization" not in encoded
    assert captured_manifests[0]["route"]["endpoint_path"] == "/safe"


@pytest.mark.asyncio
async def test_sink_failures_cannot_interrupt_http_request():
    payload = {"messages": [{"role": "user", "content": SECRET}], "stream": True}

    class BrokenRouter:
        async def _record_model_request_manifest(self, manifest):
            raise RuntimeError("sink failed")

    hooks = model_request_event_hooks(
        BrokenRouter(),
        provider="local",
        api_style="openai",
        transport="chat_completions",
        adapter="test",
        adapter_version="1",
        model="test",
        payload=payload,
        source_messages=payload["messages"],
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    async with httpx.AsyncClient(
        transport=transport, event_hooks=hooks,
    ) as client:
        response = await client.post("https://example.invalid/v1", json=payload)
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_receipt_observes_encoded_json_and_cache_header_presence():
    captured = []

    class Router:
        async def _record_model_request_manifest(self, manifest):
            captured.append(manifest)

    prepared = {'instructions':'prepared instructions','input':[]}
    encoded = {'instructions':SECRET,'input':[{'role':'user','content':SECRET_RESULT}]}
    hooks = model_request_event_hooks(
        Router(),provider='openai-codex',api_style='openai',transport='responses',
        adapter='test',adapter_version='1',model='test',payload=prepared,
        source_messages=[],
    )
    request = httpx.Request('POST','https://example.invalid/responses',json=encoded,
                            headers={'x-client-request-id':'private-affinity-value',
                                     'Authorization':'Bearer private-credential'})
    before = request.content
    await hooks['request'][0](request)
    assert request.content == before
    assert prepared == {'instructions':'prepared instructions','input':[]}
    assert len(captured) == 1
    manifest = captured[0]
    assert manifest['request']['payload_basis'] == 'httpx_encoded_json'
    assert manifest['messages']['rendered']['count'] == 2
    assert manifest['cache_diagnostics']['input_item_count'] == 1
    assert manifest['cache_diagnostics']['cache_affinity_header_presence']['x-client-request-id'] is True
    assert manifest['cache_diagnostics']['cache_affinity_header_presence']['session_id'] is False
    rendered = json.dumps(manifest)
    for secret in (SECRET,SECRET_RESULT,'private-affinity-value','private-credential'):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_logical_call_attempts_and_contextvars_are_isolated():
    async def build_pair(route: str) -> tuple[str, list[int], str]:
        await asyncio.sleep(0)
        token = begin_model_call(requested_route=route, selected_mode=route)
        try:
            ctx = Variant1RunContext.create(source=route, run_id=f"run-{route}")
            with bind_run_context(ctx):
                one = _manifest(requested_images=None)
                await asyncio.sleep(0)
                two = _manifest(requested_images=None)
                return (
                    one["logical_call_id"],
                    [one["attempt"], two["attempt"]],
                    two["run"]["run_id"],
                )
        finally:
            end_model_call(token)

    local, cloud = await asyncio.gather(build_pair("local"), build_pair("cloud"))
    assert local[0] != cloud[0]
    assert local[1] == cloud[1] == [1, 2]
    assert local[2] == "run-local"
    assert cloud[2] == "run-cloud"


def test_run_identity_includes_the_interactive_chat_session():
    chat = SimpleNamespace(
        active=SimpleNamespace(turn_session_id="chat-7"),
        viewed_session_id="chat-old",
    )
    ctx = Variant1RunContext.create(
        source="chat", run_id="run-chat", chat_session=chat)
    with bind_run_context(ctx):
        manifest = _manifest(requested_images=None)
    assert manifest["run"] == {
        "run_id": "run-chat",
        "source": "chat",
        "session_id": "chat-7",
    }


def test_headless_manifest_includes_durable_runtime_and_harness_revisions():
    config = SimpleNamespace(
        name="automation_v1",
        action_surface="trusted-local.v1",
        provider_tool_schema_revision="ipython.portable.v6",
        graph_revision="worker.ipython.v2",
    )
    ctx = Variant1RunContext.create(
        source="automation",
        run_id="run-worker",
        run_config=config,
        metadata={"chat_id": "worker:automation:manifest-canary"},
    )
    with bind_run_context(ctx):
        manifest = _manifest(requested_images=None)

    assert manifest["run"] == {
        "run_id": "run-worker",
        "source": "automation",
        "session_id": "worker:automation:manifest-canary",
    }
    assert manifest["surface"] == {
        "action_surface": "trusted-local.v1",
        "effective_action_surface": "trusted-local.v1",
        "mutation_write_enabled": False,
        "mutation_authority_revision": 0,
        "provider_tool_schema_revision": "ipython.portable.v6",
        "graph_revision": "worker.ipython.v2",
        "run_config_revision": "automation_v1",
    }


@pytest.mark.asyncio
async def test_run_context_keeps_only_bounded_manifest_ids():
    manifests = []

    class Router:
        async def _record_model_request_manifest(self, manifest):
            manifests.append(manifest)

    ctx = Variant1RunContext.create(source="chat", run_id="run-references")
    with bind_run_context(ctx):
        hooks = model_request_event_hooks(
            Router(),
            provider="local",
            api_style="openai",
            transport="chat_completions",
            adapter="test",
            adapter_version="1",
            model="test",
            payload={"messages": [{"role": "user", "content": SECRET}]},
            source_messages=[{"role": "user", "content": SECRET}],
        )
        request = SimpleNamespace(content=b'{"messages":[]}')
        for _ in range(70):
            await hooks["request"][0](request)

    refs = ctx.metadata["model_request_manifest_ids"]
    assert len(refs) == 64
    assert all(ref.startswith("mreq_") for ref in refs)
    assert SECRET not in json.dumps(ctx.metadata)


@pytest.mark.asyncio
async def test_router_publishes_off_request_path_and_defensively_copies(tmp_path):
    router = LLMRouter(
        {"mode": "local", "local": {}, "sampling": {}},
        str(tmp_path),
    )
    release_sink = asyncio.Event()
    sink_started = asyncio.Event()

    async def slow_mutating_sink(item):
        item["route"]["provider"] = "mutated-by-sink"
        sink_started.set()
        await release_sink.wait()

    router.set_model_request_manifest_sink(slow_mutating_sink)
    manifest = _manifest(requested_images=None)
    await asyncio.wait_for(
        router._record_model_request_manifest(manifest),
        timeout=0.1,
    )
    await asyncio.wait_for(sink_started.wait(), timeout=0.1)

    manifest["route"]["provider"] = "mutated-by-caller"
    first_snapshot = router.model_request_manifest_snapshot()
    assert first_snapshot["items"][0]["route"]["provider"] == "local"
    first_snapshot["items"][0]["route"]["provider"] = "mutated-snapshot"
    assert (
        router.model_request_manifest_snapshot()["items"][0]["route"]["provider"]
        == "local"
    )

    router._patch_model_request_manifest_response(manifest, {
        "provider_returned_model_id": "served-local-model",
        "system_fingerprint": "fp_test",
        "untrusted": SECRET,
    })
    patched = router.model_request_manifest_snapshot()["items"][0]
    assert patched["route"]["provider_returned_model_id"] == "served-local-model"
    assert patched["route"]["system_fingerprint"] == "fp_test"
    assert SECRET not in json.dumps(patched)

    release_sink.set()
    await asyncio.sleep(0)
    await router.stop()
