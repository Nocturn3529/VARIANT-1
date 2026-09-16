"""Simultaneous child pumps must share one serialized scheduler admission."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from artifacts.store import ContentAddressedArtifactStore
from run_context import Variant1RunContext
from session_catalog import child_worker
from session_catalog.children import ChildSessionManager
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.support.model_routes import RouteAwareRouter
from work_fabric.service import WorkService


_TEST_WORK_SERVICES: list[WorkService] = []
_TEST_CHILD_MANAGERS: list[ChildSessionManager] = []


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


async def _terminal(manager, parent, child_id):
    for _ in range(400):
        row = manager.inspect(parent, child_id)
        if row["status"] not in {"queued", "running"}:
            if row.get("work_job_id") and manager.work is not None:
                await manager.work.jobs.wait(row["work_job_id"], timeout_s=5)
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("child did not reach a terminal state")


@pytest.mark.asyncio
async def test_simultaneous_children_respect_shared_scheduler_limit(tmp_path, monkeypatch):
    host = _Host(tmp_path / "children.sqlite3")
    manager = _manager(
        str(tmp_path / "children.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    work = host.require_runtime().work
    work.scheduler.max_concurrency = 1
    current = 0
    peak = 0
    release = asyncio.Event()
    entered = asyncio.Event()

    async def fake_run(*_args, **_kwargs):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        entered.set()
        await release.wait()
        current -= 1
        return "DONE: child"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    first = await manager.spawn("parent", task="one")
    second = await manager.spawn("parent", task="two")
    await asyncio.wait_for(entered.wait(), timeout=2)
    await asyncio.sleep(0.05)
    assert peak == 1
    assert work.scheduler.active_count == 1
    release.set()
    first_row = await asyncio.wait_for(
        _terminal(manager, "parent", first["child_id"]), timeout=5
    )
    second_row = await asyncio.wait_for(
        _terminal(manager, "parent", second["child_id"]), timeout=5
    )
    assert first_row["status"] == "completed"
    assert second_row["status"] == "completed"
    assert peak == 1


@pytest.mark.asyncio
async def test_restart_generation_is_admitted_after_first_child_completes(
    tmp_path, monkeypatch,
):
    host = _Host(tmp_path / "restart.sqlite3")
    manager = _manager(
        str(tmp_path / "restart.sqlite3"), host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    work = host.require_runtime().work
    work.scheduler.max_concurrency = 1
    generations: list[int] = []

    async def fake_run(*_args, **_kwargs):
        return "DONE: generation"

    monkeypatch.setattr(child_worker, "run_child_worker", fake_run)
    admitted = await manager.spawn("parent", task="first")
    first = await asyncio.wait_for(
        _terminal(manager, "parent", admitted["child_id"]), timeout=5
    )
    generations.append(first["run_generation"])
    restarted = await manager.restart("parent", admitted["child_id"])
    second = await asyncio.wait_for(
        _terminal(manager, "parent", admitted["child_id"]), timeout=5
    )
    generations.append(second["run_generation"])
    assert first["status"] == "completed"
    assert restarted["status"] in {"queued", "running", "completed"}
    assert second["status"] == "completed"
    assert generations[1] == generations[0] + 1
