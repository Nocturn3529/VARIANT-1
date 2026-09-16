from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time
from urllib.parse import parse_qs

import httpx
import pytest

import background_tasks
import minimax_oauth
import ws_config
from llm_router import LLMRouter
from model_providers.registry import ProviderRegistry


def _router(tmp_path) -> LLMRouter:
    return LLMRouter(
        {"mode": "cloud", "local": {}, "cloud": {"provider": "anthropic"}, "sampling": {}},
        str(tmp_path), config_path=str(tmp_path / "llm.json"),
    )


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(dict(payload))


def _handlers():
    found = {}

    def on(name):
        def register(handler):
            found[name] = handler
            return handler
        return register

    ws_config.register(on)
    return found


def _server(router):
    broadcasts = []

    async def broadcast(payload):
        broadcasts.append(payload)

    runtime = SimpleNamespace(models=SimpleNamespace(config_status=lambda: {"type": "config"}))
    server = SimpleNamespace(
        router=router, hub=SimpleNamespace(broadcast=broadcast),
        require_runtime=lambda: runtime,
        engine_status_message=lambda: {"type": "engine"},
    )
    return server, broadcasts


@pytest.mark.asyncio
async def test_minimax_user_code_pkce_and_refresh_use_fixed_region_endpoints():
    seen = []

    def transport(request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        seen.append((str(request.url), form))
        if request.url.path.endswith("/oauth/code"):
            return httpx.Response(200, json={
                "state": form["state"], "user_code": "ABCD-1234",
                "verification_uri": "https://api.minimax.io/verify",
                "expired_in": 300, "interval": 2000,
            })
        if form.get("grant_type") == "refresh_token":
            return httpx.Response(200, json={
                "status": "success", "access_token": "fresh-access",
                "expired_in": 900,
            })
        return httpx.Response(200, json={
            "status": "success", "access_token": "access",
            "refresh_token": "refresh", "expired_in": 900,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as client:
        grant = await minimax_oauth.request_user_code("minimax-oauth", client=client)
        tokens = await minimax_oauth.poll_user_code(grant, client=client)
        refreshed = await minimax_oauth.refresh("minimax-oauth", tokens.refresh_token, client=client)
    assert grant.user_code == "ABCD-1234"
    assert tokens.access_token == "access"
    assert refreshed.access_token == "fresh-access"
    assert refreshed.refresh_token == "refresh"
    assert all(url.startswith("https://api.minimax.io/oauth/") for url, _form in seen)
    assert seen[0][1]["code_challenge_method"] == "S256"
    assert seen[1][1]["code_verifier"] == grant.code_verifier
    assert seen[2][1]["grant_type"] == "refresh_token"


@pytest.mark.asyncio
async def test_oauth_request_id_prevents_old_cancel_from_colliding_with_new_same_provider_flow(monkeypatch):
    monkeypatch.setattr(
        background_tasks, "spawn",
        lambda coro, name="", **_kwargs: asyncio.create_task(coro, name=name),
    )
    calls = 0
    waiting = asyncio.Event()

    async def grant(provider):
        nonlocal calls
        calls += 1
        return minimax_oauth.UserCodeGrant(
            provider=provider, user_code=f"code-{calls}",
            verification_uri="https://api.minimax.io/verify",
            code_verifier="private-verifier", expires_at=9_999_999_999,
            interval_s=2,
        )

    async def poll(value):
        if value.user_code == "code-1":
            await waiting.wait()
        return minimax_oauth.TokenSet("access", "refresh", "Bearer", minimax_oauth.SCOPE, 9_999_999_999)

    monkeypatch.setattr(minimax_oauth, "request_user_code", grant)
    monkeypatch.setattr(minimax_oauth, "poll_user_code", poll)
    saved = []
    router = SimpleNamespace(
        _kn=lambda value: value,
        set_oauth_tokens=lambda provider, **fields: saved.append((provider, fields)),
        oauth_status=lambda provider: {"provider": provider, "connected": bool(saved)},
    )
    server, _broadcasts = _server(router)
    socket = Socket()
    handlers = _handlers()
    await handlers["cloud:oauth:start"](
        server, socket, None,
        {"provider": "minimax-oauth", "request_id": "flow-a", "open_browser": False},
    )
    for _ in range(20):
        if any(row["type"] == "cloud:oauth:pending" for row in socket.sent):
            break
        await asyncio.sleep(0)
    assert any(row.get("request_id") == "flow-a" for row in socket.sent)
    await handlers["cloud:oauth:cancel"](
        server, socket, None,
        {"provider": "minimax-oauth", "request_id": "older-flow"},
    )
    assert socket.sent[-1]["cancelled"] is False
    assert server._oauth_attempts["minimax-oauth"]["id"] == "flow-a"
    await handlers["cloud:oauth:cancel"](
        server, socket, None,
        {"provider": "minimax-oauth", "request_id": "flow-a"},
    )
    assert socket.sent[-1]["cancelled"] is True
    assert saved == []
    await handlers["cloud:oauth:start"](
        server, socket, None,
        {"provider": "minimax-oauth", "request_id": "flow-b", "open_browser": False},
    )
    for _ in range(30):
        if saved:
            break
        await asyncio.sleep(0)
    assert saved[0][0] == "minimax-oauth"
    assert saved[0][1]["auth_flow"] == "user_code_pkce"
    assert any(row["type"] == "cloud:oauth:complete" and row["request_id"] == "flow-b" for row in socket.sent)


@pytest.mark.asyncio
async def test_dpapi_key_pool_lifecycle_is_exposed_without_echoing_secrets(tmp_path):
    router = _router(tmp_path)
    server, _broadcasts = _server(router)
    socket = Socket()
    handlers = _handlers()
    for index in (1, 2):
        await handlers["cloud:credential:add"](
            server, socket, None,
            {"provider": "openai", "request_id": f"add-{index}",
             "key": f"secret-{index}", "label": f"Key {index}"},
        )
    records = router.list_cloud_credentials("openai")
    assert len(records) == 2
    assert all("secret" not in row for row in records)
    first, second = records
    await handlers["cloud:credential:enable"](
        server, socket, None,
        {"provider": "openai", "credential_id": first["id"], "enabled": False, "request_id": "disable"},
    )
    await handlers["cloud:credential:priority:set"](
        server, socket, None,
        {"provider": "openai", "credential_id": second["id"], "priority": -2, "request_id": "priority"},
    )
    await handlers["cloud:credential:strategy:set"](
        server, socket, None,
        {"provider": "openai", "strategy": "round_robin", "request_id": "strategy"},
    )
    assert router.credential_pools.strategy("openai") == "round_robin"
    assert router._credential_leases("openai")[0].secret == "secret-2"
    await handlers["cloud:credential:remove"](
        server, socket, None,
        {"provider": "openai", "credential_id": second["id"], "request_id": "remove"},
    )
    assert len(router.list_cloud_credentials("openai")) == 1
    assert "secret-1" not in (tmp_path / "llm.json").read_text(encoding="utf-8")
    assert "secret-2" not in (tmp_path / "llm.json").read_text(encoding="utf-8")
    assert all("secret-" not in json.dumps(row) for row in socket.sent)


def test_provider_plugin_catalog_tracks_origin_and_rolls_back_a_broken_override(tmp_path):
    root = tmp_path / "providers"
    good = root / "good"
    good.mkdir(parents=True)
    (good / "provider.json").write_text(json.dumps({
        "name": "openai", "display_name": "OpenAI Plugin",
        "aliases": ["new-openai"], "version": "2.1",
    }), encoding="utf-8")
    broken = root / "zz-broken"
    broken.mkdir()
    (broken / "provider.json").write_text(json.dumps({
        "name": "xai", "aliases": ["wrong-xai"],
    }), encoding="utf-8")
    (broken / "provider.py").write_text("def register(registry):\n    raise RuntimeError('broken provider')\n", encoding="utf-8")
    registry = ProviderRegistry(plugin_dirs=[root])
    assert registry.get("new-openai").display_name == "OpenAI Plugin"
    assert registry.origin("openai")["version"] == "2.1"
    assert registry.get("gpt") is None  # overridden profile retired its old alias
    assert registry.get("grok").name == "xai"
    assert registry.get("wrong-xai") is None
    assert registry.origin("xai")["kind"] == "builtin"
    assert len(registry.errors) == 1
    assert len(registry.plugin_catalog()) == 1


def test_custom_endpoint_stays_inside_provider_registry_and_has_explicit_origin(tmp_path):
    router = _router(tmp_path)
    endpoint = router.save_custom_endpoint({
        "name": "Local Rack", "base_url": "http://127.0.0.1:8000/v1",
        "model": "rack-model",
    })
    info = next(item for item in router.list_provider_info() if item["name"] == endpoint["id"])
    assert info["origin"]["kind"] == "custom_endpoint"
    assert info["auth_methods"] == ["custom"]


@pytest.mark.asyncio
async def test_minimax_oauth_refresh_uses_dpapi_record_and_fixed_inference_host(tmp_path, monkeypatch):
    router = _router(tmp_path)
    router.set_oauth_tokens(
        "minimax-oauth", client_id=minimax_oauth.CLIENT_ID,
        access_token="old-access", refresh_token="refresh-token",
        expires_at=int(time.time()) - 1, auth_flow="user_code_pkce",
        replace=True,
    )

    async def refreshed(provider, refresh_token):
        assert provider == "minimax-oauth" and refresh_token == "refresh-token"
        return minimax_oauth.TokenSet(
            "fresh-access", "rotated-refresh", "Bearer", minimax_oauth.SCOPE,
            int(time.time()) + 900,
        )

    monkeypatch.setattr(minimax_oauth, "refresh", refreshed)
    assert await router.ensure_oauth_fresh("minimax-oauth") is True
    leases = router._credential_leases("minimax-oauth")
    assert len(leases) == 1
    assert leases[0].source == "oauth" and leases[0].secret == "fresh-access"
    router.set_provider_options("minimax-oauth", {"base_url": "https://wrong.example/v1"})
    assert router.provider_base_url("minimax-oauth", leases[0]) == "https://api.minimax.io/anthropic"
    assert router.oauth_required_for_route("minimax-oauth") is True
    saved = (tmp_path / "llm.json").read_text(encoding="utf-8")
    assert "fresh-access" not in saved and "rotated-refresh" not in saved
