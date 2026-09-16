"""Live public summaries, attempt ownership and durable terminal state."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat_session import ConnectionSession
from chat_finalize import _durable_turn_messages, persist_unfinalized_turn
from host_ports import build_task_turn_ports
from reasoning_summaries import ReasoningBuffer, ResponsesSummary
from tests.test_host_ports_stream import _host_with_stream
from tests.support.conversation_sessions import open_sessions


def delta(text, *, sequence=1):
    return {"type": "response.reasoning_summary_text.delta", "item_id": "reasoning-1",
            "output_index": 0, "summary_index": 0, "sequence_number": sequence, "delta": text}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["codex", "xai"])
async def test_native_public_delta_is_visible_before_provider_finishes(tmp_path, monkeypatch, transport):
    from llm_router import LLMRouter
    import llm_openai_codex_responses as codex
    import llm_xai_responses as xai
    from tests.test_cloud_stream_reliability import _StreamResponse, _client_for
    module = codex if transport == "codex" else xai
    seen, release = asyncio.Event(), asyncio.Event()
    frames, tokens = [], []
    async def public(event):
        frames.append(event)
        seen.set()
    sink = ReasoningBuffer(summary_sink=SimpleNamespace(summary_event=public))
    class Response(_StreamResponse):
        async def aiter_lines(self):
            yield "data: " + json.dumps({"type": "response.reasoning_text.delta", "delta": "PRIVATE"})
            yield "data: " + json.dumps(delta("**Inspect** the files."))
            await release.wait()
            yield "data: " + json.dumps({"type": "response.output_text.delta", "delta": "Answer"})
            yield "data: " + json.dumps({"type": "response.completed", "response": {}})
    monkeypatch.setattr(module.httpx, "AsyncClient", _client_for(Response([])))
    fn = codex.call_openai_codex_responses if transport == "codex" else xai.call_xai_responses
    async def consume():
        async for token in fn(LLMRouter({}, str(tmp_path)), [{"role": "user", "content": "fixture"}],
                              {"max_tokens": 64}, "fixture", reasoning_sink=sink):
            tokens.append(token)
    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(seen.wait(), 1)
        assert not task.done() and tokens == []
        assert frames[0]["text"] == "**Inspect** the files." and frames[0]["status"] == "running"
        assert "PRIVATE" not in json.dumps(frames)
    finally:
        release.set()
        await task
    assert tokens == ["Answer"]


@pytest.mark.asyncio
async def test_retry_discards_visible_attempt_before_starting_a_new_identity(monkeypatch):
    import llm_cloud_stream as cloud
    from model_providers import CredentialLease, ProviderRequestError
    from tests.test_cloud_stream_reliability import _router, IPYTHON_PROVIDER_SPEC
    frames, attempts = [], []
    sink = ReasoningBuffer(summary_sink=SimpleNamespace(summary_event=lambda event: frames.append(event)))
    async def native(*args, **kwargs):
        summary = ResponsesSummary()
        attempt = len(attempts)
        attempts.append(summary.summary_id)
        summary.observe(delta("failed attempt" if attempt == 0 else "accepted attempt"))
        await summary.progress(args[9])
        summary.publish(args[9])
        args[9]("PRIVATE")
        if attempt == 0:
            raise ProviderRequestError("openai", "temporary", status_code=503)
        kwargs["stream_diagnostics"].note_finish_reason("stop")
        yield "answer"
    monkeypatch.setattr(cloud, "call_cloud_once", native)
    monkeypatch.setattr(cloud.asyncio, "sleep", AsyncMock())
    router = _router()
    router._credential_leases = lambda provider: [CredentialLease(provider, "fixture", "fixture", "fake", source="environment")]
    assert [part async for part in router._call_cloud([{"role": "user", "content": "fixture"}], {"max_tokens": 64},
            reasoning_sink=sink, tools=[IPYTHON_PROVIDER_SPEC])] == ["answer"]
    assert [event["status"] for event in frames] == ["running", "discarded", "running", "done"]
    assert frames[0]["summary_id"] == frames[1]["summary_id"] != frames[2]["summary_id"] == frames[3]["summary_id"]
    assert [event["summary_revision"] for event in frames] == [1, 2, 1, 2]
    assert sink.public_text() == "accepted attempt" and "failed attempt" not in sink.text()
    assert "PRIVATE" not in json.dumps(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["done", "cancelled", "discarded"])
async def test_host_stream_lifecycle_and_reopen_preserve_one_summary(tmp_path, outcome):
    owner = SimpleNamespace(send_json=AsyncMock())
    host = _host_with_stream([])
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    session = ConnectionSession(viewed_session_id=sid)
    session.active.turn_session_id = sid
    session.active.runtime_chat_id = sid
    session.active.turn_source = "chat"
    session.active.runtime_admission_id = "admission"
    first, release = asyncio.Event(), asyncio.Event()
    async def stream(_messages, **kwargs):
        summary = ResponsesSummary()
        summary.observe(delta("First"))
        await summary.progress(kwargs["reasoning_sink"])
        first.set()
        await release.wait()
        if outcome == "discarded":
            raise RuntimeError("provider failed")
        summary.observe(delta(" second", sequence=2))
        await summary.progress(kwargs["reasoning_sink"])
        summary.publish(kwargs["reasoning_sink"])
        yield "answer"
    host.router.stream = stream
    task = asyncio.create_task(build_task_turn_ports(host, owner, session).loop.stream([], 128, None))
    await asyncio.wait_for(first.wait(), 1)
    early = owner.send_json.await_args_list[0].args[0]
    assert early["type"] == "thinking" and early["status"] == "running"
    assert not task.done()
    if outcome == "cancelled":
        task.cancel()
    release.set()
    if outcome == "done":
        await task
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else RuntimeError):
            await task
    frames = [call.args[0] for call in owner.send_json.await_args_list if call.args[0]["type"] == "thinking"]
    assert len({event["summary_id"] for event in frames}) == 1
    assert frames[-1]["status"] == outcome
    assert [event["summary_revision"] for event in frames] == list(range(1, len(frames) + 1))
    assert all(event["source"] == "chat" and event["session_id"] == sid and event["admission_id"] == "admission" for event in frames)
    assert len({event["ts"] for event in frames}) == 1
    session.active.turn_display_user_text = "task"
    await persist_unfinalized_turn(SimpleNamespace(sessions=sessions, hub=SimpleNamespace(broadcast=AsyncMock())),
                                  owner, session, "answer" if outcome == "done" else "Stopped")
    reopened = open_sessions(tmp_path / "chats")
    row = reopened.get_session(sid)["messages"][-1]["steps"][0]
    assert row["id"] == frames[0]["summary_id"] and row["status"] == outcome
    assert row["summary_revision"] == frames[-1]["summary_revision"]
    assert not host.hub.broadcast.await_count


@pytest.mark.asyncio
async def test_explicit_clear_has_new_revision_and_cannot_be_resurrected_by_annotation(tmp_path):
    source = ResponsesSummary()
    buffer = ReasoningBuffer()
    source.observe(delta("Withdrawn text"))
    await source.progress(buffer)
    source.observe({"type": "response.reasoning_summary_text.done", "item_id": "reasoning-1", "text": ""})
    await source.progress(buffer)
    await buffer.finish("done")
    event = buffer.events[source.summary_id]
    assert event["text"] == "" and event["summary_revision"] == 3
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    session = ConnectionSession()
    session.active.provider_summaries = [{"id": source.summary_id, "kind": "thinking", "label": "Reasoning summary",
        "source": "provider_summary", "detail": "", "ts": event["ts"], "status": "done", "summary_revision": 3}]
    sessions.append_messages(sid, _durable_turn_messages(session, "task", "answer", mood="neutral"))
    sessions.annotate_last_assistant(sid, steps=[{"id": source.summary_id, "kind": "thinking", "label": "Reasoning summary", "detail": "Withdrawn text"}])
    row = sessions.get_session(sid)["messages"][-1]["steps"][0]
    assert not row.get("detail") and row["summary_revision"] == 3


@pytest.mark.asyncio
async def test_cloud_generator_close_marks_live_summary_cancelled(monkeypatch):
    import llm_cloud_stream as cloud
    from model_providers import CredentialLease
    from tests.test_cloud_stream_reliability import _router, IPYTHON_PROVIDER_SPEC
    async def native(*args, **kwargs):
        summary = ResponsesSummary()
        summary.observe(delta("Working"))
        await summary.progress(args[9])
        yield "partial"
        await asyncio.Event().wait()
    monkeypatch.setattr(cloud, "call_cloud_once", native)
    router = _router()
    router._credential_leases = lambda provider: [CredentialLease(provider, "fixture", "fixture", "fake", source="environment")]
    events = []
    sink = ReasoningBuffer(summary_sink=SimpleNamespace(summary_event=lambda event: events.append(event)))
    stream = router._call_cloud([{"role": "user", "content": "fixture"}], {"max_tokens": 64},
                               reasoning_sink=sink, tools=[IPYTHON_PROVIDER_SPEC])
    assert await anext(stream) == "partial"
    await stream.aclose()
    assert [event["status"] for event in events] == ["running", "cancelled"]


@pytest.mark.asyncio
async def test_terminal_summary_cannot_resume_from_late_snapshots():
    sink = ReasoningBuffer()
    event = {"summary_id": "summary_fixture", "summary_revision": 1, "text": "Public", "status": "running", "ts": 1}
    await sink.summary_event(event)
    await sink.finish("discarded")
    await sink.summary_event({**event, "summary_revision": 10})
    await sink.summary_event({**event, "summary_revision": 11, "status": "done"})
    assert sink.events["summary_fixture"]["status"] == "discarded"
    assert sink.events["summary_fixture"]["summary_revision"] == 2


@pytest.mark.asyncio
async def test_display_failure_does_not_abort_summary_source():
    calls = []
    def unavailable(event):
        calls.append(event)
        raise RuntimeError("view disconnected")
    sink = ReasoningBuffer(summary_sink=SimpleNamespace(summary_event=unavailable))
    summary = ResponsesSummary()
    summary.observe(delta("Public"))
    await summary.progress(sink)
    await sink.finish("done")
    assert len(calls) == 2 and sink.events[summary.summary_id]["status"] == "done"
