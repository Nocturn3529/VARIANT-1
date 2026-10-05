"""Actual endpoint regression for a browser RPC on its own host socket."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import server_http
import ws_browser
from browser_fabric import interactive
from browser_fabric.adapters import EmbeddedBrowserAdapter
from browser_fabric.preferences import BrowserPreferences
from chat_session import ConnectionSession
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.support.conversation_sessions import open_sessions
from work_fabric.scope import WorkScope


@pytest.mark.asyncio
async def test_refresh_reads_its_own_ack_without_reordering_commands(tmp_path, monkeypatch):
    broker = interactive.BrowserHostBroker()
    monkeypatch.setattr(interactive, 'BROKER', broker)
    incoming = asyncio.Queue()
    sent, order = [], []
    sessions = open_sessions(tmp_path / 'sessions')
    sid = sessions.create_session()
    runtimes = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / 'runtime.sqlite3')))
    class Socket:
        query_params = {'token': 'test'}
        async def accept(self): pass
        async def receive_text(self):
            value = await incoming.get()
            if value is None: raise server_http.WebSocketDisconnect()
            return json.dumps(value)
        async def send_json(self, value):
            sent.append(value)
            if value.get('type') == 'browser:host:command':
                await incoming.put({'type': 'browser:host:result', 'id': value['id'],
                    'result': {'ok': True, 'tabs': [{'id': 'tab', 'url': 'https://fixture.invalid', 'active': True}]}})
    socket = Socket()
    await broker.register(socket)
    async def request(command): return await broker.request(command, timeout=.5)
    adapter = EmbeddedBrowserAdapter(request=request, owner_chat_id=sid)
    record = SimpleNamespace(session_id='browser', kind='embedded', scope=WorkScope(chat_id=sid), current_target_id='target')
    async def reconcile(*args, **kwargs):
        await adapter.targets()
        return record
    fabric = SimpleNamespace(store=SimpleNamespace(get_target=lambda _: SimpleNamespace(url='https://fixture.invalid')),
        session_for_owner=lambda **_: record, reconcile_current_page=reconcile)
    class Preferences(BrowserPreferences):
        def effective(self, *_): return {'selection': {'mode': 'embedded'}}
        def state(self, *_): return {'state': getattr(self, 'observed', 'idle')}
        async def _state(self, chat_id, state, message='', **fields): self.observed = state
    prefs = Preferences(fabric)
    runtime = SimpleNamespace(sessions=sessions, session_runtimes=runtimes,
        chat=SimpleNamespace(orphaned_task_payload=lambda _: None),
        tool_settings=SimpleNamespace(state=lambda: {'type': 'tools'}), browser=SimpleNamespace(preferences=prefs))
    hub = SimpleNamespace(active=set(), add=lambda ws: hub.active.add(ws), remove=lambda ws: hub.active.discard(ws))
    host = SimpleNamespace(hub=hub, require_runtime=lambda: runtime)
    handlers = {}
    def on(*names):
        def register(handler):
            for name in names: handlers[name] = handler
            return handler
        return register
    ws_browser.register(on)
    async def dispatch(srv, ws, session, msg):
        if msg['type'] == 'browser:state:get':
            order.append('refresh-start')
            await handlers['browser:state:get'](srv, ws, session, msg)
            order.append('refresh-finished')
        else:
            order.append('navigation')
            await incoming.put(None)
        return True
    monkeypatch.setattr(server_http.ws_dispatch, 'dispatch', dispatch)
    monkeypatch.setattr(server_http, 'hello_payload', lambda *_: {'type': 'hello'})
    monkeypatch.setattr(server_http, 'cancel_session_work', AsyncMock())
    await incoming.put({'type': 'browser:state:get', 'chat_id': sid, 'request_id': 'refresh'})
    await incoming.put({'type': 'chat:session:switch', 'id': sid})
    await asyncio.wait_for(server_http.websocket_endpoint(host, socket, auth_token='test',
        session_factory=ConnectionSession), 2)
    assert order == ['refresh-start', 'refresh-finished', 'navigation']
    assert next(row for row in sent if row.get('type') == 'browser:state')['state'] == 'ready'
    assert not broker._pending and not broker.available and not hub.active
    assert runtimes.attachment_count(sid) == 0
