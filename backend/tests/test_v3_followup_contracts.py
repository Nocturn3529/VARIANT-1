"""Replay V3 discovery, handle and attachment failure contracts without inference."""
from types import SimpleNamespace

import pytest

from browser_fabric import AdapterDownload
from browser_fabric.models import BrowserUnavailable
from browser_fabric.capabilities import _observation_result, _page_handle, _handle_router
from kernel_runtime.worker_bridge import ReadOnlyTools, ToolbeltNamespace, _decode_host_result
from tests.test_browser_fabric_phase4 import browser_runtime, capability_context
from tests.test_browser_result_recovery import host_for
from work_fabric.scope import WorkScope


def test_missing_capability_discloses_category_without_mounting_or_enabling():
    document = {
        'selected_category_id': 'explore',
        'category_options': [{'category_id': 'build', 'summary': 'Files and processes'}],
        'catalog_index': [
            {'alias': 'read_file', 'namespace': 'tools', 'category_id': 'build',
             'enabled': True, 'call': 'tools.read_file(path)', 'signature': 'read_file(path)'},
            {'alias': 'disabled', 'category_id': 'build', 'enabled': False},
        ],
    }
    belt = ToolbeltNamespace(document)
    context = SimpleNamespace(namespace={}, protected_globals={'toolbelt': belt})
    tools = ReadOnlyTools([], SimpleNamespace(), kernel_context=context)
    for action in (lambda: tools.read_file, lambda: tools.describe('read_file'),
                   lambda: tools.documentation('read_file')):
        with pytest.raises((AttributeError, KeyError), match="ipython\\(category='build'"):
            action()
    with pytest.raises(AttributeError, match='disabled; selecting a category will not enable'):
        tools.disabled
    with pytest.raises(AttributeError, match='No pinned catalog match'):
        tools.fictional
    assert tools.methods() == []
    described = belt.describe('build')
    assert described['selected'] is False
    assert described['capabilities'][0]['signature'] == 'read_file(path)'
    assert belt.describe('read_file')['category_id'] == 'build'
    assert document['selected_category_id'] == 'explore'
    # Retained tools consult the current protected catalog, not an obsolete one.
    context.protected_globals['toolbelt'] = ToolbeltNamespace({'catalog_index': []})
    with pytest.raises(AttributeError, match='No pinned catalog match'):
        tools.read_file


@pytest.mark.asyncio
async def test_new_page_retains_direct_calls_and_adds_parent_handle(browser_runtime):
    fabric, _ = browser_runtime
    scope = WorkScope(chat_id='v3-page')
    context = capability_context(scope)
    host = host_for(fabric)
    session = await fabric.open_session(scope=scope)
    calls = []
    class Bridge:
        async def invoke_async(self, descriptor, payload, **kwargs):
            calls.append(payload)
            return await _handle_router(host, context, payload['handle'], payload['method'], payload['arguments'])
    bridge = Bridge()
    target = fabric.store.get_target(fabric.page_ref(session.session_id).target_id)
    page = _decode_host_result(_page_handle(host, context, session, target), bridge)
    assert page.page is page and page['page'] is page
    assert page.session.id == page['session'].id == page.session_id
    assert calls == []  # Parent discovery is local, not another execution path.
    created = _decode_host_result(await page.session.new_page.async_('https://new.test'), bridge)
    assert created.kind == 'page' and created.page is created
    assert created.session.id == session.session_id
    observed = _decode_host_result(await created.observe.async_(), bridge)
    assert observed.page.id == created.id
    assert observed.session.id == created.session.id
    assert len(calls) == 2


async def attachment_fixture(fabric, adapters):
    scope = WorkScope(chat_id='v3-download')
    session = await fabric.open_session(scope=scope)
    page = await fabric.new_page(session.session_id, url='https://file.test/release.txt', scope=scope)
    target = fabric.store.get_target(page.target_id)
    adapter = adapters[0]
    status = {'document_ready': False, 'url': target.url}
    async def document_status(_target): return dict(status)
    async def unavailable(*args, **kwargs): raise BrowserUnavailable('document not ready [GUEST_NOT_READY]')
    adapter.document_status = document_status
    adapter.observe = unavailable
    await fabric._record_adapter_download(session.session_id, target.backend_target_id, '',
        AdapterDownload('release.txt', target.url, b'fresh bytes'))
    return scope, session, page, status


@pytest.mark.asyncio
async def test_attachment_observation_commits_artifact_without_fake_document(browser_runtime, tmp_path):
    fabric, adapters = browser_runtime
    scope, session, page, _ = await attachment_fixture(fabric, adapters)
    observation = await fabric.observe(page, scope=scope, include_screenshot=True, idempotency_key='read-download')
    assert observation.document['state'] == 'attachment'
    assert not observation.elements and not observation.screenshot_artifact_ref
    assert fabric.store.get_observation(observation.observation_id).document == observation.document
    reopened = type(fabric.store)(fabric.store.path)
    assert reopened.get_observation(observation.observation_id).document == observation.document
    replay = await fabric.observe(page, scope=scope, include_screenshot=True, idempotency_key='read-download')
    assert replay.observation_id == observation.observation_id
    host = host_for(fabric)
    context = capability_context(scope)
    class Bridge: pass
    bridge = Bridge()
    decoded = _decode_host_result(_observation_result(host, context, fabric, observation), bridge)
    assert decoded.document_state == 'attachment' and decoded.download_state == 'completed'
    assert decoded.download['artifact'].size == len(b'fresh bytes')
    assert "no rendered document" in repr(decoded)
    assert "navigate or reload" not in repr(decoded)
    assert decoded.page.session.id == session.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['unknown_readiness', 'missing_url', 'different_url', 'later_failed_navigation', 'later_same_url_navigation'])
async def test_old_download_cannot_hide_unavailable_or_changed_page(browser_runtime, change):
    fabric, adapters = browser_runtime
    scope, session, page, status = await attachment_fixture(fabric, adapters)
    if change == 'unknown_readiness': status.pop('document_ready')
    if change == 'missing_url': status.pop('url')
    if change == 'different_url': status['url'] = 'https://other.test'
    if change in {'later_failed_navigation', 'later_same_url_navigation'}:
        if change == 'later_failed_navigation': adapters[0].fail_actions.add('navigate')
        try: await fabric.navigate(page, status['url'], scope=scope)
        except Exception: pass
        page = fabric.page_ref(session.session_id, page.target_id)
    with pytest.raises(BrowserUnavailable, match='GUEST_NOT_READY'):
        await fabric.observe(page, scope=scope)


@pytest.mark.asyncio
async def test_completed_download_does_not_claim_ready_but_uninspectable_document_was_observed(browser_runtime):
    fabric, adapters = browser_runtime
    scope, session, page, status = await attachment_fixture(fabric, adapters)
    status['document_ready'] = True  # Host readiness also checks visibility.
    result = await fabric.observe(page, scope=scope, include_screenshot=True)
    assert result.document['state'] == 'unavailable_after_download'
    assert result.document['native_document_ready'] is True
    assert result.document['observation_status'] == 'unavailable'
    assert 'GUEST_NOT_READY' in result.document['observation_error']
    assert result.document['download_state'] == 'completed'
    assert not result.text_excerpt and not result.elements and not result.screenshot_artifact_ref


@pytest.mark.asyncio
async def test_native_new_tab_download_can_have_no_committed_url(browser_runtime):
    fabric, adapters = browser_runtime
    scope, session, page, status = await attachment_fixture(fabric, adapters)
    status['url'] = ''  # Exact Electron response captured by the native check.
    observed = await fabric.observe(page, scope=scope)
    assert observed.document['state'] == 'attachment'
    assert observed.document['native_url'] == ''
    assert observed.document['requested_url'] == 'https://file.test/release.txt'
    # A fresh read still returns the same completed artifact without replaying
    # the new-page action, and never invents rendered text/elements.
    again = await fabric.observe(page, scope=scope)
    assert again.document['downloads'] == observed.document['downloads']
    assert not again.text_excerpt and not again.elements
    # The exception is specific to a new-tab attachment, not any blank URL.
    await fabric.navigate(page, 'https://file.test/release.txt', scope=scope)
    with pytest.raises(BrowserUnavailable):
        await fabric.observe(fabric.page_ref(session.session_id, page.target_id), scope=scope)


@pytest.mark.asyncio
async def test_embedded_readiness_queries_live_inventory_not_cached_state():
    from browser_fabric import EmbeddedBrowserAdapter
    live = [{'id': 'tab', 'url': 'https://file.test', 'document_ready': False}]
    calls = []
    async def request(command):
        calls.append(command)
        assert command['action'] == 'tabs'
        return {'tabs': list(live)}
    adapter = EmbeddedBrowserAdapter(request)
    assert await adapter.document_status('tab') == {'url': 'https://file.test', 'document_ready': False}
    live[0] = {'id': 'tab', 'url': 'https://new.test', 'document_ready': True}
    assert (await adapter.document_status('tab'))['document_ready'] is True
    live.clear()
    assert await adapter.document_status('tab') == {}
    assert len(calls) == 3
