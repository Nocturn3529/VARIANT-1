"""Settings reads desktop-control status without starting the driver."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from desktop_fabric import cua_adapter
from desktop_fabric.cua_client import CuaDriverError
from desktop_fabric.status import desktop_status, request_desktop_permissions
from desktop_fabric.unsupported import UnsupportedDesktopAdapter


def _fabric(adapter):
    return SimpleNamespace(adapter=adapter)


def _mac_adapter(tmp_path, monkeypatch):
    macos = tmp_path / "CuaDriver.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / "cua-driver"
    binary.write_text("")
    binary.chmod(0o755)
    (tmp_path / "VERSION").write_text("0.28.2\n")
    monkeypatch.setattr(cua_adapter, "resolve_cua_driver_command", lambda: [str(binary), "mcp"])
    return cua_adapter.select_cua_adapter("darwin")


def test_a_missing_driver_reports_unavailable_with_its_reason():
    status = desktop_status(_fabric(UnsupportedDesktopAdapter(platform="win32", reason="no driver here")))
    driver = status["driver"]
    assert (driver["available"], driver["version"], driver["state"]) == (False, "", "unavailable")
    assert "no driver here" in driver["error"]
    assert status["can_request_permissions"] is False
    assert "permissions" not in status


def test_windows_reports_the_pinned_driver_and_its_last_start_error(monkeypatch):
    monkeypatch.setattr(cua_adapter, "resolve_cua_driver_command", lambda: ["cua-driver.exe", "mcp"])
    adapter = cua_adapter.select_cua_adapter("win32")
    adapter.host.expected_version = "0.28.2"
    status = desktop_status(_fabric(adapter))
    assert status == {"platform": "win32", "can_request_permissions": False,
                      "driver": {"available": True, "version": "0.28.2", "state": "stopped"}}

    def broken(_argv):
        raise CuaDriverError("could not start cua-driver: missing")
    adapter.host._factory = broken
    with pytest.raises(CuaDriverError):
        adapter.host.call("list_windows", {}, read_only=True)
    driver = desktop_status(_fabric(adapter))["driver"]
    assert driver["state"] == "error" and "missing" in driver["error"]
    with pytest.raises(ValueError):
        request_desktop_permissions(_fabric(adapter))


def test_linux_reports_its_session_type_and_desktop(monkeypatch):
    monkeypatch.setattr(cua_adapter, "resolve_cua_driver_command", lambda: ["cua-driver", "mcp"])
    adapter = cua_adapter.select_cua_adapter("linux")
    status = desktop_status(_fabric(adapter), environ={"XDG_SESSION_TYPE": "Wayland",
                                                       "XDG_CURRENT_DESKTOP": "GNOME"})
    assert status["session_type"] == "wayland" and status["desktop"] == "GNOME"


def test_macos_reports_grants_and_can_ask_for_them(tmp_path, monkeypatch):
    adapter = _mac_adapter(tmp_path, monkeypatch)
    status = desktop_status(_fabric(adapter))
    assert status["platform"] == "darwin"
    assert status["permissions"] == {"accessibility": None, "screen_recording": None}
    assert status["can_request_permissions"] is True
    assert status["driver"]["version"] == "0.28.2"

    permissions = adapter.host.permissions
    started = []
    permissions.start = lambda: started.append(True) or True
    assert request_desktop_permissions(_fabric(adapter)) is True and started == [True]
    permissions.active = lambda: True
    permissions.last_status = {"accessibility": True, "screen_recording": False}
    status = desktop_status(_fabric(adapter))
    assert status["driver"]["state"] == "waiting_permissions"
    assert status["permissions"] == {"accessibility": True, "screen_recording": False}


@pytest.mark.asyncio
async def test_settings_asks_over_the_websocket(tmp_path, monkeypatch):
    import ws_dispatch

    adapter = _mac_adapter(tmp_path, monkeypatch)
    adapter.host.permissions.start = lambda: True
    srv = SimpleNamespace(require_runtime=lambda: SimpleNamespace(desktop=_fabric(adapter)))
    websocket = AsyncMock()
    await ws_dispatch.HANDLERS["desktop:status"](srv, websocket, None, {
        "type": "desktop:status", "request_id": "r1"})
    reply = websocket.send_json.await_args.args[0]
    assert reply["type"] == "desktop:status" and reply["request_id"] == "r1" and reply["ok"] is True
    assert "permission_request" not in reply
    await ws_dispatch.HANDLERS["desktop:permissions:request"](srv, websocket, None, {
        "type": "desktop:permissions:request", "request_id": "r2"})
    reply = websocket.send_json.await_args.args[0]
    assert reply["permission_request"] == {"started": True} and reply["request_id"] == "r2"

    windows = SimpleNamespace(require_runtime=lambda: SimpleNamespace(
        desktop=_fabric(UnsupportedDesktopAdapter(platform="win32"))))
    await ws_dispatch.HANDLERS["desktop:permissions:request"](windows, websocket, None, {
        "type": "desktop:permissions:request"})
    reply = websocket.send_json.await_args.args[0]
    assert reply["ok"] is False and reply["error"]["code"] == "desktop_status_failed"
