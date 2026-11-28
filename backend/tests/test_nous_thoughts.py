"""Returned Nous plaintext travels with its owning turn from SSE to saved chat."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import llm_cloud_stream as cloud
from chat_finalize import _durable_turn_messages
from chat_session import ConnectionSession
from host_ports import build_task_turn_ports
from llm_router import LLMRouter
from observability import activity
from reasoning_summaries import ReasoningBuffer, SUMMARY_TEXT_LIMIT, thoughts_enabled
from run_context import Variant1RunContext, bind_run_context
from tests.support.conversation_sessions import open_sessions
from tests.test_cloud_stream_reliability import _client_for, _StreamResponse
from tests.test_host_ports_stream import _host_with_stream


def chunk(delta):
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


@pytest.mark.asyncio
async def test_nous_live_thought_survives_reconnect_and_reopening(tmp_path, monkeypatch):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    run = Variant1RunContext.create(source="chat", run_id="nous-thought-test", session_id=sid)
    first, release = asyncio.Event(), asyncio.Event()

    class Response(_StreamResponse):
        async def aiter_lines(self):
            yield chunk({"reasoning_content": "Inspect **the file**.", "reasoning_details": [
                {"type": "reasoning.text", "text": "Inspect **the file**."},
                {"type": "reasoning.encrypted", "data": "OPAQUE_FIXTURE"}]})
            yield chunk({"reasoning": " Then verify it."})
            first.set()
            await release.wait()
            yield chunk({"content": "I’ll open the file.", "tool_calls": [
                {"index": 0, "id": "call-fixture", "type": "function",
                 "function": {"name": "ipython", "arguments": '{"code":"1"}'}}]})
            yield "data: [DONE]"

    monkeypatch.setattr(cloud.httpx, "AsyncClient", _client_for(Response([])))
    router = LLMRouter({}, str(tmp_path))
    host = _host_with_stream([])
    host.router.mode = "cloud"
    async def stream(messages, **kwargs):
        async for token in cloud.call_openai(router, messages, {"max_tokens": 64}, "fixture",
                model="tencent/hy4-flash:free", label="hermes", reasoning_sink=kwargs["reasoning_sink"],
                tool_call_sink=kwargs.get("tool_call_sink")):
            yield token
    host.router.stream = stream
    session = ConnectionSession(viewed_session_id=sid)
    session.active.turn_session_id = sid
    session.active.disclosed_tool_specs = [{"name": "ipython", "description": "Fixture", "parameters": {}}]
    updated = asyncio.Event()
    async def observe(frame):
        if frame.get("type") == "thinking" and frame.get("text", "").endswith("verify it."):
            updated.set()
    owner = SimpleNamespace(send_json=AsyncMock(side_effect=observe))
    with bind_run_context(run):
        task = asyncio.create_task(build_task_turn_ports(host, owner, session).loop.stream([], 64, None))
    try:
        await asyncio.wait_for(first.wait(), 3)
        await asyncio.wait_for(updated.wait(), 2)
        assert not task.done()
        frame = owner.send_json.await_args_list[0].args[0]
        assert (frame["type"], frame["summary_source"], frame["status"]) == ("thinking", "provider_reasoning", "running")
        assert frame["text"] == "Inspect **the file**."
        live = activity.run_snapshot(sid, run.run_id)
        assert [(s["source"], s["detail"]) for s in live["steps"]] == [("provider_reasoning", "Inspect **the file**. Then verify it.")]
        assert activity.run_snapshot(sid+"-other", run.run_id) is None
    finally:
        release.set()
        result = await task
    frames = [call.args[0] for call in owner.send_json.await_args_list if call.args[0]["type"] == "thinking"]
    assert len({row["summary_id"] for row in frames}) == 1
    assert frames[-1]["status"] == "done" and frames[-1]["summary_revision"] > frames[0]["summary_revision"]
    assert result.text == "I’ll open the file."
    sessions.append_messages(sid, _durable_turn_messages(session, "Inspect the file", "Verified.", mood="neutral"))
    saved = open_sessions(tmp_path / "chats").get_session(sid)["messages"][-1]
    assert saved["text"] == "Verified."
    thought, narration = saved["steps"]
    assert (thought["id"], thought["source"], thought["status"], thought["detail"]) == (
        frame["summary_id"], "provider_reasoning", "done", "Inspect **the file**. Then verify it.")
    assert narration["kind"] == "text" and narration["detail"] == result.text
    assert "OPAQUE_FIXTURE" not in json.dumps(saved)
    assert thought["detail"] not in json.dumps(sessions.context_projection_view(sid)["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "policy", "eligible"), [
    ("hermes", {"reasoning": {"enabled": True}}, True),
    ("hermes", {"reasoning": {"enabled": False}}, False),
    ("hermes", {"reasoning_effort": "none"}, False),
    ("openai", {}, True),
    ("openai", {"reasoning": {"exclude": True}}, False),
])
async def test_only_enabled_plaintext_carriers_are_displayed(tmp_path, monkeypatch, provider, policy, eligible):
    lines = [chunk({"thinking":"Readable with signature. ","signature":"SIG_FIXTURE"}),
             chunk({"reasoning_content": "Direct text. "}), chunk({"reasoning_details": [
        {"type":"reasoning.encrypted","data":"OPAQUE_FIXTURE"},
        {"type":"reasoning.text","text":"Readable detail. ", "signature":"SIGNATURE_FIXTURE"},
        {"type":"reasoning.text","text":"Returned text", "encrypted_content":"CIPHER_FIXTURE"},
        {"type":"reasoning.summary","summary":"Provider summary"},
        {"type":"unknown","text":"HIDDEN_FIXTURE"}]}), chunk({"content":"Answer"}), "data: [DONE]"]
    monkeypatch.setattr(cloud.httpx, "AsyncClient", _client_for(_StreamResponse(lines)))
    router = LLMRouter({}, str(tmp_path))
    events = []
    monkeypatch.setattr(router, "prepare_cloud_payload", lambda value, **_: {**value, **policy})
    sink = ReasoningBuffer(SimpleNamespace(summary_event=events.append))
    reply = [part async for part in cloud.call_openai(router, [], {"max_tokens": 64}, "fixture",
             label=provider, model="tencent/hy4-flash:free", reasoning_sink=sink)]
    await sink.finish("done")
    assert reply == ["Answer"]
    assert [(e["source"], e["text"]) for e in events if e["status"] == "done"] == (
        [("provider_reasoning", "Readable with signature. Direct text. Readable detail. Returned text"),
         ("provider_summary", "Provider summary")] if eligible else [])
    assert "FIXTURE" not in json.dumps(events)


def test_nous_display_respects_default_off_and_explicit_chat_setting():
    model = "fixture/model"
    route = {"provider":"hermes", "model":model}
    router = SimpleNamespace(bound_model_route=lambda: route, model_reasoning_rule=lambda *_: {"default_enabled":False})
    assert not thoughts_enabled(router, None, model, {}, provider="hermes")
    route["reasoning_enabled"] = True
    assert thoughts_enabled(router, None, model, {}, provider="hermes")
    assert not thoughts_enabled(router, None, model, {"reasoning":{"enabled":False}}, provider="hermes")
    route["reasoning_enabled"] = False
    assert not thoughts_enabled(router, None, model, {"reasoning_effort":"high"}, provider="hermes")
    assert thoughts_enabled(router, None, model, {"reasoning_effort":"high"}, provider="openai")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_anthropic_thoughts_use_the_effective_request(tmp_path, monkeypatch, enabled):
    events = [
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "Check the schema."}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "OPAQUE_FIXTURE"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "Answer"}},
        {"type": "message_stop"},
    ]
    response = _StreamResponse(["data: " + json.dumps(event) for event in events])
    monkeypatch.setattr(cloud.httpx, "AsyncClient", _client_for(response))
    router = LLMRouter({}, str(tmp_path))
    thinking = {"type": "enabled", "budget_tokens": 1024} if enabled else {"type": "disabled"}
    monkeypatch.setattr(router, "prepare_cloud_payload", lambda body, **_: {**body, "thinking": thinking})
    frames = []
    sink = ReasoningBuffer(SimpleNamespace(summary_event=frames.append))
    answer = [part async for part in cloud.call_anthropic(
        router, [], {"max_tokens": 1024}, "fixture", model="fixture", reasoning_sink=sink,
    )]
    await sink.finish("done")
    assert answer == ["Answer"]
    assert [row["text"] for row in frames if row["status"] == "done"] == (["Check the schema."] if enabled else [])
    assert "OPAQUE_FIXTURE" not in json.dumps(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "discarded"])
async def test_thought_tail_is_bounded_and_failed_attempts_keep_their_identity(status):
    frames = []
    sink = ReasoningBuffer(SimpleNamespace(summary_event=frames.append))
    await sink.returned_thought("First ")
    await sink.returned_thought("x" * (SUMMARY_TEXT_LIMIT + 2000))
    await sink.finish(status)
    assert frames[-1]["status"] == status
    assert len(frames[-1]["text"]) == SUMMARY_TEXT_LIMIT and frames[-1]["text"].endswith("[Thought truncated]")
    retry = ReasoningBuffer(SimpleNamespace(summary_event=frames.append))
    await retry.returned_thought("Fresh attempt")
    assert frames[-1]["summary_id"] != frames[0]["summary_id"]
    assert frames[-1]["text"] == "Fresh attempt"


def test_returned_thought_snapshot_is_bound_to_its_admission():
    ctx = Variant1RunContext.create(source="chat", run_id="thought-run", session_id="thought-chat")
    with bind_run_context(ctx):
        activity.bind_live_text(1, ["Visible narration"], admission_id="original")
        activity.remember_run_display_step({"id":"thought", "kind":"thinking", "source":"provider_reasoning",
            "detail":"Returned thought", "segment":1}, admission_id="original")
    own = activity.run_snapshot("thought-chat", "thought-run", "original")
    assert own["steps"][0]["detail"] == "Returned thought" and own["text"] == "Visible narration"
    other = activity.run_snapshot("thought-chat", "thought-run", "new")
    assert other["steps"] == [] and other["text"] == ""
