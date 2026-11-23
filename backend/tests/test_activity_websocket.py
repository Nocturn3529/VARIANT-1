from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import server_http
from observability.activity import WSHub
from peers.bridge_api import register_bridge_routes


class FakeWebSocket:
    def __init__(self, token: str, events=None):
        self.query_params = {"token": token}
        self.events = list(events or [{"type": "websocket.disconnect"}])
        self.accepted = False
        self.closed = []
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def close(self, *, code):
        self.closed.append(code)

    async def send_json(self, message):
        self.sent.append(message)

    async def receive(self):
        return self.events.pop(0)


def host():
    return SimpleNamespace(
        hub=WSHub(),
        router=SimpleNamespace(engine_ready=True),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["main-deck-token", "", "invalid-\u00e9"])
async def test_activity_socket_requires_its_scoped_token(token):
    srv = host()
    websocket = FakeWebSocket(token)

    await server_http.activity_websocket_endpoint(
        srv, websocket, activity_token="presence-token"
    )

    assert websocket.accepted is False
    assert websocket.closed == [1008]
    assert not srv.hub.presence_subscribers


@pytest.mark.parametrize("host,header,expected", [
    ("127.0.0.1", "Bearer fixture", None),
    ("::1", "Bearer fixture", None),
    ("127.0.0.1", "Bearer mismatch", 401),
    ("127.0.0.1", "Bearer invalid-\u00e9", 401),
    ("127.0.0.1", "", 401),
    ("192.0.2.1", "Bearer invalid-\u00e9", 403),
])
def test_local_http_auth_rejects_bad_encoding_after_loopback_check(host, header, expected):
    request = SimpleNamespace(client=SimpleNamespace(host=host), headers={"authorization": header})
    response = server_http.require_loopback_bearer("fixture", request)
    assert getattr(response, "status_code", None) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "invalid", "invalid-\u00e9"])
async def test_command_socket_rejects_invalid_token_before_dispatch(token):
    websocket = FakeWebSocket(token)
    await server_http.websocket_endpoint(
        object(), websocket, auth_token="fixture", session_factory=lambda: pytest.fail("dispatch"),
    )
    assert websocket.closed == [1008]
    assert not websocket.accepted and not websocket.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("host,header,valid", [
    ("127.0.0.1", "Bearer fixture", True),
    ("127.0.0.1", "", False),
    ("127.0.0.1", "Bearer invalid-\u00e9", False),
    ("192.0.2.1", "Bearer fixture", False),
])
async def test_peer_bridge_checks_bearer_before_reading_request_body(host, header, valid):
    routes = {}

    def register(path):
        def bind(endpoint):
            routes[path] = endpoint
            return endpoint
        return bind

    register_bridge_routes(
        SimpleNamespace(post=register), SimpleNamespace(peer_bridge_token="fixture"),
    )
    request = SimpleNamespace(
        client=SimpleNamespace(host=host), headers={"authorization": header},
        body=AsyncMock(return_value=b"not-an-operation"),
    )
    response = await routes["/peers/bridge"](request)
    if valid:
        request.body.assert_awaited_once()
        assert response.status_code != 401
    else:
        request.body.assert_not_awaited()
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_activity_socket_is_subscribe_only_and_never_dispatches(monkeypatch):
    srv = host()
    websocket = FakeWebSocket("presence-token", [{
        "type": "websocket.receive",
        "text": '{"type":"apikey:set","key":"secret"}',
    }])

    called = False

    async def unexpected_dispatch(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(server_http.ws_dispatch, "dispatch", unexpected_dispatch)
    await server_http.activity_websocket_endpoint(
        srv, websocket, activity_token="presence-token"
    )

    assert websocket.accepted is True
    assert websocket.sent == [{"type": "hello", "engine_ready": True}]
    assert websocket.closed == [1008]
    assert called is False
    assert not srv.hub.presence_subscribers
