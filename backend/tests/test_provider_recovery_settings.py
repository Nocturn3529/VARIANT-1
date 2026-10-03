"""Settings saves preserve authoritative routes and reject stale drafts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_router import LLMRouter
from model_runtime.request_policy import project_reasoning_policy
import provider_recovery_settings as settings
import ws_provider_recovery


@pytest.fixture
def router(tmp_path):
    result = LLMRouter({"mode": "cloud", "cloud": {"provider": "openrouter"}}, str(tmp_path), data_dir=str(tmp_path))
    result.config_path = str(tmp_path / "llm-config.json")
    return result


def test_durable_save_and_stale_other_window_draft(router):
    original = settings.snapshot(router)
    assert original["config"]["enabled"] is False
    config = {**original["config"], "enabled": True,
              "fallback_routes": [{"mode": "cloud", "provider": "openrouter", "model": "stealth/space-bunny-alpha", "reasoning_effort": "max"}]}
    updated = settings.save(router, {"config": config, "expected_revision": original["revision"]})
    assert updated["config"] == config and updated["revision"] != original["revision"]
    assert json.loads(Path(router.config_path).read_text(encoding="utf-8"))["provider_recovery"] == config
    with pytest.raises(settings.SettingsConflict):
        settings.save(router, {"config": original["config"], "expected_revision": original["revision"]})
    assert settings.snapshot(router) == updated


def test_save_validates_routes_and_rejects_credential_fields(router):
    current = settings.snapshot(router)
    for extra in ({"api_key": "private-test-value"}, {"fallback_routes": [{"provider": "missing-provider", "model": "some-model"}]},
                  {"fallback_routes": [{"provider": "openrouter", "model": "some-model", "reasoning_effort": "unsupported"}]}):
        with pytest.raises(settings.SettingsValidation):
            settings.save(router, {"expected_revision": current["revision"], "config": {**current["config"], **extra}})
    assert settings.snapshot(router) == current


@pytest.mark.asyncio
async def test_save_failure_has_correlated_secret_free_error_and_rolls_back(router, monkeypatch):
    handlers = {}
    def on(*names):
        def decorate(fn):
            handlers.update(dict.fromkeys(names, fn))
            return fn
        return decorate
    ws_provider_recovery.register(on)
    sent = []
    async def send_json(value):
        sent.append(value)
    def fail(**kwargs):
        raise OSError("private-test-value persistence failure")
    monkeypatch.setattr(router, "save_config", fail)
    before = settings.snapshot(router)
    await handlers["provider-recovery:set"](SimpleNamespace(router=router), SimpleNamespace(send_json=send_json), None,
        {"type": "provider-recovery:set", "request_id": "save-one", "expected_revision": before["revision"],
         "config": {**before["config"], "enabled": True}})
    assert sent[0]["request_id"] == "save-one" and sent[0]["ok"] is False
    assert sent[0]["error"]["code"] == "routing_setting_failed"
    assert "private-test-value" not in json.dumps(sent)
    assert settings.snapshot(router) == before


def test_openrouter_max_effort_survives_route_and_wire_projection(router):
    model = "stealth/space-bunny-alpha"
    with router.bind_model_route({"mode": "cloud", "provider": "openrouter", "model": model, "reasoning_effort": "max"}):
        assert router.bound_model_route()["reasoning_effort"] == "max"
        payload = {}
        effort = project_reasoning_policy(router, router.provider_profile("openrouter"), model, payload, None)
    assert effort == "max" and payload == {"reasoning": {"effort": "max"}}
