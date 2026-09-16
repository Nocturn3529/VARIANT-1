"""Shared main-agent node helpers (no node factories)."""

from __future__ import annotations

import time
from typing import Any

from agent_task import TaskStatus
from .agent_runtime import MainTaskRuntime, _main_state, project_live_task_bundle
from .main_live import MainChatLive

__all__ = [
    "_interrupt_main",
    "_node_live_projection",
]

def _interrupt_main(loop, live: MainChatLive) -> dict:
    """Mark the main-chat loop cancelled and return the shared cancel payload.

    Loop routing is written only into the returned RunState projection — not
    mirrored onto the live bag. Task/progress is mirrored via the sole projector.
    """
    loop["interrupted"] = True
    loop["reply"] = "Stopped."
    loop["route"] = "finalize"
    loop["terminal_reason"] = "user_cancelled"
    if live.task is not None:
        live.task.status = TaskStatus.FAILED
    return {
        "main": _main_state(loop, live),
        "status": "cancelled",
        **project_live_task_bundle(live),
        "updated_at": time.time(),
    }


def _node_live_projection(loop, live: MainChatLive, runtime: MainTaskRuntime, **extra) -> dict:
    """Node return: loop main + sole task/progress projector + extras."""
    out = {
        "main": _main_state(loop, live),
        **project_live_task_bundle(live),
        "updated_at": time.time(),
    }
    out.update(extra)
    return out
