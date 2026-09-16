"""Google AI subscription OAuth and Cloud Code Assist account discovery.

Protocol reference: Oh My Pi's MIT-licensed google-antigravity provider.
This module returns credentials to LLMRouter; it never invokes another agent
or reads Antigravity's existing private credential files.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import inspect
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit, quote
import webbrowser

import httpx


PROVIDER = 'google-antigravity'
BASE_URL = 'https://daily-cloudcode-pa.googleapis.com'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
# Supply an authorized OAuth application configuration; no third-party identity is bundled.
CLIENT_ID = os.environ.get('VARIANT1_GOOGLE_OAUTH_CLIENT_ID', '').strip()
CLIENT_SECRET = os.environ.get('VARIANT1_GOOGLE_OAUTH_CLIENT_SECRET', '').strip()
SCOPES = ' '.join('https://www.googleapis.com/auth/' + name for name in (
    'cloud-platform', 'userinfo.email', 'userinfo.profile', 'cclog', 'experimentsandconfigs'))
CALLBACK_TIMEOUT = 600


class GoogleOAuthError(RuntimeError):
    pass


def _require_client_configuration():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise GoogleOAuthError(
            'Google AI subscription OAuth is not configured in this source build. '
            'Set VARIANT1_GOOGLE_OAUTH_CLIENT_ID and VARIANT1_GOOGLE_OAUTH_CLIENT_SECRET '
            'for an authorized client before starting the app, or use another inference connection.'
        )


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: int
    token_type: str = 'Bearer'
    scope: str = SCOPES
    project_id: str = ''
    account_id: str = ''
    tier: str = ''


def headers(access_token: str) -> dict:
    return {'Authorization': 'Bearer ' + access_token, 'Content-Type': 'application/json',
            'User-Agent': 'VARIANT-1/0.1 (Antigravity-compatible Google AI client)'}


def _tokens(value: dict, *, previous_refresh='') -> TokenSet:
    if not isinstance(value, dict) or not value.get('access_token'):
        raise GoogleOAuthError('Google did not return an access token.')
    try:
        expiry = int(value.get('expires_in', 0))
    except (TypeError, ValueError):
        expiry = 0
    if expiry <= 0:
        raise GoogleOAuthError('Google did not return a valid token lifetime.')
    return TokenSet(str(value['access_token']), str(value.get('refresh_token') or previous_refresh),
                    int(time.time()) + expiry, str(value.get('token_type') or 'Bearer'),
                    str(value.get('scope') or SCOPES))


async def _request(client, method, url, **kwargs):
    response = await client.request(method, url, **kwargs)
    if response.status_code != 200:
        detail = ''
        try:
            body = response.json()
            error = body.get('error')
            detail = str(error.get('message', '') if isinstance(error, dict)
                         else body.get('error_description') or error or '')[:350]
        except (ValueError, AttributeError):
            pass
        raise GoogleOAuthError(f'Google AI returned HTTP {response.status_code}' + (f': {detail}' if detail else '.'))
    value = response.json()
    if not isinstance(value, dict):
        raise GoogleOAuthError('Google AI returned an invalid response.')
    return value


async def account_project(access_token, *, client):
    auth = headers(access_token)
    async def load(project=''):
        body = {'metadata': {'ideType': 'ANTIGRAVITY'}}
        if project:
            body['cloudaicompanionProject'] = project
        result = await _request(client, 'POST', BASE_URL + '/v1internal:loadCodeAssist', headers=auth, json=body)
        if not project and result.get('cloudaicompanionProject') and not result.get('paidTier'):
            return await load(str(result['cloudaicompanionProject']))
        return result
    status = await load()
    if not status.get('currentTier') and not status.get('paidTier'):
        allowed = [row for row in status.get('allowedTiers', []) if isinstance(row, dict)]
        free = next((row for row in allowed if row.get('id') == 'free-tier'), None)
        if free is None:
            reasons = [str(row.get('reasonMessage', '')) for row in status.get('ineligibleTiers', []) if isinstance(row, dict)]
            raise GoogleOAuthError('Google AI account access is unavailable. ' + ' '.join(filter(None, reasons))[:350])
        operation = await _request(client, 'POST', BASE_URL + '/v1internal:onboardUser', headers=auth,
                                   json={'tierId': 'free-tier', 'metadata': {'ideType': 'ANTIGRAVITY'}})
        deadline = time.monotonic() + 60
        while operation.get('done') is not True:
            name = str(operation.get('name') or '')
            if not name or not name.startswith('operations/') or time.monotonic() >= deadline:
                raise GoogleOAuthError('Google account setup did not finish; retry sign-in after checking the account.')
            await asyncio.sleep(1)
            operation = await _request(client, 'GET', BASE_URL + '/v1internal/' + quote(name, safe='/'), headers=auth)
        if operation.get('error'):
            raise GoogleOAuthError('Google account setup was rejected.')
        status = await load()
    project = status.get('cloudaicompanionProject')
    if not isinstance(project, str) or not project:
        raise GoogleOAuthError('Google did not return the subscription account project.')
    tier = status.get('paidTier') or status.get('currentTier') or {}
    return project, str(tier.get('id') or '')


async def login(*, on_authorize_url=None, open_browser=True, client=None, callback_port=51121):
    """Bind before opening consent; state/PKCE belong to this one cancellable flow."""
    _require_client_configuration()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    future = asyncio.get_running_loop().create_future()
    handlers = set()

    async def receive(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        status, text = '400 Bad Request', 'This sign-in callback is invalid.'
        try:
            request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            line = request.split(b'\r\n', 1)[0].decode('ascii')
            method, target, _ = line.split(' ', 2)
            parsed = urlsplit(target)
            query = parse_qs(parsed.query)
            received_state = query.get('state', [''])[0]
            valid = (method == 'GET' and parsed.path == '/oauth-callback' and received_state.isascii()
                     and secrets.compare_digest(received_state, state))
            if valid and not future.done():
                if query.get('error'):
                    future.set_exception(GoogleOAuthError('Google sign-in was declined.'))
                elif len(query.get('code', [])) == 1:
                    future.set_result(query['code'][0])
                    status, text = '200 OK', 'Signed in. You can close this tab and return to VARIANT-1.'
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            body = text.encode()
            try:
                writer.write(f'HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode() + body)
                await writer.drain()
            except OSError:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            handlers.discard(task)

    try:
        server = await asyncio.start_server(receive, '127.0.0.1', callback_port, limit=16384)
    except OSError:
        if callback_port == 0:
            raise
        server = await asyncio.start_server(receive, '127.0.0.1', 0, limit=16384)
    redirect = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/oauth-callback'
    url = AUTH_URL + '?' + urlencode({'client_id': CLIENT_ID, 'redirect_uri': redirect, 'response_type': 'code',
        'scope': SCOPES, 'state': state, 'access_type': 'offline', 'prompt': 'consent select_account',
        'code_challenge': challenge, 'code_challenge_method': 'S256'})
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)
    try:
        if on_authorize_url:
            announced = on_authorize_url(url)
            if inspect.isawaitable(announced):
                await announced
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        code = await asyncio.wait_for(future, CALLBACK_TIMEOUT)
        raw = await _request(client, 'POST', TOKEN_URL, data={
            'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'code': code,
            'grant_type': 'authorization_code', 'redirect_uri': redirect, 'code_verifier': verifier})
        token = _tokens(raw)
        project, tier = await account_project(token.access_token, client=client)
        email = ''
        try:
            user = await _request(client, 'GET', 'https://www.googleapis.com/oauth2/v1/userinfo',
                                  headers=headers(token.access_token), params={'alt': 'json'})
            email = str(user.get('email') or '')
        except GoogleOAuthError:
            pass
        return TokenSet(token.access_token, token.refresh_token, token.expires_at, token.token_type,
                        token.scope, project, email, tier)
    finally:
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if not future.done():
            future.cancel()
        if own:
            await client.aclose()


async def refresh(refresh_token, *, client=None):
    _require_client_configuration()
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)
    try:
        raw = await _request(client, 'POST', TOKEN_URL, data={'grant_type': 'refresh_token',
            'refresh_token': refresh_token, 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET})
        return _tokens(raw, previous_refresh=refresh_token)
    finally:
        if own:
            await client.aclose()


async def list_models(access_token, *, client=None):
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)
    try:
        result = await _request(client, 'POST', BASE_URL + '/v1internal:fetchAvailableModels',
                                headers=headers(access_token), json={})
        if not isinstance(result.get('models'), dict):
            raise GoogleOAuthError('Google did not return a model catalog.')
        return sorted(str(name) for name, model in result['models'].items()
                      if str(name).startswith('gemini-') and isinstance(model, dict) and not model.get('isInternal'))
    finally:
        if own:
            await client.aclose()
