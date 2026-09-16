from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from model_runtime import engine_manager
from llm_router import LLMRouter
from llm_router_config import load_llm_config


def _router(tmp_path, *, mode="cloud", prewarm=False):
    return LLMRouter({
        "mode": mode,
        "local": {"prewarm": prewarm},
        "cloud": {"provider": "openai"},
    }, str(tmp_path), config_path=str(tmp_path / "llm_config.json"))


def test_local_engine_policy_covers_explicit_route_and_prewarm(tmp_path):
    assert _router(tmp_path, mode="local").wants_local_engine() is True
    assert _router(tmp_path, prewarm=True).wants_local_engine() is True
    assert _router(tmp_path).wants_local_engine() is False


def test_local_prewarm_is_persisted(tmp_path):
    router = _router(tmp_path)
    router.set_local_prewarm(True)

    assert router.local_prewarm is True
    assert LLMRouter(
        load_llm_config(str(tmp_path / "llm_config.json")),
        str(tmp_path),
        config_path=str(tmp_path / "llm_config.json"),
    ).local_prewarm is True


def test_malformed_llm_config_is_preserved_before_defaults(tmp_path):
    path = tmp_path / "llm_config.json"
    broken = '{"cloud":{"keys":{"openai":"encrypted-secret"}}'
    path.write_text(broken, encoding="utf-8")

    loaded = load_llm_config(str(path))

    assert loaded == {
        "mode": "local",
        "local": {},
        "inference": {"runtime": "llamacpp", "runtimes": {}},
        "sampling": {},
    }
    assert not path.exists()
    assert (tmp_path / "llm_config.json.corrupt").read_text(
        encoding="utf-8") == broken


def test_load_removes_retired_settings_controls(tmp_path):
    path = tmp_path / "llm_config.json"
    path.write_text(json.dumps({
        "mode": "cloud",
        "vision": {
            "route": "local",
            "local_enabled": True,
            "cloud_enabled": False,
            "privacy_mode": True,
        },
        "subagent_enabled": False,
        "local": {"reasoning": False},
        "cloud": {
            "provider": "xai",
            "xai": {"reasoning_effort": "high"},
        },
    }), encoding="utf-8")

    loaded = load_llm_config(str(path))

    assert loaded["mode"] == "cloud"
    assert "vision" not in loaded
    assert "subagent_enabled" not in loaded
    assert "reasoning" not in loaded["local"]
    assert "reasoning_effort" not in loaded["cloud"]["xai"]


class _FakeEngine:
    def __init__(self, ready=False, proc=None):
        self.ready = ready
        self.proc = proc
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        self.ready = False
        self.proc = None


class _FakeRouter:
    def __init__(self, wanted, *, ready=False, proc=None):
        self._wanted = wanted
        self.engine = _FakeEngine(ready=ready, proc=proc)
        self.start_calls = 0

    def wants_local_engine(self):
        return self._wanted

    async def start_local(self):
        self.start_calls += 1
        self.engine.ready = True


@pytest.mark.asyncio
async def test_reconcile_starts_a_wanted_cold_engine():
    router = _FakeRouter(True)

    assert await engine_manager.reconcile_local_engine(router) == "started"
    assert router.start_calls == 1
    assert router.engine.ready is True


@pytest.mark.asyncio
async def test_reconcile_stops_an_unwanted_loaded_engine():
    router = _FakeRouter(False, ready=True, proc=SimpleNamespace())

    assert await engine_manager.reconcile_local_engine(router) == "stopped"
    assert router.engine.stop_calls == 1
    assert router.engine.ready is False


@pytest.mark.asyncio
async def test_reconcile_leaves_matching_engine_state_unchanged():
    hot = _FakeRouter(True, ready=True)
    cold = _FakeRouter(False)

    assert await engine_manager.reconcile_local_engine(hot) == "unchanged"
    assert await engine_manager.reconcile_local_engine(cold) == "unchanged"
    assert hot.start_calls == 0
    assert cold.engine.stop_calls == 0


@pytest.mark.asyncio
async def test_reconcile_restarts_when_poll_clears_ready():
    """Stale ready=true after process exit must restart, not report unchanged."""

    class _DyingEngine(_FakeEngine):
        def poll_process(self):
            self.ready = False
            self.proc = None
            return False

    router = _FakeRouter(True, ready=True, proc=SimpleNamespace(returncode=1))
    router.engine = _DyingEngine(ready=True, proc=SimpleNamespace(returncode=1))

    assert await engine_manager.reconcile_local_engine(router) == "started"
    assert router.start_calls == 1


@pytest.mark.asyncio
async def test_ensure_local_engine_starts_when_cold():
    router = _FakeRouter(True)
    await engine_manager.ensure_local_engine(router)
    assert router.start_calls == 1
    assert router.engine.ready is True


@pytest.mark.asyncio
async def test_ensure_local_engine_noop_when_hot():
    router = _FakeRouter(True, ready=True)
    await engine_manager.ensure_local_engine(router)
    assert router.start_calls == 0


@pytest.mark.asyncio
async def test_ensure_local_engine_enters_local_lifecycle_slot_before_start():
    router = _FakeRouter(True)
    lifecycle_entered = False

    @asynccontextmanager
    async def local_gate():
        nonlocal lifecycle_entered
        lifecycle_entered = True
        try:
            yield
        finally:
            lifecycle_entered = False

    async def start_local():
        assert lifecycle_entered is True
        router.start_calls += 1
        router.engine.ready = True

    router._local_gate = local_gate
    router.start_local = start_local

    await engine_manager.ensure_local_engine(router)

    assert router.start_calls == 1
    assert lifecycle_entered is False


@pytest.mark.asyncio
async def test_ensure_local_engine_reuses_held_local_lifecycle_slot():
    router = _FakeRouter(True)
    gate_entries = 0

    @asynccontextmanager
    async def local_gate():
        nonlocal gate_entries
        gate_entries += 1
        yield

    router._local_gate = local_gate

    async with engine_manager._local_lifecycle_slot(router):
        await engine_manager.ensure_local_engine(router)

    assert router.start_calls == 1
    assert gate_entries == 1
