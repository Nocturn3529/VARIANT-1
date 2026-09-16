"""OpenAI Codex subscription OAuth for VARIANT-1.

This provider is intentionally separate from the ordinary ``openai`` API-key
profile.  ChatGPT subscription inference uses the Codex Responses endpoint,
not ``api.openai.com/v1``.

Two credential owners are supported:

* ``device_code`` -- VARIANT-1 obtains and DPAPI-persists its own token pair.
* ``codex_cli`` -- Codex owns login, persistence, and refresh.  VARIANT-1 reads
  the current access token into memory only after the user explicitly links
  that login; it never copies the refresh token into VARIANT-1 configuration.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx


CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_BASE = "https://auth.openai.com"
CODEX_TOKEN_URL = f"{CODEX_AUTH_BASE}/oauth/token"
CODEX_DEVICE_CODE_URL = f"{CODEX_AUTH_BASE}/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = f"{CODEX_AUTH_BASE}/api/accounts/deviceauth/token"
CODEX_DEVICE_VERIFY_URL = f"{CODEX_AUTH_BASE}/codex/device"
CODEX_DEVICE_REDIRECT_URI = f"{CODEX_AUTH_BASE}/deviceauth/callback"
CODEX_RESPONSES_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_MODELS_URL = f"{CODEX_RESPONSES_BASE}/models"
CODEX_REFRESH_SKEW_SECONDS = 120
DEFAULT_TIMEOUT = 30.0


class OpenAICodexOAuthError(RuntimeError):
    """A bounded, user-safe Codex OAuth or credential error."""


@dataclass(frozen=True)
class DeviceCodeGrant:
    device_auth_id: str
    user_code: str
    verification_uri: str = CODEX_DEVICE_VERIFY_URL
    interval: int = 5
    expires_in: int = 900
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: int = 0
    account_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def validate_codex_base_url(url: str, *, label: str = "Codex subscription URL") -> str:
    """Pin subscription bearers to the first-party ChatGPT Codex endpoint."""

    value = str(url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower().rstrip(".") != "chatgpt.com"
        or parsed.path.rstrip("/") != "/backend-api/codex"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise OpenAICodexOAuthError(
            f"{label} must be exactly {CODEX_RESPONSES_BASE!r}"
        )
    return value


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def token_expiry(access_token: str) -> int:
    value = _decode_jwt_claims(access_token).get("exp")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def token_account_id(access_token: str) -> str:
    claims = _decode_jwt_claims(access_token)
    auth = claims.get("https://api.openai.com/auth")
    value = auth.get("chatgpt_account_id") if isinstance(auth, dict) else ""
    return str(value or "").strip()


def parse_token_response(payload: dict[str, Any], *, now: int | None = None) -> TokenSet:
    if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
        raise OpenAICodexOAuthError("OpenAI token response did not include an access token")
    access = str(payload["access_token"]).strip()
    expiry = token_expiry(access)
    if not expiry:
        try:
            ttl = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError, OverflowError):
            ttl = 0
        if ttl > 0:
            expiry = int(now if now is not None else time.time()) + ttl
    return TokenSet(
        access_token=access,
        refresh_token=str(payload.get("refresh_token") or "").strip(),
        token_type=str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        scope=str(payload.get("scope") or "").strip(),
        expires_at=expiry,
        account_id=(
            str(payload.get("account_id") or "").strip()
            or token_account_id(access)
        ),
        raw=dict(payload),
    )


def codex_home() -> Path:
    configured = str(os.environ.get("CODEX_HOME") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def codex_auth_path() -> Path:
    return codex_home() / "auth.json"


def _read_codex_auth_document() -> dict[str, Any]:
    path = codex_auth_path()
    if not path.is_file():
        raise OpenAICodexOAuthError(
            "Codex login was not found. Sign in with Codex, or use VARIANT-1's "
            "separate ChatGPT device-code login."
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise OpenAICodexOAuthError("Codex auth state could not be read") from exc
    if not isinstance(value, dict):
        raise OpenAICodexOAuthError("Codex auth state is not a JSON object")
    return value


def load_codex_cli_tokens(*, allow_expired: bool = False) -> TokenSet:
    """Read Codex-managed tokens without persisting them anywhere in VARIANT-1."""

    payload = _read_codex_auth_document()
    if str(payload.get("auth_mode") or "").strip().lower() != "chatgpt":
        raise OpenAICodexOAuthError("Codex is not signed in with ChatGPT")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise OpenAICodexOAuthError("Codex ChatGPT auth state has no token record")
    token_set = parse_token_response(tokens)
    if not allow_expired and token_set.expires_at and token_set.expires_at <= int(time.time()):
        raise OpenAICodexOAuthError("Codex ChatGPT access token is expired")
    if not token_set.account_id:
        account_id = str(tokens.get("account_id") or "").strip()
        token_set = TokenSet(
            access_token=token_set.access_token,
            refresh_token=token_set.refresh_token,
            token_type=token_set.token_type,
            scope=token_set.scope,
            expires_at=token_set.expires_at,
            account_id=account_id,
            raw=token_set.raw,
        )
    return token_set


def codex_cli_status() -> dict[str, Any]:
    try:
        tokens = load_codex_cli_tokens(allow_expired=True)
        now = int(time.time())
        expired = bool(tokens.expires_at and tokens.expires_at <= now)
        return {
            # A linked managed login remains connected while an expired access
            # token has a refresh token; ensure_oauth_fresh asks app-server to
            # rotate it before inference.
            "connected": bool(tokens.access_token) and (
                not expired or bool(tokens.refresh_token)
            ),
            "usable": bool(tokens.access_token) and not expired,
            "needs_refresh": expired,
            "auth_flow": "codex_cli",
            "managed_external": True,
            "expires_at": tokens.expires_at,
            "expires_in": max(0, tokens.expires_at - now) if tokens.expires_at else 0,
            "account_id_present": bool(tokens.account_id),
            "refresh_managed_by": "codex",
            "error": "expired" if expired else "",
        }
    except Exception as exc:
        return {
            "connected": False,
            "usable": False,
            "needs_refresh": False,
            "auth_flow": "codex_cli",
            "managed_external": True,
            "expires_at": 0,
            "expires_in": 0,
            "account_id_present": False,
            "refresh_managed_by": "codex",
            "error": str(exc),
        }


def resolve_account_id(access_token: str, account_id: str = "") -> str:
    supplied = str(account_id or "").strip()
    if supplied:
        return supplied
    account_id = token_account_id(access_token)
    if account_id:
        return account_id
    try:
        payload = _read_codex_auth_document()
        tokens = payload.get("tokens") or {}
        stored = str(tokens.get("access_token") or "")
        if stored and secrets.compare_digest(stored, str(access_token or "")):
            return str(tokens.get("account_id") or "").strip()
    except Exception:
        pass
    return ""


def request_headers(
    access_token: str, *, accept: str = "text/event-stream", account_id: str = "",
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": accept,
        # The endpoint accepts a product-specific originator; keep VARIANT-1
        # identifiable instead of pretending to be the Codex CLI.
        "Originator": "VARIANT-1",
        "User-Agent": "VARIANT-1/0.1.0",
    }
    resolved_account_id = resolve_account_id(access_token, account_id)
    if resolved_account_id:
        headers["ChatGPT-Account-ID"] = resolved_account_id
    return headers


def _safe_oauth_error(response: httpx.Response, action: str) -> OpenAICodexOAuthError:
    detail = ""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "")
        elif error:
            detail = str(error)
        elif isinstance(payload, dict):
            detail = str(payload.get("error_description") or "")
    except Exception:
        detail = ""
    suffix = f": {detail[:240]}" if detail else ""
    return OpenAICodexOAuthError(
        f"OpenAI {action} failed (HTTP {response.status_code}){suffix}"
    )


async def request_device_code(*, client: httpx.AsyncClient | None = None) -> DeviceCodeGrant:
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        response = await client.post(
            CODEX_DEVICE_CODE_URL,
            json={"client_id": CODEX_OAUTH_CLIENT_ID},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200:
        raise _safe_oauth_error(response, "device-code request")
    try:
        payload = response.json()
    except Exception as exc:
        raise OpenAICodexOAuthError("OpenAI device-code response was not JSON") from exc
    device_auth_id = str(payload.get("device_auth_id") or "").strip()
    user_code = str(payload.get("user_code") or "").strip()
    if not device_auth_id or not user_code:
        raise OpenAICodexOAuthError("OpenAI device-code response was incomplete")
    try:
        interval = max(3, int(payload.get("interval") or 5))
    except (TypeError, ValueError):
        interval = 5
    try:
        expires_in = max(60, int(payload.get("expires_in") or 900))
    except (TypeError, ValueError):
        expires_in = 900
    return DeviceCodeGrant(
        device_auth_id=device_auth_id,
        user_code=user_code,
        interval=interval,
        expires_in=expires_in,
        raw=dict(payload),
    )


async def exchange_device_code(
    authorization_code: str,
    code_verifier: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> TokenSet:
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        response = await client.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": str(authorization_code or ""),
                "redirect_uri": CODEX_DEVICE_REDIRECT_URI,
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "code_verifier": str(code_verifier or ""),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200:
        raise _safe_oauth_error(response, "device token exchange")
    try:
        return parse_token_response(response.json())
    except OpenAICodexOAuthError:
        raise
    except Exception as exc:
        raise OpenAICodexOAuthError("OpenAI token exchange returned invalid JSON") from exc


async def poll_device_code(
    grant: DeviceCodeGrant,
    *,
    client: httpx.AsyncClient | None = None,
    sleep=None,
    on_tick=None,
) -> TokenSet:
    sleep = sleep or asyncio.sleep
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    deadline = time.monotonic() + max(60, int(grant.expires_in or 900))
    try:
        while time.monotonic() < deadline:
            if on_tick:
                try:
                    on_tick(max(0, int(deadline - time.monotonic())))
                except Exception:
                    pass
            await sleep(max(3, int(grant.interval or 5)))
            response = await client.post(
                CODEX_DEVICE_TOKEN_URL,
                json={
                    "device_auth_id": grant.device_auth_id,
                    "user_code": grant.user_code,
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if response.status_code in {403, 404}:
                continue
            if response.status_code != 200:
                raise _safe_oauth_error(response, "device authorization poll")
            try:
                payload = response.json()
            except Exception as exc:
                raise OpenAICodexOAuthError("OpenAI device authorization returned invalid JSON") from exc
            code = str(payload.get("authorization_code") or "").strip()
            verifier = str(payload.get("code_verifier") or "").strip()
            if not code or not verifier:
                raise OpenAICodexOAuthError("OpenAI device authorization response was incomplete")
            return await exchange_device_code(code, verifier, client=client)
    finally:
        if own:
            await client.aclose()
    raise OpenAICodexOAuthError("Timed out waiting for ChatGPT device authorization")


async def refresh(refresh_token: str, *, client: httpx.AsyncClient | None = None) -> TokenSet:
    if not str(refresh_token or "").strip():
        raise OpenAICodexOAuthError("No OpenAI refresh token is available")
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        response = await client.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200:
        raise _safe_oauth_error(response, "token refresh")
    token_set = parse_token_response(response.json())
    if not token_set.refresh_token:
        token_set = TokenSet(
            access_token=token_set.access_token,
            refresh_token=str(refresh_token),
            token_type=token_set.token_type,
            scope=token_set.scope,
            expires_at=token_set.expires_at,
            account_id=token_set.account_id,
            raw=token_set.raw,
        )
    return token_set


def open_verification(grant: DeviceCodeGrant) -> bool:
    try:
        return bool(webbrowser.open(grant.verification_uri))
    except Exception:
        return False


def _codex_executable() -> str:
    configured = str(os.environ.get("VARIANT1_CODEX_CLI") or "").strip()
    candidates = [configured, shutil.which("codex") or ""]
    local = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        candidates.append(str(Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise OpenAICodexOAuthError("Codex CLI was not found on this machine")


async def _read_rpc_response(proc, request_id: int, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise OpenAICodexOAuthError("Codex app-server auth request timed out") from exc
        if not raw:
            break
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            if message.get("error"):
                raise OpenAICodexOAuthError("Codex app-server rejected the auth request")
            return message
    raise OpenAICodexOAuthError("Codex app-server closed before completing auth refresh")


async def refresh_codex_cli_managed(*, timeout: float = 45.0) -> TokenSet:
    """Ask the official app-server to refresh its token, then re-read it."""

    executable = _codex_executable()
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        executable, "app-server", "--stdio", **kwargs
    )

    async def send(message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise OpenAICodexOAuthError("Codex app-server stdin is unavailable")
        proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    try:
        await send({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "variant1",
                    "title": "VARIANT-1",
                    "version": "0.1.0",
                }
            },
        })
        await _read_rpc_response(proc, 1, timeout=timeout)
        await send({"method": "initialized", "params": {}})
        await send({
            "method": "account/read",
            "id": 2,
            "params": {"refreshToken": True},
        })
        response = await _read_rpc_response(proc, 2, timeout=timeout)
        account = ((response.get("result") or {}).get("account") or {})
        if str(account.get("type") or "").lower() != "chatgpt":
            raise OpenAICodexOAuthError("Codex is not signed in with ChatGPT")
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
    return load_codex_cli_tokens()


async def ensure_codex_cli_fresh(*, skew: int = CODEX_REFRESH_SKEW_SECONDS) -> TokenSet:
    tokens = load_codex_cli_tokens(allow_expired=True)
    if tokens.expires_at and tokens.expires_at <= int(time.time()) + max(0, int(skew)):
        return await refresh_codex_cli_managed()
    return tokens


async def list_models(
    access_token: str,
    *,
    account_id: str = "",
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=15.0)
    try:
        response = await client.get(
            CODEX_MODELS_URL,
            params={"client_version": "1.0.0"},
            headers=request_headers(
                access_token, accept="application/json", account_id=account_id
            ),
        )
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200:
        raise _safe_oauth_error(response, "model listing")
    try:
        payload = response.json()
    except Exception as exc:
        raise OpenAICodexOAuthError("OpenAI Codex model listing returned invalid JSON") from exc
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("data") if isinstance(payload, dict) else None
    out: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        visibility = str(row.get("visibility") or "").strip().lower()
        if visibility in {"hide", "hidden"}:
            continue
        model = str(row.get("slug") or row.get("id") or row.get("model") or "").strip()
        if model and model not in out:
            out.append(model)
    return out


__all__ = [
    "CODEX_OAUTH_CLIENT_ID",
    "CODEX_RESPONSES_BASE",
    "DeviceCodeGrant",
    "OpenAICodexOAuthError",
    "TokenSet",
    "codex_cli_status",
    "ensure_codex_cli_fresh",
    "list_models",
    "load_codex_cli_tokens",
    "open_verification",
    "parse_token_response",
    "poll_device_code",
    "refresh",
    "request_device_code",
    "request_headers",
    "resolve_account_id",
    "token_account_id",
    "token_expiry",
    "validate_codex_base_url",
]
