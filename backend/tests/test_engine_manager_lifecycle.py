"""Lifecycle coordination and transactional local-model switching."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from model_runtime import engine_manager
from automation.scheduler import LLMScheduler, principal_for


class _Hub:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


class _Engine:
    def __init__(self, *, model="old.gguf", mmproj="old-mmproj.gguf", ready=True):
        self.model = model
        self.mmproj = mmproj
        self.ready = ready
        self.proc = SimpleNamespace() if ready else None
        self.restart_calls = []
        self.stop_calls = 0
        self.fail_models = set()
        self.drop_projector_for = set()

    def poll_process(self):
        return self.ready

    async def restart(self, model, mmproj):
        self.restart_calls.append((model, mmproj))
        self.ready = False
        self.proc = None
        self.model = model or self.model
        self.mmproj = mmproj or ""
        if model in self.fail_models:
            raise RuntimeError(f"cannot load {model}")
        if model in self.drop_projector_for:
            self.mmproj = ""
        self.ready = True
        self.proc = SimpleNamespace()

    async def stop(self):
        self.stop_calls += 1
        self.ready = False
        self.proc = None


class _Router:
    def __init__(self, engine, *, wanted=True, scheduler=None):
        self.engine = engine
        self._wanted = wanted
        self.cfg = {
            "local": {"model": engine.model, "mmproj": engine.mmproj},
        }
        self.saved = 0
        self.selected = []
        if scheduler is not None:
            self._local_gate = lambda: scheduler.slot(
                principal_for("chat", "engine transition")
            )

    def wants_local_engine(self):
        return self._wanted

    async def start_local(self):
        self.engine.ready = True
        self.engine.proc = SimpleNamespace()

    def set_local_model(self, model, mmproj="", *, strict=False):
        self.selected.append((model, mmproj))
        self.cfg["local"]["model"] = model
        self.cfg["local"]["mmproj"] = mmproj
        self.save_config(strict=strict)

    def save_config(self, *, strict=False):
        self.saved += 1
        return True


def _status(router):
    return {
        "type": "engine",
        "ready": router.engine.ready,
        "model": router.cfg["local"]["model"],
    }


async def _wait_for_queue(scheduler: LLMScheduler, depth: int = 1) -> None:
    for _ in range(50):
        if scheduler.status()["queue_depth"] >= depth:
            return
        await asyncio.sleep(0)
    raise AssertionError("engine transition did not enter the scheduler queue")


@pytest.mark.asyncio
async def test_model_switch_waits_for_active_local_generation():
    scheduler = LLMScheduler()
    engine = _Engine()
    router = _Router(engine, scheduler=scheduler)
    hub = _Hub()
    release_generation = asyncio.Event()
    generation_started = asyncio.Event()

    async def active_generation():
        async with scheduler.slot(principal_for("chat", "active generation")):
            generation_started.set()
            await release_generation.wait()

    generation = asyncio.create_task(active_generation())
    await generation_started.wait()
    switch = asyncio.create_task(engine_manager.restart_engine(
        router,
        hub,
        lambda: _status(router),
        "new.gguf",
        "new-mmproj.gguf",
    ))
    await _wait_for_queue(scheduler)

    assert engine.restart_calls == []
    assert router.cfg["local"]["model"] == "old.gguf"

    release_generation.set()
    await generation
    assert await switch == "switched"
    assert engine.restart_calls == [("new.gguf", "new-mmproj.gguf")]


@pytest.mark.asyncio
async def test_policy_stop_waits_for_active_local_generation():
    scheduler = LLMScheduler()
    engine = _Engine()
    router = _Router(engine, wanted=False, scheduler=scheduler)
    release_generation = asyncio.Event()
    generation_started = asyncio.Event()

    async def active_generation():
        async with scheduler.slot(principal_for("chat", "active generation")):
            generation_started.set()
            await release_generation.wait()

    generation = asyncio.create_task(active_generation())
    await generation_started.wait()
    reconcile = asyncio.create_task(engine_manager.reconcile_local_engine(router))
    await _wait_for_queue(scheduler)

    assert engine.stop_calls == 0
    assert engine.ready is True

    release_generation.set()
    await generation
    assert await reconcile == "stopped"
    assert engine.stop_calls == 1


@pytest.mark.asyncio
async def test_failed_model_switch_restores_previous_runtime_and_config():
    engine = _Engine()
    engine.fail_models.add("broken.gguf")
    router = _Router(engine)
    hub = _Hub()
    before = {
        "local": dict(router.cfg["local"]),
    }

    result = await engine_manager.restart_engine(
        router,
        hub,
        lambda: _status(router),
        "broken.gguf",
        "broken-mmproj.gguf",
    )

    assert result == "rolled_back"
    assert engine.restart_calls == [
        ("broken.gguf", "broken-mmproj.gguf"),
        ("old.gguf", "old-mmproj.gguf"),
    ]
    assert engine.ready is True
    assert engine.model == "old.gguf"
    assert engine.mmproj == "old-mmproj.gguf"
    assert router.cfg == before
    assert router.selected == []
    assert len(hub.messages) == 2


@pytest.mark.asyncio
async def test_config_commit_failure_also_rolls_back_healthy_candidate():
    engine = _Engine()
    router = _Router(engine)
    hub = _Hub()
    before = {
        "local": dict(router.cfg["local"]),
    }

    def fail_after_mutating(model, mmproj="", *, strict=False):
        router.cfg["local"]["model"] = model
        router.cfg["local"]["mmproj"] = mmproj
        raise OSError("config storage unavailable")

    router.set_local_model = fail_after_mutating
    result = await engine_manager.restart_engine(
        router,
        hub,
        lambda: _status(router),
        "new.gguf",
        "new-mmproj.gguf",
    )

    assert result == "rolled_back"
    assert engine.restart_calls == [
        ("new.gguf", "new-mmproj.gguf"),
        ("old.gguf", "old-mmproj.gguf"),
    ]
    assert engine.ready is True
    assert engine.model == "old.gguf"
    assert router.cfg == before


@pytest.mark.asyncio
async def test_runtime_rollback_still_runs_when_config_restore_save_fails():
    engine = _Engine()
    router = _Router(engine)
    hub = _Hub()
    before = {
        "local": dict(router.cfg["local"]),
    }

    def fail_candidate_commit(model, mmproj="", *, strict=False):
        router.cfg["local"]["model"] = model
        router.cfg["local"]["mmproj"] = mmproj
        raise OSError("candidate config commit failed")

    def fail_restore_save(*, strict=False):
        raise OSError("old config restore failed")

    router.set_local_model = fail_candidate_commit
    router.save_config = fail_restore_save

    result = await engine_manager.restart_engine(
        router,
        hub,
        lambda: _status(router),
        "new.gguf",
        "new-mmproj.gguf",
    )

    assert result == "failed"
    assert engine.restart_calls == [
        ("new.gguf", "new-mmproj.gguf"),
        ("old.gguf", "old-mmproj.gguf"),
    ]
    assert engine.ready is True
    assert engine.model == "old.gguf"
    assert engine.mmproj == "old-mmproj.gguf"
    # In-memory routing is restored even though its durable rewrite failed.
    assert router.cfg == before


@pytest.mark.asyncio
async def test_success_persists_effective_text_only_fallback():
    engine = _Engine()
    engine.drop_projector_for.add("text-fallback.gguf")
    router = _Router(engine)
    hub = _Hub()

    result = await engine_manager.restart_engine(
        router,
        hub,
        lambda: _status(router),
        "text-fallback.gguf",
        "incompatible-mmproj.gguf",
    )

    assert result == "switched"
    assert router.selected == [("text-fallback.gguf", "")]
    assert router.cfg["local"] == {
        "model": "text-fallback.gguf",
        "mmproj": "",
    }
    assert "vision" not in router.cfg


@pytest.mark.asyncio
async def test_superseded_switch_is_dropped_after_waiting_for_lifecycle_gate():
    scheduler = LLMScheduler()
    engine = _Engine()
    router = _Router(engine, scheduler=scheduler)
    hub = _Hub()
    current_generation = 2
    release_generation = asyncio.Event()
    generation_started = asyncio.Event()

    async def active_generation():
        async with scheduler.slot(principal_for("chat", "active generation")):
            generation_started.set()
            await release_generation.wait()

    generation = asyncio.create_task(active_generation())
    await generation_started.wait()
    stale_switch = asyncio.create_task(engine_manager.restart_engine(
        router,
        hub,
        lambda: _status(router),
        "stale.gguf",
        should_apply=lambda: current_generation == 1,
    ))
    await _wait_for_queue(scheduler)
    release_generation.set()
    await generation

    assert await stale_switch == "superseded"
    assert engine.restart_calls == []
    assert hub.messages == []
    assert router.cfg["local"]["model"] == "old.gguf"
