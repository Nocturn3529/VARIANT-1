import pytest

from browser_fabric.keyboard import normalize_browser_keys
from browser_fabric.models import BrowserValidationError


@pytest.mark.parametrize(("received", "canonical"), [
    ("HOME", "Home"), ("ARROWRIGHT", "ArrowRight"),
    ("right", "ArrowRight"), ("arrowleft", "ArrowLeft"),
    ("CTRL+ARROWDOWN", "Control+ArrowDown"),
    ("ctrl+shift+A", "Control+Shift+A"),
    ("controlormeta+L", "ControlOrMeta+L"),
    ("cmd+f", "Meta+f"), ("return", "Enter"),
    ("a", "a"), ("A", "A"), (" ", " "), ("+", "+"),
    ("Control++", "Control++"), ("ctrl+ ", "Control+ "),
    ("Shift++", "Shift++"), ("KeyA", "KeyA"),
    ("Shift+KeyA", "Shift+KeyA"), ("Digit1", "Digit1"),
    ("left+ctrl+A", "left+Control+A"),
    ("iskeypad+right", "iskeypad+ArrowRight"),
    ("numlock+num1", "numlock+num1"),
    ("é", "é"), ("UnrecognizedName", "UnrecognizedName"),
])
def test_common_aliases_preserve_characters_and_adapter_extensions(received, canonical):
    assert normalize_browser_keys(received) == canonical


@pytest.mark.parametrize("received", [None, [], "", "Control+", "Control+++", "x" * 501])
def test_invalid_key_shapes_fail_before_dispatch(received):
    with pytest.raises(BrowserValidationError):
        normalize_browser_keys(received)
