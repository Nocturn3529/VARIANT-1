from unittest.mock import AsyncMock

import pytest

from llm_router import LLMRouter
from model_providers import ProviderRegistry
from model_runtime import ollama_cloud


def _router(cfg=None):
    return LLMRouter(
        cfg or {
            "mode": "cloud",
            "cloud": {
                "provider": "ollama",
                "ollama_model": "gemma4:31b-cloud",
            },
        },
        ".",
        config_path=None,
    )


def test_ollama_is_one_fixed_cloud_only_provider():
    registry = ProviderRegistry()
    profile = registry.get("ollama")
    assert profile is not None
    assert profile.display_name == "Ollama Desktop Cloud"
    assert profile.base_url == "http://127.0.0.1:11434/v1"
    assert profile.default_model == "gemma4:31b-cloud"
    assert profile.env_vars == ()
    assert registry.get("ollama-cloud") is None

    router = _router({
        "mode": "cloud",
        "cloud": {
            "provider": "ollama",
            "ollama_model": "gemma4:31b-cloud",
            "provider_options": {
                "ollama": {"base_url": "https://example.invalid/v1"}
            },
        },
    })
    assert router.provider_base_url("ollama") == ollama_cloud.OLLAMA_CLOUD_OPENAI_BASE
    with pytest.raises(ValueError, match="fixed loopback"):
        router.set_provider_options("ollama", {"base_url": "https://example.invalid"})
    with pytest.raises(ollama_cloud.OllamaCloudError, match="non-cloud"):
        router.set_cloud_model("ollama", "qwen2.5:3b")


@pytest.mark.asyncio
async def test_desktop_tag_listing_filters_out_local_models(monkeypatch):
    monkeypatch.setattr(ollama_cloud, "_helper_online", AsyncMock(return_value=True))
    monkeypatch.setattr(
        ollama_cloud,
        "_request_json",
        AsyncMock(return_value={
            "models": [
                {"name": "qwen2.5:3b"},
                {"name": "qwen3.5:cloud"},
                {"name": "gemma4:31b-cloud"},
            ]
        }),
    )
    assert await ollama_cloud.available_cloud_models() == [
        "gemma4:31b-cloud",
        "qwen3.5:cloud",
    ]
    with pytest.raises(ollama_cloud.OllamaCloudError, match="not pulled"):
        await ollama_cloud.ensure_cloud_model("gpt-oss:20b-cloud")


@pytest.mark.asyncio
async def test_router_lists_desktop_cloud_tags_without_credentials(monkeypatch):
    listing = AsyncMock(return_value=["gemma4:31b-cloud"])
    monkeypatch.setattr(ollama_cloud, "available_cloud_models", listing)
    assert await _router().list_cloud_models("ollama") == ["gemma4:31b-cloud"]
    listing.assert_awaited_once_with(start_if_needed=True)
