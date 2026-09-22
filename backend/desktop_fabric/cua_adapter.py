"""Desktop Fabric adapter over cua-driver for Linux and macOS.

Windows keeps ``WindowsDesktopAdapter``. This adapter is used only when a
``cua-driver`` binary is available. It refuses windows that lack a process id,
a driver window id, or a verified process start time.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Callable, Mapping

from .adapter import (
    AdapterCapture,
    AdapterDispatch,
    AdapterFocus,
    AdapterObservation,
)
from .cua_client import (
    CuaDriverClient,
    CuaDriverError,
    resolve_cua_driver_command,
)
from .models import (
    AppRecord,
    DesktopElement,
    DesktopUnavailable,
    ProcessIdentity,
    WindowRecord,
)


_SESSION = "variant1"
_UNPROVEN_CAPTURE = "surface_identity_unproven"


def linux_started_at_from_stat(stat_text: str, *, btime: int, clk_tck: float) -> float:
    end = stat_text.rfind(")")
    if end < 0:
        return 0.0
    fields = stat_text[end + 2:].split()
    if len(fields) <= 19 or clk_tck <= 0:
        return 0.0
    return float(btime) + int(fields[19]) / float(clk_tck)


def parse_darwin_lstart(text: str) -> float:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return 0.0
    try:
        parsed = datetime.strptime(cleaned, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return 0.0
    return parsed.timestamp()


def host_process_started_at(pid: int, *, platform: str | None = None) -> float:
    system = platform or sys.platform
    if pid <= 0:
        return 0.0
    try:
        if system.startswith("linux"):
            btime = 0
            with open("/proc/stat", encoding="ascii", errors="replace") as handle:
                for line in handle:
                    if line.startswith("btime "):
                        btime = int(line.split()[1])
                        break
            clk = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
                return linux_started_at_from_stat(
                    handle.read(), btime=btime, clk_tck=float(clk),
                )
        if system == "darwin":
            completed = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "lstart="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if completed.returncode != 0:
                return 0.0
            return parse_darwin_lstart(completed.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0
    return 0.0


class CuaDesktopAdapter:
    """Map cua-driver window tools onto the Desktop Fabric adapter."""

    def __init__(
        self,
        *,
        client: CuaDriverClient | None = None,
        command: list[str] | None = None,
        platform: str | None = None,
        started_at: Callable[[int], float] | None = None,
    ) -> None:
        self.platform = platform or sys.platform
        self._client = client
        self._command = list(command or [])
        self._started_at = started_at or (
            lambda pid: host_process_started_at(pid, platform=self.platform)
        )
        self._owns_client = client is None

    def open(self) -> None:
        if self._client is None:
            if not self._command:
                raise CuaDriverError("cua-driver command is empty")
            self._client = CuaDriverClient(self._command)
        self._client.open()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def catalog(self, *, backend_instance_id: str) -> tuple[list[AppRecord], list[WindowRecord]]:
        payload = self._tool("list_windows", {})
        rows = _window_rows(payload)
        apps: dict[str, AppRecord] = {}
        windows: list[WindowRecord] = []
        for row in rows:
            record = self._window_record(row, backend_instance_id=backend_instance_id)
            if record is None:
                continue
            windows.append(record)
            if record.app_id not in apps:
                apps[record.app_id] = AppRecord(
                    app_id=record.app_id,
                    executable=record.executable,
                    display_name=record.title or record.executable or record.app_id,
                    processes=(ProcessIdentity(record.pid, record.pid_started_at),),
                    running=True,
                )
        return list(apps.values()), windows

    def validate_window(self, window: WindowRecord) -> Mapping[str, Any]:
        native_id = int(window.hwnd or 0)
        if window.pid <= 0 or native_id <= 0 or window.pid_started_at <= 0:
            return {"live": False, "reason": "missing_process_identity"}
        started = float(self._started_at(int(window.pid)) or 0)
        if started <= 0 or abs(started - float(window.pid_started_at)) > 1.0:
            return {"live": False, "reason": "process_start_changed"}
        payload = self._tool("list_windows", {"pid": int(window.pid)})
        for row in _window_rows(payload):
            if int(row.get("pid") or 0) != int(window.pid):
                continue
            if int(row.get("window_id") or 0) != native_id:
                continue
            return {
                "live": True,
                "hwnd": native_id,
                "pid": int(window.pid),
                "pid_started_at": window.pid_started_at,
            }
        return {"live": False, "reason": "window_missing"}

    async def observe(self, window: WindowRecord, *, mode: str) -> AdapterObservation:
        state = self._window_state(window, include_screenshot=False)
        elements = [
            _element(row, index)
            for index, row in enumerate(_element_rows(state))
        ]
        return AdapterObservation(
            uia=tuple(elements),
            uia_generation=1,
            completeness="accessibility",
            metadata={
                "adapter": "cua-driver",
                "snapshot_id": str(state.get("snapshot_id") or ""),
                "window_rect": list(window.bounds or ()),
                "degraded": bool(state.get("degraded")),
                "mode": mode,
            },
        )

    async def capture(self, window: WindowRecord) -> AdapterCapture:
        state = self._window_state(window, include_screenshot=True)
        error = state.get("screenshot_error") or {}
        code = ""
        if isinstance(error, dict):
            code = str(error.get("code") or "")
        if code == _UNPROVEN_CAPTURE or state.get("screenshot_frame_valid") is False:
            raise DesktopUnavailable(
                "cua-driver could not prove this screenshot belongs to the window"
            )
        png = _png_bytes(state)
        if not png:
            raise DesktopUnavailable("cua-driver returned no window screenshot")
        width = int(state.get("width") or state.get("screenshot_width") or 0)
        height = int(state.get("height") or state.get("screenshot_height") or 0)
        left, top, right, bottom = window.bounds or (0, 0, width, height)
        return AdapterCapture(
            png=png,
            width=width or max(0, right - left),
            height=height or max(0, bottom - top),
            provenance="cua-driver-window",
            coordinate_transform={
                "window_rect": [left, top, right, bottom],
                "screen_origin": [left, top],
                "capture_size": [width or max(0, right - left), height or max(0, bottom - top)],
                "mode": "window",
            },
        )

    async def preflight(
        self, window: WindowRecord, *, action: str, delivery: str,
        arguments: Mapping[str, Any],
    ) -> None:
        if int(window.pid or 0) <= 0 or int(window.hwnd or 0) <= 0:
            raise DesktopUnavailable("cua-driver action needs a pid and window id")
        if not str(action or "").strip():
            raise DesktopUnavailable("desktop action is required")

    async def dispatch(
        self, window: WindowRecord, *, action: str, delivery: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch:
        tool, payload = _action_call(window, action, element, arguments)
        result = self._tool(tool, payload)
        effect = str(result.get("effect") or "").casefold()
        if effect == "refused" or result.get("ok") is False:
            return AdapterDispatch(
                delivered=False,
                method=f"cua-driver:{tool}",
                readback=result.get("value_readback"),
                metadata={"effect": effect or "refused", "route": result.get("route")},
            )
        return AdapterDispatch(
            delivered=True,
            method=f"cua-driver:{tool}",
            readback=result.get("value_readback"),
            metadata={
                "effect": effect or "unverifiable",
                "route": result.get("route"),
            },
        )

    async def focus(self, window: WindowRecord) -> AdapterFocus:
        observation = await self.observe(window, mode="uia")
        return AdapterFocus(
            result={"focused": True, "window_id": window.window_id},
            hwnd=int(window.hwnd),
            observation=observation,
        )

    async def focus_session(self, arguments: Mapping[str, Any]) -> AdapterFocus:
        query = str(
            arguments.get("name") or arguments.get("title") or arguments.get("window") or ""
        ).casefold()
        payload = self._tool("list_windows", {})
        matches = []
        for row in _window_rows(payload):
            title = str(row.get("title") or row.get("app") or "").casefold()
            if query and query not in title:
                continue
            if int(row.get("pid") or 0) > 0 and int(row.get("window_id") or 0) > 0:
                matches.append(row)
        if len(matches) != 1:
            raise DesktopUnavailable(
                "cua-driver focus needs exactly one matching window"
            )
        native_id = int(matches[0]["window_id"])
        return AdapterFocus(
            result={"focused": True, "title": matches[0].get("title") or ""},
            hwnd=native_id,
            observation=AdapterObservation(completeness="accessibility"),
        )

    def capability_report(self) -> Mapping[str, Any]:
        return {
            "adapter": "cua-driver",
            "platform": self.platform,
            "supported": True,
            "reason": "",
            "windows_available": True,
            "driver_state": {
                "explicitly_bound_per_window": True,
                "checkpointed": False,
                "process_default": False,
                "run_registry": False,
            },
            "perception": {"uia": True, "screenshot": True, "ocr": False},
            "capture": {"cua_window_screenshot": True},
            "actions": {
                "host_selected_semantic_or_physical": True,
                "physical_input": True,
                "semantic_supported": [
                    "invoke", "set_value", "focus", "toggle", "select",
                    "expand", "collapse", "scroll_into_view",
                ],
                "physical_supported": [
                    "click", "set_value", "send_keys", "type_text", "scroll", "drag",
                ],
            },
        }

    def _window_state(self, window: WindowRecord, *, include_screenshot: bool) -> dict[str, Any]:
        return self._tool("get_window_state", {
            "pid": int(window.pid),
            "window_id": int(window.hwnd),
            "include_screenshot": include_screenshot,
            "session": _SESSION,
        })

    def _window_record(self, row: Mapping[str, Any], *, backend_instance_id: str) -> WindowRecord | None:
        pid = int(row.get("pid") or 0)
        native_id = int(row.get("window_id") or 0)
        if pid <= 1 or native_id <= 0:
            return None
        started = float(self._started_at(pid) or 0)
        if started <= 0:
            return None
        bounds = _bounds(row)
        title = str(row.get("title") or "")
        executable = str(row.get("executable") or row.get("app") or "")
        return WindowRecord(
            window_id=f"cua:{pid}:{native_id}",
            app_id=f"cua-app:{pid}",
            hwnd=native_id,
            pid=pid,
            pid_started_at=started,
            executable=executable,
            title=title,
            bounds=bounds,
            visible=not bool(row.get("minimized")),
            minimized=bool(row.get("minimized")),
            foreground=bool(row.get("focused") or row.get("foreground")),
            backend_instance_id=backend_instance_id,
        )

    def _tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self.open()
        assert self._client is not None
        payload = dict(arguments)
        payload.setdefault("session", _SESSION)
        try:
            return self._client.call_tool(name, payload)
        except CuaDriverError as exc:
            raise DesktopUnavailable(str(exc)) from exc


def select_non_windows_adapter(platform: str) -> Any:
    """Use cua-driver when it is installed; otherwise stay explicitly unsupported."""

    from .unsupported import UnsupportedDesktopAdapter

    command = resolve_cua_driver_command()
    if not command:
        return UnsupportedDesktopAdapter(platform=platform)
    adapter = CuaDesktopAdapter(command=command, platform=platform)
    try:
        adapter.open()
    except Exception:
        adapter.close()
        return UnsupportedDesktopAdapter(platform=platform)
    return adapter


def _window_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = payload.get("windows")
    if rows is None and isinstance(payload.get("items"), list):
        rows = payload.get("items")
    if isinstance(payload, list):
        rows = payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _element_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = payload.get("elements")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _bounds(row: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    raw = row.get("bounds")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        left, top, right, bottom = (int(value) for value in raw)
        if right > left and bottom > top:
            return left, top, right, bottom
    try:
        left = int(row.get("x"))
        top = int(row.get("y"))
        width = int(row.get("width"))
        height = int(row.get("height"))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return left, top, left + width, top + height


def _element(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    token = str(row.get("element_token") or row.get("id") or index)
    patterns = []
    actions = row.get("actions") or ()
    for action in actions:
        name = action.get("name") if isinstance(action, Mapping) else action
        label = str(name or "").casefold()
        if label in {"press", "invoke", "click", "do_action"}:
            patterns.append("invoke")
        elif "value" in label:
            patterns.append("value")
        elif "toggle" in label:
            patterns.append("toggle")
        elif "expand" in label or "collapse" in label:
            patterns.append("expand_collapse")
        elif "select" in label:
            patterns.append("selection_item")
        elif "scroll" in label:
            patterns.append("scroll_item")
    enabled = bool(row.get("enabled", True))
    if enabled and "invoke" not in patterns:
        patterns.append("invoke")
    frame = row.get("frame") or row.get("bounds")
    bounds = None
    if isinstance(frame, Mapping):
        try:
            left = int(frame.get("x"))
            top = int(frame.get("y"))
            width = int(frame.get("width"))
            height = int(frame.get("height"))
            bounds = [left, top, left + width, top + height]
        except (TypeError, ValueError):
            bounds = None
    elif isinstance(frame, (list, tuple)) and len(frame) == 4:
        bounds = [int(value) for value in frame]
    return {
        "role": str(row.get("role") or "control"),
        "name": str(row.get("label") or row.get("name") or ""),
        "value": str(row.get("value") or ""),
        "backend_key": token,
        "automation_id": token,
        "patterns": patterns,
        "actionable": enabled,
        "bounds": bounds,
        "provenance": "uia",
    }


def _png_bytes(payload: Mapping[str, Any]) -> bytes:
    raw = payload.get("screenshot_base64") or payload.get("image_base64") or ""
    if not raw:
        return b""
    try:
        return base64.b64decode(str(raw))
    except (ValueError, TypeError):
        return b""


def _local_point(window: WindowRecord, x: Any, y: Any) -> tuple[int, int]:
    left, top = (window.bounds or (0, 0, 0, 0))[:2]
    return int(x) - int(left), int(y) - int(top)


def _target(window: WindowRecord) -> dict[str, Any]:
    return {
        "kind": "window",
        "pid": int(window.pid),
        "window_id": int(window.hwnd),
    }


def _action_call(
    window: WindowRecord,
    action: str,
    element: DesktopElement | None,
    arguments: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    normalized = str(action or "").casefold()
    payload: dict[str, Any] = {
        "target": _target(window),
        "session": _SESSION,
        "delivery_mode": "background",
    }
    token = str(element.backend_key or "") if element is not None else ""
    if normalized in {"click", "invoke"}:
        if token:
            payload["element_token"] = token
            return "click", payload
        if arguments.get("x") is None or arguments.get("y") is None:
            raise DesktopUnavailable("cua-driver click needs a control or a point")
        x, y = _local_point(window, arguments.get("x"), arguments.get("y"))
        payload.update({"x": x, "y": y})
        button = str(arguments.get("button") or "left").casefold()
        if button == "right":
            return "right_click", payload
        if arguments.get("double"):
            return "double_click", payload
        return "click", payload
    if normalized in {"type", "type_text"}:
        payload["text"] = str(arguments.get("text") or "")
        if token:
            payload["element_token"] = token
        return "type_text", payload
    if normalized in {"send_keys", "press_key"}:
        keys = arguments.get("keys")
        if isinstance(keys, (list, tuple)):
            payload["keys"] = [str(item) for item in keys]
            return "hotkey", payload
        payload["key"] = str(keys or "")
        return "press_key", payload
    if normalized == "set_value":
        if not token:
            raise DesktopUnavailable("cua-driver set_value needs a control")
        return "set_value", {
            "pid": int(window.pid),
            "window_id": int(window.hwnd),
            "element_token": token,
            "value": str(arguments.get("value") or ""),
            "session": _SESSION,
        }
    if normalized in {"scroll", "scroll_into_view"}:
        if arguments.get("x") is not None and arguments.get("y") is not None:
            x, y = _local_point(window, arguments.get("x"), arguments.get("y"))
            payload.update({"x": x, "y": y})
        payload["direction"] = str(arguments.get("direction") or "down")
        payload["amount"] = int(arguments.get("amount") or 1)
        if token:
            payload["element_token"] = token
        return "scroll", payload
    if normalized == "drag":
        x1, y1 = _local_point(window, arguments.get("x1"), arguments.get("y1"))
        x2, y2 = _local_point(window, arguments.get("x2"), arguments.get("y2"))
        payload.update({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
        return "drag", payload
    if token:
        payload["element_token"] = token
        return "click", payload
    raise DesktopUnavailable(f"cua-driver has no mapping for {action}")


__all__ = [
    "CuaDesktopAdapter",
    "host_process_started_at",
    "linux_started_at_from_stat",
    "parse_darwin_lstart",
    "select_non_windows_adapter",
]
