from unittest.mock import AsyncMock, Mock

import pytest

from llm_router import LLMRouter
from model_providers import ProviderRegistry
from model_runtime import hermes_proxy


def _router(cfg=None):
    return LLMRouter(
        cfg or {
            "mode": "cloud",
            "cloud": {
                "provider": "hermes",
                "hermes_model": "upstage/solar-pro4:free",
            },
        },
        ".",
        config_path=None,
    )


def test_hermes_is_one_fixed_oauth_proxy_provider():
    registry = ProviderRegistry()
    profile = registry.get("hermes-agent")
    assert profile is not None
    assert profile.name == "hermes"
    assert profile.display_name == "Hermes Agent (Nous OAuth)"
    assert profile.base_url == "http://127.0.0.1:8645/v1"
    assert profile.default_model == "upstage/solar-pro4:free"
    assert profile.auth_style == "optional"
    assert profile.env_vars == ()
    assert profile.supports_reasoning is True
    assert profile.supports_vision is True

    router = _router({
        "mode": "cloud",
        "cloud": {
            "provider": "hermes",
            "hermes_model": "upstage/solar-pro4:free",
            "provider_options": {
                "hermes": {"base_url": "https://example.invalid/v1"}
            },
        },
    })
    assert router.provider_base_url("hermes") == hermes_proxy.HERMES_PROXY_OPENAI_BASE
    with pytest.raises(ValueError, match="fixed loopback"):
        router.set_provider_options("hermes", {"base_url": "https://example.invalid"})


@pytest.mark.asyncio
async def test_proxy_start_and_authentication_are_host_owned(monkeypatch):
    healthy = {"status": "ok", "upstream": "Nous Portal", "authenticated": True}
    health = AsyncMock(side_effect=[None, healthy])
    start = Mock()
    monkeypatch.setattr(hermes_proxy, "_health", health)
    monkeypatch.setattr(hermes_proxy, "_start_proxy", start)
    monkeypatch.setattr(hermes_proxy.asyncio, "sleep", AsyncMock())

    result = await hermes_proxy.ensure_proxy("upstage/solar-pro4:free")

    start.assert_called_once_with()
    assert result["endpoint"] == hermes_proxy.HERMES_PROXY_OPENAI_BASE
    assert result["model"] == "upstage/solar-pro4:free"


@pytest.mark.asyncio
async def test_proxy_rejects_wrong_upstream_or_logged_out_state(monkeypatch):
    monkeypatch.setattr(
        hermes_proxy,
        "_health",
        AsyncMock(return_value={
            "status": "ok", "upstream": "xAI Grok OAuth", "authenticated": True,
        }),
    )
    with pytest.raises(hermes_proxy.HermesProxyError, match="not Nous Portal"):
        await hermes_proxy.ensure_proxy()

    monkeypatch.setattr(
        hermes_proxy,
        "_health",
        AsyncMock(return_value={
            "status": "ok", "upstream": "Nous Portal", "authenticated": False,
        }),
    )
    with pytest.raises(hermes_proxy.HermesProxyError, match="not authenticated"):
        await hermes_proxy.ensure_proxy()


@pytest.mark.asyncio
async def test_router_lists_models_through_the_proxy_without_credentials(monkeypatch):
    listing = AsyncMock(return_value=["upstage/solar-pro4:free"])
    monkeypatch.setattr(hermes_proxy, "available_models", listing)

    router = _router()
    assert router.has_cloud_key("hermes") is True
    lease = router._credential_leases("hermes")[0]
    assert lease.source == "anonymous"
    assert lease.secret == ""
    assert await router.list_cloud_models("hermes") == ["upstage/solar-pro4:free"]
    listing.assert_awaited_once_with(start_if_needed=True)
