from __future__ import annotations

from types import SimpleNamespace

import pytest

import server_http
from observability.activity import WSHub


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
async def test_activity_socket_requires_its_scoped_token():
    srv = host()
    websocket = FakeWebSocket("main-deck-token")

    await server_http.activity_websocket_endpoint(
        srv, websocket, activity_token="presence-token"
    )

    assert websocket.accepted is False
    assert websocket.closed == [1008]
    assert not srv.hub.presence_subscribers


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
