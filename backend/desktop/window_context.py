"""Lightweight multi-window context stack for desktop perception.

Pure stack logic (unit-tested). UIA control handles live in a parallel list on the
UIA worker thread — desktop_control syncs the active lock from the stack top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


DEFAULT_MAX_DEPTH = 8


@dataclass
class WindowContext:
    """Re-acquirable window identity (hwnd + title/query for refocus)."""

    hwnd: int = 0
    title: str = ""
    query: str = ""
    metadata: dict = field(default_factory=dict)

    def summary(self) -> str:
        label = (self.title or self.query or "(unknown)").strip()
        if self.hwnd:
            return f"{label} (#{self.hwnd})"
        return label


class WindowContextStack:
    """Stack of window contexts; top is the active target."""

    def __init__(self, *, max_depth: int = DEFAULT_MAX_DEPTH):
        self._contexts: list[WindowContext] = []
        self._controls: list[Any] = []
        self._max_depth = max(1, int(max_depth or DEFAULT_MAX_DEPTH))

    def depth(self) -> int:
        return len(self._contexts)

    def is_empty(self) -> bool:
        return not self._contexts

    def get_current(self) -> Optional[WindowContext]:
        return self._contexts[-1] if self._contexts else None

    def get_current_control(self) -> Any:
        return self._controls[-1] if self._controls else None

    def get_stack(self) -> list[WindowContext]:
        return list(self._contexts)

    def invalidate_current_control(self):
        if self._controls:
            self._controls[-1] = None

    def update_current_control(self, ctrl: Any, *, hwnd: int = 0):
        if not self._contexts:
            return
        if hwnd:
            self._contexts[-1].hwnd = hwnd
        if self._controls:
            self._controls[-1] = ctrl
        else:
            self._controls.append(ctrl)

    def push(self, ctx: WindowContext, ctrl: Any = None) -> WindowContext:
        if self._max_depth and len(self._contexts) >= self._max_depth:
            self._contexts.pop(0)
            self._controls.pop(0)
        self._contexts.append(ctx)
        self._controls.append(ctrl)
        return ctx

    def pop(self) -> tuple[Optional[WindowContext], Optional[WindowContext]]:
        """Remove top; return (popped, new_current)."""
        if not self._contexts:
            return None, None
        popped = self._contexts.pop()
        self._controls.pop()
        current = self.get_current()
        return popped, current

    def replace(self, ctx: WindowContext, ctrl: Any = None):
        """Single-window mode: stack becomes exactly one entry."""
        self._contexts = [ctx]
        self._controls = [ctrl]

    def switch_to(self, index: int) -> Optional[WindowContext]:
        """Move stack[index] to the top (becomes current)."""
        if index < 0 or index >= len(self._contexts):
            return None
        if index == len(self._contexts) - 1:
            return self._contexts[-1]
        ctx = self._contexts.pop(index)
        ctrl = self._controls.pop(index)
        self._contexts.append(ctx)
        self._controls.append(ctrl)
        return ctx

    def restore_previous(self) -> tuple[Optional[WindowContext], Optional[WindowContext]]:
        """Pop current and expose the previous context as new top."""
        return self.pop()

    def clear(self):
        self._contexts = []
        self._controls = []

    def format_stack(self, *, marker: str = "→") -> str:
        if not self._contexts:
            return "(empty)"
        parts = []
        for i, ctx in enumerate(self._contexts):
            label = (ctx.title or ctx.query or "(unknown)").strip()
            if i == len(self._contexts) - 1:
                parts.append(f"{label} (current)")
            else:
                parts.append(label)
        return f" {marker} ".join(parts)


def stack_event_text(event: str, fields: dict) -> str:
    title = fields.get("title") or fields.get("window") or "(unknown)"
    depth = fields.get("depth")
    stack = fields.get("stack") or ""
    depth_s = f" · depth {depth}" if depth is not None else ""
    stack_s = f" · stack: {stack}" if stack else ""
    if event == "perception:window_pushed":
        return f"Window pushed: '{title}'{depth_s}{stack_s}"
    if event == "perception:window_popped":
        prev = fields.get("previous") or ""
        prev_s = f" · restored '{prev}'" if prev else ""
        return f"Window popped: '{title}'{prev_s}{depth_s}{stack_s}"
    if event == "perception:window_switched":
        idx = fields.get("index")
        idx_s = f" (index {idx})" if idx is not None else ""
        return f"Window switched: '{title}'{idx_s}{depth_s}{stack_s}"
    return fields.get("text") or event


def pushed_activity_fields(
    ctx: WindowContext,
    *,
    depth: int,
    stack_summary: str,
    tool: str = "",
) -> dict:
    title = (ctx.title or ctx.query or "").strip()
    text = stack_event_text("perception:window_pushed", {
        "title": title,
        "depth": depth,
        "stack": stack_summary,
    })
    return {
        "text": text,
        "title": title[:120],
        "window": title[:120],
        "query": (ctx.query or "")[:80],
        "hwnd": ctx.hwnd,
        "depth": depth,
        "stack": stack_summary[:300],
        "tool": tool or "",
    }


def popped_activity_fields(
    popped: WindowContext,
    *,
    previous: WindowContext | None,
    depth: int,
    stack_summary: str,
    tool: str = "",
) -> dict:
    title = (popped.title or popped.query or "").strip()
    prev_title = ""
    if previous is not None:
        prev_title = (previous.title or previous.query or "").strip()
    text = stack_event_text("perception:window_popped", {
        "title": title,
        "previous": prev_title,
        "depth": depth,
        "stack": stack_summary,
    })
    return {
        "text": text,
        "title": title[:120],
        "window": title[:120],
        "previous": prev_title[:120],
        "depth": depth,
        "stack": stack_summary[:300],
        "tool": tool or "",
    }


def switched_activity_fields(
    ctx: WindowContext,
    *,
    index: int,
    depth: int,
    stack_summary: str,
    tool: str = "",
) -> dict:
    title = (ctx.title or ctx.query or "").strip()
    text = stack_event_text("perception:window_switched", {
        "title": title,
        "index": index,
        "depth": depth,
        "stack": stack_summary,
    })
    return {
        "text": text,
        "title": title[:120],
        "window": title[:120],
        "index": index,
        "depth": depth,
        "stack": stack_summary[:300],
        "tool": tool or "",
    }

