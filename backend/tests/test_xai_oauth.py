"""xAI Grok subscription OAuth: the device-code flow (xai_oauth.py) and the
router's OAuth-aware credential resolution (llm_router.py).

Network is faked with httpx.MockTransport; secretstore is patched to an identity
codec so the router tests don't depend on Windows DPAPI.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.parse
from unittest.mock import AsyncMock

import httpx
import pytest

import xai_oauth
from llm_router import LLMRouter, LocalEngineError


def _client(handler):
    """An AsyncClient whose requests are served by `handler(request)->Response`."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _cfg(client_id="cid-123"):
    return xai_oauth.XaiOAuthConfig(
        client_id=client_id,
        auth_base="https://accounts.x.ai",
        device_path="/oauth2/device/code",
        token_path="/oauth2/token",
        scope="grok",
    )


# --------------------------------------------------------------------------
# Pure parsers
# --------------------------------------------------------------------------
def test_parse_device_response_normalizes_field_aliases():
    grant = xai_oauth.parse_device_response({
        "device_code": "DEV", "user_code": "WXYZ-1234",
        "verification_url": "https://x.ai/device",           # _url alias
        "verification_url_complete": "https://x.ai/device?c=WXYZ-1234",
        "interval": 3, "expires_in": 600,
    })
    assert grant.device_code == "DEV"
    assert grant.verification_uri == "https://x.ai/device"
    assert grant.verification_uri_complete.endswith("c=WXYZ-1234")
    assert grant.interval == 3 and grant.expires_in == 600


def test_parse_device_response_rejects_missing_device_code():
    with pytest.raises(xai_oauth.XaiOAuthError):
        xai_oauth.parse_device_response({"user_code": "x"})


def test_parse_token_response_computes_absolute_expiry():
    tok = xai_oauth.parse_token_response(
        {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600, "scope": "grok"},
        now=1000)
    assert tok.access_token == "AT" and tok.refresh_token == "RT"
    assert tok.expires_at == 4600
    assert tok.token_type == "Bearer"


# --------------------------------------------------------------------------
# Wired defaults (xAI Grok CLI public client + discovered endpoints)
# --------------------------------------------------------------------------
def test_from_env_defaults_to_grok_cli_client_and_discovered_endpoints(monkeypatch):
    for var in ("VARIANT1_XAI_CLIENT_ID", "VARIANT1_XAI_AUTH_BASE", "VARIANT1_XAI_DEVICE_PATH",
                "VARIANT1_XAI_TOKEN_PATH", "VARIANT1_XAI_OAUTH_SCOPE"):
        monkeypatch.delenv(var, raising=False)
    cfg = xai_oauth.XaiOAuthConfig.from_env()
    assert cfg.client_id == xai_oauth.XAI_GROK_CLI_CLIENT_ID
    assert cfg.device_url == "https://auth.x.ai/oauth2/device/code"
    assert cfg.token_url == "https://auth.x.ai/oauth2/token"
    assert "api:access" in cfg.scope and "offline_access" in cfg.scope  # API + refresh token


def test_from_env_client_id_override(monkeypatch):
    monkeypatch.setenv("VARIANT1_XAI_CLIENT_ID", "my-own-authorized-client")
    assert xai_oauth.XaiOAuthConfig.from_env().client_id == "my-own-authorized-client"


def test_from_env_rejects_non_xai_authorization_host(monkeypatch):
    monkeypatch.setenv("VARIANT1_XAI_AUTH_BASE", "https://credential-catcher.invalid")
    with pytest.raises(xai_oauth.XaiOAuthError, match="x.ai subdomain"):
        xai_oauth.XaiOAuthConfig.from_env()


def test_validate_xai_https_url_rejects_http_and_embedded_credentials():
    with pytest.raises(xai_oauth.XaiOAuthError):
        xai_oauth.validate_xai_https_url("http://auth.x.ai")
    with pytest.raises(xai_oauth.XaiOAuthError):
        xai_oauth.validate_xai_https_url("https://user:pass@auth.x.ai")
    assert xai_oauth.validate_xai_https_url("https://api.x.ai/v1") == "https://api.x.ai/v1"


# --------------------------------------------------------------------------
# Device authorization request
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_request_device_code_sends_client_id_and_scope():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/device/code"
        seen.update(urllib.parse.parse_qs(request.content.decode()))
        return httpx.Response(200, json={
            "device_code": "DEV", "user_code": "CODE-1",
            "verification_uri": "https://accounts.x.ai/device",
            "verification_uri_complete": "https://accounts.x.ai/device?user_code=CODE-1",
            "interval": 5, "expires_in": 900,
        })

    async with _client(handler) as c:
        grant = await xai_oauth.request_device_code(_cfg(), client=c)

    assert seen["client_id"] == ["cid-123"]
    assert seen["scope"] == ["grok"]
    assert grant.user_code == "CODE-1"


@pytest.mark.asyncio
async def test_request_device_code_requires_client_id(monkeypatch):
    monkeypatch.delenv("VARIANT1_XAI_CLIENT_ID", raising=False)
    with pytest.raises(xai_oauth.XaiOAuthError):
        await xai_oauth.request_device_code(_cfg(client_id=""))


@pytest.mark.asyncio
async def test_request_device_code_surfaces_oauth_error():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_client",
                                         "error_description": "unknown client"})

    async with _client(handler) as c:
        with pytest.raises(xai_oauth.XaiOAuthError, match="invalid_client"):
            await xai_oauth.request_device_code(_cfg(), client=c)


# --------------------------------------------------------------------------
# Token polling
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_poll_token_waits_for_pending_then_succeeds():
    responses = [
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT",
                                  "expires_in": 3600}),
    ]

    def handler(request):
        assert request.url.path == "/oauth2/token"
        form = urllib.parse.parse_qs(request.content.decode())
        assert form["grant_type"] == [xai_oauth.DEVICE_GRANT_TYPE]
        assert form["device_code"] == ["DEV"]
        return responses.pop(0)

    grant = xai_oauth.DeviceCodeGrant("DEV", "C", "u", "u", interval=1, expires_in=900)
    sleep = AsyncMock()
    async with _client(handler) as c:
        tok = await xai_oauth.poll_token(grant=grant, cfg=_cfg(), client=c, sleep=sleep)

    assert tok.access_token == "AT" and tok.refresh_token == "RT"
    assert sleep.await_count == 2  # slept once per pending poll


@pytest.mark.asyncio
async def test_poll_token_backs_off_on_slow_down():
    responses = [
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json={"access_token": "AT", "expires_in": 60}),
    ]
    handler = lambda request: responses.pop(0)  # noqa: E731
    grant = xai_oauth.DeviceCodeGrant("DEV", "C", "u", "u", interval=5, expires_in=900)
    sleep = AsyncMock()
    async with _client(handler) as c:
        await xai_oauth.poll_token(grant=grant, cfg=_cfg(), client=c, sleep=sleep)
    # interval started at 5, slow_down bumps it by 5 → next sleep is 10s.
    sleep.assert_awaited_once_with(10)


@pytest.mark.asyncio
async def test_poll_token_access_denied_raises():
    handler = lambda request: httpx.Response(400, json={"error": "access_denied"})  # noqa: E731
    grant = xai_oauth.DeviceCodeGrant("DEV", "C", "u", "u", interval=1, expires_in=900)
    async with _client(handler) as c:
        with pytest.raises(xai_oauth.XaiOAuthError, match="denied"):
            await xai_oauth.poll_token(grant=grant, cfg=_cfg(), client=c, sleep=AsyncMock())


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refresh_reuses_old_refresh_token_when_provider_doesnt_rotate():
    def handler(request):
        form = urllib.parse.parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["OLD-RT"]
        return httpx.Response(200, json={"access_token": "NEW-AT", "expires_in": 3600})

    async with _client(handler) as c:
        tok = await xai_oauth.refresh(_cfg(), "OLD-RT", client=c)

    assert tok.access_token == "NEW-AT"
    assert tok.refresh_token == "OLD-RT"  # kept because the response omitted one


# --------------------------------------------------------------------------
# Router integration
# --------------------------------------------------------------------------
@pytest.fixture
def _identity_secrets(monkeypatch):
    """Avoid DPAPI in unit tests: store secrets verbatim with a tag we can see."""
    from security import secretstore
    monkeypatch.setattr(secretstore, "encrypt", lambda s: ("enc:" + s) if s else "")
    monkeypatch.setattr(secretstore, "decrypt",
                        lambda s: s[4:] if s.startswith("enc:") else s)


def _router(tmp_path):
    cfg = {"mode": "cloud", "local": {}, "sampling": {},
           "cloud": {"provider": "xai"}}
    return LLMRouter(cfg, str(tmp_path), config_path=str(tmp_path / "llm_config.json"))


def test_router_stores_and_returns_oauth_bearer(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    assert not r.has_cloud_key("xai")
    r.set_oauth_tokens("xai", client_id="cid", access_token="AT", refresh_token="RT",
                       expires_at=9_999_999_999)
    assert r.has_oauth("xai") and r.has_cloud_key("xai")
    assert r.get_oauth_access_token("xai") == "AT"
    assert r._credential_leases("xai")[0].secret == "AT"
    assert r._credential_leases("grok")[0].secret == "AT"


def test_router_oauth_takes_precedence_over_api_key(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    r.add_cloud_credential("xai", "SK-APIKEY", label="API key")
    assert r._credential_leases("xai")[0].secret == "SK-APIKEY"
    r.set_oauth_tokens("xai", client_id="cid", access_token="OAUTH-AT",
                       refresh_token="RT", expires_at=9_999_999_999)
    assert r._credential_leases("xai")[0].secret == "OAUTH-AT"


def test_router_refuses_custom_non_xai_base_for_oauth(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    r.set_oauth_tokens("xai", client_id="cid", access_token="OAUTH-AT",
                       refresh_token="RT", expires_at=9_999_999_999)
    r.set_provider_options("xai", {"base_url": "https://credential-catcher.invalid/v1"})
    lease = r._credential_leases("xai")[0]
    with pytest.raises(LocalEngineError, match="x.ai subdomain"):
        r.provider_base_url("xai", lease)


def test_router_oauth_status_hides_secrets(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    r.set_oauth_tokens("xai", client_id="cid", access_token="AT", refresh_token="RT",
                       scope="grok", expires_at=9_999_999_999, auth_flow="pkce")
    st = r.oauth_status("xai")
    assert st["connected"] and st["client_id_set"] and st["scope"] == "grok"
    assert st.get("auth_flow") == "pkce"
    assert st.get("transport") == "responses"
    assert "AT" not in str(st) and "RT" not in str(st)


def test_pkce_helpers_and_authorize_url():
    v = xai_oauth.pkce_code_verifier()
    c = xai_oauth.pkce_code_challenge(v)
    assert len(v) >= 43 and c and c != v
    url = xai_oauth.build_pkce_authorize_url(
        "https://auth.x.ai/oauth2/auth",
        client_id="cid",
        redirect_uri="http://127.0.0.1:56121/callback",
        code_challenge=c,
        state="abc",
        nonce="def",
        scope="openid api:access",
    )
    assert "code_challenge=" in url and "plan=generic" in url
    assert "code_challenge_method=S256" in url


def test_pkce_callback_requires_exact_state_and_first_valid_result_wins():
    expected_state = "expected-state-123"
    handler, result = xai_oauth._make_pkce_callback_handler(
        xai_oauth.XAI_PKCE_REDIRECT_PATH,
        expected_state,
    )
    server = xai_oauth.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    callback = (
        f"http://127.0.0.1:{server.server_address[1]}"
        f"{xai_oauth.XAI_PKCE_REDIRECT_PATH}"
    )
    try:
        with httpx.Client(timeout=2.0) as client:
            malformed = (
                {"code": "poison"},
                {"code": "poison", "state": "wrong-state"},
                {"state": expected_state},
                {"code": "   ", "state": expected_state},
                {"code": "poison", "error": "access_denied", "state": expected_state},
                [("code", "poison"), ("state", expected_state), ("state", "extra")],
            )
            for params in malformed:
                response = client.get(callback, params=params)
                assert response.status_code == 400
                assert result["complete"] is False
                assert result["code"] is None

            accepted = client.get(callback, params={
                "code": "real-code",
                "state": expected_state,
            })
            assert accepted.status_code == 200
            assert result["complete"] is True
            assert result["code"] == "real-code"
            assert result["state"] == expected_state

            duplicate = client.get(callback, params={
                "code": "replacement-code",
                "state": expected_state,
            })
            assert duplicate.status_code == 200
            assert result["code"] == "real-code"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


@pytest.mark.asyncio
async def test_exchange_pkce_code_sends_verifier_and_challenge():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(urllib.parse.parse_qs(request.content.decode()))
        return httpx.Response(200, json={
            "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
        })

    async with _client(handler) as c:
        tok = await xai_oauth.exchange_pkce_code(
            _cfg(),
            token_endpoint="https://accounts.x.ai/oauth2/token",
            code="AUTHCODE",
            redirect_uri="http://127.0.0.1:56121/callback",
            code_verifier="verifier-xyz",
            code_challenge="challenge-abc",
            client=c,
        )
    assert tok.access_token == "AT"
    assert seen["grant_type"] == ["authorization_code"]
    assert seen["code_verifier"] == ["verifier-xyz"]
    assert seen["code_challenge"] == ["challenge-abc"]
    assert seen["code_challenge_method"] == ["S256"]


def test_subscription_first_hides_api_key_when_oauth_present(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    r.cfg.setdefault("cloud", {})["xai_credential_policy"] = "subscription_first"
    r.add_cloud_credential("xai", "SK-APIKEY", label="API key")
    r.set_oauth_tokens("xai", client_id="cid", access_token="OAUTH-AT",
                       refresh_token="RT", expires_at=9_999_999_999, auth_flow="pkce")
    leases = r._credential_leases("xai")
    assert len(leases) == 1
    assert leases[0].source == "oauth" and leases[0].secret == "OAUTH-AT"


def test_try_both_includes_api_key_after_oauth(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    r.cfg.setdefault("cloud", {})["xai_credential_policy"] = "try_both"
    r.add_cloud_credential("xai", "SK-APIKEY", label="API key")
    r.set_oauth_tokens("xai", client_id="cid", access_token="OAUTH-AT",
                       refresh_token="RT", expires_at=9_999_999_999)
    sources = [L.source for L in r._credential_leases("xai")]
    assert "oauth" in sources
    assert "pool" in sources or any(s != "oauth" for s in sources)


def test_router_clear_oauth(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    r.set_oauth_tokens("xai", client_id="cid", access_token="AT", expires_at=9_999_999_999)
    r.clear_oauth("xai")
    assert not r.has_oauth("xai")
    assert r.oauth_status("xai")["connected"] is False


@pytest.mark.asyncio
async def test_ensure_oauth_fresh_refreshes_when_expiring(tmp_path, _identity_secrets, monkeypatch):
    r = _router(tmp_path)
    r.set_oauth_tokens("xai", client_id="cid", access_token="OLD", refresh_token="RT",
                       expires_at=1)  # already expired
    new = xai_oauth.TokenSet(access_token="FRESH", refresh_token="RT2",
                             token_type="Bearer", scope="grok", expires_at=9_999_999_999)
    refresh = AsyncMock(return_value=new)
    monkeypatch.setattr(xai_oauth, "refresh", refresh)

    ok = await r.ensure_oauth_fresh("xai")

    assert ok is True
    refresh.assert_awaited_once()
    assert r.get_oauth_access_token("xai") == "FRESH"
    assert r.oauth_status("xai")["expires_in"] > 0


@pytest.mark.asyncio
async def test_concurrent_oauth_refresh_uses_one_rotating_token_generation(
    tmp_path, _identity_secrets, monkeypatch,
):
    r = _router(tmp_path)
    r.set_oauth_tokens(
        "xai", client_id="cid", access_token="OLD", refresh_token="RT",
        expires_at=1,
    )
    calls = []

    async def refresh(_cfg, token):
        calls.append(token)
        await asyncio.sleep(0)
        return xai_oauth.TokenSet(
            access_token="FRESH", refresh_token="",
            token_type="Bearer", scope="grok", expires_at=9_999_999_999,
        )

    monkeypatch.setattr(xai_oauth, "refresh", refresh)

    assert await asyncio.gather(
        r.ensure_oauth_fresh("xai"), r.ensure_oauth_fresh("xai"),
    ) == [True, True]
    assert calls == ["RT"]
    assert r._oauth_rec("xai")["refresh_token"] == "enc:RT"


@pytest.mark.asyncio
async def test_ensure_oauth_fresh_skips_refresh_when_token_still_valid(tmp_path, _identity_secrets, monkeypatch):
    r = _router(tmp_path)
    r.set_oauth_tokens("xai", client_id="cid", access_token="AT", refresh_token="RT",
                       expires_at=9_999_999_999)
    refresh = AsyncMock()
    monkeypatch.setattr(xai_oauth, "refresh", refresh)

    ok = await r.ensure_oauth_fresh("xai")

    assert ok is True
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_oauth_fresh_noop_without_record(tmp_path, _identity_secrets):
    r = _router(tmp_path)
    assert await r.ensure_oauth_fresh("xai") is False


@pytest.mark.asyncio
async def test_expired_oauth_is_never_leased_after_refresh_failure(
    tmp_path, _identity_secrets, monkeypatch,
):
    r = _router(tmp_path)
    r.add_cloud_credential("xai", "API-FALLBACK", label="API")
    r.set_oauth_tokens(
        "xai", client_id="cid", access_token="EXPIRED", refresh_token="RT",
        expires_at=1, auth_flow="pkce",
    )
    r.cfg.setdefault("cloud", {})["xai_credential_policy"] = "subscription_first"
    monkeypatch.setattr(
        xai_oauth, "refresh", AsyncMock(side_effect=RuntimeError("refresh rejected"))
    )

    assert r.get_oauth_access_token("xai") == ""
    assert await r.ensure_oauth_fresh("xai") is False
    assert r._credential_leases("xai") == []


def test_oauth_rotation_clears_auth_failure_runtime_state(
    tmp_path, _identity_secrets,
):
    from model_providers.credentials import CredentialLease

    r = _router(tmp_path)
    lease = CredentialLease(
        "xai", "oauth", "Subscription", "OLD", source="oauth"
    )
    r.credential_pools.mark_failure(lease, status_code=401, detail="expired")
    assert r.credential_pools.available(lease) is True
    r.set_oauth_tokens(
        "xai", access_token="NEW", refresh_token="RT",
        expires_at=9_999_999_999,
    )
    assert r.credential_pools.available(lease) is True


def test_pkce_server_binds_only_registered_non_reusable_port(monkeypatch):
    calls = []

    class FakeServer:
        allow_reuse_address = True

        def __init__(self, address, _handler):
            calls.append((address, self.allow_reuse_address))
            self.server_address = address

        def serve_forever(self, **_kwargs):
            return None

    monkeypatch.setattr(xai_oauth, "ThreadingHTTPServer", FakeServer)
    server, thread, _result, redirect = xai_oauth._start_pkce_callback_server(
        expected_state="state"
    )
    thread.join(timeout=1)
    assert server.server_address == ("127.0.0.1", 56121)
    assert calls == [(("127.0.0.1", 56121), False)]
    assert redirect == "http://127.0.0.1:56121/callback"


def test_pkce_wait_stops_immediately_after_cancellation():
    calls = []
    server = type("Server", (), {
        "shutdown": lambda self: calls.append("shutdown"),
        "server_close": lambda self: calls.append("close"),
    })()
    thread = type("Thread", (), {
        "join": lambda self, timeout=None: calls.append(("join", timeout)),
    })()
    with pytest.raises(xai_oauth.XaiOAuthError, match="cancelled"):
        xai_oauth._wait_pkce_callback(
            server, thread, {"cancelled": True}, timeout=300
        )
    assert calls == ["shutdown", "close", ("join", 1.0)]
