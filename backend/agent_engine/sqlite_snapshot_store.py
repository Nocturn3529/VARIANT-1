"""Native SQLite durability for VARIANT-1 agent state machines.

Snapshots are complete, strict-JSON ``RunState`` values captured at validated
node boundaries. The store owns optimistic writer fencing, privacy scrubbing,
deletion tombstones, and full-state recovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
import zlib
from contextlib import closing
from typing import Any, Optional, Sequence, cast

from core_invariants import (
    canonical_json_bytes as _canonical_json,
    sqlite_wal_connection,
)
from session_catalog.profiles import IPYTHON_SCHEMA_REVISION, is_action_surface
from work_fabric.scope import WorkScope, coerce_work_scope

from .errors import DurableCheckpointUnavailable
from .run_contract import RUN_STATE_SCHEMA_VERSION, validate_checkpoint_contract
from .snapshot_store import (
    RunSnapshotStore,
    SnapshotCursor,
    SnapshotHeadFilter,
    StoredRunSnapshot,
)
from .state import RunState


_CODEC_VERSION = "json.zlib.v1"
_PRIVACY_REVISION = 1
_MIGRATION_NAME = "native_full_state_v1"
_DEFAULT_MAX_STATE_BYTES = 64 * 1024 * 1024
_TERMINAL_STATUSES = frozenset({
    "completed", "failed", "cancelled", "error", "truncated",
})
_IMAGE_DATA_KEYS = frozenset({
    "initial_image_b64",
    "pending_image_b64",
    "image_b64",
    "data_b64",
})
_MAIN_TRANSITIONS = frozenset({
    ("init", "prepare"),
    ("prepare", "loop_init"),
    ("loop_init", "model_step"),
    ("loop_init", "tool"),
    ("loop_init", "main_finalize"),
    ("model_step", "model_step"),
    ("model_step", "tool"),
    ("model_step", "main_finalize"),
    ("tool", "model_step"),
    ("tool", "main_finalize"),
    ("main_finalize", "model_step"),
    ("main_finalize", "finalize"),
    ("finalize", "end"),
})
_WORKER_TRANSITIONS = frozenset({
    ("init", "prepare"),
    ("prepare", "worker_step"),
    ("prepare", "worker_tool"),
    ("prepare", "worker_finalize"),
    ("worker_step", "worker_step"),
    ("worker_step", "worker_tool"),
    ("worker_step", "worker_finalize"),
    ("worker_tool", "worker_step"),
    ("worker_tool", "worker_finalize"),
    ("worker_finalize", "finalize"),
    ("finalize", "end"),
})


class SnapshotStoreConflict(DurableCheckpointUnavailable):
    """A snapshot write lost its optimistic concurrency fence."""


class SnapshotTombstonedError(DurableCheckpointUnavailable):
    """A deleted thread refused a late write or import."""


def _backend_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def default_snapshot_path() -> str:
    data_dir = os.path.abspath(
        os.environ.get("VARIANT1_AGENT_SNAPSHOT_DIR")
        or os.path.join(os.environ.get("VARIANT1_DATA_DIR") or _backend_root(), "data", "agent")
    )
    return os.path.abspath(
        os.environ.get("VARIANT1_AGENT_SNAPSHOT_DB")
        or os.path.join(data_dir, "snapshots.sqlite3")
    )


def _finite_float(value: Any, fallback: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return fallback
    return out if math.isfinite(out) else fallback


def _normalize_and_scrub(value: Any, *, path: str = "$") -> tuple[Any, int]:
    """Return strict JSON data with transient image bytes removed."""
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value[:100]:
            return "", 1
        return value, 0
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number at {path}")
        return value, 0
    if isinstance(value, (list, tuple)):
        out = []
        removed = 0
        for index, item in enumerate(value):
            cleaned, count = _normalize_and_scrub(item, path=f"{path}[{index}]")
            out.append(cleaned)
            removed += count
        return out, removed
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        removed = 0
        local_image_source = (
            str(value.get("type") or "").lower() == "base64"
            and str(value.get("media_type") or "").lower().startswith("image/")
        )
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError(f"non-string object key at {path}: {raw_key!r}")
            key = raw_key
            if key in _IMAGE_DATA_KEYS and isinstance(item, str) and item:
                out[key] = ""
                removed += 1
                continue
            if local_image_source and key == "data" and isinstance(item, str) and item:
                out[key] = ""
                removed += 1
                continue
            if key in {"url", "image_url"} and isinstance(item, str) and item.startswith("data:image/"):
                out[key] = ""
                removed += 1
                continue
            cleaned, count = _normalize_and_scrub(
                item,
                path=f"{path}.{key}",
            )
            out[key] = cleaned
            removed += count
        return out, removed
    raise TypeError(f"unsupported snapshot value at {path}: {type(value).__name__}")


def _max_state_bytes() -> int:
    raw = os.environ.get("VARIANT1_MAX_SNAPSHOT_STATE_BYTES")
    try:
        value = int(raw) if raw else _DEFAULT_MAX_STATE_BYTES
    except (TypeError, ValueError) as exc:
        raise ValueError("VARIANT1_MAX_SNAPSHOT_STATE_BYTES must be an integer") from exc
    return max(1024, value)


def _encode_snapshot(
    state: RunState,
    *,
    completed_node: str,
    next_node: str,
) -> tuple[RunState, bytes, str, int]:
    cleaned, removed = _normalize_and_scrub(dict(state))
    if not isinstance(cleaned, dict):
        raise TypeError("RunState must encode as a JSON object")
    normalized = cast(RunState, cleaned)
    _validate_run_state(normalized, completed_node=completed_node, next_node=next_node)
    state_json = _canonical_json(normalized)
    max_bytes = _max_state_bytes()
    if len(state_json) > max_bytes:
        raise ValueError(f"snapshot state exceeds {max_bytes} bytes")
    envelope = _canonical_json({
        "codec_version": _CODEC_VERSION,
        "completed_node": completed_node,
        "next_node": next_node,
        "privacy_revision": _PRIVACY_REVISION,
        "state": normalized,
    })
    return normalized, zlib.compress(state_json, level=6), hashlib.sha256(envelope).hexdigest(), removed


def _decode_state(blob: bytes, codec_version: str) -> RunState:
    if codec_version != _CODEC_VERSION:
        raise DurableCheckpointUnavailable(
            f"unsupported native snapshot codec: {codec_version!r}"
        )
    try:
        max_bytes = _max_state_bytes()
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(blob, max_bytes + 1)
        if len(raw) > max_bytes or decompressor.unconsumed_tail:
            raise ValueError(f"uncompressed snapshot exceeds {max_bytes} bytes")
        tail = decompressor.flush()
        if len(raw) + len(tail) > max_bytes:
            raise ValueError(f"uncompressed snapshot exceeds {max_bytes} bytes")
        if not decompressor.eof or decompressor.unused_data:
            raise ValueError("snapshot contains incomplete or trailing compressed data")
        decoded = json.loads(
            (raw + tail).decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {value}")
            ),
        )
    except Exception as exc:
        raise DurableCheckpointUnavailable(f"native snapshot could not be decoded: {exc}") from exc
    if not isinstance(decoded, dict):
        raise DurableCheckpointUnavailable("native snapshot state is not an object")
    return cast(RunState, decoded)


def _payload_checksum(
    state: RunState,
    completed_node: str,
    next_node: str,
    *,
    codec_version: str,
    privacy_revision: int,
) -> str:
    return hashlib.sha256(_canonical_json({
        "codec_version": codec_version,
        "completed_node": completed_node,
        "next_node": next_node,
        "privacy_revision": privacy_revision,
        "state": state,
    })).hexdigest()


def _validate_run_state(
    state: RunState,
    *,
    completed_node: str,
    next_node: str,
) -> None:
    source = str(state.get("source") or "")
    if source not in {"chat", "subagent", "automation"}:
        raise ValueError(f"unsupported snapshot source: {source!r}")
    try:
        schema = int(state.get("state_schema_version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot has invalid state_schema_version") from exc
    if schema != RUN_STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported snapshot state schema: {schema}")
    _, contract_error = validate_checkpoint_contract(
        state,
        expected_source=source,
        accept_supported_revision=True,
    )
    if contract_error:
        raise ValueError(f"snapshot run contract is invalid: {contract_error}")
    action_surface = str(state.get("action_surface") or "").strip()
    if not is_action_surface(action_surface):
        raise ValueError(f"unsupported snapshot action surface: {action_surface!r}")
    provider_revision = str(state.get("provider_tool_schema_revision") or "").strip()
    if provider_revision != IPYTHON_SCHEMA_REVISION:
        raise ValueError(
            f"unsupported snapshot provider schema: {provider_revision!r}"
        )
    if not str(state.get("status") or "").strip():
        raise ValueError("snapshot has no status")
    raw_scope = state.get("work_scope")
    if raw_scope is not None:
        if not isinstance(raw_scope, dict):
            raise ValueError("snapshot work_scope must be an object")
        unknown_scope = set(raw_scope).difference(WorkScope.__dataclass_fields__)
        if unknown_scope:
            raise ValueError(
                "snapshot work_scope has unknown field(s): "
                + ", ".join(sorted(str(item) for item in unknown_scope))
            )
        try:
            coerce_work_scope(raw_scope)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"snapshot work_scope is invalid: {exc}") from exc
    transition = (completed_node, next_node)
    allowed = _MAIN_TRANSITIONS if source == "chat" else _WORKER_TRANSITIONS
    if transition not in allowed:
        raise ValueError(
            f"invalid {source} snapshot boundary: {completed_node!r} -> {next_node!r}"
        )


def _record_commit_timing(**fields: Any) -> None:
    try:
        from observability.trace_events import record_trace_event

        record_trace_event("snapshot:commit", **fields)
    except Exception:
        pass


class SQLiteRunSnapshotStore(RunSnapshotStore):
    """Append-only, full-state SQLite implementation of RunSnapshotStore."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(path or default_snapshot_path())
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_wal_connection(self.path)

    def _init_db(self) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_threads (
                    thread_id TEXT PRIMARY KEY,
                    head_seq INTEGER,
                    head_snapshot_id TEXT,
                    run_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    resumable INTEGER NOT NULL DEFAULT 0,
                    state_schema_version INTEGER NOT NULL DEFAULT 0,
                    machine_revision TEXT NOT NULL DEFAULT '',
                    action_surface TEXT NOT NULL DEFAULT '',
                    provider_tool_schema_revision TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    terminal_at REAL,
                    tombstoned_at REAL
                );

                CREATE TABLE IF NOT EXISTS agent_snapshots (
                    thread_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    parent_snapshot_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    completed_node TEXT NOT NULL,
                    next_node TEXT NOT NULL,
                    codec_version TEXT NOT NULL,
                    privacy_revision INTEGER NOT NULL,
                    state_blob BLOB NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (thread_id, seq),
                    FOREIGN KEY (thread_id) REFERENCES agent_threads(thread_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_threads_chat
                    ON agent_threads(chat_id, source, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_threads_status
                    ON agent_threads(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS snapshot_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    rows_changed INTEGER NOT NULL DEFAULT 0,
                    values_scrubbed INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO snapshot_migrations(name, applied_at) VALUES (?, ?)",
                (_MIGRATION_NAME, time.time()),
            )

    @staticmethod
    def _state_fields(state: RunState, *, now: float) -> dict[str, Any]:
        task = state.get("task") if isinstance(state.get("task"), dict) else {}
        output = state.get("output") if isinstance(state.get("output"), dict) else {}
        status = str(state.get("status") or task.get("status") or output.get("completion_status") or "")
        created_at = _finite_float(state.get("created_at"), now)
        updated_at = _finite_float(state.get("updated_at"), now)
        if updated_at < created_at:
            updated_at = created_at
        return {
            "run_id": str(state.get("run_id") or task.get("task_id") or ""),
            "source": str(state.get("source") or ""),
            "chat_id": str(state.get("chat_id") or ""),
            "status": status,
            "title": str(state.get("title") or state.get("goal") or task.get("goal") or "")[:500],
            "model": str(task.get("model_name") or "")[:500],
            "resumable": 0 if status.lower() in _TERMINAL_STATUSES else 1,
            "state_schema_version": int(state.get("state_schema_version") or 0),
            "machine_revision": str(state.get("graph_revision") or ""),
            "action_surface": str(state.get("action_surface") or ""),
            "provider_tool_schema_revision": str(state.get("provider_tool_schema_revision") or ""),
            "created_at": created_at,
            "updated_at": updated_at,
            "terminal_at": updated_at if status.lower() in _TERMINAL_STATUSES else None,
        }

    def commit_boundary_sync(
        self,
        state: RunState,
        *,
        completed_node: str,
        next_node: str,
        expected_head_sequence: Optional[int],
    ) -> SnapshotCursor:
        total_started = time.perf_counter()
        completed_node = str(completed_node or "").strip()
        next_node = str(next_node or "").strip()
        if not completed_node or not next_node:
            raise ValueError("snapshot boundary requires completed_node and next_node")

        encode_started = time.perf_counter()
        normalized, state_blob, checksum, removed = _encode_snapshot(
            state,
            completed_node=completed_node,
            next_node=next_node,
        )
        encode_ms = (time.perf_counter() - encode_started) * 1000
        thread_id = str(
            normalized.get("thread_id") or normalized.get("run_id") or ""
        ).strip()
        if not thread_id:
            raise ValueError("snapshot state has no thread_id or run_id")
        now = time.time()
        fields = self._state_fields(normalized, now=now)

        lock_started = time.perf_counter()
        with self._lock:
            lock_wait_ms = (time.perf_counter() - lock_started) * 1000
            connect_started = time.perf_counter()
            with closing(self._connect()) as conn:
                connect_ms = (time.perf_counter() - connect_started) * 1000
                begin_started = time.perf_counter()
                conn.execute("BEGIN IMMEDIATE")
                begin_ms = (time.perf_counter() - begin_started) * 1000
                write_started = time.perf_counter()
                try:
                    row = conn.execute(
                        "SELECT head_seq, head_snapshot_id, tombstoned_at "
                        "FROM agent_threads WHERE thread_id=?",
                        (thread_id,),
                    ).fetchone()
                    if row is not None and row["tombstoned_at"] is not None:
                        raise SnapshotTombstonedError(
                            f"snapshot thread is deleted: {thread_id}"
                        )

                    if row is None:
                        if expected_head_sequence is not None:
                            raise SnapshotStoreConflict(
                                f"snapshot thread {thread_id!r} does not exist at "
                                f"expected sequence {expected_head_sequence}"
                            )
                        conn.execute(
                            """
                            INSERT INTO agent_threads(
                                thread_id, created_at, updated_at
                            ) VALUES (?, ?, ?)
                            """,
                            (
                                thread_id,
                                fields["created_at"],
                                fields["updated_at"],
                            ),
                        )
                        current_seq = None
                        parent_snapshot_id = ""
                    else:
                        current_seq = (
                            int(row["head_seq"])
                            if row["head_seq"] is not None else None
                        )
                        parent_snapshot_id = str(row["head_snapshot_id"] or "")
                        if expected_head_sequence is None:
                            if current_seq is not None:
                                raise SnapshotStoreConflict(
                                    f"snapshot thread {thread_id!r} already has "
                                    f"head sequence {current_seq}"
                                )
                        elif current_seq != int(expected_head_sequence):
                            raise SnapshotStoreConflict(
                                f"snapshot head changed for {thread_id!r}: "
                                f"expected {expected_head_sequence}, "
                                f"found {current_seq}"
                            )

                    next_seq = 1 if current_seq is None else current_seq + 1
                    snapshot_id = "snap_" + uuid.uuid4().hex
                    conn.execute(
                        """
                        INSERT INTO agent_snapshots(
                            thread_id, seq, snapshot_id, parent_snapshot_id, run_id,
                            completed_node, next_node, codec_version, privacy_revision,
                            state_blob, payload_sha256, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            thread_id,
                            next_seq,
                            snapshot_id,
                            parent_snapshot_id,
                            fields["run_id"],
                            completed_node,
                            next_node,
                            _CODEC_VERSION,
                            _PRIVACY_REVISION,
                            state_blob,
                            checksum,
                            now,
                            fields["updated_at"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE agent_threads SET
                            head_seq=?, head_snapshot_id=?, run_id=?, source=?, chat_id=?,
                            status=?, title=?, model=?, resumable=?,
                            state_schema_version=?, machine_revision=?, action_surface=?,
                            provider_tool_schema_revision=?, updated_at=?, terminal_at=?
                        WHERE thread_id=?
                        """,
                        (
                            next_seq,
                            snapshot_id,
                            fields["run_id"],
                            fields["source"],
                            fields["chat_id"],
                            fields["status"],
                            fields["title"],
                            fields["model"],
                            fields["resumable"],
                            fields["state_schema_version"],
                            fields["machine_revision"],
                            fields["action_surface"],
                            fields["provider_tool_schema_revision"],
                            fields["updated_at"],
                            fields["terminal_at"],
                            thread_id,
                        ),
                    )
                    if removed:
                        conn.execute(
                            "UPDATE snapshot_migrations SET "
                            "values_scrubbed=values_scrubbed+? WHERE name=?",
                            (removed, _MIGRATION_NAME),
                        )
                    write_ms = (time.perf_counter() - write_started) * 1000
                    commit_started = time.perf_counter()
                    conn.execute("COMMIT")
                    commit_ms = (time.perf_counter() - commit_started) * 1000
                    _record_commit_timing(
                        status="ok",
                        thread_id=thread_id,
                        completed_node=completed_node,
                        next_node=next_node,
                        sequence=next_seq,
                        state_bytes=len(state_blob),
                        encode_ms=round(encode_ms, 3),
                        lock_wait_ms=round(lock_wait_ms, 3),
                        connect_ms=round(connect_ms, 3),
                        begin_ms=round(begin_ms, 3),
                        write_ms=round(write_ms, 3),
                        commit_ms=round(commit_ms, 3),
                        total_ms=round(
                            (time.perf_counter() - total_started) * 1000,
                            3,
                        ),
                    )
                    return SnapshotCursor(thread_id, next_seq, snapshot_id)
                except Exception as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    _record_commit_timing(
                        status="error",
                        thread_id=thread_id,
                        completed_node=completed_node,
                        next_node=next_node,
                        state_bytes=len(state_blob),
                        encode_ms=round(encode_ms, 3),
                        lock_wait_ms=round(lock_wait_ms, 3),
                        connect_ms=round(connect_ms, 3),
                        begin_ms=round(begin_ms, 3),
                        total_ms=round(
                            (time.perf_counter() - total_started) * 1000,
                            3,
                        ),
                        error_type=type(exc).__name__,
                    )
                    raise

    def _stored_from_row(
        self,
        row: sqlite3.Row,
        *,
        historical: bool = False,
    ) -> StoredRunSnapshot:
        codec_version = str(row["codec_version"])
        privacy_revision = int(row["privacy_revision"])
        if privacy_revision != _PRIVACY_REVISION:
            raise DurableCheckpointUnavailable(
                f"unsupported native snapshot privacy revision: {privacy_revision}"
            )
        state = _decode_state(row["state_blob"], codec_version)
        checksum = _payload_checksum(
            state,
            str(row["completed_node"]),
            str(row["next_node"]),
            codec_version=codec_version,
            privacy_revision=privacy_revision,
        )
        if checksum != row["payload_sha256"]:
            raise DurableCheckpointUnavailable(
                f"native snapshot checksum mismatch: {row['snapshot_id']}"
            )
        privacy_state, removed = _normalize_and_scrub(state)
        if removed or privacy_state != state:
            raise DurableCheckpointUnavailable(
                f"native snapshot violates privacy revision: {row['snapshot_id']}"
            )
        _validate_run_state(
            state,
            completed_node=str(row["completed_node"]),
            next_node=str(row["next_node"]),
        )
        state_thread_id = str(state.get("thread_id") or state.get("run_id") or "")
        if state_thread_id != str(row["thread_id"]):
            raise DurableCheckpointUnavailable(
                f"native snapshot thread identity mismatch: {row['snapshot_id']}"
            )
        indexed = self._state_fields(state, now=float(row["snapshot_updated_at"]))
        indexed_fields = (
            ("run_id", "thread_run_id"),
            ("source", "thread_source"),
            ("chat_id", "thread_chat_id"),
            ("status", "thread_status"),
        )
        # A thread's indexed status follows its head and can legitimately be
        # newer than a historical snapshot's status. Runtime identity remains
        # exact to the snapshot row even when a stable thread has since started
        # another run generation.
        if historical:
            indexed_fields = (
                ("run_id", "run_id"),
                ("source", "thread_source"),
                ("chat_id", "thread_chat_id"),
            )
        for field, column in indexed_fields:
            if str(indexed[field]) != str(row[column] or ""):
                raise DurableCheckpointUnavailable(
                    f"native snapshot {field} index mismatch: {row['snapshot_id']}"
                )
        return StoredRunSnapshot(
            cursor=SnapshotCursor(
                thread_id=str(row["thread_id"]),
                sequence=int(row["seq"]),
                snapshot_id=str(row["snapshot_id"]),
            ),
            parent_snapshot_id=str(row["parent_snapshot_id"] or ""),
            run_id=(
                str(indexed["run_id"])
                if historical else str(row["thread_run_id"] or "")
            ),
            source=(
                str(indexed["source"])
                if historical else str(row["thread_source"] or "")
            ),
            status=(
                str(indexed["status"])
                if historical else str(row["thread_status"] or "")
            ),
            completed_node=str(row["completed_node"]),
            next_node=str(row["next_node"]),
            state=state,
            created_at=float(row["created_at"]),
            updated_at=float(row["snapshot_updated_at"]),
        )

    @staticmethod
    def _snapshot_select() -> str:
        return (
            "SELECT s.*, s.updated_at AS snapshot_updated_at, "
            "t.source AS thread_source, t.status AS thread_status, "
            "t.chat_id AS thread_chat_id, t.run_id AS thread_run_id "
            "FROM agent_threads AS t JOIN agent_snapshots AS s "
            "ON s.thread_id=t.thread_id "
        )

    @classmethod
    def _head_select(cls) -> str:
        return (
            cls._snapshot_select()
            + "AND s.seq=t.head_seq AND s.snapshot_id=t.head_snapshot_id "
        )

    def load_head_sync(self, thread_id: str) -> Optional[StoredRunSnapshot]:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                self._head_select()
                + "WHERE t.thread_id=? AND t.tombstoned_at IS NULL",
                (str(thread_id),),
            ).fetchone()
            return self._stored_from_row(row) if row is not None else None

    def load_cursor_sync(
        self, cursor: SnapshotCursor,
    ) -> Optional[StoredRunSnapshot]:
        thread_id = str(cursor.thread_id or "").strip()
        snapshot_id = str(cursor.snapshot_id or "").strip()
        sequence = int(cursor.sequence or 0)
        if not thread_id or not snapshot_id or sequence < 1:
            raise ValueError("snapshot cursor must be exact and positive")
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                self._snapshot_select()
                + "WHERE t.thread_id=? AND s.seq=? AND s.snapshot_id=? "
                  "AND t.tombstoned_at IS NULL",
                (thread_id, sequence, snapshot_id),
            ).fetchone()
            return (
                self._stored_from_row(row, historical=True)
                if row is not None else None
            )

    def load_latest_for_chat_sync(
        self,
        chat_id: str,
        *,
        source: str = "chat",
    ) -> Optional[StoredRunSnapshot]:
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            raise ValueError("chat-scoped snapshot lookup requires chat_id")
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                self._head_select()
                + "WHERE t.chat_id=? AND t.source=? AND t.tombstoned_at IS NULL "
                "ORDER BY t.updated_at DESC, t.head_seq DESC LIMIT 1",
                (chat_id, str(source)),
            ).fetchone()
            return self._stored_from_row(row) if row is not None else None

    def list_heads_sync(
        self,
        filters: SnapshotHeadFilter = SnapshotHeadFilter(),
    ) -> Sequence[StoredRunSnapshot]:
        clauses = ["t.tombstoned_at IS NULL"]
        params: list[Any] = []
        if filters.source:
            clauses.append("t.source=?")
            params.append(filters.source)
        if filters.chat_id:
            clauses.append("t.chat_id=?")
            params.append(filters.chat_id)
        if filters.statuses:
            clauses.append("t.status IN (" + ",".join("?" for _ in filters.statuses) + ")")
            params.extend(filters.statuses)
        if filters.updated_before is not None:
            clauses.append("t.updated_at<?")
            params.append(float(filters.updated_before))
        sql = self._head_select() + "WHERE " + " AND ".join(clauses)
        sql += " ORDER BY t.updated_at DESC, t.thread_id"
        if filters.limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(filters.limit)))
        with self._lock, closing(self._connect()) as conn:
            return [self._stored_from_row(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def delete_thread_sync(self, thread_id: str) -> None:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            return
        now = time.time()
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT thread_id FROM agent_threads WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO agent_threads(
                            thread_id, status, created_at, updated_at, tombstoned_at
                        ) VALUES (?, 'deleted', ?, ?, ?)
                        """,
                        (thread_id, now, now, now),
                    )
                else:
                    conn.execute("DELETE FROM agent_snapshots WHERE thread_id=?", (thread_id,))
                    conn.execute(
                        """
                        UPDATE agent_threads SET
                            head_seq=NULL, head_snapshot_id=NULL,
                            status='deleted', resumable=0,
                            updated_at=?, terminal_at=?, tombstoned_at=?
                        WHERE thread_id=?
                        """,
                        (now, now, now, thread_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def copy_thread_sync(self, source_thread_id: str, target_thread_id: str) -> None:
        source = str(source_thread_id or "").strip()
        target = str(target_thread_id or "").strip()
        if not source or not target:
            raise ValueError("source and target thread ids are required")
        if source == target:
            return
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                source_row = conn.execute(
                    "SELECT * FROM agent_threads WHERE thread_id=? AND tombstoned_at IS NULL",
                    (source,),
                ).fetchone()
                if source_row is None or source_row["head_seq"] is None:
                    raise ValueError(f"source snapshot thread does not exist: {source}")
                if conn.execute(
                    "SELECT 1 FROM agent_threads WHERE thread_id=?",
                    (target,),
                ).fetchone() is not None:
                    raise ValueError(f"target snapshot thread already exists: {target}")
                snapshot_rows = conn.execute(
                    "SELECT * FROM agent_snapshots WHERE thread_id=? ORDER BY seq",
                    (source,),
                ).fetchall()
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO agent_threads(
                        thread_id, run_id, source, chat_id, status, title,
                        model, resumable, state_schema_version, machine_revision,
                        action_surface, provider_tool_schema_revision, created_at, updated_at,
                        terminal_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target,
                        source_row["run_id"],
                        source_row["source"],
                        source_row["chat_id"],
                        source_row["status"],
                        source_row["title"],
                        source_row["model"],
                        source_row["resumable"],
                        source_row["state_schema_version"],
                        source_row["machine_revision"],
                        source_row["action_surface"],
                        source_row["provider_tool_schema_revision"],
                        source_row["created_at"],
                        now,
                        source_row["terminal_at"],
                    ),
                )
                id_map: dict[str, str] = {"": ""}
                head_id = ""
                expected_seq = 1
                expected_parent = ""
                last_source_snapshot_id = ""
                for snapshot_row in snapshot_rows:
                    if int(snapshot_row["seq"]) != expected_seq:
                        raise DurableCheckpointUnavailable("snapshot copy source has a sequence gap")
                    source_parent = str(snapshot_row["parent_snapshot_id"] or "")
                    if source_parent != expected_parent:
                        raise DurableCheckpointUnavailable("snapshot copy source has broken ancestry")
                    if int(snapshot_row["privacy_revision"]) != _PRIVACY_REVISION:
                        raise DurableCheckpointUnavailable(
                            "snapshot copy source has an unsupported privacy revision"
                        )
                    state = _decode_state(snapshot_row["state_blob"], snapshot_row["codec_version"])
                    completed_node = str(snapshot_row["completed_node"])
                    next_node = str(snapshot_row["next_node"])
                    if _payload_checksum(
                        state,
                        completed_node,
                        next_node,
                        codec_version=str(snapshot_row["codec_version"]),
                        privacy_revision=int(snapshot_row["privacy_revision"]),
                    ) != snapshot_row["payload_sha256"]:
                        raise DurableCheckpointUnavailable(
                            f"cannot copy corrupt native snapshot: {snapshot_row['snapshot_id']}"
                        )
                    state_thread_id = str(state.get("thread_id") or state.get("run_id") or "")
                    if state_thread_id != source:
                        raise DurableCheckpointUnavailable(
                            f"cannot copy snapshot with mismatched thread: {snapshot_row['snapshot_id']}"
                        )
                    state["thread_id"] = target
                    _, blob, checksum, _ = _encode_snapshot(
                        state,
                        completed_node=completed_node,
                        next_node=next_node,
                    )
                    snapshot_id = "snap_" + uuid.uuid4().hex
                    if source_parent not in id_map:
                        raise DurableCheckpointUnavailable("snapshot copy parent is missing")
                    parent_id = id_map[source_parent]
                    id_map[str(snapshot_row["snapshot_id"])] = snapshot_id
                    head_id = snapshot_id
                    last_source_snapshot_id = str(snapshot_row["snapshot_id"])
                    conn.execute(
                        """
                        INSERT INTO agent_snapshots(
                            thread_id, seq, snapshot_id, parent_snapshot_id, run_id,
                            completed_node, next_node, codec_version, privacy_revision,
                            state_blob, payload_sha256, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target,
                            snapshot_row["seq"],
                            snapshot_id,
                            parent_id,
                            snapshot_row["run_id"],
                            completed_node,
                            next_node,
                            _CODEC_VERSION,
                            _PRIVACY_REVISION,
                            blob,
                            checksum,
                            snapshot_row["created_at"],
                            snapshot_row["updated_at"],
                        ),
                    )
                    expected_parent = last_source_snapshot_id
                    expected_seq += 1
                if (
                    expected_seq - 1 != int(source_row["head_seq"])
                    or last_source_snapshot_id != str(source_row["head_snapshot_id"] or "")
                ):
                    raise DurableCheckpointUnavailable("snapshot copy source head is inconsistent")
                conn.execute(
                    "UPDATE agent_threads SET head_seq=?, head_snapshot_id=? WHERE thread_id=?",
                    (source_row["head_seq"], head_id, target),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def copy_thread_through_sync(
        self,
        source: SnapshotCursor,
        target_thread_id: str,
        *,
        target_run_id: str,
        target_chat_id: str,
    ) -> SnapshotCursor:
        """Copy verified native ancestry through an exact resumable cursor."""
        if not isinstance(source, SnapshotCursor):
            raise TypeError("source must be a SnapshotCursor")
        source_thread = str(source.thread_id or "").strip()
        source_snapshot = str(source.snapshot_id or "").strip()
        source_sequence = int(source.sequence or 0)
        target_thread = str(target_thread_id or "").strip()
        target_run = str(target_run_id or "").strip()
        target_chat = str(target_chat_id or "").strip()
        if not source_thread or not source_snapshot or source_sequence < 1:
            raise ValueError("source cursor is incomplete")
        if not target_thread or not target_run:
            raise ValueError("target thread and run ids are required")
        if source_thread == target_thread:
            raise ValueError("snapshot prefix target must be a new thread")

        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                source_row = conn.execute(
                    "SELECT * FROM agent_threads "
                    "WHERE thread_id=? AND tombstoned_at IS NULL",
                    (source_thread,),
                ).fetchone()
                if source_row is None or source_row["head_seq"] is None:
                    raise ValueError(
                        f"source snapshot thread does not exist: {source_thread}"
                    )
                if source_sequence > int(source_row["head_seq"]):
                    raise DurableCheckpointUnavailable(
                        "source snapshot cursor is newer than the thread head"
                    )
                selected = conn.execute(
                    "SELECT * FROM agent_snapshots "
                    "WHERE thread_id=? AND seq=? AND snapshot_id=?",
                    (source_thread, source_sequence, source_snapshot),
                ).fetchone()
                if selected is None:
                    raise DurableCheckpointUnavailable(
                        "source snapshot cursor does not identify one exact boundary"
                    )
                target_row = conn.execute(
                    "SELECT tombstoned_at FROM agent_threads WHERE thread_id=?",
                    (target_thread,),
                ).fetchone()
                if target_row is not None:
                    if target_row["tombstoned_at"] is not None:
                        raise SnapshotTombstonedError(
                            f"snapshot prefix target is deleted: {target_thread}"
                        )
                    raise ValueError(
                        f"target snapshot thread already exists: {target_thread}"
                    )

                snapshot_rows = conn.execute(
                    "SELECT * FROM agent_snapshots "
                    "WHERE thread_id=? AND seq<=? ORDER BY seq",
                    (source_thread, source_sequence),
                ).fetchall()
                if len(snapshot_rows) != source_sequence:
                    raise DurableCheckpointUnavailable(
                        "snapshot prefix source has a sequence gap"
                    )

                prepared: list[dict[str, Any]] = []
                expected_seq = 1
                expected_parent = ""
                source_run_id = str(source_row["run_id"] or "")
                selected_fields: dict[str, Any] | None = None
                for snapshot_row in snapshot_rows:
                    sequence = int(snapshot_row["seq"])
                    if sequence != expected_seq:
                        raise DurableCheckpointUnavailable(
                            "snapshot prefix source has a sequence gap"
                        )
                    parent = str(snapshot_row["parent_snapshot_id"] or "")
                    if parent != expected_parent:
                        raise DurableCheckpointUnavailable(
                            "snapshot prefix source has broken ancestry"
                        )
                    privacy_revision = int(snapshot_row["privacy_revision"])
                    if privacy_revision != _PRIVACY_REVISION:
                        raise DurableCheckpointUnavailable(
                            "snapshot prefix source has an unsupported privacy revision"
                        )
                    codec_version = str(snapshot_row["codec_version"])
                    state = _decode_state(snapshot_row["state_blob"], codec_version)
                    completed_node = str(snapshot_row["completed_node"])
                    next_node = str(snapshot_row["next_node"])
                    if _payload_checksum(
                        state,
                        completed_node,
                        next_node,
                        codec_version=codec_version,
                        privacy_revision=privacy_revision,
                    ) != snapshot_row["payload_sha256"]:
                        raise DurableCheckpointUnavailable(
                            "cannot fork corrupt native snapshot: "
                            f"{snapshot_row['snapshot_id']}"
                        )
                    _validate_run_state(
                        state,
                        completed_node=completed_node,
                        next_node=next_node,
                    )
                    state_thread_id = str(
                        state.get("thread_id") or state.get("run_id") or ""
                    )
                    if state_thread_id != source_thread:
                        raise DurableCheckpointUnavailable(
                            "cannot fork snapshot with mismatched thread: "
                            f"{snapshot_row['snapshot_id']}"
                        )

                    state["thread_id"] = target_thread
                    if str(state.get("run_id") or "") in {"", source_run_id}:
                        state["run_id"] = target_run
                    if target_chat or "chat_id" in state:
                        state["chat_id"] = target_chat
                    task = state.get("task")
                    if isinstance(task, dict):
                        task = dict(task)
                        if str(task.get("task_id") or "") in {"", source_run_id}:
                            task["task_id"] = target_run
                        state["task"] = task

                    normalized, blob, checksum, _ = _encode_snapshot(
                        state,
                        completed_node=completed_node,
                        next_node=next_node,
                    )
                    fields = self._state_fields(
                        normalized,
                        now=float(snapshot_row["updated_at"]),
                    )
                    prepared.append({
                        "sequence": sequence,
                        "source_snapshot_id": str(snapshot_row["snapshot_id"]),
                        "source_parent_id": parent,
                        "run_id": str(fields["run_id"]),
                        "completed_node": completed_node,
                        "next_node": next_node,
                        "blob": blob,
                        "checksum": checksum,
                        "created_at": float(snapshot_row["created_at"]),
                        "updated_at": float(snapshot_row["updated_at"]),
                        "fields": fields,
                    })
                    selected_fields = fields
                    expected_parent = str(snapshot_row["snapshot_id"])
                    expected_seq += 1

                if not prepared or selected_fields is None:
                    raise DurableCheckpointUnavailable(
                        "snapshot prefix source is empty"
                    )
                if (
                    int(selected_fields["resumable"]) != 1
                    or prepared[-1]["next_node"] == "end"
                ):
                    raise DurableCheckpointUnavailable(
                        "terminal native snapshot cannot seed a resumable fork"
                    )

                now = time.time()
                conn.execute(
                    """
                    INSERT INTO agent_threads(
                        thread_id, run_id, source, chat_id, status, title,
                        model, resumable, state_schema_version, machine_revision,
                        action_surface, provider_tool_schema_revision, created_at,
                        updated_at, terminal_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_thread,
                        selected_fields["run_id"],
                        selected_fields["source"],
                        selected_fields["chat_id"],
                        selected_fields["status"],
                        selected_fields["title"],
                        selected_fields["model"],
                        selected_fields["resumable"],
                        selected_fields["state_schema_version"],
                        selected_fields["machine_revision"],
                        selected_fields["action_surface"],
                        selected_fields["provider_tool_schema_revision"],
                        now,
                        now,
                        None,
                    ),
                )

                id_map: dict[str, str] = {"": ""}
                target_head_id = ""
                for row in prepared:
                    source_parent = str(row["source_parent_id"])
                    if source_parent not in id_map:
                        raise DurableCheckpointUnavailable(
                            "snapshot prefix copy parent is missing"
                        )
                    snapshot_id = "snap_" + uuid.uuid4().hex
                    parent_id = id_map[source_parent]
                    id_map[str(row["source_snapshot_id"])] = snapshot_id
                    target_head_id = snapshot_id
                    conn.execute(
                        """
                        INSERT INTO agent_snapshots(
                            thread_id, seq, snapshot_id, parent_snapshot_id,
                            run_id, completed_node, next_node, codec_version,
                            privacy_revision, state_blob, payload_sha256,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_thread,
                            row["sequence"],
                            snapshot_id,
                            parent_id,
                            row["run_id"],
                            row["completed_node"],
                            row["next_node"],
                            _CODEC_VERSION,
                            _PRIVACY_REVISION,
                            row["blob"],
                            row["checksum"],
                            row["created_at"],
                            row["updated_at"],
                        ),
                    )
                conn.execute(
                    "UPDATE agent_threads SET head_seq=?, head_snapshot_id=? "
                    "WHERE thread_id=?",
                    (source_sequence, target_head_id, target_thread),
                )
                conn.execute("COMMIT")
                return SnapshotCursor(
                    target_thread,
                    source_sequence,
                    target_head_id,
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise

    async def commit_boundary(
        self,
        state: RunState,
        *,
        completed_node: str,
        next_node: str,
        expected_head_sequence: Optional[int],
    ) -> SnapshotCursor:
        return await asyncio.to_thread(
            self.commit_boundary_sync,
            state,
            completed_node=completed_node,
            next_node=next_node,
            expected_head_sequence=expected_head_sequence,
        )

    async def load_head(self, thread_id: str) -> Optional[StoredRunSnapshot]:
        return await asyncio.to_thread(self.load_head_sync, thread_id)

    async def load_cursor(
        self, cursor: SnapshotCursor,
    ) -> Optional[StoredRunSnapshot]:
        return await asyncio.to_thread(self.load_cursor_sync, cursor)

    async def load_latest_for_chat(
        self,
        chat_id: str,
        *,
        source: str = "chat",
    ) -> Optional[StoredRunSnapshot]:
        return await asyncio.to_thread(
            self.load_latest_for_chat_sync,
            chat_id,
            source=source,
        )

    async def list_heads(
        self,
        filters: SnapshotHeadFilter = SnapshotHeadFilter(),
    ) -> Sequence[StoredRunSnapshot]:
        return await asyncio.to_thread(self.list_heads_sync, filters)

    async def delete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread_sync, thread_id)

    async def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        await asyncio.to_thread(self.copy_thread_sync, source_thread_id, target_thread_id)

    async def copy_thread_through(
        self,
        source: SnapshotCursor,
        target_thread_id: str,
        *,
        target_run_id: str,
        target_chat_id: str,
    ) -> SnapshotCursor:
        return await asyncio.to_thread(
            self.copy_thread_through_sync,
            source,
            target_thread_id,
            target_run_id=target_run_id,
            target_chat_id=target_chat_id,
        )
