import json

import pytest
from llm_openai_codex_responses import _reasoning_replay_item
from llm_router import LLMRouter
from model_runtime.context import context_limit_tokens, model_route_support_coordinates
from model_runtime.message_graph import build_message_graph, render_openai_responses


def test_codex_reasoning_replay_stays_adjacent_to_tool_exchange(tmp_path):
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "content": [],
        "summary": [],
        "encrypted_content": "opaque-provider-state",
    }
    messages = [
        {"role": "user", "content": "compute"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "ipython",
                    "arguments": json.dumps({"code": "print(42)"}),
                },
                "provider_replay": {"responses": {
                    "item_id": "fc_1",
                    "reasoning_items": [reasoning],
                }},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
    ]

    _, items, _ = render_openai_responses(build_message_graph(messages))
    assert [item["type"] for item in items] == [
        "message", "reasoning", "function_call", "function_call_output",
    ]
    assert items[1]["encrypted_content"] == "opaque-provider-state"
    assert items[2]["id"] == "fc_1"
    assert items[2]["call_id"] == items[3]["call_id"] == "call_1"


def test_codex_replay_parser_rejects_nonopaque_reasoning():
    assert _reasoning_replay_item({"type": "reasoning"}) is None
    assert _reasoning_replay_item({
        "type": "message", "encrypted_content": "opaque",
    }) is None
    assert _reasoning_replay_item({
        "id": "rs_1", "type": "reasoning", "encrypted_content": "opaque",
    }) == {
        "id": "rs_1", "type": "reasoning", "encrypted_content": "opaque",
    }


@pytest.mark.asyncio
async def test_codex_client_cap_rejects_oversize_utf8_instead_of_returning_a_prefix(tmp_path, monkeypatch):
    import llm_openai_codex_responses as codex
    from tests.test_cloud_stream_reliability import _StreamResponse, _client_for
    monkeypatch.setattr(codex, "MAX_RESPONSE_OUTPUT_BYTES", 6)
    response = _StreamResponse(["data: " + json.dumps({"type":"response.output_text.delta", "delta":"ab🙂cd"})])
    monkeypatch.setattr(codex.httpx, "AsyncClient", _client_for(response))
    output = []
    with pytest.raises(codex.ProviderRequestError, match="resource limit"):
        async for text in codex.call_openai_codex_responses(LLMRouter({}, str(tmp_path)),
                [{"role":"user","content":"fixture"}], {"max_tokens":64}, "fake"):
            output.append(text)
    assert output == []


def test_codex_route_coordinates_context_and_reasoning_clamp(tmp_path):
    router = LLMRouter({
        "mode": "cloud",
        "cloud": {
            "provider": "openai-codex",
            "openai-codex_model": "gpt-5.3-codex-spark",
        },
    }, str(tmp_path))
    spark = {
        "mode": "cloud", "provider": "openai-codex",
        "model": "gpt-5.3-codex-spark",
    }
    luna = {**spark, "model": "gpt-5.6-luna"}

    assert model_route_support_coordinates(router, spark)["adapter"] == (
        "openai_codex.responses"
    )
    assert context_limit_tokens(router, spark) == 128_000
    assert context_limit_tokens(router, luna) == 272_000
    assert router.get_reasoning_effort(
        "openai-codex", spark["model"],
    ) == "xhigh"
    assert router.get_reasoning_effort(
        "openai-codex", luna["model"],
    ) == "max"
    with router.bind_model_route({**luna, "reasoning_effort": "high"}):
        assert router.get_reasoning_effort(
            "openai-codex", luna["model"],
        ) == "high"
