"""Chat admission and input recovery retain their original durable owners."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import chat_pipeline
from agent_engine.presets import chat_task_default
from agent_engine.runner import run_main_chat_task
from assistant_turn import AssistantTurn
from chat_finalize import _durable_turn_messages
from chat_session import ConnectionSession
from chat_setup_stage import prepare_chat_turn_stage
from model_runtime import engine_manager
from run_context import Variant1RunContext, bind_run_context
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.support.conversation_sessions import open_sessions
from test_agent_engine import FakePorts, SPEC


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["context", "writer"])
async def test_transferred_admission_is_released_before_body_entry(tmp_path, boundary):
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")))
    session = ConnectionSession(viewed_session_id=sid)
    seq = session.reserve_turn()
    admission = registry.try_reserve_run(sid, attachment_id=session.attachment_id)
    session.active.runtime_admission_id = admission
    session.active.runtime_chat_id = sid
    def context(*args, **kwargs):
        if boundary == "context":
            raise RuntimeError("injected context failure")
        return Variant1RunContext.create(source="chat", metadata={"_server_bound_kind": "chat"})
    ports = SimpleNamespace(io=SimpleNamespace(runtime_registry=registry, sessions=store),
                            session=SimpleNamespace(make_run_context=context))
    lock = registry.writer_lock(sid)
    await lock.acquire()
    try:
        task = asyncio.create_task(chat_pipeline.chat_task(ports, None, "test", session,
            turn_seq=seq, runtime_admission_id=admission))
        await asyncio.sleep(0)
        if boundary == "writer":
            task.cancel()
        with pytest.raises(RuntimeError if boundary == "context" else asyncio.CancelledError):
            await task
        assert not registry.is_busy(sid)
        assert not session.busy
        assert registry.try_reserve_run(sid, attachment_id=session.attachment_id)
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_one_socket_can_admit_two_chats_and_steer_only_the_viewed_chat(tmp_path):
    import ws_dispatch
    store = open_sessions(tmp_path / "chats")
    first, second = store.create_session(), store.create_session()
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")))
    connection = ConnectionSession(viewed_session_id=first)
    socket = SimpleNamespace(send_json=AsyncMock())
    release = asyncio.Event()
    started = {}
    async def run_task(ws, text, session, **kwargs):
        started[session.active.runtime_chat_id] = session
        await release.wait()
    runtime = SimpleNamespace(sessions=store, session_runtimes=registry,
        chat=SimpleNamespace(run_task=run_task, snapshot_resume_state=lambda sid: (None, "")),
        kernel=SimpleNamespace(interrupt=AsyncMock(return_value={"status": "idle"})))
    host = SimpleNamespace(require_runtime=lambda: runtime)
    try:
        await ws_dispatch.HANDLERS["chat"](host, socket, connection, {"text": "task A"})
        await asyncio.sleep(0)
        first_task = connection.active.turn_task
        registry.move_attachment(connection.attachment_id, second)
        await ws_dispatch.HANDLERS["chat"](host, socket, connection, {"text": "task B"})
        await asyncio.sleep(0)
        assert registry.is_busy(first) and registry.is_busy(second)
        assert started[first] is not started[second]
        await ws_dispatch.HANDLERS["chat"](host, socket, connection, {"text": "B constraint", "delivery": "steer"})
        assert registry.queued_input_count(first) == 0
        assert registry.queued_input_count(second) == 1
        assert connection.active.turn_task is first_task
        assert not any(call.args[0].get("type") == "chat:rejected" for call in socket.send_json.call_args_list)
    finally:
        release.set()
        tasks = [session.active.turn_task for session in started.values()]
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_from_socket_viewing_b_does_not_interrupt_its_running_a(tmp_path):
    import ws_dispatch
    store = open_sessions(tmp_path / "chats")
    first, second = store.create_session(), store.create_session()
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")))
    connection = ConnectionSession(viewed_session_id=first)
    other = ConnectionSession(viewed_session_id=second, attachment_id=connection.attachment_id)
    socket = SimpleNamespace(send_json=AsyncMock())
    tasks, admissions = [], []
    for sid, session in ((first, connection), (second, other)):
        registry.attach(sid, session.attachment_id, session, socket)
        admission = registry.try_reserve_run(sid, attachment_id=session.attachment_id)
        admissions.append(admission)
        session.reserve_turn()
        session.active.runtime_admission_id = admission
        session.active.runtime_chat_id = session.active.turn_session_id = sid
        session.active.turn_display_user_text = "task for " + sid
        task = asyncio.create_task(asyncio.Event().wait())
        tasks.append(task)
        session.active.turn_task = task
        registry.bind_admission_task(admission, task)
    connection.viewed_session_id = second
    host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(sessions=store, session_runtimes=registry),
                           hub=SimpleNamespace(broadcast=AsyncMock()))
    try:
        await ws_dispatch.HANDLERS["cancel"](host, socket, connection, {"type": "cancel"})
        assert not connection.interrupt
        assert not tasks[0].done() and registry.is_busy(first)
        assert tasks[1].cancelled() and not registry.is_busy(second)
        assert store.get_session(first)["messages"] == []
        assert store.get_session(second)["messages"][-1]["text"] == "Task stopped."
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for admission in admissions: registry.finish_run(admission, status="test cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation", ["before_start", "during_warmup", "cancel_warmup"])
async def test_admitted_chat_survives_navigation_before_setup(
    tmp_path, monkeypatch, navigation,
):
    store = open_sessions(tmp_path / "chats")
    first = store.create_session()
    second = store.create_session()
    registry = SessionRuntimeRegistry(
        SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    )
    session = ConnectionSession(viewed_session_id=first)
    sequence = session.reserve_turn()
    registry.attach(first, session.attachment_id, session)
    admission = registry.try_reserve_run(first, attachment_id=session.attachment_id)
    session.active.runtime_admission_id = admission
    session.active.runtime_chat_id = first
    entered = asyncio.Event()
    release = asyncio.Event()
    route = {"mode": "local"}
    monkeypatch.setattr(chat_pipeline, "session_model_route", lambda *_args: route)

    async def warmup(_router):
        entered.set()
        await release.wait()

    monkeypatch.setattr(engine_manager, "ensure_local_engine", warmup)

    def make_context(source, title, *, session, chat_transport, metadata):
        assert metadata["chat_id"] == first
        return Variant1RunContext.create(
            source=source, title=title, chat_session=session,
            chat_transport=chat_transport, metadata=metadata,
        )

    ports = SimpleNamespace(
        io=SimpleNamespace(
            sessions=store, runtime_registry=registry, emit=AsyncMock(),
            hub=SimpleNamespace(broadcast=AsyncMock()),
            router=SimpleNamespace(
                mode="local", engine_ready=False, cloud_route_ready=lambda: False,
            ),
        ),
        session=SimpleNamespace(make_run_context=make_context),
    )

    async def handle(*args, **kwargs):
        await chat_pipeline._handle_chat_body(ports, *args, **kwargs)

    ports.session.handle_chat = handle
    if navigation == "before_start":
        registry.move_attachment(session.attachment_id, second)
        release.set()
    turn = asyncio.create_task(chat_pipeline.chat_task(
        ports, SimpleNamespace(send_json=AsyncMock()), "sent from first chat",
        session, turn_seq=sequence, runtime_admission_id=admission,
    ))
    registry.bind_admission_task(admission, turn)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if navigation == "during_warmup":
            registry.move_attachment(session.attachment_id, second)
            release.set()
        if navigation == "cancel_warmup":
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, timeout=2)
        else:
            await asyncio.wait_for(turn, timeout=2)
    finally:
        if not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)

    if navigation == "cancel_warmup":
        assert not registry.is_busy(first)
        assert session.busy is False
        assert not registry.writer_lock(first).locked()
        assert registry.try_reserve_run(first, attachment_id=session.attachment_id)
        return
    assert session.viewed_session_id == second
    assert store.get_session(second)["messages"] == []
    messages = store.get_session(first)["messages"]
    assert messages[0]["text"] == "sent from first chat"
    assert "isn't loaded" in messages[1]["text"]
    assert not registry.is_busy(first)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_only", [False, True])
async def test_resume_distinguishes_checkpointed_and_pending_input(
    tmp_path, terminal_only,
):
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    registry = SessionRuntimeRegistry(repository)
    session = ConnectionSession(viewed_session_id=sid)
    registry.attach(sid, session.attachment_id, session)
    delivered_ticket = registry.enqueue_input(sid, "already applied", delivery="steer")
    delivered = registry.claim_input(sid, "steer", run_id="resume-run")
    registry.record_input_delivery(sid, session, delivered, None)
    pending_ticket = registry.enqueue_input(sid, "still pending", delivery="steer")
    snapshot = {
        "run_id": "resume-run", "thread_id": "resume-run", "chat_id": sid,
        "source": "chat", "goal": "original task", "status": "running",
        "task": {
            "task_id": "resume-run", "goal": "original task",
            "status": "completed" if terminal_only else "in_progress",
            "model_name": "test-model",
        },
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "original task"},
            {"role": "user", "content": "already applied"},
        ],
        "input": {"delivered": [delivered]},
        "main": {"loop": {
            "route": "finalize" if terminal_only else "model_step",
            "reply": "finished before restart" if terminal_only else "",
            "terminal_reason": "completed" if terminal_only else "",
        }},
        "tools": {"disclosed_names": ["ipython"]},
    }
    registry = SessionRuntimeRegistry(repository)
    await registry.startup_reconcile(store)
    assert repository.get_ticket(delivered_ticket.ticket_id).state == "resume_queued"
    assert repository.get_ticket(pending_ticket.ticket_id).state == "resume_queued"
    setup_ports = SimpleNamespace(
        io=SimpleNamespace(
            sessions=store, runtime_registry=registry,
            router=SimpleNamespace(
                mode="local", engine_ready=True, cloud_route_ready=lambda: False,
            ),
        ),
        session=SimpleNamespace(
            snapshot_resume_state=lambda _sid: (snapshot, ""),
            compress_messages=AsyncMock(return_value=[]),
        ),
    )
    # Textual resume follows the same validated setup path as the resume button.
    await prepare_chat_turn_stage(
        setup_ports, SimpleNamespace(send_json=AsyncMock()), "resume", session,
        resume=False, reserved=True, client_id="test", source="chat", images=[],
        attachment_text="",
    )
    assert repository.get_ticket(delivered_ticket.ticket_id).state == "running"
    assert repository.get_ticket(pending_ticket.ticket_id).state == (
        "resume_queued" if terminal_only else "queued"
    )

    fake = FakePorts([] if terminal_only else [AssistantTurn(text="finished")])
    turn_ports = fake.build()
    turn_ports.loop.drain_steering = lambda: registry.claim_input(
        sid, "steer", run_id="resume-run"
    )
    turn_ports.loop.drain_follow_up = lambda: registry.claim_input(
        sid, "follow_up", run_id="resume-run"
    )
    turn_ports.loop.record_active_input = lambda row, answer: registry.record_input_delivery(
        sid, session, row, answer
    )
    context = Variant1RunContext.create(source="chat", chat_session=session)
    with bind_run_context(context):
        result = await run_main_chat_task(
            config=chat_task_default().with_overrides(checkpoints=False),
            text="original task", base_system="system", full_tspec=[SPEC],
            convo_tail=[], images=[], is_resume=True, resume_snap=snapshot,
            ports=turn_ports,
        )

    assert [row["id"] for row in session.active.delivered_inputs] == (
        [delivered_ticket.ticket_id] if terminal_only
        else [delivered_ticket.ticket_id, pending_ticket.ticket_id]
    )
    if terminal_only:
        assert fake.stream_messages == []
        assert result.loop_result.reply == "finished before restart"
    else:
        contents = [row.get("content") for row in fake.stream_messages[0]]
        assert contents.count("already applied") == 1
        assert contents.count("still pending") == 1
    transcript = _durable_turn_messages(
        session, "original task", result.loop_result.reply, mood="neutral"
    )
    registry.begin_transcript_commit(session.active.delivered_inputs)
    store.append_messages(sid, transcript)
    registry.complete_transcript_commit(sid, session.active.delivered_inputs)
    assert repository.get_ticket(delivered_ticket.ticket_id).state == "completed"
    assert sum(row["text"] == "already applied" for row in transcript) == 1
    assert repository.get_ticket(pending_ticket.ticket_id).state == (
        "resume_queued" if terminal_only else "completed"
    )
