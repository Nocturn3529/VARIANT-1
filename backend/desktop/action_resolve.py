"""Private action gates, target activation, and live-control checks."""

from __future__ import annotations

import platform

import tools

from . import errors as derr


def _ensure_gates(ctx):
    if platform.system() != "Windows":
        raise ctx._tagged_tool_error(
            "desktop control is Windows-only.",
            derr.tool_unavailable_error("Windows-only", tool="desktop"),
        )


def _ensure_live(ctx, auto, record):
    control = record.get("control")
    try:
        if control is not None and control.Exists(0, 0):
            return control
    except Exception:
        pass
    key = record.get("key")
    if key:
        try:
            title, controls = ctx._collect_controls(
                auto, max_controls=ctx.MAX_CONTROLS)
            ctx._store_snapshot(title, controls)
            for candidate in controls:
                if candidate.get("key") == key:
                    record["control"] = candidate["control"]
                    return candidate["control"]
        except Exception:
            pass
    element_id = record.get("id", "")
    window = getattr(ctx.session, "snapshot_title", "") or ""
    error = derr.DesktopError(
        error_type=derr.DesktopErrorType.STALE_HANDLE,
        detail=f"control [{element_id}] could not be re-resolved",
        tool="desktop",
        window=window,
        recovery_action=derr.RecoveryAction.REFOCUS.value,
    )
    raise ctx._tagged_tool_error(
        f"control [{element_id}] is stale; input was not sent.", error)


__all__ = [
    "_ensure_gates",
    "_ensure_live",
]
