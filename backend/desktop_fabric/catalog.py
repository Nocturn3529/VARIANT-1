"""Strong Win32 app/window identity discovery for Desktop Fabric."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import platform
from typing import Any

from .models import AppRecord, ProcessIdentity, WindowRecord, stable_digest


def _process_details(pid: int) -> tuple[str, float]:
    try:
        import psutil
        process = psutil.Process(int(pid))
        return os.path.normcase(os.path.abspath(process.exe())), float(process.create_time())
    except Exception:
        return "", 0.0


def _text(user32: Any, hwnd: int, *, class_name: bool = False) -> str:
    try:
        if class_name:
            buf = ctypes.create_unicode_buffer(512)
            length = int(user32.GetClassNameW(hwnd, buf, len(buf)) or 0)
            return buf.value[:length]
        length = max(0, int(user32.GetWindowTextLengthW(hwnd)))
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value
    except Exception:
        return ""


def _monitor_device(user32: Any, hwnd: int) -> str:
    try:
        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]
        handle = user32.MonitorFromWindow(hwnd, 2)  # nearest
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if handle and user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return str(info.szDevice or "")
    except Exception:
        pass
    return ""


class Win32DesktopCatalog:
    """Enumerate top-level windows without using titles as identity.

    HWND reuse is disambiguated by PID and process creation time.  Package and
    virtual-desktop enrichment are left empty when Windows does not expose them
    to this process; absence is not represented as a false assertion.
    """

    def available(self) -> bool:
        return platform.system() == "Windows"

    def scan(self, *, backend_instance_id: str = "") -> tuple[list[AppRecord], list[WindowRecord]]:
        if not self.available():
            return [], []
        user32 = ctypes.windll.user32
        try:
            dwmapi = ctypes.windll.dwmapi
        except Exception:
            dwmapi = None
        foreground = int(user32.GetForegroundWindow() or 0)
        windows: list[WindowRecord] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd: int, _lparam: int) -> bool:
            hwnd = int(hwnd)
            if not hwnd or not bool(user32.IsWindow(hwnd)):
                return True
            visible = bool(user32.IsWindowVisible(hwnd))
            title = _text(user32, hwnd)
            class_name = _text(user32, hwnd, class_name=True)
            # Keep visible untitled application surfaces, but discard internal
            # hidden plumbing windows that cannot be targeted as app windows.
            if not visible and hwnd != foreground:
                return True
            if not title and class_name in {"Progman", "WorkerW", "Shell_TrayWnd"}:
                return True
            pid_value = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
            pid = int(pid_value.value)
            if pid <= 0:
                return True
            executable, started_at = _process_details(pid)
            if started_at <= 0:
                # A process start time is required for a strong, reusable ID.
                # Retain the row as a backend-generation identity, explicitly
                # anchored to 0 rather than pretending title uniqueness.
                started_at = 0.0
            rect = wintypes.RECT()
            bounds = None
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            try:
                owner = int(user32.GetWindow(hwnd, 4) or 0)  # GW_OWNER
                root_owner = int(user32.GetAncestor(hwnd, 3) or 0)  # GA_ROOTOWNER
            except Exception:
                owner = root_owner = 0
            try:
                dpi = int(user32.GetDpiForWindow(hwnd) or 96)
            except Exception:
                dpi = 96
            monitor = _monitor_device(user32, hwnd)
            cloaked: bool | None = None
            if dwmapi is not None:
                value = wintypes.DWORD()
                try:
                    if int(dwmapi.DwmGetWindowAttribute(
                        hwnd, 14, ctypes.byref(value), ctypes.sizeof(value))) == 0:
                        cloaked = bool(value.value)
                except Exception:
                    pass
            app_seed = executable or f"pid:{pid}:{started_at:.6f}"
            app_id = "app_" + stable_digest("win32-app", app_seed)[:24]
            window_id = "win_" + stable_digest(
                "win32-window", hwnd, pid, started_at, executable, class_name,
            )[:24]
            windows.append(WindowRecord(
                window_id=window_id, app_id=app_id, hwnd=hwnd, pid=pid,
                pid_started_at=started_at, executable=executable,
                class_name=class_name, title=title, owner_hwnd=owner,
                root_owner_hwnd=root_owner, monitor=monitor, dpi=dpi,
                bounds=bounds, visible=visible,
                minimized=bool(user32.IsIconic(hwnd)), cloaked=cloaked,
                occluded=None, foreground=hwnd == foreground,
                backend_instance_id=backend_instance_id,
            ))
            return True

        callback = WNDENUMPROC(visit)
        user32.EnumWindows(callback, 0)

        grouped: dict[str, list[WindowRecord]] = {}
        for window in windows:
            grouped.setdefault(window.app_id, []).append(window)
        apps = []
        for app_id, members in grouped.items():
            first = members[0]
            identities = {
                (item.pid, item.pid_started_at) for item in members
                if item.pid > 0
            }
            display = os.path.basename(first.executable) if first.executable else (
                first.title or first.class_name or f"Process {first.pid}")
            apps.append(AppRecord(
                app_id=app_id, executable=first.executable,
                package_family=first.package_family,
                app_user_model_id=first.app_user_model_id,
                display_name=display,
                processes=tuple(ProcessIdentity(pid, started) for pid, started in sorted(identities)),
                installed=None, running=True,
                launch_identity={"executable": first.executable} if first.executable else {},
            ))
        return apps, windows

    def validate(self, window: WindowRecord) -> dict[str, Any]:
        """Compare a durable window record with the current Win32 object."""
        if not self.available():
            return {"live": False, "reason": "windows_unavailable"}
        user32 = ctypes.windll.user32
        if not window.hwnd or not bool(user32.IsWindow(int(window.hwnd))):
            return {"live": False, "reason": "hwnd_missing"}
        pid_value = wintypes.DWORD()
        user32.GetWindowThreadProcessId(int(window.hwnd), ctypes.byref(pid_value))
        pid = int(pid_value.value)
        executable, started_at = _process_details(pid)
        same = (
            pid == window.pid
            and (not window.pid_started_at or abs(started_at - window.pid_started_at) < 0.001)
            and (not window.executable or executable == window.executable)
        )
        return {
            "live": bool(same), "reason": "identity_match" if same else "identity_changed",
            "hwnd": window.hwnd, "pid": pid, "pid_started_at": started_at,
            "executable": executable,
            "foreground": int(user32.GetForegroundWindow() or 0) == window.hwnd,
            "title": _text(user32, window.hwnd),
            "class_name": _text(user32, window.hwnd, class_name=True),
        }


__all__ = ["Win32DesktopCatalog"]
