"""Desktop Fabric adapter over cua-driver, on every platform.

``CuaDriverHost`` owns the driver process. This adapter maps Desktop Fabric's
window identity, observations and actions onto the driver's MCP tools. It
refuses windows that lack a process id, a driver window id, or a verified
process start time, so a recycled pid is never mistaken for the bound window.
"""

from __future__ import annotations

import asyncio
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
    CuaDriverError,
    cua_driver_env,
    resolve_cua_driver_command,
)
from .driver_host import CuaDriverHost, CuaRunEnded
from .models import (
    AppRecord,
    DesktopElement,
    DesktopUnavailable,
    ProcessIdentity,
    WindowRecord,
)


_UNPROVEN_CAPTURE = "surface_identity_unproven"
# Optional structured fields some driver versions add to a window state.
_STATE_CONTEXT_FIELDS = ("focused_element", "selected_text", "document_text")


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
        if system.startswith("win"):
            import psutil

            return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return 0.0
    return 0.0


def windows_desktop_locked() -> bool:
    """True while the Windows input desktop is the lock or secure desktop."""

    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not handle:
            return True
        try:
            return not bool(user32.SwitchDesktop(handle))
        finally:
            user32.CloseDesktop(handle)
    except Exception:
        return False


def _current_run_id() -> str:
    try:
        from run_context import current_run_context

        context = current_run_context()
    except Exception:
        return ""
    return str(getattr(context, "run_id", "") or "") if context is not None else ""


class CuaDesktopAdapter:
    """Map cua-driver window tools onto the Desktop Fabric adapter."""

    def __init__(
        self,
        *,
        host: CuaDriverHost | None = None,
        client: Any = None,
        command: list[str] | None = None,
        platform: str | None = None,
        started_at: Callable[[int], float] | None = None,
        locked: Callable[[], bool] | None = None,
        run_id: Callable[[], str] | None = None,
    ) -> None:
        self.platform = platform or sys.platform
        if host is None:
            if client is not None:
                host = CuaDriverHost.from_client(client)
            elif command:
                host = CuaDriverHost(list(command))
            else:
                raise CuaDriverError("cua-driver command is empty")
        self.host = host
        self._started_at = started_at or (
            lambda pid: host_process_started_at(pid, platform=self.platform)
        )
        self._locked = locked or (
            windows_desktop_locked if self.platform.startswith("win") else (lambda: False)
        )
        self._run_id = run_id or _current_run_id

    def close(self) -> None:
        self.host.close()

    def end_run(self, run_id: str) -> None:
        self.host.end_run(run_id)

    # Catalog -----------------------------------------------------------

    def catalog(self, *, backend_instance_id: str) -> tuple[list[AppRecord], list[WindowRecord]]:
        payload = self._tool("list_windows", {}, read_only=True)
        apps: dict[str, AppRecord] = {}
        windows: list[WindowRecord] = []
        for row in _window_rows(payload):
            record = self._window_record(row, backend_instance_id=backend_instance_id)
            if record is None:
                continue
            windows.append(record)
            if record.app_id not in apps:
                apps[record.app_id] = AppRecord(
                    app_id=record.app_id,
                    executable=record.executable,
                    display_name=record.executable or record.title or record.app_id,
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
        payload = self._tool("list_windows", {"pid": int(window.pid)}, read_only=True)
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

    # Observation -------------------------------------------------------

    async def observe(self, window: WindowRecord, *, mode: str) -> AdapterObservation:
        self._refuse_locked()
        state = await self._atool("get_window_state", {
            "pid": int(window.pid),
            "window_id": int(window.hwnd),
            "include_screenshot": False,
            "include_accessibility_tree": True,
        }, read_only=True)
        elements = [
            _element(row, index)
            for index, row in enumerate(_element_rows(state))
        ]
        metadata: dict[str, Any] = {
            "adapter": "cua-driver",
            "snapshot_id": str(state.get("snapshot_id") or ""),
            "window_rect": list(window.bounds or ()),
            "degraded": bool(state.get("degraded")),
            "elements_complete": bool(state.get("elements_complete", True)),
            "mode": mode,
        }
        for key in _STATE_CONTEXT_FIELDS:
            if state.get(key) not in (None, ""):
                metadata[key] = state.get(key)
        return AdapterObservation(
            uia=tuple(elements),
            uia_generation=1,
            completeness="accessibility",
            metadata=metadata,
        )

    async def capture(self, window: WindowRecord) -> AdapterCapture:
        self._refuse_locked()
        state = await self._atool("get_window_state", {
            "pid": int(window.pid),
            "window_id": int(window.hwnd),
            "include_screenshot": True,
            "include_accessibility_tree": False,
        }, read_only=True)
        error = state.get("screenshot_error") or {}
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        if code == _UNPROVEN_CAPTURE or state.get("screenshot_frame_valid") is False:
            raise DesktopUnavailable(
                "cua-driver could not prove this screenshot belongs to the window"
            )
        png = _png_bytes(state)
        if not png:
            raise DesktopUnavailable("cua-driver returned no window screenshot")
        width = int(state.get("screenshot_width") or state.get("width") or 0)
        height = int(state.get("screenshot_height") or state.get("height") or 0)
        left, top, right, bottom = window.bounds or (0, 0, width, height)
        return AdapterCapture(
            png=png,
            width=width or max(0, right - left),
            height=height or max(0, bottom - top),
            provenance="cua-driver-window",
            # Per-window capture is not obscured by windows in front of it.
            occlusion_independent=True,
            coordinate_transform={
                "window_rect": [left, top, right, bottom],
                "screen_origin": [left, top],
                "capture_size": [width or max(0, right - left), height or max(0, bottom - top)],
                "mode": "window",
            },
        )

    # Actions -----------------------------------------------------------

    async def preflight(
        self, window: WindowRecord, *, action: str, delivery: str,
        arguments: Mapping[str, Any],
    ) -> None:
        if int(window.pid or 0) <= 0 or int(window.hwnd or 0) <= 0:
            raise DesktopUnavailable("cua-driver action needs a pid and window id")
        if not str(action or "").strip():
            raise DesktopUnavailable("desktop action is required")
        self._refuse_locked()

    async def dispatch(
        self, window: WindowRecord, *, action: str, delivery: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch:
        self._refuse_locked()
        tool, payload = _action_call(window, action, element, arguments)
        result = await self._atool(tool, payload)
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
        payload = await self._atool("list_windows", {}, read_only=True)
        matches = []
        for row in _window_rows(payload):
            title = str(row.get("title") or _app_name(row)).casefold()
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
            "driver": dict(self.host.status()),
            "driver_state": {
                "explicitly_bound_per_window": True,
                "checkpointed": False,
                "process_default": False,
                "run_registry": True,
            },
            "perception": {"uia": True, "screenshot": True, "ocr": False},
            "capture": {"cua_window_screenshot": True, "occlusion_independent": True},
            "actions": {
                "host_selected_semantic_or_physical": True,
                "physical_input": True,
                "background_input": True,
                "semantic_supported": [
                    "invoke", "set_value", "set_range_value", "focus", "toggle",
                    "select", "expand", "collapse", "scroll_into_view",
                ],
                "physical_supported": [
                    "click", "set_value", "send_keys", "type_text", "scroll", "drag",
                ],
            },
        }

    # Driver calls ------------------------------------------------------

    def _refuse_locked(self) -> None:
        if self._locked():
            raise DesktopUnavailable(
                "The desktop is locked. Ask the user to unlock it, then observe again."
            )

    def _window_record(self, row: Mapping[str, Any], *, backend_instance_id: str) -> WindowRecord | None:
        pid = int(row.get("pid") or 0)
        native_id = int(row.get("window_id") or 0)
        if pid <= 1 or native_id <= 0:
            return None
        started = float(self._started_at(pid) or 0)
        if started <= 0:
            return None
        title = str(row.get("title") or "")
        return WindowRecord(
            window_id=f"cua:{pid}:{native_id}",
            app_id=f"cua-app:{pid}",
            hwnd=native_id,
            pid=pid,
            pid_started_at=started,
            executable=_app_name(row),
            title=title,
            bounds=_bounds(row),
            visible=not bool(row.get("minimized")),
            minimized=bool(row.get("minimized")),
            foreground=bool(row.get("focused") or row.get("foreground")),
            backend_instance_id=backend_instance_id,
        )

    def _tool(self, name: str, arguments: Mapping[str, Any], *, read_only: bool = False) -> dict[str, Any]:
        try:
            return self.host.call(name, arguments, run_id=self._run_id(), read_only=read_only)
        except CuaRunEnded as exc:
            raise DesktopUnavailable(str(exc)) from exc
        except CuaDriverError as exc:
            raise DesktopUnavailable(str(exc)) from exc

    async def _atool(self, name: str, arguments: Mapping[str, Any], *, read_only: bool = False) -> dict[str, Any]:
        # Driver calls block on the process pipe; keep them off the event loop.
        # The run id is read here, where the run context is bound.
        run_id = self._run_id()

        def call() -> dict[str, Any]:
            try:
                return self.host.call(name, arguments, run_id=run_id, read_only=read_only)
            except CuaDriverError as exc:
                raise DesktopUnavailable(str(exc)) from exc

        return await asyncio.to_thread(call)


def select_cua_adapter(platform: str | None = None) -> Any:
    """cua-driver on every platform; explicitly unsupported when it is missing.

    The driver process itself starts on first desktop use.
    """

    from .unsupported import UnsupportedDesktopAdapter

    system = platform or sys.platform
    command = resolve_cua_driver_command()
    if not command:
        return UnsupportedDesktopAdapter(
            platform=system,
            reason="the bundled cua-driver is missing; repair or reinstall VARIANT-1",
        )
    if system == "darwin":
        from .cua_macos import MacDriverClient, MacPermissionRequest, app_bundle_for

        if not app_bundle_for(command[0]):
            return UnsupportedDesktopAdapter(
                platform=system,
                reason="macOS desktop control needs CuaDriver.app; repair or reinstall VARIANT-1",
            )
        # One permission request per backend, shared by every driver restart.
        permissions = MacPermissionRequest(command[0], env=cua_driver_env())
        host = CuaDriverHost(
            command,
            client_factory=lambda argv: MacDriverClient(argv, permissions=permissions),
        )
        host.permissions = permissions
        return CuaDesktopAdapter(host=host, platform=system)
    return CuaDesktopAdapter(host=CuaDriverHost(command), platform=system)


def _app_name(row: Mapping[str, Any]) -> str:
    return str(
        row.get("app_name") or row.get("executable") or row.get("app") or ""
    )


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


def _rect(raw: Any) -> tuple[int, int, int, int] | None:
    if isinstance(raw, Mapping):
        try:
            left = int(raw.get("x"))
            top = int(raw.get("y"))
            width = int(raw.get("width"))
            height = int(raw.get("height"))
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return left, top, left + width, top + height
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        left, top, right, bottom = (int(value) for value in raw)
        if right > left and bottom > top:
            return left, top, right, bottom
    return None


def _bounds(row: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    return _rect(row.get("bounds")) or _rect({
        key: row.get(key) for key in ("x", "y", "width", "height")
    })


def _element(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    token = str(row.get("element_token") or row.get("id") or index)
    patterns = []
    actions = row.get("actions") or ()
    for action in actions:
        name = action.get("name") if isinstance(action, Mapping) else action
        label = str(name or "").casefold()
        if label in {"press", "invoke", "click", "do_action"}:
            patterns.append("invoke")
        elif "range" in label:
            patterns.append("range_value")
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
    rect = _rect(row.get("frame") or row.get("bounds"))
    return {
        "role": str(row.get("role") or "control"),
        "name": str(row.get("label") or row.get("name") or ""),
        "value": str(row.get("value") or ""),
        "backend_key": token,
        "automation_id": token,
        "patterns": patterns,
        "actionable": enabled,
        "bounds": list(rect) if rect else None,
        "provenance": "uia",
    }


def _png_bytes(payload: Mapping[str, Any]) -> bytes:
    raw = (
        payload.get("_image_base64")
        or payload.get("screenshot_base64")
        or payload.get("image_base64")
        or ""
    )
    if not raw:
        return b""
    try:
        return base64.b64decode(str(raw))
    except (ValueError, TypeError):
        return b""


def _local_point(window: WindowRecord, x: Any, y: Any) -> tuple[int, int]:
    left, top = (window.bounds or (0, 0, 0, 0))[:2]
    return int(x) - int(left), int(y) - int(top)


def _action_call(
    window: WindowRecord,
    action: str,
    element: DesktopElement | None,
    arguments: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """One driver tool and its arguments for a Desktop Fabric action.

    Every call names the exact pid and window; points are window-local
    screenshot pixels, the space the driver captures and clicks in.
    """

    normalized = str(action or "").casefold()
    payload: dict[str, Any] = {
        "pid": int(window.pid),
        "window_id": int(window.hwnd),
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
    if normalized in {"set_value", "set_range_value"}:
        if not token:
            raise DesktopUnavailable(f"cua-driver {normalized} needs a control")
        payload.pop("delivery_mode", None)
        payload.update({"element_token": token, "value": str(arguments.get("value") or "")})
        return "set_value", payload
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
        payload.update({"from_x": x1, "from_y": y1, "to_x": x2, "to_y": y2})
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
    "select_cua_adapter",
    "windows_desktop_locked",
]
