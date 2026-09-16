"""Bound-window capture may recover its target but never widen to the monitor."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tools import ToolError

import desktop.context as dcontext
import desktop.vision_bridge as dvision
import desktop_control as dc
from desktop import vision_capture as vc


def _bundle(mode: str, fallback_reason: str = "", window_title: str = "") -> vc.CaptureBundle:
    return vc.CaptureBundle(
        png=b"" if mode == 'unavailable' else b"fakepng",
        meta=vc.CaptureMeta(mode=mode, fallback_reason=fallback_reason, window_title=window_title),
    )


async def _fake_uia(self, fn):
    """Stand-in for DesktopControlContext._uia: just run fn() (no real thread/session)."""
    return fn()


@pytest.mark.asyncio
async def test_recoverable_fallback_retries_after_successful_refocus():
    captures = [
        _bundle("unavailable", fallback_reason="invalid_lock"),
        _bundle("window", window_title="Discord"),
    ]

    def fake_capture_on_thread(self, prefer_window=True):
        return captures.pop(0)

    refocus = AsyncMock(return_value=(True, "restored 'Discord'"))
    with (
        patch.object(dcontext.DesktopControlContext, "_uia", new=_fake_uia),
        patch.object(dcontext.DesktopControlContext, "_vision_capture_on_uia_thread", fake_capture_on_thread),
        patch.object(dcontext.DesktopControlContext, "refocus_locked_window", new=refocus),
        patch.object(dcontext.DesktopControlContext, "_emit_capture_scope", new=AsyncMock()),
    ):
        bundle = await dvision.vision_capture(dc._control_context(), prefer_window=True)

    assert bundle.meta.mode == "window"
    assert bundle.meta.window_title == "Discord"
    refocus.assert_awaited_once()


@pytest.mark.asyncio
async def test_recoverable_fallback_remains_unavailable_when_refocus_fails():
    """A lost bound target must not substitute a full-monitor capture."""
    only_capture = _bundle("unavailable", fallback_reason="window_closed")
    calls = []

    def fake_capture_on_thread(self, prefer_window=True):
        calls.append(1)
        return only_capture

    refocus = AsyncMock(return_value=(False, "window not found"))
    with (
        patch.object(dcontext.DesktopControlContext, "_uia", new=_fake_uia),
        patch.object(dcontext.DesktopControlContext, "_vision_capture_on_uia_thread", fake_capture_on_thread),
        patch.object(dcontext.DesktopControlContext, "refocus_locked_window", new=refocus),
        patch.object(dcontext.DesktopControlContext, "_emit_capture_scope", new=AsyncMock()),
    ):
        with pytest.raises(ToolError, match='could not be captured'):
            await dvision.vision_capture(dc._control_context(), prefer_window=True)
    assert len(calls) == 1, "must not recapture when refocus failed"
    refocus.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_recoverable_fallback_skips_refocus_entirely():
    """A minimized/off-screen window can't be fixed by refocusing -- don't waste
    a re-resolve attempt (and don't risk an unwanted foreground-window steal)."""
    only_capture = _bundle("monitor", fallback_reason="minimized")

    refocus = AsyncMock(return_value=(True, "restored"))
    with (
        patch.object(dcontext.DesktopControlContext, "_uia", new=_fake_uia),
        patch.object(dcontext.DesktopControlContext, "_vision_capture_on_uia_thread", return_value=only_capture),
        patch.object(dcontext.DesktopControlContext, "refocus_locked_window", new=refocus),
        patch.object(dcontext.DesktopControlContext, "_emit_capture_scope", new=AsyncMock()),
    ):
        bundle = await dvision.vision_capture(dc._control_context(), prefer_window=True)

    assert bundle.meta.mode == "monitor"
    refocus.assert_not_awaited()


@pytest.mark.asyncio
async def test_window_mode_capture_never_triggers_refocus():
    """The common, already-working case -- a clean window crop -- must not pay
    for a refocus check at all."""
    only_capture = _bundle("window", window_title="Notepad")

    refocus = AsyncMock(return_value=(True, "restored"))
    with (
        patch.object(dcontext.DesktopControlContext, "_uia", new=_fake_uia),
        patch.object(dcontext.DesktopControlContext, "_vision_capture_on_uia_thread", return_value=only_capture),
        patch.object(dcontext.DesktopControlContext, "refocus_locked_window", new=refocus),
        patch.object(dcontext.DesktopControlContext, "_emit_capture_scope", new=AsyncMock()),
    ):
        bundle = await dvision.vision_capture(dc._control_context(), prefer_window=True)

    assert bundle.meta.mode == "window"
    refocus.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefer_window_false_skips_refocus_even_on_recoverable_fallback():
    """When the caller explicitly didn't ask for a window-preferring capture,
    don't second-guess it with a refocus attempt."""
    only_capture = _bundle("monitor", fallback_reason="no_lock")

    refocus = AsyncMock(return_value=(True, "restored"))
    with (
        patch.object(dcontext.DesktopControlContext, "_uia", new=_fake_uia),
        patch.object(dcontext.DesktopControlContext, "_vision_capture_on_uia_thread", return_value=only_capture),
        patch.object(dcontext.DesktopControlContext, "refocus_locked_window", new=refocus),
        patch.object(dcontext.DesktopControlContext, "_emit_capture_scope", new=AsyncMock()),
    ):
        bundle = await dvision.vision_capture(dc._control_context(), prefer_window=False)

    assert bundle.meta.mode == "monitor"
    refocus.assert_not_awaited()
