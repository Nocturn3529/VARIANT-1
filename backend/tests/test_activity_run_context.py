"""Activity stream identity comes only from Variant1RunContext (#14)."""

from __future__ import annotations

import asyncio

import pytest

from observability.activity import (
    HUB,
    WSHub,
    emit_activity,
    new_run,
    presence_projection,
)
from run_context import Variant1RunContext, bind_run_context, clear_run_context


@pytest.fixture(autouse=True)
def _clear_ctx():
    clear_run_context()
    yield
    clear_run_context()


@pytest.mark.asyncio
async def test_emit_activity_uses_bound_run_context():
    seen = []

    async def capture(msg):
        seen.append(msg)

    prev = HUB.broadcast
    HUB.broadcast = capture  # type: ignore[method-assign]
    try:
        ctx = Variant1RunContext.create(source="chat", title="t", run_id="run-abc")
        with bind_run_context(ctx):
            bag = new_run("chat", "Hello")
            assert bag["id"] == "run-abc"
            assert ctx.metadata.get("_activity_run") is bag
            await emit_activity("note", text="hi")
            await emit_activity("tool:start", tool="read_file", call_id="call-1")
            await emit_activity("tool:result", tool="read_file", call_id="call-1")
            assert ctx.metadata["tools_used"] == ["read_file"]
        assert seen and seen[0]["run_id"] == "run-abc"
        assert seen[0]["source"] == "chat"
        assert seen[0]["text"] == "hi"
    finally:
        HUB.broadcast = prev  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_emit_activity_without_context_has_no_run_id_fallback():
    """No process-global _CURRENT_RUN — untagged emit lacks run_id."""
    seen = []

    async def capture(msg):
        seen.append(msg)

    prev = HUB.broadcast
    HUB.broadcast = capture  # type: ignore[method-assign]
    try:
        # Even if someone called new_run without a context, identity is not ambient.
        new_run("orphan", "x")
        await emit_activity("note", text="untagged")
        assert seen
        assert "run_id" not in seen[0]
        assert seen[0]["text"] == "untagged"
    finally:
        HUB.broadcast = prev  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_hub_slow_socket_does_not_block_healthy_clients_or_caller():
    class SlowSocket:
        def __init__(self):
            self.cancelled = False

        async def send_json(self, _message):
            try:
                await asyncio.Future()
            finally:
                self.cancelled = True

    class FastSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    hub = WSHub(send_timeout_s=0.05)
    slow = SlowSocket()
    fast = FastSocket()
    hub.add(slow)
    hub.add(fast)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await hub.broadcast({"type": "test"})
    elapsed = loop.time() - started
    await asyncio.sleep(0)

    assert elapsed < 0.5
    assert fast.messages == [{"type": "test"}]
    assert fast in hub.active
    assert slow not in hub.active
    assert slow.cancelled is True


@pytest.mark.asyncio
async def test_presence_subscriber_gets_only_minimised_allowed_messages():
    class Socket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    hub = WSHub()
    full = Socket()
    presence = Socket()
    hub.add(full)
    hub.add_presence_subscriber(presence)

    sensitive_activity = {
        "type": "activity",
        "event": "task:start",
        "run_id": "run-1",
        "source": "chat",
        "mood": "focused",
        "text": "private prompt text",
        "args_preview": "secret arguments",
        "session_id": "private-session",
    }
    await hub.broadcast(sensitive_activity)
    await hub.broadcast({"type": "config", "api_key": "must-not-cross"})
    await hub.broadcast({
        "type": "proactive", "mood": "happy", "text": "private inbox text"
    })

    assert full.messages == [
        sensitive_activity,
        {"type": "config", "api_key": "must-not-cross"},
        {"type": "proactive", "mood": "happy", "text": "private inbox text"},
    ]
    assert presence.messages == [
        {
            "type": "activity",
            "event": "task:start",
            "run_id": "run-1",
            "source": "chat",
            "mood": "focused",
        },
        {"type": "proactive", "mood": "happy"},
    ]
    assert presence_projection({"type": "chat:token", "token": "secret"}) is None
