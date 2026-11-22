"""Rejected Python calls retain their source across resume and have a bounded wire form."""

import json

import pytest

import llm_cloud_stream as cloud
import tool_calling
from agent_engine.presets import chat_task_default
from agent_engine.runner import run_main_chat_task
from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
from agent_types import ToolBatchResult
from assistant_turn import AssistantTurn, truncated_tool_outcomes
from llm_stream_diagnostics import StreamDiagnostics
from llm_recovery import portable_messages
from model_runtime.message_graph import (
    build_message_graph, render_anthropic_messages, render_gemini_generate_content,
    render_openai_chat, render_openai_responses,
)
from test_agent_engine import FakePorts, SPEC
from test_model_request_adapter_hooks import _Router, _install_transport, _profile, _wire_payload


PREVIEW = "meituan/longcat-2.5-preview:free"
RELEASE = "meituan/longcat-2.5:free"


def _arguments(messages):
    return {
        call["id"]: json.loads(call["function"]["arguments"])
        for message in messages for call in message.get("tool_calls", [])
    }


def _tool_delta(call_id, code):
    return {"tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": "ipython", "arguments": json.dumps({"code": code})},
    }]}


async def _provider_step(monkeypatch, router, messages, delta, finish, *, provider, model):
    chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
    requests = _install_transport(monkeypatch, cloud, f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
    calls = tool_calling.ToolCallAccumulator()
    diagnostics = StreamDiagnostics()
    text = "".join([part async for part in cloud.call_openai(
        router, messages, {"max_tokens": 16_000}, "test-key", label=provider, model=model,
        profile=_profile(provider, "https://fixture.test/v1", model), tools=[SPEC],
        tool_call_sink=calls, stream_diagnostics=diagnostics,
    )])
    return (
        _wire_payload(requests)["messages"],
        AssistantTurn(text=text, tool_calls=tuple(calls.actions()), stop_reason=diagnostics.finish_reason),
    )


@pytest.mark.asyncio
async def test_two_rejected_cells_resume_with_full_sources_and_execute_correction_once(tmp_path, monkeypatch):
    first = "effects.append('partial-1')\n#" + "a" * 72_000
    second = "effects.append('partial-2')\n#" + "b" * 72_000
    normal = "effects.append('normal')\n#" + "c" * 8_000
    corrected = "effects.append('corrected')"
    responses = iter([
        (_tool_delta("cut-1", first), "length"), (_tool_delta("normal", normal), "tool_calls"),
        (_tool_delta("cut-2", second), "length"), (_tool_delta("corrected", corrected), "tool_calls"),
        ({"content": "done"}, "stop"),
    ])
    router = _Router()
    wires, effects = [], []

    async def stream(messages, _max_out, _images):
        delta, finish = next(responses)
        wire, turn = await _provider_step(monkeypatch, router, messages, delta, finish,
                                          provider="hermes", model=PREVIEW)
        wires.append(wire)
        return turn

    async def run_cells(actions):
        outcomes = []
        for action in actions:
            exec(action["args"]["code"], {"__builtins__": {}, "effects": effects})
            outcomes.append({"call_id": action["id"], "tool": "ipython", "ok": True,
                             "executed": True, "model_result": "done"})
        return ToolBatchResult(text="done", executed=True, outcomes=outcomes)

    ports = FakePorts([]).build()
    ports.loop.stream, ports.loop.run_actions = stream, run_cells
    task = dict(config=chat_task_default().with_overrides(checkpoints=True), text="edit",
                base_system="system", full_tspec=[SPEC], convo_tail=[], images=[], ports=ports)
    db = str(tmp_path / "checkpoints.sqlite3")
    store = SQLiteRunSnapshotStore(db)

    class CheckpointSaved(Exception):
        pass

    async def interrupt(completed, next_node, state):
        if completed == "model_step" and state["main"]["loop"]["step"] == 3:
            assert next_node == "tool"
            store.commit_boundary_sync(state, completed_node=completed, next_node=next_node,
                                       expected_head_sequence=None)
            raise CheckpointSaved(state["thread_id"])

    with pytest.raises(CheckpointSaved) as interruption:
        await run_main_chat_task(**task, is_resume=False, resume_snap=None,
                                 snapshot_store=store, commit=interrupt)
    assert effects == ["normal"]
    reopened = SQLiteRunSnapshotStore(db)
    saved = reopened.load_head_sync(interruption.value.args[0]).state
    assert _arguments(saved["messages"]) == {
        "cut-1": {"code": first}, "normal": {"code": normal}, "cut-2": {"code": second},
    }
    result = await run_main_chat_task(**task, is_resume=True, resume_snap=saved, snapshot_store=reopened)
    assert result.loop_result.reply == "done"
    assert result.loop_result.length_recoveries == 2
    assert effects == ["normal", "corrected"]
    assert _arguments(result.messages) == {**_arguments(saved["messages"]), "corrected": {"code": corrected}}
    assert len(wires) == 5
    for turn_index, wire in enumerate(wires):
        args = _arguments(wire)
        results = {m["tool_call_id"]: m["content"] for m in wire if m["role"] == "tool"}
        assert list(args) == list(results) == ["cut-1", "normal", "cut-2", "corrected"][:turn_index]
        for call_id in ("cut-1", "cut-2"):
            if call_id in args:
                assert len(json.dumps(args[call_id])) < 150
                assert "not executed" in results[call_id]
        if "normal" in args:
            assert args["normal"] == {"code": normal}
        if "corrected" in args:
            assert args["corrected"] == {"code": corrected}


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "model", "compact"), [
    ("hermes", RELEASE, True), ("hermes", "meituan/longcat-2.5", False), ("openrouter", PREVIEW, False),
])
async def test_wire_compaction_requires_exact_route_and_paired_host_rejection(monkeypatch, provider, model, compact):
    source = "x" * 6000
    action = {"tool": "ipython", "id": "repeat", "args": {"code": source}}
    call = tool_calling.format_assistant_turn_message([action])
    rejected, = tool_calling.format_tool_result_messages([action], outcomes=truncated_tool_outcomes([action]))
    # Older rows and ordinary errors can have identical visible text. Reused IDs
    # must bind by causal position, including when the renderer repairs an ID.
    legacy = {"role": "tool", "tool_call_id": "repeat", "content": rejected["content"], "is_error": True}
    history = [call, legacy, call, rejected]
    wire, _ = await _provider_step(monkeypatch, _Router(), history, {"content": "ok"}, "stop",
                                   provider=provider, model=model)
    first, second = _arguments(wire).values()
    assert first == {"code": source}
    if compact:
        assert len(second["code"]) < 150
    else:
        assert second == {"code": source}
    assert all(not any(k.startswith("_variant1") for k in row) for row in wire)
    assert len(_arguments(history)["repeat"]["code"]) == 6000


def test_small_and_provider_replay_calls_keep_their_arguments():
    source = "#" + "x" * 72_000
    calls = [
        {"tool": "ipython", "id": "signed", "args": {"code": source}, "provider_replay": {
            "gemini": {"id": "signed", "thought_signature": "opaque"},
            "responses": {"reasoning_items": [{"type": "reasoning", "id": "r1", "encrypted_content": "sealed"}]},
        }},
        {"tool": "ipython", "id": "sibling", "args": {"code": source}},
    ]
    small = [{"tool": "ipython", "id": "small", "args": {"code": "print(1)"}}]
    history = []
    for batch in (calls, small):
        history.append(tool_calling.format_assistant_turn_message(batch))
        history.extend(tool_calling.format_tool_result_messages(batch, outcomes=truncated_tool_outcomes(batch)))
    graph = build_message_graph(history)
    assert _arguments(render_openai_chat(graph, compact_rejected_ipython=True)) == _arguments(history)
    portable = build_message_graph(portable_messages(history))
    assert _arguments(render_openai_chat(portable, compact_rejected_ipython=True)) == _arguments(history)
    _, gemini = render_gemini_generate_content(graph)
    signed = next(p for msg in gemini for p in msg["parts"] if p.get("thoughtSignature") == "opaque")
    assert signed["functionCall"]["args"] == {"code": source}
    _, responses, _ = render_openai_responses(graph)
    assert any(item.get("encrypted_content") == "sealed" for item in responses)
    assert json.loads(next(i["arguments"] for i in responses if i.get("call_id") == "signed")) == {"code": source}
    _, anthropic = render_anthropic_messages(graph)
    assert next(b["input"] for msg in anthropic for b in msg["content"] if b.get("id") == "signed") == {"code": source}


def test_cutoff_counts_utf8_and_portable_recovery_keeps_only_attested_rejections():
    # Compact JSON for {"code": ""} takes 11 bytes.
    at_limit = "é" * 2042 + "x"
    actions = [
        {"tool": "ipython", "id": "below", "args": {"code": at_limit}},
        {"tool": "ipython", "id": "above", "args": {"code": at_limit + "x"}},
        {"tool": "another", "id": "other", "args": {"code": at_limit + "x"}},
        {"tool": "ipython", "id": "executed", "args": {"code": at_limit + "x"}},
    ]
    outcomes = truncated_tool_outcomes(actions)
    outcomes[-1]["executed"] = True
    history = [tool_calling.format_assistant_turn_message(actions)]
    history.extend(tool_calling.format_tool_result_messages(actions, outcomes=outcomes))
    for messages in (history, portable_messages(history)):
        args = _arguments(render_openai_chat(build_message_graph(messages), compact_rejected_ipython=True))
        assert len(args["above"]["code"]) < 150
        for index in (0, 2, 3):
            assert args[actions[index]["id"]] == actions[index]["args"]
