"""WebSocket state and protocol for the visible Main Deck browser host."""

from __future__ import annotations

from browser_fabric import interactive
from browser_fabric.models import BrowserConflict, BrowserValidationError


def _preferences(srv):
    return srv.require_runtime().browser.preferences


def _chat_id(srv, msg):
    chat_id = str(msg.get('chat_id') or '')
    if not chat_id or srv.require_runtime().sessions.get_session(chat_id) is None:
        raise BrowserValidationError('The requested chat is unavailable.')
    return chat_id


def _error(exc):
    return {'code': getattr(exc, 'state', None) or ('revision_conflict' if isinstance(exc, BrowserConflict) else 'browser_request_failed'),
            'message': str(exc) or type(exc).__name__}


def register(on):
    @on('browser:recordings:get')
    async def _recordings(srv, websocket, session, msg):
        response = {'type': 'browser:recordings', 'request_id': msg.get('request_id'),
                    'chat_id': msg.get('chat_id')}
        try:
            response['items'] = _preferences(srv).recordings(_chat_id(srv, msg))
        except Exception as exc:
            response['error'] = _error(exc)
        await websocket.send_json(response)

    @on('browser:settings:get')
    async def _settings(srv, websocket, session, msg):
        response = {'type': 'browser:settings', 'request_id': msg.get('request_id')}
        try:
            response.update(await _preferences(srv).settings())
        except Exception as exc:
            response['error'] = _error(exc)
        await websocket.send_json(response)

    @on('browser:state:get')
    async def _state(srv, websocket, session, msg):
        response = {'type': 'browser:state', 'request_id': msg.get('request_id'), 'chat_id': msg.get('chat_id')}
        try:
            response.update(await _preferences(srv).refresh_state(_chat_id(srv, msg)))
        except Exception as exc:
            response['error'] = _error(exc)
        await websocket.send_json(response)

    @on('browser:selection:set')
    async def _selection(srv, websocket, session, msg):
        response = {'type': 'browser:selection:result', 'request_id': msg.get('request_id'),
                    'scope': msg.get('scope'), 'chat_id': msg.get('chat_id') or '', 'ok': False}
        try:
            scope = str(msg.get('scope') or '')
            chat_id = _chat_id(srv, msg) if scope == 'chat' else ''
            revision = msg.get('expected_revision')
            if revision is not None and (type(revision) is not int or revision < 0):
                raise BrowserValidationError('expected_revision must be a non-negative integer.')
            response.update(await _preferences(srv).set_selection(scope, chat_id, msg.get('selection'), revision))
            response['ok'] = True
        except Exception as exc:
            response['error'] = _error(exc)
        await websocket.send_json(response)

    @on('browser:resolve')
    async def _resolve(srv, websocket, session, msg):
        response = {'type': 'browser:resolve:result', 'request_id': msg.get('request_id'),
                    'chat_id': msg.get('chat_id'), 'operation_id': msg.get('operation_id'), 'ok': False}
        try:
            await _preferences(srv).resolve(_chat_id(srv, msg), str(msg.get('operation_id') or ''), str(msg.get('action') or ''))
            response['ok'] = True
        except Exception as exc:
            response['error'] = _error(exc)
        await websocket.send_json(response)

    @on("browser:host:register")
    async def _register_host(srv, websocket, session, msg):
        await interactive.register_host(websocket)
        await websocket.send_json({
            "type": "browser:host:registered",
            "available": True,
        })

    @on("browser:host:result")
    async def _host_result(srv, websocket, session, msg):
        interactive.resolve_host_result(
            websocket,
            str(msg.get("id") or ""),
            msg.get("result"),
        )
