"""Unit tests for modal_detect helpers."""

from __future__ import annotations

import desktop.errors as de
import desktop.modal_detect as md


def test_infer_modal_type_save_as():
    assert md.infer_modal_type("Save As", "", ["Save", "Cancel"]) == md.ModalType.SAVE_AS.value


def test_infer_modal_type_confirmation():
    assert md.infer_modal_type("Confirm delete", "", ["Yes", "No"]) == md.ModalType.CONFIRMATION.value


def test_infer_modal_type_error():
    assert md.infer_modal_type("Error", "Operation failed", ["OK"]) == md.ModalType.ERROR.value


def test_infer_modal_type_permission():
    assert md.infer_modal_type(
        "Allow access?",
        "App wants permission",
        ["Allow", "Deny"],
    ) == md.ModalType.PERMISSION.value


def test_modal_snapshot_signature_stable():
    snap = md.ModalSnapshot(
        present=True,
        modal_type=md.ModalType.CONFIRMATION.value,
        title="Quit?",
        blocking=True,
        confidence="high",
        actions=("OK", "Cancel"),
    )
    assert snap.signature() == "confirmation|Quit?|True|OK,Cancel"


def test_detected_activity_fields_include_modal_type():
    snap = md.ModalSnapshot(
        present=True,
        modal_type=md.ModalType.SAVE_AS.value,
        title="Save As",
        blocking=True,
        confidence="high",
        actions=("Save", "Cancel"),
    )
    fields = md.detected_activity_fields(snap, tool="read_ui")
    assert fields["modal_type"] == "save_as"
    assert fields["blocking"] is True
    assert "Save" in fields["actions"]
    assert "Modal detected" in fields["text"]


def test_dismissed_activity_fields():
    snap = md.ModalSnapshot(present=True, title="Error", modal_type="error")
    fields = md.dismissed_activity_fields(snap)
    assert "dismissed" in fields["text"].lower()
    assert fields["title"] == "Error"


def test_modal_blocking_error_recovery_action():
    snap = md.ModalSnapshot(
        present=True,
        modal_type=md.ModalType.CONFIRMATION.value,
        title="Overwrite?",
        blocking=True,
        confidence="high",
    )
    err = md.modal_blocking_error(snap, tool="read_ui")
    assert err.error_type == de.DesktopErrorType.MODAL_BLOCKING
    assert err.recovery_action == de.RecoveryAction.INTERACT_MODAL.value


def test_format_modal_summary_low_confidence_note():
    snap = md.ModalSnapshot(
        present=True,
        modal_type=md.ModalType.UNKNOWN.value,
        title="Something",
        confidence="low",
        blocking=False,
    )
    text = md.format_modal_summary(snap)
    assert "uncertain" in text.lower()
    assert "computer.observe" in text.lower()


def test_smaller_same_process_document_is_not_inferred_as_modal():
    class Window:
        ProcessId = 42
        NativeWindowHandle = 0
        ClassName = "DocumentWindow"
        ControlTypeName = "WindowControl"

        @staticmethod
        def IsWindowPatternAvailable():
            return False

    assert md._looks_like_modal_overlay(Window(), Window()) is False
