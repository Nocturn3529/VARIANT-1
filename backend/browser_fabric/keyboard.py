"""Normalize common browser key names without changing printable characters."""

from __future__ import annotations

from .models import BrowserValidationError


_NAMED_KEYS = {
    name.casefold(): name
    for name in (
        "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End",
        "PageUp", "PageDown", "Enter", "Escape", "Backspace", "Delete",
        "Insert", "Tab", "Space", "CapsLock", "NumLock", "ScrollLock",
        "Pause", "PrintScreen", "Alt", "Control", "Meta", "Shift",
        "ControlOrMeta", *(f"F{index}" for index in range(1, 25)),
    )
}
_NAMED_KEYS.update({
    "left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp",
    "down": "ArrowDown", "return": "Enter", "esc": "Escape",
    "del": "Delete", "spacebar": "Space", "pgup": "PageUp",
    "pgdn": "PageDown", "ctrl": "Control", "cmd": "Meta",
    "command": "Meta", "option": "Alt",
})
_MODIFIERS = {
    "alt": "Alt", "option": "Alt", "control": "Control", "ctrl": "Control",
    "meta": "Meta", "cmd": "Meta", "command": "Meta", "shift": "Shift",
    "controlormeta": "ControlOrMeta",
}

BROWSER_KEYS_GUIDANCE = (
    "Use names such as Home, ArrowRight and Enter, a character, or a chord "
    "such as Control+L. Common named keys/modifiers accept case-insensitive "
    "aliases; printable characters preserve case. '+' is a character and "
    "Control++ is a chord. Use fill for text."
)


def normalize_browser_keys(value: str) -> str:
    """Keep the shared public spelling; adapters translate native key codes.

    Unknown names remain adapter-owned so this convenience normalization does
    not narrow managed Playwright's additional key support. A plus is a chord
    separator only after a nonempty token, preserving the literal plus key.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        raise BrowserValidationError("keys must contain between 1 and 500 characters")
    tokens: list[str] = []
    token = ""
    for character in value:
        if character == "+" and token:
            tokens.append(token)
            token = ""
        else:
            token += character
    tokens.append(token)
    if any(not part for part in tokens):
        raise BrowserValidationError(
            "keys needs a final key, for example ArrowRight, Control+L or Control++"
        )
    # Direction/location flags in a modifier position belong to the adapter;
    # only the final key may use Left/Right as an arrow-name alias.
    modifiers = [_MODIFIERS.get(part.casefold(), part) for part in tokens[:-1]]
    key = tokens[-1]
    if len(key) != 1:
        key = _NAMED_KEYS.get(key.casefold(), key)
    return "+".join([*modifiers, key])
