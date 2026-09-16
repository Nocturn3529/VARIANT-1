from __future__ import annotations

from types import SimpleNamespace

import pytest

import ws_dispatch


class _WebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


class _KernelManager:
    def __init__(self):
        self.calls = []

    def status(self, chat_id):
        return {"state": "ready", "generation": 3, "pid": 42, "chat_id": chat_id}

    def execution_history(self, chat_id, *, after_sequence=0, limit=100):
        return {
            "schema": "variant1.kernel-cell-ledger.v1",
            "runtime_chat_id": chat_id,
            "items": [{"sequence": after_sequence + 1, "execution_id": "cell-a"}],
            "next_sequence": after_sequence + 1,
            "limit": limit,
        }

    async def bounded_namespace_view(self, chat_id, *, limit=100):
        return {
            "schema": "variant1.kernel-namespace-view.v1",
            "runtime_chat_id": chat_id,
            "values": [{"name": "answer", "type": "int"}],
            "excluded": [],
            "limit": limit,
        }

    def export_notebook(self, chat_id, *, after_sequence=0, limit=200):
        return {"runtime_chat_id": chat_id, "artifact_ref": "artifact://sha256/" + "a" * 64}

    async def interrupt(self, chat_id, *, intent="stop"):
        self.calls.append(("interrupt", chat_id, intent))
        return {"status": "requested", "runtime_chat_id": chat_id}

    async def restart(self, chat_id, *, reason=""):
        self.calls.append(("restart", chat_id, reason))
        return {"status": "closed", "runtime_chat_id": chat_id}

def _stack():
    manager = _KernelManager()
    composed = SimpleNamespace(
        kernel=manager,
        sessions=SimpleNamespace(get_active=lambda: "fallback-chat"),
    )
    server = SimpleNamespace(
        require_runtime=lambda: composed,
        _test_runtime=composed,
    )
    session = SimpleNamespace(
        active=None,
        viewed_session_id="chat-a",
    )
    return server, session, manager


@pytest.mark.asyncio
async def test_kernel_snapshot_and_namespace_are_chat_scoped_and_correlated():
    server, session, _manager = _stack()
    websocket = _WebSocket()
    await ws_dispatch.HANDLERS["kernel:get"](
        server,
        websocket,
        session,
        {"type": "kernel:get", "request_id": "req-get", "after_sequence": 8},
    )
    await ws_dispatch.HANDLERS["kernel:namespace"](
        server,
        websocket,
        session,
        {"type": "kernel:namespace", "request_id": "req-ns"},
    )
    assert websocket.messages[0]["request_id"] == "req-get"
    assert websocket.messages[0]["runtime_chat_id"] == "chat-a"
    assert websocket.messages[0]["cursor"] == 9
    assert websocket.messages[1]["request_id"] == "req-ns"
    assert websocket.messages[1]["values"][0]["name"] == "answer"


@pytest.mark.asyncio
async def test_kernel_mutations_require_request_id_and_echo_operation():
    server, session, manager = _stack()
    websocket = _WebSocket()
    await ws_dispatch.HANDLERS["kernel:restart"](
        server,
        websocket,
        session,
        {"type": "kernel:restart", "request_id": "req-restart", "reason": "test"},
    )
    await ws_dispatch.HANDLERS["kernel:interrupt"](
        server,
        websocket,
        session,
        {"type": "kernel:interrupt"},
    )
    assert websocket.messages[0]["type"] == "kernel:accepted"
    assert websocket.messages[0]["operation"] == "restart"
    assert websocket.messages[1]["type"] == "kernel:rejected"
    assert manager.calls[0] == ("restart", "chat-a", "operator_restart")
    assert websocket.messages[0]["result"]["requested_reason"] == "test"


@pytest.mark.asyncio
async def test_direct_kernel_interrupt_is_terminal_stop_intent():
    server, session, manager = _stack()
    websocket = _WebSocket()
    await ws_dispatch.HANDLERS["kernel:interrupt"](
        server,
        websocket,
        session,
        {"type": "kernel:interrupt", "request_id": "req-stop"},
    )
    assert websocket.messages[0]["type"] == "kernel:accepted"
    assert websocket.messages[0]["operation"] == "interrupt"
    assert manager.calls == [("interrupt", "chat-a", "stop")]


def test_kernel_websocket_family_is_registered():
    expected = {
        "kernel:get",
        "kernel:history",
        "kernel:namespace",
        "kernel:notebook:export",
        "kernel:interrupt",
        "kernel:restart",
    }
    assert expected <= set(ws_dispatch.HANDLERS)
