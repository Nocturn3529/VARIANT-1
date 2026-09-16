"""Framework-neutral durable snapshot contracts for VARIANT-1 agent runs.

This module intentionally contains no storage implementation or orchestration
framework dependency.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable

from .state import RunState


@dataclass(frozen=True)
class SnapshotCursor:
    """Optimistic-concurrency cursor for one thread's committed head."""

    thread_id: str
    sequence: int
    snapshot_id: str


@dataclass(frozen=True)
class StoredRunSnapshot:
    """One complete, validated RunState captured at a node boundary."""

    cursor: SnapshotCursor
    parent_snapshot_id: str
    run_id: str
    source: str
    status: str
    completed_node: str
    next_node: str
    state: RunState
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class SnapshotHeadFilter:
    """Framework-neutral filters used by resume, orphan, and retention paths."""

    source: str = ""
    chat_id: str = ""
    statuses: tuple[str, ...] = ()
    updated_before: Optional[float] = None
    limit: Optional[int] = None


@runtime_checkable
class RunSnapshotStore(Protocol):
    """Durability surface required by VARIANT-1's native executor.

    Implementations must save strict JSON-safe full state synchronously at each
    successful boundary.  A head-sequence mismatch must fail rather than allow
    concurrent writers to overwrite one another.
    """

    async def commit_boundary(
        self,
        state: RunState,
        *,
        completed_node: str,
        next_node: str,
        expected_head_sequence: Optional[int],
    ) -> SnapshotCursor:
        """Atomically append a snapshot and advance the thread head.

        ``expected_head_sequence=None`` means the thread must not have a head.
        """
        ...

    async def load_head(self, thread_id: str) -> Optional[StoredRunSnapshot]:
        """Load one thread's latest committed snapshot."""
        ...

    async def load_cursor(
        self, cursor: SnapshotCursor,
    ) -> Optional[StoredRunSnapshot]:
        """Load exactly one committed snapshot without resolving the head.

        Restore-point capture and replay use this lookup so a caller-supplied
        historical cursor cannot silently drift to a newer boundary.
        """
        ...

    async def load_latest_for_chat(
        self,
        chat_id: str,
        *,
        source: str = "chat",
    ) -> Optional[StoredRunSnapshot]:
        """Load the latest candidate used by chat resume/orphan discovery."""
        ...

    async def list_heads(
        self,
        filters: SnapshotHeadFilter = SnapshotHeadFilter(),
    ) -> Sequence[StoredRunSnapshot]:
        """List thread heads without exposing backend rows or framework types."""
        ...

    async def delete_thread(self, thread_id: str) -> None:
        """Delete snapshots and tombstone the thread against late resurrection."""
        ...

    async def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Copy a complete thread for supported session-copy workflows."""
        ...

    async def copy_thread_through(
        self,
        source: SnapshotCursor,
        target_thread_id: str,
        *,
        target_run_id: str,
        target_chat_id: str,
    ) -> SnapshotCursor:
        """Fork verified ancestry through one compatible nonterminal boundary.

        This is deliberately distinct from :meth:`copy_thread`: conversation
        forks must bind to an exact historical cursor instead of resolving a
        mutable thread head.
        """
        ...



@dataclass
class SnapshotBoundaryCommitter:
    """Adapt a RunSnapshotStore cursor to the native runner's commit callback."""

    store: RunSnapshotStore
    cursor: Optional[SnapshotCursor] = None

    async def __call__(
        self,
        completed_node: str,
        next_node: str,
        state: RunState,
    ) -> None:
        thread_id = str(state.get("thread_id") or state.get("run_id") or "").strip()
        if not thread_id:
            raise ValueError("durable boundary state has no thread_id or run_id")
        if self.cursor is not None and self.cursor.thread_id != thread_id:
            raise ValueError(
                "snapshot cursor thread does not match boundary state "
                f"({self.cursor.thread_id!r} != {thread_id!r})"
            )
        expected = self.cursor.sequence if self.cursor is not None else None
        pending = asyncio.create_task(self.store.commit_boundary(
            state,
            completed_node=completed_node,
            next_node=next_node,
            expected_head_sequence=expected,
        ))
        try:
            committed = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # ``to_thread``-backed SQLite work cannot be cancelled once it has
            # started. Settle it and record the resulting cursor before the
            # caller's cancellation propagates.
            committed = await pending
            self.cursor = committed
            raise
        # Advance the fence only after the store confirms a successful commit.
        self.cursor = committed
