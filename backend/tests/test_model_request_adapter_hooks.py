"""Adapter contracts for privacy-safe model request manifests.

These tests intentionally exercise the HTTPX request hook rather than calling
the manifest builder directly.  The mock transport therefore sees the same
encoded request whose structural receipt is emitted by each adapter.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import llm_cloud_stream as cloud
import llm_local_stream as local
import llm_openai_codex_responses as codex_responses
import llm_xai_responses as xai_responses
from model_runtime.prompt_cache import resolve_prompt_cache_identity
import tool_calling


_REAL_ASYNC_CLIENT = httpx.AsyncClient
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
    "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
)
_TOOLS = [{
    "name": "read_file",
    "description": "Read a file.",
    "params": {
        "path": {"type": "string", "required": True},
    },
}]
_TOOL_HISTORY = [
    {"role": "system", "content": "System instructions"},
    {"role": "user", "content": "Read the requested file"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"C:/private.txt"}',
            },
        }],
    },
    {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "private tool result",
    },
]


class _Inference:
    def start(self, _model):
        return "telemetry-1", {"status": "streaming", "output_tokens": 0}

    def token(self, _telemetry_id, _token):
        return {"status": "streaming", "output_tokens": 1}

    def complete(self, _telemetry_id, **_kwargs):
        return {
            "status": "complete",
            "prompt_tokens": 2,
            "output_tokens": 1,
            "time_to_last_token_s": 0.01,
        }

    def fail(self, _telemetry_id, **_kwargs):
        return {"status": "error"}


class _Router:
    def __init__(self):
        self.manifests: list[dict] = []
        self.usage: list[tuple] = []
        self.cfg = {"cloud": {}}
        self.model_name = "local-test.gguf"
        self.engine = SimpleNamespace(
            ready=True,
            model=self.model_name,
            reasoning_budget=0,
            base_url="https://local.test",
            ctx_size=8192,
            poll_process=lambda: True,
        )
        self._inference = _Inference()

    async def _record_model_request_manifest(self, manifest):
        self.manifests.append(manifest)

    async def _publish_inference(self, _snapshot, force=False):
        del force

    def _record_usage(self, *args, **kwargs):
        self.usage.append((args, kwargs))

    def _observe_cloud_response(self, *_args, **_kwargs):
        return None

    def _provider_headers(self, _profile, key):
        return {"Authorization": f"Bearer {key}"}

    def _openai_url(self, base, path):
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    def provider_profile(self, _name):
        return None

    def get_cloud_model(self, _name):
        return None

    def get_reasoning_effort(self, _provider=None, _model=None):
        return "low"


def _profile(
    name: str,
    base_url: str,
    default_model: str,
    *,
    prompt_cache_body_field: str = "",
    prompt_cache_header: str = "",
    **overrides,
):
    values = dict(
        name=name,
        base_url=base_url,
        default_model=default_model,
        omit_temperature=False,
        request_defaults={},
        sampling_fields=("temperature",),
        sampling_forbidden_model_patterns=(),
        completion_token_field="max_tokens",
        max_completion_token_model_patterns=(),
        structured_output_style="",
        prompt_cache_body_field=prompt_cache_body_field,
        prompt_cache_header=prompt_cache_header,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_transport(monkeypatch, adapter_module, body: str):
    """Replace an adapter's client with a real client plus MockTransport."""
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
            request=request,
        )

    def client_factory(*_args, **kwargs):
        return _REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
            event_hooks=kwargs.get("event_hooks"),
            trust_env=False,
        )

    monkeypatch.setattr(adapter_module.httpx, "AsyncClient", client_factory)
    return captured


def _wire_payload(captured: list[httpx.Request]) -> dict:
    assert len(captured) == 1
    return json.loads(captured[0].content.decode("utf-8"))


def _assert_receipt_matches_wire(router: _Router, captured, *, adapter: str):
    assert len(router.manifests) == 1
    manifest = router.manifests[0]
    wire = captured[0].content
    payload = json.loads(wire.decode("utf-8"))

    assert manifest["type"] == "model:request_manifest"
    assert manifest["route"]["adapter"] == adapter
    assert manifest["request"]["wire_body_bytes"] == len(wire)
    assert manifest["request"]["payload_keys"] == sorted(payload)
    assert router.usage
    assert (
        router.usage[-1][1]["manifest_ref"]["manifest_id"]
        == manifest["manifest_id"]
    )
    return manifest, payload


@pytest.mark.asyncio
async def test_local_hook_describes_final_chat_completions_payload(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        local,
        'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        "data: [DONE]\n\n",
    )
    router = _Router()
    cache_identity = resolve_prompt_cache_identity("durable-local-chat")

    tokens = [
        token
        async for token in local.call_local_inner(
            router,
            [{"role": "user", "content": "Inspect the desktop"}],
            {"temperature": 0.2, "top_p": 0.8, "max_tokens": 64},
            tools=_TOOLS,
            prompt_cache_identity=cache_identity,
        )
    ]

    assert tokens == ["ok"]
    manifest, payload = _assert_receipt_matches_wire(
        router, captured, adapter="llama_cpp.chat_completions")
    assert payload["messages"][0]["content"] == "Inspect the desktop"
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert manifest["route"]["transport"] == "chat_completions"
    assert manifest["tools"]["requested_count"] == 1
    assert manifest["tools"]["rendered_count"] == 1
    assert manifest["budget"]["context_limit_tokens"] == 8192
    assert payload["stream_options"] == {"include_usage": True}
    assert manifest["prompt_cache"]["key_id"] == cache_identity.key
    assert manifest["prompt_cache"]["application"] == "local_server_prefix_cache"


@pytest.mark.asyncio
async def test_openai_hook_describes_final_chat_completions_payload(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        cloud,
        'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        "data: [DONE]\n\n",
    )
    router = _Router()
    profile = _profile("openai", "https://openai.test/v1", "gpt-test")

    tokens = [
        token
        async for token in cloud.call_openai(
            router,
            [{"role": "user", "content": "Look at this screen"}],
            {"temperature": 0.1, "max_tokens": 32},
            "test-key",
            image_b64=_TINY_PNG_B64,
            profile=profile,
            tools=_TOOLS,
        )
    ]

    assert tokens == ["ok"]
    manifest, payload = _assert_receipt_matches_wire(
        router, captured, adapter="openai.chat_completions")
    assert payload['max_tokens'] == 32
    assert manifest['generation_budget']['application'] == 'remote'
    assert manifest['generation_budget']['requested_tokens'] == 32
    content = payload["messages"][0]["content"]
    assert content[-1]["type"] == "image_url"
    assert manifest["images"]["requested_count"] == 1
    assert manifest["images"]["rendered_count"] == 1
    assert manifest["images"]["loss"] is False
    assert manifest["tools"]["rendered"][0]["name"] == "read_file"


@pytest.mark.asyncio
async def test_openai_compatible_reasoning_alias_stays_out_of_visible_reply(monkeypatch):
    _install_transport(
        monkeypatch,
        cloud,
        'data: {"choices":[{"delta":{"reasoning":"private"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n",
    )
    router = _Router()
    profile = _profile(
        "hermes", "http://127.0.0.1:8645/v1", "upstage/solar-pro4:free")
    reasoning = []

    tokens = [
        token
        async for token in cloud.call_openai(
            router,
            [{"role": "user", "content": "Think, then answer"}],
            {"temperature": 0, "max_tokens": 32},
            "",
            profile=profile,
            reasoning_sink=reasoning.append,
        )
    ]

    assert tokens == ["ok"]
    assert reasoning == ["private"]


@pytest.mark.asyncio
async def test_anthropic_hook_sees_converted_tool_protocol(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        cloud,
        (
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"ok"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ),
    )
    router = _Router()
    profile = _profile(
        "anthropic", "https://anthropic.test", "claude-test")

    tokens = [
        token
        async for token in cloud.call_anthropic(
            router,
            _TOOL_HISTORY,
            {"temperature": 0.2, "max_tokens": 48},
            "test-key",
            profile=profile,
            tools=_TOOLS,
        )
    ]

    assert tokens == ["ok"]
    manifest, payload = _assert_receipt_matches_wire(
        router, captured, adapter="anthropic.messages")
    assistant = next(
        item for item in payload["messages"] if item["role"] == "assistant")
    result = next(
        block
        for item in payload["messages"]
        for block in item["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    assert assistant["content"][0]["type"] == "tool_use"
    assert result["tool_use_id"] == "call_1"
    assert manifest["tool_protocol"]["rendered"]["calls"] == 1
    assert manifest["tool_protocol"]["rendered"]["results"] == 1
    assert manifest["tool_protocol"]["valid"] is True


@pytest.mark.asyncio
async def test_anthropic_json_mode_uses_native_output_config(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        cloud,
        (
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"{}"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        ),
    )
    profile = _profile(
        "anthropic", "https://anthropic.test", "claude-test",
        structured_output_style="anthropic_json_schema",
    )

    tokens = [token async for token in cloud.call_anthropic(
        _Router(), [{"role": "user", "content": "return json"}],
        {"max_tokens": 48}, "test-key", profile=profile, json_mode=True,
    )]

    assert tokens == ["{}"]
    payload = json.loads(captured[0].content)
    assert payload["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_openai_reasoning_model_uses_profile_policy_fields(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        cloud,
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n',
    )
    profile = _profile(
        "openai", "https://api.openai.com/v1", "gpt-5.6",
        sampling_fields=("temperature", "top_p"),
        completion_token_field="auto",
        max_completion_token_model_patterns=("gpt-5*", "o1*", "o3*", "o4*"),
        sampling_forbidden_model_patterns=("gpt-5*", "o1*", "o3*", "o4*"),
    )

    tokens = [token async for token in cloud.call_openai(
        _Router(), [{"role": "user", "content": "test"}],
        {"temperature": 0.2, "top_p": 0.8, "max_tokens": 48},
        "test-key", profile=profile, model="gpt-5.6",
    )]

    assert tokens == ["ok"]
    payload = json.loads(captured[0].content)
    assert payload["max_completion_tokens"] == 48
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload


@pytest.mark.asyncio
async def test_gemini_hook_preserves_native_tool_history_protocol(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        cloud,
        (
            'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},'
            '"finishReason":"STOP"}],'
            '"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":1}}\n\n'
        ),
    )
    router = _Router()
    profile = _profile(
        "gemini", "https://gemini.test/v1beta", "gemini-test")

    tokens = [
        token
        async for token in cloud.call_gemini(
            router,
            _TOOL_HISTORY,
            {"temperature": 0.2, "top_p": 0.9, "max_tokens": 48},
            "test-key",
            profile=profile,
            tools=_TOOLS,
        )
    ]

    assert tokens == ["ok"]
    manifest, payload = _assert_receipt_matches_wire(
        router, captured, adapter="gemini.generate_content")
    assert payload["tools"][0]["functionDeclarations"][0]["name"] == "read_file"
    call = next(
        part["functionCall"]
        for content in payload["contents"]
        for part in content["parts"]
        if "functionCall" in part
    )
    result = next(
        part["functionResponse"]
        for content in payload["contents"]
        for part in content["parts"]
        if "functionResponse" in part
    )
    assert call["id"] == result["id"] == "call_1"
    assert call["name"] == result["name"] == "read_file"
    assert manifest["tool_protocol"]["source"]["calls"] == 1
    assert manifest["tool_protocol"]["source"]["results"] == 1
    assert manifest["tool_protocol"]["rendered"]["calls"] == 1
    assert manifest["tool_protocol"]["rendered"]["results"] == 1
    assert manifest["tool_protocol"]["call_loss"] is False
    assert manifest["tool_protocol"]["result_loss"] is False
    assert manifest["tool_protocol"]["valid"] is True
    assert "tool_protocol_loss" not in {
        transform["kind"] for transform in manifest["transforms"]
    }


@pytest.mark.asyncio
async def test_gemini_rejects_clean_eof_without_terminal_candidate(monkeypatch):
    _install_transport(
        monkeypatch,
        cloud,
        'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}\n\n',
    )
    router = _Router()
    profile = _profile(
        "gemini", "https://gemini.test/v1beta", "gemini-test")
    tokens = []

    with pytest.raises(cloud.ProviderRequestError, match="terminal candidate"):
        async for token in cloud.call_gemini(
            router,
            [{"role": "user", "content": "test"}],
            {"temperature": 0.2, "top_p": 0.9, "max_tokens": 48},
            "test-key",
            profile=profile,
        ):
            tokens.append(token)

    assert tokens == ["partial"]


@pytest.mark.asyncio
async def test_gemini_stream_replays_exact_id_and_signature_without_manifest_leak(
        monkeypatch):
    signature = "opaque-provider-thought-signature"
    captured = _install_transport(
        monkeypatch,
        cloud,
        (
            'data: {"candidates":[{"content":{"role":"model","parts":[{'
            '"functionCall":{"id":"gcall_9","name":"read_file",'
            '"args":{"path":"C:/private.txt"}},'
            f'"thoughtSignature":"{signature}"'
            '}]},"finishReason":"STOP"}],'
            '"usageMetadata":{"promptTokenCount":3,'
            '"candidatesTokenCount":1}}\n\n'
        ),
    )
    router = _Router()
    profile = _profile(
        "gemini", "https://gemini.test/v1beta", "gemini-test")
    initial = [{"role": "user", "content": "read it"}]
    accumulator = tool_calling.ToolCallAccumulator()

    first_tokens = [
        token
        async for token in cloud.call_gemini(
            router,
            initial,
            {"temperature": 0.2, "top_p": 0.9, "max_tokens": 48},
            "test-key",
            profile=profile,
            tools=_TOOLS,
            tool_call_sink=accumulator,
        )
    ]
    assert first_tokens == []
    actions = accumulator.actions()
    outcomes = [{
            "tool": "read_file",
            "call_id": actions[0]["id"],
            "result": "contents",
            "model_result": "contents",
            "ok": True,
            "executed": True,
        }]
    history = initial + [
        tool_calling.format_assistant_turn_message(actions),
        *tool_calling.format_tool_result_messages(actions, outcomes=outcomes),
    ]
    assert signature in json.dumps(history)

    [
        token
        async for token in cloud.call_gemini(
            router,
            history,
            {"temperature": 0.2, "top_p": 0.9, "max_tokens": 48},
            "test-key",
            profile=profile,
            tools=_TOOLS,
        )
    ]

    assert len(captured) == 2
    second_payload = json.loads(captured[1].content.decode("utf-8"))
    call_part = next(
        part
        for content in second_payload["contents"]
        for part in content["parts"]
        if "functionCall" in part
    )
    result_part = next(
        part
        for content in second_payload["contents"]
        for part in content["parts"]
        if "functionResponse" in part
    )
    assert call_part["functionCall"]["id"] == "gcall_9"
    assert call_part["thoughtSignature"] == signature
    assert result_part["functionResponse"]["id"] == "gcall_9"
    assert signature not in json.dumps(router.manifests)


@pytest.mark.asyncio
async def test_xai_responses_hook_preserves_requested_image(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        xai_responses,
        (
            'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            'data: {"type":"response.completed","response":{"usage":'
            '{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}\n\n'
        ),
    )
    router = _Router()
    profile = _profile(
        "xai",
        "https://xai.test/v1",
        "grok-test",
        prompt_cache_header="x-grok-conv-id",
    )
    cache_identity = resolve_prompt_cache_identity("durable-xai-chat")

    tokens = [
        token
        async for token in xai_responses.call_xai_responses(
            router,
            [{"role": "user", "content": "Inspect this screenshot"}],
            {"temperature": 0.2, "max_tokens": 48},
            "test-key",
            image_b64=_TINY_PNG_B64,
            profile=profile,
            tools=_TOOLS,
            prompt_cache_identity=cache_identity,
        )
    ]

    assert tokens == ["ok"]
    manifest, payload = _assert_receipt_matches_wire(
        router, captured, adapter="xai.responses")
    assert any(
        part.get("type") in {"input_image", "image_url"}
        for item in payload["input"]
        if item.get("type") == "message"
        for part in item.get("content", [])
    )
    assert payload["store"] is False
    assert manifest["images"]["requested_count"] == 1
    assert manifest["images"]["rendered_count"] == 1
    assert manifest["images"]["loss"] is False
    assert captured[0].headers["x-grok-conv-id"] == cache_identity.key
    assert manifest["prompt_cache"]["key_id"] == cache_identity.key
    assert manifest["prompt_cache"]["application"] == "header.x-grok-conv-id"
    projection = next(
        item for item in manifest["transforms"]
        if item["kind"] == "image_projection"
    )
    assert projection == {
        "kind": "image_projection",
        "requested": 1,
        "rendered": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('cache_header', ['', 'x-client-request-id', 'session_id', None])
async def test_codex_responses_projects_the_same_durable_cache_identity(monkeypatch, cache_header):
    captured = _install_transport(
        monkeypatch,
        codex_responses,
        (
            'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            'data: {"type":"response.completed","response":{"usage":'
            '{"input_tokens":3,"output_tokens":1,"total_tokens":4}}}\n\n'
        ),
    )
    router = _Router()
    profile = _profile(
        "openai-codex",
        "https://chatgpt.com/backend-api/codex",
        "gpt-test",
        prompt_cache_body_field="prompt_cache_key",
        prompt_cache_header=cache_header,
    )
    if cache_header is None:
        from model_providers.builtin import builtin_profiles
        profile = next(p for p in builtin_profiles() if p.name == 'openai-codex')
        cache_header = profile.prompt_cache_header
        assert cache_header == 'session_id'
    cache_identity = resolve_prompt_cache_identity("durable-codex-chat")

    tokens = [
        token
        async for token in codex_responses.call_openai_codex_responses(
            router,
            [{"role": "user", "content": "Inspect the repository"}],
            {"max_tokens": 48},
            "oauth-token",
            profile=profile,
            model="gpt-test",
            prompt_cache_identity=cache_identity,
        )
    ]

    assert tokens == ["ok"]
    manifest, payload = _assert_receipt_matches_wire(
        router, captured, adapter="openai_codex.responses"
    )
    assert payload["prompt_cache_key"] == cache_identity.key
    assert manifest["prompt_cache"]["key_id"] == cache_identity.key
    assert manifest["prompt_cache"]["application"] == (
        "body.prompt_cache_key" + (f"+header.{cache_header}" if cache_header else "")
    )
    presence = manifest['cache_diagnostics']['cache_affinity_header_presence']
    assert presence['x-client-request-id'] is (cache_header == 'x-client-request-id')
    assert presence['session_id'] is (cache_header == 'session_id')
    if cache_header:
        assert captured[0].headers[cache_header] == cache_identity.key


@pytest.mark.asyncio
async def test_codex_consumer_keeps_advisory_budget_until_terminal(monkeypatch):
    long_text = '長い応答🙂' * 500
    _install_transport(
        monkeypatch,
        codex_responses,
        (
            'data: ' + json.dumps({'type':'response.output_text.delta', 'delta':long_text}) + '\n\n'
            + 'data: {"type":"response.completed","response":{"usage":{"input_tokens":100,"output_tokens":2500,"total_tokens":2600}}}\n\n'
        ),
    )
    diagnostics = cloud.StreamDiagnostics()

    budget_router = _Router()
    tokens = [
        token
        async for token in codex_responses.call_openai_codex_responses(
            budget_router, [{"role": "user", "content": "test"}],
            {"max_tokens": 1}, "oauth-token",
            profile=_profile(
                "openai-codex", "https://chatgpt.com/backend-api/codex",
                "gpt-test",
            ),
            model="gpt-test",
            stream_diagnostics=diagnostics,
        )
    ]

    assert tokens == [long_text]
    assert diagnostics.finish_reason == "completed"
    assert budget_router.usage[-1][1]['raw_usage']['output_tokens'] == 2500
    manifest = budget_router.manifests[-1]
    assert manifest['generation']['max_output_tokens'] is None
    assert manifest['generation_budget'] == {'application':'advisory_unapplied',
        'requested_tokens':1, 'resource_limit_bytes':codex_responses.MAX_RESPONSE_OUTPUT_BYTES}


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['valid', 'oversize', 'incomplete'])
async def test_codex_compaction_uses_terminal_and_semantic_bounds(monkeypatch, variant):
    import copy
    import llm_profiles
    import transcript_economy as economy
    from test_transcript_economy import _complete_recap, _compressible_messages
    recap = _complete_recap(confirmed='é🙂' * (1700 if variant != 'oversize' else 9000))
    assert len(recap.encode('utf-8')) > 1900 * 3
    terminal = 'response.incomplete' if variant == 'incomplete' else 'response.completed'
    events = [{'type':'response.output_text.delta', 'delta':recap},
              {'type':terminal, 'response':{'usage':{'input_tokens':9000, 'output_tokens':2500},
                  'incomplete_details':{'reason':'max_output_tokens'}}}]
    _install_transport(monkeypatch, codex_responses,
        ''.join('data: ' + json.dumps(event) + '\n\n' for event in events))
    router = _Router()
    async def stream(messages, **options):
        async for token in codex_responses.call_openai_codex_responses(
            router, messages, options['sampling'], 'fake',
            profile=_profile('openai-codex', 'https://chatgpt.com/backend-api/codex', 'gpt-test'),
            model='gpt-test', stream_diagnostics=options['stream_diagnostics'],
            tool_call_sink=options['tool_call_sink'], reasoning_sink=options['reasoning_sink']):
            yield token
    router.stream = stream
    async def complete(messages, **options):
        return await llm_profiles.complete(router, messages, **options)
    messages = _compressible_messages()
    for row in messages[2:]: row['content'] *= 500
    from observability import context_lineage
    context_lineage.attach_to_messages(messages, context_lineage.new_receipt('main_chat_step'))
    original = copy.deepcopy(messages)
    result = await economy.compress_messages(messages, complete=complete)
    assert messages == original
    assert router.usage[-1][1]['raw_usage']['output_tokens'] == 2500
    if variant == 'valid':
        assert len(result) < len(messages)
        assert any(row.get('variant1_compaction') is True for row in result)
        assert 'context_compression' in json.dumps(result)
    else:
        assert result == original


@pytest.mark.asyncio
async def test_codex_responses_projects_tool_screenshot_pixels(monkeypatch):
    captured = _install_transport(
        monkeypatch,
        codex_responses,
        (
            'data: {"type":"response.output_text.delta","delta":"seen"}\n\n'
            'data: {"type":"response.completed","response":{"usage":'
            '{"input_tokens":4,"output_tokens":1,"total_tokens":5}}}\n\n'
        ),
    )
    router = _Router()
    profile = _profile(
        "openai-codex",
        "https://chatgpt.com/backend-api/codex",
        "gpt-test",
    )
    image = {
        "data_b64": _TINY_PNG_B64,
        "media_type": "image/png",
        "origin": "tool_result",
        "tool_call_id": "call_1",
        "tool_call_ids": ["call_1"],
        "tool_name": "browser_screenshot",
        "artifact_ref": "artifact://sha256/test",
        "capture": {"status": "captured"},
    }

    tokens = [
        token
        async for token in codex_responses.call_openai_codex_responses(
            router,
            _TOOL_HISTORY,
            {"max_tokens": 48},
            "oauth-token",
            image_b64=image,
            profile=profile,
            model="gpt-test",
        )
    ]

    assert tokens == ["seen"]
    manifest, payload = _assert_receipt_matches_wire(
        router, captured, adapter="openai_codex.responses"
    )
    assert any(
        part.get("type") == "input_image"
        for item in payload["input"]
        if item.get("type") == "message"
        for part in item.get("content", [])
    )
    assert manifest["images"]["captured_count"] == 1
    assert manifest["images"]["selected_count"] == 1
    assert manifest["images"]["encoded_count"] == 1
    assert manifest["images"]["rendered_count"] == 1
    assert manifest["images"]["loss"] is False
