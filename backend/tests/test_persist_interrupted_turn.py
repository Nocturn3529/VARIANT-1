"""Terminal paths outside the normal finalizer still persist their exchange."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import chat_pipeline
import pytest
from chat_finalize import _durable_turn_messages
from chat_session import ActiveTurn, ConnectionSession
from run_context import Variant1RunContext, current_run_context
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.support.conversation_sessions import open_sessions


def test_persist_unfinalized_turn_writes_messages(tmp_path):
    store = open_sessions(tmp_path / "chat_sessions")
    sid = store.create_session()

    class Srv:
        sessions = store
        hub = SimpleNamespace(broadcast=AsyncMock())

    session = ConnectionSession()
    session.active = ActiveTurn(
        turn_session_id=sid,
        turn_display_user_text="List everything under SCRATCH\\files",
        turn_display_attachments=[],
        turn_client_id="deck-test",
        turn_persisted=False,
    )
    ws = SimpleNamespace(send_json=AsyncMock())

    async def run():
        await chat_pipeline.persist_unfinalized_turn(
            Srv(), ws, session, "Task stopped.",
            meta={"source": "chat", "client_id": "deck-test"},
        )

    asyncio.run(run())
    sess = store.get_session(sid)
    assert sess is not None
    assert len(sess["messages"]) == 2
    assert sess["messages"][0]["role"] == "user"
    assert "SCRATCH" in sess["messages"][0]["text"]
    assert sess["messages"][1]["text"] == "Task stopped."
    assert session.active.turn_persisted is True
    # Second call is a no-op (no double append).
    asyncio.run(run())
    assert len(store.get_session(sid)["messages"]) == 2
    Srv.hub.broadcast.assert_awaited()


def test_persist_unfinalized_turn_skips_without_user_text(tmp_path):
    store = open_sessions(tmp_path / "chat_sessions")
    sid = store.create_session()
    session = ConnectionSession()
    session.active = ActiveTurn(
        turn_session_id=sid,
        turn_display_user_text="",
        turn_display_attachments=[],
        turn_client_id="",
        turn_persisted=False,
    )

    class Srv:
        sessions = store
        hub = SimpleNamespace(broadcast=AsyncMock())

    asyncio.run(chat_pipeline.persist_unfinalized_turn(
        Srv(), SimpleNamespace(send_json=AsyncMock()), session, "Task stopped.",
    ))
    assert store.get_session(sid)["messages"] == []


def test_persist_unfinalized_turn_preserves_error_mood(tmp_path):
    store = open_sessions(tmp_path / "chat_sessions")
    sid = store.create_session()

    host = SimpleNamespace(
        sessions=store,
        hub=SimpleNamespace(broadcast=AsyncMock()),
    )
    session = ConnectionSession()
    session.active = ActiveTurn(
        turn_session_id=sid,
        turn_display_user_text="Do the task",
        turn_display_attachments=[],
    )

    asyncio.run(chat_pipeline.persist_unfinalized_turn(
        host,
        SimpleNamespace(send_json=AsyncMock()),
        session,
        "Something went wrong: boom",
        mood="concerned",
    ))

    assistant = store.get_session(sid)["messages"][-1]
    assert assistant["mood"] == "concerned"


def test_persist_unfinalized_turn_falls_back_when_hub_transport_fails(tmp_path):
    store = open_sessions(tmp_path / "chat_sessions")
    sid = store.create_session()
    host = SimpleNamespace(
        sessions=store,
        hub=SimpleNamespace(
            broadcast=AsyncMock(side_effect=RuntimeError("hub offline")),
        ),
    )
    websocket = SimpleNamespace(send_json=AsyncMock())
    session = ConnectionSession()
    session.active = ActiveTurn(
        turn_session_id=sid,
        turn_display_user_text="Keep this turn",
        turn_display_attachments=[],
    )

    asyncio.run(chat_pipeline.persist_unfinalized_turn(
        host,
        websocket,
        session,
        "Persisted reply",
    ))

    assert session.active.turn_persisted is True
    websocket.send_json.assert_awaited_once()
    assert websocket.send_json.await_args.args[0]["type"] == "chat:appended"


def test_persistence_failure_does_not_complete_active_input_ticket(
    tmp_path, monkeypatch,
):
    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
        runtimes = SessionRuntimeRegistry(repository)
        runtimes.ensure_runtime(sid)
        ticket = runtimes.enqueue_input(
            sid, "use the corrected input", delivery="steer",
            ticket_id="ticket-persist-failure",
        )
        row = runtimes.claim_input(sid, "steer", run_id="run-a")
        session = ConnectionSession(viewed_session_id=sid)
        session.active = ActiveTurn(
            turn_session_id=sid,
            turn_display_user_text="start the task",
            turn_display_attachments=[],
        )
        runtimes.record_input_delivery(sid, session, row, None)
        host = SimpleNamespace(
            sessions=store,
            runtime_registry=runtimes,
            hub=SimpleNamespace(broadcast=AsyncMock()),
        )

        monkeypatch.setattr(
            store, "_append_segment_atomic",
            lambda _sid, _messages: (_ for _ in ()).throw(
                OSError("disk unavailable")
            ),
        )
        await chat_pipeline.persist_unfinalized_turn(
            host,
            SimpleNamespace(send_json=AsyncMock()),
            session,
            "Task stopped.",
        )

        durable = repository.get_ticket(ticket.ticket_id)
        assert durable.state == "transcript_failed"
        assert durable.terminal is True
        assert session.active.turn_persisted is False
        assert store.get_session(sid)["messages"] == []
        recovered = await runtimes.startup_reconcile(store)
        assert recovered["tickets_requeued"] == 0
        assert repository.get_ticket(ticket.ticket_id).state == "transcript_failed"

    asyncio.run(run())


def test_normal_finalizer_does_not_complete_ticket_when_transcript_write_fails(
    tmp_path, monkeypatch,
):
    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
        runtimes = SessionRuntimeRegistry(repository)
        runtimes.ensure_runtime(sid)
        ticket = runtimes.enqueue_input(
            sid, "correct the plan", delivery="steer",
            ticket_id="ticket-finalizer-failure",
        )
        row = runtimes.claim_input(sid, "steer", run_id="run-finalizer")
        session = ConnectionSession(viewed_session_id=sid)
        session.active = ActiveTurn(
            turn_session_id=sid,
            turn_display_user_text="start the task",
            turn_display_attachments=[],
        )
        runtimes.record_input_delivery(sid, session, row, None)
        websocket = SimpleNamespace(send_json=AsyncMock())
        ports = SimpleNamespace(
            io=SimpleNamespace(
                sessions=store,
                runtime_registry=runtimes,
                hub=SimpleNamespace(broadcast=AsyncMock()),
                emit=AsyncMock(),
            ),
            session=SimpleNamespace(set_last_user_text=lambda _text: None),
            tts=SimpleNamespace(tts_enabled=lambda: False),
        )
        monkeypatch.setattr(
            store, "_append_segment_atomic",
            lambda _sid, _messages: (_ for _ in ()).throw(
                OSError("disk unavailable")
            ),
        )

        await chat_pipeline.finish_chat_turn(
            ports,
            websocket,
            session,
            "start the task",
            "neutral",
            "finished",
            extract_memory=False,
        )

        durable = repository.get_ticket(ticket.ticket_id)
        assert durable.state == "transcript_failed"
        assert durable.terminal is True
        assert session.active.turn_persisted is False
        assert store.get_session(sid)["messages"] == []
        websocket.send_json.assert_awaited_once()
        assert websocket.send_json.await_args.args[0]["type"] == "done"
        recovered = await runtimes.startup_reconcile(store)
        assert recovered["tickets_requeued"] == 0
        assert repository.get_ticket(ticket.ticket_id).state == "transcript_failed"

    asyncio.run(run())


def _chat_context(_kind, _text, *, session, chat_transport, metadata):
    return Variant1RunContext.create(
        source="chat",
        run_id="chat-lifecycle-test",
        chat_session=session,
        chat_transport=chat_transport,
        metadata=metadata,
    )


def test_chat_task_clears_delivered_inputs_before_the_next_turn(tmp_path):
    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        session = ConnectionSession()
        seq = session.reserve_turn()
        session.active.turn_session_id = sid
        session.active.delivered_inputs = [{
            "text": "old steering",
            "assistant_text": "old intermediate answer",
        }]

        ports = SimpleNamespace(
            io=SimpleNamespace(sessions=store, emit=AsyncMock()),
            session=SimpleNamespace(
                make_run_context=_chat_context,
                handle_chat=AsyncMock(),
            ),
        )
        await chat_pipeline.chat_task(
            ports,
            SimpleNamespace(send_json=AsyncMock()),
            "first turn",
            session,
            turn_seq=seq,
        )

        assert session.busy is False
        assert session.active.delivered_inputs == []
        rows = _durable_turn_messages(
            session,
            "second turn",
            "second answer",
            mood="neutral",
        )
        assert [row["text"] for row in rows] == ["second turn", "second answer"]

    asyncio.run(run())


def test_chat_task_persists_visible_error_then_clears_turn(tmp_path):
    async def fail_chat(*_args, **_kwargs):
        raise RuntimeError("boom")

    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        hub = SimpleNamespace(broadcast=AsyncMock())
        session = ConnectionSession()
        seq = session.reserve_turn()
        session.active.turn_session_id = sid
        session.active.turn_display_user_text = "Do the task"
        session.active.turn_display_attachments = []

        ports = SimpleNamespace(
            io=SimpleNamespace(
                sessions=store,
                hub=hub,
                emit=AsyncMock(),
            ),
            session=SimpleNamespace(
                make_run_context=_chat_context,
                handle_chat=fail_chat,
            ),
        )
        await chat_pipeline.chat_task(
            ports,
            SimpleNamespace(send_json=AsyncMock()),
            "Do the task",
            session,
            turn_seq=seq,
        )

        messages = store.get_session(sid)["messages"]
        assert [row["role"] for row in messages] == ["user", "assistant"]
        assert "internal error" in messages[-1]["text"]
        assert "boom" not in messages[-1]["text"]
        assert messages[-1]["mood"] == "concerned"
        assert session.active.turn_session_id is None
        assert session.active.turn_persisted is False

    asyncio.run(run())


def test_failed_turn_parks_undelivered_durable_inputs(tmp_path):
    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
        runtimes = SessionRuntimeRegistry(repository)
        runtimes.ensure_runtime(sid)
        session = ConnectionSession(viewed_session_id=sid)
        seq = session.reserve_turn()
        session.active.turn_session_id = sid

        async def failed_chat(*_args, **_kwargs):
            runtimes.enqueue_input(
                sid,
                "durable follow-up",
                delivery="follow_up",
                ticket_id="ticket-failed-turn",
            )
            current_run_context().metadata["_terminal_status"] = "failed"

        hub = SimpleNamespace(broadcast=AsyncMock())
        ports = SimpleNamespace(
            io=SimpleNamespace(
                sessions=store,
                hub=hub,
                emit=AsyncMock(),
                runtime_registry=runtimes,
            ),
            session=SimpleNamespace(
                make_run_context=_chat_context,
                handle_chat=failed_chat,
            ),
        )

        await chat_pipeline.chat_task(
            ports,
            SimpleNamespace(send_json=AsyncMock()),
            "first turn",
            session,
            turn_seq=seq,
        )

        ticket = repository.get_ticket("ticket-failed-turn")
        assert ticket.state == "parked"
        assert ticket.error == "turn_failed_before_input_delivery"
        settle_events = [
            call.args[0]
            for call in hub.broadcast.await_args_list
            if call.args and call.args[0].get("type") == "chat:queue_snapshot"
        ]
        assert settle_events == [runtimes.queue_snapshot(sid)]

    asyncio.run(run())


def test_aborted_turn_parks_undelivered_durable_input(tmp_path):
    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
        runtimes = SessionRuntimeRegistry(repository)
        runtimes.ensure_runtime(sid)
        session = ConnectionSession(viewed_session_id=sid)
        seq = session.reserve_turn()
        session.active.turn_session_id = sid

        async def aborted_chat(*_args, **_kwargs):
            runtimes.enqueue_input(
                sid,
                "do this later",
                delivery="follow_up",
                ticket_id="ticket-aborted-turn",
            )
            raise asyncio.CancelledError

        ports = SimpleNamespace(
            io=SimpleNamespace(
                sessions=store,
                hub=SimpleNamespace(broadcast=AsyncMock()),
                emit=AsyncMock(),
                runtime_registry=runtimes,
            ),
            session=SimpleNamespace(
                make_run_context=_chat_context,
                handle_chat=aborted_chat,
            ),
        )

        with pytest.raises(asyncio.CancelledError):
            await chat_pipeline.chat_task(
                ports,
                SimpleNamespace(send_json=AsyncMock()),
                "first turn",
                session,
                turn_seq=seq,
            )

        ticket = repository.get_ticket("ticket-aborted-turn")
        assert ticket.state == "parked"
        assert ticket.error == "turn_cancelled_before_input_delivery"

    asyncio.run(run())


def test_cancel_after_durable_terminal_does_not_replace_reply_with_stop(tmp_path):
    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        store.append_messages(sid, [
            {"role": "user", "text": "finish normally"},
            {"role": "assistant", "text": "finished normally"},
        ])
        repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
        runtimes = SessionRuntimeRegistry(repository)
        runtimes.ensure_runtime(sid)
        session = ConnectionSession(viewed_session_id=sid)
        seq = session.reserve_turn()
        admission = runtimes.try_reserve_run(
            sid, attachment_id=session.attachment_id
        )
        session.active.runtime_admission_id = admission
        session.active.runtime_chat_id = sid
        session.active.turn_session_id = sid
        session.active.turn_display_user_text = "finish normally"
        session.active.turn_display_attachments = []
        entered_cleanup = asyncio.Event()

        async def post_terminal_cleanup(*_args, **_kwargs):
            session.active.turn_persisted = True
            session.active.terminal_sent = True
            entered_cleanup.set()
            await asyncio.Event().wait()

        hub = SimpleNamespace(broadcast=AsyncMock())
        ports = SimpleNamespace(
            io=SimpleNamespace(
                sessions=store,
                hub=hub,
                emit=AsyncMock(),
                runtime_registry=runtimes,
            ),
            session=SimpleNamespace(
                make_run_context=_chat_context,
                handle_chat=post_terminal_cleanup,
            ),
        )
        turn = asyncio.create_task(chat_pipeline.chat_task(
            ports,
            SimpleNamespace(send_json=AsyncMock()),
            "finish normally",
            session,
            turn_seq=seq,
            runtime_admission_id=admission,
        ))
        runtimes.bind_admission_task(admission, turn)
        await entered_cleanup.wait()

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert [row["text"] for row in store.get_session(sid)["messages"]] == [
            "finish normally", "finished normally",
        ]
        assert not any(
            call.args and call.args[0].get("type") == "done"
            for call in hub.broadcast.await_args_list
        )
        assert session.active.turn_session_id is None
        assert runtimes.active_admission(sid) == ""

    asyncio.run(run())


def test_queued_resume_reserves_a_new_admission_after_predecessor_finishes(tmp_path):
    async def run():
        store = open_sessions(tmp_path / "chat_sessions")
        sid = store.create_session()
        repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
        runtimes = SessionRuntimeRegistry(repository)
        runtimes.ensure_runtime(sid)
        session = ConnectionSession(viewed_session_id=sid)
        seq = session.reserve_turn()
        predecessor = runtimes.try_reserve_run(
            sid, attachment_id=session.attachment_id
        )
        assert predecessor
        session.active.runtime_admission_id = predecessor
        session.active.runtime_chat_id = sid
        lock = runtimes.writer_lock(sid)
        await lock.acquire()
        seen = []

        async def resumed_chat(*_args, **_kwargs):
            seen.append(session.active.runtime_admission_id)

        ports = SimpleNamespace(
            io=SimpleNamespace(
                sessions=store,
                hub=SimpleNamespace(broadcast=AsyncMock()),
                emit=AsyncMock(),
                runtime_registry=runtimes,
            ),
            session=SimpleNamespace(
                make_run_context=_chat_context,
                handle_chat=resumed_chat,
                snapshot_resume_state=lambda _sid: ({}, ""),
            ),
        )
        queued = asyncio.create_task(chat_pipeline.chat_task(
            ports,
            SimpleNamespace(send_json=AsyncMock()),
            "resume",
            session,
            resume=True,
            turn_seq=seq,
        ))
        await asyncio.sleep(0)
        runtimes.finish_run(predecessor, status="terminal")
        session.active.runtime_admission_id = ""
        lock.release()
        await queued

        assert len(seen) == 1
        assert seen[0] and seen[0] != predecessor
        assert session.busy is False
        assert runtimes.active_admission(sid) == ""

    asyncio.run(run())
