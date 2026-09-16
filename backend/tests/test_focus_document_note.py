"""Active-document context from focus_window.

Windows 11 Notepad restores the previous session's tabs, so focusing 'Notepad'
often lands on a window showing an existing file. focus_window reports that
fact without prescribing a next action.
"""

from __future__ import annotations

from desktop.targeting import _open_document_note


# ---- focus_window document note ---------------------------------------------

def test_note_fires_for_restored_notepad_tab():
    note = _open_document_note("main.log - Notepad", "Notepad")
    assert note == "\nActive content: 'main.log'."
    assert "Ctrl+N" not in note


def test_note_strips_dirty_marker():
    note = _open_document_note("*report.txt - Notepad", "notepad")
    assert "report.txt" in note


def test_note_silent_for_blank_document():
    assert _open_document_note("Untitled - Notepad", "Notepad") == ""


def test_note_silent_when_doc_matches_query():
    # The user asked for this document — nothing stale about it.
    assert _open_document_note("report.txt - Notepad", "report.txt") == ""


def test_note_silent_without_document_segment():
    assert _open_document_note("Calculator", "Calculator") == ""
