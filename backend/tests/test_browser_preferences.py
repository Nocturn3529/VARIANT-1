from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from browser_fabric import AdapterTarget, AdapterObservation, BrowserAdapter, create_browser_fabric
from browser_fabric.models import BrowserConflict, ElementRecord
from browser_fabric.personal_profiles import BrowserProfileRequired, prepare_snapshot, discover_browsers, selected_source
from work_fabric.scope import WorkScope


class Adapter(BrowserAdapter):
    capabilities = frozenset({'navigate', 'observe', 'tabs'})
    kind = 'managed'

    def __init__(self, owner):
        self.owner = owner
        self.pages = ()

    async def launch(self, targets):
        self.owner.launches += 1
        if self.owner.locked:
            raise BrowserProfileRequired('profile_locked', 'Close the selected browser and retry.')
        self.pages = tuple(AdapterTarget(t.backend_target_id, 'Example', t.url or 'about:blank', True) for t in targets)
        return self.pages

    async def close(self):
        self.owner.closes += 1

    async def targets(self):
        return self.pages

    async def observe(self, backend_target_id, **kwargs):
        return AdapterObservation(title='Example', url='https://example.test', text='Example')

    async def perform(self, backend_target_id, action, params, **kwargs):
        from browser_fabric import AdapterResult
        return AdapterResult(value={'ok': True})


@pytest.fixture
def stack(tmp_path):
    owner = SimpleNamespace(launches=0, closes=0, locked=False)
    fabric = create_browser_fabric(path=str(tmp_path / 'browser.sqlite3'), profile_root=str(tmp_path / 'profiles'), adapter_factory=lambda *args: Adapter(owner))
    catalog = [{'id': 'chrome', 'label': 'Chrome', 'executable': str(tmp_path / 'chrome.exe'), 'user_data_dir': str(tmp_path / 'chrome'),
                'profiles': [{'id': f'p{i}', 'label': f'Person {i}', 'directory_name': f'Profile {i}'} for i in range(8)]}]
    fabric.preferences.discover = lambda: catalog
    return fabric, owner, catalog


async def open_chat(fabric, chat_id):
    return await fabric.preferences.session(chat_id, scope=WorkScope(chat_id=chat_id), metadata={'owner_kind': 'chat', 'owner_id': chat_id}, fallback='managed')


async def waiting(fabric, chat_id):
    for _ in range(100):
        state = fabric.preferences.state(chat_id)
        if state.get('pending_operation_id'):
            return state
        await asyncio.sleep(.01)
    raise AssertionError('Browser did not report a pending state')


@pytest.mark.asyncio
async def test_discovery_does_not_select_or_launch_and_default_cannot_rebind_chat(stack):
    fabric, owner, _ = stack
    try:
        settings = await fabric.preferences.settings()
        assert len(settings['browsers'][0]['profiles']) == 8
        assert settings['default']['selection'] == {'mode': 'embedded'}
        assert owner.launches == 0
        await fabric.preferences.set_selection('default', '', {'mode': 'managed'}, 0)
        first = await open_chat(fabric, 'chat-a')
        await fabric.preferences.set_selection('default', '', {'mode': 'embedded'}, 1)
        again = await open_chat(fabric, 'chat-a')
        assert again.session_id == first.session_id
        assert fabric.preferences.effective('chat-a')['selection'] == {'mode': 'managed'}
        assert fabric.preferences.effective('chat-b')['selection'] == {'mode': 'embedded'}
        with pytest.raises(BrowserConflict):
            await fabric.preferences.set_selection('default', '', {'mode': 'managed'}, 0)
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_missing_profile_waits_without_launching_or_using_another_profile(stack):
    fabric, owner, catalog = stack
    await fabric.preferences.set_selection('default', '', {'mode': 'personal', 'browser_id': 'chrome', 'profile_id': 'p7'}, 0)
    catalog[0]['profiles'].pop()
    task = asyncio.create_task(open_chat(fabric, 'chat-a'))
    try:
        state = await waiting(fabric, 'chat-a')
        assert state['state'] == 'selection_required' and owner.launches == 0
        with pytest.raises(BrowserConflict):
            await fabric.preferences.resolve('chat-b', state['pending_operation_id'], 'retry')
        await fabric.preferences.set_selection('chat', 'chat-a', {'mode': 'managed'}, state['revision'])
        result = await asyncio.wait_for(task, 2)
        assert result.kind == 'managed' and owner.launches == 1
        assert fabric.preferences.state('chat-a').get('pending_operation_id') is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_locked_profile_retries_the_suspended_startup_and_stop_clears_wait(stack):
    fabric, owner, _ = stack
    owner.locked = True
    task = asyncio.create_task(open_chat(fabric, 'chat-a'))
    try:
        state = await waiting(fabric, 'chat-a')
        assert state['state'] == 'profile_locked'
        assert owner.launches == 1 and owner.closes == 1
        owner.locked = False
        await fabric.preferences.resolve('chat-a', state['pending_operation_id'], 'retry')
        await asyncio.wait_for(task, 2)
        assert owner.launches == 2
        # A different chat gets its own profile/session and waiting identity.
        owner.locked = True
        other = asyncio.create_task(open_chat(fabric, 'chat-b'))
        second = await waiting(fabric, 'chat-b')
        await fabric.preferences.resolve('chat-b', second['pending_operation_id'], 'cancel')
        with pytest.raises(asyncio.CancelledError):
            await other
        assert fabric.preferences.state('chat-b')['state'] == 'cancelled'
        await fabric.delete_chat('chat-b')
        assert fabric.store.browser_preference('chat-b')['revision'] == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_snapshot_uses_only_explicit_profile_and_survives_reopen(tmp_path):
    source = tmp_path / 'chrome'
    for i in range(8):
        p = source / f'Profile {i}'
        p.mkdir(parents=True)
        (p / 'Preferences').write_text(json.dumps({'profile': i}))
        (p / 'Cookies').write_bytes(f'cookie-{i}'.encode())
    (source / 'Local State').write_text(json.dumps({'profile': {'last_used': 'Profile 2', 'info_cache': {f'Profile {i}': {'name': f'Person {i}'} for i in range(8)}}}))
    destination = tmp_path / 'owned' / 'selected'
    selected = {'profile_id': 'p7', 'browser_id': 'chrome', 'directory_name': 'Profile 7', 'user_data_dir': str(source)}
    await prepare_snapshot(selected, str(destination))
    assert (destination / 'Default' / 'Cookies').read_bytes() == b'cookie-7'
    normalized = json.loads((destination / 'Local State').read_text())['profile']
    assert normalized['info_cache'] == {'Default': {'name': 'Person 7'}}
    assert normalized['last_used'] == 'Default'
    assert not (destination / 'Profile 2').exists()
    # The managed copy is durable; reopening must not overwrite its live state.
    (destination / 'Default' / 'Cookies').write_bytes(b'updated-in-managed-copy')
    await prepare_snapshot(selected, str(destination))
    assert (destination / 'Default' / 'Cookies').read_bytes() == b'updated-in-managed-copy'
    assert (source / 'Profile 7' / 'Cookies').read_bytes() == b'cookie-7'


def test_installed_profile_discovery_ignores_last_used_identity(tmp_path, monkeypatch):
    local = tmp_path / 'local'
    programs = tmp_path / 'programs'
    exe = programs / 'Google/Chrome/Application/chrome.exe'
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b'not executed')
    data = local / 'Google/Chrome/User Data'
    for i in range(8):
        (data / f'Profile {i}').mkdir(parents=True)
    state = {'profile': {'last_used': 'Profile 2', 'info_cache': {f'Profile {i}': {'name': f'User {i}'} for i in range(8)}}}
    (data / 'Local State').write_text(json.dumps(state))
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setenv('PROGRAMFILES', str(programs))
    monkeypatch.setenv('PROGRAMFILES(X86)', str(tmp_path / 'empty'))
    first = discover_browsers()
    selected = first[0]['profiles'][7]
    state['profile']['last_used'] = 'Profile 1'
    (data / 'Local State').write_text(json.dumps(state))
    second = discover_browsers()
    assert first == second and len(first[0]['profiles']) == 8
    source = selected_source(second, {'browser_id': first[0]['id'], 'profile_id': selected['id']})
    assert source['directory_name'] == 'Profile 7'


@pytest.mark.asyncio
async def test_selection_and_resource_identity_survive_backend_reconstruction(stack):
    fabric, owner, catalog = stack
    await fabric.preferences.set_selection('default', '', {'mode': 'managed'}, 0)
    first = await open_chat(fabric, 'chat-a')
    await fabric.shutdown()
    restarted = create_browser_fabric(path=fabric.store.path, profile_root=fabric.profile_root, adapter_factory=lambda *a: Adapter(owner))
    restarted.preferences.discover = lambda: catalog
    restarted.startup()
    try:
        assert restarted.preferences.state('chat-a')['state'] == 'connection_failed'
        second = await open_chat(restarted, 'chat-a')
        assert second.session_id == first.session_id
        assert second.profile_id == first.profile_id
        assert restarted.preferences.effective('chat-a')['selection'] == {'mode': 'managed'}
    finally:
        await restarted.shutdown()


@pytest.mark.asyncio
async def test_settings_protocol_is_correlated_and_does_not_switch_the_viewed_chat(stack):
    import ws_browser
    fabric, _, _ = stack
    handlers = {}
    def on(name):
        def register(handler):
            handlers[name] = handler
            return handler
        return register
    ws_browser.register(on)
    runtime = SimpleNamespace(browser=fabric, sessions=SimpleNamespace(get_session=lambda cid: {} if cid == 'chat-a' else None))
    srv = SimpleNamespace(require_runtime=lambda: runtime)
    connection = SimpleNamespace(viewed_session_id='chat-kept')
    sent = []
    class WebSocket:
        async def send_json(self, value):
            sent.append(value)
    ws = WebSocket()
    await handlers['browser:settings:get'](srv, ws, connection, {'request_id': 'catalog-1'})
    assert sent[-1]['request_id'] == 'catalog-1' and len(sent[-1]['browsers'][0]['profiles']) == 8
    await handlers['browser:selection:set'](srv, ws, connection, {'request_id': 'select-1', 'scope': 'chat', 'chat_id': 'chat-a', 'selection': {'mode': 'managed'}, 'expected_revision': 0})
    assert sent[-1]['ok'] is True and sent[-1]['chat_id'] == 'chat-a'
    await handlers['browser:selection:set'](srv, ws, connection, {'request_id': 'select-stale', 'scope': 'chat', 'chat_id': 'chat-a', 'selection': {'mode': 'embedded'}, 'expected_revision': 0})
    assert sent[-1]['ok'] is False and sent[-1]['error']['code'] == 'revision_conflict'
    await handlers['browser:state:get'](srv, ws, connection, {'request_id': 'state-1', 'chat_id': 'chat-a'})
    assert sent[-1]['request_id'] == 'state-1' and sent[-1]['selection'] == {'mode': 'managed'}
    assert connection.viewed_session_id == 'chat-kept'


@pytest.mark.asyncio
async def test_password_readiness_does_not_revoke_page_access(stack):
    fabric, _, _ = stack
    session = await open_chat(fabric, 'chat-a')
    try:
        observation = SimpleNamespace(session_id=session.session_id, url='https://example.test/sign-in', elements=[ElementRecord('password-field', 'textbox', 'Password', input_type='password')])
        await fabric.preferences.observation('chat-a', observation)
        assert fabric.preferences.state('chat-a')['state'] == 'login_required'
        assert (await fabric.acquire_session(session.session_id, scope=WorkScope(chat_id='chat-a'))).session_id == session.session_id
        observation.elements = []
        await fabric.preferences.observation('chat-a', observation)
        assert fabric.preferences.state('chat-a')['state'] == 'ready'
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_shutdown_settles_profile_wait_and_prevents_a_new_one(stack):
    fabric, owner, _ = stack
    owner.locked = True
    task = asyncio.create_task(open_chat(fabric, 'chat-shutdown'))
    await waiting(fabric, 'chat-shutdown')
    await fabric.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not fabric.preferences._pending and not fabric.preferences._active_tasks
    with pytest.raises(asyncio.CancelledError):
        await open_chat(fabric, 'chat-late')
    assert owner.launches == 1
