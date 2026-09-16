"""Unit tests for the ``desktop.errors`` taxonomy."""

from __future__ import annotations

import desktop.errors as de


def test_from_control_count_empty_vs_low():
    empty = de.from_control_count(0, 3)
    assert empty.error_type == de.DesktopErrorType.EMPTY_TREE
    assert empty.recovery_action == de.RecoveryAction.REREAD.value

    low = de.from_control_count(2, 3)
    assert low.error_type == de.DesktopErrorType.LOW_YIELD


def test_from_lock_reason_mapping():
    closed = de.from_lock_reason("window_closed", window="Notepad")
    assert closed.error_type == de.DesktopErrorType.WINDOW_CLOSED

    stale = de.from_lock_reason("handle_invalid", window="Chrome")
    assert stale.error_type == de.DesktopErrorType.STALE_HANDLE


def test_error_tag_roundtrip():
    tag = de.error_tag(de.DesktopErrorType.FOCUS_LOST)
    assert "[desktop_error:FOCUS_LOST]" in tag
    assert de.parse_error_tag("result\n[desktop_error:FOCUS_LOST]") == de.DesktopErrorType.FOCUS_LOST


def test_activity_fields_include_error_type():
    err = de.from_control_count(1, 3, window="App")
    fields = de.activity_fields(err)
    assert fields["error_type"] == "LOW_YIELD"
    assert fields["recovery_action"] == de.RecoveryAction.REREAD.value
    assert fields["severity"] == de.ErrorSeverity.RECOVERABLE.value
    assert fields["is_recoverable"] is True
    assert "LOW_YIELD" in fields["text"]
    assert "stage" not in fields


def test_modal_blocking_recovery_action():
    err = de.modal_blocking_error(modal_type="confirmation", title="Quit?")
    assert err.error_type == de.DesktopErrorType.MODAL_BLOCKING
    assert err.recovery_action == de.RecoveryAction.INTERACT_MODAL.value


def test_tool_unavailable_error_is_not_recoverable():
    unavail = de.tool_unavailable_error("disabled", tool="read_ui")
    assert unavail.error_type == de.DesktopErrorType.TOOL_UNAVAILABLE
    assert unavail.severity == de.ErrorSeverity.UNAVAILABLE
    assert unavail.is_recoverable is False
