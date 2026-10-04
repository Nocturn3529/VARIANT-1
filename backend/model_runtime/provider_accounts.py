"""Explicit account checks. Configuration presence is never connection evidence."""
from __future__ import annotations

import asyncio
import time


def account_snapshot(router, provider: str) -> dict:
    cached = getattr(router, "_provider_account_checks", {}).get(provider)
    if cached and time.time() - cached["checked_at"] < 300:
        return dict(cached)
    oauth = router.oauth_status(provider)
    if oauth.get("connected") or oauth.get("stored"):
        state = "refresh_needed" if oauth.get("needs_refresh") or not oauth.get("connected") else "saved"
    else:
        state = "unchecked"
    return {"state": state, "checked_at": 0, "detail": "Check the connection to verify current access."}


async def check_account(router, provider: str) -> dict:
    generation = getattr(router, "_provider_account_generations", {}).get(provider, 0)
    def save(state, detail):
        if getattr(router, "_provider_account_generations", {}).get(provider, 0) != generation:
            return account_snapshot(router, provider)
        return _save(router, provider, state, detail)
    try:
        async with asyncio.timeout(35):
            if provider == "hermes":
                from .hermes_proxy import available_models
                await available_models()
            elif provider == "ollama":
                from .ollama_cloud import available_cloud_models
                await available_cloud_models(start_if_needed=False)
            else:
                profile = router.provider_profile(provider)
                if profile is None:
                    raise ValueError("Unknown provider")
                if provider in {"openai-codex", "xai", "google-antigravity", "minimax-oauth", "minimax-oauth-cn"} and router.oauth_status(provider).get("connected"):
                    if not await router.ensure_oauth_fresh(provider):
                        return save("unavailable", "Account could not be refreshed. Reconnect to authorize again.")
                models = await router.list_cloud_models(provider, start_if_needed=False)
                if not models:
                    raise ValueError("No accessible models")
        detail = "Service and model listing checked. Individual model inference has not been tested."
        if provider == "ollama":
            detail = "Desktop helper and cloud tags checked. Cloud account access still needs an inference request."
            return save("service_ready", detail)
        return save("ready", detail)
    except Exception:
        return save("unavailable", "Connection check failed. Reconnect the account or check its desktop service.")


def invalidate_account(router, provider: str) -> None:
    getattr(router, "_provider_account_checks", {}).pop(provider, None)
    if not hasattr(router, "_provider_account_generations"):
        router._provider_account_generations = {}
    generations = router._provider_account_generations
    generations[provider] = generations.get(provider, 0) + 1


def _save(router, provider: str, state: str, detail: str) -> dict:
    checks = getattr(router, "_provider_account_checks", None)
    if checks is None:
        checks = router._provider_account_checks = {}
    checks[provider] = {"state": state, "detail": detail, "checked_at": time.time()}
    return dict(checks[provider])
