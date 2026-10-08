"""Desktop-control status for Settings, read without starting the driver.

The driver starts on first desktop use, so this reports what is known:
whether a driver is installed, whether it runs, why it last failed and, on
macOS, CuaDriver's last reported grants and any grant request in progress.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping


def _platform(adapter: Any) -> str:
    return str(getattr(adapter, "platform", "") or sys.platform)


def desktop_status(fabric: Any, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    adapter = getattr(fabric, "adapter", None)
    report = dict(adapter.capability_report()) if adapter is not None else {}
    platform = _platform(adapter)
    host = getattr(adapter, "host", None)
    driver = dict(host.status()) if host is not None else {}
    permissions = getattr(host, "permissions", None)
    waiting = bool(permissions is not None and permissions.active())
    available = bool(report.get("supported")) and host is not None
    if not available:
        state = "unavailable"
    elif waiting:
        state = "waiting_permissions"
    elif driver.get("running"):
        state = "running"
    elif driver.get("last_error"):
        state = "error"
    else:
        state = "stopped"
    error = str(driver.get("last_error") or "") if available else str(report.get("reason") or "")
    status: dict[str, Any] = {
        "platform": platform,
        "driver": {
            "available": available,
            "version": str(driver.get("server_version") or driver.get("expected_version") or ""),
            "state": state,
            **({"error": error} if error and state in {"error", "unavailable"} else {}),
        },
        "can_request_permissions": bool(available and permissions is not None),
    }
    if platform == "darwin":
        status["permissions"] = (
            dict(permissions.last_status) if permissions is not None
            else {"accessibility": None, "screen_recording": None}
        )
    if platform.startswith("linux"):
        env = os.environ if environ is None else environ
        status["session_type"] = str(env.get("XDG_SESSION_TYPE") or "").lower() or None
        status["desktop"] = str(env.get("XDG_CURRENT_DESKTOP") or "") or None
    return status


def request_desktop_permissions(fabric: Any) -> bool:
    """Ask macOS for CuaDriver's grants; False when nothing new was started."""

    host = getattr(getattr(fabric, "adapter", None), "host", None)
    permissions = getattr(host, "permissions", None)
    if permissions is None:
        raise ValueError("this platform has no desktop permissions to request")
    return bool(permissions.start())


__all__ = ["desktop_status", "request_desktop_permissions"]
