import json
from types import SimpleNamespace

import pytest

from session_catalog.service import IPYTHON_PROVIDER_SPEC
from model_runtime.llama_server import LocalEngineError
from model_runtime import capabilities
from model_runtime.request_policy import (
    effective_reasoning_budget,
    resolve_cloud_request_policy,
)
from llm_router import LLMRouter
from llm_stream_diagnostics import StreamDiagnostics
from model_providers import CredentialLease, ProviderRegistry, ProviderRequestError
from model_providers import credentials as credential_module
from tool_calling import ToolCallAccumulator
from tests.support.model_config import with_test_support


def _router(cfg=None):
    return LLMRouter(
        with_test_support(
            cfg or {"mode": "cloud", "cloud": {"provider": "openai"}}
        ),
        ".",
    )


def test_local_capability_snapshot_uses_running_engine_facts_without_probe(tmp_path):
    projector = tmp_path / "vision.mmproj"
    projector.write_bytes(b"projector")
    router = SimpleNamespace(
        mode="local",
        engine=SimpleNamespace(
            mmproj=str(projector),
            supports_reasoning=False,
            ctx_size=32768,
            model="local.gguf",
        ),
    )

    assert capabilities.resolve(router).to_dict() == {
        "vision": True,
        "tools": True,
        "thinking": False,
        "ctx_size": 32768,
        "source": "local",
        "model": "local.gguf",
    }


def test_cloud_capability_snapshot_uses_provider_profile_without_probe():
    profile = SimpleNamespace(supports_vision=True, supports_reasoning=True)
    router = SimpleNamespace(
        mode="cloud",
        cloud_provider="xai",
        get_cloud_model=lambda: "grok-4.6",
        provider_profile=lambda provider: profile if provider == "xai" else None,
        context_limit_tokens=lambda route: (
            131072 if route == {
                "mode": "cloud", "provider": "xai", "model": "grok-4.6",
            } else 0
        ),
    )

    assert capabilities.resolve(router).to_dict() == {
        "vision": True,
        "tools": True,
        "thinking": True,
        "ctx_size": 131072,
        "source": "cloud",
        "model": "grok-4.6",
    }


def test_bundled_registry_has_hermes_aligned_profiles_and_aliases():
    registry = ProviderRegistry()
    assert len(registry.list()) == 48
    assert registry.get('google-ai').name == 'google-antigravity'
    assert registry.get('google-antigravity').api_style == 'gemini'
    assert registry.canonical_name("claude") == "anthropic"
    assert registry.canonical_name("grok") == "xai"
    assert registry.get("lm-studio").name == "lmstudio"
    assert registry.get("codex-oauth").name == "openai-codex"
    assert registry.get("fireworks").base_url.endswith("/inference/v1")
    assert registry.get("upstage").default_model == "solar-pro4"
    assert registry.get("minimax-oauth").api_style == "anthropic"
    assert registry.get("minimax-oauth-cn").base_url.startswith("https://api.minimaxi.com/")
    hermes = registry.get("hermes")
    assert hermes.reasoning_effort_field == "reasoning_effort"
    assert hermes.reasoning_efforts == (
        "minimal", "low", "medium", "high", "xhigh", "max",
    )


def test_opencode_zen_free_route_is_anonymous_and_uses_exact_model_ids():
    registry = ProviderRegistry()
    profile = registry.get("opencode")

    assert profile.name == "opencode-zen"
    assert profile.auth_style == "optional"
    assert profile.base_url == "https://opencode.ai/zen/v1"
    assert profile.models_url == "https://opencode.ai/zen/v1/models"
    assert profile.default_model == "x-preview-f-free"
    assert "OPENCODE_API_KEY" in profile.env_vars
    assert profile.supports_reasoning is True
    assert all(not model.startswith("opencode/") for model in profile.fallback_models)


def test_provider_profiles_project_model_compatible_sampling_and_token_fields():
    registry = ProviderRegistry()
    openai = registry.get("openai")
    gpt5 = resolve_cloud_request_policy(
        openai, "gpt-5.6", {"temperature": 0.2, "top_p": 0.8},
    )
    gpt4 = resolve_cloud_request_policy(
        openai, "gpt-4o", {"temperature": 0.2, "top_p": 0.8},
    )
    claude = resolve_cloud_request_policy(
        registry.get("anthropic"),
        "claude-opus-4-7",
        {"temperature": 0.2, "top_p": 0.8},
    )

    assert gpt5.completion_token_field == "max_completion_tokens"
    assert gpt5.sampling == {}
    assert gpt4.completion_token_field == "max_tokens"
    assert gpt4.sampling == {"temperature": 0.2, "top_p": 0.8}
    assert claude.sampling == {}
    assert claude.structured_output_style == "anthropic_json_schema"


def test_reasoning_budget_is_separate_from_visible_output_allowance():
    router = SimpleNamespace(reasoning=True)
    sampling = {"max_tokens": 16_000, "reasoning_max_tokens": 1_024}

    assert effective_reasoning_budget(router, sampling, None) == 1_024
    assert effective_reasoning_budget(router, sampling, 256) == 256


def test_opencode_zen_is_ready_without_copying_desktop_credentials():
    router = _router({"mode": "cloud", "cloud": {"provider": "opencode-zen"}})

    assert router.has_cloud_key("opencode-zen") is True
    leases = router._credential_leases("opencode-zen")
    assert len(leases) == 1
    assert leases[0].source == "anonymous"
    assert leases[0].secret == ""
    assert router.get_cloud_model("opencode-zen") == "x-preview-f-free"
    assert "Authorization" not in router._provider_headers(
        router.provider_profile("opencode-zen"), leases[0].secret,
    )


def test_declarative_plugin_can_override_a_profile(tmp_path):
    plugin = tmp_path / "openai-override"
    plugin.mkdir()
    (plugin / "provider.json").write_text(json.dumps({
        "name": "openai", "display_name": "Private OpenAI",
        "base_url": "https://models.example.test/v1",
    }), encoding="utf-8")
    registry = ProviderRegistry(plugin_dirs=[tmp_path])
    assert registry.get("openai").display_name == "Private OpenAI"
    assert registry.get("openai").base_url == "https://models.example.test/v1"


def test_legacy_key_is_exposed_as_pool_credential_without_leaking_secret(monkeypatch):
    monkeypatch.setattr(credential_module.secretstore, "encrypt", lambda value: f"enc:{value}")
    monkeypatch.setattr(credential_module.secretstore, "decrypt", lambda value: value.removeprefix("enc:"))
    router = _router({"mode": "cloud", "cloud": {
        "provider": "openai", "keys": {"openai": "enc:secret-value"}}})
    public = router.list_cloud_credentials("openai")
    assert public == [{
        "id": "legacy-primary", "label": "Primary", "priority": -1000,
        "enabled": True, "base_url": "", "source": "legacy", "status": "ready",
        "cooldown_until": 0, "failures": 0, "last_error": "",
    }]
    assert "secret-value" not in repr(public)
    assert router._credential_leases("openai")[0].secret == "secret-value"


def test_pool_rotation_skips_credentials_in_cooldown(monkeypatch):
    monkeypatch.setattr(credential_module.secretstore, "encrypt", lambda value: f"enc:{value}")
    monkeypatch.setattr(credential_module.secretstore, "decrypt", lambda value: value.removeprefix("enc:"))
    router = _router()
    router.credential_pools.save = lambda: True  # This fixture has no config path; test rotation independently.
    first = router.add_cloud_credential("openai", "one", label="one")
    router.add_cloud_credential("openai", "two", label="two")
    lease = router.credential_pools.leases("openai")[0]
    assert lease.credential_id == first["id"]
    router.credential_pools.mark_failure(lease, status_code=429, detail="retry-after: 60")
    assert router.credential_pools.leases("openai")[0].label == "two"


def test_failed_legacy_primary_cools_down_so_pool_can_take_over(monkeypatch):
    monkeypatch.setattr(credential_module.secretstore, "encrypt", lambda value: f"enc:{value}")
    monkeypatch.setattr(credential_module.secretstore, "decrypt", lambda value: value.removeprefix("enc:"))
    router = _router({"mode": "cloud", "cloud": {
        "provider": "openai", "keys": {"openai": "enc:old"}}})
    router.credential_pools.save = lambda: True
    router.add_cloud_credential("openai", "new", label="replacement")
    legacy = router.credential_pools.leases("openai")[0]
    assert legacy.source == "legacy"
    router.credential_pools.mark_failure(legacy, status_code=401, detail="expired")
    assert router.credential_pools.leases("openai")[0].label == "replacement"


@pytest.mark.asyncio
async def test_provider_fallback_only_happens_after_bounded_pre_output_retry(monkeypatch):
    """Cloud retries a transient route, then falls back before visible output.

    ``call_cloud_once`` lives on ``llm_cloud_stream`` (extracted from the
    router facade) — patch there, not a retired ``router._call_cloud_once``.
    """
    import llm_cloud_stream

    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(llm_cloud_stream.asyncio, "sleep", no_sleep)

    router = _router({"mode": "cloud", "cloud": {
        "provider": "openai", "fallback_chain": ["anthropic"]}})
    router._credential_leases = lambda provider: [
        CredentialLease(provider, provider, provider, "key", source="environment")]
    calls = []

    async def fake_call(router_arg, profile, lease, model, messages, sampling, json_mode,
                        image_b64, reasoning_budget, reasoning_sink,
                        tools=None, tool_call_sink=None, stream_diagnostics=None,
                        prompt_cache_identity=None):
        calls.append(profile.name)
        if profile.name == "openai":
            raise ProviderRequestError("openai", "rate limited", status_code=429)
        yield "fallback"

    monkeypatch.setattr(llm_cloud_stream, "call_cloud_once", fake_call)
    output = [token async for token in router._call_cloud(
        [], {}, tools=[IPYTHON_PROVIDER_SPEC]
    )]
    assert output == ["fallback"]
    assert calls == ["openai", "openai", "openai", "openai", "anthropic"]
    assert delays == [2.0, 4.0, 8.0]

    async def partial_then_fail(router_arg, profile, lease, model, messages, sampling,
                                json_mode, image_b64, reasoning_budget, reasoning_sink,
                                tools=None, tool_call_sink=None,
                                stream_diagnostics=None,
                                prompt_cache_identity=None):
        yield "partial"
        raise ProviderRequestError(profile.name, "connection lost")

    monkeypatch.setattr(llm_cloud_stream, "call_cloud_once", partial_then_fail)
    with pytest.raises(LocalEngineError, match="connection lost"):
        [token async for token in router._call_cloud(
            [], {}, tools=[IPYTHON_PROVIDER_SPEC]
        )]


@pytest.mark.asyncio
async def test_transient_cloud_retry_reuses_route_and_discards_failed_tool_fragments(
    monkeypatch,
):
    import llm_cloud_stream

    router = _router({"mode": "cloud", "cloud": {
        "provider": "openai", "fallback_chain": ["anthropic"]}})
    router._credential_leases = lambda provider: [
        CredentialLease(provider, provider, provider, "key", source="environment")
    ]
    calls = []
    delays = []
    sink = ToolCallAccumulator()

    async def no_sleep(delay):
        delays.append(delay)

    async def fake_call(router_arg, profile, lease, model, messages, sampling,
                        json_mode, image_b64, reasoning_budget, reasoning_sink,
                        tools=None, tool_call_sink=None, stream_diagnostics=None,
                        prompt_cache_identity=None):
        calls.append(profile.name)
        tool_call_sink.add_openai_delta([{
            "index": 0,
            "id": f"{profile.name}-{len(calls)}",
            "function": {
                "name": "discarded_tool" if len(calls) == 1 else "working_tool",
                "arguments": "{}",
            },
        }])
        if len(calls) == 1:
            raise ProviderRequestError(
                profile.name,
                "temporary unavailable",
                status_code=503,
            )
        stream_diagnostics.note_finish_reason("tool_calls")
        yield "ok"

    monkeypatch.setattr(llm_cloud_stream.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(llm_cloud_stream, "call_cloud_once", fake_call)

    output = [token async for token in router._call_cloud(
        [], {}, tools=[IPYTHON_PROVIDER_SPEC], tool_call_sink=sink,
    )]

    assert output == ["ok"]
    assert calls == ["openai", "openai"]
    assert delays == [2.0]
    assert sink.actions() == [{
        "tool": "working_tool", "args": {}, "id": "openai-2",
    }]


@pytest.mark.asyncio
async def test_xai_oauth_request_validation_uses_responses_coordinate(monkeypatch):
    router = _router({
        "mode": "cloud",
        "cloud": {
            "provider": "xai",
            "xai_model": "grok-4.6",
            "xai_credential_policy": "subscription_first",
        },
    })
    router.has_oauth = lambda provider: provider == "xai"

    async def fresh(_provider):
        return True

    router.ensure_oauth_fresh = fresh
    router._credential_leases = lambda provider: [
        CredentialLease(provider, "oauth", "Subscription", "token", source="oauth")
    ]
    validated = []
    router.validate_model_request = lambda **kwargs: validated.append(kwargs) or {}

    async def fake_call(
        router_arg, profile, lease, model, messages, sampling, json_mode,
        image_b64, reasoning_budget, reasoning_sink, tools=None,
        tool_call_sink=None, stream_diagnostics=None,
        prompt_cache_identity=None,
    ):
        yield "ok"

    import llm_cloud_stream

    monkeypatch.setattr(llm_cloud_stream, "call_cloud_once", fake_call)
    output = [token async for token in router._call_cloud(
        [], {}, tools=[IPYTHON_PROVIDER_SPEC],
    )]

    assert output == ["ok"]
    assert validated[0]["adapter"] == "xai.responses"


@pytest.mark.asyncio
async def test_failed_cloud_attempt_cannot_contaminate_tool_calls(monkeypatch):
    import llm_cloud_stream

    router = _router({"mode": "cloud", "cloud": {
        "provider": "openai", "fallback_chain": ["anthropic"]}})
    router._credential_leases = lambda provider: [
        CredentialLease(provider, provider, provider, "key", source="environment")]
    sink = ToolCallAccumulator()
    diagnostics = StreamDiagnostics()

    async def fake_call(router_arg, profile, lease, model, messages, sampling,
                        json_mode, image_b64, reasoning_budget, reasoning_sink,
                        tools=None, tool_call_sink=None, stream_diagnostics=None,
                        prompt_cache_identity=None):
        tool_call_sink.add_openai_delta([{
            "index": 0,
            "id": f"{profile.name}-call",
            "function": {
                "name": "failed_tool" if profile.name == "openai" else "working_tool",
                "arguments": "{}",
            },
        }])
        if profile.name == "openai":
            raise ProviderRequestError("openai", "pre-output failure")
        stream_diagnostics.note_finish_reason("tool_calls")
        yield "ok"

    monkeypatch.setattr(llm_cloud_stream, "call_cloud_once", fake_call)
    output = [token async for token in router._call_cloud(
        [], {}, tools=[IPYTHON_PROVIDER_SPEC], tool_call_sink=sink,
        stream_diagnostics=diagnostics)]

    assert output == ["ok"]
    assert sink.actions() == [{
        "tool": "working_tool", "args": {}, "id": "anthropic-call"}]
    assert diagnostics.provider == "anthropic"
    assert diagnostics.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_cloud_discards_failed_attempt_reasoning_before_fallback(monkeypatch):
    import llm_cloud_stream

    router = _router({"mode": "cloud", "cloud": {
        "provider": "openai", "fallback_chain": ["anthropic"]}})
    router._credential_leases = lambda provider: [
        CredentialLease(provider, provider, provider, "key", source="environment")]
    calls = []
    reasoning = []

    async def fake_call(router_arg, profile, lease, model, messages, sampling,
                        json_mode, image_b64, reasoning_budget, reasoning_sink,
                        tools=None, tool_call_sink=None, stream_diagnostics=None,
                        prompt_cache_identity=None):
        calls.append(profile.name)
        reasoning_sink("visible reasoning")
        raise ProviderRequestError(profile.name, "failed after reasoning")
        yield

    monkeypatch.setattr(llm_cloud_stream, "call_cloud_once", fake_call)
    with pytest.raises(LocalEngineError, match="failed after reasoning"):
        _ = [token async for token in router._call_cloud(
            [], {}, tools=[IPYTHON_PROVIDER_SPEC],
            reasoning_sink=reasoning.append)]

    assert calls == ["openai", "anthropic"]
    assert reasoning == []
