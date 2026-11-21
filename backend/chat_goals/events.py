"""Allowlisted projection of a Goal completion attached to an assistant row."""
from __future__ import annotations

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
