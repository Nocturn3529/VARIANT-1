"""Unit tests for vision_capture helpers."""

from __future__ import annotations

import os
import sys

import pytest

from desktop import vision_capture as vc


def test_normalize_rect_valid():
    assert vc.normalize_rect((10, 20, 200, 400)) == (10, 20, 200, 400)


def test_normalize_rect_rejects_small():
    assert vc.normalize_rect((0, 0, 5, 5)) is None


def test_rect_capturable_minimized():
    ok, why = vc.rect_capturable((0, 0, 400, 300), iconic=True)
    assert ok is False
    assert why == "minimized"


def test_rect_capturable_off_screen():
    ok, why = vc.rect_capturable((-32000, -32000, -31000, -31000))
    assert ok is False
    assert why == "off_screen"


def test_capture_event_text_window_mode():
    meta = vc.CaptureMeta(
        mode="window",
        origin=(100, 50),
        width=800,
        height=600,
        window_title="Notepad",
    )
    text = vc.capture_event_text(meta)
    assert "cropped" in text.lower()
    assert "Notepad" in text


def test_capture_event_text_fallback():
    meta = vc.CaptureMeta(mode="monitor", fallback_reason="minimized")
    text = vc.capture_event_text(meta)
    assert "fallback" in text.lower()
    assert "minimized" in text


def test_capture_event_text_names_the_monitor():
    meta = vc.CaptureMeta(mode="monitor", fallback_reason="no_lock", monitor="active")
    text = vc.capture_event_text(meta)
    assert "active monitor" in text
    assert "no_lock" in text


@pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"),
    reason="mss active-monitor capture needs Win32 or a DISPLAY",
)
def test_grab_active_monitor_reports_scope():
    """Fallback capture targets the monitor with the foreground window (multi-
    display: the task window is often not on the primary monitor)."""
    png, origin, monitor = vc.grab_active_monitor_png()
    assert png[:4] == b"\x89PNG"
    assert monitor in ("active", "primary")
    assert isinstance(origin, tuple) and len(origin) == 2

