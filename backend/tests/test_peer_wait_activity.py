"""Transient wait display for an admitted peer message handle."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from capability_broker import InvocationContext
from peers import PeerCommunicationService, PeerRepository
from peers.capabilities import register_peers_tool
from run_context import Variant1RunContext, bind_run_context
from tests.support.conversation_sessions import open_sessions
from tests.test_peers import _stack, _settle_service
from tests.test_session_catalog import catalog_stack  # noqa: F401 (fixture)
from work_fabric.capabilities import register_work_fabric_tools


class WaitFrames:
    def __init__(self):
        self.rows = []
        self.started = asyncio.Event()

    def publish(self, frame):
        if frame.get("event") == "peer:wait":
            self.rows.append(frame)
            if frame["status"] == "running":
                self.started.set()


def admit(runtimes, chat):
    admission = runtimes.try_reserve_run(chat)
    run = Variant1RunContext.create(source="chat", session_id=chat, work_scope={"chat_id": chat},
                                    metadata={"admission_id": admission})
    runtimes.begin_run(admission, run_id=run.run_id, thread_id=run.run_id)
    return run, admission


def invocation(chat, run):
    return InvocationContext(chat_id=chat, run_id=run.run_id, outer_tool_call_id="wait-cell",
        cell_execution_id="cell-execution", nested_call_id="nested-wait", catalog_release_id="test-catalog")


@pytest.mark.asyncio
async def test_cpython_outgoing_handle_shows_only_its_actually_blocking_wait(catalog_stack, tmp_path):
    registry, _enabled, runtimes, _artifacts, broker, catalog, kernel = catalog_stack
    sessions = open_sessions(tmp_path / "chats")
    sessions.bind_runtime_lifecycle(runtimes.ensure_runtime)
    hub = WaitFrames()
    host = SimpleNamespace(hub=hub, remote_handle_routers={})
    chat = SimpleNamespace(start_next_queued_input=AsyncMock(return_value={"status": "unavailable"}))
    service = PeerCommunicationService(host, PeerRepository(str(tmp_path / "astb.sqlite3")),
                                       sessions=sessions, session_runtimes=runtimes, chat_service=chat)
    runtime = SimpleNamespace(peers=service, broker=broker, registry=registry)
    host.require_runtime = lambda: runtime
    registry.remove("peers")
    registry.remove("remote_handle_dispatch")
    register_work_fabric_tools(host)
    register_peers_tool(registry, host.require_runtime, host)
    catalog.reconcile_registry()
    sender = sessions.create_session("Sender", make_active=False)
    recipient = sessions.create_session("Recipient", make_active=False)
    catalog.select(sender, "operate")
    run, admission = admit(runtimes, sender)
    task = started = None
    try:
        # The request predates the current Python cell; its send call cannot
        # identify the cell that is now waiting on the durable handle.
        request = await service.send("chat:" + sender, "chat:" + recipient, "Check once")
        message_id = request["message_id"]
        with bind_run_context(run):
            task = asyncio.create_task(kernel.execute(chat_id=sender, run_id=run.run_id,
                work_scope=run.work_scope.to_dict(), outer_tool_call_id="wait-cell",
                code=f"m = await peers.inspect_message.async_(message_id={message_id!r})\n"
                     "print((await m.wait.async_(timeout_s=0))['status'])\n"
                     "print((await m.wait.async_(timeout_s=30))['status'])\n"
                     "print((await m.wait.async_(timeout_s=1))['status'])"))
        started = asyncio.create_task(hub.started.wait())
        done, _ = await asyncio.wait((started, task), timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            pytest.fail(f"Cell exited before blocking: {(await task).to_dict()}")
        assert started in done
        assert not task.done() and len(hub.rows) == 1
        await service.reply("chat:" + recipient, message_id, "Checked")
        result = await asyncio.wait_for(task, 10)
        assert result.ok, result.to_dict()
        assert result.output.text().count("replied") == 2 and "pending" in result.output.text()
        assert [row["status"] for row in hub.rows] == ["running", "done"]
        start, finish = hub.rows
        for key in ("session_id", "run_id", "outer_call_id", "call_id", "message_id"):
            assert start[key] and finish[key] == start[key]
        assert (start["session_id"], start["run_id"], start["outer_call_id"], start["message_id"]) == (
            sender, run.run_id, "wait-cell", message_id)
        assert start["call_id"] != start["outer_call_id"]
        assert not sessions.get_session(sender)["messages"]
    finally:
        for pending in (task, started):
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        runtimes.finish_run(admission, status="test_cleanup")
        await service.shutdown()
        await kernel.shutdown()
        await runtimes.shutdown()


@pytest.mark.parametrize("finish", ["timeout", "cancel"])
@pytest.mark.asyncio
async def test_wait_display_clears_on_timeout_or_propagated_cancellation(tmp_path, finish):
    service, runtimes, _, _, first, second = _stack(tmp_path)
    service.host.hub = frames = WaitFrames()
    run, admission = admit(runtimes, first)
    try:
        request = await service.send("chat:" + first, "chat:" + second, "Check once")
        with bind_run_context(run):
            task = asyncio.create_task(service.wait_message("chat:" + first, request["message_id"],
                timeout_s=30 if finish == "cancel" else 0.1, _invocation=invocation(first, run)))
        await asyncio.wait_for(frames.started.wait(), 2)
        # Unrelated changes may wake and re-arm the same wait.
        await service.send("chat:" + second, "chat:" + first, "Notice", message_kind="notice")
        if finish == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)["status"] == "pending"
        assert [row["status"] for row in frames.rows] == ["running", "done"]
        assert all(row["call_id"] == "nested-wait" for row in frames.rows)
    finally:
        runtimes.finish_run(admission, status="test_cleanup")
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_wait_display_requires_request_direction_and_live_admission(tmp_path):
    service, runtimes, _, _, first, second = _stack(tmp_path)
    service.host.hub = frames = WaitFrames()
    run, admission = admit(runtimes, first)
    a, b = "chat:" + first, "chat:" + second
    try:
        notice = await service.send(a, b, "Notice", message_kind="notice")
        request = await service.send(a, b, "Check once")
        with bind_run_context(run):
            call = invocation(first, run)
            assert (await service.wait_message(a, notice["message_id"], 0.01, _invocation=call))["status"] == "pending"
            assert (await service.wait_message(a, request["message_id"], 0, _invocation=call))["status"] == "pending"
            assert (await service.wait_message(a, request["message_id"], 0.01))["status"] == "pending"
            # Another attempt of the same logical run cannot publish against
            # the new active cell.
            runtimes.finish_run(admission, status="test_resume")
            admission = runtimes.try_reserve_run(first)
            runtimes.begin_run(admission, run_id=run.run_id, thread_id=run.run_id)
            assert (await service.wait_message(a, request["message_id"], 0.02, _invocation=call))["status"] == "pending"
            run.metadata["admission_id"] = admission
            incoming = await service.send(b, a, "Another request")
            assert (await service.wait_message(a, request["message_id"], 5, _invocation=call))["status"] == "attention"
            assert (await service.wait_message(a, incoming["message_id"], 0.01, _invocation=call))["status"] == "pending"
        assert frames.rows == []
    finally:
        runtimes.finish_run(admission, status="test_cleanup")
        await _settle_service(service, runtimes)


@pytest.mark.parametrize("failure", ["setup", "publish"])
@pytest.mark.asyncio
async def test_display_failure_preserves_wait_result(tmp_path, monkeypatch, failure):
    service, runtimes, _, _, first, second = _stack(tmp_path)
    service.host.hub = WaitFrames()
    run, admission = admit(runtimes, first)

    def unavailable(*_args):
        raise RuntimeError("test display unavailable")

    try:
        request = await service.send("chat:" + first, "chat:" + second, "Check once")
        if failure == "setup":
            monkeypatch.setattr(runtimes, "active_admission", unavailable)
        else:
            monkeypatch.setattr(service.host.hub, "publish", unavailable)
        with bind_run_context(run):
            result = await service.wait_message("chat:" + first, request["message_id"], 0.1,
                                                _invocation=invocation(first, run))
        assert result["status"] == "pending" and result["message"]["message_id"] == request["message_id"]
    finally:
        runtimes.finish_run(admission, status="test_cleanup")
        await _settle_service(service, runtimes)
