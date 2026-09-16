from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import ws_dispatch
from chat_session import ConnectionSession
from tests.support.conversation_sessions import open_sessions
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository


def _chat_stack(tmp_path, run_task):
    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    runtimes = SessionRuntimeRegistry(repository)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(runtimes.ensure_runtime)
    sid = store.create_session()
    chat = SimpleNamespace(
        run_task=run_task,
    )
    runtime = SimpleNamespace(
        chat=chat,
        sessions=store,
        session_runtimes=runtimes,
        kernel=SimpleNamespace(interrupt=AsyncMock(return_value={
            "status": "requested",
        })),
    )
    server = SimpleNamespace(
        require_runtime=lambda: runtime,
        hub=SimpleNamespace(broadcast=AsyncMock()),
        tools_cfg=None,
    )
    return server, runtimes, repository, ConnectionSession(viewed_session_id=sid), sid


def test_connection_session_does_not_own_a_second_active_input_queue():
    assert not hasattr(ConnectionSession, "enqueue_user_input")
    assert not hasattr(ConnectionSession, "drain_steering")
    assert not hasattr(ConnectionSession, "drain_follow_up")


@pytest.mark.asyncio
async def test_continue_parked_ticket_uses_normal_turn_and_commits_identity_once(tmp_path):
    from chat_finalize import persist_unfinalized_turn
    from chat_stream import stream_meta
    entered, release = asyncio.Event(), asyncio.Event()
    calls, starts = [], []
    async def run_task(socket, text, turn, **kwargs):
        calls.append((text, kwargs))
        starts.append(stream_meta(turn))
        turn.active.turn_session_id = sid
        turn.active.turn_display_user_text = text
        entered.set()
        await release.wait()
        assert await persist_unfinalized_turn(srv, socket, turn, "done")
        turn.busy = False
    srv, registry, repository, session, sid = _chat_stack(tmp_path, run_task)
    first = registry.enqueue_input(sid, "first queued task", delivery="steer")
    second = registry.enqueue_input(sid, "second queued task", delivery="follow_up")
    registry.park_queued_input_tickets(sid, reason="stop")
    revision = registry.queue_snapshot(sid)["revision"]
    socket = AsyncMock()
    message = {"type": "chat:queue:continue", "session_id": sid,
               "ticket_id": first.ticket_id, "expected_revision": revision, "request_id": "continue-1"}
    await ws_dispatch.HANDLERS[message["type"]](srv, socket, session, message)
    response = socket.send_json.await_args.args[0]
    assert response["type"] == "chat:queue_result" and response["accepted"]
    await asyncio.wait_for(entered.wait(), 1)
    assert starts[0]["ticket_id"] == first.ticket_id
    assert starts[0]["request_id"] == "continue-1"
    await ws_dispatch.HANDLERS[message["type"]](srv, socket, session, message)
    assert socket.send_json.await_args.args[0]["accepted"] is False
    assert len(calls) == 1 and calls[0][1]["ticket_id"] == first.ticket_id
    task = session.active.turn_task
    release.set()
    await task
    assert repository.get_ticket(first.ticket_id).state == "completed"
    assert repository.get_ticket(second.ticket_id).state == "parked"
    messages = srv.require_runtime().sessions.get_session(sid)["messages"]
    assert [row["text"] for row in messages] == ["first queued task", "done"]
    assert messages[0]["ticket_id"] == first.ticket_id


@pytest.mark.asyncio
async def test_ordinary_send_does_not_release_parked_queue(tmp_path):
    seen = []
    async def run_task(*args, **kwargs):
        seen.append(registry.claim_input(sid, "follow_up", run_id="ordinary"))
    srv, registry, repository, session, sid = _chat_stack(tmp_path, run_task)
    ticket = registry.enqueue_input(sid, "saved task", delivery="follow_up")
    registry.park_queued_input_tickets(sid, reason="stop")
    await ws_dispatch.HANDLERS["chat"](srv, AsyncMock(), session, {"type": "chat", "text": "new task"})
    task = session.active.turn_task
    await task
    assert seen == [None] and repository.get_ticket(ticket.ticket_id).state == "parked"


@pytest.mark.asyncio
async def test_queue_remove_is_revisioned_and_rejects_foreign_selection(tmp_path):
    srv, registry, repository, session, sid = _chat_stack(tmp_path, AsyncMock())
    ticket = registry.enqueue_input(sid, "saved task", delivery="follow_up")
    other = srv.require_runtime().sessions.create_session()
    socket = AsyncMock()
    message = {"type": "chat:queue:remove", "session_id": sid, "ticket_id": ticket.ticket_id,
               "expected_revision": registry.queue_snapshot(sid)["revision"], "request_id": "remove-1"}
    session.viewed_session_id = other
    await ws_dispatch.HANDLERS[message["type"]](srv, socket, session, message)
    assert socket.send_json.await_args.args[0]["error"] == "stale_queue_selection"
    assert repository.get_ticket(ticket.ticket_id).state == "queued"
    session.viewed_session_id = sid
    await ws_dispatch.HANDLERS[message["type"]](srv, socket, session, message)
    assert socket.send_json.await_args.args[0]["accepted"]
    assert repository.get_ticket(ticket.ticket_id).state == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("attachments", ["malformed", [{}] * 9, [{"kind":"text", "text":{}}]])
async def test_invalid_attachment_envelope_rejects_before_run_or_voice_cancellation(tmp_path, attachments):
    srv, runtimes, repository, session, sid = _chat_stack(tmp_path, AsyncMock())
    runtime = srv.require_runtime()
    runtime.voice = SimpleNamespace(cancel_transcription=AsyncMock())
    session.transcribe_task = object()
    socket = AsyncMock()
    await ws_dispatch.HANDLERS["chat"](srv, socket, session,
        {"text":"keep my draft", "attachments":attachments, "client_id":"attachment-client", "session_id":sid})
    reply = socket.send_json.await_args.args[0]
    assert reply["type"] == "chat:rejected" and reply["error"] == "attachment_preparation_failed"
    assert reply["session_id"] == sid and reply["client_id"] == "attachment-client"
    runtime.voice.cancel_transcription.assert_not_awaited()
    runtime.chat.run_task.assert_not_awaited()
    assert not runtimes.is_busy(sid) and not repository.list_tickets(sid)


@pytest.mark.asyncio
async def test_new_chat_send_cancels_stt_on_original_socket_after_turn_bag_split(tmp_path):
    from speech.service import SpeechService
    entered = asyncio.Event()
    captured = []
    async def run_task(_socket, _text, turn, **kwargs):
        captured.append(asyncio.current_task())
        entered.set()
    srv, runtimes, _, session, first = _chat_stack(tmp_path, run_task)
    second = srv.require_runtime().sessions.create_session()
    session.viewed_session_id = second
    session.active.runtime_chat_id = first
    session.busy = True
    voice_task = asyncio.create_task(asyncio.Event().wait())
    session.transcribe_task = voice_task
    session.transcribe_request_id = "speech-B"
    session.transcribe_session_id = second
    srv.require_runtime().voice = SpeechService(SimpleNamespace())
    socket = AsyncMock()
    try:
        await ws_dispatch.HANDLERS["chat"](srv, socket, session,
            {"text":"typed B", "session_id":second, "client_id":"typed-B"})
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.gather(*captured)
        assert voice_task.cancelled() and session.transcribe_task is None
        assert session.busy and not session.interrupt
        assert any(call.args[0].get("request_id") == "speech-B" and call.args[0].get("cancelled")
                   for call in socket.send_json.await_args_list)
    finally:
        voice_task.cancel()
        await asyncio.gather(voice_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_explicit_chat_target_survives_a_stale_connection_view(tmp_path):
    entered = asyncio.Event()
    captured = []
    async def run_task(_socket, _text, turn, **kwargs):
        captured.append((turn, asyncio.current_task()))
        entered.set()
    srv, runtimes, _, session, first = _chat_stack(tmp_path, run_task)
    second = srv.require_runtime().sessions.create_session()
    await ws_dispatch.HANDLERS['chat'](srv, AsyncMock(), session,
                                      {'type':'chat', 'session_id':second, 'text':'Only B'})
    await asyncio.wait_for(entered.wait(), 3)
    turn, task = captured[0]
    await task
    assert turn.active.runtime_chat_id == second
    assert session.viewed_session_id == first
    assert turn is not session


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chat', 'cancel'])
async def test_stale_admission_cannot_steer_or_stop_a_new_run(tmp_path, kind):
    srv, runtimes, repository, session, sid = _chat_stack(tmp_path, AsyncMock())
    admission = runtimes.try_reserve_run(sid, attachment_id=session.attachment_id)
    socket = AsyncMock()
    try:
        await ws_dispatch.HANDLERS[kind](srv, socket, session,
            {'type':kind, 'session_id':sid, 'admission_id':'previous-admission', 'text':'stale'})
        assert socket.send_json.await_args.args[0]['error'] == 'stale_run'
        assert runtimes.active_admission(sid) == admission
        assert not session.interrupt
        assert not repository.list_tickets(sid)
    finally:
        runtimes.finish_run(admission, status='test-complete')


@pytest.mark.asyncio
async def test_admission_replacement_between_validation_and_enqueue_rejects_steer(tmp_path, monkeypatch):
    srv, runtimes, repository, session, sid = _chat_stack(tmp_path, AsyncMock())
    old = runtimes.try_reserve_run(sid, attachment_id=session.attachment_id)
    original = ws_dispatch._validated_active_input
    replacement = ""
    async def swap(socket, msg):
        nonlocal replacement
        runtimes.finish_run(old, status="test-complete")
        replacement = runtimes.try_reserve_run(sid, attachment_id=session.attachment_id)
        return await original(socket, msg)
    monkeypatch.setattr(ws_dispatch, "_validated_active_input", swap)
    socket = AsyncMock()
    try:
        await ws_dispatch.HANDLERS["chat"](srv, socket, session,
            {"type": "chat", "session_id": sid, "admission_id": old, "text": "stale steer"})
        assert socket.send_json.await_args.args[0]["error"] == "stale_run"
        assert not repository.list_tickets(sid)
        srv.require_runtime().kernel.interrupt.assert_not_awaited()
    finally:
        runtimes.finish_run(replacement or old, status="test-complete")


@pytest.mark.asyncio
async def test_stop_for_idle_viewed_chat_does_not_cancel_retained_other_turn(tmp_path):
    from speech.service import SpeechService
    srv, runtimes, _, session, first = _chat_stack(tmp_path, AsyncMock())
    second = srv.require_runtime().sessions.create_session()
    session.viewed_session_id = second
    session.active.runtime_chat_id = first
    session.busy = True
    task = asyncio.create_task(asyncio.Event().wait())
    session.active.turn_task = task
    voice_task = asyncio.create_task(asyncio.Event().wait())
    preview_task = asyncio.create_task(asyncio.Event().wait())
    session.transcribe_task = voice_task
    session.transcribe_request_id = "speech-A"
    session.transcribe_session_id = first
    session.tts_preview_task = preview_task
    session.tts_preview_request_id = "preview-A"
    session.tts_preview_session_id = first
    srv.require_runtime().voice = SpeechService(SimpleNamespace())
    try:
        socket = AsyncMock()
        await ws_dispatch.HANDLERS["cancel"](srv, socket, session,
            {"type": "cancel", "session_id": second})
        assert not task.done() and not task.cancelling()
        assert not voice_task.cancelling() and not preview_task.cancelling()
        assert not session.interrupt and session.busy
        assert not socket.send_json.await_args.args[0]["accepted"]
    finally:
        for pending in (task, voice_task, preview_task): pending.cancel()
        await asyncio.gather(task, voice_task, preview_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_annotation_does_not_rebind_the_viewed_chat(tmp_path):
    srv, runtimes, _, session, first = _chat_stack(tmp_path, AsyncMock())
    second = srv.require_runtime().sessions.create_session()
    session.viewed_session_id = second
    socket = AsyncMock()
    runtimes.attach(second, session.attachment_id, session, socket)
    await ws_dispatch.HANDLERS["chat:session:annotate"](srv, socket, session,
        {"type": "chat:session:annotate", "id": first})
    assert session.viewed_session_id == second
    assert socket.send_json.await_args.args[0]["ok"]


@pytest.mark.asyncio
async def test_cancelled_active_turn_uses_generic_stop_and_clears_state():
    started = asyncio.Event()

    async def active_turn():
        started.set()
        await asyncio.Event().wait()

    session = ConnectionSession()
    session.busy = True
    session.active.task = object()
    session.active.turn_client_id = "deck-1"
    session.active.turn_source = "chat"
    session.active.turn_task = asyncio.create_task(active_turn())
    await started.wait()
    websocket = AsyncMock()

    await ws_dispatch.HANDLERS["cancel"](
        MagicMock(), websocket, session, {"type": "cancel"},
    )

    payloads = [call.args[0] for call in websocket.send_json.await_args_list]
    assert payloads[0]["type"] == "cancelling"
    assert payloads[0]["accepted"] is True
    assert payloads[-1]["cancelled"] is True
    assert payloads[-1]["text"] == "Task stopped."
    assert session.active.turn_session_id is None
    assert session.active.delivered_inputs == []


@pytest.mark.asyncio
async def test_busy_plain_chat_queues_durable_steering_without_cancelling_active_turn(
    tmp_path,
):
    started = asyncio.Event()

    async def active_turn():
        started.set()
        await asyncio.Event().wait()

    async def unused_chat_task(*_args, **_kwargs):
        raise AssertionError("busy input must not start another turn")

    srv, _runtimes, repository, session, sid = _chat_stack(
        tmp_path, unused_chat_task,
    )
    session.busy = True
    session.active.turn_task = asyncio.create_task(active_turn())
    await started.wait()
    websocket = AsyncMock()

    await ws_dispatch.HANDLERS["chat"](
        srv, websocket, session,
        {"type": "chat", "text": "Use the other file.", "client_id": "deck"},
    )

    assert session.interrupt is False
    assert session.active.turn_task.cancelled() is False
    ticket = repository.list_tickets(sid)[0]
    assert ticket.text == "Use the other file."
    assert ticket.delivery == "steer"
    queued = websocket.send_json.await_args.args[0]
    assert queued.pop("queue") == _runtimes.queue_snapshot(sid)
    assert queued == {
        "type": "chat:queued",
        "id": ticket.ticket_id,
        "delivery": "steer",
        "queue_size": 1,
        "session_id": sid,
        "client_id": "deck",
    }
    srv.require_runtime().kernel.interrupt.assert_not_awaited()
    session.active.turn_task.cancel()
    await asyncio.gather(session.active.turn_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_valid_steer_after_losing_admission_race_does_not_interrupt_kernel(tmp_path, monkeypatch):
    srv, registry, repository, session, sid = _chat_stack(tmp_path, AsyncMock())
    admission = registry.try_reserve_run(sid)
    monkeypatch.setattr(registry, 'is_busy', lambda _sid: False)
    socket = AsyncMock()
    try:
        await ws_dispatch.HANDLERS['chat'](srv, socket, session, {
            'type':'chat', 'session_id':sid, 'text':'apply at boundary', 'client_id':'late-deck',
        })
        assert socket.send_json.await_args.args[0]['type'] == 'chat:queued'
        assert repository.list_tickets(sid)[0].text == 'apply at boundary'
        srv.require_runtime().kernel.interrupt.assert_not_awaited()
        srv.require_runtime().chat.run_task.assert_not_awaited()
    finally:
        registry.finish_run(admission, status='complete')


@pytest.mark.asyncio
async def test_chat_after_visible_done_waits_for_release_then_starts_new_turn(tmp_path):
    release = asyncio.Event()
    next_started = asyncio.Event()

    async def next_turn(*_args, **_kwargs):
        next_started.set()

    srv, _runtimes, repository, session, _sid = _chat_stack(
        tmp_path, next_turn,
    )
    session.busy = True
    session.active.terminal_sent = True

    async def finishing_turn():
        await release.wait()
        session.busy = False
        session.clear_active_turn()

    session.active.turn_task = asyncio.create_task(finishing_turn())
    websocket = AsyncMock()

    dispatch = asyncio.create_task(ws_dispatch.HANDLERS["chat"](
        srv,
        websocket,
        session,
        {"type": "chat", "text": "Start the next turn.", "client_id": "deck-next"},
    ))
    await asyncio.sleep(0)
    assert not dispatch.done()
    assert repository.list_tickets(session.viewed_session_id) == []

    release.set()
    await dispatch
    await asyncio.wait_for(next_started.wait(), timeout=1)
    payloads = [call.args[0] for call in websocket.send_json.await_args_list]
    assert not any(row.get("type") == "chat:queued" for row in payloads)


@pytest.mark.asyncio
async def test_second_deck_after_visible_done_becomes_new_turn_not_late_ticket(tmp_path):
    release = asyncio.Event()
    next_started = asyncio.Event()

    async def next_turn(*_args, **_kwargs):
        next_started.set()

    srv, runtimes, repository, owner, sid = _chat_stack(tmp_path, next_turn)
    observer = ConnectionSession(viewed_session_id=sid)
    owner_ws = AsyncMock()
    observer_ws = AsyncMock()
    runtimes.attach(sid, owner.attachment_id, owner, owner_ws)
    runtimes.attach(sid, observer.attachment_id, observer, observer_ws)
    admission = runtimes.try_reserve_run(
        sid, attachment_id=owner.attachment_id,
    )
    assert admission
    owner.busy = True
    owner.active.runtime_chat_id = sid
    owner.active.runtime_admission_id = admission
    owner.active.terminal_sent = True

    async def finishing_turn():
        await release.wait()
        runtimes.finish_run(admission, status="ok")
        owner.busy = False
        owner.clear_active_turn()

    owner.active.turn_task = asyncio.create_task(finishing_turn())
    runtimes.bind_admission_task(admission, owner.active.turn_task)
    dispatch = asyncio.create_task(ws_dispatch.HANDLERS["chat"](
        srv,
        observer_ws,
        observer,
        {"type": "chat", "text": "new turn", "client_id": "deck-observer"},
    ))
    await asyncio.sleep(0)
    assert repository.list_tickets(sid) == []

    release.set()
    await dispatch
    await asyncio.wait_for(next_started.wait(), timeout=1)
    assert repository.list_tickets(sid) == []


@pytest.mark.asyncio
async def test_busy_explicit_follow_up_uses_durable_queue(tmp_path):
    async def unused_chat_task(*_args, **_kwargs):
        raise AssertionError("busy input must not start another turn")

    srv, _runtimes, repository, session, sid = _chat_stack(
        tmp_path, unused_chat_task,
    )
    session.busy = True
    websocket = AsyncMock()

    await ws_dispatch.HANDLERS["chat"](
        srv, websocket, session,
        {"type": "chat", "delivery": "follow_up", "text": "Summarize when done."},
    )

    ticket = repository.list_tickets(sid)[0]
    assert ticket.delivery == "follow_up"
    assert ticket.text == "Summarize when done."
    assert websocket.send_json.await_args.args[0]["delivery"] == "follow_up"
    srv.require_runtime().kernel.interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_chat_rejects_empty_active_input(tmp_path):
    async def unused_chat_task(*_args, **_kwargs):
        raise AssertionError("invalid input must not start another turn")

    srv, _runtimes, repository, session, sid = _chat_stack(
        tmp_path, unused_chat_task,
    )
    session.busy = True
    websocket = AsyncMock()

    await ws_dispatch.HANDLERS["chat"](
        srv, websocket, session,
        {"type": "chat", "text": "   "},
    )

    assert repository.list_tickets(sid) == []
    websocket.send_json.assert_awaited_once_with({
        "type": "chat:queue_rejected",
        "error": "active_input_empty",
        "session_id": sid,
        "client_id": "",
    })


@pytest.mark.asyncio
async def test_explicit_cancel_parks_durable_input_tickets(tmp_path):
    async def unused_chat_task(*_args, **_kwargs):
        return None

    srv, runtimes, repository, session, sid = _chat_stack(
        tmp_path, unused_chat_task,
    )
    runtimes.enqueue_input(sid, "redirect", delivery="steer")
    runtimes.enqueue_input(sid, "later", delivery="follow_up")
    websocket = AsyncMock()

    await ws_dispatch.HANDLERS["cancel"](
        srv, websocket, session, {"type": "cancel"},
    )

    assert repository.list_tickets(sid, states=("queued",)) == []
    assert len(repository.list_tickets(sid, states=("parked",))) == 2
    assert websocket.send_json.await_args_list[0].args[0]["cleared_inputs"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error", "reply_type"),
    [
        ({"text": "   "}, "empty_input", "chat:rejected"),
        ({
            "text": "Use this file",
            "attachments": [{
                "name": "note.txt",
                "kind": "text",
                "text": "attachment body",
            }],
        }, "active_input_attachments_not_supported", "chat:queue_rejected"),
    ],
)
async def test_admission_loss_reuses_active_input_validation(
    tmp_path, monkeypatch, payload, error, reply_type,
):
    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    runtimes = SessionRuntimeRegistry(repository)
    store = open_sessions(tmp_path / "chats")
    store.bind_runtime_lifecycle(runtimes.ensure_runtime)
    sid = store.create_session()
    owner = ConnectionSession(viewed_session_id=sid)
    admission = runtimes.try_reserve_run(
        sid, attachment_id=owner.attachment_id
    )
    assert admission
    # Reproduce two idle snapshots followed by another window winning the
    # atomic reservation before this request reaches try_reserve_run.
    monkeypatch.setattr(runtimes, "is_busy", lambda _sid: False)
    session = ConnectionSession(viewed_session_id=sid)
    websocket = AsyncMock()
    runtime = SimpleNamespace(
        sessions=store,
        session_runtimes=runtimes,
    )
    server = SimpleNamespace(require_runtime=lambda: runtime)

    await ws_dispatch.HANDLERS["chat"](
        server,
        websocket,
        session,
        {"type": "chat", **payload},
    )

    websocket.send_json.assert_awaited_once_with({
        "type": reply_type,
        "error": error,
        "session_id": sid,
        "client_id": "",
    })
    assert repository.list_tickets(sid) == []
    runtimes.finish_run(admission, status="test_complete")
