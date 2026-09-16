"""Private SendInput primitives used by the single Desktop Fabric adapter.

These functions deliver input only. Desktop Fabric owns the one pre/post state
observation and all durable receipts, so the driver never captures, verifies,
or rereads the UI after an action.
"""

from __future__ import annotations

import math
import re
import time

import tools



_PLUS_SHORTCUT_MODIFIERS = frozenset({
    "ctrl", "control", "shift", "alt", "option", "cmd", "command",
    "commandorcontrol", "win", "windows", "meta", "super", "lwin", "rwin",
})
_KEY_TOKEN_ALIASES = {
    "ctrl": "{Ctrl}",
    "control": "{Ctrl}",
    "command": "{Ctrl}",
    "cmd": "{Ctrl}",
    "commandorcontrol": "{Ctrl}",
    "win": "{Win}",
    "windows": "{Win}",
    "meta": "{Win}",
    "super": "{Win}",
    "lwin": "{LWin}",
    "rwin": "{RWin}",
    "shift": "{Shift}",
    "alt": "{Alt}",
    "option": "{Alt}",
    "enter": "{Enter}",
    "return": "{Enter}",
    "tab": "{Tab}",
    "escape": "{Escape}",
    "esc": "{Escape}",
    "delete": "{Delete}",
    "del": "{Delete}",
    "backspace": "{Back}",
    "back": "{Back}",
    "space": "{Space}",
    "spacebar": "{Space}",
    "home": "{Home}",
    "end": "{End}",
    "insert": "{Insert}",
    "ins": "{Insert}",
    "plus": "{+}",
    "minus": "{-}",
    "pageup": "{PageUp}",
    "pgup": "{PageUp}",
    "pagedown": "{PageDown}",
    "pgdn": "{PageDown}",
    "up": "{Up}",
    "arrowup": "{Up}",
    "down": "{Down}",
    "arrowdown": "{Down}",
    "left": "{Left}",
    "arrowleft": "{Left}",
    "right": "{Right}",
    "arrowright": "{Right}",
}


def _strip_key_braces(token: str) -> tuple[str, bool]:
    raw = str(token or "").strip()
    if raw.startswith("{") and raw.endswith("}") and len(raw) > 2:
        return raw[1:-1].strip(), True
    return raw, False


def _key_token_name(token: str) -> str:
    inner, _braced = _strip_key_braces(token)
    return re.sub(r"[\s_\-]+", "", inner).lower()


def _split_plus_shortcut(keys: str) -> list[str] | None:
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for char in keys:
        if char == "{":
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        if char == "+" and depth == 0:
            part = "".join(buf).strip()
            if not part:
                return None
            parts.append(part)
            buf = []
        else:
            buf.append(char)
    tail = "".join(buf).strip()
    if not tail:
        return None
    parts.append(tail)
    return parts if len(parts) >= 2 else None


def _normalize_key_part(part: str) -> str | None:
    inner, braced = _strip_key_braces(part)
    name = _key_token_name(part)
    if not name:
        return None
    if name in _KEY_TOKEN_ALIASES:
        return _KEY_TOKEN_ALIASES[name]
    if re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", name):
        return "{" + name.upper() + "}"
    if len(inner) == 1 and inner.isalnum():
        return inner.lower()
    if len(inner) == 1 and inner.isascii() and inner.isprintable():
        return "{" + inner + "}"
    return None


def _unsupported_keys(keys):
    raise tools.ToolError(
        f"Unsupported key or chord {str(keys)!r}. Use named keys such as WIN+R, "
        "CTRL+O or ENTER; use computer.type_text for literal text.",
        code="desktop_key_invalid",
    )


def _normalize_plus_shortcut(keys: str) -> str:
    parts = _split_plus_shortcut(keys)
    if not parts and keys.endswith("++"):
        parts = _split_plus_shortcut(keys[:-1] + "{+}")
    if not parts:
        if "+" in keys and len(keys) > 1 and "{" not in keys:
            _unsupported_keys(keys)
        return keys
    names = [_key_token_name(part) for part in parts]
    if not any(name in _PLUS_SHORTCUT_MODIFIERS for name in names):
        _unsupported_keys(keys)
    normalized = [_normalize_key_part(part) for part in parts]
    if any(part is None for part in normalized):
        _unsupported_keys(keys)
    return "".join(str(part) for part in normalized)


def _normalize_keys_for_send(keys) -> str:
    if isinstance(keys, (list, tuple)):
        return "".join(_normalize_keys_for_send(item) for item in keys)
    raw = str(keys or "").strip()
    if not raw:
        return ""
    shortcut = _normalize_plus_shortcut(raw)
    if shortcut != raw:
        return shortcut
    name = _key_token_name(raw)
    if name in _KEY_TOKEN_ALIASES or re.fullmatch(
        r"f(?:[1-9]|1[0-9]|2[0-4])", name
    ):
        return str(_normalize_key_part(raw) or raw)
    if len(raw) == 1 or ("{" in raw and "}" in raw):
        # Preserve the existing explicit SendKeys grammar (e.g. {Ctrl}{End})
        # and individual characters. Plain unknown names are never text input.
        return raw
    _unsupported_keys(raw)


def _send_keys_direct(auto, keys, *, guard=None) -> dict:
    """Send desktop-global keys, checking the caller's focus fence per event."""

    normalized = _normalize_keys_for_send(keys)
    if not normalized:
        raise tools.ToolError("computer.press_key needs keys")
    # SendKeys also treats unknown braced names as literal text. Validate the
    # complete request first, including explicit native grammar, so a typo
    # cannot turn into an edit after an earlier key was already delivered.
    tokens = re.compile(r"\{(?:[^{}]+|[{}])\}")
    remainder = tokens.sub("", normalized)
    if "{" in remainder or "}" in remainder:
        _unsupported_keys(keys)
    known = getattr(auto, "SpecialKeyNames", None)
    if not isinstance(known, dict):
        known = {value[1:-1].upper(): True for value in _KEY_TOKEN_ALIASES.values()}
        known.update({f"F{i}": True for i in range(1, 25)})
    for match in tokens.finditer(normalized):
        fields = match.group()[1:-1].split()
        if (not fields or len(fields) > 2
                or (len(fields) == 2 and not fields[1].isdigit())):
            _unsupported_keys(keys)
        if len(fields[0]) != 1 and fields[0].upper() not in known:
            _unsupported_keys(keys)
    if guard is None:
        auto.SendKeys(normalized, waitTime=0)
    else:
        _guarded_send_keys(auto.SendKeys, normalized, guard)
    return {"keys_sent": normalized}


def _guarded_send_keys(sender, normalized, guard):
    """Retain UIAutomation's grammar without patching process-global functions.

    Its parser dispatches keybd_event / SendUnicodeChar from module globals.
    A private function namespace intercepts those events for this call only;
    already-held modifiers are released even when the next key loses focus.
    """
    import types

    guard()
    if not isinstance(sender, types.FunctionType) or "keybd_event" not in sender.__globals__:
        # Alternate drivers implement their own single-call delivery.
        return sender(normalized, waitTime=0)
    namespace = dict(sender.__globals__)
    native_key = namespace["keybd_event"]
    native_unicode = namespace["SendUnicodeChar"]
    held = {}

    def key(key, scan, flags, extra):
        if not flags & 2:
            guard()
        native_key(key, scan, flags, extra)
        if flags & 2:
            held.pop(key, None)
        else:
            held[key] = (scan, flags, extra)

    def character(*args, **kwargs):
        guard()
        return native_unicode(*args, **kwargs)

    namespace.update(keybd_event=key, SendUnicodeChar=character)
    scoped = types.FunctionType(sender.__code__, namespace, sender.__name__,
                                sender.__defaults__, sender.__closure__)
    scoped.__kwdefaults__ = sender.__kwdefaults__
    try:
        scoped(normalized, waitTime=0)
    finally:
        for key_code, (scan, flags, extra) in reversed(list(held.items())):
            native_key(key_code, scan, flags | 2, extra)


def _send_input_batch(events) -> int:
    """Use the native array API; uiautomation.SendInput returns only its last event."""
    import ctypes
    array = (type(events[0]) * len(events))(*events)
    return int(ctypes.windll.user32.SendInput(
        len(events), ctypes.byref(array), ctypes.sizeof(events[0])))


def _type_text_direct(auto, text, *, guard=None) -> dict:
    """Send literal text without changing the caller's focused child control."""

    if text is None:
        raise tools.ToolError("computer.type_text needs text")
    value = str(text)
    # Literal text is not SendKeys grammar. Use UTF-16 packets for every
    # printable code unit, including both halves of supplementary characters.
    # Windows editors require real Return/Tab keys for these control characters.
    normalized = value.replace('\r\n', '\n').replace('\r', '\n')
    events = []
    raw = normalized.encode('utf-16-le', errors='surrogatepass')
    for offset in range(0, len(raw), 2):
        unit = int.from_bytes(raw[offset:offset+2], 'little')
        virtual = 0x0D if unit == 10 else 0x09 if unit == 9 else 0
        scan = 0 if virtual else unit
        flags = 0 if virtual else 0x0004  # KEYEVENTF_UNICODE
        events.append(auto.KeyboardInput(virtual, scan, flags))
        events.append(auto.KeyboardInput(virtual, scan, flags | 0x0002))
    delivered = 0
    for offset in range(0, len(events), 2):
        batch = events[offset:offset+2]
        if guard is not None:
            guard()
        inserted = _send_input_batch(batch)
        delivered += inserted
        if inserted != len(batch):
            raise tools.ToolError(
                f"Literal text input was only partially delivered ({delivered}/{len(events)} input events); inspect the editor before retrying.",
                code="desktop_input_partial",
            )
        # WinUI/RichEdit can consume Unicode packets asynchronously through
        # TSF. Flooding the queue replaces characters with later packet values.
        # Let the editor consume each UTF-16 unit, including surrogate halves.
        if offset + 2 < len(events):
            time.sleep(0.05)
    return {"typed_characters": len(value), "input_events": delivered, "method": "unicode_send_input"}


def _scroll_direct(auto, args, *, guard=None) -> dict:
    values = dict(args or {})
    direction = str(values.get("direction") or "down").casefold()
    amount = max(1, min(int(values.get("amount") or 3), 20))
    try:
        point = (int(values.get("x")), int(values.get("y")))
    except (TypeError, ValueError) as exc:
        raise tools.ToolError("computer.scroll needs integer x and y") from exc
    if guard is not None:
        guard(point)
    auto.SetCursorPos(*point)
    if guard is not None:
        guard(point)
    if direction == "up":
        auto.WheelUp(amount)
    else:
        auto.WheelDown(amount)
    return {"direction": direction, "amount": amount}


def _click_direct(auto, args, *, guard=None) -> dict:
    values = dict(args or {})
    try:
        x, y = int(values.get("x")), int(values.get("y"))
    except (TypeError, ValueError) as exc:
        raise tools.ToolError("computer.click needs integer x and y") from exc
    button = str(values.get("button") or "left").casefold()
    double = bool(values.get("double"))
    if button not in {"left", "right", "middle"}:
        raise tools.ToolError("computer.click button must be left, right, or middle")
    if button == "middle" and not hasattr(auto, "MiddleClick"):
        raise tools.ToolError("middle click is unavailable in this desktop driver")
    if guard is not None:
        guard((x, y))
    if button == "right":
        auto.RightClick(x, y)
    elif button == "middle":
        auto.MiddleClick(x, y)
    else:
        auto.Click(x, y)
        if double:
            if guard is not None:
                guard((x, y))
            auto.Click(x, y)
    return {"x": x, "y": y, "button": button, "count": 2 if double else 1}


def _drag_direct(auto, args, *, guard=None) -> dict:
    values = dict(args or {})
    try:
        points = [
            (int(values.get("x1")), int(values.get("y1"))),
            (int(values.get("x2")), int(values.get("y2"))),
        ]
    except (TypeError, ValueError) as exc:
        raise tools.ToolError(
            "computer.drag needs integer start and end coordinates") from exc
    (x1, y1), (x2, y2) = points
    distance = math.hypot(x2 - x1, y2 - y1)
    steps = max(8, min(160, int(distance // 12)))
    pressed = False
    try:
        if guard is not None:
            guard((x1, y1))
            guard((x2, y2))
        auto.PressMouse(x1, y1, waitTime=0.12)
        pressed = True
        for index in range(1, steps + 1):
            if guard is not None:
                guard((x1 + (x2 - x1) * index // steps,
                       y1 + (y2 - y1) * index // steps))
            auto.MoveTo(
                x1 + (x2 - x1) * index // steps,
                y1 + (y2 - y1) * index // steps,
                moveSpeed=0,
                waitTime=0.01,
            )
    finally:
        if pressed:
            auto.ReleaseMouse(waitTime=0.12)
    return {"from": [x1, y1], "to": [x2, y2]}


__all__ = [
    "_click_direct",
    "_drag_direct",
    "_scroll_direct",
    "_send_keys_direct",
    "_type_text_direct",
    "_normalize_keys_for_send",
]
