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
