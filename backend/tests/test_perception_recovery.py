"""Unit tests for deterministic perception configuration and events."""

from __future__ import annotations

import desktop.perception_recovery as prec


def test_merge_recovery_config_defaults():
    cfg = prec.merge_recovery_config(None)
    assert cfg.reread_enabled is True
    assert cfg.min_controls_for_uia == prec.DEFAULT_MIN_CONTROLS_FOR_UIA
    assert cfg.reread_wait_sec == prec.DEFAULT_REREAD_WAIT_SEC


def test_merge_recovery_config_overrides_and_clamps():
    cfg = prec.merge_recovery_config({
        "reread_enabled": False,
        "min_controls_for_uia": -5,
        "reread_wait_sec": -1,
        "micro_verify_radius_px": 2,
    })
    assert cfg.reread_enabled is False
    assert cfg.min_controls_for_uia == 0
    assert cfg.reread_wait_sec == 0.0
    assert not hasattr(cfg, "micro_verify_radius_px")


def test_is_poor_yield_threshold():
    cfg = prec.RecoveryConfig(min_controls_for_uia=3)
    assert prec.is_poor_yield(0, cfg) is True
    assert prec.is_poor_yield(2, cfg) is True
    assert prec.is_poor_yield(3, cfg) is False


def test_should_skip_reread_flags():
    assert prec.should_skip_reread({}) is False
    assert prec.should_skip_reread({"skip_reread": True}) is True
    assert prec.should_skip_reread({"skip_reread": "yes"}) is True
    assert prec.should_skip_reread({"_reread_internal": True}) is True


def test_reread_note_is_neutral_evidence():
    assert "UIA reread recovered" in prec.reread_note(4, recovered=True)
    note = prec.reread_note(1, recovered=False)
    assert "UIA reread remained sparse" in note
    assert "click" not in note.lower()
    assert "ground" not in note.lower()


def test_perception_event_text_low_yield():
    text = prec.perception_event_text("perception:low_yield", {"controls": 1})
    assert "low yield" in text.lower()
    assert "one bounded reread" in text.lower()


def test_activity_fields_has_no_stage_contract():
    fields = prec.activity_fields("perception:low_yield", controls=1)
    assert fields["controls"] == 1
    assert "stage" not in fields
    assert fields["text"]


def test_focus_loss_event_and_warning():
    fields = prec.focus_loss_activity_fields(
        last_title="Notepad",
        reason="window_closed",
        fallback_title="Chrome",
        query="notepad",
    )
    assert fields["reason"] == "window_closed"
    assert "Notepad" in fields["text"]
    assert "Chrome" in fields["text"]
    warning = prec.focus_loss_warning_text(
        "Notepad", "window_closed", "Chrome", query="notepad",
    )
    assert "TARGET LOST" in warning
    assert "computer.focus" in warning.lower()


def test_focus_restored_activity_fields():
    fields = prec.focus_restored_activity_fields(title="Discord", query="discord")
    assert "Discord" in fields["text"]
    assert fields["title"] == "Discord"
