"""Internal minima cross the real cloud dispatch and preserve chat policy."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

import llm_cloud_stream as cloud
from llm_router import LLMRouter
from model_providers import CredentialLease
from model_providers.base import ProviderProfile
from model_providers.builtin import builtin_profiles
from model_runtime.request_policy import project_reasoning_policy
from tests.test_model_request_adapter_hooks import _install_transport, _wire_payload


def _path(payload, path):
    value = payload
    for part in path.split("."):
        value = value[part]
    return value


@pytest.mark.parametrize("provider,model,path,expected", [
    ("openai-codex", "gpt-5.6-luna", "reasoning.effort", "none"),
    ("openai-codex", "gpt-6-astra", "reasoning.effort", "low"),
    ("openai-codex", "gpt-5.3-codex-spark", "reasoning.effort", "low"),
    ("openai", "gpt-5.5", "reasoning_effort", "none"),
    ("openai", "gpt-5.4-mini", "reasoning_effort", "none"),
    ("openai", "o1", "reasoning_effort", "low"),
    ("openai", "o3", "reasoning_effort", "low"),
    ("openai", "o4-mini", "reasoning_effort", "low"),
    ("openrouter", "openai/gpt-5.6-luna", "reasoning.effort", "none"),
    ("openrouter", "openai/gpt-6-astra", "reasoning.effort", "low"),
    ("openrouter", "minimax/minimax-m3:free", "reasoning.effort", "low"),
    ("xai", "grok-4.3", "reasoning.effort", "low"),
    ("anthropic", "claude-sonnet-4-6", "thinking.type", "disabled"),
    ("anthropic", "claude-mythos-preview", "thinking.type", "adaptive"),
    ("anthropic", "claude-mythos-preview", "output_config.effort", "low"),
    ("gemini", "gemini-2.5-pro", "generationConfig.thinkingConfig.thinkingBudget", 128),
    ("gemini", "gemini-2.5-flash", "generationConfig.thinkingConfig.thinkingBudget", 0),
    ("gemini", "gemini-3.1-pro-preview", "generationConfig.thinkingConfig.thinkingLevel", "low"),
    ("gemini", "gemini-3-flash-preview", "generationConfig.thinkingConfig.thinkingLevel", "minimal"),
    ("gemini", "gemini-3.8-flash", "generationConfig.thinkingConfig.thinkingLevel", "low"),
    ("nvidia", "nvidia/nemotron-3-ultra-550b-a55b", "chat_template_kwargs.enable_thinking", False),
])
@pytest.mark.asyncio
async def test_minimum_survives_cloud_dispatch(monkeypatch, tmp_path, provider, model, path, expected):
    if provider == "anthropic":
        events = [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                  {"type": "message_stop"}]
    elif provider == "gemini":
        events = [{"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}],
                   "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1}}]
    elif provider in {"openai-codex", "xai"}:
        events = [{"type": "response.output_text.delta", "delta": "done"},
                  {"type": "response.completed", "response": {"output": [], "usage": {"input_tokens": 10, "output_tokens": 1}}}]
    else:
        events = [{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 1}}]
    captured = _install_transport(monkeypatch, cloud, "".join("data: " + json.dumps(e) + "\n\n" for e in events))
    router = LLMRouter({"mode": "cloud", "cloud": {"provider": provider, f"{provider}_model": model, "credential_pools": {}}}, str(tmp_path))
    route = {"mode": "cloud", "provider": provider, "model": model, "reasoning_effort": "xhigh"}
    config_before = deepcopy(router.cfg)
    with router.bind_model_route(route):
        before = router.get_reasoning_effort(provider, model)
        out = [tok async for tok in cloud.call_cloud_once(
            router, router.provider_profile(provider),
            CredentialLease(provider, "test", "Test", "not-a-real-key", source="oauth"),
            model, [{"role": "user", "content": "summarize"}], {"max_tokens": 100},
            False, None, 0,
        )]
        assert router.get_reasoning_effort(provider, model) == before
    payload = _wire_payload(captured)
    assert out == ["done"]
    assert _path(payload, path) == expected
    assert router.cfg == config_before
    if provider == "openai-codex":
        assert "summary" not in payload["reasoning"]
        assert "include" not in payload
        assert "max_output_tokens" not in payload


def test_custom_profile_minimum_replaces_conflicts_without_mutating_defaults():
    profile = ProviderProfile("custom-test", "Custom").with_overrides({
        "reasoning_effort_field": "reasoning.effort",
        "reasoning_efforts": ["deep", "quick"],
        "reasoning_model_rules": [{"patterns": ["mandatory-*"], "efforts": ["quick", "deep"],
            "minimum_fields": {"reasoning.max_tokens": None}}],
    })
    original = {"reasoning": {"max_tokens": 999, "exclude": False}, "other": 1}
    payload = deepcopy(original)
    router = SimpleNamespace(get_reasoning_effort=lambda *_: "deep")
    project_reasoning_policy(router, profile, "mandatory-custom", payload, 0)
    assert payload == {"reasoning": {"effort": "quick", "exclude": False}, "other": 1}
    assert original["reasoning"]["max_tokens"] == 999


def test_unknown_protocol_receives_no_invented_reasoning_fields():
    profile = ProviderProfile("custom-unknown", "Unknown")
    payload = {"model": "future-model"}
    project_reasoning_policy(None, profile, "future-model", payload, 0)
    assert payload == {"model": "future-model"}


def test_ordered_model_rule_does_not_leak_to_another_model_or_main_turn():
    profiles = {p.name: p for p in builtin_profiles()}
    router = SimpleNamespace(get_reasoning_effort=lambda *_: "xhigh")
    for budget in (None, 1024):
        payload = {}
        project_reasoning_policy(router, profiles["openai-codex"], "gpt-5.6-luna", payload,
                                 budget, effort_field="reasoning.effort")
        assert payload == {"reasoning": {"effort": "xhigh"}}
    payload = {}
    project_reasoning_policy(router, profiles["openai"], "gpt-4o", payload, 0)
    assert payload == {}


def test_provider_rules_do_not_share_mutable_state_between_registries():
    profiles = {p.name: p for p in builtin_profiles()}
    profiles["anthropic"].reasoning_model_rules[0]["minimum_fields"]["thinking"]["type"] = "changed"
    fresh = {p.name: p for p in builtin_profiles()}
    assert fresh["anthropic"].reasoning_model_rules[0]["minimum_fields"]["thinking"]["type"] == "adaptive"


def test_local_minimum_remains_per_request_and_omits_unknown_wire_controls():
    from llm_local_stream import build_local_template_payload

    engine = SimpleNamespace(supports_llama_extensions=True, reasoning_budget=777)
    router = SimpleNamespace(engine=engine)
    payload = build_local_template_payload(router, [], 0, [])
    assert payload["reasoning_budget"] == 0
    assert payload["chat_template_kwargs"]["enable_thinking"] is False
    assert engine.reasoning_budget == 777
    engine.supports_llama_extensions = False
    assert build_local_template_payload(router, [], 0, []) == {"messages": []}
