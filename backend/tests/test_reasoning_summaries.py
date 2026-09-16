import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reasoning_summaries import ReasoningBuffer, ResponsesSummary
from tests.test_host_ports_stream import _host_with_stream
from tests.support.conversation_sessions import open_sessions
from chat_session import ConnectionSession
from chat_finalize import _durable_turn_messages, _display_transcript, persist_unfinalized_turn
from host_ports import build_task_turn_ports


def test_public_summary_snapshots_dedupe_without_decoding_private_content():
    summary = ResponsesSummary()
    events = [
        {"type":"response.reasoning_summary_text.delta", "item_id":"rs", "output_index":0,
         "summary_index":0, "sequence_number":1, "delta":"Plan "},
        {"type":"response.reasoning_summary_text.delta", "item_id":"rs", "output_index":0,
         "summary_index":0, "sequence_number":1, "delta":"Plan "},
        {"type":"response.reasoning_summary_text.delta", "item_id":"rs", "output_index":0,
         "summary_index":0, "sequence_number":2, "delta":"the work."},
        {"type":"response.reasoning_summary_text.done", "item_id":"rs", "summary_index":0, "text":"Plan the work."},
        {"type":"response.reasoning_text.delta", "delta":"PRIVATE_NOT_A_SUMMARY"},
        {"type":"response.output_item.done", "item":{"type":"reasoning", "id":"rs",
            "summary":[{"type":"summary_text", "text":"Plan the work."}],
            "content":[{"type":"reasoning_text", "text":"PRIVATE_CONTENT"}], "encrypted_content":"OPAQUE"}},
        {"type":"response.completed", "response":{"output":[{"type":"reasoning", "id":"rs",
            "summary":[{"type":"summary_text", "text":"Plan the work."}]}]}},
    ]
    assert sum(summary.observe(event) for event in events) == len(b"Plan the work.")
    sink = ReasoningBuffer();summary.publish(sink)
    assert sink.public_text() == "Plan the work."
    assert "PRIVATE" not in sink.text() and "OPAQUE" not in sink.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["codex", "xai"])
@pytest.mark.parametrize("form", ["delta", "done", "item", "response"])
async def test_responses_adapters_emit_only_explicit_public_summary(tmp_path, monkeypatch, transport, form):
    from llm_router import LLMRouter
    import llm_openai_codex_responses as codex
    import llm_xai_responses as xai
    from tests.test_cloud_stream_reliability import _StreamResponse, _client_for
    module = codex if transport == "codex" else xai
    item = {"type":"reasoning", "id":"rs", "summary":[{"type":"summary_text", "text":"Public summary"}],
            "content":[{"type":"reasoning_text", "text":"Private payload"}], "encrypted_content":"Opaque replay"}
    events = [{"type":"response.reasoning_text.delta", "delta":"Private payload"}]
    if form == "delta":
        events.append({"type":"response.reasoning_summary_text.delta", "item_id":"rs", "output_index":0, "summary_index":0, "delta":"Public summary"})
    elif form == "done":
        events.append({"type":"response.reasoning_summary_text.done", "item_id":"rs", "output_index":0, "summary_index":0, "text":"Public summary"})
    elif form == "item":
        events.append({"type":"response.output_item.done", "output_index":0, "item":item})
    events.extend([{"type":"response.output_text.delta", "delta":"Answer"},
                   {"type":"response.completed", "response":{"output":[item]}}])
    monkeypatch.setattr(module.httpx, "AsyncClient", _client_for(_StreamResponse(["data: " + json.dumps(event) for event in events])))
    router = LLMRouter({},str(tmp_path));sink=ReasoningBuffer()
    fn = codex.call_openai_codex_responses if transport == "codex" else xai.call_xai_responses
    text = [token async for token in fn(router,[{"role":"user","content":"fixture"}],{"max_tokens":64},"fake",reasoning_sink=sink)]
    assert text == ["Answer"]
    assert sink.public_text() == "Public summary"


@pytest.mark.asyncio
async def test_retry_buffer_keeps_summary_type_and_discards_failed_attempt(monkeypatch):
    import llm_cloud_stream as cloud
    from model_providers import CredentialLease, ProviderRequestError
    from tests.test_cloud_stream_reliability import _router, IPYTHON_PROVIDER_SPEC
    attempts=[]
    async def native(*args,**kwargs):
        sink=args[9]
        attempts.append(1)
        sink.summary("discarded" if len(attempts)==1 else "retained")
        sink("private")
        if len(attempts)==1:
            raise ProviderRequestError("openai","temporary failure",status_code=503)
        kwargs["stream_diagnostics"].note_finish_reason("stop")
        yield "ok"
    monkeypatch.setattr(cloud,"call_cloud_once",native)
    monkeypatch.setattr(cloud.asyncio,"sleep",AsyncMock())
    router=_router()
    router._credential_leases=lambda provider:[CredentialLease(provider,"fixture","fixture","fake",source="environment")]
    sink=ReasoningBuffer()
    assert [token async for token in router._call_cloud([{"role":"user","content":"fixture"}],{"max_tokens":64},reasoning_sink=sink,tools=[IPYTHON_PROVIDER_SPEC])] == ["ok"]
    assert sink.public_text()=="retained" and "discarded" not in sink.text()


@pytest.mark.asyncio
async def test_chat_emits_separate_completed_summaries_and_preserves_reload_annotations(tmp_path):
    owner=SimpleNamespace(send_json=AsyncMock())
    host=_host_with_stream([])
    session=ConnectionSession();session.active.turn_source="chat";session.active.turn_client_id="owner"
    public="A public summary longer than the old 400 character limit. "*30
    async def stream(_messages,**kwargs):
        kwargs["reasoning_sink"]("PRIVATE_NOT_A_SUMMARY")
        kwargs["reasoning_sink"].summary(public)
        yield "answer"
    host.router.stream=stream
    ports=build_task_turn_ports(host,owner,session)
    await ports.loop.stream([],128,None)
    await ports.loop.stream([],128,None)
    frames=[call.args[0] for call in owner.send_json.await_args_list if call.args[0]["type"]=="thinking"]
    assert len(frames)==2 and frames[0]["summary_id"] != frames[1]["summary_id"]
    assert all(frame["summary_source"]=="provider_summary" and frame["source"]=="chat" and frame["status"]=="done" for frame in frames)
    assert all(frame["text"]==public.strip() for frame in frames)
    host.hub.broadcast.assert_not_awaited()
    sessions=open_sessions(tmp_path / "chats");sid=sessions.create_session()
    transcript=_durable_turn_messages(session,"task","answer",mood="neutral")
    sessions.append_messages(sid,transcript)
    emitted=_display_transcript(sessions,transcript,sid)[-1]["steps"]
    assert [step["id"] for step in emitted]==[frame["summary_id"] for frame in frames]
    sessions.annotate_last_assistant(sid,steps=[
        {**emitted[0],"detail":"client clipped this"},
        {"id":"cell-1","label":"Run Python","kind":"tool","ts":(emitted[0]["ts"]+emitted[1]["ts"])/2},
    ])
    reopened=open_sessions(tmp_path / "chats")
    saved=reopened.get_session(sid)["messages"][-1]["steps"]
    assert [step["id"] for step in saved]==[emitted[0]["id"],"cell-1",emitted[1]["id"]]
    assert [step["detail"] for step in saved if step["kind"]=="thinking"]==[public.strip(),public.strip()]
    assert "PRIVATE_NOT_A_SUMMARY" not in json.dumps(reopened.get_session(sid))
    context=reopened.context_projection_view(sid)["messages"]
    assert "summary" not in json.dumps(context) and "PRIVATE" not in json.dumps(context)
    session.active.turn_session_id=sid;session.active.turn_display_user_text="next task"
    assert await persist_unfinalized_turn(SimpleNamespace(sessions=reopened,hub=SimpleNamespace(broadcast=AsyncMock())),owner,session,"Stopped.")
    assert len(reopened.get_session(sid)["messages"][-1]["steps"])==2
    session.active.clear();assert session.active.provider_summaries==[]


@pytest.mark.asyncio
async def test_private_or_missing_reasoning_produces_no_summary_event():
    owner=SimpleNamespace(send_json=AsyncMock());host=_host_with_stream([]);session=ConnectionSession()
    async def stream(_messages,**kwargs):
        kwargs["reasoning_sink"]("PRIVATE_NOT_A_SUMMARY")
        yield "answer"
    host.router.stream=stream
    await build_task_turn_ports(host,owner,session).loop.stream([],128,None)
    assert not session.active.provider_summaries
    assert [call.args[0]["type"] for call in owner.send_json.await_args_list]==["token"]
