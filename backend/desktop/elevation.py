"""UIPI / elevation detection for desktop control.

Windows User Interface Privilege Isolation (UIPI) makes an elevated (admin)
process effectively invisible and untouchable to an un-elevated automation
client: the elevated window's HWND is still enumerable via Win32, but it does
NOT appear among the UIA root's children (so ``find_window`` can't match it and
a tree walk yields zero controls), and synthesized clicks/keys sent to it are
silently discarded.

Probed live 2026-07-08 with Task Manager (runs elevated by default on admin
accounts): Win32 ``EnumWindows`` sees ``TaskManagerWindow`` fine, the UIA root
lists nothing for that process. Settings — an UN-elevated WinUI 3 app — walks
perfectly with the standard ControlView walker (200+ nodes), so WinUI 3 / XAML
itself is not the problem; the elevation boundary is. These helpers let
perception name that boundary instead of reporting a generic empty tree.

Pure Win32 via ctypes — safe to call from any thread (no COM/UIA needed).
"""

from __future__ import annotations

import ctypes
from typing import Optional

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20  # TokenElevation information class

_our_elevated: Optional[bool] = None


def our_process_elevated() -> bool:
    """Whether THIS process runs elevated (cached — it can't change at runtime)."""
    global _our_elevated
    if _our_elevated is None:
        try:
            _our_elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            _our_elevated = False
    return _our_elevated


def process_elevated(pid: int) -> Optional[bool]:
    """Whether the process with ``pid`` runs elevated; None if undeterminable."""
    if not pid:
        return None
    try:
        k32 = ctypes.windll.kernel32
        adv = ctypes.windll.advapi32
    except Exception:
        return None
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        tok = ctypes.c_void_p()
        if not adv.OpenProcessToken(h, _TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            elev = ctypes.c_ulong(0)
            ret = ctypes.c_ulong(0)
            ok = adv.GetTokenInformation(
                tok, _TOKEN_ELEVATION, ctypes.byref(elev),
                ctypes.sizeof(elev), ctypes.byref(ret),
            )
            return bool(elev.value) if ok else None
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


def _window_pid(hwnd: int) -> int:
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(
            ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _window_title(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
        return buf.value or ""
    except Exception:
        return ""


def find_elevated_window(query: str) -> Optional[dict]:
    """A VISIBLE top-level Win32 window whose title contains ``query`` and whose
    process is elevated while ours is not — i.e. a window UIA cannot see.

    Returns ``{"hwnd", "title", "pid"}`` or None. Only meaningful as a
    second-chance check after the UIA-level ``find_window`` came up empty.
    """
    q = (query or "").strip().lower()
    if not q or our_process_elevated():
        return None
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return None
    hits: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if q not in title.lower():
                return True
            hits.append({"hwnd": int(hwnd or 0), "title": title,
                         "pid": _window_pid(hwnd)})
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return None
    for hit in hits:
        if process_elevated(hit["pid"]):
            return hit
    return None


def foreground_elevation_mismatch() -> Optional[dict]:
    """The Win32 foreground window, iff its process is elevated and ours is not.

    Returns ``{"hwnd", "title", "pid"}`` or None. This is the observation-side
    check: when the user/agent brought an elevated app to the front, the UIA
    walk sees either nothing or a stale background window — recovery retries
    can never fix that.
    """
    if our_process_elevated():
        return None
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return None
    if not hwnd:
        return None
    pid = _window_pid(hwnd)
    if process_elevated(pid):
        return {"hwnd": int(hwnd), "title": _window_title(hwnd), "pid": pid}
    return None


def uipi_guidance(title: str) -> str:
    """Bounded model-facing diagnostic for an elevated target."""
    name = (title or "this window").strip() or "this window"
    return (
        f"'{name}' belongs to an ELEVATED (administrator) process. Windows UIPI "
        "blocks this VARIANT-1 process from observing or controlling it. Retrying the "
        "same action cannot cross that boundary. Run VARIANT-1 elevated or ask the "
        "user to perform the interaction."
    )
