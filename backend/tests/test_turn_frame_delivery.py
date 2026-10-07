"""Display frames never decide whether a chat run survives."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import host_ports


class ClosedSocket:
    async def send_json(self, _payload):
        raise RuntimeError('Cannot call "send" once a close message has been sent.')


class Socket:
    def __init__(self):
        self.frames = []

    async def send_json(self, payload):
        self.frames.append(payload)


def _host(transports):
    runtimes = SimpleNamespace(attached_transports=lambda _chat_id: list(transports))
    return SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(session_runtimes=runtimes)
    )


def _session(chat_id="chat-a"):
    return SimpleNamespace(
        active=SimpleNamespace(runtime_chat_id=chat_id, turn_session_id=""),
        viewed_session_id="",
    )


@pytest.mark.asyncio
async def test_frame_for_an_unviewed_chat_is_dropped_without_failing_the_run():
    owner = ClosedSocket()
    await host_ports._send_turn_frame(
        _host([owner]), owner, _session(), {"type": "token", "token": "hi"}
    )


@pytest.mark.asyncio
async def test_frame_falls_through_to_another_attached_deck():
    owner, viewer = ClosedSocket(), Socket()
    await host_ports._send_turn_frame(
        _host([owner, viewer]), owner, _session(), {"type": "token", "token": "hi"}
    )
    assert viewer.frames == [{"type": "token", "token": "hi"}]


@pytest.mark.asyncio
async def test_native_display_failure_does_not_abort_the_owned_turn():
    owner = ClosedSocket()
    host = _host([owner])
    host.hub = SimpleNamespace(broadcast=AsyncMock(side_effect=RuntimeError('observer unavailable')))
    await host_ports._send_turn_frame(host, owner, _session(), {'type':'token','token':'hi'})
    host.hub.broadcast.assert_awaited_once_with({'type':'token','token':'hi','session_id':'chat-a'})


@pytest.mark.asyncio
async def test_native_tokens_never_wait_for_slow_deck_and_terminal_frames_keep_order():
    import asyncio
    from host_chat_service import NativeChatEventTransport
    from observability.activity import WSHub
    gate = asyncio.Event()
    class SlowSocket(Socket):
        async def send_json(self, payload):
            await gate.wait()
            await super().send_json(payload)
    socket = SlowSocket(); hub = WSHub(send_timeout_s=0.01)
    hub.add(socket)
    transport = NativeChatEventTransport(SimpleNamespace(hub=hub), 'chat-a')
    async def produce():
        for n in range(30):
            await transport.send_json({'type':'token','token':str(n)})
        await transport.send_json({'type':'thinking','text':'Progress'})
    await asyncio.wait_for(produce(), timeout=0.1)
    assert not socket.frames
    await transport.send_json({'type':'done'})
    await hub.broadcast({'type':'run:settled'})
    gate.set()
    await asyncio.wait_for(hub._drainers[socket], timeout=1)
    assert [row['token'] for row in socket.frames[:30]] == [str(n) for n in range(30)]
    assert [row['type'] for row in socket.frames[-3:]] == ['thinking','done','run:settled']
    hub.remove(socket)
