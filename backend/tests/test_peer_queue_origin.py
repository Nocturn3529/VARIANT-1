"""Queued peer messages say who sent them and reach a busy chat at its next step."""

from __future__ import annotations

import asyncio

import pytest

from peers import PeerError
from tests.test_peers import _settle_service, _stack


@pytest.mark.asyncio
async def test_queue_snapshot_carries_the_peer_origin_of_a_waiting_message(tmp_path):
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    admission = runtimes.try_reserve_run(second, attachment_id="")
    running = asyncio.create_task(asyncio.Event().wait())
    runtimes.bind_admission_task(admission, running)
    try:
        message = await service.send(
            f"chat:{first}", f"chat:{second}", "Review the parser", request_id="send-1",
        )
        items = runtimes.queue_snapshot(second)["items"]
        assert [item.get("origin") for item in items] == [{
            "kind": "peer",
            "peer_id": f"chat:{first}",
            "message_id": message["message_id"],
            "display_name": "First",
            "content": "Review the parser",
        }]
        # The same body transcript rows show as peer_display.content.
        assert service.message_display(items[0]["origin"])["content"] == "Review the parser"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        runtimes.finish_run(admission, status="test")
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_a_request_reaches_a_busy_chat_at_its_next_step(tmp_path):
    """Owner decision 2026-10-07: peers steer each other by default."""
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    admission = runtimes.try_reserve_run(second, attachment_id="")
    running = asyncio.create_task(asyncio.Event().wait())
    runtimes.bind_admission_task(admission, running)
    try:
        steer = await service.send(
            f"chat:{first}", f"chat:{second}", "Check the parser now", request_id="r-1",
        )
        later = await service.send(
            f"chat:{first}", f"chat:{second}", "After your turn", request_id="r-2",
            delivery="follow_up",
        )
        tickets = {
            row["ticket_id"]: row["delivery"] for row in runtimes.queue_snapshot(second)["items"]
        }
        assert tickets == {
            steer["delivery_ticket_id"]: "steer",
            later["delivery_ticket_id"]: "follow_up",
        }
        # The running loop drains steer tickets at each step boundary.
        claimed = runtimes.claim_input(second, "steer", run_id=admission)
        assert claimed is not None and "Check the parser now" in claimed["text"]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        runtimes.finish_run(admission, status="test")
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_retrying_a_committed_follow_up_without_delivery_keeps_its_mode(tmp_path):
    """A lost acknowledgement from before the steer default still reconciles."""
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    try:
        original = await service.send(
            f"chat:{first}", f"chat:{second}", "Old request", request_id="old-1",
            delivery="follow_up",
        )
        retried = await service.send(
            f"chat:{first}", f"chat:{second}", "Old request", request_id="old-1",
        )
        assert retried["message_id"] == original["message_id"]
        assert retried["delivery"] == "follow_up"
        with pytest.raises(PeerError) as conflict:
            await service.send(
                f"chat:{first}", f"chat:{second}", "Old request", request_id="old-1",
                delivery="steer",
            )
        assert conflict.value.code == "peer_request_conflict"
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_websocket_send_keeps_an_omitted_delivery_omitted(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import ws_peers

    service, runtimes, sessions, _chat, first, second = _stack(tmp_path)
    handlers = {}

    def on(*names):
        def register(handler):
            for name in names:
                handlers[name] = handler
            return handler
        return register

    ws_peers.register(on)
    host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(
        sessions=sessions, peers=service,
    ))
    websocket = SimpleNamespace(send_json=AsyncMock())
    try:
        await service.send(
            f"chat:{first}", f"chat:{second}", "Old request", request_id="ws-1",
            delivery="follow_up",
        )
        await handlers["peers:send"](host, websocket, None, {
            "type": "peers:send", "chat_id": first, "request_id": "ws-1",
            "peer_id": f"chat:{second}", "text": "Old request",
        })
        reply = websocket.send_json.await_args.args[0]
        assert reply["ok"] is True, reply
        assert reply["result"]["delivery"] == "follow_up"
    finally:
        await _settle_service(service, runtimes)
