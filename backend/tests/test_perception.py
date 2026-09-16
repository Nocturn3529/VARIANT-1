"""Integrated UIA perception pipeline tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import desktop.context as dcontext
import desktop_control as dc
import desktop.errors as derr
import desktop.perception_delta as pd
import desktop.perception_recovery as prec
import desktop.tree as dtree
from support.mock_uia import window_tree
from support.snapshots import collect_snapshot


def _make_controls(n: int, prefix: str = "Btn"):
    return [
        {
            "id": i,
            "key": f"rn:Button|{prefix}{i}#{i}",
            "role": "Button",
            "name": f"{prefix}{i}",
            "value": "",
            "state": "",
            "offscreen": False,
            "bounds": [0, 0, 10, 10],
            "control": None,
        }
        for i in range(1, n + 1)
    ]


@pytest.mark.asyncio
async def test_format_perception_output_incremental_mode():
    controls = _make_controls(5)
    ctx = dc._control_context()
    dtree.set_incremental_baseline(ctx, "App", controls)
    mutated = list(controls)
    mutated[0] = {**mutated[0], "value": "changed"}

    with patch.object(dcontext.DesktopControlContext, "_emit_perception", new=AsyncMock()):
        body = await dtree.format_perception_output(
            ctx, "App", mutated, {"mode": "incremental"}, tool_name="focus_window",
        )

    assert "incremental update" in body.lower()
    assert "change" in body.lower()


@pytest.mark.asyncio
async def test_format_perception_output_force_full_skips_delta():
    controls = _make_controls(3)
    ctx = dc._control_context()
    dtree.set_incremental_baseline(ctx, "App", controls)

    with patch.object(dcontext.DesktopControlContext, "_emit_perception", new=AsyncMock()):
        body = await dtree.format_perception_output(
            ctx, "App", controls, {"mode": "incremental"}, force_full=True,
        )

    assert "incremental update" not in body.lower()
    assert "[1]" in body


def test_low_control_count_is_poor_yield():
    low = derr.from_control_count(1, 3, tool="read_ui")
    assert low.error_type == derr.DesktopErrorType.LOW_YIELD
    assert prec.is_poor_yield(1, prec.RecoveryConfig(min_controls_for_uia=3))


def test_incremental_baseline_invalidated_on_window_mismatch():
    baseline = pd.make_baseline("App A", _make_controls(2))
    assert baseline.matches_window("App B") is False
    assert baseline.matches_window("App A") is True


@pytest.mark.asyncio
async def test_collect_and_format_roundtrip_golden():
    auto, win = window_tree({
        "name": "Notepad",
        "role": "WindowControl",
        "children": [
            {"role": "Button", "name": "Save", "automation_id": "saveBtn"},
            {"role": "Edit", "name": "", "value": "hello world"},
        ],
    })
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        title, controls = dtree.collect_controls(dc._control_context(), auto, top=win)
    snap = collect_snapshot(title, controls)
    assert snap["title"] == "Notepad"
    assert len(snap["controls"]) == 2

    text = dc._control_context().format_controls(controls, title)
    assert "Save" in text
    assert "hello world" in text


@pytest.mark.asyncio
async def test_collect_controls_exposes_readable_state_without_action_id():
    auto, win = window_tree({
        "name": "Calculator",
        "role": "WindowControl",
        "children": [
            {"role": "Button", "name": "One", "automation_id": "num1Button"},
            {"role": "Text", "name": "Display is 17", "automation_id": "CalculatorResults"},
        ],
    })
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        title, controls = dtree.collect_controls(dc._control_context(), auto, top=win)

    button = next(c for c in controls if c["role"] == "Button")
    display = next(c for c in controls if c["role"] == "Text")
    assert display["actionable"] is False
    assert button["id"] > 0
    assert display["id"] < 0

    text = dc._control_context().format_controls(controls, title)
    assert f'[{button["id"]}] Button "One"' in text
    assert 'State Text "Display is 17"' in text
    assert '] Text "Display is 17"' not in text


@pytest.mark.asyncio
async def test_readable_state_does_not_shift_or_duplicate_action_ids():
    auto, win = window_tree({
        "name": "Calculator",
        "role": "WindowControl",
        "children": [
            {"role": "Button", "name": "One", "automation_id": "num1Button"},
            {"role": "Text", "name": "1", "automation_id": "num1Label"},
            {"role": "Button", "name": "Seven", "automation_id": "num7Button"},
            {"role": "Text", "name": "7", "automation_id": "num7Label"},
            {"role": "Text", "name": "Display is 17", "automation_id": "CalculatorResults"},
        ],
    })
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        title, controls = dtree.collect_controls(dc._control_context(), auto, top=win)

    buttons = [c for c in controls if c["actionable"]]
    assert [(c["id"], c["name"]) for c in buttons] == [
        (1, "One"),
        (2, "Seven"),
    ]

    text = dc._control_context().format_controls(controls, title)
    assert '[1] Button "One"' in text
    assert '[2] Button "Seven"' in text
    assert 'State Text "Display is 17"' in text
    assert 'State Text "1"' not in text
    assert 'State Text "7"' not in text


@pytest.mark.asyncio
async def test_collect_controls_surfaces_drawing_canvas():
    """Paint's canvas is a Group the actionable-role filter used to drop, so
    the model guessed stroke coordinates and pressed on the ribbon (live
    2026-07-08). It must appear as role Canvas with its rect in the text."""
    auto, win = window_tree({
        "name": "Untitled - Paint",
        "role": "WindowControl",
        "children": [
            {"role": "Button", "name": "Pencil", "automation_id": "PencilTool"},
            {"role": "Group", "name": "Using Brush tool on Canvas",
             "automation_id": "image", "bounds": [214, 256, 1706, 1079]},
            {"role": "Group", "name": "Brushes"},   # non-canvas group stays excluded
        ],
    })
    with patch("desktop.modal_detect.scan_modal_uia", return_value=None):
        title, controls = dtree.collect_controls(dc._control_context(), auto, top=win)

    canvases = [c for c in controls if c["role"] == "Canvas"]
    assert len(canvases) == 1
    assert canvases[0]["bounds"] == [214, 256, 1706, 1079]
    assert not any(c["name"] == "Brushes" for c in controls)

    text = dc._control_context().format_controls(controls, title)
    assert "CANVAS VIEW" in text
    assert "(214,256)-(1706,1079)" in text
    assert "non-drawable workspace" in text
