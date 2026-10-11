"""Goal events waiting for the running turn's assistant row (one turn per chat at a time)."""
from __future__ import annotations

import threading

_EVENTS: dict[str, dict] = {}
_LOCK = threading.Lock()


def record_goal_event(chat_id: str, event: dict) -> None:
    with _LOCK:
        _EVENTS[str(chat_id)] = dict(event)


def take_goal_event(chat_id: str) -> dict | None:
    with _LOCK:
        return _EVENTS.pop(str(chat_id or ""), None)


def clean_goal_event(value) -> dict | None:
    """Allowlist a stored goal event."""
    if not isinstance(value, dict) or value.get("event") != "achieved":
        return None
    goal_id = str(value.get("goal_id") or "").strip()[:160]
    if not goal_id:
        return None
    try:
        time_used = round(max(0.0, float(value.get("time_used_s") or 0)), 3)
        tokens = max(0, int(value.get("tokens_used") or 0))
    except (TypeError, ValueError, OverflowError):
        return None
    return {"event": "achieved", "goal_id": goal_id, "time_used_s": time_used, "tokens_used": tokens}
