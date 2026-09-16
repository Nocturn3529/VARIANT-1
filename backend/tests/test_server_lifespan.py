from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import lifecycle
import server_http
import server_lifespan


@pytest.mark.asyncio
async def test_critical_startup_failure_prevents_readiness_and_removes_handshake(
    tmp_path,
):
    port_file = tmp_path / "backend.json"
    runtime = {
        "host": "127.0.0.1",
        "port": 43125,
        "port_file": str(port_file),
        "instance_id": "instance-failed",
    }
    shutdown_called = False

    async def fail_startup():
        raise RuntimeError("runtime reconciliation failed")

    async def shutdown():
        nonlocal shutdown_called
        shutdown_called = True

    lifespan = lifecycle.make_lifespan(
        runtime=runtime,
        auth_token=lambda: "token",
        version="0.1.0",
        start_workers=fail_startup,
        shutdown=shutdown,
    )

    with pytest.raises(RuntimeError, match="runtime reconciliation failed"):
        async with lifespan(None):
            raise AssertionError("failed startup must not yield application control")

    assert runtime["ready"] is False
    assert runtime["startup_error"] == "runtime reconciliation failed"
    assert not port_file.exists()
    assert shutdown_called is True


@pytest.mark.asyncio
async def test_start_workers_awaits_critical_initialization():
    class CriticalFailureLifecycle:
        async def start_critical_services(self):
            raise RuntimeError("required asset unavailable")

        async def start_optional_services(self):
            return None

    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(
            lifecycle=CriticalFailureLifecycle(),
        ),
    )

    with pytest.raises(RuntimeError, match="required asset unavailable"):
        await server_lifespan.start_workers(host)

    assert host.startup_ready is False
    assert "required asset unavailable" in host.startup_error
    assert host.startup_failures[-1]["worker"] == "critical-startup"


@pytest.mark.asyncio
async def test_critical_startup_recovers_identity_before_starting_work():
    calls: list[str] = []

    class Lifecycle:
        def dev_reset_on_launch(self):
            calls.append("dev-reset")

    class SessionRuntimes:
        async def startup_reconcile(self, _store):
            calls.append("session-reconcile")
            return {
                "chats": 1,
                "tickets_requeued": 0,
                "tickets_completed": 0,
                "deletions_deferred": 0,
            }

    class Work:
        async def start(self):
            # Composition has already registered durable handlers by lifespan
            # entry; the startup reorder must not replace or clear them.
            assert self.registered_kinds == {"kernel.checkpoint.v1"}
            calls.append("work-start")

        registered_kinds = {"kernel.checkpoint.v1"}

    runtime = SimpleNamespace(
        lifecycle=Lifecycle(),
        sessions=object(),
        session_runtimes=SessionRuntimes(),
        work=Work(),
    )
    host = SimpleNamespace(
        require_runtime=lambda: runtime,
        session_runtimes=SessionRuntimes(),
    )

    await server_lifespan.start_critical_services(host)

    assert calls == [
        "dev-reset",
        "session-reconcile",
        "work-start",
    ]


@pytest.mark.asyncio
async def test_unexpected_long_lived_worker_failure_degrades_health():
    blocker = asyncio.Event()

    class RuntimeLifecycle:
        async def start_critical_services(self):
            return None

        async def start_optional_services(self):
            return None

        async def connect_all_mcp(self):
            return None

        async def automation_loop(self):
            raise RuntimeError("automation worker crashed")

        async def consolidation_loop(self):
            await blocker.wait()

    router = SimpleNamespace(engine_ready=False, model_name="")
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(lifecycle=RuntimeLifecycle()),
        gateway=None,
        router=router,
        memory=None,
        version="0.1.0",
        instance_id="instance-a",
        start_time=0.0,
        engine_label=lambda: "llama.cpp",
    )

    tasks = await server_lifespan.start_workers(host)
    assert host.startup_ready is True
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        payload = server_http.health_payload(host)
        assert payload["ready"] is False
        assert payload["status"] == "error"
        assert "automation worker crashed" in payload["startup_error"]
        assert payload["startup_failures"][-1]["worker"] == "automation-loop"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_stops_grok_adapter_before_peers_and_execution():
    order = []

    class AsyncService:
        def __init__(self, name):
            self.name = name

        async def shutdown(self):
            order.append(self.name)

    class SyncService:
        def shutdown(self):
            order.append("execution")

    runtime = SimpleNamespace(
        peers=AsyncService("peers"),
        work=None, extensions=None, browser=None, desktop=None, coding=None,
        execution=SyncService(),
        session_runtimes=AsyncService("session_runtimes"),
        kernel=AsyncService("kernel"),
    )
    host = SimpleNamespace(
        require_runtime=lambda: runtime,
        grok_peer_integration=AsyncService("grok"),
        gateway=None, background_tasks=None, mcp=None, voice=None,
        searxng=None, router=None,
    )

    result = await server_lifespan.shutdown(host)

    assert result["ok"] is True
    assert order.index("grok") < order.index("peers") < order.index("execution")
