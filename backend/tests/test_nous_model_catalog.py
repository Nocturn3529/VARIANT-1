"""Exact Nous catalog contracts through the native picker and serialized cloud request."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import llm_cloud_stream as cloud
from llm_router import LLMRouter
from model_options import _provider_row
from model_providers import ProviderRequestError
from model_runtime import hermes_proxy
from model_runtime.context import normalize_model_route
from model_runtime.request_policy import project_reasoning_policy
from session_catalog.service import IPYTHON_PROVIDER_SPEC
from session_catalog.support import ACTION_SURFACE, SupportMatrix, SupportRule, UnsupportedModelRoute
from tests.support.conversation_sessions import open_sessions
from tests.test_model_request_adapter_hooks import _install_transport, _wire_payload


QWEN = "qwen/qwen4-max:free"
QWEN_PAID = "qwen/qwen4-max"
STEP = "stepfun/step-3.7-flash:free"
HY = "tencent/hy4-flash:free"
LONGCAT = "meituan/longcat-2.5:free"
SOLAR = "upstage/solar-mini4:free"
RETIRED = "meituan/longcat-2.5-preview:free"


def model(model_id, context, **reasoning):
    return {"id": model_id, "context_length": context, "supported_parameters": ["reasoning", "tools"],
            "reasoning": reasoning}


ROWS = [
    model(QWEN, 1_000_000, mandatory=False, default_enabled=True, default_effort="high",
          supported_efforts=["xhigh", "high", "low", "none"]),
    model(STEP, 262144, mandatory=True, default_effort="medium", supported_efforts=["high", "medium", "low"]),
    model(HY, 1048576, mandatory=False, default_enabled=True),
    {"id": LONGCAT, "context_length": 1048576, "supported_parameters": ["reasoning", "tools"]},
    model(SOLAR, 524288, mandatory=False, default_enabled=False, default_effort="medium",
          supported_efforts=["max", "xhigh", "high", "medium", "low", "minimal", "none"]),
    {"id": QWEN_PAID, "context_length": 123456, "supported_parameters": ["tools"]},
]


@pytest.fixture
def nous(tmp_path, monkeypatch):
    listing = AsyncMock(return_value=ROWS)
    monkeypatch.setattr(hermes_proxy, "available_model_catalog", listing)
    monkeypatch.setattr(hermes_proxy, "ensure_proxy", AsyncMock())
    coverage = [{"profile": ACTION_SURFACE, "provider": "hermes", "model": row["id"],
                 "adapter": "openai.*", "status": "canary"} for row in ROWS]
    router = LLMRouter({"mode": "cloud", "cloud": {"provider": "hermes"},
                        "action_surface": {"support_matrix": coverage}}, str(tmp_path),
                       config_path=str(tmp_path / "config.json"))
    return router, listing


@pytest.mark.asyncio
@pytest.mark.parametrize(("mid", "preference", "budget", "expected"), [
    (QWEN, {}, None, {"enabled": True, "effort": "high"}),
    (QWEN, {"reasoning_effort": "xhigh"}, None, {"enabled": True, "effort": "xhigh"}),
    (QWEN, {"reasoning_effort": "none"}, None, {"enabled": False}),
    (QWEN, {"reasoning_effort": "max"}, None, {"enabled": True, "effort": "high"}),
    (QWEN, {}, 0, {"enabled": False}),
    (STEP, {}, 0, {"enabled": True, "effort": "low"}),
    (STEP, {"reasoning_effort": "none"}, None, {"enabled": True, "effort": "medium"}),
    (HY, {}, None, None),
    (HY, {"reasoning_enabled": False}, None, {"enabled": False}),
    (HY, {"reasoning_enabled": True}, None, {"enabled": True}),
    (HY, {}, 0, {"enabled": False}),
    (LONGCAT, {"reasoning_effort": "max"}, None, None),
    (LONGCAT, {}, 0, None),
    (QWEN_PAID, {"reasoning_effort": "xhigh"}, None, None),
    (SOLAR, {}, None, {"enabled": False}),
    (SOLAR, {"reasoning_effort": "medium"}, None, {"enabled": True, "effort": "medium"}),
])
async def test_normal_dispatch_refreshes_cold_catalog_and_keeps_pinned_id(nous, monkeypatch, mid, preference, budget, expected):
    router, listing = nous
    sse = 'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    captured = _install_transport(monkeypatch, cloud, sse)
    route = {"mode": "cloud", "provider": "hermes", "model": mid, **preference}
    with router.bind_model_route(route, allow_recovery=False):
        out = [token async for token in cloud.call_cloud(router, [{"role": "user", "content": "test"}],
                                                         {"max_tokens": 16}, reasoning_budget=budget,
                                                         tools=[IPYTHON_PROVIDER_SPEC])]
    assert out == ["ok"]
    payload = _wire_payload(captured)
    assert payload["model"] == mid and payload.get("reasoning") == expected
    assert "reasoning_effort" not in payload
    assert captured[0].url.path == "/v1/chat/completions"
    assert [item["function"]["name"] for item in payload["tools"]] == ["ipython"]
    manifest = router.model_request_manifest_snapshot()["items"][-1]
    assert manifest["generation"]["thinking_enabled"] == (expected.get("enabled") if expected else None)
    if mid == HY and "reasoning_enabled" in preference:
        assert manifest["route"]["reasoning_enabled"] is preference["reasoning_enabled"]
    await router.prepare_nous_model(mid)
    listing.assert_awaited_once()


@pytest.mark.asyncio
async def test_picker_preserves_missing_session_pin_and_separates_free_and_paid(nous):
    router, listing = nous
    assert router.get_cloud_model("hermes") == LONGCAT
    router.cfg["cloud"]["hermes_model"] = RETIRED
    current = {"mode": "cloud", "provider": "hermes", "model": RETIRED}
    provider = {"name": "hermes", "model": RETIRED, "default_model": LONGCAT}
    row = await _provider_row(SimpleNamespace(router=router), provider, current, refresh=True,
                              semaphore=asyncio.Semaphore(1))
    models = {item["id"]: item for item in row["models"]}
    assert not models[RETIRED]["selectable"] and "Unavailable" in models[RETIRED]["detail"]
    assert models[QWEN]["reasoning_efforts"] == ["none", "low", "high", "xhigh"]
    assert models[QWEN]["default_reasoning_effort"] == "high"
    assert models[HY]["reasoning_toggle"] and models[HY]["reasoning_default_enabled"]
    assert models[STEP]["reasoning_mandatory"] and not models[STEP].get("reasoning_toggle")
    assert models[SOLAR]["default_reasoning_effort"] == "none"
    assert not models[QWEN_PAID]["reasoning_efforts"] and not models[LONGCAT]["reasoning_efforts"]
    for item in ROWS:
        route = {**current, "model": item["id"]}
        assert router.context_limit_tokens(route) == item["context_length"]
    with pytest.raises(ProviderRequestError, match="unavailable"):
        await router.prepare_nous_model(RETIRED)
    assert router.get_cloud_model("hermes") == RETIRED
    listing.return_value = [model(QWEN, 99999, supported_efforts=["low"], default_effort="low")]
    await router.list_cloud_models("hermes")
    assert router.nous_model_available(LONGCAT) is False
    assert router.reasoning_efforts("hermes", QWEN) == ("low",)


@pytest.mark.asyncio
@pytest.mark.parametrize("specific_first", [False, True])
async def test_picker_and_request_share_first_matching_install_rule(nous, specific_first):
    router, _ = nous
    wildcard = {"patterns": ["QWEN/*"], "efforts": [], "effort_field": ""}
    specific = {"patterns": [QWEN.upper()], "efforts": ["max"], "effort_field": "reasoning.effort"}
    rules = [specific, wildcard] if specific_first else [wildcard, specific]
    router.cfg["cloud"]["provider_options"] = {"hermes": {"profile": {"reasoning_model_rules": rules}}}
    await router.list_cloud_models("hermes")
    selection = normalize_model_route(router, {"mode": "cloud", "provider": "hermes",
                                               "model": QWEN, "reasoning_effort": "max"})
    assert selection.get("reasoning_efforts", []) == (["max"] if specific_first else [])
    wire = {}
    with router.bind_model_route(selection):
        project_reasoning_policy(router, router.provider_profile("hermes"), QWEN, wire, None)
    assert wire == ({"reasoning": {"effort": "max"}} if specific_first else {})


@pytest.mark.asyncio
async def test_session_switch_validates_exact_model_before_saving_and_echoes_false(nous, tmp_path):
    import ws_dispatch
    from tests.test_session_model_routing import _Socket
    router, _ = nous
    await router.list_cloud_models("hermes")
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    sessions.set_model_route(sid, {"mode": "cloud", "provider": "hermes", "model": RETIRED})
    runtime = SimpleNamespace(sessions=sessions, session_runtimes=SimpleNamespace(
        is_busy=lambda _: False, ensure_runtime=lambda _: SimpleNamespace(
            identity=SimpleNamespace(action_surface=ACTION_SURFACE), mutation_write_enabled=False)))
    host = SimpleNamespace(router=router, require_runtime=lambda: runtime, _compress_messages=None)
    session = SimpleNamespace(busy=False, viewed_session_id=sid)
    socket = _Socket()

    async def select(mid, **fields):
        await ws_dispatch.HANDLERS["mode:set"](host, socket, session, {"type": "mode:set", "scope": "session",
            "id": sid, "mode": "cloud", "provider": "hermes", "model": mid, **fields})
        return socket.messages[-1]

    assert (await select(RETIRED))["code"] == "model_unavailable"
    assert (await select(QWEN, reasoning_effort="max"))["code"] == "unsupported_reasoning_effort"
    assert (await select(STEP, reasoning_enabled=False))["code"] == "unsupported_reasoning_toggle"
    assert sessions.get_model_route(sid)["model"] == RETIRED
    saved = await select(HY, reasoning_enabled=False)
    assert saved["type"] == "chat:context" and saved["reasoning_enabled"] is False
    assert open_sessions(tmp_path / "chats").get_model_route(sid)["reasoning_enabled"] is False
    switched = await select(QWEN, reasoning_effort="none")
    assert switched["reasoning_effort"] == "none" and "reasoning_enabled" not in switched
    assert sessions.get_model_route(sid) == {"mode": "cloud", "provider": "hermes", "model": QWEN,
                                           "reasoning_effort": "none"}


@pytest.mark.asyncio
async def test_live_catalog_capabilities_cannot_override_explicit_qualification_denial(nous, monkeypatch):
    router, listing = nous
    await router.list_cloud_models("hermes")
    denied = SupportRule(profile=ACTION_SURFACE, provider="hermes", model=QWEN, adapter="*", status="revoked")
    router._support_matrix = SupportMatrix((*router._support_matrix.rules, denied))
    with router.bind_model_route({"mode": "cloud", "provider": "hermes", "model": QWEN}):
        with pytest.raises(UnsupportedModelRoute):
            _ = [part async for part in cloud.call_cloud(router, [], {}, tools=[IPYTHON_PROVIDER_SPEC])]
    hermes_proxy.ensure_proxy.assert_not_awaited()
    listing.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_route_accepts_only_verified_toggle_and_retains_false(nous):
    import provider_recovery_settings as settings
    router, _ = nous
    await router.list_cloud_models("hermes")
    initial = settings.snapshot(router)
    route = {"mode": "cloud", "provider": "hermes", "model": HY, "reasoning_enabled": False}
    config = {**initial["config"], "fallback_routes": [route]}
    saved = settings.save(router, {"expected_revision": initial["revision"], "config": config})
    assert saved["config"]["fallback_routes"] == [route]
    for invalid in ({**route, "model": STEP}, {**route, "model": QWEN_PAID},
                    {**route, "reasoning_enabled": "false"}, {**route, "reasoning_effort": "low"}):
        with pytest.raises(settings.SettingsValidation):
            settings.save(router, {"expected_revision": saved["revision"],
                                   "config": {**config, "fallback_routes": [invalid]}})
    assert settings.snapshot(router)["revision"] == saved["revision"]
