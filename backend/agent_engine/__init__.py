"""VARIANT-1-native agent execution and durability contracts."""

from __future__ import annotations

from .snapshot_utils import mutation_elevation_blocked_by_threads
from .config import AgentRunConfig
from .errors import DurableCheckpointUnavailable
from .executor import execute_headless_worker, execute_main_chat
from .presets import automation_v1, chat_task_default, subagent_v1
from .snapshot_store import (
    RunSnapshotStore,
    SnapshotBoundaryCommitter,
    SnapshotCursor,
    StoredRunSnapshot,
)
from .sqlite_snapshot_store import SQLiteRunSnapshotStore, default_snapshot_path
from .state import RunState, new_run_state


__all__ = [
    "AgentRunConfig",
    "DurableCheckpointUnavailable",
    "RunState",
    "RunSnapshotStore",
    "SnapshotBoundaryCommitter",
    "SnapshotCursor",
    "SQLiteRunSnapshotStore",
    "StoredRunSnapshot",
    "automation_v1",
    "chat_task_default",
    "default_snapshot_path",
    "execute_headless_worker",
    "execute_main_chat",
    "new_run_state",
    "mutation_elevation_blocked_by_threads",
    "subagent_v1",
]
