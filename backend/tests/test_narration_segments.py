"""Narration between tool calls is split per model call and kept on reload."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat_finalize import _display_transcript, _durable_turn_messages
from chat_session import ConnectionSession
from host_ports import build_task_turn_ports
from session_catalog.service import IPYTHON_PROVIDER_SPEC
from tests.support.conversation_sessions import open_sessions
from tests.test_host_ports_stream import _host_with_stream


def _tool_call(call_id: str) -> list[dict]:
    return [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": "ipython", "arguments": json.dumps(
            {"category": "base", "code": "print(1)"}
        )},
    }]


@pytest.mark.asyncio
async def test_each_model_call_is_a_segment_and_tool_call_text_is_saved(tmp_path):
    owner = SimpleNamespace(send_json=AsyncMock())
    host = _host_with_stream([])
    session = ConnectionSession()
    session.active.turn_source = "chat"
    session.active.turn_client_id = "owner"
    session.active.disclosed_tool_specs = [IPYTHON_PROVIDER_SPEC]
    calls = iter([
        ("Let me check peers.", _tool_call("call-1")),
        ("Now the files.", _tool_call("call-2")),
        ("All done.", None),
    ])

    async def stream(_messages, **kwargs):
        text, tool_calls = next(calls)
        for word in text.split(" "):
            yield word + " "
        if tool_calls:
            kwargs["tool_call_sink"].add_openai_delta(tool_calls)

    host.router.stream = stream
    ports = build_task_turn_ports(host, owner, session)
    turns = [await ports.loop.stream([], 128, None) for _ in range(3)]
    assert [bool(turn.tool_calls) for turn in turns] == [True, True, False]

    tokens = [
        call.args[0] for call in owner.send_json.await_args_list
        if call.args[0]["type"] == "token"
    ]
    assert sorted({frame["segment"] for frame in tokens}) == [1, 2, 3]
    assert "".join(f["token"] for f in tokens if f["segment"] == 1).strip() == "Let me check peers."

    narration = session.active.text_segments
    assert [(row["segment"], row["detail"].strip()) for row in narration] == [
        (1, "Let me check peers."), (2, "Now the files."),
    ]

    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    transcript = _durable_turn_messages(session, "task", "All done.", mood="neutral")
    sessions.append_messages(sid, transcript)
    shown = _display_transcript(sessions, transcript, sid)[-1]
    assert shown["text"] == "All done."
    assert [step["detail"].strip() for step in shown["steps"] if step["kind"] == "text"] == [
        "Let me check peers.", "Now the files.",
    ]
    sessions.annotate_last_assistant(sid, steps=[{
        "id": "cell-1", "label": "Run Python", "kind": "tool",
        "ts": (narration[0]["ts"] + narration[1]["ts"]) / 2,
    }])
    reopened = open_sessions(tmp_path / "chats")
    saved = reopened.get_session(sid)["messages"][-1]["steps"]
    assert [step["kind"] for step in saved] == ["text", "tool", "text"]
    assert saved[0]["segment"] == 1 and saved[2]["segment"] == 2
    session.active.clear()
    assert session.active.text_segments == [] and session.active.model_segment == 0
