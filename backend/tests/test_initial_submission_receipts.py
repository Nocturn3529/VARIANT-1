"""Reconcile one original composer ticket across sockets, races and restart."""
import asyncio
import sqlite3
import uuid
from unittest.mock import AsyncMock

import pytest

import ws_dispatch
from chat_session import ConnectionSession
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.test_ws_dispatch_chat import _chat_stack


def _ticket(session):
    return f"{session.attachment_id}.{uuid.uuid4()}"


async def _observe(server, session, chat, ticket):
    socket = AsyncMock()
    await ws_dispatch.HANDLERS["chat:execution:get"](server, socket, session, {
        "type": "chat:execution:get", "session_id": chat, "request_id": "read", "ticket_id": ticket,
    })
    reply = socket.send_json.await_args.args[0]
    assert (reply["session_id"], reply["request_id"], reply["ticket_id"]) == (chat, "read", ticket)
    assert reply["submission"]["ticket_id"] == ticket
    return reply["submission"]


@pytest.mark.asyncio
async def test_attachment_only_initial_ticket_tracks_one_admission_and_exact_settlement(tmp_path):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []
    async def run(_socket, text, session, **kw):
        calls.append((text, kw["ticket_id"], kw["source"]))
        adm = kw["runtime_admission_id"]
        registry.begin_run(adm, run_id="same-logical-run", thread_id="thread")
        entered.set()
        await finish.wait()
        repo.store_run_settlement(chat, adm, {"run_id": "same-logical-run", "settled": True}, "first reply")
        session.busy = False
    server, registry, repo, owner, chat = _chat_stack(tmp_path, run)
    registry.open_submission_origin(owner.attachment_id)
    observer = ConnectionSession(viewed_session_id=chat)
    ticket = _ticket(owner)
    initial_queue = registry.queue_snapshot(chat)
    incoming = {"type": "chat", "session_id": chat, "request_id": "send", "ticket_id": ticket,
                "text": "", "attachments": [{"kind": "text", "name": "note.txt", "text": "input"}]}
    socket = AsyncMock()
    await ws_dispatch.HANDLERS["chat"](server, socket, owner, incoming)
    task = owner.active.turn_task
    adm = registry.active_admission(chat)
    pre = await _observe(server, observer, chat, ticket)
    assert pre["status"] == "admitted" and pre["admission_id"] == adm and "run_id" not in pre
    await entered.wait()
    running = await _observe(server, observer, chat, ticket)
    assert running["status"] == "admitted" and running["run_id"] == "same-logical-run"
    await ws_dispatch.HANDLERS["chat"](server, socket, observer, incoming)
    assert socket.send_json.await_args.args[0]["submission"]["admission_id"] == adm
    await ws_dispatch.HANDLERS["chat"](server, socket, observer, {**incoming, "attachments": []})
    assert socket.send_json.await_args.args[0]["error"] == "submission_ticket_conflict"
    assert registry.queue_snapshot(chat) == initial_queue
    finish.set()
    await task
    assert calls == [("", ticket, "")]
    repo.store_run_settlement(chat, "different-admission", {"run_id": "same-logical-run", "settled": True}, "other")
    reopened = SessionRuntimeRegistry(SessionRuntimeRepository(repo.path))
    server.require_runtime().session_runtimes = reopened
    settled = await _observe(server, observer, chat, ticket)
    assert settled["settlement"]["admission_id"] == adm and settled["settlement"]["final_reply"] == "first reply"
    await ws_dispatch.HANDLERS["chat"](server, socket, observer, incoming)
    assert socket.send_json.await_args.args[0]["submission"]["status"] == "settled" and len(calls) == 1
    unrelated = server.require_runtime().sessions.create_session()
    assert await _observe(server, observer, unrelated, ticket) == {"ticket_id": ticket, "status": "unknown"}


@pytest.mark.asyncio
async def test_absence_requires_a_drained_origin_and_no_competing_ingress(tmp_path):
    server, registry, repo, original, chat = _chat_stack(tmp_path, AsyncMock())
    observer = ConnectionSession(viewed_session_id=chat)
    registry.open_submission_origin(original.attachment_id)
    ticket = _ticket(original)
    assert (await _observe(server, original, chat, ticket))["status"] == "absent"
    registry.open_submission_origin(observer.attachment_id)
    assert (await _observe(server, observer, chat, ticket))["status"] == "unknown"
    registry.retire_submission_origin(original.attachment_id)
    assert (await _observe(server, observer, chat, ticket))["status"] == "absent"
    assert (await _observe(server, observer, chat, "legacy-ticket"))["status"] == "unknown"
    registry.open_submission_origin("another-live-sender")
    assert (await _observe(server, observer, chat, ticket))["status"] == "unknown"
    restarted = SessionRuntimeRegistry(SessionRuntimeRepository(repo.path))
    restarted.open_submission_origin(observer.attachment_id)
    server.require_runtime().session_runtimes = restarted
    assert (await _observe(server, observer, chat, ticket))["status"] == "unknown"


@pytest.mark.asyncio
async def test_delayed_original_and_two_window_retry_do_not_enqueue_or_execute_twice(tmp_path, monkeypatch):
    waiting, resume, started, finish = (asyncio.Event() for _ in range(4))
    calls = []
    async def run(_socket, text, session, **kw):
        calls.append(kw["ticket_id"])
        registry.begin_run(kw["runtime_admission_id"], run_id="r", thread_id="t")
        started.set()
        await finish.wait()
        session.busy = False
    server, registry, _repo, first, chat = _chat_stack(tmp_path, run)
    other = ConnectionSession(viewed_session_id=chat)
    registry.open_submission_origin(first.attachment_id)
    registry.open_submission_origin(other.attachment_id)
    async def resolve(_refs, _owner):
        if not waiting.is_set():
            waiting.set()
            await resume.wait()
        return ""
    monkeypatch.setattr("browser_fabric.deck_tabs.resolve_tab_references", resolve)
    ticket = _ticket(first)
    msg = {"type": "chat", "session_id": chat, "ticket_id": ticket, "request_id": ticket, "text": "one",
           "attachments": [{"kind":"text", "text":"input"}],
           "references": [{"tabId": "test"}]}
    socket = AsyncMock()
    original = asyncio.create_task(ws_dispatch.HANDLERS["chat"](server, socket, first, msg))
    await waiting.wait()
    assert (await _observe(server, other, chat, ticket))["status"] == "unknown"
    await ws_dispatch.HANDLERS["chat"](server, socket, other, msg)
    await started.wait()
    resume.set()
    await original
    assert calls == [ticket] and not registry.queue_snapshot(chat)["items"]
    assert socket.send_json.await_args.args[0]["submission"]["status"] == "admitted"
    run_task = other.active.turn_task
    finish.set()
    await run_task


@pytest.mark.asyncio
async def test_busy_attachment_rejects_with_original_ticket_and_plain_queue_retry_is_idempotent(tmp_path):
    server, registry, repo, owner, chat = _chat_stack(tmp_path, AsyncMock())
    registry.attach(chat, owner.attachment_id, owner, AsyncMock())
    registry.open_submission_origin(owner.attachment_id)
    admission = registry.try_reserve_run(chat, attachment_id=owner.attachment_id)
    registry.begin_run(admission, run_id="existing-run", thread_id="t")
    ticket = _ticket(owner)
    socket = AsyncMock()
    message = {"session_id": chat, "request_id": "send", "ticket_id": ticket, "text": "draft"}
    await ws_dispatch.HANDLERS["chat"](server, socket, owner, {**message, "attachments": [{"kind": "text","text":"a"}]})
    rejection = socket.send_json.await_args.args[0]
    assert (rejection["type"], rejection["ticket_id"], rejection["request_id"]) == ("chat:queue_rejected",ticket,"send")
    assert repo.get_ticket(ticket) is None
    assert (await _observe(server, owner, chat, ticket))["status"] == "absent"
    await ws_dispatch.HANDLERS["chat"](server, socket, owner, message)
    assert socket.send_json.await_args.args[0]["type"] == "chat:queued"
    await ws_dispatch.HANDLERS["chat"](server, socket, owner, message)
    assert socket.send_json.await_args.args[0]["submission"]["status"] == "queued"
    assert len(registry.queue_snapshot(chat)["items"]) == 1
    registry.finish_run(admission, status="done")


@pytest.mark.parametrize("stop", ["inventory", "composer"])
@pytest.mark.asyncio
async def test_stop_before_coroutine_entry_is_proven_and_goal_cannot_restart(tmp_path, stop):
    from chat_goals import ChatGoalService, ChatGoalStore
    run = AsyncMock()
    server, registry, _repo, session, chat = _chat_stack(tmp_path, run)
    goals = ChatGoalService(server, ChatGoalStore(str(tmp_path / "goals.sqlite")))
    server.require_runtime().goals = goals
    registry.register_idle_listener(goals.notify_idle)
    goals.store.create(chat, "Do some work", creator_run_id="created-earlier")
    ticket = _ticket(session)
    socket = AsyncMock()
    await ws_dispatch.HANDLERS["chat"](server, socket, session, {"session_id": chat, "text":"work", "ticket_id":ticket})
    task = session.active.turn_task
    terminal = ("cancel", {"session_id": chat}) if stop == "composer" else (
        "chat:runtime:action", {"id": chat, "action": "stop_cell"})
    await ws_dispatch.HANDLERS[terminal[0]](server, socket, session, terminal[1])
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    run.assert_not_awaited()
    actual = await _observe(server, session, chat, ticket)
    assert actual["status"] == "rejected" and actual["reason"] == "cancelled_before_start"
    assert not registry.is_busy(chat) and not session.busy and goals.current(chat)["status"] == "paused"
    await goals.shutdown()


def test_preexisting_queue_and_exact_run_proof_survive_additive_ticket_upgrade(tmp_path):
    path = str(tmp_path / "runtime.sqlite")
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(path))
    ticket = registry.enqueue_input("chat", "keep this queued input", delivery="follow_up")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE astb_input_ticket DROP COLUMN initial_admission_id")
        conn.execute("ALTER TABLE astb_input_ticket DROP COLUMN submission_fingerprint")
    repo = SessionRuntimeRepository(path)
    reopened = SessionRuntimeRegistry(repo)
    assert reopened.submission_observation("chat", ticket.ticket_id, connection_id="socket")["status"] == "queued"
    assert repo.claim_ticket("chat", "follow_up", run_id="selection-placeholder")
    repo.transition_ticket(ticket.ticket_id, "completed",
                           proof={"chat_id":"chat", "run_id":"actual-run", "admission_id":"actual-admission"})
    assert reopened.submission_observation("chat", ticket.ticket_id, connection_id="socket")["status"] == "unknown"
    repo.store_run_settlement("chat", "actual-admission", {"run_id":"actual-run","settled":True}, "done")
    observed = reopened.submission_observation("chat", ticket.ticket_id, connection_id="socket")
    assert observed["status"] == "settled" and observed["run_id"] == "actual-run"
