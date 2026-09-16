"""Stateless desktop control helpers shared by the facade and DesktopControlContext."""

from __future__ import annotations

import zlib

from . import constants as dconst
from .session import DesktopSessionState
from . import window_pick as dwpick

def role(control_type_name: str) -> str:
    name = control_type_name or ""
    return name[:-7] if name.endswith("Control") else name


def stable_id(session: DesktopSessionState, key: str) -> int:
    if key in session.id_registry:
        return session.id_registry[key]
    session.next_id += 1
    session.id_registry[key] = session.next_id
    session.mark_updated()
    return session.next_id


def stable_state_id(session: DesktopSessionState, key: str) -> int:
    """Stable internal identity for readable state without consuming action IDs."""
    registry_key = "state|" + key
    if registry_key in session.id_registry:
        return session.id_registry[registry_key]
    candidate = -((zlib.crc32(registry_key.encode("utf-8")) & 0x7FFFFFFF) + 1)
    used = set(session.id_registry.values())
    while candidate in used:
        candidate -= 1
    session.id_registry[registry_key] = candidate
    session.mark_updated()
    return candidate


def control_score(c: dict) -> int:
    s = 0
    if not c.get("offscreen"):
        s += 100
    if (c.get("name") or "").strip():
        s += 25
    if (c.get("value") or "").strip():
        s += 6
    return s + dconst._ROLE_WEIGHT.get(c.get("role"), 0)


def prompt_cap_now(runtime, *, prompt_cap: int = 100) -> int:
    try:
        if runtime.ctx_getter:
            ctx = int(runtime.ctx_getter() or 0)
            if ctx and ctx < 10000:
                return 50
            if ctx and ctx < 20000:
                return 80
            return 120
    except Exception:
        pass
    return prompt_cap


def prompt_subset(controls: list, cap: int | None, *, prompt_cap_fn) -> tuple[list, int]:
    cap = cap or prompt_cap_fn()
    if len(controls) <= cap:
        return list(controls), 0
    ranked = sorted(controls, key=control_score, reverse=True)[:cap]
    ranked.sort(key=lambda c: c.get("id", 0))
    return ranked, len(controls) - len(ranked)


def format_controls(
    controls: list,
    window_title: str = "",
    cap: int | None = None,
    *,
    prompt_cap_fn,
) -> str:
    header = f"Foreground window: {window_title or '(unknown)'}"
    if not controls:
        return header + "\n(no actionable controls detected)"
    actionable = [c for c in controls if c.get("actionable", True)]
    readable = [c for c in controls if not c.get("actionable", True)]
    shown, hidden = prompt_subset(actionable, cap, prompt_cap_fn=prompt_cap_fn)
    state_rows, state_hidden = compact_readable_state(
        readable,
        actionable=actionable,
        window_title=window_title,
    )
    lines = [header]
    if actionable:
        lines.append(
            f"{len(actionable)} actionable controls detected "
            "(refer to one by its [number]):"
        )
    else:
        lines.append("(no actionable controls detected)")
    for c in shown:
        seg = f"[{c['id']}] {c.get('role', '?')}"
        name = (c.get("name") or "").strip()
        if name:
            seg += f' "{name}"'
        val = (c.get("value") or "").strip()
        if val:
            seg += f' = "{val[:60]}"'
        st = (c.get("state") or "").strip()
        if st:
            seg += f" <{st}>"
        if c.get("offscreen"):
            seg += " (offscreen)"
        # UIA exposes Paint's whole canvas workspace, including gray area around
        # the bitmap. Keep the coordinate frame factual; the focus driver attaches
        # the visual state so the actual bitmap remains visible to the model.
        b = c.get("bounds")
        if c.get("role") == "Canvas" and b and len(b) == 4:
            seg += (
                f" — CANVAS VIEW, screen rect "
                f"({b[0]},{b[1]})-({b[2]},{b[3]}); this view may include "
                "non-drawable workspace around the bitmap"
            )
        lines.append(seg)
    if hidden:
        lines.append(f"(+{hidden} more actionable controls omitted.)")
    if state_rows:
        lines.append("Readable state:")
        for c in state_rows:
            seg = f"State {c.get('role', '?')}"
            name = (c.get("name") or "").strip()
            if name:
                seg += f' "{name}"'
            val = (c.get("value") or "").strip()
            if val:
                seg += f' = "{val[:60]}"'
            st = (c.get("state") or "").strip()
            if st:
                seg += f" <{st}>"
            if c.get("offscreen"):
                seg += " (offscreen)"
            lines.append(seg)
        if state_hidden:
            lines.append(f"(+{state_hidden} more readable state items omitted.)")
    return "\n".join(lines)


_ACTION_SYMBOLS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "decimal separator": ".",
    "clear entry": "CE",
    "clear": "C",
    "plus": "+",
    "minus": "-",
    "multiply by": "×",
    "divide by": "÷",
    "equals": "=",
    "percent": "%",
}


def compact_readable_state(
    readable: list,
    *,
    actionable: list,
    window_title: str = "",
    limit: int = 16,
) -> tuple[list, int]:
    """Keep factual state while dropping child labels that duplicate controls."""
    action_labels: set[str] = set()
    action_symbols: set[str] = set()
    for control in actionable:
        for raw in (control.get("name"), control.get("value")):
            value = (raw or "").strip()
            if value:
                action_labels.add(value.lower())
        symbol = _ACTION_SYMBOLS.get((control.get("name") or "").strip().lower())
        if symbol:
            action_symbols.add(symbol)

    title = (window_title or "").strip().lower()
    seen: set[tuple[str, str, str, str]] = set()
    candidates = []
    for control in readable:
        name = (control.get("name") or "").strip()
        value = (control.get("value") or "").strip()
        state = (control.get("state") or "").strip()
        primary = name or value
        if not primary and not state:
            continue
        primary_lower = primary.lower()
        if primary_lower == title:
            continue
        if primary_lower in action_labels and not value and not state:
            continue
        if primary in action_symbols:
            continue
        if len(primary) == 1 and 0xE000 <= ord(primary) <= 0xF8FF:
            continue
        signature = (
            str(control.get("role") or ""),
            name.lower(),
            value.lower(),
            state.lower(),
        )
        if signature in seen:
            continue
        seen.add(signature)
        candidates.append(control)

    safe_limit = max(1, int(limit or 1))
    return candidates[:safe_limit], max(0, len(candidates) - safe_limit)


def derive_label(auto, control, max_nodes: int = 30, max_chars: int = 80) -> str:
    try:
        parts, seen = [], 0
        for child, _depth in auto.WalkControl(control, includeTop=False, maxDepth=4):
            seen += 1
            if seen > max_nodes:
                break
            try:
                nm = (child.Name or "").strip()
            except Exception:
                nm = ""
            if nm and nm not in parts:
                parts.append(nm)
                if sum(len(x) for x in parts) >= max_chars:
                    break
        return " · ".join(parts)[:max_chars]
    except Exception:
        return ""


def parse_max_controls(args, *, default: int) -> int:
    try:
        max_controls = int((args or {}).get("max") or default)
    except (ValueError, TypeError):
        max_controls = default
    return max(1, min(max_controls, 1000))


def window_title(win) -> str:
    if win is None:
        return ""
    try:
        return (win.Name or "").strip()
    except Exception:
        return ""


def target_top(auto):
    return dwpick.target_top(auto)


def is_skippable(name, classname, pid) -> bool:
    return dwpick.is_skippable(name, classname, pid)
