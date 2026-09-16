"""Regression boundaries from the 2026-09-05 backend review."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from automation.store import AutomationStore, AutomationPersistenceError
from durable_document import DocumentLoadError
from model_runtime.platform_store import AtomicJsonStore
from model_runtime.remote_nodes import RemoteNodeManager
from model_runtime.runtime_recipes import RuntimeRecipeManager
from kernel_runtime.worker_bridge import _promoted_helper_contract
from session_catalog.mutation_worker_client import MutationWorkerClient
from session_catalog.mutation_contracts import WorkerLimits, MutationWorkerError
from tools import validate_arguments, ToolError
from desktop.registry import COMPUTER_OBJECT_METHODS
from test_session_catalog import catalog_stack
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.support.conversation_sessions import open_sessions
import threading
from execution_hosts.input_queue import InputQueue
from execution_hosts.models import ExecutionValidationError
from execution_hosts.service import TerminalService
from dataclasses import replace
import time
from work_fabric.jobs import JobExecutionContext
from work_fabric.models import JobRecord, LeaseLost
from work_fabric.scope import WorkScope
from work_fabric.scheduler import WorkScheduler


@pytest.mark.parametrize("raw", [b"{broken", b"[]", b'{"items": 42}', b'\xff'])
def test_invalid_inventory_is_preserved_before_recovery(tmp_path, raw):
    path = tmp_path / "inventory.json"
    path.write_bytes(raw)
    store = AtomicJsonStore(str(path), {"items": []})
    assert store.load() == {"items": []}
    preserved, = tmp_path.glob("inventory.json.invalid-*")
    assert preserved.read_bytes() == raw
    store.save({"items": [{"id": "new"}]})
    assert json.loads(path.read_text()) == {"items": [{"id": "new"}]}


def test_unreadable_inventory_blocks_save_until_successful_reload(tmp_path, monkeypatch):
    path = tmp_path / "inventory.json"
    path.write_text('{"items": [{"id": "kept"}]}')
    store = AtomicJsonStore(str(path), {"items": []})
    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("busy")))
        with pytest.raises(DocumentLoadError):
            store.load()
    with pytest.raises(DocumentLoadError):
        store.save({"items": []})
    assert store.load()["items"] == [{"id": "kept"}]
    store.save({"items": [{"id": "kept"}, {"id": "new"}]})


def test_failed_quarantine_keeps_automation_document_and_blocks_writes(tmp_path, monkeypatch):
    path = tmp_path / "automation.json"
    store = AutomationStore(str(path))
    path.write_text('{"tasks": "not a task list"}')
    with monkeypatch.context() as patch:
        patch.setattr("durable_document.os.replace", lambda *a: (_ for _ in ()).throw(PermissionError("busy")))
        with pytest.raises(AutomationPersistenceError):
            store.load()
    with pytest.raises(AutomationPersistenceError):
        store.save()
    assert path.read_text() == '{"tasks": "not a task list"}'


@pytest.mark.parametrize("kind", ["node", "recipe"])
def test_inventory_mutation_publishes_only_after_persistence(tmp_path, monkeypatch, kind):
    manager = RemoteNodeManager(str(tmp_path)) if kind == "node" else RuntimeRecipeManager(
        str(tmp_path), None, None, AsyncMock()
    )
    payload = {"name": "original", "base_url": "http://localhost:8000", "model": "sample"}
    row = manager.save(payload)
    disk = manager.store.load()
    if kind == "node":
        manager.probes[row["id"]] = {"ready": True}
    monkeypatch.setattr(manager.store, "save", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    for operation in (lambda: manager.save({**row, "name": "changed"}), lambda: manager.remove(row["id"])):
        with pytest.raises(OSError):
            operation()
        assert manager.get(row["id"])["name"] == "original"
        assert manager.store.load() == disk
    if kind == "node":
        assert manager.probes[row["id"]] == {"ready": True}


def test_required_empty_inputs_and_explicit_content_constraints():
    params = {"arguments": {"type": "object", "required": True}}
    assert validate_arguments("helper", {"arguments": {}}, params) == {"arguments": {}}
    nested = {"arguments": {"type": "object", "required": True,
        "properties": {"items": {"type": "array", "required": True}}}}
    assert validate_arguments("helper", {"arguments": {"items": []}}, nested)
    method = next(m for m in COMPUTER_OBJECT_METHODS if m["name"] == "set_value")
    assert validate_arguments("computer.set_value", {"view": "v", "target": 0, "value": ""}, method["params"])["value"] == ""
    with pytest.raises(ToolError, match="needs"):
        validate_arguments("helper", {}, params)
    with pytest.raises(ToolError, match="null"):
        validate_arguments("helper", {"arguments": None}, params)
    with pytest.raises(ToolError, match="null"):
        validate_arguments(
            "helper",
            {"arguments": None},
            {"arguments": {"type": "object", "required": True, "default": None}},
        )
    for value, spec in [("", {"type": "string", "minLength": 1}), ([], {"type": "array", "minItems": 1}), ({}, {"type": "object", "minProperties": 1})]:
        with pytest.raises(ToolError):
            validate_arguments("bounded", {"value": value}, {"value": {**spec, "required": True}})


def test_promoted_defaults_use_evaluated_values_without_repeating_effects():
    effects = []
    def choose():
        effects.append("definition")
        return 3
    tag = ["original"]
    def helper(limit=choose(), *, labels=tag, extra=None):
        return [limit, labels, extra]
    tag = ["replaced"]
    _, schema, source = _promoted_helper_contract(helper)
    namespace = {}
    exec(source, namespace)
    assert namespace["run"]({}) == helper() == [3, ["original"], None]
    assert schema["properties"]["limit"]["default"] == 3
    assert effects == ["definition"]


def test_retained_grants_outlive_history_pagination_and_reset_revokes(catalog_stack):
    _, _, runtimes, _, _, service, _ = catalog_stack
    chat = "long-mount-history"
    runtimes.ensure_runtime(chat, is_new=True)
    service.select(chat, "build")
    for index in range(202):
        service.select(chat, "explore" if index % 2 == 0 else "operate")
    record, loaded = service._record_and_catalog(chat)
    retained = service._retained_category_ids(chat, catalog_release_id=loaded.release_id, selected_category_id=record.identity.selected_category_id)
    assert set(retained) == {"build", "explore", "operate"}
    assert len(service.repository.history(chat, limit=1000)) == 200
    service.reset(chat)
    assert service.repository.retained_categories(chat, loaded.release_id) == []


@pytest.mark.asyncio
async def test_mutation_timeout_cancels_nested_host_wait_and_awaits_cleanup(tmp_path, monkeypatch):
    client = MutationWorkerClient(str(tmp_path), limits=WorkerLimits(timeout_s=.03))
    events = []
    async def blocked_once(request, *, proxy_call):
        try:
            await proxy_call("held", {}, "request")
        finally:
            await asyncio.sleep(0)
            events.append("worker_cleanup")
    async def nested(*args):
        try:
            await asyncio.Event().wait()
        finally:
            events.append("nested_settled")
    monkeypatch.setattr(client, "_run_once", blocked_once)
    with pytest.raises(MutationWorkerError, match="timed out"):
        await client.run({}, proxy_call=nested)
    assert events == ["nested_settled", "worker_cleanup"]


@pytest.mark.asyncio
async def test_snapshot_deletion_uses_composed_store(tmp_path, monkeypatch):
    owner = SimpleNamespace(delete_thread=AsyncMock())
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")), snapshot_store=owner)
    sessions = open_sessions(tmp_path / "chats")
    sessions.bind_runtime_lifecycle(registry.ensure_runtime)
    chat = sessions.create_session("delete")
    registry.repository.link_thread(chat, "owned-thread", source="chat")
    monkeypatch.setattr("agent_engine.sqlite_snapshot_store.SQLiteRunSnapshotStore", lambda: (_ for _ in ()).throw(AssertionError("default store touched")))
    await registry.delete_chat(chat, sessions)
    owner.delete_thread.assert_awaited_once_with("owned-thread")


@pytest.mark.asyncio
async def test_failed_kernel_shutdown_retains_cleanup_owner_for_retry(catalog_stack, monkeypatch):
    *_, manager = catalog_stack
    lease = SimpleNamespace(chat_id="cleanup-owner", _closed=False)
    manager._leases[lease.chat_id] = lease
    close = AsyncMock(side_effect=[OSError("close failed"), {}])
    monkeypatch.setattr(manager, "_close_lease_serialized", close)
    result = await manager.shutdown()
    assert result["ok"] is False
    assert result["failures"][0]["chat_id"] == lease.chat_id
    assert manager._leases[lease.chat_id] is lease
    assert manager._closed is False
    assert (await manager.shutdown())["ok"] is True
    assert close.await_count == 2
    assert not manager._leases


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,status", [("destination", "switched"), ("missing", "fallback"), ("broken", "rejected")])
async def test_chat_switch_receipts_correlate_effective_navigation(monkeypatch, requested, status):
    import ws_chat_sessions as module
    handlers = {}
    def on(kind):
        def register(fn):
            handlers[kind] = fn
            return fn
        return register
    module.register(on)
    def set_active(target):
        if target == "broken":
            raise OSError("persistence failed")
        return target if target == "destination" else "current"
    sessions = SimpleNamespace(set_active=set_active)
    runtime = SimpleNamespace(sessions=sessions, chat=SimpleNamespace(sessions_message=lambda: {}), session_runtimes=None)
    host = SimpleNamespace(require_runtime=lambda: runtime, hub=SimpleNamespace(broadcast=AsyncMock()))
    connection = SimpleNamespace(viewed_session_id="current")
    socket = SimpleNamespace(send_json=AsyncMock())
    monkeypatch.setattr(module, "_session_payload", lambda _host, sid: {"id": sid})
    await handlers["chat:session:switch"](host, socket, connection, {"id": requested, "request_id": "nav-1"})
    reply = socket.send_json.await_args.args[0]
    receipt = reply.get("navigation", reply)
    assert receipt["request_id"] == "nav-1"
    assert receipt["requested_id"] == requested
    assert receipt["status"] == status
    assert receipt["effective_id"] == connection.viewed_session_id
    assert connection.viewed_session_id == ("destination" if status == "switched" else "current")


@pytest.mark.asyncio
async def test_stalled_stdin_does_not_block_loop_peer_or_terminal_stop():
    entered, release = threading.Event(), threading.Event()
    owner_thread = threading.get_ident()
    writes = []
    class Runtime:
        def write(self, raw):
            assert threading.get_ident() != owner_thread
            entered.set()
            release.wait(3)
            writes.append(raw)
            return len(raw)
        def terminate(self, **kwargs):
            release.set()
            records["first"].live = False
            records["first"].state = "terminated"
            service._live.pop('first', None)  # emulate the settled owner/watcher
    records = {name: SimpleNamespace(state="running", live=True) for name in ("first", "peer")}
    def transition(identity, state, **kwargs):
        records[identity].state = state
        return records[identity]
    repository = SimpleNamespace(get_terminal=lambda key: records[key], transition_terminal=transition,
                                 record_action=lambda *args: None, input_receipts=lambda *a: [])
    service = TerminalService(repository, backend_instance_id="test")
    runtime = Runtime()
    service._live.update(first=runtime, peer=SimpleNamespace(write=lambda raw: len(raw)))
    try:
        first = service.write("first", "blocked")
        assert first["state"] == "queued"
        assert await asyncio.to_thread(entered.wait, 1)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), .2)
        assert service.write("peer", "ok")["accepted_bytes"] == 2
        closed = service.close("first")
        assert closed.state == "terminated"
        for queue in service._inputs.values():
            queue.close()
        await asyncio.to_thread(service._inputs["first"]._thread.join, 1)
        assert writes == [b"blocked"]
    finally:
        release.set()
        for queue in service._inputs.values():
            queue.close()


def test_input_queue_backpressure_cancels_only_undelivered_bytes():
    entered, release = threading.Event(), threading.Event()
    written, outcomes = [], []
    def write(raw):
        entered.set()
        release.wait(2)
        written.append(raw)
        return len(raw)
    queue = InputQueue(SimpleNamespace(write=write), outcomes.append, max_bytes=5)
    first = queue.submit(b"abc")
    assert entered.wait(1)
    second = queue.submit(b"de")
    with pytest.raises(ExecutionValidationError, match="backpressure"):
        queue.submit(b"x")
    queue.close()
    release.set()
    queue._thread.join(1)
    assert written == [b"abc"]
    final = {r["write_id"]: r for r in queue.status()}
    assert final[first["write_id"]]["state"] == "written"
    assert final[second["write_id"]]["state"] == "cancelled"
    assert final[second["write_id"]]["written_bytes"] == 0


@pytest.mark.parametrize("change", [{"lease_owner": "other"}, {"lease_epoch": 2}, {"status": "waiting"}, {"lease_expires_at": 1}, {"lease_expires_at": 0}, {"lease_expires_at": None}])
def test_work_context_revokes_cooperation_after_lease_loss(change):
    original = JobRecord(job_id="j", owner_kind="chat", owner_id="a", kind="test", status="running",
                         scope=WorkScope(), lease_owner="mine", lease_epoch=1, lease_expires_at=time.time()+60)
    current = original
    context = JobExecutionContext(SimpleNamespace(require=lambda _: current), original, lease_ttl_s=60)
    assert context.cancellation_requested() is False
    current = replace(original, **change)
    assert context.cancellation_requested() is True


@pytest.mark.asyncio
async def test_heartbeat_revokes_and_cancels_active_handler():
    revoked = threading.Event()
    effects = []
    def heartbeat():
        raise LeaseLost("replacement owns the job")
    context = SimpleNamespace(heartbeat=heartbeat, request_local_cancellation=revoked.set)
    async def handler():
        await asyncio.Event().wait()
        effects.append("must not dispatch")
    task = asyncio.create_task(handler())
    scheduler = WorkScheduler(SimpleNamespace(), lease_ttl_s=6)
    await asyncio.wait_for(scheduler._heartbeat_loop(context, asyncio.Event(), task), 3)
    await asyncio.gather(task, return_exceptions=True)
    assert revoked.is_set() and task.cancelled()
    assert effects == []


@pytest.mark.asyncio
async def test_shutdown_reports_failure_and_continues_other_services():
    import server_lifespan
    kernel = SimpleNamespace(shutdown=AsyncMock(return_value={"ok": True}))
    runtime = SimpleNamespace(work=SimpleNamespace(shutdown=AsyncMock(side_effect=OSError("work cleanup failed"))),
                              session_runtimes=SimpleNamespace(shutdown=AsyncMock()), kernel=kernel)
    host = SimpleNamespace(require_runtime=lambda: runtime, router=SimpleNamespace(stop=AsyncMock()))
    result = await server_lifespan.shutdown(host)
    assert result["ok"] is False
    assert [x["service"] for x in result["failures"]] == ["work"]
    kernel.shutdown.assert_awaited_once()
    host.router.stop.assert_awaited_once()


def test_automation_history_preserves_intentional_cancellation(tmp_path):
    from automation.history import AutomationHistoryStore
    history = AutomationHistoryStore(str(tmp_path / "history.json"))
    history.add("a", 1, 2, "cancelled", "Stopped")
    assert history.list()[0]["status"] == "cancelled"
    assert AutomationHistoryStore(history.path).list()[0]["status"] == "cancelled"
