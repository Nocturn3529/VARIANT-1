"""Queued peer messages say who sent them; a peer cannot message itself."""

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
        }]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        runtimes.finish_run(admission, status="test")
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_a_peer_cannot_send_a_message_to_itself(tmp_path):
    service, runtimes, _sessions, _chat, first, _second = _stack(tmp_path)
    try:
        with pytest.raises(PeerError) as refused:
            await service.send(f"chat:{first}", f"chat:{first}", "Note to self")
        assert refused.value.code == "peer_self_send"
        assert runtimes.queue_snapshot(first)["items"] == []
    finally:
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
