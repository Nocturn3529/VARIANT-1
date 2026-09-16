"""Unit tests for perception_delta incremental UI diffing."""

from __future__ import annotations

import desktop.perception_delta as pd


def _ctrl(
    cid,
    role="Button",
    name="",
    value="",
    state="",
    offscreen=False,
    actionable=True,
):
    return {
        "id": cid,
        "role": role,
        "name": name,
        "value": value,
        "state": state,
        "offscreen": offscreen,
        "actionable": actionable,
    }


def test_compute_delta_added_removed_modified():
    baseline = pd.make_baseline("App", [
        _ctrl(1, "Button", "OK"),
        _ctrl(2, "Edit", "Name", value=""),
    ])
    controls = [
        _ctrl(1, "Button", "OK", state="selected"),
        _ctrl(3, "ListItem", "New"),
    ]
    delta = pd.compute_delta(baseline, controls)
    assert delta.modified_count == 1
    assert delta.added_count == 1
    assert delta.removed_count == 1
    assert delta.total_changes == 3


def test_should_force_full_on_large_delta():
    cfg = pd.PerceptionConfig(max_delta_changes=5, max_delta_ratio=0.5)
    baseline = pd.make_baseline("App", [_ctrl(i, "Button", f"B{i}") for i in range(10)])
    controls = [_ctrl(i, "Button", f"X{i}") for i in range(10)]
    delta = pd.compute_delta(baseline, controls)
    assert pd.should_force_full(delta, len(controls), cfg)


def test_format_delta_no_changes():
    baseline = pd.make_baseline("App", [_ctrl(1, "Button", "OK")])
    controls = [_ctrl(1, "Button", "OK")]
    delta = pd.compute_delta(baseline, controls)
    text = pd.format_delta_output("App", controls, delta)
    assert "no changes" in text.lower()


def test_removed_readable_state_is_not_formatted_as_actionable_id():
    baseline = pd.make_baseline("Calculator", [
        _ctrl(1, "Text", "Display is 1", actionable=False),
    ])
    controls = [
        _ctrl(2, "Text", "Display is 17", actionable=False),
    ]

    delta = pd.compute_delta(baseline, controls)

    assert any(line.startswith('- State Text "Display is 1"') for line in delta.lines)
    assert not any("[1] Text" in line for line in delta.lines)


def test_parse_perception_mode_defaults_full():
    assert pd.parse_perception_mode({}) == "full"
    assert pd.parse_perception_mode({"mode": "incremental"}) == "incremental"
    assert pd.parse_perception_mode({"mode": "incremental"}, cfg=pd.PerceptionConfig()) == "incremental"
    assert pd.parse_perception_mode({}, cfg=pd.PerceptionConfig(incremental_default=True)) == "incremental"
    assert pd.parse_perception_mode({"force_full": True}, cfg=pd.PerceptionConfig(incremental_default=True)) == "full"


def test_incremental_activity_fields():
    baseline = pd.make_baseline("App", [_ctrl(1)])
    delta = pd.compute_delta(baseline, [_ctrl(1), _ctrl(2, name="New")])
    fields = pd.incremental_activity_fields(delta, savings_pct=80, window="App")
    assert fields["added"] == 1
    assert fields["savings_pct"] == 80
    assert "Incremental" in fields["text"]


def test_estimate_savings_positive_on_small_delta():
    baseline = pd.make_baseline("App", [_ctrl(i) for i in range(100)])
    controls = [_ctrl(i) for i in range(100)]
    controls[5] = _ctrl(5, value="changed")
    delta = pd.compute_delta(baseline, controls)
    assert pd.estimate_savings_pct(100, delta) > 50
