"""Activity placement shared by the main chat and coworker side surface."""

from __future__ import annotations

import asyncio

from observability import activity
import coworker


def test_activity_surface_policy():
    assert coworker.activity_surface("tool:start") == "side"
    assert coworker.activity_surface("task:done") == "both"
    assert coworker.activity_surface("task:thinking") == "side"
    assert coworker.activity_surface("loop:progress") == "both"
    assert coworker.activity_surface("loop:start") == "both"
    assert coworker.activity_surface("loop:status") == "both"
    assert coworker.activity_surface("loop:detail") == "side"


def test_emit_activity_tags_surface():
    seen = []

    class Hub:
        async def broadcast(self, msg):
            seen.append(msg)

    old = activity.HUB
    activity.HUB = Hub()
    try:
        asyncio.run(activity.emit_activity("tool:start", tool="read_file", text="x"))
    finally:
        activity.HUB = old
    assert seen
    assert seen[0].get("surface") == "side"
