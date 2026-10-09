"""cua-driver adapter maps windows and actions without a live desktop."""

from __future__ import annotations

import base64
import os
import sys

import pytest

from desktop_fabric.adapter import AdapterFocus
from desktop_fabric.cua_adapter import (
    CuaDesktopAdapter,
    host_process_started_at,
    linux_started_at_from_stat,
    parse_darwin_lstart,
    select_cua_adapter,
)
from desktop_fabric.cua_client import (
    CuaDriverClient,
    CuaDriverError,
    cua_driver_env,
    unwrap_tool_result,
)
from desktop_fabric.driver_host import CuaDriverHost, session_label
from desktop_fabric.models import DesktopElement, DesktopUnavailable, WindowRecord
from desktop_fabric.unsupported import UnsupportedDesktopAdapter


class _Died(CuaDriverError):
    """The driver process exits while answering this call."""


class _ScriptedClient:
    def __init__(self, responses, *, version=""):
        self.responses = list(responses)
        self.calls = []
        self.opened = 0
        self.closed = 0
        self.dead = False
        self.server_info = {"name": "cua-driver", "version": version} if version else {}

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def alive(self):
        return not self.dead

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        result = self.responses.pop(0) if self.responses else {}
        if isinstance(result, _Died):
            self.dead = True
        if isinstance(result, Exception):
            raise result
        return result


def _adapter(responses, started=100.0, *, run_id="", locked=False):
    client = _ScriptedClient(responses)
    adapter = CuaDesktopAdapter(
        client=client, platform="linux", started_at=lambda _pid: started,
        locked=lambda: locked, run_id=lambda: run_id,
    )
    return adapter, client


def _window(bounds=(100, 200, 500, 700)):
    return WindowRecord(
        window_id="cua:50:7", app_id="cua-app:50", hwnd=7, pid=50,
        pid_started_at=100.0, bounds=bounds,
    )


def test_linux_and_darwin_start_time_parsers():
    stat = "42 (my proc) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 250 0 0"
    assert linux_started_at_from_stat(stat, btime=1_000, clk_tck=100) == 1002.5
    assert parse_darwin_lstart("Wed Sep 23 12:00:00 2026") > 0
    assert parse_darwin_lstart("") == 0


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows process identity")
def test_windows_process_start_time_identifies_this_process():
    import psutil

    started = host_process_started_at(os.getpid())
    assert started > 0
    assert abs(started - psutil.Process(os.getpid()).create_time()) < 0.01


def test_catalog_reads_the_driver_window_rows():
    adapter, _client = _adapter([{
        "windows": [
            {"pid": 50, "window_id": 7, "title": "Notes", "app_name": "Notes",
             "bounds": {"x": 10, "y": 20, "width": 30, "height": 40}},
            {"title": "no identity"},
            {"pid": 51, "window_id": 8, "title": "dead"},
        ],
    }], started=100.0)
    adapter._started_at = lambda pid: 100.0 if pid == 50 else 0.0
    apps, windows = adapter.catalog(backend_instance_id="backend")
    assert len(windows) == 1
    window = windows[0]
    assert (window.pid, window.hwnd, window.window_id) == (50, 7, "cua:50:7")
    assert window.pid_started_at == 100.0
    assert window.bounds == (10, 20, 40, 60)
    assert window.executable == "Notes"
    assert apps[0].display_name == "Notes"


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
async def test_observe_and_click_name_the_exact_pid_and_window():
    adapter, client = _adapter([
        {"snapshot_id": "s1", "elements": [{
            "element_token": "s1:4", "role": "button", "label": "Save",
            "actions": ["press"], "frame": {"x": 1, "y": 2, "width": 3, "height": 4},
        }], "focused_element": "Save"},
        {"effect": "confirmed", "route": "accessibility"},
        {"effect": "refused", "route": "background_unavailable"},
    ])
    window = _window()
    observation = await adapter.observe(window, mode="fused")
    assert observation.uia[0]["backend_key"] == "s1:4"
    assert observation.uia[0]["bounds"] == [1, 2, 4, 6]
    assert "invoke" in observation.uia[0]["patterns"]
    assert observation.metadata["focused_element"] == "Save"
    name, request = client.calls[0]
    assert name == "get_window_state"
    assert (request["pid"], request["window_id"]) == (50, 7)
    assert request["include_screenshot"] is False
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
    assert (client.calls[1][1]["pid"], client.calls[1][1]["window_id"]) == (50, 7)
    refused = await adapter.dispatch(
        window, action="click", delivery="physical", element=None,
        arguments={"x": 130, "y": 240, "button": "left"},
    )
    assert refused.delivered is False
    assert (client.calls[2][1]["x"], client.calls[2][1]["y"]) == (30, 40)


@pytest.mark.asyncio
async def test_right_click_double_click_and_drag_use_the_driver_argument_names():
    adapter, client = _adapter([{}, {}, {}])
    window = _window()
    await adapter.dispatch(window, action="click", delivery="physical", element=None,
                           arguments={"x": 110, "y": 210, "button": "right"})
    await adapter.dispatch(window, action="click", delivery="physical", element=None,
                           arguments={"x": 110, "y": 210, "double": True})
    await adapter.dispatch(window, action="drag", delivery="physical", element=None,
                           arguments={"x1": 110, "y1": 210, "x2": 150, "y2": 260})
    (right, right_args), (double, double_args), (drag, drag_args) = client.calls
    assert (right, double, drag) == ("right_click", "double_click", "drag")
    assert right_args["pid"] == 50 and double_args["pid"] == 50
    assert (drag_args["from_x"], drag_args["from_y"], drag_args["to_x"], drag_args["to_y"]) == (10, 10, 50, 60)


@pytest.mark.asyncio
async def test_delivery_follows_the_fabric_and_retries_in_the_foreground_when_asked():
    refusal = CuaDriverError(
        "Background delivery is not available for target window class 'TkTopLevel' on this event "
        'kind (text_input). Retry this action with delivery_mode:"foreground"; Cua Driver will '
        'activate the target for the action and restore the previous foreground afterward.')
    adapter, client = _adapter([{}, refusal, {"effect": "confirmed"}, refusal])
    window = _window()
    await adapter.dispatch(window, action="type_text", delivery="physical", element=None,
                           arguments={"text": "a"})
    retried = await adapter.dispatch(window, action="type_text", delivery="auto", element=None,
                                     arguments={"text": "b"})
    assert [args["delivery_mode"] for _name, args in client.calls] == [
        "foreground", "background", "foreground"]
    assert retried.delivered and retried.metadata["delivery_mode"] == "foreground"
    # Semantic input never falls back to raising the window.
    with pytest.raises(DesktopUnavailable, match="foreground"):
        await adapter.dispatch(window, action="type_text", delivery="semantic", element=None,
                               arguments={"text": "c"})
    assert len(client.calls) == 4


@pytest.mark.asyncio
async def test_capture_reads_the_image_content_item():
    png = b"\x89PNG\r\n\x1a\nfake"
    adapter, client = _adapter([unwrap_tool_result({
        "structuredContent": {"screenshot_width": 400, "screenshot_height": 500},
        "content": [{"type": "image", "mimeType": "image/png",
                     "data": base64.b64encode(png).decode()}],
    })])
    captured = await adapter.capture(_window())
    assert captured.png == png
    assert (captured.width, captured.height) == (400, 500)
    assert captured.occlusion_independent is True
    assert captured.coordinate_transform["screen_origin"] == [100, 200]
    assert client.calls[0][1]["include_accessibility_tree"] is False
    # Native resolution: the driver reads x/y in the returned PNG's pixels.
    assert client.calls[0][1]["max_image_dimension"] == 0


@pytest.mark.asyncio
async def test_capture_refuses_unproven_screenshot():
    adapter, _client = _adapter([{
        "screenshot_error": {"code": "surface_identity_unproven"},
        "screenshot_base64": "aGk=",
    }])
    with pytest.raises(Exception, match="prove this screenshot"):
        await adapter.capture(_window((0, 0, 10, 10)))


@pytest.mark.asyncio
async def test_locked_desktop_is_reported_instead_of_acting():
    adapter, client = _adapter([], locked=True)
    with pytest.raises(DesktopUnavailable, match="desktop is locked"):
        await adapter.dispatch(_window(), action="click", delivery="physical",
                               element=None, arguments={"x": 110, "y": 210})
    with pytest.raises(DesktopUnavailable, match="desktop is locked"):
        await adapter.observe(_window(), mode="uia")
    assert client.calls == []


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


@pytest.mark.asyncio
async def test_each_run_drives_its_own_session_and_late_input_is_refused():
    adapter, client = _adapter([{}, {}], run_id="run-1")
    window = _window()
    await adapter.dispatch(window, action="click", delivery="physical", element=None,
                           arguments={"x": 110, "y": 210})
    assert client.calls[0][1]["session"] == session_label("run-1") == "variant1-run-1"
    adapter.end_run("run-1")
    assert client.calls[1] == ("end_session", {"session": "variant1-run-1"})
    with pytest.raises(DesktopUnavailable, match="run has ended"):
        await adapter.dispatch(window, action="click", delivery="physical", element=None,
                               arguments={"x": 110, "y": 210})
    assert len(client.calls) == 2


def test_missing_driver_stays_unsupported_with_a_reason(monkeypatch):
    monkeypatch.setattr(
        "desktop_fabric.cua_adapter.resolve_cua_driver_command", lambda: None,
    )
    for platform in ("win32", "linux", "darwin"):
        adapter = select_cua_adapter(platform)
        assert isinstance(adapter, UnsupportedDesktopAdapter)
        assert "cua-driver is missing" in adapter.capability_report()["reason"]


def test_every_platform_selects_cua_without_starting_the_driver(monkeypatch):
    monkeypatch.setattr(
        "desktop_fabric.cua_adapter.resolve_cua_driver_command",
        lambda: ["cua-driver", "mcp"],
    )
    # macOS needs CuaDriver.app; tests/test_cua_macos.py covers it.
    for platform in ("win32", "linux"):
        adapter = select_cua_adapter(platform)
        assert isinstance(adapter, CuaDesktopAdapter)
        assert adapter.host.status()["running"] is False


def test_host_restarts_a_dead_driver_but_never_replays_input():
    clients = []

    def factory(_argv):
        clients.append(_ScriptedClient([], version="0.28.2"))
        return clients[-1]

    host = CuaDriverHost(["cua-driver", "mcp"], client_factory=factory, expected_version="0.28.2")
    assert host.status()["running"] is False
    host.call("list_windows", {}, read_only=True)
    assert host.generation == 1
    # The process dies during an action: the error surfaces, nothing is retried.
    clients[0].responses = [_Died("closed")]
    with pytest.raises(CuaDriverError):
        host.call("click", {"pid": 1, "window_id": 2})
    assert [name for name, _ in clients[0].calls] == ["list_windows", "click"]
    # The next call starts a fresh process.
    host.call("list_windows", {}, read_only=True)
    assert host.generation == 2 and len(clients) == 2
    # A read interrupted by process death is retried once on a new process.
    clients[1].responses = [_Died("closed")]
    host.call("list_windows", {}, read_only=True)
    assert host.generation == 3
    assert [name for name, _ in clients[2].calls] == ["list_windows"]


def test_host_refuses_a_driver_that_does_not_match_the_pin():
    client = _ScriptedClient([], version="0.30.0")
    host = CuaDriverHost(["cua-driver", "mcp"], client_factory=lambda _argv: client,
                         expected_version="0.28.2")
    with pytest.raises(CuaDriverError, match="does not match the pinned 0.28.2"):
        host.call("list_windows", {}, read_only=True)
    assert client.closed == 1


def test_driver_environment_has_no_secrets_and_telemetry_is_off():
    env = cua_driver_env({
        "PATH": "/bin", "SystemRoot": r"C:\Windows", "DISPLAY": ":0",
        "OPENROUTER_API_KEY": "secret", "VARIANT1_DATA_DIR": "/data",
        "LC_ALL": "C.UTF-8", "AWS_SECRET_ACCESS_KEY": "secret",
    })
    assert env["DO_NOT_TRACK"] == "1"
    assert env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] == "0"
    assert {"PATH", "SystemRoot", "DISPLAY", "LC_ALL"} <= set(env)
    assert "OPENROUTER_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert "VARIANT1_DATA_DIR" not in env
    opted_in = cua_driver_env({"PATH": "/bin", "VARIANT1_CUA_TELEMETRY": "1"})
    assert "DO_NOT_TRACK" not in opted_in
    assert "CUA_DRIVER_RS_TELEMETRY_ENABLED" not in opted_in


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
        write_message({"jsonrpc":"2.0","id":message["id"],"result":{
            "protocolVersion":"2025-06-18","serverInfo":{"name":"cua-driver","version":"0.28.2"}}})
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
        assert client.server_info["version"] == "0.28.2"
        assert client.alive()
        result = client.call_tool("list_windows", {"session": "variant1"})
    finally:
        client.close()
    assert result["windows"][0]["title"] == "Calc"
    assert not client.alive()


def test_unwrap_prefers_structured_content_and_keeps_the_image():
    assert unwrap_tool_result({
        "structuredContent": {"ok": True},
        "content": [{"type": "text", "text": "{\"ok\": false}"}],
    }) == {"ok": True}
    unwrapped = unwrap_tool_result({
        "structuredContent": {"pid": 1},
        "content": [{"type": "image", "mimeType": "image/png", "data": "aGk="}],
    })
    assert unwrapped["_image_base64"] == "aGk=" and unwrapped["_image_mime_type"] == "image/png"
