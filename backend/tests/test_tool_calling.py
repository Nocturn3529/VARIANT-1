"""Native tool schemas, accumulation, and call-bound follow-up messages."""

from __future__ import annotations

import json

import tool_calling
from model_runtime.message_graph import build_message_graph


SPECS = [
    {
        "name": "read_file",
        "description": "Read a text file.",
        "params": {"path": {"type": "string", "required": True}},
    },
    {"name": "glob", "description": "List files.", "params": {}},
]


def test_provider_schemas_preserve_required_arguments():
    openai = tool_calling.to_openai_tools(SPECS)
    anthropic = tool_calling.to_anthropic_tools(SPECS)
    gemini = tool_calling.to_gemini_tools(SPECS)

    assert openai[0]["function"]["parameters"]["required"] == ["path"]
    assert anthropic[0]["input_schema"]["required"] == ["path"]
    assert gemini[0]["functionDeclarations"][0]["parameters"]["required"] == ["path"]


def test_openai_delta_accumulates_directly_to_actions():
    acc = tool_calling.ToolCallAccumulator()
    acc.add_openai_delta([{
        "index": 0, "id": "call_1",
        "function": {"name": "read_file", "arguments": '{"path":'},
    }])
    acc.add_openai_delta([{
        "index": 0, "function": {"arguments": '"a.txt"}'},
    }])

    assert acc.actions() == [{
        "tool": "read_file", "args": {"path": "a.txt"}, "id": "call_1",
    }]


def test_openai_complete_argument_object_is_encoded_as_json_once():
    acc = tool_calling.ToolCallAccumulator()
    acc.add_openai_delta([{
        "index": 0,
        "id": "call_object",
        "function": {"name": "read_file", "arguments": {"path": "a.txt"}},
    }])

    assert acc.actions() == [{
        "tool": "read_file", "args": {"path": "a.txt"},
        "id": "call_object",
    }]


def test_malformed_arguments_fail_closed():
    acc = tool_calling.ToolCallAccumulator()
    acc.add_openai_delta([{
        "index": 0, "id": "bad",
        "function": {"name": "read_file", "arguments": '{"path":"x"'},
    }])
    action = acc.actions()[0]
    assert action["args"] == {}
    assert "malformed tool arguments" in action["argument_error"]


def test_anthropic_and_gemini_calls_accumulate_without_text_protocol():
    anthropic = tool_calling.ToolCallAccumulator()
    anthropic.anthropic_block_start(0, {
        "type": "tool_use", "id": "a1", "name": "glob", "input": {},
    })
    anthropic.anthropic_input_json_delta(0, '{}')
    gemini = tool_calling.ToolCallAccumulator()
    gemini.add_gemini_function_call("glob", {})

    assert anthropic.actions()[0]["tool"] == "glob"
    assert gemini.actions()[0]["tool"] == "glob"


def test_gemini_unsigned_stream_replay_dedupes_by_candidate_part():
    acc = tool_calling.ToolCallAccumulator()
    call = {
        "name": "glob",
        "args": {"pattern": "*.py"},
        "provider_replay": {"gemini": {
            "candidate_index": 0, "part_index": 1,
        }},
    }
    acc.add_gemini_function_call(call)
    acc.add_gemini_function_call(call)
    acc.add_gemini_function_call({
        **call,
        "provider_replay": {"gemini": {
            "candidate_index": 0, "part_index": 2,
        }},
    })

    assert len(acc.actions()) == 2


def _turn_with_results(actions, outcomes):
    return [
        tool_calling.format_assistant_turn_message(actions),
        *tool_calling.format_tool_result_messages(actions, outcomes=outcomes),
    ]


def test_provider_replay_survives_durable_transcript_roundtrip():
    action = {
        "tool": "glob", "args": {}, "id": "provider-call-1",
        "provider_replay": {"gemini": {"thought_signature": "opaque"}},
    }
    messages = _turn_with_results(
        [action],
        outcomes=[{
            "tool": "glob", "call_id": "provider-call-1",
            "result": "done", "model_result": "done",
            "ok": True, "executed": True,
        }],
    )

    restored = json.loads(json.dumps(messages))
    graph = build_message_graph(restored)
    assert graph.calls[0].provider_replay == {
        "gemini": {"thought_signature": "opaque"}
    }


def test_provider_replay_survives_checkpoint_actions():
    actions = tool_calling.durable_provider_replay_actions([{
        "tool": "glob", "args": {}, "id": "provider-call-1",
        "provider_replay": {
            "responses": {"reasoning_items": [{
                "type": "reasoning",
                "encrypted_content": "opaque-encrypted-state",
            }]},
        },
    }])

    serialized = json.dumps(actions)
    assert "opaque-encrypted-state" in serialized
    assert actions[0]["provider_replay"]["responses"]["reasoning_items"]


def test_followup_uses_exact_call_bound_outcomes():
    actions = [
        {"tool": "read_file", "args": {"path": "a"}, "id": "a"},
        {"tool": "read_file", "args": {"path": "b"}, "id": "b"},
    ]
    outcomes = [
        {"tool": "read_file", "call_id": "b", "model_result": "B", "ok": True},
        {"tool": "read_file", "call_id": "a", "model_result": "A", "ok": True},
    ]
    messages = _turn_with_results(
        actions, outcomes=outcomes)

    assert [m["role"] for m in messages] == ["assistant", "tool", "tool"]
    assert messages[1]["content"] == "A"
    assert messages[2]["content"] == "B"


def test_failed_tool_result_preserves_provider_error_flag():
    messages = _turn_with_results([
        {"tool": "read_file", "args": {"path": "missing"}, "id": "a"},
    ], outcomes=[{
        "tool": "read_file", "call_id": "a",
        "model_result": "not found", "ok": False,
    }])

    assert messages[1]["is_error"] is True


def test_missing_call_bound_outcome_is_an_explicit_tool_result():
    messages = _turn_with_results([
        {"tool": "read_file", "args": {}, "id": "a"},
        {"tool": "glob", "args": {}, "id": "b"},
    ], outcomes=[{
        "tool": "read_file", "call_id": "a", "model_result": "contents",
    }])

    assert messages[1]["content"] == "contents"
    assert messages[2]["content"] == "no result returned for this call"


def test_single_call_helper_needs_no_result_parser():
    messages = _turn_with_results([
        {"tool": "glob", "args": {}, "id": "g1"},
    ], outcomes=[{
        "tool": "glob", "call_id": "g1", "model_result": "file.txt",
    }])
    assert messages[1]["content"] == "file.txt"


def test_should_send_provider_tools_only_when_schemas_exist():
    assert tool_calling.should_send_provider_tools(mode="local", tool_specs=SPECS)
    assert not tool_calling.should_send_provider_tools(mode="cloud", tool_specs=[])
