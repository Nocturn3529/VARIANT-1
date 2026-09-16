from __future__ import annotations

from types import SimpleNamespace

import pytest

import ws_dispatch


class _Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


def test_extension_and_mcp_websocket_families_are_registered():
    assert {
        "extension-v2:list", "extension-v2:rescan", "extension-v2:set-enabled",
        "extension-v2:package", "mcp-v2:invoke",
    } <= set(ws_dispatch.HANDLERS)


def test_retired_panel_protocols_stay_deleted():
    assert {
        "artifact:list", "artifact:create", "artifact:publish",
        "browser-fabric:open", "browser-fabric:observe", "browser-fabric:action",
        "desktop-fabric:catalog", "desktop-fabric:observe", "desktop-fabric:act",
        "review:snapshot", "review:discover", "review:start", "review:files",
    }.isdisjoint(ws_dispatch.HANDLERS)


@pytest.mark.asyncio
async def test_mcp_v2_server_command_owns_list_reconnect_and_remove():
    class Mcp:
        def __init__(self):
            self.calls = []

        def configured(self):
            return [{"server_id": "demo", "spec": {"transport": "stdio"},
                     "status": "configured"}]

        def status(self, server_id):
            return {"server_id": server_id, "status": "disconnected"}

        async def reconnect(self, server_id):
            self.calls.append(("reconnect", server_id))
            return {"server_id": server_id, "status": "connected"}

        async def remove(self, server_id):
            self.calls.append(("remove", server_id))
            return True

    mcp = Mcp()
    host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(
        extensions=SimpleNamespace(mcp=mcp),
    ))
    socket = _Socket()
    session = SimpleNamespace()
    handler = ws_dispatch.HANDLERS["mcp-v2:server"]

    await handler(host, socket, session, {
        "type": "mcp-v2:server", "request_id": "mcp-list", "action": "list",
    })
    assert socket.messages[-1]["result"] == [{
        "server_id": "demo", "spec": {"transport": "stdio"},
        "status": "disconnected",
    }]

    for action in ("reconnect", "remove"):
        await handler(host, socket, session, {
            "type": "mcp-v2:server", "request_id": f"mcp-{action}",
            "action": action, "server_id": "demo",
        })

    assert mcp.calls == [("reconnect", "demo"), ("remove", "demo")]
    assert socket.messages[-1]["result"] == {
        "server_id": "demo", "status": "removed", "removed": True,
    }
