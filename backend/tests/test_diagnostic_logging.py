from __future__ import annotations

import tool_runner


def test_desktop_log_includes_action_and_result_preview(capsys):
    tool_runner._log_desktop_outcome(
        "computer",
        {"action": "type", "text": "17"},
        "ok",
        42,
        result="Typed 2 characters into Calculator",
    )

    line = capsys.readouterr().out
    assert "tool=computer" in line
    assert "action=type" in line
    assert "text=17" in line
    assert "ms=42" in line
    assert "result=Typed 2 characters into Calculator" in line


def test_desktop_drag_preview_keeps_both_endpoints():
    preview = tool_runner._desktop_args_preview(
        "computer",
        {"action": "drag", "x": 10, "y": 20, "x2": 30, "y2": 40},
    )

    assert preview == "action=drag x=10 y=20 x2=30 y2=40"


def test_desktop_drag_preview_extracts_endpoints_from_path():
    preview = tool_runner._desktop_args_preview(
        "computer",
        {"action": "drag", "path": [
            {"x": 10, "y": 20},
            {"x": 50, "y": 5},
            {"x": 30, "y": 40},
        ]},
    )

    assert preview == "action=drag x=10 y=20 x2=30 y2=40"
