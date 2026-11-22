import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ws_dispatch
from tests.support.conversation_sessions import open_sessions


def _fork(sessions, chat, head, name):
    from time import time
    owner = sessions.context_head(chat)
    sid = "chat:" + name
    with sessions.repository._write() as conn:
        conn.execute("INSERT INTO conversation_branch(branch_id,conversation_id,name,base_node_id,head_node_id,"
                     "parent_branch_id,runtime_chat_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (name, owner["conversation_id"], name, head, head, owner["branch_id"], sid, time(), time()))
    return sid


def test_clock_rollback_preserves_recent_canonical_order_and_uncapped_summary(tmp_path, monkeypatch):
    sessions = open_sessions(tmp_path / "chats")
    chat = sessions.create_session("Clock")
    sessions.append_messages(chat, [{"role":"user","text":str(i)} for i in range(20_000)])
    old = sessions.context_head(chat)
    fork = _fork(sessions, chat, old["head_node_id"], "fork")
    with monkeypatch.context() as m:
        m.setattr("chat_sessions.service.time.time", lambda: 10.0)
        sessions.append_messages(chat, [{"role":"user","text":"new question"}, {"role":"assistant","text":"new reply"}])
    shown = sessions.get_session(chat)["messages"]
    assert len(shown) == 1000 and [r["text"] for r in shown[-2:]] == ["new question","new reply"]
    assert [r["content"] for r in sessions.canonical_context(chat)["messages"][-2:]] == ["new question","new reply"]
    assert shown[-1]["node_id"] == sessions.context_head(chat)["head_node_id"]
    monkeypatch.setattr(sessions, "_full_session", lambda *_: pytest.fail("summaries cannot hydrate transcripts"))
    monkeypatch.setattr(sessions.repository, "_node", lambda *_: pytest.fail("summaries cannot decode bodies"))
    assert {r["id"]:r["message_count"] for r in sessions.list_sessions()} == {chat:20002, fork:20000}


@pytest.mark.parametrize("fts", [True, False])
def test_title_only_hits_and_message_hits_resolve_to_reachable_branches(tmp_path, fts):
    sessions = open_sessions(tmp_path / "chats", max_messages=2)
    sessions.repository.fts_enabled = fts and sessions.repository.fts_enabled
    main = sessions.create_session("Original")
    sessions.append_messages(main, [{"role":"user","text":"shared ancestor"}])
    fork = _fork(sessions, main, sessions.context_head(main)["head_node_id"], "side")
    sessions.append_messages(main, [{"role":"user","text":"main only"}])
    sessions.append_messages(fork, [{"role":"assistant","text":"fork needle"}, {"role":"user","text":"later"}])
    sessions.rename(main, "Unmentioned capybara")
    title, = sessions.search("capybara")
    assert title["kind"] == "title" and title["session_id"] == main and not title.get("node_id")
    hit, = sessions.search("needle")
    assert hit["kind"] == "message" and hit["session_id"] == fork and hit["branch_id"] == "side"
    assert hit["node_id"] == sessions.get_session(fork)["messages"][0]["node_id"]
    assert sessions.history_window(main, hit["node_id"]) is None
    assert sessions.history_window(fork, hit["node_id"])["messages"][1]["text"] == "fork needle"


@pytest.mark.asyncio
@pytest.mark.parametrize("fts", [True, False])
async def test_history_window_bounds_content_and_hides_goal_and_peer_envelopes(tmp_path, fts):
    sessions = open_sessions(tmp_path / "chats", max_messages=2)
    sessions.repository.fts_enabled = fts and sessions.repository.fts_enabled
    chat = sessions.create_session("Display fixture")
    sessions.bind_peer_display(lambda origin: {"display_name":"Cedar", "content":"Cedar's public answer"},
                               lambda *_: None)
    sessions.append_messages(chat, [
        {"role":"user","text":"genuinely typed"},
        {"role":"user","text":"privategoalwire", "origin":{"kind":"goal","event":"continuation","goal_id":"goal"}},
        {"role":"user","text":"privatepeerwire Cedar's public answer",
         "origin":{"kind":"peer","peer_id":"peer-a","message_id":"msg-a"}},
        {"role":"assistant","text":("prefix " * 1800) + "needle-tail " + ("suffix " * 1200)},
        {"role":"user","text":"more"}, {"role":"assistant","text":"latest"}, {"role":"user","text":"last"},
    ])
    assert sessions.search("privategoalwire") == [] and sessions.search("privatepeerwire") == []
    peer_hit, = sessions.search("Cedar")
    assert peer_hit["snippet"] == "Cedar's public answer"
    hit, = sessions.search("needle")
    assert "needle" in hit["snippet"] and hit["node_id"] not in [r["node_id"] for r in sessions.get_session(chat)["messages"]]
    active = sessions.create_session("Other active chat")
    srv = SimpleNamespace(require_runtime=lambda: SimpleNamespace(sessions=sessions))
    ws = AsyncMock()
    message = {"session_id":chat, "node_id":hit["node_id"], "request_id":"history-read", "query":"needle"}
    await ws_dispatch.HANDLERS["chat:history:around"](srv, ws, SimpleNamespace(), message)
    frame = ws.send_json.await_args.args[0]
    assert frame["type"] == "chat:history:window" and frame["session_id"] == chat and frame["request_id"] == "history-read"
    assert frame["has_before"] and frame["has_after"] and frame["branch_id"] == hit["branch_id"]
    texts = [r["text"] for r in frame["messages"]]
    assert len(texts) == 5 and texts[:2] == ["Continuing goal", "Cedar's public answer"]
    assert "privategoalwire" not in json.dumps(frame) and "privatepeerwire" not in json.dumps(frame)
    target = frame["messages"][2]
    assert "needle-tail" in target["text"] and len(target["text"]) == 4096
    assert target["text_offset"] > 0 and target["text_total_chars"] > 8000
    assert sessions.get_active() == active
    await ws_dispatch.HANDLERS["chat:history:around"](srv, ws, SimpleNamespace(), {**message, "session_id":active})
    missing = ws.send_json.await_args.args[0]
    assert missing["error"] == "history_node_unavailable" and missing["messages"] == []


def test_missing_peer_display_never_falls_back_to_internal_text(tmp_path):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session("Peer fixture")
    sessions.append_messages(sid, [{"role":"user","text":"private orchestration instruction",
        "origin":{"kind":"peer","peer_id":"p","message_id":"m"}}])
    node = sessions.context_head(sid)["head_node_id"]
    assert sessions.search("orchestration") == []
    assert sessions.history_window(sid, node)["messages"][0]["text"] == "Peer message"
