from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import server_http
from chat_session import ConnectionSession
from tests.support.conversation_sessions import open_sessions
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository


@pytest.mark.asyncio
async def test_cancel_session_work_cancels_and_settles_owned_tasks():
    session = ConnectionSession()
    session.busy = True
    session.active.task = object()
    settled = []

    async def worker(label):
        try:
            await asyncio.Event().wait()
        finally:
            settled.append(label)

    session.active.turn_task = asyncio.create_task(worker("turn"))
    session.transcribe_task = asyncio.create_task(worker("transcribe"))
    await asyncio.sleep(0)

    await server_http.cancel_session_work(session)

    assert set(settled) == {"turn", "transcribe"}
    assert session.interrupt is True
    assert session.active.turn_task is None
    assert session.transcribe_task is None
    assert session.busy is False
    assert session.active.task is None
    assert session.active.delivered_inputs == []


@pytest.mark.asyncio
async def test_owner_disconnect_persists_and_notifies_remaining_chat_windows(
    tmp_path,
):
    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    runtimes = SessionRuntimeRegistry(repository)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(runtimes.ensure_runtime)
    sid = store.create_session()
    owner = ConnectionSession(viewed_session_id=sid)
    observer = ConnectionSession(viewed_session_id=sid)
    owner_socket = SimpleNamespace(send_json=AsyncMock())
    observer_socket = SimpleNamespace(send_json=AsyncMock())
    runtimes.attach(sid, owner.attachment_id, owner, owner_socket)
    runtimes.attach(sid, observer.attachment_id, observer, observer_socket)
    admission = runtimes.try_reserve_run(
        sid, attachment_id=owner.attachment_id
    )
    owner.reserve_turn()
    owner.active.runtime_chat_id = sid
    owner.active.runtime_admission_id = admission
    owner.active.turn_session_id = sid
    owner.active.turn_display_user_text = "keep this disconnected turn"
    owner.active.turn_display_attachments = []
    runtimes.enqueue_input(
        sid,
        "late follow-up",
        delivery="follow_up",
        ticket_id="ticket-owner-disconnect",
    )

    async def running():
        await asyncio.Event().wait()

    turn = asyncio.create_task(running())
    owner.active.turn_task = turn
    runtimes.bind_admission_task(admission, turn)
    hub = SimpleNamespace(broadcast=AsyncMock())
    runtime = SimpleNamespace(
        sessions=store,
        session_runtimes=runtimes,
    )
    host = SimpleNamespace(
        hub=hub,
        require_runtime=lambda: runtime,
    )

    detached = runtimes.detach(owner.attachment_id)
    await asyncio.gather(*detached, return_exceptions=True)
    await server_http.cancel_session_work(
        owner,
        host=host,
        websocket=owner_socket,
        runtime_registry=runtimes,
    )

    observer_events = [
        call.args[0] for call in observer_socket.send_json.await_args_list
    ]
    assert any(
        row.get("type") == "done" and row.get("cancelled")
        for row in observer_events
    )
    broadcasts = [call.args[0] for call in hub.broadcast.await_args_list]
    assert any(row.get("type") == "chat:appended" for row in broadcasts)
    settlement = next(
        row for row in broadcasts if row.get("type") == "chat:queue_snapshot"
    )
    assert [row["ticket_id"] for row in settlement["items"]] == ["ticket-owner-disconnect"]
    assert settlement["items"][0]["error"] == "owner_disconnect"
    assert [row["text"] for row in store.get_session(sid)["messages"]] == [
        "keep this disconnected turn",
        "Task stopped.",
    ]
    assert repository.get_ticket("ticket-owner-disconnect").state == "parked"
    assert runtimes.active_admission(sid) == ""


@pytest.mark.asyncio
async def test_initial_websocket_hydration_failure_releases_attachment(monkeypatch):
    class Socket:
        query_params = {"token": "secret"}

        async def accept(self):
            return None

        async def send_json(self, _value):
            return None

    class Hub:
        def __init__(self):
            self.active = set()

        def add(self, websocket):
            self.active.add(websocket)

        def remove(self, websocket):
            self.active.discard(websocket)

    class Runtimes:
        def __init__(self):
            self.attached = []
            self.detached = []

        def attach(self, chat_id, attachment_id, session, transport):
            self.attached.append((chat_id, attachment_id))

        def detach(self, attachment_id):
            self.detached.append(attachment_id)
            return []

    class BrokenTools:
        def state(self):
            raise RuntimeError("malformed tools hydration")

    runtimes = Runtimes()
    runtime = SimpleNamespace(
        chat=SimpleNamespace(orphaned_task_payload=lambda *_: None),
        tool_settings=BrokenTools(),
        session_runtimes=runtimes,
        sessions=SimpleNamespace(get_active=lambda: "chat-a"),
    )
    host = SimpleNamespace(
        hub=Hub(),
        require_runtime=lambda: runtime,
    )
    monkeypatch.setattr(server_http, "hello_payload", lambda *_args: {"type": "hello"})

    with pytest.raises(RuntimeError, match="malformed tools"):
        await server_http.websocket_endpoint(
            host,
            Socket(),
            auth_token="secret",
            session_factory=ConnectionSession,
        )

    assert host.hub.active == set()
    assert len(runtimes.attached) == 1
    assert runtimes.detached == [runtimes.attached[0][1]]


@pytest.mark.asyncio
async def test_detached_chat_disconnect_keeps_run_and_durable_output(
    tmp_path, monkeypatch,
):
    from chat_finalize import persist_unfinalized_turn

    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    runtimes = SessionRuntimeRegistry(repository)
    sessions = open_sessions(tmp_path / "chats")
    sessions.bind_runtime_lifecycle(runtimes.ensure_runtime)
    chat_id = sessions.create_session()
    session = ConnectionSession(viewed_session_id=chat_id)
    orphaned = AsyncMock(return_value={"should_not": "replay"})
    turn_holder = {}

    class Socket:
        query_params = {
            "token": "secret",
            "view_role": "detached_chat",
            "view_chat_id": chat_id,
        }

        def __init__(self):
            self.sent = []
            self.reads = 0
            self.disconnected = False
            self.closed = None

        async def accept(self):
            return None

        async def close(self, code):
            self.closed = code

        async def send_json(self, value):
            if self.disconnected:
                raise RuntimeError("socket closed")
            self.sent.append(value)

        async def receive_text(self):
            self.reads += 1
            if self.reads == 1:
                return json.dumps({"type": "browser:host:register"})
            admission = runtimes.try_reserve_run(
                chat_id, attachment_id=session.attachment_id,
            )
            session.reserve_turn()
            session.active.runtime_chat_id = chat_id
            session.active.turn_session_id = chat_id
            session.active.runtime_admission_id = admission
            session.active.turn_display_user_text = "finish after view closes"
            session.active.turn_display_attachments = []
            task = asyncio.create_task(asyncio.Event().wait())
            session.active.turn_task = task
            runtimes.bind_admission_task(admission, task)
            turn_holder["task"] = task
            self.disconnected = True
            raise server_http.WebSocketDisconnect()

    class Hub:
        def __init__(self):
            self.active = set()
            self.messages = []

        def add(self, websocket):
            self.active.add(websocket)

        def remove(self, websocket):
            self.active.discard(websocket)

        async def broadcast(self, message):
            self.messages.append(message)

    runtime = SimpleNamespace(
        sessions=sessions,
        session_runtimes=runtimes,
        chat=SimpleNamespace(orphaned_task_payload=orphaned),
        tool_settings=SimpleNamespace(state=lambda: {"type": "tools"}),
    )
    host = SimpleNamespace(hub=Hub(), require_runtime=lambda: runtime)
    socket = Socket()
    monkeypatch.setattr(
        server_http, "hello_payload",
        lambda _host, orphan: {"type": "hello", "orphan": orphan},
    )

    await server_http.websocket_endpoint(
        host, socket, auth_token="secret", session_factory=lambda: session,
    )

    assert orphaned.await_count == 0
    hello = next(row for row in socket.sent if row.get("type") == "hello")
    assert hello["orphan"] is None
    assert hello["view_role"] == "detached_chat"
    assert hello["view_chat_id"] == chat_id
    assert any(
        row.get("error") == "detached_chat_browser_host_forbidden"
        for row in socket.sent
    )
    assert runtimes.attachment_count(chat_id) == 0
    assert runtimes.active_admission(chat_id)
    assert not turn_holder["task"].done()

    durable = await persist_unfinalized_turn(
        host, socket, session, "completed while detached",
    )
    assert durable is True
    assert [row["text"] for row in sessions.get_session(chat_id)["messages"]] == [
        "finish after view closes", "completed while detached",
    ]

    await runtimes.shutdown()
    assert turn_holder["task"].cancelled()


@pytest.mark.asyncio
async def test_detached_chat_requires_existing_pinned_chat(tmp_path):
    sessions = open_sessions(tmp_path / "chats")

    class Socket:
        query_params = {
            "token": "secret",
            "view_role": "detached_chat",
            "view_chat_id": "missing-chat",
        }

        def __init__(self):
            self.accepted = False
            self.closed = None

        async def accept(self):
            self.accepted = True

        async def close(self, code):
            self.closed = code

    socket = Socket()
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(sessions=sessions),
    )

    await server_http.websocket_endpoint(
        host, socket, auth_token="secret", session_factory=ConnectionSession,
    )

    assert socket.accepted is False
    assert socket.closed == 1008
