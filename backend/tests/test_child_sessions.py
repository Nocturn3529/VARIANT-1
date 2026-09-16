import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from session_catalog import child_worker
from artifacts.store import ContentAddressedArtifactStore
from capability_broker import CapabilityBroker, InvocationContext
from session_catalog.children import (
    ChildSessionManager,
    reported_child_text,
    register_children_tool,
)
from session_catalog.profiles import ACTION_SURFACE
from llm_usage import current_usage_category, current_usage_observer
from run_context import Variant1RunContext, current_run_context
from session_runtime import RuntimeIdentity, SessionRuntimeRegistry, SessionRuntimeRepository
from tools import ToolRegistry
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService
from work_fabric.capabilities import register_work_fabric_tools
from tests.support.model_routes import RouteAwareRouter

_TEST_WORK_SERVICES = []
_TEST_CHILD_MANAGERS = []


@pytest.fixture(autouse=True)
async def close_owned_test_work_services():
    yield
    managers = list(_TEST_CHILD_MANAGERS)
    _TEST_CHILD_MANAGERS.clear()
    await asyncio.gather(
        *(manager.drain_spawn_pumps() for manager in managers),
        return_exceptions=True,
    )
    services = list(_TEST_WORK_SERVICES)
    _TEST_WORK_SERVICES.clear()
    await asyncio.gather(*(service.shutdown() for service in services))


class _Runtimes:
    def __init__(self, path):
        self.usage = []
        self.repository = SessionRuntimeRepository(str(path))
        self._registry = SessionRuntimeRegistry(self.repository)

    def __getattr__(self, name):
        return getattr(self._registry, name)

    def ensure_runtime(self, *args, **kwargs):
        return self._registry.ensure_runtime(*args, **kwargs)

    def set_budget(self, *args, **kwargs):
        return self._registry.set_budget(*args, **kwargs)

    def attached_session(self, _chat_id):
        return None

    def record_run_usage(self, chat_id, run_id, usage):
        self.usage.append((chat_id, run_id, dict(usage)))
        return self._registry.record_run_usage(chat_id, run_id, usage)

    async def delete_child_runtime(self, runtime_id, *, parent_chat_id):
        return await self._registry.delete_child_runtime(
            runtime_id, parent_chat_id=parent_chat_id
        )


class _Host:
    def __init__(self, path):
        self.router = RouteAwareRouter()
        session_runtimes = _Runtimes(path)
        work = WorkService.open(str(path) + ".work.sqlite3")
        _TEST_WORK_SERVICES.append(work)
        self._runtime = SimpleNamespace(
            work=work,
            session_runtimes=session_runtimes,
            catalog=None,
        )

    def require_runtime(self):
        return self._runtime

    def make_run_context(self, source, title, **kwargs):
        return Variant1RunContext.create(
            source=source, title=title, session_id="child-session",
            metadata=kwargs.get("metadata"),
        )

    def child_worker_ports(self):
        return SimpleNamespace()


def _manager(database_path, host, artifact_store):
    manager = ChildSessionManager(
        str(database_path), host, artifact_store,
        work=host.require_runtime().work,
    )
    _TEST_CHILD_MANAGERS.append(manager)
    return manager


def _runtimes(host):
    return host.require_runtime().session_runtimes


@pytest.mark.asyncio
async def test_child_pins_parent_route_across_dispatch_restart_and_nested_spawn(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    host = _Host(tmp_path / "children.sqlite3")
    manager = _manager(str(tmp_path / "children.sqlite3"), host,
                       ContentAddressedArtifactStore(str(tmp_path / "artifacts")))
    monkeypatch.setattr(manager, "_enqueue", AsyncMock(return_value="held"))
    route = {"mode": "cloud", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}
    with host.router.bind_model_route(route):
        child = await manager.spawn("parent", task="work")
    host.router.cloud_provider = "different-provider"
    host.router.model_name = "different-model"
    seen = []
    async def run(*args, **kwargs):
        seen.append(host.router.bound_model_route())
        assert _runtimes(host).is_busy(child["child_chat_id"])
        assert _runtimes(host).automatic_kernel_eviction_blocked(child["child_chat_id"])
        return "DONE: fixture"
    monkeypatch.setattr(child_worker, "run_child_worker", run)
    async def dispatch(generation):
        await manager._work_handler(SimpleNamespace(
            job=SimpleNamespace(input_manifest={"child_id": child["child_id"], "generation": generation}),
            cancellation_requested=lambda: False,
        ))
    await dispatch(1)
    assert not _runtimes(host).is_busy(child["child_chat_id"])
    await manager.restart("parent", child["child_id"])
    await dispatch(2)
    nested = await manager.spawn(child["child_chat_id"], task="nested")
    for actual in [*seen, nested["model_route"]]:
        assert all(actual[key] == value for key, value in route.items())
    assert host.router.bound_model_route() is None


def test_reported_child_text_separates_exact_report_from_display_provenance():
    projected = (
        "[child FINISHED] CHILD-EXACT\n"
        "(Self-reported by the child; verify critical results.)"
    )
    assert reported_child_text(projected) == "CHILD-EXACT"
    assert reported_child_text("raw fake worker result") == "raw fake worker result"


def test_child_status_accepts_colon_or_whitespace_delimiters_without_prefix_guessing():
    assert child_worker._status("DONE: CHILD-EXACT") == ("done", "CHILD-EXACT")
    assert child_worker._status("DONE\n\nCHILD-EXACT") == ("done", "CHILD-EXACT")
    assert child_worker._status("PARTIAL progress") == ("partial", "progress")
    assert child_worker._status("DONEISH") == ("unknown", "DONEISH")


async def _terminal(manager, parent, child_id):
    for _ in range(200):
        row = manager.inspect(parent, child_id)
        if row["status"] not in {"queued", "running"}:
            if row.get("work_job_id") and manager.work is not None:
                await manager.work.jobs.wait(row["work_job_id"], timeout_s=5)
            return row
        await asyncio.sleep(0.005)
    raise AssertionError("child did not reach a terminal state")


@pytest.mark.asyncio
async def test_child_message_delivery_restart_and_recursive_usage(tmp_path, monkeypatch):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    release = asyncio.Event()
    calls = 0

    async def fake_run(
        _ports, task, context, inbound_messages=None, ack_inbound=None,
        **_kwargs,
    ):
        nonlocal calls
        calls += 1
        assert current_run_context().metadata["parent_thread_id"] == "parent-a"
        assert current_usage_category() == "child_session"
        observer = current_usage_observer()
        observer({"total_tokens": 17, "cost_usd": 0.25})
        if calls == 1:
            await release.wait()
        messages = inbound_messages() if inbound_messages else []
        if messages:
            before_ack = manager.inspect("parent-a", admitted["child_id"])
            pending = [
                row for row in before_ack["messages"]
                if row["direction"] == "parent_to_child"
            ]
            assert pending[0]["consumed_at"] is None
            ack_inbound([item["message_id"] for item in messages])
        return f"run={calls};task={task};messages={len(messages)}"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-a", task="inspect fixture", name="worker")
    manager.send("parent-a", admitted["child_id"], "use the second path")
    release.set()
    first = await _terminal(manager, "parent-a", admitted["child_id"])
    assert first["status"] == "completed" and "messages=1" in first["result_text"]
    assert first["reported_text"] == first["result_text"]
    assert first["usage"]["total_tokens"] == 17
    parent_messages = [
        row for row in first["messages"] if row["direction"] == "parent_to_child"
    ]
    assert parent_messages[0]["consumed_at"] is not None
    assert {row[0] for row in _runtimes(host).usage[:2]} == {
        first["child_chat_id"], "parent-a"
    }
    assert _runtimes(host).usage[0][2]["total_tokens"] == 17
    with pytest.raises(RuntimeError, match="terminal"):
        manager.send("parent-a", admitted["child_id"], "late")

    restarted = await manager.restart("parent-a", admitted["child_id"])
    assert restarted["child_id"] == admitted["child_id"]
    second = await _terminal(manager, "parent-a", admitted["child_id"])
    assert second["status"] == "completed" and "run=2" in second["result_text"]
    assert calls == 2 and len(_runtimes(host).usage) == 4


@pytest.mark.asyncio
async def test_child_partial_failure_is_not_persisted_as_completed(
    tmp_path, monkeypatch,
):
    host = _Host(tmp_path / "partial-child.sqlite3")
    manager = _manager(
        str(tmp_path / "partial-child.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )

    async def fake_run(*_args, **_kwargs):
        return (
            "[child FAILED] native output ended at its limit before completion; "
            "partial output: useful intermediate evidence"
        )

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-a", task="long child task")
    terminal = await _terminal(manager, "parent-a", admitted["child_id"])

    assert terminal["status"] == "failed"
    assert "partial output: useful intermediate evidence" in terminal["result_text"]


@pytest.mark.asyncio
async def test_cancelled_child_persists_partial_usage(tmp_path, monkeypatch):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    running = asyncio.Event()

    async def fake_run(
        _ports, _task, _context, inbound_messages=None, ack_inbound=None,
        **_kwargs,
    ):
        del inbound_messages, ack_inbound
        current_usage_observer()({"total_tokens": 9, "cost_usd": 0.1})
        running.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-b", task="wait")
    await running.wait()
    cancelled = await manager.cancel("parent-b", admitted["child_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["usage"]["total_tokens"] == 9
    assert len(_runtimes(host).usage) == 2
    assert _runtimes(host).usage[0][2]["total_tokens"] == 9


@pytest.mark.asyncio
async def test_partial_child_budget_rollup_recovers_each_ancestor_once(
    tmp_path, monkeypatch,
):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )

    async def fake_run(
        _ports, _task, _context, inbound_messages=None, ack_inbound=None,
        **_kwargs,
    ):
        del inbound_messages, ack_inbound
        current_usage_observer()({"total_tokens": 13, "cost_usd": 0.2})
        return "complete"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    original_record = _runtimes(host).record_run_usage
    fail_parent_once = True

    def flaky_record(chat_id, run_id, usage):
        nonlocal fail_parent_once
        if chat_id == "parent-rollup" and fail_parent_once:
            fail_parent_once = False
            raise RuntimeError("injected ancestor charge failure")
        return original_record(chat_id, run_id, usage)

    monkeypatch.setattr(_runtimes(host), "record_run_usage", flaky_record)
    admitted = await manager.spawn("parent-rollup", task="account once")
    terminal = await _terminal(manager, "parent-rollup", admitted["child_id"])
    assert terminal["status"] == "completed"
    assert terminal["usage_rollup_state"] == "pending"
    child_before = _runtimes(host).ensure_runtime(
        terminal["child_chat_id"]
    )
    parent_before = _runtimes(host).ensure_runtime("parent-rollup")
    assert child_before.budget_used["tokens"] == 13
    assert parent_before.budget_used.get("tokens", 0) == 0

    report = manager.recover_usage_rollups()
    recovered = manager.inspect("parent-rollup", admitted["child_id"])
    child_after = _runtimes(host).ensure_runtime(terminal["child_chat_id"])
    parent_after = _runtimes(host).ensure_runtime("parent-rollup")
    assert report == {"pending": 1, "completed": 1}
    assert recovered["usage_rollup_state"] == "complete"
    assert child_after.budget_used["tokens"] == 13
    assert parent_after.budget_used["tokens"] == 13


@pytest.mark.asyncio
async def test_child_cancelled_in_spawn_tick_is_durably_terminal(tmp_path, monkeypatch):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    called = False

    async def fake_run(
        _ports, _task, _context, inbound_messages=None, ack_inbound=None,
        **_kwargs,
    ):
        nonlocal called
        del inbound_messages, ack_inbound
        called = True
        return "should not run"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-immediate", task="cancel immediately")
    cancelled = await manager.cancel(
        "parent-immediate", admitted["child_id"]
    )

    assert cancelled["status"] == "cancelled"
    assert cancelled["error"] == "cancelled by parent"
    assert cancelled["completed_at"] is not None
    assert cancelled["usage"] == {
        "cost_usd": 0.0,
        "llm_calls": 0,
        "total_tokens": 0,
        "wall_time_s": 0.0,
    }
    assert called is False
    assert manager.list("parent-immediate")[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_queued_cancel_wins_before_gated_pump(tmp_path, monkeypatch):
    host = _Host(tmp_path / "queued-cancel.sqlite3")
    manager = _manager(
        str(tmp_path / "queued-cancel.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    work = host.require_runtime().work
    gate = asyncio.Event()
    original = work.scheduler.run_once
    called = False

    async def gated_run_once():
        await gate.wait()
        return await original()

    async def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return "should not run"

    monkeypatch.setattr(work.scheduler, "run_once", gated_run_once)
    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-queued", task="hold queue")
    cancelled = await manager.cancel("parent-queued", admitted["child_id"])
    assert cancelled["status"] == "cancelled"
    assert called is False
    gate.set()
    await manager.drain_spawn_pumps()
    assert manager.inspect("parent-queued", admitted["child_id"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_running_cancel_does_not_wait_on_sleep(tmp_path, monkeypatch):
    host = _Host(tmp_path / "running-cancel.sqlite3")
    manager = _manager(
        str(tmp_path / "running-cancel.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    running = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(*_args, **_kwargs):
        running.set()
        await release.wait()
        return "still running"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-running", task="hold running")
    await running.wait()
    assert manager.inspect("parent-running", admitted["child_id"])["status"] == "running"
    cancelled = await manager.cancel("parent-running", admitted["child_id"])
    release.set()
    assert cancelled["status"] == "cancelled"


@pytest.mark.asyncio
async def test_completed_cancel_does_not_rewrite_status(tmp_path, monkeypatch):
    host = _Host(tmp_path / "completed-cancel.sqlite3")
    manager = _manager(
        str(tmp_path / "completed-cancel.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )

    async def fake_run(*_args, **_kwargs):
        return "DONE: already finished"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-done", task="finish first")
    terminal = await _terminal(manager, "parent-done", admitted["child_id"])
    assert terminal["status"] == "completed"
    cancelled = await manager.cancel("parent-done", admitted["child_id"])
    assert cancelled["status"] == "completed"
    assert cancelled.get("error") != "cancelled by parent"


@pytest.mark.asyncio
async def test_spawn_pump_failure_is_recorded_and_cleared(tmp_path, monkeypatch):
    host = _Host(tmp_path / "pump-fail.sqlite3")
    manager = _manager(
        str(tmp_path / "pump-fail.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    work = host.require_runtime().work

    async def boom():
        raise RuntimeError("injected pump failure")

    monkeypatch.setattr(work.scheduler, "run_once", boom)
    admitted = await manager.spawn("parent-pump", task="pump fails")
    pumps = list(manager._spawn_pumps.values())
    if pumps:
        await asyncio.wait(pumps)
    await manager.drain_spawn_pumps()
    assert any(
        isinstance(exc, RuntimeError) and "injected pump failure" in str(exc)
        for exc in manager._spawn_pump_errors
    )
    assert admitted["status"] in {"queued", "interrupted", "failed", "cancelled"}


@pytest.mark.asyncio
async def test_started_scheduler_does_not_create_unstarted_pump(tmp_path, monkeypatch):
    host = _Host(tmp_path / "started-sched.sqlite3")
    manager = _manager(
        str(tmp_path / "started-sched.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    work = host.require_runtime().work
    manager._require_work()
    await work.start()
    assert work.started is True

    async def fake_run(*_args, **_kwargs):
        return "DONE: started scheduler"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-started", task="live scheduler")
    assert manager._spawn_pumps == {}
    terminal = await _terminal(manager, "parent-started", admitted["child_id"])
    assert terminal["status"] == "completed"
    await work.shutdown()


@pytest.mark.asyncio
async def test_child_of_mutable_astb_parent_is_forced_static(tmp_path, monkeypatch):
    host = _Host(tmp_path / "astb.sqlite3")
    _runtimes(host).repository.ensure_runtime(
        "mutable-parent",
        RuntimeIdentity(action_surface=ACTION_SURFACE),
    )
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )

    async def fake_run(
        _ports, _task, _context, inbound_messages=None, ack_inbound=None,
        **_kwargs,
    ):
        del inbound_messages, ack_inbound
        return "DONE: static child"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("mutable-parent", task="inspect safely")
    finished = await _terminal(manager, "mutable-parent", admitted["child_id"])
    child = _runtimes(host).repository.get_runtime(finished["child_chat_id"])

    assert child is not None
    assert child.identity.action_surface == ACTION_SURFACE
    assert finished["action_surface"] == ACTION_SURFACE


@pytest.mark.asyncio
async def test_one_children_seed_dispatches_the_lifecycle(tmp_path, monkeypatch):
    host = _Host(tmp_path / "astb.sqlite3")
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host, artifacts
    )

    async def fake_run(
        _ports, task, _context, inbound_messages=None, ack_inbound=None,
        **_kwargs,
    ):
        del inbound_messages, ack_inbound
        return f"DONE: {task}"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    registry = ToolRegistry()
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=host.require_runtime().session_runtimes,
        enabled_resolver=lambda: {tool.name for tool in registry.all()},
        artifact_store=artifacts,
    )
    host.require_runtime().registry = registry
    host.require_runtime().broker = broker
    host.require_runtime().session_artifacts = artifacts
    host.remote_handle_routers = {}
    register_work_fabric_tools(host)
    register_children_tool(registry, manager)
    context = InvocationContext(
        chat_id="parent-seed",
        run_id="run-seed",
        outer_tool_call_id="outer-seed",
        cell_execution_id="cell-seed",
        nested_call_id="nested-spawn",
        catalog_release_id="astb.test.release.v1",
        surface="ipython",
        work_scope=WorkScope(chat_id="parent-seed"),
    )
    spawned = await broker.invoke_name(
        "children",
        {"operation": "spawn", "task": "inspect through one seed"},
        context,
    )
    assert spawned.ok, spawned.to_dict()
    identity = spawned.result_value["$variant1_handle"]
    child_id = identity["id"]
    waited = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "wait",
            "arguments": {},
        },
        replace(context, nested_call_id="nested-wait"),
    )
    assert waited.ok, waited.error
    identity = waited.result_value["$variant1_handle"]
    assert identity["metadata"]["terminal"] is True
    finished = manager.inspect("parent-seed", child_id)
    listed = await broker.invoke_name(
        "children",
        {"operation": "list"},
        replace(context, nested_call_id="nested-list"),
    )
    assert listed.ok, listed.to_dict()
    assert finished["status"] == "completed"
    assert listed.result_value[0]["$variant1_handle"]["id"] == child_id
    tree = await broker.invoke_name(
        "children",
        {"operation": "tree"},
        replace(context, nested_call_id="nested-tree"),
    )
    assert tree.ok, tree.to_dict()
    assert tree.result_value["total"] == 1
    assert tree.result_value["items"][0]["child_id"] == child_id
    assert tree.result_value["capacity"] == {
        "mode": "cloud",
        "max_depth": 4,
        "max_active": 8,
        "max_admitted": 16,
    }
    inspected = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "inspect",
            "arguments": {},
        },
        replace(context, nested_call_id="nested-inspect"),
    )
    assert inspected.ok, inspected.to_dict()
    assert inspected.result_value["reported_text"].endswith(
        "inspect through one seed"
    )
    registered = {tool.name for tool in registry.all()}
    assert registered == {"children", "remote_handle_dispatch"}


def test_child_capacity_adapts_execution_not_api_to_local_mode(tmp_path):
    host = _Host(tmp_path / "astb.sqlite3")
    host.router = SimpleNamespace(
        mode="local",
        cfg={
            "subagents": {
                "max_depth": 5,
                "max_active_local": 1,
                "max_active_cloud": 9,
            }
        },
    )
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )

    assert manager.capacity() == {
        "mode": "local",
        "max_depth": 5,
        "max_active": 1,
        "max_admitted": 16,
    }


@pytest.mark.asyncio
async def test_child_execution_gate_allows_five_cloud_and_queues_local(tmp_path):
    host = _Host(tmp_path / "astb.sqlite3")
    host.router = SimpleNamespace(
        mode="cloud",
        cfg={"subagents": {"max_active_cloud": 8, "max_active_local": 1}},
    )
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )

    cloud_entered = 0
    cloud_all_entered = asyncio.Event()
    release_cloud = asyncio.Event()

    async def cloud_worker():
        nonlocal cloud_entered
        async with manager._execution_slot():
            cloud_entered += 1
            if cloud_entered == 5:
                cloud_all_entered.set()
            await release_cloud.wait()

    cloud_tasks = [asyncio.create_task(cloud_worker()) for _ in range(5)]
    await asyncio.wait_for(cloud_all_entered.wait(), timeout=1)
    assert manager._active_executions == 5
    release_cloud.set()
    await asyncio.gather(*cloud_tasks)

    host.router.mode = "local"
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def local_worker(position: int):
        async with manager._execution_slot():
            (first_entered if position == 0 else second_entered).set()
            if position == 0:
                await release_first.wait()

    first = asyncio.create_task(local_worker(0))
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second = asyncio.create_task(local_worker(1))
    await asyncio.sleep(0)
    assert second_entered.is_set() is False
    assert manager._active_executions == 1
    release_first.set()
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    await asyncio.gather(first, second)
    assert manager._active_executions == 0


@pytest.mark.asyncio
async def test_child_execution_is_owned_by_one_work_job(tmp_path, monkeypatch):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )

    async def fake_run(_ports, _task, _context, **_kwargs):
        return "DONE: work owned"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent-work", task="work owned")
    finished = await _terminal(manager, "parent-work", admitted["child_id"])
    job = host.require_runtime().work.jobs.require(finished["work_job_id"])

    assert job.kind == "child.execute.v1"
    assert job.owner_kind == "child"
    assert job.owner_id == admitted["child_id"]
    assert job.status == "succeeded"
    assert not hasattr(manager, "_tasks")


@pytest.mark.asyncio
async def test_parent_deletion_traverses_more_than_model_list_cap(tmp_path):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    deleted = []

    async def delete_runtime(runtime_id, *, parent_chat_id):
        deleted.append((runtime_id, parent_chat_id))
        return True

    _runtimes(host).delete_child_runtime = delete_runtime
    with manager._connect() as conn:
        conn.executemany(
            "INSERT INTO astb_child_handle(child_id,child_chat_id,parent_chat_id,"
            "depth,name,task_text,context_text,action_surface,status,created_at,updated_at) "
            "VALUES (?,?,?,1,'worker','task','',?,'completed',?,?)",
            [
                (
                    f"child-{index}", f"child-chat-{index}", "parent-many",
                    ACTION_SURFACE, 1.0 + index, 1.0 + index,
                )
                for index in range(105)
            ],
        )

    await manager.delete_chat("parent-many")

    assert len(deleted) == 105
    assert manager.list("parent-many", limit=100) == []


@pytest.mark.asyncio
async def test_failed_child_cleanup_retains_handle_for_retry(tmp_path):
    host = _Host(tmp_path / "astb.sqlite3")
    manager = _manager(
        str(tmp_path / "astb.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    fail = True

    async def delete_runtime(runtime_id, *, parent_chat_id):
        del parent_chat_id
        if runtime_id == "child-chat-bad" and fail:
            raise OSError("kernel cleanup failed")
        return True

    _runtimes(host).delete_child_runtime = delete_runtime
    with manager._connect() as conn:
        conn.executemany(
            "INSERT INTO astb_child_handle(child_id,child_chat_id,parent_chat_id,"
            "depth,name,task_text,context_text,action_surface,status,created_at,updated_at) "
            "VALUES (?,?,?,1,'worker','task','',?,'completed',1,1)",
            [
                ("child-good", "child-chat-good", "parent-retry", ACTION_SURFACE),
                ("child-bad", "child-chat-bad", "parent-retry", ACTION_SURFACE),
            ],
        )

    with pytest.raises(RuntimeError, match="cleanup remains incomplete"):
        await manager.delete_chat("parent-retry")
    remaining = manager.list("parent-retry")
    assert [row["child_id"] for row in remaining] == ["child-bad"]
    assert remaining[0]["deletion_state"] == "failed"
    assert "kernel cleanup failed" in remaining[0]["deletion_error"]

    fail = False
    await manager.delete_chat("parent-retry")
    assert manager.list("parent-retry") == []
