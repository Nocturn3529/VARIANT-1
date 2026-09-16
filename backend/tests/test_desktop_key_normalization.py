"""Keyboard shortcut normalization for desktop control."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from desktop.input_primitives import _normalize_keys_for_send, _send_keys_direct
import tools


def test_plus_style_shortcut_removes_literal_spaces_before_sendkeys():
    assert _normalize_keys_for_send("{Control} + {Shift} + {N}") == "{Ctrl}{Shift}n"


def test_plus_style_shortcut_accepts_unbraced_model_spelling():
    assert _normalize_keys_for_send("Ctrl + Shift + N") == "{Ctrl}{Shift}n"


def test_existing_sendkeys_syntax_is_preserved():
    assert _normalize_keys_for_send("{Ctrl}{End}") == "{Ctrl}{End}"
    assert _normalize_keys_for_send("{Enter}") == "{Enter}"


def test_unbraced_named_keys_and_sequences_are_normalized():
    assert _normalize_keys_for_send("ENTER") == "{Enter}"
    assert _normalize_keys_for_send("HOME") == "{Home}"
    assert _normalize_keys_for_send("ARROWDOWN") == "{Down}"
    assert _normalize_keys_for_send(["HOME", *(["ARROWDOWN"] * 5)]) == (
        "{Home}{Down}{Down}{Down}{Down}{Down}"
    )


def test_direct_variant1_mic_hotkey_is_not_a_harness_restriction():
    assert _normalize_keys_for_send("Ctrl + Space") == "{Ctrl}{Space}"
    assert _normalize_keys_for_send("{Ctrl}{Space}") == "{Ctrl}{Space}"


@pytest.mark.parametrize("keys,expected", [
    ("WIN+R", "{Win}r"), ("Windows + E", "{Win}e"),
    ("META+R", "{Win}r"), ("SUPER+R", "{Win}r"),
    ("LWIN+R", "{LWin}r"), ("RWIN+R", "{RWin}r"),
    ("WIN", "{Win}"), (["WIN", "r"], "{Win}r"),
    ("CTRL++", "{Ctrl}{+}"), ("CTRL+PLUS", "{Ctrl}{+}"),
    ("CTRL+Insert", "{Ctrl}{Insert}"),
])
def test_windows_chords_and_common_keys_are_normalized_before_dispatch(keys, expected):
    sent = []
    result = _send_keys_direct(SimpleNamespace(SendKeys=lambda text, **_: sent.append(text)), keys)
    assert sent == [expected]
    assert result == {"keys_sent": expected}


@pytest.mark.parametrize("keys", [
    "UNKNOWN+R", "CTRL+MISSPELLED", "WIN+{MISSPELLED}", "CTRL+", "CTRL+++",
    "MISSPELLED", ["CTRL", "MISSPELLED"], ["ENTER", "UNKNOWN+R"],
    "{MISSPELLED}", "{ENTER}{MISSPELLED}", "{Ctrl", "{Ctrl}}",
])
def test_invalid_key_input_is_rejected_before_any_input_is_sent(keys):
    sent = []
    with pytest.raises(tools.ToolError, match="Unsupported key or chord"):
        _send_keys_direct(SimpleNamespace(SendKeys=lambda text, **_: sent.append(text)), keys)
    assert sent == []


def test_native_grammar_is_validated_against_the_installed_key_catalog():
    sent = []
    auto = SimpleNamespace(SpecialKeyNames={"CTRL": 17, "BROWSERBACK": 166},
                           SendKeys=lambda text, **_: sent.append(text))
    assert _send_keys_direct(auto, "{Ctrl}{BrowserBack}") == {
        "keys_sent": "{Ctrl}{BrowserBack}"}
    assert sent == ["{Ctrl}{BrowserBack}"]


def test_win_r_round_trips_through_installed_parser_without_literal_text(monkeypatch):
    uiautomation = pytest.importorskip("uiautomation")
    parser = sys.modules[uiautomation.SendKeys.__module__]
    events, literal = [], []
    # Stub both native dispatch branches: this fixture never sends desktop input.
    monkeypatch.setattr(parser, "keybd_event", lambda key, scan, flags, extra: events.append((key, flags)))
    monkeypatch.setattr(parser, "SendUnicodeChar", lambda char, *args: literal.append(char))
    monkeypatch.setattr(parser, "_VKtoSC", lambda key: key)
    monkeypatch.setattr(parser.time, "sleep", lambda _seconds: None)
    _send_keys_direct(uiautomation, "WIN+R")
    assert not literal
    assert [key for key, flags in events] == [parser.SpecialKeyNames["WIN"], ord("R"), ord("R"), parser.SpecialKeyNames["WIN"]]
    assert [bool(flags & 2) for key, flags in events] == [False, False, True, True]


@pytest.mark.parametrize("spelling", ["BACKSPACE", "{Backspace}", "BACK", "{Back}"])
def test_backspace_alias_round_trips_through_installed_sendkeys_parser(
    monkeypatch,
    spelling,
):
    uiautomation = pytest.importorskip("uiautomation")
    parser_module = sys.modules[uiautomation.SendKeys.__module__]
    events = []
    monkeypatch.setattr(
        parser_module,
        "keybd_event",
        lambda key, scan, flags, extra: events.append(
            (key, scan, flags, extra)
        ),
    )
    monkeypatch.setattr(parser_module, "_VKtoSC", lambda key: key)
    monkeypatch.setattr(parser_module.time, "sleep", lambda _seconds: None)

    receipt = _send_keys_direct(uiautomation, spelling)

    assert receipt == {"keys_sent": "{Back}"}
    assert len(events) == 2
    assert {event[0] for event in events} == {
        parser_module.SpecialKeyNames["BACK"]
    }
