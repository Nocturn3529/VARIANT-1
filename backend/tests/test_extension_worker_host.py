from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from extensions.runtime_v2 import create_extension_v2_runtime
from extensions.worker_host import (
    PluginIdempotencyConflict, PluginWorkerHost, PluginWorkerLost, UnknownPluginEffect, _WorkerProcess,
    MAX_MESSAGE_BYTES, PROTOCOL_SCHEMA,
)
import ws_dispatch


def _executable_package(root: Path) -> Path:
    (root / "python").mkdir(parents=True)
    (root / "schemas").mkdir()
    (root / "schemas" / "input.json").write_text(
        json.dumps({"type": "object"}), encoding="utf-8"
    )
    (root / "python" / "plugin_impl.py").write_text(
        """
import asyncio
import os
import subprocess
import sys
from pathlib import Path

def echo(arguments, context):
    print("plugin diagnostic on stdout")
    return {"echo": arguments, "chat_id": context.get("chat_id", "")}

def fd_noise(arguments, context):
    # Direct writes/reads to public fds must not reach host JSONL framing.
    os.write(1, b'not a JSONL protocol frame\\n')
    consumed = os.read(0, 1)
    return {"consumed": consumed.decode("utf-8"), "ok": True}

async def async_echo(arguments, context):
    await asyncio.sleep(0.01)
    return {"async": arguments.get("value"), "scope": context.get("work_scope", {})}

async def wait_after_write(arguments, context):
    Path(arguments["marker"]).write_text("started")
    await asyncio.sleep(60)
    return {"finished": True}

async def wait_read(arguments, context):
    Path(arguments["marker"]).write_text("started")
    await asyncio.sleep(60)
    return {"finished": True}

async def cancellation_observed_read(arguments, context):
    marker = Path(arguments["marker"])
    marker.write_text("started")
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        marker.write_text("cancelled")
        raise

async def delayed_read(arguments, context):
    marker = Path(arguments["marker"])
    marker.write_text("started")
    await asyncio.sleep(float(arguments.get("delay", 0.2)))
    marker.write_text("finished")
    return {"finished": True}

async def spawn_descendant(arguments, context):
    child = (
        "import time; from pathlib import Path; time.sleep(1); "
        f"Path({arguments['descendant']!r}).write_text('survived')"
    )
    subprocess.Popen([sys.executable, "-c", child])
    Path(arguments["marker"]).write_text("started")
    await asyncio.sleep(60)
    return {"finished": True}

def counted_write(arguments, context):
    path = Path(arguments["marker"])
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return {"count": count}

def crash_once_read(arguments, context):
    path = Path(arguments["marker"])
    if not path.exists():
        path.write_text("crashed")
        os._exit(91)
    return {"recovered": True}

def crash_after_write(arguments, context):
    path = Path(arguments["marker"])
    prior = path.read_text() if path.exists() else ""
    path.write_text(prior + "x")
    os._exit(92)
""".strip(),
        encoding="utf-8",
    )
    capabilities = [
        ("demo.echo", "echo", "read"),
        ("demo.fd-noise", "fd_noise", "read"),
        ("demo.async", "async_echo", "read"),
        ("demo.counted", "counted_write", "write"),
        ("demo.wait", "wait_after_write", "write"),
        ("demo.wait-read", "wait_read", "read"),
        ("demo.cancel-observed", "cancellation_observed_read", "read"),
        ("demo.delayed-read", "delayed_read", "read"),
        ("demo.descendant", "spawn_descendant", "write"),
        ("demo.crash-read", "crash_once_read", "read"),
        ("demo.crash-write", "crash_after_write", "write"),
    ]
    manifest = {
        "schema_version": 2,
        "id": "com.example.executable",
        "name": "Executable test plugin",
        "version": "1.0.0",
        "compatibility": {},
        "entrypoints": {
            "python": {"module": "plugin_impl", "environment": "isolated"}
        },
        "contributes": {
            "capabilities": [
                {
                    "id": identifier,
                    "handler": f"plugin_impl:{handler}",
                    "input_schema": "schemas/input.json",
                    "effect_class": effect,
                    "parallel_safe": effect == "read",
                }
                for identifier, handler, effect in capabilities
            ]
        },
    }
    (root / "variant1.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _runtime(tmp_path: Path):
    runtime = create_extension_v2_runtime(
        str(tmp_path / "data"),
        environment_builder=lambda *_: {"mode": "test-source"},
        worker_deadline_s=5,
        worker_startup_deadline_s=10,
    )
    runtime.packages.install(str(_executable_package(tmp_path / "plugin")))
    return runtime


class _Socket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, value):
        self.messages.append(value)


def _websocket_context(runtime):
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(extensions=runtime),
    )
    session = SimpleNamespace(
        viewed_session_id="chat-plugin-ws",
        active=SimpleNamespace(runtime_chat_id="", turn_session_id=""),
    )
    return host, session


@pytest.mark.asyncio
async def test_worker_accepts_response_larger_than_default_asyncio_line_limit(tmp_path):
    runtime = _runtime(tmp_path)
    await runtime.start()
    try:
        value = "x" * 100_000
        result = await runtime.workers.invoke("com.example.executable", "demo.echo", {"value": value})
        assert result["result"]["echo"]["value"] == value
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_worker_protocol_fds_are_not_plugin_public_fds(tmp_path):
    runtime = _runtime(tmp_path)
    await runtime.start()
    try:
        result = await runtime.workers.invoke(
            "com.example.executable", "demo.fd-noise", {},
        )
        assert result["result"] == {"consumed": "", "ok": True}
        state = runtime.workers.state()
        assert state["workers"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [b"not json\n", b"x" * (MAX_MESSAGE_BYTES + 3) + b"\n"],
                         ids=["malformed", "oversized"])
async def test_invalid_worker_frame_fails_pending_without_waiting_for_live_process(frame):
    from unittest.mock import AsyncMock
    worker = _WorkerProcess(package_digest="test", config_path=Path("unused"), command=[],
                            cwd=".", generation=1, startup_deadline_s=1)
    stream = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 2)
    stream.feed_data(frame)
    worker.process = SimpleNamespace(returncode=None, stdout=stream)
    worker.terminate = AsyncMock()
    future = asyncio.get_running_loop().create_future()
    worker._pending["one"] = future
    await asyncio.wait_for(worker._read_stdout(), .5)
    with pytest.raises(PluginWorkerLost):
        await future
    worker.terminate.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_accepts_frame_at_declared_limit():
    from unittest.mock import AsyncMock
    worker = _WorkerProcess(package_digest="test", config_path=Path("unused"), command=[],
                            cwd=".", generation=1, startup_deadline_s=1)
    frame = json.dumps({"schema": PROTOCOL_SCHEMA, "id": "one", "value": ""}).encode()
    frame = frame[:-2] + b"x" * (MAX_MESSAGE_BYTES - len(frame)) + frame[-2:]
    assert len(frame) == MAX_MESSAGE_BYTES
    stream = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 2)
    stream.feed_data(frame + b"\n")
    stream.feed_eof()
    worker.process = SimpleNamespace(returncode=0, stdout=stream)
    worker.terminate = AsyncMock()
    future = asyncio.get_running_loop().create_future()
    worker._pending["one"] = future
    await worker._read_stdout()
    assert len((await future)["value"]) > 4_000_000


@pytest.mark.asyncio
async def test_cancelled_worker_request_settles_when_cancel_pipe_stalls():
    sent = asyncio.Event()

    class StalledCancelInput:
        def __init__(self):
            self.messages = []

        def write(self, data):
            self.messages.append(json.loads(data))

        async def drain(self):
            if len(self.messages) == 1:
                sent.set()
                return
            await asyncio.Event().wait()

    worker = _WorkerProcess(
        package_digest="test", config_path=Path("unused"), command=[], cwd=".",
        generation=1, startup_deadline_s=1,
    )
    stream = StalledCancelInput()
    worker.process = SimpleNamespace(returncode=None, stdin=stream)
    invocation = asyncio.create_task(worker.request(
        {"id": "stalled-cancel", "operation": "invoke"}, deadline_s=30,
    ))
    await sent.wait()
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(invocation, timeout=2)
    assert [message["operation"] for message in stream.messages] == ["invoke", "cancel"]
    assert worker.pending_count == 0


@pytest.mark.asyncio
async def test_late_host_cancellation_does_not_retire_a_pending_peer():
    worker = _WorkerProcess(
        package_digest="test", config_path=Path("unused"), command=[], cwd=".",
        generation=1, startup_deadline_s=1,
    )
    worker.process = SimpleNamespace(returncode=None)
    peer = asyncio.get_running_loop().create_future()
    worker._pending["peer"] = peer
    host = object.__new__(PluginWorkerHost)
    host._submitted_by_request = {}
    host._submitted_by_operation = {}
    host._inflight = {"completed": (worker, "completed-correlation")}
    host._cancelled_requests = set()

    assert await host.cancel("completed") is False
    assert worker.alive
    assert not worker.retire_requested
    assert worker.pending_count == 1
    assert not peer.done()
    peer.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_count", [1, 2])
async def test_direct_cancellation_reaches_worker_without_aborting_peers(tmp_path, peer_count):
    runtime = _runtime(tmp_path)
    runtime.workers.max_read_retries = 0
    await runtime.start()
    marker = tmp_path / "cancel-direct.txt"
    invocation = asyncio.create_task(runtime.workers.invoke(
        "com.example.executable", "demo.cancel-observed",
        {"marker": str(marker)}, request_id="cancel-direct",
    ))
    peers = []
    try:
        for _ in range(300):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.read_text() == "started"
        peer_markers = [tmp_path / f"peer-{index}.txt" for index in range(peer_count)]
        for index, peer_marker in enumerate(peer_markers):
            peers.append(asyncio.create_task(runtime.workers.invoke(
                "com.example.executable", "demo.delayed-read",
                {"marker": str(peer_marker), "delay": 0.8},
                request_id=f"peer-{index}",
            )))
        for _ in range(300):
            if all(path.exists() for path in peer_markers):
                break
            await asyncio.sleep(0.01)
        assert all(path.read_text() == "started" for path in peer_markers)
        worker = next(iter(runtime.workers._workers.values()))
        assert worker.pending_count == peer_count + 1

        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        for _ in range(100):
            if marker.read_text() == "cancelled":
                break
            await asyncio.sleep(0.01)
        assert marker.read_text() == "cancelled"
        assert worker.alive
        assert all(not peer.done() for peer in peers)

        results = await asyncio.gather(*peers)
        assert all(result["result"] == {"finished": True} for result in results)
        assert all(path.read_text() == "finished" for path in peer_markers)
        assert all(runtime.workers.operation(result["operation_id"])["attempt_count"] == 1
                   for result in results)
    finally:
        for task in [invocation, *peers]:
            if not task.done():
                task.cancel()
        await asyncio.gather(invocation, *peers, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_direct_plugin_invocation_and_reaps_worker(tmp_path):
    runtime = _runtime(tmp_path)
    await runtime.start()
    marker = tmp_path / "direct-shutdown.txt"
    invocation = asyncio.create_task(runtime.workers.invoke(
        "com.example.executable", "demo.wait-read",
        {"marker": str(marker)},
        idempotency_key="direct-shutdown",
        request_id="direct-shutdown",
    ))
    for _ in range(300):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.read_text() == "started"

    await runtime.shutdown()
    result = await asyncio.gather(invocation, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert runtime.workers.state()["workers"] == []


@pytest.mark.asyncio
async def test_real_worker_sync_async_idempotency_and_shutdown(tmp_path):
    runtime = _runtime(tmp_path)
    await runtime.start()

    sync = await runtime.workers.invoke(
        "com.example.executable", "demo.echo", {"value": "hello"},
        context={"chat_id": "chat-1"}, idempotency_key="echo-1",
    )
    assert sync["result"] == {"echo": {"value": "hello"}, "chat_id": "chat-1"}
    assert sync["replayed"] is False

    async_result = await runtime.workers.invoke(
        "com.example.executable", "demo.async", {"value": 42},
        context={"work_scope": {"chat_id": "chat-1"}}, idempotency_key="async-1",
    )
    assert async_result["result"]["async"] == 42

    marker = tmp_path / "count.txt"
    first = await runtime.workers.invoke(
        "com.example.executable", "demo.counted", {"marker": str(marker)},
        idempotency_key="count-once",
    )
    replay = await runtime.workers.invoke(
        "com.example.executable", "demo.counted", {"marker": str(marker)},
        idempotency_key="count-once",
    )
    assert first["result"] == {"count": 1}
    assert replay["replayed"] is True and replay["operation_id"] == first["operation_id"]
    assert marker.read_text() == "1"
    with pytest.raises(PluginIdempotencyConflict):
        await runtime.workers.invoke(
            "com.example.executable", "demo.counted", {"marker": str(tmp_path / "other")},
            idempotency_key="count-once",
        )

    await runtime.shutdown()
    assert runtime.workers.state()["started"] is False
    assert runtime.workers.state()["workers"] == []


@pytest.mark.asyncio
async def test_read_worker_loss_retries_but_write_loss_is_unknown_and_never_replayed(tmp_path):
    runtime = _runtime(tmp_path)
    read_marker = tmp_path / "read-crash.txt"
    recovered = await runtime.workers.invoke(
        "com.example.executable", "demo.crash-read", {"marker": str(read_marker)},
        idempotency_key="read-retry",
    )
    assert recovered["result"] == {"recovered": True}
    assert recovered["attempt_count"] == 2

    write_marker = tmp_path / "write-crash.txt"
    with pytest.raises(UnknownPluginEffect) as captured:
        await runtime.workers.invoke(
            "com.example.executable", "demo.crash-write", {"marker": str(write_marker)},
            idempotency_key="write-once",
        )
    operation = runtime.workers.operation(captured.value.operation_id)
    assert operation["status"] == "unknown_effect"
    assert write_marker.read_text() == "x"

    with pytest.raises(UnknownPluginEffect):
        await runtime.workers.invoke(
            "com.example.executable", "demo.crash-write", {"marker": str(write_marker)},
            idempotency_key="write-once",
        )
    assert write_marker.read_text() == "x"

    wait_marker = tmp_path / "wait-started.txt"
    invocation = asyncio.create_task(runtime.workers.invoke(
        "com.example.executable", "demo.wait", {"marker": str(wait_marker)},
        idempotency_key="cancel-write", request_id="cancel-me", deadline_s=5,
    ))
    for _ in range(100):
        if wait_marker.exists():
            break
        await asyncio.sleep(0.01)
    assert wait_marker.read_text() == "started"
    assert await runtime.workers.cancel("cancel-me") is True
    with pytest.raises(UnknownPluginEffect):
        await invocation

    deadline_marker = tmp_path / "deadline-started.txt"
    with pytest.raises(UnknownPluginEffect):
        await runtime.workers.invoke(
            "com.example.executable", "demo.wait", {"marker": str(deadline_marker)},
            idempotency_key="deadline-write", request_id="deadline-me", deadline_s=0.1,
        )
    assert deadline_marker.read_text() == "started"

    await runtime.shutdown()


@pytest.mark.asyncio
async def test_worker_tree_termination_kills_plugin_descendants(tmp_path):
    runtime = _runtime(tmp_path)
    started = tmp_path / "descendant-started.txt"
    survived = tmp_path / "descendant-survived.txt"
    invocation = asyncio.create_task(runtime.workers.invoke(
        "com.example.executable",
        "demo.descendant",
        {"marker": str(started), "descendant": str(survived)},
        idempotency_key="descendant-cancel",
        request_id="descendant-cancel",
        deadline_s=5,
    ))
    for _ in range(300):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.read_text() == "started"
    assert await runtime.workers.cancel("descendant-cancel") is True
    with pytest.raises(UnknownPluginEffect):
        await invocation
    await asyncio.sleep(1.2)
    assert not survived.exists()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_reconciliation_retries_reads_and_fences_effects(tmp_path):
    runtime = _runtime(tmp_path)
    read = await runtime.workers.invoke(
        "com.example.executable", "demo.echo", {"value": "recover"},
        context={"chat_id": "chat-recover"}, idempotency_key="recover-read",
    )
    marker = tmp_path / "reconcile-count.txt"
    write = await runtime.workers.invoke(
        "com.example.executable", "demo.counted", {"marker": str(marker)},
        idempotency_key="recover-write",
    )
    await runtime.shutdown()

    database = tmp_path / "data" / "extensions" / "extensions.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE extension_worker_operation_v2 SET status='dispatched',result_json='',"
            "finished_at=0 WHERE operation_id IN (?,?)",
            (read["operation_id"], write["operation_id"]),
        )

    recovered = create_extension_v2_runtime(
        str(tmp_path / "data"), environment_builder=lambda *_: {"mode": "test-source"},
        worker_deadline_s=5,
    )
    state = await recovered.start()
    assert state["workers"]["last_reconciliation"] == {
        "safe_prepared": 1, "unknown_effect": 1,
    }
    replayed_read = await recovered.workers.invoke(
        "com.example.executable", "demo.echo", {"value": "recover"},
        context={"chat_id": "chat-recover"}, idempotency_key="recover-read",
    )
    assert replayed_read["result"]["echo"] == {"value": "recover"}
    with pytest.raises(UnknownPluginEffect):
        await recovered.workers.invoke(
            "com.example.executable", "demo.counted", {"marker": str(marker)},
            idempotency_key="recover-write",
        )
    assert marker.read_text() == "1"
    await recovered.shutdown()


@pytest.mark.asyncio
async def test_websocket_invoke_returns_durable_handle_before_worker_completion(tmp_path):
    runtime = _runtime(tmp_path)
    await runtime.start()
    host, session = _websocket_context(runtime)
    socket = _Socket()
    try:
        await ws_dispatch.HANDLERS["extension-v2:invoke"](
            host, socket, session, {
                "type": "extension-v2:invoke",
                "request_id": "invoke-async-1",
                "package_id": "com.example.executable",
                "contribution_id": "demo.async",
                "arguments": {"value": 73},
                "deadline_ms": 5_000,
            },
        )
        accepted = socket.messages[-1]
        handle = accepted["result"]
        assert accepted["type"] == "extension-v2:accepted"
        assert accepted["request_id"] == "invoke-async-1"
        assert accepted["operation"] == "invoke"
        assert handle["schema"] == "variant1.plugin-operation-handle.v1"
        assert handle["request_id"] == "invoke-async-1"
        assert handle["operation_id"].startswith("plugin-op-")
        assert handle["status"] == "prepared"
        assert handle["terminal"] is False

        operation_id = handle["operation_id"]
        assert runtime.workers.operation(operation_id)["status"] == "prepared"
        for _ in range(300):
            if runtime.workers.operation(operation_id)["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)

        await ws_dispatch.HANDLERS["extension-v2:operation"](
            host, socket, session, {
                "type": "extension-v2:operation",
                "request_id": "poll-async-1",
                "operation_id": operation_id,
            },
        )
        polled = socket.messages[-1]
        assert polled["request_id"] == "poll-async-1"
        assert polled["result"]["status"] == "succeeded"
        assert polled["result"]["result"]["async"] == 73
        assert polled["result"]["context"]["request_id"] == "invoke-async-1"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_worker_host_owns_submit_and_predispatch_cancellation(
    tmp_path, monkeypatch,
):
    runtime = _runtime(tmp_path)
    await runtime.start()
    entered = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_worker(_package):
        entered.set()
        await never_release.wait()
        raise AssertionError("cancelled submission resumed unexpectedly")

    monkeypatch.setattr(runtime.workers, "_get_worker", blocked_worker)
    try:
        handle = await runtime.workers.submit(
            "com.example.executable",
            "demo.echo",
            {"value": "cancel-before-dispatch"},
            idempotency_key="submit-before-dispatch",
            request_id="submit-before-dispatch",
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert runtime.workers.state()["submitted"] == 1
        assert runtime.workers.operation(handle["operation_id"])["status"] == (
            "prepared"
        )

        assert await runtime.workers.cancel(handle["operation_id"]) is True
        operation = runtime.workers.operation(handle["operation_id"])
        assert operation["status"] == "failed"
        assert operation["attempt_count"] == 0
        assert operation["error"]["code"] == "cancelled"
        assert runtime.workers.state()["submitted"] == 0
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_idempotent_submit_aliases_share_one_host_task_and_cancel_target(
    tmp_path,
):
    runtime = _runtime(tmp_path)
    await runtime.start()
    marker = tmp_path / "submit-alias-started.txt"
    try:
        first = await runtime.workers.submit(
            "com.example.executable",
            "demo.wait-read",
            {"marker": str(marker)},
            idempotency_key="submit-alias-once",
            request_id="submit-alias-first",
            deadline_s=5,
        )
        second = await runtime.workers.submit(
            "com.example.executable",
            "demo.wait-read",
            {"marker": str(marker)},
            idempotency_key="submit-alias-once",
            request_id="submit-alias-second",
            deadline_s=5,
        )
        assert second["operation_id"] == first["operation_id"]
        assert runtime.workers.state()["submitted"] == 1
        for _ in range(300):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.read_text() == "started"

        assert await runtime.workers.cancel("submit-alias-second") is True
        operation = runtime.workers.operation(first["operation_id"])
        assert operation["status"] == "failed"
        assert operation["attempt_count"] == 1
        assert operation["error"]["code"] == "cancelled"
        assert runtime.workers.state()["submitted"] == 0
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_websocket_can_cancel_running_effect_by_durable_operation_handle(tmp_path):
    runtime = _runtime(tmp_path)
    await runtime.start()
    host, session = _websocket_context(runtime)
    socket = _Socket()
    marker = tmp_path / "ws-wait-started.txt"
    try:
        await ws_dispatch.HANDLERS["extension-v2:invoke"](
            host, socket, session, {
                "type": "extension-v2:invoke",
                "request_id": "invoke-wait-1",
                "package_id": "com.example.executable",
                "contribution_id": "demo.wait",
                "arguments": {"marker": str(marker)},
                "deadline_ms": 5_000,
            },
        )
        handle = socket.messages[-1]["result"]
        operation_id = handle["operation_id"]
        for _ in range(300):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.read_text() == "started"
        assert runtime.workers.operation(operation_id)["status"] == "dispatched"

        # This is the next message on the same serial WebSocket receive loop.
        # The durable operation id alone is sufficient for correlation.
        await ws_dispatch.HANDLERS["extension-v2:cancel"](
            host, socket, session, {
                "type": "extension-v2:cancel",
                "request_id": "cancel-wait-1",
                "operation_id": operation_id,
            },
        )
        cancelled = socket.messages[-1]
        assert cancelled["request_id"] == "cancel-wait-1"
        assert cancelled["result"] == {
            "request_id": "",
            "operation_id": operation_id,
            "cancelled": True,
        }

        await ws_dispatch.HANDLERS["extension-v2:operation"](
            host, socket, session, {
                "type": "extension-v2:operation",
                "request_id": "poll-wait-1",
                "operation_id": operation_id,
            },
        )
        operation = socket.messages[-1]["result"]
        assert operation["status"] == "unknown_effect"
        assert operation["error"]["code"] == "cancelled"
        assert marker.read_text() == "started"

        read_marker = tmp_path / "ws-read-started.txt"
        await ws_dispatch.HANDLERS["extension-v2:invoke"](
            host, socket, session, {
                "type": "extension-v2:invoke",
                "request_id": "invoke-read-1",
                "package_id": "com.example.executable",
                "contribution_id": "demo.wait-read",
                "arguments": {"marker": str(read_marker)},
                "deadline_ms": 5_000,
            },
        )
        read_operation_id = socket.messages[-1]["result"]["operation_id"]
        for _ in range(300):
            if read_marker.exists():
                break
            await asyncio.sleep(0.01)
        assert read_marker.read_text() == "started"
        other_marker = tmp_path / "unrelated-read.txt"
        unrelated = asyncio.create_task(runtime.workers.invoke(
            "com.example.executable", "demo.delayed-read",
            {"marker": str(other_marker)},
            idempotency_key="unrelated-during-cancel",
            request_id="unrelated-during-cancel",
        ))
        for _ in range(300):
            if other_marker.exists():
                break
            await asyncio.sleep(0.01)
        assert other_marker.read_text() == "started"
        await ws_dispatch.HANDLERS["extension-v2:cancel"](
            host, socket, session, {
                "type": "extension-v2:cancel",
                "request_id": "cancel-read-1",
                "target_request_id": "invoke-read-1",
            },
        )
        assert socket.messages[-1]["result"]["cancelled"] is True
        read_operation = runtime.workers.operation(read_operation_id)
        assert read_operation["status"] == "failed"
        assert read_operation["error"]["code"] == "cancelled"
        assert read_operation["attempt_count"] == 1
        unaffected = await unrelated
        assert unaffected["result"] == {"finished": True}
        assert other_marker.read_text() == "finished"
    finally:
        await runtime.shutdown()
