from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

from execution_hosts import ExecutionOwner, create_execution_runtime
from execution_hosts.pipe_reader import PipeInput, PipeReader
from execution_hosts.local import inherited_environment, run_bounded_child


def test_reader_owns_close_and_stops_with_writer_still_open():
    read_fd, write_fd = os.pipe()
    raw = os.fdopen(read_fd, "rb", buffering=0)
    closed_by = []
    received = bytearray()
    ready = threading.Event()

    class Stream:
        def fileno(self):
            return raw.fileno()

        def read(self, size):
            assert not os.get_blocking(raw.fileno())
            return raw.read(size)

        def close(self):
            closed_by.append(threading.get_ident())
            raw.close()

    def on_output(data):
        received.extend(data)
        ready.set()

    reader = PipeReader(Stream(), on_output, name="test-held-writer")
    try:
        os.write(write_fd, b"retained tail")
        assert ready.wait(2)
        assert not reader.join(0.01)
        reader.request_stop()
        assert reader.join(1)
        assert received == b"retained tail"
        assert closed_by == [reader.thread.ident]
        assert reader.status() == {"complete": False, "eof": False, "reader_settled": True}
    finally:
        os.close(write_fd)
        reader.request_stop()
        reader.join(1)


def test_closing_stdin_cancels_a_write_when_child_does_not_read():
    read_fd, write_fd = os.pipe()
    writer = PipeInput(os.fdopen(write_fd, "wb", buffering=0))
    result = []
    errors = []

    def write():
        try:
            result.append(writer.write(b"x" * 1024 * 1024))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    try:
        time.sleep(0.05)
        assert thread.is_alive()
        writer.close()
        thread.join(1)
        assert not thread.is_alive()
        assert not errors
        assert len(result) == 1 and 0 < result[0] < 1024 * 1024
        assert len(os.read(read_fd, 1024 * 1024)) == result[0]
    finally:
        os.close(read_fd)
        writer.close()
        thread.join(1)


@pytest.mark.parametrize("cause", ["timeout", "cancel", "overflow"])
def test_bounded_child_can_stop_while_feeding_full_stdin(tmp_path, cause):
    cancelled = threading.Event()
    timer = threading.Timer(0.2, cancelled.set)
    timer.daemon = True
    if cause == "cancel":
        timer.start()
    source = "import time; time.sleep(30)"
    if cause == "overflow":
        source = "import sys,time; sys.stdout.write('x'*70000); sys.stdout.flush(); time.sleep(30)"
    started = time.monotonic()
    try:
        result = run_bounded_child(
            [sys.executable, "-u", "-c", source], cwd=str(tmp_path),
            env=inherited_environment(), input_bytes=b"x" * 1024 * 1024,
            timeout=0.2 if cause == "timeout" else 30,
            cancellation_requested=cancelled.is_set,
            max_stdout_bytes=100 if cause == "overflow" else 1024,
        )
        assert time.monotonic() - started < 3
        assert result.timed_out == (cause == "timeout")
        assert result.cancelled == (cause == "cancel")
        assert result.output_limit_exceeded == (cause == "overflow")
    finally:
        timer.cancel()


def test_watcher_start_failure_reaps_child_and_removes_all_lifecycle_maps(tmp_path, monkeypatch):
    runtime = create_execution_runtime(data_dir=str(tmp_path / "runtime"))
    service = runtime.processes
    original_spawn = service._spawn_runtime
    original_start = threading.Thread.start
    children = []

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    def start(thread):
        if thread.name.startswith("variant1-process-watch-"):
            raise RuntimeError("injected watcher start failure")
        return original_start(thread)

    monkeypatch.setattr(service, "_spawn_runtime", spawn)
    monkeypatch.setattr(threading.Thread, "start", start)
    try:
        with pytest.raises(RuntimeError, match="injected watcher start failure"):
            service.start([sys.executable, "-c", "import time; time.sleep(30)"],
                          owner=ExecutionOwner("chat", "watcher-failure"),
                          cwd=str(tmp_path), process_id="proc_watcher_failure")
        assert children[0].wait(0) is not None
        assert service.get("proc_watcher_failure").state == "failed"
        assert not service._live and not service._watchers
        assert not service._registered and not service._pending
    finally:
        runtime.shutdown()


def test_next_process_starts_while_previous_cleanup_is_delayed(tmp_path, monkeypatch):
    runtime = create_execution_runtime(data_dir=str(tmp_path / "runtime"))
    service = runtime.processes
    owner = ExecutionOwner("chat", "cleanup-fixture")
    entered, release, started = threading.Event(), threading.Event(), threading.Event()
    original = service._spawn_runtime
    errors = []
    records = []

    def spawn(identity, recipe, registered):
        child = original(identity, recipe, registered)
        if identity == "proc_first":
            close = child.close

            def delayed_close(**kwargs):
                entered.set()
                assert release.wait(5)
                close(**kwargs)

            child.close = delayed_close
        return child

    monkeypatch.setattr(service, "_spawn_runtime", spawn)

    def start_second():
        try:
            records.append(service.start([sys.executable, "-c", "print('second')"],
                                         owner=owner, cwd=str(tmp_path)))
        except BaseException as exc:
            errors.append(exc)
        finally:
            started.set()

    second = threading.Thread(target=start_second, daemon=True)
    try:
        service.start([sys.executable, "-c", "print('first')"],
                      owner=owner, cwd=str(tmp_path), process_id="proc_first")
        assert entered.wait(5)
        second.start()
        assert started.wait(2), "another process was blocked by unrelated cleanup"
        assert not errors
        assert service.wait(records[0].process_id, timeout=5).exit_code == 0
    finally:
        release.set()
        if second.ident is not None:
            second.join(5)
        runtime.shutdown()


def test_held_output_pipe_is_reported_incomplete_and_does_not_block_reuse(tmp_path, monkeypatch):
    runtime = create_execution_runtime(data_dir=str(tmp_path / "runtime"))
    service = runtime.processes
    owner = ExecutionOwner("chat", "held-output")
    original = service._spawn_runtime
    writers = []
    readers = []

    def spawn(identity, recipe, registered):
        child = original(identity, recipe, registered)
        if identity == "proc_held_pipe":
            assert child.wait(5) == 0
            assert child.join_output(2)
            read_fd, write_fd = os.pipe()
            writers.append(write_fd)
            reader = PipeReader(os.fdopen(read_fd, "rb", buffering=0),
                                lambda data: service.repository.append_output(
                                    "process", identity, "stdout", data), name="test-extra-pipe")
            readers.append(reader)
            child._readers["stdout"] = reader
            os.write(write_fd, b"tail held by external writer")
        return child

    monkeypatch.setattr(service, "_spawn_runtime", spawn)
    try:
        record = service.start([sys.executable, "-c", "print('first')"],
                               owner=owner, cwd=str(tmp_path), process_id="proc_held_pipe")
        finished = service.wait(record.process_id, timeout=5)
        assert finished.state == "exited"
        assert finished.recovery["output_drain_incomplete"] is True
        assert finished.recovery["readers_settled"] is True
        assert b"tail held by external writer" in b"".join(
            frame.data for frame in service.logs(record.process_id).frames)
        second = service.start([sys.executable, "-c", "print('second')"],
                               owner=owner, cwd=str(tmp_path))
        assert service.wait(second.process_id, timeout=5).exit_code == 0
        assert not readers[0].thread.is_alive()
    finally:
        for fd in writers:
            os.close(fd)
        runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["process", "terminal", "bounded"])
async def test_cancelled_admission_never_spawns_after_lock_is_released(tmp_path, monkeypatch, kind):
    runtime = create_execution_runtime(data_dir=str(tmp_path / "runtime"))
    service = runtime.terminals if kind == "terminal" else runtime.processes
    locked, release = threading.Event(), threading.Event()
    spawns = []

    def hold_lock():
        with service._lock:
            locked.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert await asyncio.to_thread(locked.wait, 1)
    owner = ExecutionOwner("chat", "cancelled-admission")
    args = [sys.executable, "-c", "raise AssertionError('must not spawn')"]
    if kind != "terminal":
        monkeypatch.setattr(service, "_spawn_runtime", lambda *a, **kw: spawns.append(a))
        start = runtime.run_bounded_process if kind == "bounded" else runtime.start_process
        task = asyncio.create_task(start(args, owner=owner, cwd=str(tmp_path)))
    else:
        monkeypatch.setattr("execution_hosts.service.spawn_terminal", lambda *a, **kw: spawns.append(a))
        task = asyncio.create_task(runtime.open_terminal(argv=args, owner=owner, cwd=str(tmp_path)))
    try:
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not spawns
    finally:
        release.set()
        holder.join(2)
        runtime.shutdown()
    assert not spawns


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["process", "terminal"])
async def test_cancel_after_spawn_reaps_exact_child_and_allows_other_starts(tmp_path, monkeypatch, kind):
    import execution_hosts.service as module

    runtime = create_execution_runtime(data_dir=str(tmp_path / "runtime"))
    owner = ExecutionOwner("chat", "cancel-spawned")
    entered, release = threading.Event(), threading.Event()
    children = []
    original = runtime.processes._spawn_runtime if kind == "process" else module.spawn_terminal

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        if not children:
            children.append(child)
            entered.set()
            assert release.wait(5)
        return child

    if kind == "process":
        monkeypatch.setattr(runtime.processes, "_spawn_runtime", spawn)
        task = asyncio.create_task(runtime.start_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            owner=owner, cwd=str(tmp_path), process_id="proc_cancel_spawned"))
    else:
        monkeypatch.setattr(module, "spawn_terminal", spawn)
        task = asyncio.create_task(runtime.open_terminal(
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            owner=owner, cwd=str(tmp_path), force_pipe_fallback=True))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # This independent child must be admitted while the first OS start is
        # still pending, and it must survive cancellation of that first start.
        other = await asyncio.wait_for(runtime.start_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            owner=owner, cwd=str(tmp_path)), 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert children[0].wait(0) is not None
        assert runtime.processes.get(other.process_id).live
        if kind == "process":
            record = runtime.processes.get("proc_cancel_spawned")
            assert record.state == "terminated"
            assert "spawn_cancelled" in record.recovery
        assert not runtime.terminals._pending and not runtime.processes._pending
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(runtime.shutdown)


@pytest.mark.parametrize("during_spawn", [False, True])
def test_stop_prevents_restart_resurrection(tmp_path, monkeypatch, during_spawn):
    runtime = create_execution_runtime(data_dir=str(tmp_path / "runtime"))
    service = runtime.processes
    owner = ExecutionOwner("chat", "restart-race")
    original = service._spawn_runtime
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    children, errors = [], []

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        if len(children) == 2:
            entered.set()
            assert release.wait(5)
        return child

    monkeypatch.setattr(service, "_spawn_runtime", spawn)
    stopper = None
    try:
        record = service.start([sys.executable, "-c", "raise SystemExit(1)"],
                               owner=owner, cwd=str(tmp_path), restart="always", max_attempts=3,
                               restart_delay_s=0.01 if during_spawn else 10)
        if during_spawn:
            assert entered.wait(3)
        else:
            deadline = time.monotonic() + 3
            while service.get(record.process_id).state != "restarting" and time.monotonic() < deadline:
                time.sleep(0.01)
            assert service.get(record.process_id).state == "restarting"

        def stop():
            try:
                service.stop(record.process_id)
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()

        pending = service._pending[record.process_id]
        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        assert pending.cancelled.wait(2)
        release.set()
        assert stopped.wait(5)
        assert not errors
        assert service.get(record.process_id).state == "terminated"
        assert len(children) == (2 if during_spawn else 1)
        assert all(child.wait(0) is not None for child in children)
        assert not service._live and not service._pending
    finally:
        release.set()
        if stopper is not None:
            stopper.join(5)
        runtime.shutdown()
