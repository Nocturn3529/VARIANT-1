from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

import google_ai_oauth as oauth


@pytest.fixture(autouse=True)
def configured_test_oauth_client(monkeypatch):
    monkeypatch.setattr(oauth, 'CLIENT_ID', 'synthetic-test-client')
    monkeypatch.setattr(oauth, 'CLIENT_SECRET', 'synthetic-test-secret')


@pytest.mark.asyncio
async def test_unconfigured_client_fails_before_browser_or_network(monkeypatch):
    monkeypatch.setattr(oauth, 'CLIENT_ID', '')
    monkeypatch.setattr(oauth, 'CLIENT_SECRET', '')
    def unexpected(*args, **kwargs):
        pytest.fail('Unconfigured OAuth must not start browser, listener, or HTTP client')
    monkeypatch.setattr(asyncio, 'start_server', unexpected)
    monkeypatch.setattr(oauth.webbrowser, 'open', unexpected)
    monkeypatch.setattr(httpx, 'AsyncClient', unexpected)
    with pytest.raises(oauth.GoogleOAuthError, match='not configured'):
        await oauth.login()
    with pytest.raises(oauth.GoogleOAuthError, match='not configured'):
        await oauth.refresh('synthetic-refresh')


@pytest.mark.asyncio
async def test_login_owns_callback_state_pkce_and_subscription_project():
    authorization = {}
    requests = []
    def respond(request):
        requests.append(request)
        if request.url == oauth.TOKEN_URL:
            form = parse_qs(request.content.decode())
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).decode().rstrip('=')
            assert challenge == authorization['code_challenge'][0]
            assert form['code'] == ['code-fixture']
            assert form['redirect_uri'] == authorization['redirect_uri']
            return httpx.Response(200, json={'access_token': 'access-fixture', 'refresh_token': 'refresh-fixture', 'expires_in': 3600})
        if request.url.path.endswith('loadCodeAssist'):
            assert request.headers['Authorization'] == 'Bearer access-fixture'
            return httpx.Response(200, json={'cloudaicompanionProject': 'subscription-project',
                                            'currentTier': {'id': 'free-tier'}, 'paidTier': {'id': 'plus-fixture'}})
        return httpx.Response(200, json={'email': 'fixture@example.test'})

    async def authorize(url):
        authorization.update(parse_qs(urlsplit(url).query))
        redirect = authorization['redirect_uri'][0]
        async with httpx.AsyncClient(trust_env=False) as browser:
            wrong = await browser.get(redirect, params={'code': 'wrong', 'state': 'wrong'})
            assert wrong.status_code == 400
            correct = await browser.get(redirect, params={'code': 'code-fixture', 'state': authorization['state'][0]})
            assert correct.status_code == 200
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        tokens = await oauth.login(on_authorize_url=authorize, open_browser=False, client=client, callback_port=0)
    assert tokens.project_id == 'subscription-project'
    assert tokens.tier == 'plus-fixture'
    assert tokens.account_id == 'fixture@example.test'
    assert 'access-fixture' not in repr(tokens)
    address = urlsplit(authorization['redirect_uri'][0])
    with pytest.raises(OSError):
        await asyncio.open_connection(address.hostname, address.port)


@pytest.mark.asyncio
async def test_cancel_closes_callback_listener():
    ready = asyncio.Event()
    authorization = {}
    async def announce(url):
        authorization.update(parse_qs(urlsplit(url).query))
        ready.set()
    task = asyncio.create_task(oauth.login(on_authorize_url=announce, open_browser=False, callback_port=0))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    address = urlsplit(authorization['redirect_uri'][0])
    with pytest.raises(OSError):
        await asyncio.open_connection(address.hostname, address.port)


@pytest.mark.asyncio
async def test_refresh_preserves_refresh_token_and_model_discovery_is_real():
    seen = []
    def respond(request):
        seen.append(request)
        if request.url == oauth.TOKEN_URL:
            assert parse_qs(request.content.decode())['refresh_token'] == ['stored-refresh']
            return httpx.Response(200, json={'access_token': 'new-access', 'expires_in': 900})
        return httpx.Response(200, json={'models': {'gemini-fixture': {}, 'gemini-hidden': {'isInternal': True}, 'internal-agent': {}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tokens = await oauth.refresh('stored-refresh', client=client)
        assert tokens.refresh_token == 'stored-refresh'
        assert await oauth.list_models(tokens.access_token, client=client) == ['gemini-fixture']
    assert seen[-1].method == 'POST'
    assert 'fetchAvailableModels' in seen[-1].url.path


@pytest.mark.asyncio
async def test_subscription_stream_retains_python_call_signature_usage_and_manifest(monkeypatch):
    import llm_cloud_stream as cloud
    from model_providers.registry import ProviderRegistry
    from model_runtime.prompt_cache import resolve_prompt_cache_identity
    from session_catalog.service import IPYTHON_PROVIDER_SPEC
    from tests.test_model_request_adapter_hooks import _Router, _install_transport, _assert_receipt_matches_wire
    from tool_calling import ToolCallAccumulator
    event = {'response': {'candidates': [{'content': {'parts': [
        {'text': 'Working'}, {'functionCall': {'name': 'ipython', 'args': {'category': 'Build', 'code': 'x = 7'}}, 'thoughtSignature': 'opaque-signature'}]},
        'finishReason': 'STOP'}], 'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 4,
                                                  'cachedContentTokenCount': 8, 'totalTokenCount': 16}}}
    captured = _install_transport(monkeypatch, cloud, 'data: ' + json.dumps(event) + '\n\n')
    router = _Router()
    router._oauth_rec = lambda _: {'project_id': 'subscription-project'}
    profile = ProviderRegistry().get(oauth.PROVIDER)
    sink = ToolCallAccumulator()
    result = [piece async for piece in cloud.call_gemini(router, [{'role': 'user', 'content': 'Use Python'}],
        {'max_tokens': 64}, 'bearer-fixture', profile=profile, model='gemini-fixture',
        tools=[IPYTHON_PROVIDER_SPEC], tool_call_sink=sink,
        prompt_cache_identity=resolve_prompt_cache_identity('chat-fixture'))]
    assert result == ['Working']
    manifest, payload = _assert_receipt_matches_wire(router, captured, adapter='google_ai.generate_content')
    assert payload['project'] == 'subscription-project'
    assert payload['request']['contents'][0]['parts'][0]['text'] == 'Use Python'
    assert captured[0].headers['authorization'] == 'Bearer bearer-fixture'
    assert 'key=' not in str(captured[0].url)
    assert manifest['tools']['rendered'][0]['name'] == 'ipython'
    assert manifest['messages']['rendered']['count'] == 1
    assert router.usage[-1][1]['raw_usage']['cachedContentTokenCount'] == 8
    calls = sink.actions()
    assert calls[0]['tool'] == 'ipython'
    assert calls[0]['provider_replay']['gemini']['thought_signature'] == 'opaque-signature'


def test_oauth_save_failure_restores_the_previous_account(tmp_path, monkeypatch):
    from tests.test_provider_parity_hunt import _router
    router = _router(tmp_path)
    router.set_oauth_tokens(oauth.PROVIDER, access_token='old', refresh_token='old-refresh',
                            expires_at=int(time.time()) + 900, project_id='old-project')
    old = dict(router._oauth_rec(oauth.PROVIDER))
    monkeypatch.setattr(router, 'save_config', lambda: False)
    with pytest.raises(OSError, match='could not be saved'):
        router.set_oauth_tokens(oauth.PROVIDER, access_token='new', project_id='new-project', replace=True)
    assert router._oauth_rec(oauth.PROVIDER) == old
    assert router.oauth_required_for_route(oauth.PROVIDER)


def test_connected_native_oauth_route_follows_saved_account_and_explicit_revocation(tmp_path, monkeypatch):
    from tests.test_provider_parity_hunt import _router
    from session_catalog.profiles import ACTION_SURFACE
    from session_catalog.support import UnsupportedModelRoute
    router = _router(tmp_path)
    route = dict(profile=ACTION_SURFACE, provider=oauth.PROVIDER,
                 model='gemini-fixture', adapter='gemini.generate_content')
    with pytest.raises(UnsupportedModelRoute):
        router._support_matrix.validate(**route)
    save = router.save_config
    monkeypatch.setattr(router, 'save_config', lambda: False)
    with pytest.raises(OSError):
        router.set_oauth_tokens(oauth.PROVIDER, access_token='fixture')
    with pytest.raises(UnsupportedModelRoute):
        router._support_matrix.validate(**route)
    monkeypatch.setattr(router, 'save_config', save)
    router.set_oauth_tokens(oauth.PROVIDER, access_token='fixture')
    assert router._support_matrix.validate(**route).status == 'developer'
    monkeypatch.setattr(router, 'save_config', lambda: False)
    with pytest.raises(OSError):
        router.clear_oauth(oauth.PROVIDER)
    assert router._oauth_rec(oauth.PROVIDER)['access_token']
    assert router._support_matrix.validate(**route).status == 'developer'
    monkeypatch.setattr(router, 'save_config', save)
    router.replace_support_matrix([{**route, 'status': 'revoked'}])
    with pytest.raises(UnsupportedModelRoute, match='revoked'):
        router._support_matrix.validate(**route)
    router.replace_support_matrix([])
    router.clear_oauth(oauth.PROVIDER)
    with pytest.raises(UnsupportedModelRoute):
        router._support_matrix.validate(**route)


def test_oauth_encryption_failure_does_not_publish_partial_account(tmp_path, monkeypatch):
    from tests.test_provider_parity_hunt import _router
    import security.secretstore as secrets
    router = _router(tmp_path)
    router.set_oauth_tokens(oauth.PROVIDER, access_token='old', project_id='old-project')
    old = dict(router._oauth_rec(oauth.PROVIDER))
    revision = dict(router._oauth_revisions)
    def failed_encrypt(_):
        raise OSError('encryption unavailable')
    monkeypatch.setattr(secrets, 'encrypt', failed_encrypt)
    with pytest.raises(OSError, match='encryption unavailable'):
        router.set_oauth_tokens(oauth.PROVIDER, access_token='new', replace=True)
    assert router._oauth_rec(oauth.PROVIDER) == old
    assert router._oauth_revisions == revision


@pytest.mark.asyncio
async def test_ui_oauth_receipt_persists_account_project(monkeypatch):
    import ws_config
    import background_tasks
    from tests.test_ws_oauth_lifecycle import Server, Socket
    found = {}
    def on(name):
        def decorate(fn):
            found[name] = fn
            return fn
        return decorate
    ws_config.register(on)
    async def login(**kwargs):
        await kwargs['on_authorize_url']('https://accounts.google.com/fixture')
        return oauth.TokenSet('a', 'r', int(time.time()) + 900, project_id='project', tier='plus')
    monkeypatch.setattr(oauth, 'login', login)
    server, socket = Server(), Socket()
    await found['cloud:oauth:start'](server, socket, None, {'provider': oauth.PROVIDER, 'request_id': 'google-flow', 'open_browser': False})
    task = server._oauth_attempts[oauth.PROVIDER]['task']
    await task
    assert server.router.saved[-1][1]['project_id'] == 'project'
    assert any(row.get('type') == 'cloud:oauth:complete' and row['request_id'] == 'google-flow' for row in socket.sent)
    await background_tasks.cancel_all()
