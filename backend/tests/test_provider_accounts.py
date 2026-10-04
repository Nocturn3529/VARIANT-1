import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_router import LLMRouter
from model_runtime.provider_accounts import account_snapshot, check_account, invalidate_account
from model_runtime import hermes_proxy


def router():
    return LLMRouter({"cloud": {"provider": "hermes"}}, ".")


def test_optional_auth_does_not_claim_a_checked_connection():
    r = router()
    rows = {row["name"]: row for row in r.list_provider_info()}
    for name in ("hermes", "ollama", "lmstudio"):
        assert rows[name]["connection"]["state"] == "unchecked"
    assert "oauth" in rows["hermes"]["auth_methods"]
    assert r.oauth_status("hermes")["connected"] is False


@pytest.mark.asyncio
async def test_hermes_checks_model_access_without_inference_or_token_import(monkeypatch):
    listing = AsyncMock(return_value=["meituan/longcat-2.5-preview:free"])
    monkeypatch.setattr(hermes_proxy, "available_models", listing)
    r = router()
    checked = await check_account(r, "hermes")
    assert checked["state"] == "ready"
    assert "inference has not been tested" in checked["detail"]
    assert r.oauth_status("hermes")["connected"] is True
    assert not r.cfg["cloud"].get("oauth")
    invalidate_account(r, "hermes")
    assert account_snapshot(r, "hermes")["state"] == "unchecked"


@pytest.mark.asyncio
async def test_check_cannot_restore_status_after_disconnect(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def listing():
        started.set()
        await release.wait()
        return ["model"]
    monkeypatch.setattr(hermes_proxy, "available_models", listing)
    r = router()
    task = asyncio.create_task(check_account(r, "hermes"))
    await started.wait()
    invalidate_account(r, "hermes")
    release.set()
    assert (await task)["state"] == "unchecked"
    assert r.oauth_status("hermes")["connected"] is False


@pytest.mark.asyncio
async def test_failed_account_check_does_not_expose_provider_body(monkeypatch):
    monkeypatch.setattr(hermes_proxy, "available_models", AsyncMock(side_effect=RuntimeError("secret-token-in-response")))
    result = await check_account(router(), "hermes")
    assert result["state"] == "unavailable"
    assert "secret-token" not in str(result)


@pytest.mark.asyncio
async def test_native_refresh_failure_marks_account_unavailable():
    r = SimpleNamespace(oauth_status=lambda _: {"connected": True},
        provider_profile=lambda _: object(), ensure_oauth_fresh=AsyncMock(return_value=False))
    assert (await check_account(r, "xai"))["state"] == "unavailable"


def test_stale_checks_expire(monkeypatch):
    r = router()
    r._provider_account_checks = {"hermes": {"state": "ready", "checked_at": 1, "detail": "old"}}
    assert account_snapshot(r, "hermes")["state"] == "unchecked"
