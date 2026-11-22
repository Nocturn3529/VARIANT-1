"""Exercise backpressure and framing at the private worker pipe."""

import asyncio
from dataclasses import replace
import io
import json
import os
import threading
from uuid import uuid4

import psutil
import pytest

from kernel_runtime.repl_writer import FrameNotSent, FrameWriteUncertain, ReplFrameWriter, check_write
from test_kernel_runtime import kernel_stack  # noqa: F401


@pytest.mark.asyncio
async def test_frame_writer_completes_short_writes_before_the_next_frame():
    class ShortPipe(io.BytesIO):
        def write(self, data):
            return super().write(data[:7])

    stream = ShortPipe()
    writer = ReplFrameWriter(stream, pid=0, max_frame_bytes=2048)
    try:
        first = writer.submit("one", "execute", {"code": "x = '東京'"})
        second = writer.submit("two", "interrupt", {"target_id": "one"})
        await asyncio.wait_for(asyncio.wrap_future(second), 2)
        check_write(first)
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert [(row["id"], row["type"]) for row in rows] == [("one", "execute"), ("two", "interrupt")]
        assert rows[0]["code"] == "x = '東京'"
    finally:
        writer.retire()
        await writer.wait_stopped()


@pytest.mark.asyncio
async def test_torn_frame_retires_only_its_writer_and_withdraws_queued_frames():
    entered, release = threading.Event(), threading.Event()

    class BrokenPipe(io.BytesIO):
        def write(self, data):
            if self.tell() == 0:
                return super().write(data[:5])
            entered.set()
            assert release.wait(3)
            return 0

    stream = BrokenPipe()
    writer = ReplFrameWriter(stream, pid=0, max_frame_bytes=4096)
    try:
        active = writer.submit("one", "execute", {"code": "a = 1"})
        assert await asyncio.to_thread(entered.wait, 2)
        queued = writer.submit("two", "execute", {"code": "b = 1"})
        release.set()
        with pytest.raises(FrameWriteUncertain):
            await asyncio.wait_for(asyncio.wrap_future(active), 2)
        with pytest.raises(FrameNotSent):
            check_write(queued)
        assert len(stream.getvalue()) == 5
        with pytest.raises(FrameNotSent):
            writer.submit("three", "execute", {"code": "c = 1"})
    finally:
        release.set()
        writer.retire()
        await writer.wait_stopped()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["kernel_interrupt", "cancel_turn", "configured_timeout", "restart", "steer_then_stop"])
async def test_stalled_real_worker_write_preserves_other_chats_and_fences_effects(kernel_stack, tmp_path, stop):
    manager, runtimes, _ = kernel_stack
    manager.limits = replace(manager.limits, cell_timeout_s=0.0, interrupt_grace_s=.15)

    async def cell(chat, source, **kwargs):
        return await manager.execute(chat_id=chat, code=source, run_id=chat,
                                     outer_tool_call_id=uuid4().hex, **kwargs)

    receiver = None
    rescue = None
    pending = None
    rescued = threading.Event()
    try:
        for chat in ("stalled", "peer"):
            runtimes.ensure_runtime(chat, is_new=True)
            assert (await cell(chat, "retained = 42")).ok
        pid = int((await cell("stalled", "import os; print(os.getpid())")).output.text().strip())
        lease = manager._leases["stalled"]
        owner = psutil.Process(lease.process.pid)
        assert pid in {owner.pid, *(child.pid for child in owner.children(recursive=True))}
        receiver = psutil.Process(pid)
        assert pid != os.getpid()
        receiver.suspend()

        def rescue_reader():
            rescued.set()
            try:
                receiver.resume()
            except psutil.NoSuchProcess:
                pass

        # Also unblocks the pre-fix synchronous write if the test regresses.
        rescue = threading.Timer(5, rescue_reader)
        rescue.daemon = True
        rescue.start()
        writer = lease.transport.writer
        original = writer._stream
        entered = threading.Event()

        class ObservedPipe:
            def write(self, data):
                entered.set()
                return original.write(data)

            def flush(self):
                original.flush()

        writer._stream = ObservedPipe()
        code = "with open('effect.txt', 'a') as f: f.write('once')\n#" + "x" * (1024 * 1024)
        pending = asyncio.create_task(cell("stalled", code, timeout_s=.6 if stop == "configured_timeout" else 0))
        assert await asyncio.to_thread(entered.wait, 2)
        heartbeat = await asyncio.wait_for(cell("peer", "print(retained)"), 2)
        assert heartbeat.ok and heartbeat.output.text().strip() == "42" and not rescued.is_set()
        if stop == "cancel_turn":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError) as stopped:
                await asyncio.wait_for(pending, 3)
            result = stopped.value.kernel_result
        else:
            if stop == "steer_then_stop":
                assert (await manager.interrupt("stalled", intent="steer"))["status"] == "requested"
                await asyncio.sleep(manager.limits.interrupt_grace_s * 2)
                assert not pending.done() and not writer.retired
            if stop in {"kernel_interrupt", "steer_then_stop"}:
                assert (await manager.interrupt("stalled"))["status"] == "requested"
            elif stop == "restart":
                assert (await manager.restart("stalled"))["status"] == "closed"
            result = await asyncio.wait_for(pending, 3)
        assert result.status == "unknown_effect" and result.error_code == "kernel_submission_unconfirmed"
        assert result.hard_restarted and not rescued.is_set()
        evidence = manager.execution_history("stalled")["items"][-1]
        assert (evidence["status"], evidence["error_code"]) == (result.status, result.error_code)
        assert not writer._thread.is_alive()
        fresh = await cell("stalled", "print('fresh')")
        assert fresh.ok and fresh.generation > lease.generation
        assert not (tmp_path / "effect.txt").exists()
    finally:
        if rescue is not None:
            rescue.cancel()
        if receiver is not None:
            try:
                receiver.resume()
            except psutil.NoSuchProcess:
                pass
        if pending is not None and not pending.done():
            pending.cancel()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_lost_write_acknowledgement_after_real_effect_never_replays(kernel_stack, tmp_path):
    import time

    manager, runtimes, _ = kernel_stack
    chat = "uncertain-effect"
    runtimes.ensure_runtime(chat, is_new=True)
    marker = tmp_path / "written"

    async def cell(code):
        return await manager.execute(chat_id=chat, code=code, run_id=chat, outer_tool_call_id=uuid4().hex)

    try:
        original = await cell("marker = 42")
        writer = manager._leases[chat].transport.writer
        stream = writer._stream

        class LostAcknowledgement:
            def write(self, data):
                stream.write(data)
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                raise BrokenPipeError("fixture lost the byte count after dispatch")

        writer._stream = LostAcknowledgement()
        result = await cell("with open('written', 'a') as f: f.write('once')\nimport time\ntime.sleep(10)")
        assert result.status == "unknown_effect" and result.hard_restarted
        assert result.error_code == "kernel_submission_unconfirmed"
        successor = await cell("print(43)")
        assert successor.ok and successor.generation > original.generation
        assert marker.read_text() == "once"
        assert not writer._thread.is_alive()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_flush", [False, True])
async def test_terminal_before_flush_keeps_the_witnessed_effect(kernel_stack, tmp_path, fail_flush):
    manager, runtimes, _ = kernel_stack
    chat = "early-terminal"
    runtimes.ensure_runtime(chat, is_new=True)
    received = asyncio.Event()
    release = threading.Event()
    pending = None

    async def cell(code):
        return await manager.execute(chat_id=chat, code=code, run_id=chat, outer_tool_call_id=uuid4().hex)

    try:
        first = await cell("marker = 0")
        transport = manager._leases[chat].transport
        pipe, deliver = transport.writer._stream, transport._deliver

        class LateFlush:
            def write(self, data):
                return pipe.write(data)

            def flush(self):
                assert release.wait(4)
                if fail_flush:
                    raise OSError("fixture flush failed after the worker replied")
                pipe.flush()

        def observe(event):
            deliver(event)
            if event.get("type") == "done" and transport.terminal_buffered(event["id"]):
                received.set()

        transport.writer._stream, transport._deliver = LateFlush(), observe
        pending = asyncio.create_task(cell("with open('effect', 'a') as f: f.write('once')\nprint(42)"))
        await asyncio.wait_for(received.wait(), 3)
        assert not pending.done()
        assert (tmp_path / "effect").read_text() == "once"
        release.set()
        result = await asyncio.wait_for(pending, 3)
        assert result.ok and result.reply_status == "ok" and result.output.text().strip() == "42"
        assert result.hard_restarted == fail_flush
        next_cell = await cell("print(43)")
        assert next_cell.ok and (next_cell.generation > first.generation) == fail_flush
        assert (tmp_path / "effect").read_text() == "once"
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stop_during_mount_never_submits_cell(kernel_stack, tmp_path):
    manager, runtimes, _ = kernel_stack
    chat = "mount-backpressure"
    runtimes.ensure_runtime(chat, is_new=True)
    entered = threading.Event()
    pending = None
    rescue = None
    receiver = None

    async def cell(code):
        return await manager.execute(chat_id=chat, code=code, run_id=chat, outer_tool_call_id=uuid4().hex)

    try:
        pid = int((await cell("import os; print(os.getpid())")).output.text().strip())
        lease = manager._leases[chat]
        owner = psutil.Process(lease.process.pid)
        assert pid in {owner.pid, *(child.pid for child in owner.children(recursive=True))}
        receiver = psutil.Process(pid)
        receiver.suspend()
        rescue = threading.Timer(5, receiver.resume)
        rescue.daemon = True
        rescue.start()
        manager.catalog_service.select(chat, "build")
        stream = lease.transport.writer._stream

        class ObservedPipe:
            def write(self, data):
                entered.set()
                return stream.write(data)

            def flush(self):
                stream.flush()

        lease.transport.writer._stream = ObservedPipe()
        pending = asyncio.create_task(cell("open('should-not-exist', 'w').close()"))
        assert await asyncio.to_thread(entered.wait, 2)
        assert not lease._admissions
        control = await manager.interrupt(chat)
        assert control["status"] == "requested" and control["execution_ids"] == [lease.current_cell["execution_id"]]
        result = await asyncio.wait_for(pending, 3)
        assert result.status == "cancelled" and result.error_code == "kernel_cell_not_submitted"
        assert not (tmp_path / "should-not-exist").exists()
        assert (await cell("print('fresh')")).ok
    finally:
        if rescue is not None:
            rescue.cancel()
        if receiver is not None:
            try:
                receiver.resume()
            except psutil.NoSuchProcess:
                pass
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await manager.shutdown()
