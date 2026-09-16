from __future__ import annotations

import base64
import io

from PIL import Image
import pytest

from browser_fabric import create_browser_fabric
from browser_fabric.models import BrowserValidationError
from work_fabric.scope import WorkScope


@pytest.mark.asyncio
async def test_viewport_uses_the_existing_host_and_survives_reconstruction(tmp_path):
    commands = []
    tabs = {}

    async def request(command):
        commands.append(command)
        action = command['action']
        tab = command.get('tab_id')
        if action == 'tabs':
            return {'ok': True, 'tabs': list(tabs.values())}
        if action == 'new_page':
            tabs.setdefault(tab, {'id': tab, 'tab_id': tab, 'title': 'Fixture', 'url': command.get('url') or 'about:blank', 'active': True,
                                  'viewport': {'width': 800, 'height': 480, 'mode': 'auto', 'visible_width': 182, 'visible_height': 400, 'device_scale_factor': 1}})
        state = tabs[tab]
        result = {'ok': True, 'state': dict(state), 'target': dict(state)}
        if action == 'set_viewport':
            state['viewport'] = {**state['viewport'], **{key: command[key] for key in ('width', 'height', 'mode') if key in command}}
            result.update(state=dict(state), viewport=state['viewport'])
        if action == 'read':
            result.update(title='Fixture', url=state['url'], text='Fixture', elements=[], viewport=state['viewport'])
        if action == 'screenshot':
            out = io.BytesIO()
            Image.new('RGB', (state['viewport']['width'], state['viewport']['height'])).save(out, format='PNG')
            result.update(image=base64.b64encode(out.getvalue()).decode(), viewport=state['viewport'])
        return result

    path = str(tmp_path / 'browser.sqlite3')
    fabric = create_browser_fabric(path=path, embedded_request=request)
    scope = WorkScope(chat_id='chat-viewport')
    session = await fabric.open_session(kind='embedded', scope=scope)
    page = fabric.page_ref(session.session_id)
    try:
        before = len(commands)
        with pytest.raises(BrowserValidationError, match='INVALID_VIEWPORT'):
            await fabric.perform(page, 'set_viewport', params={'width': 12, 'height': 720}, scope=scope)
        assert len(commands) == before
        result = await fabric.perform(page, 'set_viewport', params={'width': 1280, 'height': 720}, scope=scope)
        assert result['viewport']['width'] == 1280
        image = await fabric.screenshot(fabric.page_ref(session.session_id), scope=scope)
        assert (image['image_width'], image['image_height']) == (1280, 720)
        observation = await fabric.observe(fabric.page_ref(session.session_id), max_elements=0, scope=scope)
        assert observation.viewport['width'] == 1280
        assert next(c for c in reversed(commands) if c['action'] == 'read')['max_elements'] == 0
        assert fabric.store.get_target(page.target_id).viewport['mode'] == 'fixed'
    finally:
        await fabric.shutdown()
    restarted = create_browser_fabric(path=path, embedded_request=request)
    restarted.startup()
    tabs.clear()
    try:
        await restarted.acquire_session(session.session_id, scope=scope)
        target = restarted.store.get_target(page.target_id)
        assert target.viewport['width'] == 1280 and target.viewport['height'] == 720
        assert sum(command['action'] == 'set_viewport' for command in commands) == 2
    finally:
        await restarted.shutdown()
