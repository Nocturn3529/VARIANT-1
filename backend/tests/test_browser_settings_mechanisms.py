from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from browser_fabric.adapters import ManagedPlaywrightAdapter
from browser_fabric.cloud import CloudBrowserLease
from browser_fabric.models import BrowserUnavailable, BrowserValidationError
from browser_fabric.preferences import BrowserPreferences
from browser_fabric.settings import is_private_url


class Page:
    url = 'https://example.test'
    viewport_size = {'width': 800, 'height': 600}
    def __init__(self):
        self.listeners = {}
        self.evaluate = AsyncMock(return_value='')
        self.title = AsyncMock(return_value='Example')
        self.close = AsyncMock()
        self.goto = AsyncMock()
    def on(self, event, callback):
        self.listeners[event] = callback
    def is_closed(self):
        return False


def runtime():
    page = Page()
    context = SimpleNamespace(pages=[page], close=AsyncMock(), route=AsyncMock(), unroute=AsyncMock(), on=lambda *_: None)
    remote = SimpleNamespace(contexts=[context], close=AsyncMock())
    chromium = SimpleNamespace(connect_over_cdp=AsyncMock(return_value=remote), launch_persistent_context=AsyncMock(return_value=context))
    return SimpleNamespace(chromium=chromium, stop=AsyncMock()), context, remote, page


@pytest.mark.asyncio
async def test_cdp_attach_retains_users_context_and_skips_launch(tmp_path):
    driver, context, remote, _ = runtime()
    adapter = ManagedPlaywrightAdapter(str(tmp_path), playwright_factory=lambda: driver,
        browser_settings={'cdp_url': 'http://127.0.0.1:9222', 'command_timeout_s': 15})
    pages = await adapter.launch(())
    assert len(pages) == 1
    driver.chromium.connect_over_cdp.assert_awaited_once_with('http://127.0.0.1:9222', timeout=15000)
    driver.chromium.launch_persistent_context.assert_not_awaited()
    await adapter.close()
    context.close.assert_not_awaited()
    remote.close.assert_not_awaited()
    driver.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_recording_dialog_and_private_policy_are_consumed(tmp_path):
    driver, context, _, page = runtime()
    adapter = ManagedPlaywrightAdapter(str(tmp_path), playwright_factory=lambda: driver,
        headless=False, browser_settings={'record_sessions': True, '_recording_id': 'owned',
                                         'dialog_policy': 'auto_accept', 'allow_private_urls': False})
    await adapter.launch(())
    options = driver.chromium.launch_persistent_context.await_args.kwargs
    assert options['headless'] is False
    assert Path(options['record_video_dir']) == tmp_path / 'recordings' / 'owned'
    dialog = SimpleNamespace(accept=AsyncMock(), dismiss=AsyncMock())
    page.listeners['dialog'](dialog)
    await asyncio.gather(*adapter._page_tasks)
    dialog.accept.assert_awaited_once()
    route_handler = context.route.await_args.args[1]
    route = SimpleNamespace(request=SimpleNamespace(url='http://127.0.0.1/private'),
                            abort=AsyncMock(), continue_=AsyncMock())
    await route_handler(route)
    route.abort.assert_awaited_once_with('blockedbyclient')
    await adapter.close()
    context.unroute.assert_awaited_once_with('**/*', route_handler)
    context.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_timeout_can_be_overridden_without_changing_default(tmp_path):
    locator = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
    page = SimpleNamespace(locator=lambda _: locator)
    adapter = ManagedPlaywrightAdapter(str(tmp_path), browser_settings={'click_timeout_s': 7})
    await adapter._perform_core(page, 'click', {'backend_ref': 'mf_aaaaaaaaaaaa_1'})
    assert locator.click.await_args.kwargs['timeout'] == 7000
    await adapter._perform_core(page, 'click', {'backend_ref': 'mf_aaaaaaaaaaaa_1', 'timeout_ms': 200000})
    assert locator.click.await_args.kwargs['timeout'] == 200000


@pytest.mark.parametrize('selection', [
    {'mode': 'managed', 'headed': 'false'},
    {'mode': 'managed', 'command_timeout_s': 0},
    {'mode': 'embedded', 'record_sessions': True},
    {'mode': 'cdp', 'cdp_url': 'file:///profile'},
    {'mode': 'cloud', 'cloud_provider': 'browserbase'},
    {'mode': 'cloud', 'cloud_provider': 'unimplemented'},
    {'mode': 'embedded', 'navigation_timeout_s': 30},
])
def test_unsupported_settings_cannot_silently_save(selection):
    with pytest.raises(BrowserValidationError):
        BrowserPreferences.normalize(selection)


@pytest.mark.asyncio
async def test_private_browser_policy_recognizes_local_and_lan():
    assert await is_private_url('http://localhost/app')
    assert await is_private_url('http://192.168.1.1/app')
    assert await is_private_url('http://[::1]/app')
    assert not await is_private_url('https://8.8.8.8/')


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['browserbase', 'browser-use', 'firecrawl'])
async def test_cloud_browser_uses_cdp_and_releases_without_persisting_signed_url(tmp_path, provider):
    requests = []
    signed = 'wss://fixture.test/cdp?token=private-cdp-token'
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'session-1', 'cdpUrl': signed, 'connectUrl': signed})
    factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
    resolver = AsyncMock(return_value=SimpleNamespace(secret='private-provider-key', base_url=''))
    options = {'mode': 'cloud', 'cloud_provider': provider, 'project_id': 'project'}
    lease = CloudBrowserLease(options, str(tmp_path), resolver, client_factory=factory)
    assert await lease.connect() == signed
    assert requests[0].method == 'POST'
    persisted = lease.path.read_text()
    assert 'private-cdp-token' not in persisted and 'private-provider-key' not in persisted
    await lease.close()
    assert json.loads(lease.path.read_text())['state'] == 'closed'
    assert len(requests) == 2
    assert requests[1].method == {'browserbase': 'POST', 'browser-use': 'PATCH', 'firecrawl': 'DELETE'}[provider]


@pytest.mark.asyncio
async def test_ambiguous_cloud_create_is_not_blindly_replayed(tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout('connection lost')
    factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
    resolver = AsyncMock(return_value=SimpleNamespace(secret='fixture', base_url=''))
    lease = CloudBrowserLease({'cloud_provider': 'firecrawl'}, str(tmp_path), resolver, client_factory=factory)
    with pytest.raises(httpx.ReadTimeout):
        await lease.connect()
    recovered = CloudBrowserLease({'cloud_provider': 'firecrawl'}, str(tmp_path), resolver, client_factory=factory)
    with pytest.raises(BrowserUnavailable, match='interrupted'):
        await recovered.connect()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cloud_reconnect_uses_list_for_firecrawl_and_does_not_create_again(tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == '/v2/interact'
        return httpx.Response(200, json={'sessions': [{'id': 'known', 'status': 'active', 'cdpUrl': 'wss://fixture.test/cdp'}]})
    factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
    resolver = AsyncMock(return_value=SimpleNamespace(secret='fixture', base_url=''))
    lease = CloudBrowserLease({'cloud_provider': 'firecrawl'}, str(tmp_path), resolver, client_factory=factory)
    lease._save({'state': 'active', 'id': 'known'})
    assert await lease.connect() == 'wss://fixture.test/cdp'
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_rejected_cloud_allocation_can_retry_after_correcting_credentials(tmp_path):
    count = 0
    def respond(_request):
        nonlocal count
        count += 1
        return httpx.Response(401) if count == 1 else httpx.Response(200, json={'id': 'new', 'cdpUrl': 'wss://fixture.test/cdp'})
    factory = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
    resolver = AsyncMock(return_value=SimpleNamespace(secret='fixture', base_url=''))
    lease = CloudBrowserLease({'cloud_provider': 'firecrawl'}, str(tmp_path), resolver, client_factory=factory)
    with pytest.raises(BrowserUnavailable, match='401'):
        await lease.connect()
    assert lease.lease['state'] == 'closed'
    assert await lease.connect() == 'wss://fixture.test/cdp'
