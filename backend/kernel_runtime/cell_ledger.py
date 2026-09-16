"""Durable, append-only ledger for admitted Python cell executions.

The ledger is evidence, not a replay engine.  It records the exact admitted
scope and immutable CAS references for source/result payloads, while live
kernel objects remain owned by :mod:`kernel_runtime.manager`.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import sqlite3
import time
from typing import Any, Iterator

from core_invariants import canonical_json, sqlite_transaction, sqlite_writer_lock
from tool_core import json_safe


CELL_LEDGER_SCHEMA = "variant1.kernel-cell-ledger.v1"
CELL_ENTRY_SCHEMA = "variant1.kernel-cell-ledger-entry.v2"
_MAX_PAGE = 500


def _json(value: Any) -> str:
    return canonical_json(json_safe(value))


def _json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except Exception:
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _json_strings(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value or "[]")
    except Exception:
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(item) for item in decoded if str(item or "").strip())


@dataclass(frozen=True, slots=True)
class KernelCellRecord:
    sequence: int
    execution_id: str
    chat_id: str
    run_id: str
    outer_tool_call_id: str
    kernel_generation: int
    workspace_revision: int
    workspace_fingerprint: str
    workspace_root_ids: tuple[str, ...]
    work_scope: dict[str, Any]
    source_ref: str
    source_sha256: str
    result_ref: str
    result_sha256: str
    status: str
    execution_count: int
    started_at: float
    completed_at: float
    duration_ms: float
    error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CELL_ENTRY_SCHEMA,
            "sequence": int(self.sequence),
            "execution_id": self.execution_id,
            "chat_id": self.chat_id,
            "run_id": self.run_id,
            "outer_tool_call_id": self.outer_tool_call_id,
            "kernel_generation": int(self.kernel_generation),
            "workspace_revision": int(self.workspace_revision),
            "workspace_fingerprint": self.workspace_fingerprint,
            "workspace_root_ids": list(self.workspace_root_ids),
            "work_scope": dict(self.work_scope),
            "source_ref": self.source_ref,
            "source_sha256": self.source_sha256,
            # Compatibility name used by v1 capsule ledgers.
            "code_sha256": self.source_sha256,
            "result_ref": self.result_ref,
            "result_sha256": self.result_sha256,
            "status": self.status,
            "execution_count": int(self.execution_count),
            "started_at": float(self.started_at),
            "completed_at": float(self.completed_at),
            "duration_ms": (None if self.error_code == "kernel_host_interrupted" else float(self.duration_ms)),
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class KernelCellOutcomeSnapshot:
    """Exact chat-scoped outcome facts without reading cell payloads."""

    chat_id: str
    cell_count: int
    non_ok_count: int
    latest_non_ok_execution_id: str
    latest_non_ok_sequence: int
    latest_non_ok_kernel_generation: int
    latest_non_ok_status: str
    latest_non_ok_error_code: str
    later_cell_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "cell_count": int(self.cell_count),
            "non_ok_count": int(self.non_ok_count),
            "latest_non_ok_execution_id": self.latest_non_ok_execution_id,
            "latest_non_ok_sequence": int(self.latest_non_ok_sequence),
            "latest_non_ok_kernel_generation": int(
                self.latest_non_ok_kernel_generation
            ),
            "latest_non_ok_status": self.latest_non_ok_status,
            "latest_non_ok_error_code": self.latest_non_ok_error_code,
            "later_cell_count": int(self.later_cell_count),
        }


class KernelCellLedgerStore:
    """SQLite-backed append-only execution ledger with monotonic cursors."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._lock = sqlite_writer_lock(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                with sqlite_transaction(
                    connection,
                    immediate=immediate,
                    fault_name=("cell_ledger.before_commit" if immediate else ""),
                ):
                    yield connection
            finally:
                connection.close()

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS kernel_cell_ledger (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        execution_id TEXT NOT NULL UNIQUE,
                        chat_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        outer_tool_call_id TEXT NOT NULL,
                        kernel_generation INTEGER NOT NULL,
                        workspace_revision INTEGER NOT NULL,
                        workspace_fingerprint TEXT NOT NULL,
                        workspace_root_ids_json TEXT NOT NULL,
                        work_scope_json TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        result_ref TEXT NOT NULL,
                        result_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL,
                        execution_count INTEGER NOT NULL,
                        started_at REAL NOT NULL,
                        completed_at REAL NOT NULL,
                        duration_ms REAL NOT NULL,
                        error_code TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS kernel_cell_ledger_chat_sequence_idx
                        ON kernel_cell_ledger(chat_id, sequence);
                    CREATE TABLE IF NOT EXISTS kernel_cell_admission (
                        execution_id TEXT PRIMARY KEY,
                        instance_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE TRIGGER IF NOT EXISTS kernel_cell_admission_no_update
                    BEFORE UPDATE ON kernel_cell_admission BEGIN
                        SELECT RAISE(ABORT, 'kernel admission evidence is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS kernel_cell_admission_no_delete
                    BEFORE DELETE ON kernel_cell_admission BEGIN
                        SELECT RAISE(ABORT, 'kernel admission evidence is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS kernel_cell_ledger_no_update
                    BEFORE UPDATE ON kernel_cell_ledger
                    BEGIN
                        SELECT RAISE(ABORT, 'kernel cell ledger is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS kernel_cell_ledger_no_delete
                    BEFORE DELETE ON kernel_cell_ledger
                    BEGIN
                        SELECT RAISE(ABORT, 'kernel cell ledger is append-only');
                    END;
                    """
                )
            finally:
                connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> KernelCellRecord:
        return KernelCellRecord(
            sequence=int(row["sequence"]),
            execution_id=str(row["execution_id"]),
            chat_id=str(row["chat_id"]),
            run_id=str(row["run_id"]),
            outer_tool_call_id=str(row["outer_tool_call_id"]),
            kernel_generation=int(row["kernel_generation"]),
            workspace_revision=int(row["workspace_revision"]),
            workspace_fingerprint=str(row["workspace_fingerprint"]),
            workspace_root_ids=_json_strings(str(row["workspace_root_ids_json"])),
            work_scope=_json_object(str(row["work_scope_json"])),
            source_ref=str(row["source_ref"]),
            source_sha256=str(row["source_sha256"]),
            result_ref=str(row["result_ref"]),
            result_sha256=str(row["result_sha256"]),
            status=str(row["status"]),
            execution_count=int(row["execution_count"]),
            started_at=float(row["started_at"]),
            completed_at=float(row["completed_at"]),
            duration_ms=float(row["duration_ms"]),
            error_code=str(row["error_code"]),
        )

    def admit(self, instance_id: str, payload: dict[str, Any]) -> None:
        """Persist exact source/owner evidence before a worker can execute it."""
        encoded = _json(payload)
        with self._transaction(immediate=True) as connection:
            connection.execute('INSERT INTO kernel_cell_admission VALUES (?, ?, ?, ?)',
                               (payload['execution_id'], instance_id, encoded, time.time()))

    def unsettled(self) -> tuple[dict[str, Any], ...]:
        with self._transaction() as connection:
            rows = connection.execute('''SELECT a.instance_id,a.payload_json FROM kernel_cell_admission a
                LEFT JOIN kernel_cell_ledger l ON a.execution_id=l.execution_id
                WHERE l.execution_id IS NULL ORDER BY a.created_at''').fetchall()
        return tuple({'instance_id': row['instance_id'], **json.loads(row['payload_json'])} for row in rows)

    def append(
        self,
        *,
        execution_id: str,
        chat_id: str,
        run_id: str,
        outer_tool_call_id: str,
        kernel_generation: int,
        workspace_revision: int,
        workspace_fingerprint: str,
        workspace_root_ids: tuple[str, ...],
        work_scope: dict[str, Any],
        source_ref: str,
        source_sha256: str,
        result_ref: str,
        result_sha256: str,
        status: str,
        execution_count: int,
        started_at: float,
        completed_at: float,
        duration_ms: float,
        error_code: str = "",
    ) -> KernelCellRecord:
        clean_execution = str(execution_id or "").strip()
        clean_chat = str(chat_id or "").strip()
        if not clean_execution or not clean_chat:
            raise ValueError("execution_id and chat_id are required")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO kernel_cell_ledger(
                    execution_id, chat_id, run_id, outer_tool_call_id,
                    kernel_generation, workspace_revision,
                    workspace_fingerprint, workspace_root_ids_json,
                    work_scope_json, source_ref, source_sha256, result_ref,
                    result_sha256, status, execution_count, started_at,
                    completed_at, duration_ms, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_execution,
                    clean_chat,
                    str(run_id or ""),
                    str(outer_tool_call_id or ""),
                    int(kernel_generation),
                    max(0, int(workspace_revision)),
                    str(workspace_fingerprint or ""),
                    _json(list(workspace_root_ids)),
                    _json(dict(work_scope or {})),
                    str(source_ref or ""),
                    str(source_sha256 or ""),
                    str(result_ref or ""),
                    str(result_sha256 or ""),
                    str(status or "unknown"),
                    max(0, int(execution_count)),
                    float(started_at),
                    float(completed_at),
                    max(0.0, float(duration_ms)),
                    str(error_code or ""),
                    time.time(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM kernel_cell_ledger WHERE execution_id=?",
                (clean_execution,),
            ).fetchone()
        if row is None:
            raise RuntimeError("kernel cell ledger insert was not observable")
        return self._record(row)

    def get(self, execution_id: str) -> KernelCellRecord:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM kernel_cell_ledger WHERE execution_id=?",
                (str(execution_id or ""),),
            ).fetchone()
        if row is None:
            raise KeyError(str(execution_id or ""))
        return self._record(row)

    def for_outer_call(self, chat_id: str, run_id: str, call_id: str) -> dict:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM kernel_cell_ledger WHERE chat_id=? AND run_id=? AND outer_tool_call_id=? ORDER BY sequence",
                (chat_id, run_id, call_id),
            ).fetchall()
            admissions = connection.execute(
                "SELECT payload_json FROM kernel_cell_admission WHERE "
                "json_extract(payload_json,'$.chat_id')=? AND json_extract(payload_json,'$.run_id')=? "
                "AND json_extract(payload_json,'$.outer_tool_call_id')=?",
                (chat_id, run_id, call_id),
            ).fetchall()
        return {"cells": tuple(self._record(row) for row in rows),
                "admissions": tuple(json.loads(row[0]) for row in admissions)}

    def list(
        self,
        chat_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[KernelCellRecord, ...]:
        cap = max(1, min(int(limit or 100), _MAX_PAGE))
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM kernel_cell_ledger
                WHERE chat_id=? AND sequence>?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (str(chat_id or ""), max(0, int(after_sequence)), cap),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def tail(self, chat_id: str, *, limit: int = 100) -> tuple[KernelCellRecord, ...]:
        cap = max(1, min(int(limit or 100), _MAX_PAGE))
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM kernel_cell_ledger
                WHERE chat_id=?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (str(chat_id or ""), cap),
            ).fetchall()
        return tuple(reversed(tuple(self._record(row) for row in rows)))

    def latest(self, chat_id: str) -> KernelCellRecord | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM kernel_cell_ledger WHERE chat_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (str(chat_id or ""),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def outcome_snapshot(self, chat_id: str) -> KernelCellOutcomeSnapshot:
        """Return exact non-OK history for one durable chat in one DB snapshot."""
        clean_chat = str(chat_id or "").strip()
        with self._transaction() as connection:
            row = connection.execute(
                """
                WITH scoped AS (
                    SELECT sequence, execution_id, kernel_generation, status, error_code
                    FROM kernel_cell_ledger
                    WHERE chat_id=?
                ), summary AS (
                    SELECT
                        COUNT(*) AS cell_count,
                        COALESCE(SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END), 0)
                            AS non_ok_count,
                        MAX(CASE WHEN status<>'ok' THEN sequence END)
                            AS latest_non_ok_sequence
                    FROM scoped
                )
                SELECT
                    summary.cell_count,
                    summary.non_ok_count,
                    COALESCE(summary.latest_non_ok_sequence, 0)
                        AS latest_non_ok_sequence,
                    COALESCE(latest.execution_id, '') AS latest_non_ok_execution_id,
                    COALESCE(latest.kernel_generation, 0)
                        AS latest_non_ok_kernel_generation,
                    COALESCE(latest.status, '') AS latest_non_ok_status,
                    COALESCE(latest.error_code, '') AS latest_non_ok_error_code,
                    CASE
                        WHEN summary.latest_non_ok_sequence IS NULL THEN 0
                        ELSE (
                            SELECT COUNT(*) FROM scoped
                            WHERE sequence>summary.latest_non_ok_sequence
                        )
                    END AS later_cell_count
                FROM summary
                LEFT JOIN scoped AS latest
                    ON latest.sequence=summary.latest_non_ok_sequence
                """,
                (clean_chat,),
            ).fetchone()
        if row is None:
            raise RuntimeError("kernel cell outcome snapshot was not observable")
        return KernelCellOutcomeSnapshot(
            chat_id=clean_chat,
            cell_count=int(row["cell_count"]),
            non_ok_count=int(row["non_ok_count"]),
            latest_non_ok_execution_id=str(row["latest_non_ok_execution_id"]),
            latest_non_ok_sequence=int(row["latest_non_ok_sequence"]),
            latest_non_ok_kernel_generation=int(
                row["latest_non_ok_kernel_generation"]
            ),
            latest_non_ok_status=str(row["latest_non_ok_status"]),
            latest_non_ok_error_code=str(row["latest_non_ok_error_code"]),
            later_cell_count=int(row["later_cell_count"]),
        )


__all__ = [
    "CELL_ENTRY_SCHEMA",
    "CELL_LEDGER_SCHEMA",
    "KernelCellLedgerStore",
    "KernelCellOutcomeSnapshot",
    "KernelCellRecord",
]
