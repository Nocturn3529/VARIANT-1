"""Foreground-window selection helpers for UIA perception."""

from __future__ import annotations

from . import constants as dconst


def is_overlay(name, classname) -> bool:
    if (classname or "") in dconst._OVERLAY_CLASSES:
        return True
    n = (name or "").strip().lower()
    return any(s in n for s in dconst._OVERLAY_NAME_SUBSTR)


def is_own_window(name, pid) -> bool:
    if dconst.UI_PID and pid == dconst.UI_PID:
        return True
    # PID is authoritative when Electron supplied it. The exact title fallback
    # covers both the public product spelling and the internal code stem for
    # startup/tests where the UI PID is not available yet.
    return (name or "").strip().casefold() in {"variant-1", "variant1"}


def is_skippable(name, classname, pid) -> bool:
    return (
        is_own_window(name, pid)
        or is_overlay(name, classname)
        or (name or "").strip() in dconst._SHELL_NAMES
    )


def choose_window(candidates):
    """From [{name, ctype, offscreen, pid, area, ctrl}], pick the best real target."""
    best, best_area = None, 0
    for w in candidates:
        if w.get("ctype") not in ("WindowControl", "PaneControl"):
            continue
        if w.get("offscreen"):
            continue
        if is_skippable(w.get("name"), w.get("classname"), w.get("pid")):
            continue
        if w.get("area", 0) > best_area:
            best, best_area = w, w.get("area", 0)
    return best


def target_top(auto):
    """Top-level window to inspect: foreground unless VARIANT-1/overlay, then largest real window."""
    fg = auto.GetForegroundControl()
    top = None
    try:
        top = fg.GetTopLevelControl() if fg else None
    except Exception:
        top = fg
    try:
        if top and not is_skippable(top.Name, top.ClassName, top.ProcessId):
            return top
    except Exception:
        if top:
            return top
    try:
        cands = []
        for w in auto.GetRootControl().GetChildren():
            try:
                r = w.BoundingRectangle
                cands.append({
                    "ctrl": w,
                    "name": w.Name or "",
                    "ctype": w.ControlTypeName,
                    "classname": w.ClassName or "",
                    "offscreen": bool(w.IsOffscreen),
                    "pid": w.ProcessId,
                    "area": max(0, r.right - r.left) * max(0, r.bottom - r.top),
                })
            except Exception:
                continue
        pick = choose_window(cands)
        if pick:
            return pick["ctrl"]
    except Exception:
        pass
    return top
