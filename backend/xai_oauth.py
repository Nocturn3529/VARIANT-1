"""
VARIANT-1 xAI Grok OAuth — first-class subscription login.

Primary path (Hermes-aligned): OAuth 2.0 **authorization code + PKCE** with a
loopback callback on ``127.0.0.1:56121/callback``. Fallback: RFC 8628 **device
grant** through the legacy Grok CLI-compatible route.

Reuses xAI's public Grok CLI client id (same as Hermes). Consent UI may brand
as Grok Build / Grok CLI — not VARIANT-1. Not for open distribution; personal
account tooling only.

    PKCE:  authorize → loopback code → token exchange (+ code_verifier)
    Device: device_code → user_code → poll token
    Refresh: refresh_token → access_token

Env overrides: VARIANT1_XAI_CLIENT_ID, VARIANT1_XAI_AUTH_BASE, VARIANT1_XAI_DEVICE_PATH,
VARIANT1_XAI_TOKEN_PATH, VARIANT1_XAI_OAUTH_SCOPE.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit

import httpx

# The device grant's token endpoint grant_type (RFC 8628 §3.4).
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_TIMEOUT = 30.0
# When a provider omits `interval`, RFC 8628 says poll no faster than every 5s.
DEFAULT_POLL_INTERVAL = 5

# xAI's Grok CLI public OAuth client id (Hermes uses the same value).
XAI_GROK_CLI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
XAI_DEFAULT_AUTH_BASE = "https://auth.x.ai"
XAI_DEFAULT_DEVICE_PATH = "/oauth2/device/code"
XAI_DEFAULT_TOKEN_PATH = "/oauth2/token"
XAI_DEFAULT_SCOPE = "openid profile email offline_access grok-cli:access api:access"
# Hermes-aligned loopback (see hermes_cli/auth.py XAI_OAUTH_REDIRECT_*).
XAI_PKCE_REDIRECT_HOST = "127.0.0.1"
XAI_PKCE_REDIRECT_PORT = 56121
XAI_PKCE_REDIRECT_PATH = "/callback"
XAI_PKCE_CALLBACK_TIMEOUT = 300.0


class XaiOAuthError(RuntimeError):
    """Raised when the device/token exchange fails or is declined."""


class XaiOAuthPending(RuntimeError):
    """Internal: the user hasn't approved yet (authorization_pending/slow_down)."""


def validate_xai_https_url(url: str, *, label: str = "xAI OAuth URL") -> str:
    """Keep subscription bearers and refresh tokens on xAI-controlled HTTPS hosts."""
    value = str(url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host or not (
            host == "x.ai" or host.endswith(".x.ai")):
        raise XaiOAuthError(
            f"{label} must use HTTPS on x.ai or an x.ai subdomain; refusing {value!r}")
    if parsed.username or parsed.password:
        raise XaiOAuthError(f"{label} must not contain embedded credentials")
    return value


@dataclass
class XaiOAuthConfig:
    """Endpoints + client identity for the device flow. Build via `from_env`."""

    client_id: str = XAI_GROK_CLI_CLIENT_ID
    auth_base: str = XAI_DEFAULT_AUTH_BASE
    device_path: str = XAI_DEFAULT_DEVICE_PATH
    token_path: str = XAI_DEFAULT_TOKEN_PATH
    scope: str = XAI_DEFAULT_SCOPE

    @classmethod
    def from_env(cls, client_id: str = "") -> "XaiOAuthConfig":
        return cls(
            client_id=(client_id or os.environ.get("VARIANT1_XAI_CLIENT_ID", "")).strip()
            or XAI_GROK_CLI_CLIENT_ID,
            auth_base=validate_xai_https_url(
                os.environ.get("VARIANT1_XAI_AUTH_BASE", XAI_DEFAULT_AUTH_BASE),
                label="xAI authorization base"),
            device_path=os.environ.get("VARIANT1_XAI_DEVICE_PATH", XAI_DEFAULT_DEVICE_PATH),
            token_path=os.environ.get("VARIANT1_XAI_TOKEN_PATH", XAI_DEFAULT_TOKEN_PATH),
            scope=os.environ.get("VARIANT1_XAI_OAUTH_SCOPE", XAI_DEFAULT_SCOPE),
        )

    @property
    def device_url(self) -> str:
        return self.auth_base.rstrip("/") + "/" + self.device_path.lstrip("/")

    @property
    def token_url(self) -> str:
        return self.auth_base.rstrip("/") + "/" + self.token_path.lstrip("/")

    def require_client_id(self) -> None:
        if not self.client_id:
            raise XaiOAuthError(
                "No xAI OAuth client id configured. Set VARIANT1_XAI_CLIENT_ID to a "
                "client id that xAI has authorized for your use before starting the "
                "Grok subscription login.")


@dataclass
class DeviceCodeGrant:
    """The device-authorization response the user acts on."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int = DEFAULT_POLL_INTERVAL
    expires_in: int = 900
    raw: dict = field(default_factory=dict)


@dataclass
class TokenSet:
    """A resolved token set. `expires_at` is an absolute unix time."""

    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: int = 0
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Pure parsers (no network — unit-testable)
# --------------------------------------------------------------------------
def parse_device_response(data: dict) -> DeviceCodeGrant:
    """Normalize a device-authorization response (field names per RFC 8628)."""
    if not isinstance(data, dict) or not data.get("device_code"):
        raise XaiOAuthError(f"malformed device-code response: {str(data)[:200]}")
    verification = data.get("verification_uri") or data.get("verification_url") or ""
    complete = (data.get("verification_uri_complete")
                or data.get("verification_url_complete") or verification)
    try:
        interval = int(data.get("interval") or DEFAULT_POLL_INTERVAL)
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL
    try:
        expires_in = int(data.get("expires_in") or 900)
    except (TypeError, ValueError):
        expires_in = 900
    return DeviceCodeGrant(
        device_code=str(data["device_code"]),
        user_code=str(data.get("user_code") or ""),
        verification_uri=str(verification),
        verification_uri_complete=str(complete),
        interval=max(1, interval),
        expires_in=max(1, expires_in),
        raw=data,
    )


def parse_token_response(data: dict, *, now: int = None) -> TokenSet:
    """Turn a token-endpoint success body into a TokenSet with absolute expiry."""
    if not isinstance(data, dict) or not data.get("access_token"):
        raise XaiOAuthError(f"token response had no access_token: {str(data)[:200]}")
    now = int(now if now is not None else time.time())
    try:
        ttl = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        ttl = 3600
    return TokenSet(
        access_token=str(data["access_token"]),
        refresh_token=str(data.get("refresh_token") or ""),
        token_type=str(data.get("token_type") or "Bearer"),
        scope=str(data.get("scope") or ""),
        expires_at=now + max(0, ttl),
        raw=data,
    )


def _oauth_error(status: int, body: dict) -> str:
    """OAuth error bodies carry a machine `error` + human `error_description`."""
    if isinstance(body, dict):
        code = body.get("error") or ""
        desc = body.get("error_description") or ""
        if code or desc:
            return f"{code}: {desc}".strip(": ")
    return f"HTTP {status}"


# --------------------------------------------------------------------------
# Network steps (each accepts an injectable client for testing)
# --------------------------------------------------------------------------
async def _post_form(client: httpx.AsyncClient, url: str, form: dict) -> tuple[int, dict]:
    resp = await client.post(url, data=form,
                             headers={"Accept": "application/json"})
    try:
        body = resp.json()
    except Exception:
        body = {"error": "non_json_response", "error_description": (resp.text or "")[:200]}
    return resp.status_code, body


async def request_device_code(cfg: XaiOAuthConfig, *,
                              client: httpx.AsyncClient = None) -> DeviceCodeGrant:
    """Kick off the flow: ask xAI for a device + user code."""
    cfg.require_client_id()
    form = {"client_id": cfg.client_id}
    if cfg.scope:
        form["scope"] = cfg.scope
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        status, body = await _post_form(client, cfg.device_url, form)
    finally:
        if own:
            await client.aclose()
    if status >= 400:
        raise XaiOAuthError(f"device authorization failed ({_oauth_error(status, body)})")
    return parse_device_response(body)


async def _exchange_once(client: httpx.AsyncClient, cfg: XaiOAuthConfig,
                         device_code: str) -> TokenSet:
    """One token-endpoint poll. Raises XaiOAuthPending while the user is deciding."""
    status, body = await _post_form(client, cfg.token_url, {
        "grant_type": DEVICE_GRANT_TYPE,
        "device_code": device_code,
        "client_id": cfg.client_id,
    })
    if status < 400 and body.get("access_token"):
        return parse_token_response(body)
    err = (body.get("error") if isinstance(body, dict) else "") or ""
    if err in ("authorization_pending", "slow_down"):
        raise XaiOAuthPending(err)
    if err == "expired_token":
        raise XaiOAuthError("the login request expired before it was approved — start again")
    if err == "access_denied":
        raise XaiOAuthError("access was denied on the xAI consent screen")
    raise XaiOAuthError(f"token exchange failed ({_oauth_error(status, body)})")


async def poll_token(cfg: XaiOAuthConfig, grant: DeviceCodeGrant, *,
                     client: httpx.AsyncClient = None, sleep=None,
                     on_tick=None) -> TokenSet:
    """Poll the token endpoint until the user approves, then return the tokens.

    `sleep` is injectable (defaults to asyncio.sleep) so tests don't wait in real
    time. `on_tick`, if given, is called once per poll with seconds-remaining —
    handy for a CLI/GUI "waiting for approval…" line. Honors `slow_down` by
    backing the interval off, per RFC 8628 §3.5.
    """
    import asyncio
    sleep = sleep or asyncio.sleep
    cfg.require_client_id()
    interval = max(1, int(grant.interval or DEFAULT_POLL_INTERVAL))
    deadline = time.time() + max(1, int(grant.expires_in or 900))
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        while True:
            if time.time() >= deadline:
                raise XaiOAuthError("timed out waiting for the xAI login to be approved")
            if on_tick:
                try:
                    on_tick(max(0, int(deadline - time.time())))
                except Exception:
                    pass
            try:
                return await _exchange_once(client, cfg, grant.device_code)
            except XaiOAuthPending as pending:
                if str(pending) == "slow_down":
                    interval += 5
            await sleep(interval)
    finally:
        if own:
            await client.aclose()


async def refresh(cfg: XaiOAuthConfig, refresh_token: str, *,
                  client: httpx.AsyncClient = None) -> TokenSet:
    """Mint a fresh access token from a stored refresh token."""
    cfg.require_client_id()
    if not refresh_token:
        raise XaiOAuthError("no refresh token to refresh from")
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        status, body = await _post_form(client, cfg.token_url, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cfg.client_id,
        })
    finally:
        if own:
            await client.aclose()
    if status >= 400 or not body.get("access_token"):
        raise XaiOAuthError(f"token refresh failed ({_oauth_error(status, body)})")
    tok = parse_token_response(body)
    if not tok.refresh_token:
        # Providers that don't rotate the refresh token expect reuse of the old one.
        tok.refresh_token = refresh_token
    return tok


def open_verification(grant: DeviceCodeGrant) -> bool:
    """Best-effort: open the approval URL in the user's browser. Never raises."""
    url = grant.verification_uri_complete or grant.verification_uri
    if not url:
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


# --------------------------------------------------------------------------
# PKCE loopback (Hermes-aligned primary login)
# --------------------------------------------------------------------------
def pkce_code_verifier(length: int = 64) -> str:
    """RFC 7636 code_verifier (urlsafe, no padding)."""
    n = max(43, min(128, int(length or 64)))
    return secrets.token_urlsafe(n)[:n]


def pkce_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256((verifier or "").encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_pkce_authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
    scope: str = XAI_DEFAULT_SCOPE,
    referrer: str = "variant1-dev",
) -> str:
    """Build the browser authorize URL (includes plan=generic like Hermes)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        # Hermes: opts consent into xAI generic OAuth plan tier for loopback.
        "plan": "generic",
        "referrer": referrer,
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


async def discover_oauth_endpoints(
    *,
    discovery_url: str = XAI_DISCOVERY_URL,
    client: httpx.AsyncClient = None,
) -> dict[str, str]:
    """Fetch authorization_endpoint + token_endpoint from OIDC discovery."""
    url = validate_xai_https_url(discovery_url, label="xAI OIDC discovery")
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        resp = await client.get(url, headers={"Accept": "application/json"})
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise XaiOAuthError(f"OIDC discovery failed (HTTP {resp.status_code})")
    try:
        payload = resp.json()
    except Exception as e:
        raise XaiOAuthError(f"OIDC discovery returned invalid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise XaiOAuthError("OIDC discovery response was not an object")
    auth_ep = str(payload.get("authorization_endpoint") or "").strip()
    tok_ep = str(payload.get("token_endpoint") or "").strip()
    if not auth_ep or not tok_ep:
        raise XaiOAuthError("OIDC discovery missing authorization/token endpoints")
    return {
        "authorization_endpoint": validate_xai_https_url(
            auth_ep, label="authorization_endpoint"),
        "token_endpoint": validate_xai_https_url(tok_ep, label="token_endpoint"),
    }


def _make_pkce_callback_handler(expected_path: str, expected_state: str):
    expected_state = str(expected_state or "")
    if not expected_state:
        raise ValueError("expected OAuth state must be non-empty")
    result: dict = {
        "code": None, "state": None, "error": None, "error_description": None,
        "complete": False, "cancelled": False,
    }
    lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            return

        def _cors(self):
            origin = self.headers.get("Origin") or ""
            if origin in ("https://accounts.x.ai", "https://auth.x.ai"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return
            qs = parse_qs(parsed.query or "", keep_blank_values=True)
            states = qs.get("state") or []
            codes = qs.get("code") or []
            errors = qs.get("error") or []
            state = states[0] if len(states) == 1 else None
            code = (
                codes[0]
                if len(codes) == 1 and isinstance(codes[0], str) and codes[0].strip()
                else None
            )
            error = (
                errors[0]
                if len(errors) == 1 and isinstance(errors[0], str) and errors[0].strip()
                else None
            )
            valid_state = bool(
                isinstance(state, str)
                and secrets.compare_digest(state, expected_state)
            )
            # A callback completes the flow only when it carries the exact
            # per-login state and exactly one terminal OAuth outcome. Stray
            # loopback requests must not poison the real browser callback.
            well_formed = valid_state and bool(code) != bool(error)
            if not well_formed:
                body = b"Invalid OAuth callback. Return to the login tab and try again."
                self.send_response(400)
                self._cors()
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            with lock:
                first = not result["complete"]
                if first:
                    result["code"] = code
                    result["state"] = state
                    result["error"] = error
                    result["error_description"] = (
                        (qs.get("error_description") or [None])[0]
                    )
                    result["complete"] = True
            body = (
                b"<html><body style='font-family:system-ui;padding:2rem'>"
                b"<h2>VARIANT-1</h2><p>Login "
                + (b"complete" if first else b"was already captured")
                + b". You can close this tab "
                b"and return to the terminal / app.</p></body></html>"
            )
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler, result


def _start_pkce_callback_server(
    *,
    expected_state: str,
) -> tuple:
    host = XAI_PKCE_REDIRECT_HOST
    path = XAI_PKCE_REDIRECT_PATH
    handler_cls, result = _make_pkce_callback_handler(path, expected_state)

    class _Server(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True

    try:
        server = _Server((host, XAI_PKCE_REDIRECT_PORT), handler_cls)
    except OSError as exc:
        raise XaiOAuthError(
            f"xAI sign-in needs {host}:{XAI_PKCE_REDIRECT_PORT}, but that "
            "registered callback port is already in use"
        ) from exc
    redirect_uri = f"http://{host}:{XAI_PKCE_REDIRECT_PORT}{path}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    return server, thread, result, redirect_uri


def _wait_pkce_callback(server, thread, result: dict, *, timeout: float) -> dict:
    import time as _t
    deadline = _t.monotonic() + max(30.0, float(timeout or XAI_PKCE_CALLBACK_TIMEOUT))
    try:
        while _t.monotonic() < deadline:
            if result.get("cancelled"):
                raise XaiOAuthError("xAI sign-in was cancelled")
            if result.get("complete"):
                return result
            _t.sleep(0.1)
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        thread.join(timeout=1.0)
    raise XaiOAuthError(
        "timed out waiting for the browser login callback "
        f"(expected {XAI_PKCE_REDIRECT_HOST}:{XAI_PKCE_REDIRECT_PORT})")


async def exchange_pkce_code(
    cfg: XaiOAuthConfig,
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    code_challenge: str = "",
    client: httpx.AsyncClient = None,
) -> TokenSet:
    """Exchange authorization code + PKCE verifier for tokens."""
    cfg.require_client_id()
    if not code_verifier:
        raise XaiOAuthError("PKCE code_verifier is empty")
    if not code:
        raise XaiOAuthError("authorization code is empty")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": cfg.client_id,
        "code_verifier": code_verifier,
    }
    # Hermes defense-in-depth: some xAI token endpoints re-check challenge.
    if code_challenge:
        form["code_challenge"] = code_challenge
        form["code_challenge_method"] = "S256"
    own = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=DEFAULT_TIMEOUT)
    try:
        status, body = await _post_form(client, token_endpoint, form)
    finally:
        if own:
            await client.aclose()
    if status >= 400 or not (isinstance(body, dict) and body.get("access_token")):
        raise XaiOAuthError(f"PKCE token exchange failed ({_oauth_error(status, body)})")
    return parse_token_response(body)


async def login_pkce(
    cfg: XaiOAuthConfig = None,
    *,
    open_browser: bool = True,
    timeout: float = XAI_PKCE_CALLBACK_TIMEOUT,
    client: httpx.AsyncClient = None,
    on_authorize_url=None,
) -> TokenSet:
    """Run Hermes-style PKCE login; returns TokenSet (caller persists it).

    Opens the system browser to xAI consent, waits on loopback callback.
    """
    cfg = cfg or XaiOAuthConfig.from_env()
    cfg.require_client_id()
    discovery = await discover_oauth_endpoints(client=client)
    auth_ep = discovery["authorization_endpoint"]
    tok_ep = discovery["token_endpoint"]

    verifier = pkce_code_verifier()
    challenge = pkce_code_challenge(verifier)
    state = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    server, thread, result, redirect_uri = _start_pkce_callback_server(
        expected_state=state,
    )
    try:
        authorize_url = build_pkce_authorize_url(
            auth_ep,
            client_id=cfg.client_id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=state,
            nonce=nonce,
            scope=cfg.scope,
        )
        if on_authorize_url:
            try:
                on_authorize_url(authorize_url, redirect_uri)
            except Exception:
                pass
        if open_browser:
            try:
                webbrowser.open(authorize_url)
            except Exception:
                pass

        # Blocking wait in a worker thread so the asyncio loop stays free.
        import asyncio
        loop = asyncio.get_event_loop()
        cb = await loop.run_in_executor(
            None,
            lambda: _wait_pkce_callback(server, thread, result, timeout=timeout),
        )
    except BaseException:
        result["cancelled"] = True
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        raise

    callback_state = cb.get("state")
    if not isinstance(callback_state, str) or not secrets.compare_digest(
            callback_state, state):
        raise XaiOAuthError("OAuth state mismatch (possible CSRF) — try again")
    if cb.get("error"):
        desc = cb.get("error_description") or cb.get("error")
        raise XaiOAuthError(f"access denied or error on consent: {desc}")
    code = cb.get("code") or ""
    return await exchange_pkce_code(
        cfg,
        token_endpoint=tok_ep,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        code_challenge=challenge,
        client=client,
    )
