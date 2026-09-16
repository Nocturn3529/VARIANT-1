"""Local reasoning policy and development reset behavior."""

from __future__ import annotations

import server
from memory_store import MemoryStore


def test_dev_reset_on_launch_clears_memory(tmp_path, monkeypatch):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    store.remember_explicit("chat-a", "old memory")
    monkeypatch.setenv("VARIANT1_DEV_RESET", "1")
    monkeypatch.setattr(server.APP.require_runtime().memory, "store", store)

    server.APP.require_runtime().lifecycle.dev_reset_on_launch()

    assert store.count_items(include_tombstoned=True) == 0


def test_router_thinking_is_model_runtime_policy_without_a_settings_toggle():
    from llm_router import LLMRouter

    router = LLMRouter({
        "mode": "local",
        "local": {"reasoning": True, "reasoning_budget": 1024},
    }, "/tmp")
    assert router.engine.reasoning_budget == 1024
    assert router._thinking_enabled(None) is True
    assert router._thinking_enabled(0) is False
    assert router._thinking_enabled(-1) is True

    assert router.engine.reasoning_budget == 1024
    assert router.reasoning is True
    assert not hasattr(router, "set_reasoning")


def test_router_keeps_unrestricted_reasoning_only_when_explicitly_configured():
    from llm_router import LLMRouter

    router = LLMRouter({
        "mode": "local",
        "local": {"reasoning": True, "reasoning_budget": -1},
    }, "/tmp")

    assert router.engine.reasoning_budget == -1
