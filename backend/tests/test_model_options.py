from __future__ import annotations

from types import SimpleNamespace

import pytest

from model_options import model_options
from ws_config import _local_picker_model


class _Sessions:
    def has_session(self, sid):
        return sid == "chat-1"

    def get_model_route(self, sid):
        assert sid == "chat-1"
        return {
            "mode": "cloud",
            "provider": "xai",
            "model": "grok-4.6",
            "reasoning_effort": "high",
        }


class _Router:
    mode = "cloud"
    cloud_provider = "xai"
    model_name = "local.gguf"
    inference_runtime_id = "llamacpp"
    cfg = {"local": {"model": r"models\local.gguf"}}
    engine = SimpleNamespace(mmproj="")

    def __init__(self):
        self.discovery_calls = []

    def _kn(self, value):
        return str(value)

    def provider_profile(self, name):
        return SimpleNamespace(reasoning_efforts=("low", "medium", "high"))

    def get_cloud_model(self, name):
        return "grok-4.6" if name == "xai" else ""

    def has_oauth(self, name):
        return name == "xai"

    def list_provider_info(self):
        return [
            {
                "name": "anthropic", "display_name": "Anthropic",
                "configured": False, "api_key_configured": False,
                "credential_count": 0, "models": [],
            },
            {
                "name": "xai", "display_name": "xAI",
                "description": "Grok", "configured": True,
                "api_key_configured": False, "credential_count": 0,
                "model": "grok-4.6", "default_model": "grok-4.3",
                "fallback_models": [], "supports_reasoning": True,
                "supports_vision": True,
            },
            {
                "name": "lmstudio", "display_name": "LM Studio",
                "configured": True, "api_key_configured": False,
                "credential_count": 0, "model": "local-model",
                "default_model": "local-model", "fallback_models": [],
            },
        ]

    async def list_cloud_models(self, provider, *, start_if_needed=True):
        self.discovery_calls.append((provider, start_if_needed))
        if provider == "lmstudio":
            raise RuntimeError("offline")
        return ["grok-4.6", "grok-4.5"]


@pytest.mark.asyncio
async def test_model_options_groups_connected_providers_and_caches_discovery():
    router = _Router()
    models = SimpleNamespace(scan_models=lambda: [{
        "name": "local.gguf",
        "path": r"C:\models\local.gguf",
        "size_bytes": 123,
        "vision": False,
    }])
    runtime = SimpleNamespace(sessions=_Sessions(), models=models)
    host = SimpleNamespace(router=router, require_runtime=lambda: runtime)

    first = await model_options(host, "chat-1", request_id="one")
    assert first["current"]["model"] == "grok-4.6"
    assert [row["id"] for row in first["providers"]] == ["local", "xai"]
    assert len(first["providers"][0]["models"]) == 1
    assert first["providers"][0]["models"][0]["detail"] == "123 B"
    assert [row["id"] for row in first["providers"][1]["models"]] == [
        "grok-4.6", "grok-4.3", "grok-4.5",
    ]
    assert first["providers"][1]["models"][0]["reasoning_efforts"] == [
        "low", "medium", "high",
    ]
    assert router.discovery_calls == [("xai", False), ("lmstudio", False)]

    second = await model_options(host, "chat-1", request_id="two")
    assert second["request_id"] == "two"
    # xAI uses the five-minute cache. An unavailable loopback is probed again
    # so it appears as soon as the user starts it.
    assert router.discovery_calls == [
        ("xai", False), ("lmstudio", False), ("lmstudio", False),
    ]

    await model_options(host, "chat-1", refresh=True)
    assert router.discovery_calls[-2:] == [("xai", False), ("lmstudio", False)]


@pytest.mark.asyncio
async def test_model_options_rejects_unknown_chat():
    router = _Router()
    runtime = SimpleNamespace(sessions=_Sessions(), models=SimpleNamespace(scan_models=lambda: []))
    host = SimpleNamespace(router=router, require_runtime=lambda: runtime)
    with pytest.raises(ValueError, match="unknown chat"):
        await model_options(host, "missing")


def test_local_picker_model_accepts_only_exact_scanned_identity():
    models = SimpleNamespace(scan_models=lambda: [{
        "path": r"C:\models\picked.gguf",
        "mmproj": r"C:\models\picked.mmproj",
    }])
    host = SimpleNamespace(
        router=SimpleNamespace(inference_runtime_id="llamacpp"),
        require_runtime=lambda: SimpleNamespace(models=models),
    )
    assert _local_picker_model(host, r"C:\models\picked.gguf") == (
        r"C:\models\picked.gguf",
        r"C:\models\picked.mmproj",
    )
    with pytest.raises(ValueError, match="no longer"):
        _local_picker_model(host, r"C:\models\missing.gguf")
