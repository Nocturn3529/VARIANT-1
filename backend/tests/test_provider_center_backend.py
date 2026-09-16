from __future__ import annotations

import json

import pytest

import llm_router as router_module
from llm_router import LLMRouter
from llm_router_config import load_llm_config
from model_providers.custom_endpoints import endpoint_id, endpoint_url


def _router(tmp_path):
    path = tmp_path / "llm.json"
    return LLMRouter({
        "mode": "cloud",
        "local": {},
        "cloud": {"provider": "anthropic"},
        "sampling": {},
    }, str(tmp_path), config_path=str(path)), path


def test_custom_endpoint_is_a_durable_first_class_provider(tmp_path):
    router, path = _router(tmp_path)
    saved = router.save_custom_endpoint({
        "name": "Studio Rack",
        "base_url": "http://127.0.0.1:9001/v1/",
        "model": "rack-model",
        "context_length": 65536,
        "models": ["rack-model", "rack-small"],
        "make_default": True,
    })

    assert saved["id"] == "custom-studio-rack"
    assert saved["is_current"] is True
    profile = router.provider_profile(saved["id"])
    assert profile is not None
    assert profile.base_url == "http://127.0.0.1:9001/v1"
    assert router.get_cloud_model(saved["id"]) == "rack-model"
    assert router.context_limit_tokens({
        "mode": "cloud", "provider": saved["id"], "model": "rack-model",
    }) == 65536

    reloaded = LLMRouter(
        load_llm_config(str(path)), str(tmp_path), config_path=str(path),
    )
    assert reloaded.provider_profile(saved["id"]) is not None
    assert reloaded.list_custom_endpoints()[0]["models"] == [
        "rack-model", "rack-small",
    ]
    assert reloaded.remove_custom_endpoint(saved["id"]) is True
    assert reloaded.mode == "local"
    assert reloaded.provider_profile(saved["id"]) is None
    assert reloaded.list_custom_endpoints() == []
    assert saved["id"] not in path.read_text(encoding="utf-8")
    assert load_llm_config(str(path))["mode"] == "local"


def test_custom_endpoint_ids_and_urls_are_bounded():
    assert endpoint_id("", "My Local API") == "custom-my-local-api"
    assert endpoint_url("http://localhost:8080/v1/") == "http://localhost:8080/v1"
    with pytest.raises(ValueError, match="embedded credentials"):
        endpoint_url("https://user:secret@example.com/v1")
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        endpoint_url("file:///tmp/server")


@pytest.mark.asyncio
async def test_custom_endpoint_validation_discovers_models(tmp_path, monkeypatch):
    router, _path = _router(tmp_path)
    captured = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "alpha"}, {"name": "beta"}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers):
            captured.update(url=url, headers=headers)
            return Response()

    monkeypatch.setattr(router_module.httpx, "AsyncClient", Client)
    result = await router.validate_custom_endpoint({
        "name": "Probe",
        "base_url": "https://inference.example/v1",
        "api_key": "private",
    })

    assert result["ok"] is True
    assert result["models"] == ["alpha", "beta"]
    assert captured["url"] == "https://inference.example/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer private"


def test_provider_catalog_exposes_hermes_style_auth_metadata(tmp_path):
    router, _path = _router(tmp_path)
    providers = {item["name"]: item for item in router.list_provider_info()}

    assert {"oauth", "api_key"} <= set(providers["xai"]["auth_methods"])
    assert providers["openai-codex"]["auth_methods"] == ["oauth"]
    assert providers["ollama"]["auth_methods"] == ["external"]
    assert providers["opencode-free"]["auth_methods"] == []
    assert providers["fireworks"]["credential_env_vars"] == [
        "FIREWORKS_API_KEY",
    ]
    assert providers["fireworks"]["api_key_configured"] is False
    assert len(providers) >= 45


def test_settings_key_contract_replaces_and_clears_one_saved_key(tmp_path):
    router, _path = _router(tmp_path)
    router.add_cloud_credential("openai", "first", label="old one")
    router.add_cloud_credential("openai", "second", label="old two")

    saved = router.replace_cloud_credential("openai", "replacement")

    assert saved["label"] == "OpenAI API key"
    assert len(router.list_cloud_credentials("openai")) == 1
    assert router._credential_leases("openai")[0].secret == "replacement"
    provider_rows = {row["name"]: row for row in router.list_provider_info()}
    assert provider_rows["openai"]["api_key_configured"] is True

    assert router.clear_cloud_credentials("openai") is True
    assert router.list_cloud_credentials("openai") == []


def test_custom_endpoint_key_edit_replaces_instead_of_growing_a_pool(tmp_path):
    router, _path = _router(tmp_path)
    endpoint = {
        "name": "Local Rack",
        "base_url": "http://127.0.0.1:9900/v1",
        "model": "rack-model",
    }
    saved = router.save_custom_endpoint({**endpoint, "api_key": "first"})
    router.save_custom_endpoint({
        **endpoint,
        "id": saved["id"],
        "api_key": "replacement",
    })

    assert len(router.list_cloud_credentials(saved["id"])) == 1
    assert router._credential_leases(saved["id"])[0].secret == "replacement"


def test_custom_endpoint_default_and_activation_switch_router_to_cloud(tmp_path):
    router, path = _router(tmp_path)
    router.set_mode("local")
    saved = router.save_custom_endpoint({
        "name": "Local Bridge",
        "base_url": "http://127.0.0.1:9901/v1",
        "model": "bridge-model",
    })

    assert router.mode == "local"
    assert saved["is_current"] is False
    activated = router.activate_custom_endpoint(saved["id"])
    assert activated["is_current"] is True
    assert router.mode == "cloud"
    assert load_llm_config(str(path))["mode"] == "cloud"

    router.set_mode("local")
    made_default = router.save_custom_endpoint({
        "id": saved["id"],
        "name": "Local Bridge",
        "base_url": "http://127.0.0.1:9901/v1",
        "model": "bridge-model",
        "make_default": True,
    })
    assert router.mode == "cloud"
    assert made_default["is_current"] is True


def test_legacy_process_default_provider_selection_commits_cloud_mode(tmp_path):
    router, path = _router(tmp_path)
    router.set_mode("local")

    router.set_cloud_provider("xai")

    assert router.mode == "cloud"
    persisted = load_llm_config(str(path))
    assert persisted["mode"] == "cloud"
    assert persisted["cloud"]["provider"] == "xai"
