import base64
import json
import time

import httpx
import pytest

import openai_codex_oauth as codex_oauth
from llm_router import LLMRouter


def _jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def _access_token(*, expires_in: int = 3600, account_id: str = "acct-test") -> str:
    return _jwt({
        "exp": int(time.time()) + expires_in,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    })


def _write_codex_auth(home, access: str) -> None:
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access,
            "refresh_token": "refresh-test",
            "account_id": "acct-test",
        },
    }), encoding="utf-8")


def test_codex_base_url_is_pinned():
    assert codex_oauth.validate_codex_base_url(
        "https://chatgpt.com/backend-api/codex/"
    ) == codex_oauth.CODEX_RESPONSES_BASE
    for invalid in (
        "http://chatgpt.com/backend-api/codex",
        "https://example.test/backend-api/codex",
        "https://chatgpt.com/backend-api/codex/extra",
        "https://chatgpt.com/backend-api/codex?forward=1",
    ):
        with pytest.raises(codex_oauth.OpenAICodexOAuthError):
            codex_oauth.validate_codex_base_url(invalid)


def test_explicit_codex_cli_link_resolves_runtime_only_token(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    access = _access_token()
    _write_codex_auth(home, access)
    monkeypatch.setenv("CODEX_HOME", str(home))
    router = LLMRouter({
        "mode": "cloud",
        "cloud": {
            "provider": "openai-codex",
            "oauth": {
                "openai-codex": {
                    "auth_flow": "codex_cli",
                    "managed_external": True,
                }
            },
            "provider_options": {
                "openai-codex": {"base_url": "https://evil.example/v1"}
            },
        },
    }, str(tmp_path))

    assert router.has_oauth("openai-codex") is True
    lease = router._credential_leases("openai-codex")[0]
    assert lease.secret == access
    assert lease.source == "codex_cli_oauth"
    assert router.provider_base_url("openai-codex", lease) == (
        codex_oauth.CODEX_RESPONSES_BASE
    )
    assert "refresh-test" not in repr(router.oauth_status("openai-codex"))


def test_expired_managed_access_remains_linked_for_preflight_refresh(
    tmp_path, monkeypatch,
):
    home = tmp_path / "codex"
    _write_codex_auth(home, _access_token(expires_in=-60))
    monkeypatch.setenv("CODEX_HOME", str(home))

    status = codex_oauth.codex_cli_status()
    assert status["connected"] is True
    assert status["usable"] is False
    assert status["needs_refresh"] is True


@pytest.mark.asyncio
async def test_codex_model_listing_uses_account_header_and_filters_hidden():
    access = _access_token()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {access}"
        assert request.headers["chatgpt-account-id"] == "acct-test"
        assert request.headers["originator"] == "VARIANT-1"
        return httpx.Response(200, json={"models": [
            {"slug": "gpt-5.6-luna", "visibility": "list"},
            {"slug": "internal", "visibility": "hidden"},
            {"slug": "gpt-5.3-codex-spark", "visibility": "list"},
        ]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        assert await codex_oauth.list_models(access, client=client) == [
            "gpt-5.6-luna", "gpt-5.3-codex-spark",
        ]


@pytest.mark.asyncio
async def test_codex_refresh_keeps_non_rotating_refresh_token():
    fresh_access = _access_token(expires_in=7200)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(codex_oauth.CODEX_TOKEN_URL)
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(200, json={"access_token": fresh_access})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        tokens = await codex_oauth.refresh("refresh-old", client=client)
    assert tokens.access_token == fresh_access
    assert tokens.refresh_token == "refresh-old"


@pytest.mark.asyncio
async def test_variant1_owned_account_id_is_used_when_token_omits_claim():
    access = _jwt({"exp": int(time.time()) + 3600})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["chatgpt-account-id"] == "acct-from-device-flow"
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        assert await codex_oauth.list_models(
            access, account_id="acct-from-device-flow", client=client
        ) == []
