"""Window-aware desktop capture for vision-grounded actions.

UIA BoundingRectangle values and Win32 GetWindowRect both use physical screen
pixels when the process is DPI-aware (mss enables this on Windows). Prefer
GetWindowRect via NativeWindowHandle for the locked window crop rect.
"""

from __future__ import annotations

from dataclasses import dataclass
import platform


MIN_CAPTURE_WIDTH = 8
MIN_CAPTURE_HEIGHT = 8


@dataclass(frozen=True)
class CaptureMeta:
    """Metadata for one vision capture."""

    mode: str = "monitor"          # "window" | "monitor"
    origin: tuple[int, int] = (0, 0)
    width: int = 0
    height: int = 0
    window_title: str = ""
    fallback_reason: str = ""
    dpi_scale: float = 1.0
    monitor: str = ""              # "active" | "primary" | "" (window crop / unknown)

    @property
    def cropped(self) -> bool:
        return self.mode == "window"


@dataclass(frozen=True)
class CaptureBundle:
    png: bytes
    meta: CaptureMeta


def normalize_rect(rect) -> tuple[int, int, int, int] | None:
    """Validate a screen rect (physical pixels). Returns int tuple or None."""
    if not rect or len(rect) != 4:
        return None
    try:
        left, top, right, bottom = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    except (TypeError, ValueError):
        return None
    width = right - left
    height = bottom - top
    if width < MIN_CAPTURE_WIDTH or height < MIN_CAPTURE_HEIGHT:
        return None
    return left, top, right, bottom


def rect_capturable(rect, *, iconic: bool = False) -> tuple[bool, str]:
    """Whether a window rect is suitable for cropping."""
    norm = normalize_rect(rect)
    if not norm:
        return False, "degenerate_bounds"
    if iconic:
        return False, "minimized"
    left, top, right, bottom = norm
    # Heuristic: window parked far off-screen (Windows "minimize to tray" trick).
    if right < -10000 or bottom < -10000:
        return False, "off_screen"
    return True, ""


def win32_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Physical-pixel window rect via GetWindowRect."""
    if platform.system() != "Windows" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ok = ctypes.windll.user32.GetWindowRect(int(hwnd), ctypes.byref(rect))
        if not ok:
            return None
        return normalize_rect((rect.left, rect.top, rect.right, rect.bottom))
    except Exception:
        return None


def win32_is_iconic(hwnd: int) -> bool:
    if platform.system() != "Windows" or not hwnd:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsIconic(int(hwnd)))
    except Exception:
        return False


def dpi_scale_for_point(x: int, y: int) -> float:
    """Effective DPI scale at a screen point (1.0 = 96 DPI). Physical pixels."""
    if platform.system() != "Windows":
        return 1.0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            shcore = ctypes.windll.shcore
        except Exception:
            return 1.0
        pt = ctypes.wintypes.POINT(int(x), int(y))
        MONITOR_DEFAULTTONEAREST = 2
        MDT_EFFECTIVE_DPI = 0
        monitor = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        hr = shcore.GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI,
                                     ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        if hr != 0 or not dpi_x.value:
            return 1.0
        return float(dpi_x.value) / 96.0
    except Exception:
        return 1.0


def grab_rect_png(left: int, top: int, right: int, bottom: int) -> bytes:
    """Capture a screen region to PNG bytes (physical pixels, RAM only)."""
    import mss
    import mss.tools

    width = int(right) - int(left)
    height = int(bottom) - int(top)
    with mss.MSS() as sct:
        shot = sct.grab({"left": int(left), "top": int(top), "width": width, "height": height})
        return mss.tools.to_png(shot.rgb, shot.size)


def grab_primary_monitor_png() -> tuple[bytes, tuple[int, int]]:
    """Full primary monitor capture. Returns (png, origin)."""
    import mss
    import mss.tools

    with mss.MSS() as sct:
        mons = sct.monitors
        mon = mons[1] if len(mons) > 1 else mons[0]
        shot = sct.grab(mon)
        png = mss.tools.to_png(shot.rgb, shot.size)
        return png, (int(mon["left"]), int(mon["top"]))


def win32_monitor_rect_for_hwnd(hwnd: int) -> tuple[int, int, int, int] | None:
    """Physical rect of the monitor containing a window (rcMonitor)."""
    if platform.system() != "Windows" or not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        mon = user32.MonitorFromWindow(int(hwnd), MONITOR_DEFAULTTONEAREST)
        if not mon:
            return None

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        r = mi.rcMonitor
        return normalize_rect((r.left, r.top, r.right, r.bottom))
    except Exception:
        return None


def grab_active_monitor_png() -> tuple[bytes, tuple[int, int], str]:
    """Capture the monitor containing the FOREGROUND window; primary as last
    resort. Returns (png, origin, monitor) with monitor 'active' or 'primary'.

    With multiple displays the task's window is often not on the primary
    monitor — capturing the wrong display can falsely report that work on the
    other screen is incomplete.
    """
    if platform.system() == "Windows":
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetForegroundWindow()
            rect = win32_monitor_rect_for_hwnd(hwnd) if hwnd else None
            if rect:
                png = grab_rect_png(*rect)
                return png, (rect[0], rect[1]), "active"
        except Exception:
            pass
    png, origin = grab_primary_monitor_png()
    return png, origin, "primary"


def grab_monitor_for_hwnd_png(
    hwnd: int,
) -> tuple[bytes, tuple[int, int], str] | None:
    """Capture the monitor containing an exact durable HWND."""

    rect = win32_monitor_rect_for_hwnd(int(hwnd or 0))
    if not rect:
        return None
    png = grab_rect_png(*rect)
    return png, (rect[0], rect[1]), "locked"


def capture_event_text(meta: CaptureMeta) -> str:
    if meta.cropped:
        # A cropped capture with a fallback_reason means the crop came from
        # the foreground-window fallback, not a held target lock.
        kind = "foreground window" if meta.fallback_reason else "locked window"
        return (
            f"Vision capture: cropped to {kind} "
            f"'{meta.window_title or '(window)'}' "
            f"({meta.width}×{meta.height} @ origin {meta.origin})"
        )
    scope = f"{meta.monitor} monitor" if meta.monitor else "monitor"
    if meta.fallback_reason:
        return f"Vision capture: full {scope} (fallback: {meta.fallback_reason})"
    return f"Vision capture: full {scope} (no window lock)"
