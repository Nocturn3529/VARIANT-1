"""MiniMax user-code/PKCE OAuth for the single VARIANT-1 provider router.

Protocol fields and fixed endpoints follow the installed Hermes Agent's
MiniMax OAuth implementation. Tokens are returned to LLMRouter for DPAPI
persistence; this module owns neither a second key store nor model transport.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import secrets
import time
import uuid
import webbrowser
from typing import Any

import httpx


CLIENT_ID = "78257093-7e40-4613-99e0-527b14b39113"
SCOPE = "group_id profile model.completion"
USER_CODE_GRANT = "urn:ietf:params:oauth:grant-type:user_code"
PORTALS = {
    "minimax-oauth": "https://api.minimax.io",
    "minimax-oauth-cn": "https://api.minimaxi.com",
}


class MiniMaxOAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UserCodeGrant:
    provider: str
    user_code: str
    verification_uri: str
    code_verifier: str
    expires_at: int
    interval_s: float

    @property
    def expires_in(self) -> int:
        return max(0, self.expires_at - int(time.time()))


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    refresh_token: str
    token_type: str
    scope: str
    expires_at: int


def portal_for(provider: str) -> str:
    try:
        return PORTALS[str(provider or "").strip().lower()]
    except KeyError as exc:
        raise MiniMaxOAuthError("unsupported MiniMax OAuth region") from exc


def _expiry(raw: Any, *, now: int | None = None) -> int:
    current = int(time.time() if now is None else now)
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MiniMaxOAuthError("MiniMax OAuth response has no valid expiry") from exc
    if value <= 0:
        raise MiniMaxOAuthError("MiniMax OAuth expiry must be positive")
    # MiniMax sometimes returns a Unix-millisecond absolute expiry and
    # sometimes a small TTL in seconds. Never treat the former as a huge TTL.
    return value // 1000 if value > current * 500 else current + value


def _pkce() -> tuple[str, str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge, secrets.token_urlsafe(16)


def _error(response: httpx.Response, action: str) -> MiniMaxOAuthError:
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str((body.get("base_resp") or {}).get("status_msg") or body.get("error") or "")
    except Exception:
        detail = response.text[:300]
    return MiniMaxOAuthError(
        f"MiniMax OAuth {action} failed ({response.status_code})"
        + (f": {detail[:300]}" if detail else "")
    )


async def _post(
    url: str, form: dict[str, str], *, client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    if client is not None:
        return await client.post(url, data=form, headers={"Accept": "application/json"})
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=8.0),
        trust_env=False, follow_redirects=False,
    ) as owned:
        return await owned.post(url, data=form, headers={"Accept": "application/json"})


async def request_user_code(
    provider: str, *, client: httpx.AsyncClient | None = None,
) -> UserCodeGrant:
    portal = portal_for(provider)
    verifier, challenge, state = _pkce()
    response = await _post(
        portal + "/oauth/code",
        {
            "response_type": "code", "client_id": CLIENT_ID, "scope": SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state,
        },
        client=client,
    )
    if response.status_code != 200:
        raise _error(response, "authorization")
    try:
        payload = response.json()
    except Exception as exc:
        raise MiniMaxOAuthError("MiniMax OAuth authorization returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("state") != state:
        raise MiniMaxOAuthError("MiniMax OAuth authorization state did not match")
    code = str(payload.get("user_code") or "").strip()
    uri = str(payload.get("verification_uri") or "").strip()
    if not code or not uri.startswith("https://"):
        raise MiniMaxOAuthError("MiniMax OAuth authorization omitted its code or HTTPS verification URL")
    try:
        interval = max(2.0, min(30.0, int(payload.get("interval") or 2000) / 1000.0))
    except (TypeError, ValueError):
        interval = 2.0
    return UserCodeGrant(
        provider=provider, user_code=code, verification_uri=uri,
        code_verifier=verifier, expires_at=_expiry(payload.get("expired_in")),
        interval_s=interval,
    )


def _tokens(payload: Any, *, existing_refresh: str = "") -> TokenSet:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise MiniMaxOAuthError("MiniMax OAuth token response was not successful")
    access = str(payload.get("access_token") or "").strip()
    refresh = str(payload.get("refresh_token") or existing_refresh).strip()
    if not access or not refresh:
        raise MiniMaxOAuthError("MiniMax OAuth token response omitted access or refresh token")
    return TokenSet(
        access_token=access, refresh_token=refresh,
        token_type=str(payload.get("token_type") or "Bearer"), scope=SCOPE,
        expires_at=_expiry(payload.get("expired_in")),
    )


async def poll_user_code(
    grant: UserCodeGrant, *, client: httpx.AsyncClient | None = None,
) -> TokenSet:
    portal = portal_for(grant.provider)
    while time.time() < grant.expires_at:
        response = await _post(
            portal + "/oauth/token",
            {
                "grant_type": USER_CODE_GRANT, "client_id": CLIENT_ID,
                "user_code": grant.user_code, "code_verifier": grant.code_verifier,
            }, client=client,
        )
        if response.status_code != 200:
            raise _error(response, "token exchange")
        try:
            payload = response.json()
        except Exception as exc:
            raise MiniMaxOAuthError("MiniMax OAuth token response was not JSON") from exc
        if isinstance(payload, dict) and payload.get("status") == "success":
            return _tokens(payload)
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise MiniMaxOAuthError("MiniMax OAuth authorization was denied")
        await asyncio.sleep(grant.interval_s)
    raise MiniMaxOAuthError("MiniMax OAuth user code expired before approval")


async def refresh(
    provider: str, refresh_token: str, *, client: httpx.AsyncClient | None = None,
) -> TokenSet:
    response = await _post(
        portal_for(provider) + "/oauth/token",
        {
            "grant_type": "refresh_token", "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }, client=client,
    )
    if response.status_code != 200:
        raise _error(response, "refresh")
    try:
        return _tokens(response.json(), existing_refresh=refresh_token)
    except (ValueError, TypeError) as exc:
        raise MiniMaxOAuthError("MiniMax OAuth refresh returned invalid JSON") from exc


def open_verification(grant: UserCodeGrant) -> bool:
    return bool(webbrowser.open(grant.verification_uri))


__all__ = [
    "CLIENT_ID", "MiniMaxOAuthError", "PORTALS", "TokenSet", "UserCodeGrant",
    "open_verification", "poll_user_code", "portal_for", "refresh", "request_user_code",
]
