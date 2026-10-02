"""Local reasoning policy and development reset behavior."""

from __future__ import annotations

import server




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
