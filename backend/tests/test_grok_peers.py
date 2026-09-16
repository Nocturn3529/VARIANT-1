"""Peer transport contracts: identity, durable writes, lifecycle and ACP framing."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from peers.grok_acp import ACPError, GrokACP
from tests.test_peers import _stack, _settle_service
import ws_peers


def _handler():
    handlers = {}
    def on(*names):
        def register(handler):
            handlers.update({name: handler for name in names})
            return handler
        return register
    ws_peers.register(on)
    return handlers["peers:send"]


@pytest.mark.asyncio
async def test_ui_lost_ack_same_id_reconciles_exactly_once(tmp_path):
    service, runtimes, sessions, _chat, first, second = _stack(tmp_path)
    host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(sessions=sessions, peers=service))
    request = {"type": "peers:send", "chat_id": first, "peer_id": f"chat:{second}",
               "request_id": "lost-ack", "text": "One request", "sender_peer_id": "forged"}
    socket = SimpleNamespace(send_json=AsyncMock(side_effect=ConnectionError("closed")))
    try:
        with pytest.raises(ConnectionError):
            await _handler()(host, socket, None, request)
        assert socket.send_json.await_count == 1
        socket.send_json = AsyncMock()
        await _handler()(host, socket, None, request)
        reply = socket.send_json.call_args.args[0]
        assert reply["ok"] is True
        assert reply["result"]["sender_peer_id"] == f"chat:{first}"
        rows = service.inbox(f"chat:{second}")["messages"]
        assert len(rows) == 1
        assert rows[0]["message_id"] == reply["result"]["message_id"]
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_ui_permission_post_entry_failure_is_uncertain():
    grok = SimpleNamespace(answer_permission=Mock(side_effect=RuntimeError("ack lost")))
    host = SimpleNamespace(grok_peer_integration=grok, require_runtime=lambda: SimpleNamespace(
        sessions=SimpleNamespace(has_session=lambda _: True), peers=object()))
    socket = SimpleNamespace(send_json=AsyncMock())
    await _handler()(host, socket, None, {"type": "peers:grok:permission", "chat_id": "a",
        "request_id": "r", "binding_id": "b", "permission_id": "p", "option_id": "once"})
    assert socket.send_json.call_args.args[0]["error"]["commit_state"] == "unknown"


class ProtocolProcess:
    def __init__(self, state="written"):
        self.receipt_state = state
        self.frames = []
        self.writes = []
        self.live = True

    def write(self, _process_id, raw):
        self.writes.append(json.loads(raw))
        return {"write_id": "input-1", "state": "queued"}

    def input_status(self, _process_id):
        return [{"write_id": "input-1", "state": self.receipt_state}]

    def logs(self, _process_id, after_cursor, **_kwargs):
        frames = self.frames[after_cursor:]
        return SimpleNamespace(to_dict=lambda: {"frames": frames, "next_cursor": len(self.frames)})

    def get(self, _process_id):
        return self

    def output(self, raw):
        self.frames.append({"stream": "stdout", "data_base64": base64.b64encode(raw).decode()})


@pytest.mark.asyncio
async def test_acp_utf8_fragmentation_and_actual_write_before_ack():
    process = ProtocolProcess(state="writing")
    client = GrokACP(SimpleNamespace(processes=process), "p")
    written = AsyncMock()
    task = asyncio.create_task(client.request("session/new", {}, timeout=2, on_written=written))
    await asyncio.sleep(.03)
    identity = process.writes[0]["id"]
    raw = (json.dumps({"jsonrpc": "2.0", "id": identity, "result": {"text": "Café 松"}}, ensure_ascii=False) + "\n").encode()
    split = raw.index("松".encode()) + 1
    process.output(raw[:split])
    await asyncio.sleep(.1)
    process.output(raw[split:])
    await asyncio.sleep(.1)
    assert not task.done()
    written.assert_not_awaited()
    process.receipt_state = "written"
    assert await task == {"text": "Café 松"}
    written.assert_awaited_once()
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["partial", "cancelled", "unknown_effect"])
async def test_acp_uncertain_input_never_claims_complete_delivery(state):
    process = ProtocolProcess(state=state)
    client = GrokACP(SimpleNamespace(processes=process), "p")
    written = AsyncMock()
    with pytest.raises(ACPError, match="not completely written"):
        await client.request("session/prompt", {}, on_written=written)
    written.assert_not_awaited()
    await client.close()


@pytest.mark.asyncio
async def test_unlimited_model_wait_still_bounds_os_write_wait():
    process = ProtocolProcess(state="writing")
    client = GrokACP(SimpleNamespace(processes=process), "p")
    with pytest.raises(ACPError, match="input delivery timed out"):
        await client.request("session/prompt", {}, timeout=None, write_timeout=.03)
    await client.close()


@pytest.mark.asyncio
async def test_acp_disconnect_cancels_pending_permission_handler():
    process = ProtocolProcess()
    request_started = asyncio.Event()
    request_finished = asyncio.Event()
    async def permission(_message):
        request_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_finished.set()
    client = GrokACP(SimpleNamespace(processes=process), "p", on_request=permission)
    client.start()
    process.output((json.dumps({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission"}) + "\n").encode())
    await asyncio.wait_for(request_started.wait(), 1)
    process.live = False
    await asyncio.wait_for(request_finished.wait(), 1)
    assert client.closed
    await client.close()
