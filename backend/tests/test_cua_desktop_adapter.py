"""cua-driver adapter maps windows and actions without a live desktop."""

from __future__ import annotations

import sys

import pytest

from desktop_fabric.adapter import AdapterFocus
from desktop_fabric.cua_adapter import (
    CuaDesktopAdapter,
    linux_started_at_from_stat,
    parse_darwin_lstart,
    select_non_windows_adapter,
)
from desktop_fabric.cua_client import CuaDriverClient, unwrap_tool_result
from desktop_fabric.models import DesktopElement, WindowRecord
from desktop_fabric.unsupported import UnsupportedDesktopAdapter


class _ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _adapter(responses, started=100.0):
    client = _ScriptedClient(responses)
    adapter = CuaDesktopAdapter(
        client=client, platform="linux", started_at=lambda _pid: started,
    )
    return adapter, client


def test_linux_and_darwin_start_time_parsers():
    stat = "42 (my proc) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 250 0 0"
    assert linux_started_at_from_stat(stat, btime=1_000, clk_tck=100) == 1002.5
    assert parse_darwin_lstart("Wed Sep 23 12:00:00 2026") > 0
    assert parse_darwin_lstart("") == 0


def test_catalog_keeps_only_windows_with_pid_window_id_and_start_time():
    adapter, _client = _adapter([{
        "windows": [
            {"pid": 50, "window_id": 7, "title": "Notes", "x": 10, "y": 20,
             "width": 30, "height": 40, "executable": "/bin/notes"},
            {"title": "no identity"},
            {"pid": 51, "window_id": 8, "title": "dead"},
        ],
    }], started=100.0)
    adapter._started_at = lambda pid: 100.0 if pid == 50 else 0.0
    _apps, windows = adapter.catalog(backend_instance_id="backend")
    assert len(windows) == 1
    window = windows[0]
    assert window.pid == 50
    assert window.hwnd == 7
    assert window.window_id == "cua:50:7"
    assert window.pid_started_at == 100.0
    assert window.bounds == (10, 20, 40, 60)


def test_validate_window_rejects_recycled_pid_and_missing_window():
    adapter, _client = _adapter([{"windows": []}])
    window = WindowRecord(
        window_id="cua:50:7", app_id="cua-app:50", hwnd=7, pid=50,
        pid_started_at=100.0,
    )
    adapter._started_at = lambda _pid: 400.0
    assert adapter.validate_window(window)["reason"] == "process_start_changed"
    adapter._started_at = lambda _pid: 100.0
    assert adapter.validate_window(window)["reason"] == "window_missing"


@pytest.mark.asyncio
async def test_observe_and_click_use_driver_token_and_window_local_point():
    adapter, client = _adapter([
        {"elements": [{
            "element_token": "s1:4",
            "role": "button",
            "label": "Save",
            "actions": ["press"],
            "frame": {"x": 1, "y": 2, "width": 3, "height": 4},
        }]},
        {"effect": "confirmed", "route": "accessibility"},
        {"effect": "refused", "route": "background_unavailable"},
    ])
    window = WindowRecord(
        window_id="cua:50:7", app_id="cua-app:50", hwnd=7, pid=50,
        pid_started_at=100.0, bounds=(100, 200, 500, 700),
    )
    observation = await adapter.observe(window, mode="fused")
    assert observation.uia[0]["backend_key"] == "s1:4"
    assert "invoke" in observation.uia[0]["patterns"]
    element = DesktopElement(
        element_ref="el", observation_id="obs", window_id=window.window_id,
        window_generation=1, element_generation=1, role="button", name="Save",
        backend_key="s1:4",
    )
    sent = await adapter.dispatch(
        window, action="click", delivery="semantic", element=element, arguments={},
    )
    assert sent.delivered is True
    assert client.calls[1][0] == "click"
    assert client.calls[1][1]["element_token"] == "s1:4"
    assert client.calls[1][1]["target"] == {"kind": "window", "pid": 50, "window_id": 7}
    refused = await adapter.dispatch(
        window, action="click", delivery="physical", element=None,
        arguments={"x": 130, "y": 240, "button": "left"},
    )
    assert refused.delivered is False
    point = client.calls[2][1]
    assert point["x"] == 30
    assert point["y"] == 40


@pytest.mark.asyncio
async def test_capture_refuses_unproven_screenshot():
    adapter, _client = _adapter([{
        "screenshot_error": {"code": "surface_identity_unproven"},
        "screenshot_base64": "aGk=",
    }])
    window = WindowRecord(
        window_id="cua:50:7", app_id="cua-app:50", hwnd=7, pid=50,
        pid_started_at=100.0, bounds=(0, 0, 10, 10),
    )
    with pytest.raises(Exception, match="prove this screenshot"):
        await adapter.capture(window)


@pytest.mark.asyncio
async def test_focus_returns_the_native_window_id():
    adapter, _client = _adapter([{"elements": []}])
    window = WindowRecord(
        window_id="cua:50:7", app_id="cua-app:50", hwnd=7, pid=50,
        pid_started_at=100.0,
    )
    focused = await adapter.focus(window)
    assert isinstance(focused, AdapterFocus)
    assert focused.hwnd == 7


def test_missing_driver_stays_unsupported(monkeypatch):
    monkeypatch.setattr(
        "desktop_fabric.cua_adapter.resolve_cua_driver_command", lambda: None,
    )
    adapter = select_non_windows_adapter("linux")
    assert isinstance(adapter, UnsupportedDesktopAdapter)
    adapter = select_non_windows_adapter("darwin")
    assert isinstance(adapter, UnsupportedDesktopAdapter)


def test_stdio_client_speaks_newline_delimited_mcp():
    script = r"""
import json, sys
def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode())
def write_message(payload):
    sys.stdout.buffer.write(json.dumps(payload).encode() + b"\n")
    sys.stdout.buffer.flush()
while True:
    message = read_message()
    if not message:
        break
    if message.get("method") == "initialize":
        write_message({"jsonrpc":"2.0","id":message["id"],"result":{"protocolVersion":"2025-06-18"}})
    elif message.get("method") == "notifications/initialized":
        continue
    elif message.get("method") == "tools/call":
        write_message({"jsonrpc":"2.0","id":message["id"],"result":{
            "structuredContent":{"windows":[{"pid":4,"window_id":9,"title":"Calc"}]}
        }})
        break
"""
    client = CuaDriverClient([sys.executable, "-c", script], timeout_s=5)
    client.open()
    try:
        result = client.call_tool("list_windows", {"session": "variant1"})
    finally:
        client.close()
    assert result["windows"][0]["title"] == "Calc"


def test_unwrap_prefers_structured_content():
    assert unwrap_tool_result({
        "structuredContent": {"ok": True},
        "content": [{"type": "text", "text": "{\"ok\": false}"}],
    }) == {"ok": True}
