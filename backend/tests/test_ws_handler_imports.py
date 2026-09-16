"""Ensure Deck-critical WS handlers do not reference missing module globals.

Regression for the model:list NameError (os.path.basename without import os)
that closed every Main Deck WebSocket after the onOpen model:list burst.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

import ws_automations
import ws_config
import ws_dispatch
import ws_surface

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _module_uses_name(path: pathlib.Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == name:
            return True
    return False


def _module_imports_name(path: pathlib.Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == name or alias.name.split(".")[0] == name:
                    return True
        if isinstance(node, ast.ImportFrom):
            if node.module and (node.module == name or node.module.split(".")[0] == name):
                return True
            for alias in node.names:
                if alias.name == name:
                    return True
    return False


def _asyncio_create_task_count(path: pathlib.Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "create_task"
    )


@pytest.mark.parametrize(
    "rel,names",
    [
        ("ws_config.py", ("os",)),
        ("ws_automations.py", ("asyncio",)),
        ("ws_surface.py", ("asyncio",)),
    ],
)
def test_handler_modules_import_names_they_use(rel, names):
    path = ROOT / rel
    assert path.is_file(), path
    for name in names:
        if _module_uses_name(path, name):
            assert _module_imports_name(path, name), (
                f"{rel} references {name!r} but does not import it "
                f"(will NameError at runtime and can kill the WebSocket)"
            )


def test_model_list_handler_registered_and_callable():
    fn = ws_dispatch.HANDLERS.get("model:list")
    assert fn is not None
    assert callable(fn)


@pytest.mark.asyncio
async def test_model_list_failure_returns_owned_error(monkeypatch):
    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    monkeypatch.setattr(
        ws_config, "_local_models_msg",
        lambda _srv: (_ for _ in ()).throw(OSError("scan failed")),
    )
    socket = Socket()
    await ws_dispatch.HANDLERS["model:list"](object(), socket, object(), {})
    assert socket.sent == [{"type": "models:error", "error": "scan failed"}]


@pytest.mark.asyncio
async def test_cloud_credential_handler_requires_an_acceptance_receipt():
    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    class Router:
        cloud_provider = "openai"

        @staticmethod
        def replace_cloud_credential(_provider, secret):
            if secret == "reject":
                raise ValueError("invalid key")

    class Hub:
        async def broadcast(self, _value):
            return None

    srv = SimpleNamespace(
        router=Router(),
        hub=Hub(),
        require_runtime=lambda: SimpleNamespace(
            models=SimpleNamespace(config_status=lambda: {}),
        ),
        engine_status_message=lambda: {},
    )
    socket = Socket()
    await ws_dispatch.HANDLERS["cloud:credential:set"](
        srv, socket, object(),
        {"provider": "openai", "key": "ok", "request_id": "save-1"},
    )
    await ws_dispatch.HANDLERS["cloud:credential:set"](
        srv, socket, object(),
        {"provider": "openai", "key": "reject", "request_id": "save-2"},
    )
    assert socket.sent[0]["type"] == "cloud:credential:accepted"
    assert socket.sent[0]["request_id"] == "save-1"
    assert socket.sent[1] == {
        "type": "cloud:credential:rejected",
        "request_id": "save-2",
        "provider": "openai",
        "operation": "set",
        "error": "invalid key",
    }


@pytest.mark.asyncio
async def test_endpoint_operations_echo_owned_receipts():
    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    endpoint = {
        "id": "custom-rack",
        "name": "Rack",
        "base_url": "http://127.0.0.1:9900/v1",
        "model": "rack-model",
        "is_current": True,
    }

    class Router:
        mode = "local"

        @staticmethod
        async def validate_custom_endpoint(_value):
            return {"ok": True, "reachable": True, "models": ["rack-model"],
                    "message": "ready"}

        @staticmethod
        def save_custom_endpoint(_value):
            return endpoint

        @staticmethod
        def activate_custom_endpoint(endpoint_id):
            assert endpoint_id == endpoint["id"]
            return endpoint

        @staticmethod
        def list_custom_endpoints():
            return [endpoint]

        @staticmethod
        def remove_custom_endpoint(endpoint_id):
            assert endpoint_id == endpoint["id"]
            return True

    class Hub:
        def __init__(self):
            self.sent = []

        async def broadcast(self, value):
            self.sent.append(value)

    router = Router()
    hub = Hub()
    srv = SimpleNamespace(
        router=router,
        hub=hub,
        require_runtime=lambda: SimpleNamespace(
            models=SimpleNamespace(config_status=lambda: {"type": "config"}),
        ),
        engine_status_message=lambda: {"type": "engine"},
    )
    socket = Socket()

    await ws_dispatch.HANDLERS["cloud:custom-endpoint:validate"](
        srv, socket, object(),
        {"endpoint": endpoint, "request_id": "validate-1"},
    )
    await ws_dispatch.HANDLERS["cloud:custom-endpoint:save"](
        srv, socket, object(),
        {"endpoint": endpoint, "request_id": "save-1"},
    )
    await ws_dispatch.HANDLERS["cloud:custom-endpoint:activate"](
        srv, socket, object(),
        {"id": endpoint["id"], "request_id": "activate-1"},
    )
    await ws_dispatch.HANDLERS["cloud:custom-endpoint:remove"](
        srv, socket, object(),
        {"id": endpoint["id"], "request_id": "remove-1"},
    )

    receipts = {row["request_id"]: row for row in socket.sent}
    assert receipts["validate-1"]["operation"] == "validate"
    assert receipts["save-1"]["operation"] == "save"
    assert receipts["activate-1"]["operation"] == "activate"
    assert receipts["remove-1"] == {
        "type": "cloud:custom-endpoint:removed",
        "request_id": "remove-1",
        "operation": "remove",
        "removed": True,
        "id": "custom-rack",
        "fallback_mode": "local",
    }


@pytest.mark.asyncio
async def test_correlated_voice_options_wait_for_backend_settlement():
    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    class Hub:
        async def broadcast(self, _value):
            return None

    class Voice:
        fail = False

        def set_config(self, _key, _value):
            if self.fail:
                raise ValueError("invalid speech option")

    voice = Voice()
    srv = SimpleNamespace(
        hub=Hub(),
        require_runtime=lambda: SimpleNamespace(voice=voice),
        engine_status_message=lambda: {"type": "engine"},
    )
    socket = Socket()

    await ws_dispatch.HANDLERS["tts:set"](
        srv, socket, object(),
        {"key": "tts_options", "value": {}, "request_id": "voice-1"},
    )
    voice.fail = True
    await ws_dispatch.HANDLERS["tts:set"](
        srv, socket, object(),
        {"key": "tts_options", "value": {}, "request_id": "voice-2"},
    )

    assert socket.sent == [
        {"type": "speech:accepted", "request_id": "voice-1"},
        {"type": "speech:rejected", "request_id": "voice-2",
         "error": "invalid speech option"},
    ]


@pytest.mark.asyncio
async def test_failed_first_model_switch_never_promotes_rejected_path():
    class Models:
        @staticmethod
        async def restart_engine(_path, _mmproj, *, should_apply):
            assert should_apply()
            return "failed"

        @staticmethod
        def scan_models():
            return []

    class Hub:
        def __init__(self):
            self.sent = []

        async def broadcast(self, value):
            self.sent.append(value)

    srv = SimpleNamespace(
        router=SimpleNamespace(cfg={"local": {"model": ""}}, engine_ready=False),
        hub=Hub(),
        data_dir="C:/variant-data",
        _local_model_switch_generation=1,
        require_runtime=lambda: SimpleNamespace(models=Models()),
    )

    await ws_config._switch_local_model(
        srv, "C:/broken.gguf", "", 1,
    )

    assert srv.hub.sent[0]["type"] == "models"
    assert srv.hub.sent[0]["current"] == ""
    assert srv.hub.sent[1]["type"] == "models:error"
    assert srv.hub.sent[1]["operation"] == "switch"
    assert srv.hub.sent[1]["attempted"] == "C:/broken.gguf"
    assert srv.hub.sent[1]["current"] == ""


def test_detached_ws_work_uses_retained_background_registry():
    assert _asyncio_create_task_count(ROOT / "ws_automations.py") == 0
    assert _asyncio_create_task_count(ROOT / "ws_surface.py") == 0
    # STT and TTS preview each own one explicit request-scoped task stored on
    # ConnectionSession so a newer request, Stop, or disconnect can cancel it.
    # Engine restarts use background_tasks.spawn.
    assert _asyncio_create_task_count(ROOT / "ws_config.py") == 2


def test_platform_credential_handlers_registered():
    for name in ("cloud:credential:set", "cloud:credential:clear", "cloud:credential:list",
                 "cloud:credential:add", "cloud:credential:remove", "cloud:credential:enable",
                 "cloud:credential:priority:set", "cloud:credential:strategy:set"):
        assert name in ws_dispatch.HANDLERS, name
    assert {
        "cloud:fallbacks:set",
        "cloud:provider:options:set",
    }.isdisjoint(ws_dispatch.HANDLERS)


def test_messaging_credential_handlers_registered():
    assert "messaging:credential:set" in ws_dispatch.HANDLERS
    assert "messaging:credential:clear" in ws_dispatch.HANDLERS


def test_dispatch_swallows_handler_exceptions_without_leaking_details(capsys):
    """A broken handler must not raise out of dispatch (would drop the socket)."""
    import asyncio

    sensitive_detail = "database password=do-not-send-to-client"

    async def boom(srv, websocket, session, msg):
        raise RuntimeError(sensitive_detail)

    class WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    original = ws_dispatch.HANDLERS.get("ping")
    ws_dispatch.HANDLERS["ping"] = boom
    try:
        ws = WS()
        server_logs = []
        srv = type("Srv", (), {"log": server_logs.append})()
        ok = asyncio.run(ws_dispatch.dispatch(srv, ws, object(), {"type": "ping"}))
        assert ok is True
        assert ws.sent == [{"type": "error", "error": "handler_failed:ping"}]
        assert sensitive_detail not in str(ws.sent)
        assert sensitive_detail in capsys.readouterr().out
        assert any(sensitive_detail in line for line in server_logs)
    finally:
        if original is not None:
            ws_dispatch.HANDLERS["ping"] = original
        else:
            ws_dispatch.HANDLERS.pop("ping", None)


@pytest.mark.parametrize(
    "payload,error",
    [
        ([], "invalid_envelope:expected_object"),
        (None, "invalid_envelope:expected_object"),
        ("chat", "invalid_envelope:expected_object"),
        ({"type": []}, "invalid_envelope:type_must_be_nonempty_string"),
        ({}, "invalid_envelope:type_must_be_nonempty_string"),
        ({"type": ""}, "invalid_envelope:type_must_be_nonempty_string"),
        ({"type": "   "}, "invalid_envelope:type_must_be_nonempty_string"),
    ],
)
def test_dispatch_rejects_invalid_envelopes(payload, error):
    """Malformed JSON values get a stable error without dropping the socket."""
    import asyncio

    class WS:
        def __init__(self):
            self.sent = []

        async def send_json(self, value):
            self.sent.append(value)

    ws = WS()
    ok = asyncio.run(ws_dispatch.dispatch(object(), ws, object(), payload))
    assert ok is True
    assert ws.sent == [{"type": "error", "error": error}]
