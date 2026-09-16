import asyncio

import pytest

from browser_fabric.interactive import BrowserHostBroker, BrowserHostUnavailable


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_browser_host_broker_correlates_one_targeted_result():
    broker = BrowserHostBroker()
    socket = FakeSocket()
    await broker.register(socket)

    pending = asyncio.create_task(broker.request({"action": "state"}, timeout=1))
    await asyncio.sleep(0)

    assert len(socket.sent) == 1
    envelope = socket.sent[0]
    assert envelope["type"] == "browser:host:command"
    assert envelope["command"] == {"action": "state"}
    assert broker.resolve(socket, envelope["id"], {
        "ok": True,
        "state": {"url": "https://example.com/"},
    }) is True
    assert (await pending)["state"]["url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_browser_host_broker_fails_fast_without_main_deck():
    broker = BrowserHostBroker()
    with pytest.raises(BrowserHostUnavailable, match="Open the Main Deck"):
        await broker.request({"action": "read"})


@pytest.mark.asyncio
async def test_browser_host_disconnect_fails_its_pending_commands():
    broker = BrowserHostBroker()
    socket = FakeSocket()
    await broker.register(socket)
    pending = asyncio.create_task(broker.request({"action": "read"}, timeout=1))
    await asyncio.sleep(0)

    broker.unregister(socket)

    with pytest.raises(BrowserHostUnavailable, match="disconnected"):
        await pending
