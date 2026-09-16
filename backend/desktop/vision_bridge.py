"""Window/monitor pixel capture for desktop perception.

Extracted from perception.py (issue #10)."""
from __future__ import annotations

import asyncio
import base64
import platform
from typing import Any

import tools
from . import vision_capture as vc

def physical_rect_for_window(ctx: Any, win) -> tuple[tuple[int, int, int, int] | None, str]:
    """Best-effort physical-pixel crop rect for a UIA window. UIA worker thread."""
    hwnd = 0
    try:
        hwnd = int(getattr(win, "NativeWindowHandle", 0) or 0)
    except Exception:
        hwnd = 0
    iconic = vc.win32_is_iconic(hwnd)
    rect = vc.win32_window_rect(hwnd) if hwnd else None
    if rect is None:
        try:
            r = win.BoundingRectangle
            rect = vc.normalize_rect((r.left, r.top, r.right, r.bottom))
        except Exception:
            rect = None
    ok, why = vc.rect_capturable(rect, iconic=iconic)
    if not ok:
        return None, why
    return rect, ""


def vision_capture_on_uia_thread(ctx: Any, prefer_window: bool = True):
    """Capture pixels, preferring the bound target window when capturable."""
    fallback = "no_lock"
    title = ""
    if prefer_window and ctx.session.target_window is not None:
        alive, dead_reason = ctx._locked_target_alive(ctx.session.target_window)
        title = ctx.session.target_meta.get("title") or ctx._window_title(ctx.session.target_window)
        if alive:
            rect, why = physical_rect_for_window(ctx, ctx.session.target_window)
            if rect:
                try:
                    png = vc.grab_rect_png(*rect)
                    ox, oy = rect[0], rect[1]
                    w, h = rect[2] - rect[0], rect[3] - rect[1]
                    scale = vc.dpi_scale_for_point(ox, oy)
                    return vc.CaptureBundle(
                        png,
                        vc.CaptureMeta(
                            mode="window",
                            origin=(ox, oy),
                            width=w,
                            height=h,
                            window_title=title,
                            dpi_scale=scale,
                        ),
                    )
                except Exception as e:
                    fallback = str(e)[:80]
            else:
                fallback = why or "crop_unavailable"
        else:
            fallback = dead_reason or "invalid_lock"
    # Never had a lock at all: crop to the FOREGROUND window (the same picker
    # UIA perception reads, skipping VARIANT-1's own windows) before settling for
    # a whole monitor — in a full-monitor frame a small outcome (one new chat
    # line) is easy to miss in a whole-monitor observation (observed live
    # 2026-07-14, Discord DM send). Lock-LOSS
    # reasons still fall through to monitor mode so vision_capture's
    # refocus-and-recapture recovery can restore the locked crop instead.
    if prefer_window and fallback == "no_lock" and ctx.session.target_window is None:
        top = None
        try:
            top = ctx._target_top(ctx._load_uia())
        except Exception:
            top = None
        if top is not None:
            rect, _why = physical_rect_for_window(ctx, top)
            if rect:
                try:
                    png = vc.grab_rect_png(*rect)
                    ox, oy = rect[0], rect[1]
                    w, h = rect[2] - rect[0], rect[3] - rect[1]
                    scale = vc.dpi_scale_for_point(ox, oy)
                    return vc.CaptureBundle(
                        png,
                        vc.CaptureMeta(
                            mode="window",
                            origin=(ox, oy),
                            width=w,
                            height=h,
                            window_title=ctx._window_title(top),
                            fallback_reason=fallback,
                            dpi_scale=scale,
                        ),
                    )
                except Exception as e:
                    fallback = str(e)[:80]
    if prefer_window:
        # Preserve the failed scope for one bounded re-resolve attempt, but do
        # not present unrelated monitor pixels as the bound window's evidence.
        return vc.CaptureBundle(b"", vc.CaptureMeta(
            mode="unavailable", origin=(0, 0), window_title=title,
            fallback_reason=fallback,
        ))
    # Explicit monitor capture only. Foreground can belong
    # to an unrelated app or sibling document and is not capture authority.
    saved = ctx.session.window_stack.get_current()
    saved_hwnd = int(
        (ctx.session.scope or {}).get("fabric_strict_hwnd")
        or (getattr(saved, "hwnd", 0) if saved is not None else 0)
        or 0
    )
    locked_monitor = (
        vc.grab_monitor_for_hwnd_png(saved_hwnd) if saved_hwnd else None
    )
    if locked_monitor is not None:
        png, origin, monitor = locked_monitor
    else:
        png, origin, monitor = vc.grab_active_monitor_png()
    scale = vc.dpi_scale_for_point(origin[0], origin[1])
    return vc.CaptureBundle(
        png,
        vc.CaptureMeta(
            mode="monitor",
            origin=origin,
            fallback_reason=fallback,
            dpi_scale=scale,
            monitor=monitor,
        ),
    )


# Capture fallback reasons worth one re-resolve attempt before settling for a
# full-monitor screenshot: the locked window probably still exists, we just lost
# our held reference to it (a stale UIA element is common right after the target
# window's own UI re-renders -- e.g. right after sending a chat message, when the
# message list updates). "minimized" / "off_screen" / "degenerate_bounds" are NOT
# included here -- refocusing can't fix a window that genuinely isn't capturable.
RECOVERABLE_CAPTURE_FALLBACKS = frozenset({
    "no_lock", "invalid_lock", "window_closed", "handle_invalid", "no_handle",
})


async def vision_capture(ctx: Any, prefer_window: bool = True):
    """Window-aware PNG capture for the computer observation surface."""
    bundle = await ctx._uia(lambda: ctx._vision_capture_on_uia_thread(prefer_window))
    if (prefer_window and bundle.meta.mode == "unavailable"
            and bundle.meta.fallback_reason in RECOVERABLE_CAPTURE_FALLBACKS):
        ok, _msg = await ctx.refocus_locked_window()
        if ok:
            bundle = await ctx._uia(lambda: ctx._vision_capture_on_uia_thread(prefer_window))
    sess = ctx.session
    sess.last_vision_meta = bundle.meta
    sess.mark_updated()
    # Exact capture scope remains in the desktop event/trace ledger. Only
    # fallbacks/errors are promoted into the concise operational log.
    await ctx._emit_capture_scope(bundle.meta)
    if not bundle.png:
        raise tools.ToolError("The target window could not be captured", code="desktop_capture_unavailable")
    return bundle


async def capture_raw_b64_async(ctx: Any) -> str | None:
    """RAM-only PNG as base64 for multimodal desktop observations."""
    try:
        bundle = await ctx._vision_capture(prefer_window=True)
        return base64.b64encode(bundle.png).decode("ascii")
    except Exception:
        return None


async def attach_visual_state(
    ctx: Any,
    *,
    tool: str,
    controls: int = 0,
) -> str:
    """Attach the bound window's current pixels to the next model request.

    This is part of the observation returned by canvas actions, not a separate
    model instruction. Provider/model image routing is owned by the model loop;
    capture must not pre-emptively discard pixels based on coarse route flags.
    """
    if platform.system() != "Windows":
        return ""
    try:
        bundle = await vision_capture(ctx, prefer_window=True)
    except Exception:
        return ""
    ctx._deliver_image(base64.b64encode(bundle.png).decode("ascii"))
    await ctx._emit_perception_quality_metrics(
        tool=tool, window=bundle.meta.window_title, step="capture",
        controls=int(controls or 0), include_capture=True,
        include_modal=True, include_stack=True,
    )
    return f"Visual state attached. {vc.capture_event_text(bundle.meta)}."


async def screenshot(ctx: Any, args):
    """Capture the current target and attach it to the next model turn."""
    if platform.system() != "Windows":
        raise tools.ToolError("computer.observe screenshots are Windows-only.")

    bundle = await vision_capture(ctx, prefer_window=True)
    ctx._deliver_image(base64.b64encode(bundle.png).decode("ascii"))
    await ctx._emit_perception_quality_metrics(
        tool="computer", window=bundle.meta.window_title, step="capture",
        controls=0, include_capture=True,
        include_modal=True, include_stack=True,
    )
    scope = vc.capture_event_text(bundle.meta)
    return (
        f"Screenshot captured for visual inspection. {scope}. "
        "The image is attached to the next model turn."
    )


