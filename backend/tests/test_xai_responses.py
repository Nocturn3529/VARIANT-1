"""xAI OAuth Responses transport (personal SuperGrok path)."""

from __future__ import annotations

from llm_cloud_stream import BufferedToolCallSink
from model_runtime.message_graph import build_message_graph, render_openai_responses
from model_runtime.responses_protocol import push_function_call, responses_tools


def test_system_and_user_map_to_instructions_and_input():
    ins, items, _ = render_openai_responses(build_message_graph([
        {"role": "system", "content": "You are VARIANT-1."},
        {"role": "user", "content": "Hello"},
    ]))
    assert "VARIANT-1" in ins
    assert items[0]["role"] == "user"
    assert items[0]["content"][0]["type"] == "input_text"


def test_tool_result_maps_to_function_call_output():
    _, items, _ = render_openai_responses(build_message_graph([
        {"role": "user", "content": "t"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": "{\"path\":\"README.md\"}"},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Wednesday"},
    ]))
    kinds = [i.get("type") for i in items]
    assert "function_call" in kinds
    assert "function_call_output" in kinds
    call = next(i for i in items if i.get("type") == "function_call")
    out = next(i for i in items if i.get("type") == "function_call_output")
    assert call["call_id"] == out["call_id"] == "call_1"
    assert call["id"].startswith("fc_")
    assert call["id"] != call["call_id"]
    assert "Wednesday" in out["output"]


def test_responses_tools_from_variant1_specs():
    tools = responses_tools([
        {"name": "read_file", "description": "read a file", "params": {
            "path": {"type": "string", "required": True},
        }},
    ])
    assert tools and tools[0]["type"] == "function"
    assert tools[0]["name"] == "read_file"


def test_xai_ignores_unidentified_argument_completion_and_deduplicates_item_done():
    sink = BufferedToolCallSink()
    unidentified = {
        "type": "function_call",
        "name": "ipython",
        "arguments": '{"code":"print(1)"}',
    }
    completed = {
        **unidentified,
        "id": "fc_provider_item",
        "call_id": "call_provider_exact",
    }

    push_function_call(sink, unidentified, 0)
    push_function_call(sink, completed, 1)
    push_function_call(sink, completed, 2)

    assert len(sink._events) == 1
    tool_call = sink._events[0][1][0][0]
    assert tool_call["id"] == "call_provider_exact"
    assert tool_call["function"]["name"] == "ipython"


def test_xai_reasoning_effort_default_low(tmp_path):
    from llm_router import LLMRouter
    cfg = {"mode": "cloud", "cloud": {"provider": "xai"}, "local": {}, "sampling": {}}
    r = LLMRouter(cfg, str(tmp_path), config_path=str(tmp_path / "llm.json"))
    assert r.get_reasoning_effort("xai", "grok-4.6") == "low"
    with r.bind_model_route({
        "mode": "cloud", "provider": "xai", "model": "grok-4.6",
        "reasoning_effort": "high",
    }):
        assert r.get_reasoning_effort("xai", "grok-4.6") == "high"
    assert not hasattr(r, "set_xai_reasoning_effort")
