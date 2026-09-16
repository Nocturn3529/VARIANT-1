"""Coworker activity placement for the main and side UI surfaces."""

from __future__ import annotations

_SURFACE_BOTH = frozenset({
    "task:start", "task:done",
    "loop:progress", "loop:start", "loop:status",
})
_SURFACE_SIDE = frozenset({
    "task:thinking", "task:step",
    "tool:start", "tool:result",
    "loop:detail",
})


def activity_surface(event: str) -> str:
    """Return ``main``, ``side``, or ``both`` for an activity event."""
    name = str(event or "").strip()
    if name in _SURFACE_BOTH:
        return "both"
    if name in _SURFACE_SIDE or name.startswith("tool:"):
        return "side"
    if name.startswith("loop:"):
        return "side" if name == "loop:detail" else "both"
    if name.startswith("task:"):
        return "side" if name in {"task:thinking", "task:step"} else "both"
    if name == "note":
        return "both"
    return "side"
