"""End-to-end check of the chat-session WebSocket protocol through the real
server dispatch loop (no LLM needed — session ops don't call the model).

Uses Starlette's TestClient WITHOUT entering its lifespan context, so the
backend's background loops (automations, events, etc.) never start.
The canonical SQL chat-session service is isolated by pytest fixtures.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("httpx")  # TestClient needs httpx; skip cleanly if absent.

from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, Mock, patch

import server


def _drain_until(ws, mtype, limit=40):
    for _ in range(limit):
        m = ws.receive_json()
        if m.get("type") == mtype:
            return m
    raise AssertionError(f"did not receive a {mtype!r} message")


def _request_sessions(ws):
    ws.send_json({"type": "chat:sessions"})
    return _drain_until(ws, "chat:sessions")


def test_chat_session_protocol_over_ws():
    client = TestClient(server.app)
    with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
        # The Main Deck owns hydration; the server pushes only shared state.
        sessions = _request_sessions(ws)
        assert "active_id" in sessions and "items" in sessions

        # New session: broadcast list (active flips) + a direct transcript reply.
        ws.send_json({"type": "chat:session:new", "request_id": "new-chat-test"})
        new_list = _drain_until(ws, "chat:sessions")
        new_sess = _drain_until(ws, "chat:session")
        new_id = new_sess["session"]["id"]
        assert new_list["active_id"] == new_id
        assert new_sess["session"]["messages"] == []
        assert new_sess["navigation"] == {"request_id": "new-chat-test", "requested_id": "",
                                          "effective_id": new_id, "status": "created"}

        ws.send_json({"type": "chat:session:new", "request_id": "new-chat-test"})
        replay_list = _drain_until(ws, "chat:sessions")
        replay = _drain_until(ws, "chat:session")
        assert replay["session"]["id"] == new_id
        assert len(replay_list["items"]) == len(new_list["items"])

        # Fetch by id.
        ws.send_json({"type": "chat:session:get", "id": new_id})
        got = _drain_until(ws, "chat:session")
        assert got["session"]["id"] == new_id

        # Rename propagates to the broadcast list.
        ws.send_json({"type": "chat:session:rename", "id": new_id, "title": "Renamed session"})
        renamed = _drain_until(ws, "chat:sessions")
        assert any(s["id"] == new_id and s["title"] == "Renamed session"
                   for s in renamed["items"])

        # Delete the active session: it disappears and active falls back.
        ws.send_json({"type": "chat:session:delete", "id": new_id})
        after_list = _drain_until(ws, "chat:sessions")
        assert all(s["id"] != new_id for s in after_list["items"])
        assert after_list["active_id"] != new_id
        fallback = _drain_until(ws, "chat:session")
        assert fallback["session"] is not None


def test_chat_project_protocol_is_correlated_and_persistent(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    client = TestClient(server.app)
    with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
        _request_sessions(ws)
        session_id = server.APP.require_runtime().sessions.create_session()
        ws.send_json({
            "type": "chat:project:set",
            "chat_id": session_id,
            "root": str(project_root),
            "request_id": "project-set-1",
        })
        result = _drain_until(ws, "chat:project:result")
        assert result == {
            "type": "chat:project:result",
            "chat_id": session_id,
            "request_id": "project-set-1",
            "ok": True,
            "project": {
                "root": str(project_root.resolve()), "name": "project",
            },
        }
        assert server.APP.require_runtime().sessions.get_project(session_id) == result["project"]

        ws.send_json({
            "type": "chat:project:set",
            "chat_id": session_id,
            "root": "relative/path",
            "request_id": "project-set-2",
        })
        rejected = _drain_until(ws, "chat:project:result")
        assert rejected["ok"] is False
        assert rejected["project"] is None
        assert rejected["error"]["code"] == "project_root_not_absolute"

        ws.send_json({
            "type": "chat:project:set",
            "chat_id": session_id,
            "root": None,
            "request_id": "project-clear-1",
        })
        cleared = _drain_until(ws, "chat:project:result")
        assert cleared["ok"] is True and cleared["project"] is None


@pytest.mark.asyncio
async def test_chat_project_commit_has_one_reply_when_broadcast_fails(tmp_path):
    import ws_chat_sessions

    handlers = {}

    def on(*names):
        def decorate(fn):
            for name in names:
                handlers[name] = fn
            return fn
        return decorate

    ws_chat_sessions.register(on)
    sessions = server.APP.require_runtime().sessions
    chat_id = sessions.create_session()
    project = tmp_path / "project"
    project.mkdir()
    websocket = SimpleNamespace(send_json=AsyncMock())
    runtime = SimpleNamespace(
        sessions=sessions,
        chat=SimpleNamespace(
            sessions_message=lambda: {"type": "chat:sessions"},
        ),
    )
    srv = SimpleNamespace(
        require_runtime=lambda: runtime,
        hub=SimpleNamespace(broadcast=AsyncMock(side_effect=RuntimeError("offline"))),
    )

    await handlers["chat:project:set"](
        srv, websocket, SimpleNamespace(),
        {
            "type": "chat:project:set", "chat_id": chat_id,
            "root": str(project), "request_id": "project-once",
        },
    )

    websocket.send_json.assert_awaited_once()
    reply = websocket.send_json.await_args.args[0]
    assert reply["ok"] is True and reply["request_id"] == "project-once"
    assert sessions.get_project(chat_id)["root"] == str(project.resolve())


def test_chat_search_protocol_over_ws():
    client = TestClient(server.app)
    with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
        _request_sessions(ws)

        # Seed a session with a distinctive exchange, then search for it.
        sessions = server.APP.require_runtime().sessions
        sid = sessions.create_session()
        sessions.append_messages(sid, [
            {"role": "user", "text": "please open the quokka spreadsheet"},
            {"role": "assistant", "text": "Opened the quokka spreadsheet."},
        ])

        ws.send_json({"type": "chat:search", "query": "quokka"})
        res = _drain_until(ws, "chat:search:results")
        assert res["query"] == "quokka"
        assert res["items"], "seeded message must be found"
        assert res["items"][0]["session_id"] == sid
        assert "quokka" in res["items"][0]["snippet"].lower()

        # Blank query returns no hits (client clears back to the session list).
        ws.send_json({"type": "chat:search", "query": "   "})
        empty = _drain_until(ws, "chat:search:results")
        assert empty["items"] == []


def test_chat_pin_archive_protocol_over_ws():
    client = TestClient(server.app)
    with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
        _request_sessions(ws)
        sessions = server.APP.require_runtime().sessions
        sid = sessions.create_session()
        sessions.append_messages(sid, [
            {"role": "user", "text": "pin me"},
            {"role": "assistant", "text": "ok"},
        ])

        ws.send_json({"type": "chat:session:pin", "id": sid, "value": True})
        pinned = _drain_until(ws, "chat:sessions")
        row = next(s2 for s2 in pinned["items"] if s2["id"] == sid)
        assert row.get("pinned") is True
        assert pinned["items"][0]["id"] == sid, "pinned session sorts first"

        ws.send_json({"type": "chat:session:archive", "id": sid, "value": True})
        archived = _drain_until(ws, "chat:sessions")
        row = next(s2 for s2 in archived["items"] if s2["id"] == sid)
        assert row.get("archived") is True

        ws.send_json({"type": "chat:session:pin", "id": sid, "value": False})
        unpinned = _drain_until(ws, "chat:sessions")
        row = next(s2 for s2 in unpinned["items"] if s2["id"] == sid)
        assert not row.get("pinned")


@pytest.mark.asyncio
async def test_detached_same_chat_switch_hydrates_without_changing_global_default(
    tmp_path, monkeypatch,
):
    import ws_chat_sessions
    from tests.support.conversation_sessions import open_sessions

    handlers = {}

    def on(*names):
        def decorate(fn):
            for name in names:
                handlers[name] = fn
            return fn
        return decorate

    ws_chat_sessions.register(on)
    sessions = open_sessions(tmp_path / "chats")
    pinned = sessions.create_session("Pinned", make_active=True)
    main_default = sessions.create_session("Main", make_active=True)
    attach = Mock()
    runtime = SimpleNamespace(
        sessions=sessions,
        session_runtimes=SimpleNamespace(attach=attach),
    )
    srv = SimpleNamespace(
        require_runtime=lambda: runtime,
        hub=SimpleNamespace(broadcast=AsyncMock()),
    )
    session = SimpleNamespace(
        view_role="detached_chat",
        viewed_session_id=pinned,
        attachment_id="detached-attachment",
    )
    websocket = SimpleNamespace(send_json=AsyncMock())
    monkeypatch.setattr(
        ws_chat_sessions,
        "_session_payload",
        lambda _srv, sid: {"id": sid, "project": None},
    )

    await handlers["chat:session:switch"](
        srv, websocket, session,
        {"type": "chat:session:switch", "id": pinned, "request_id": "hydrate"},
    )

    assert sessions.get_active() == main_default
    attach.assert_called_once_with(
        pinned, "detached-attachment", session, websocket,
    )
    srv.hub.broadcast.assert_not_awaited()
    reply = websocket.send_json.await_args.args[0]
    assert reply["type"] == "chat:session"
    assert reply["session"]["id"] == pinned
    assert reply["navigation"]["effective_id"] == pinned

    websocket.send_json.reset_mock()
    await handlers["chat:session:switch"](
        srv, websocket, session,
        {"type": "chat:session:switch", "id": main_default, "request_id": "wrong"},
    )
    rejected = websocket.send_json.await_args.args[0]
    assert rejected["type"] == "chat:switch:result"
    assert rejected["status"] == "rejected"
    assert sessions.get_active() == main_default


def test_mutation_authority_protocol_settles_and_rejects_with_request_id():
    client = TestClient(server.app)
    with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
        _request_sessions(ws)
        sid = server.APP.require_runtime().sessions.create_session()
        with patch.object(
            server.APP.require_runtime().catalog.mutation_authority,
            "set_mutation",
            return_value={
                "ok": True,
                "mutation_enabled": True,
                "authority_revision": 4,
            },
        ) as set_mutation:
            ws.send_json({
                "type": "chat:runtime:mutation:set",
                "id": sid,
                "enabled": True,
                "request_id": "mutation-request-1",
                "expected_revision": 0,
            })
            done = _drain_until(ws, "chat:runtime:mutation:set:done")
            assert done["id"] == sid
            assert done["enabled"] is True
            assert done["request_id"] == "mutation-request-1"
            assert done["authority_revision"] == 4
            assert set_mutation.call_args.kwargs["expected_revision"] == 0

        with patch.object(
            server.APP.require_runtime().catalog.mutation_authority,
            "set_mutation",
            side_effect=RuntimeError(
                "mutation authority CAS failed (4 != 3)"
            ),
        ):
            ws.send_json({
                "type": "chat:runtime:mutation:set",
                "id": sid,
                "enabled": False,
                "request_id": "mutation-request-2",
                "expected_revision": 3,
            })
            rejected = _drain_until(
                ws, "chat:runtime:mutation:set:rejected"
            )
            assert rejected["id"] == sid
            assert rejected["enabled"] is False
            assert rejected["request_id"] == "mutation-request-2"
            assert "authority CAS failed" in rejected["error"]
            snapshot = _drain_until(ws, "chat:session")
            assert snapshot["session"]["id"] == sid

        ws.send_json({
            "type": "chat:runtime:mutation:set",
            "id": sid,
            "enabled": "false",
            "request_id": "mutation-request-3",
            "expected_revision": 0,
        })
        malformed = _drain_until(
            ws, "chat:runtime:mutation:set:rejected"
        )
        assert malformed["request_id"] == "mutation-request-3"
        assert malformed["enabled"] is False
        assert "must be a boolean" in malformed["error"]

        ws.send_json({
            "type": "chat:runtime:mutation:set",
            "id": sid,
            "enabled": False,
            "request_id": "mutation-request-4",
        })
        missing_revision = _drain_until(
            ws, "chat:runtime:mutation:set:rejected"
        )
        assert missing_revision["request_id"] == "mutation-request-4"
        assert "expected_revision" in missing_revision["error"]


