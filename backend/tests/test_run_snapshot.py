"""A chat opened mid-run gets the run's steps and current partial text."""

from __future__ import annotations

import pytest

from observability import activity
from observability.activity import HUB, emit_activity
from run_context import Variant1RunContext, bind_run_context, clear_run_context


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    clear_run_context()
    monkeypatch.setattr(activity, "_LIVE_RUNS", activity.OrderedDict())

    async def capture(_msg):
        return None

    monkeypatch.setattr(HUB, "broadcast", capture)
    yield
    clear_run_context()


def _ctx(run_id="run-1", chat="chat-a"):
    return Variant1RunContext.create(source="chat", run_id=run_id, session_id=chat)


@pytest.mark.asyncio
async def test_snapshot_replays_tool_steps_narration_and_live_text_in_order():
    parts = ["Reading ", "the"]
    with bind_run_context(_ctx()):
        activity.bind_live_text(1, [])
        activity.remember_run_narration({
            "id": "text_1", "kind": "text", "label": "Narration",
            "detail": "Let me check.", "segment": 1, "status": "done", "ts": 1.0,
        })
        await emit_activity("tool:start", tool="ipython", status="running",
                            call_id="call-1", args_preview='{"code": "1"}')
        await emit_activity("tool:result", tool="ipython", status="ok",
                            call_id="call-1", text="1")
        activity.bind_live_text(2, parts)
        await emit_activity("tool:start", tool="read_file", status="running",
                            call_id="call-2")
    parts.append(" file")

    snapshot = activity.run_snapshot("chat-a", "run-1")
    assert snapshot["run_id"] == "run-1"
    assert [(step["kind"], step.get("call_id"), step["status"]) for step in snapshot["steps"]] == [
        ("text", None, "done"), ("tool", "call-1", "ok"), ("tool", "call-2", "running"),
    ]
    assert snapshot["steps"][1]["result_preview"] == "1"
    assert (snapshot["segment"], snapshot["text"]) == (2, "Reading the file")


@pytest.mark.asyncio
async def test_snapshot_belongs_only_to_the_chat_s_active_run():
    with bind_run_context(_ctx()):
        await emit_activity("tool:start", tool="ipython", call_id="call-1")
    assert activity.run_snapshot("chat-a", "other-run") is None
    assert activity.run_snapshot("chat-b", "run-1") is None
    with bind_run_context(_ctx()):
        await emit_activity("task:done", status="ok")
    assert activity.run_snapshot("chat-a", "run-1") is None
    with bind_run_context(_ctx(run_id="run-2")):
        await emit_activity("tool:start", tool="ipython", call_id="call-9")
    assert [step["call_id"] for step in activity.run_snapshot("chat-a", "run-2")["steps"]] == [
        "call-9",
    ]


@pytest.mark.asyncio
async def test_chat_session_payload_carries_the_snapshot_only_while_busy():
    from types import SimpleNamespace

    import ws_chat_sessions

    with bind_run_context(_ctx()):
        await emit_activity("tool:start", tool="ipython", call_id="call-1")
    state = {"active_run_id": "run-1"}
    runtimes = SimpleNamespace(
        snapshot=lambda _sid: dict(state),
        queue_snapshot=lambda _sid: {"items": []},
    )
    srv = SimpleNamespace(require_runtime=lambda: SimpleNamespace(
        sessions=SimpleNamespace(get_session=lambda sid: {"id": sid, "messages": []}),
        session_runtimes=runtimes, kernel=None, session_control=None,
    ))

    busy = ws_chat_sessions._session_payload(srv, "chat-a")
    assert [step["call_id"] for step in busy["run_snapshot"]["steps"]] == ["call-1"]
    state["active_run_id"] = ""
    assert "run_snapshot" not in ws_chat_sessions._session_payload(srv, "chat-a")


@pytest.mark.asyncio
async def test_reopening_during_tools_shows_narration_once_not_as_reply_text():
    with bind_run_context(_ctx()):
        activity.bind_live_text(1, ["Let me check."], admission_id="adm-1")
        activity.remember_run_narration({
            "id": "text_1", "kind": "text", "label": "Narration",
            "detail": "Let me check.", "segment": 1, "status": "done", "ts": 1.0,
        })
        await emit_activity("tool:start", tool="ipython", call_id="call-1")
    during_tools = activity.run_snapshot("chat-a", "run-1", "adm-1")
    assert [step["kind"] for step in during_tools["steps"]] == ["text", "tool"]
    assert (during_tools["segment"], during_tools["text"]) == (0, "")

    with bind_run_context(_ctx()):
        activity.bind_live_text(2, ["Next"], admission_id="adm-1")
    next_call = activity.run_snapshot("chat-a", "run-1", "adm-1")
    assert (next_call["segment"], next_call["text"]) == (2, "Next")
    assert next_call["revision"] > during_tools["revision"]


@pytest.mark.asyncio
async def test_partial_text_of_another_admission_is_not_offered():
    with bind_run_context(_ctx()):
        activity.bind_live_text(1, ["Old admission output"], admission_id="old")
    stale = activity.run_snapshot("chat-a", "run-1", "new")
    assert stale["admission_id"] == "new"
    assert (stale["segment"], stale["text"]) == (0, "")
    own = activity.run_snapshot("chat-a", "run-1", "old")
    assert (own["segment"], own["text"]) == (1, "Old admission output")
