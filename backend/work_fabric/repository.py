"""Transactional SQLite authority for VARIANT-1 product work.

The repository uses short-lived connections and one process-wide writer lock
per database path.  Every aggregate mutation appends its event and outbox row
inside the same ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from core_invariants import (
    canonical_json,
    request_fingerprint as canonical_request_fingerprint,
    sqlite_read_connection,
    sqlite_unit_of_work,
    sqlite_wal_connection,
    sqlite_writer_lock,
    strict_json_value,
)
from .models import (
    JOB_LEASED_STATES,
    JOB_STATES,
    JOB_TERMINAL_STATES,
    InvalidTransition,
    JobRecord,
    LeaseLost,
    OperationRecord,
    OutboxItem,
    ProjectionSnapshot,
    RepositoryCorrupt,
    WorkActor,
    WorkConflict,
    WorkEvent,
    WorkNotFound,
)
from .scope import (
    EMPTY_WORK_SCOPE,
    WorkScope,
    append_json_scope_visibility,
    coerce_work_scope,
)


SCHEMA_VERSION = 5
_DEFAULT_INLINE_JSON_BYTES = 4 * 1024 * 1024
_TERMINAL_OPERATION_STATES = frozenset({
    "succeeded", "failed", "cancelled", "unknown_effect"
})

def _backend_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def default_work_path(*, data_dir: str | None = None) -> str:
    """Return the Work database path.

    An explicit ``data_dir`` is already VARIANT-1's ``data`` directory.  The
    ``VARIANT1_DATA_DIR`` environment value and backend fallback are application
    roots, so ``data/work`` is appended to them.
    """

    if data_dir:
        root = os.path.abspath(data_dir)
    else:
        app_root = os.path.abspath(
            os.environ.get("VARIANT1_DATA_DIR") or _backend_root()
        )
        root = os.path.join(app_root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_WORK_DB")
        or os.path.join(root, "work", "work.sqlite3")
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: Any, field: str, *, required: bool = False, limit: int = 1000) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    if "\x00" in text:
        raise ValueError(f"{field} contains a NUL character")
    return text


def _json_value(value: Any, *, path: str = "$") -> Any:
    return strict_json_value(value, path=path)


def _max_inline_json_bytes() -> int:
    raw = os.environ.get("VARIANT1_WORK_MAX_INLINE_JSON_BYTES")
    try:
        value = int(raw) if raw else _DEFAULT_INLINE_JSON_BYTES
    except (TypeError, ValueError) as exc:
        raise ValueError("VARIANT1_WORK_MAX_INLINE_JSON_BYTES must be an integer") from exc
    return max(64 * 1024, value)


def _json(value: Any) -> str:
    encoded = canonical_json(_json_value(value))
    if len(encoded.encode("utf-8")) > _max_inline_json_bytes():
        raise ValueError(
            "inline Work Fabric JSON exceeds the configured limit; store it as an artifact"
        )
    return encoded


def _load_json(value: str | None, *, expected: type, field: str) -> Any:
    try:
        decoded = json.loads(value or ("{}" if expected is dict else "[]"))
    except Exception as exc:
        raise RepositoryCorrupt(f"invalid JSON in {field}: {exc}") from exc
    if not isinstance(decoded, expected):
        raise RepositoryCorrupt(
            f"invalid {field}: expected {expected.__name__}, got {type(decoded).__name__}"
        )
    return decoded


class WorkRepository:
    """Synchronous durable repository; safe to call from multiple threads."""

    def __init__(self, path: str | None = None, *, data_dir: str | None = None) -> None:
        if path and data_dir:
            raise ValueError("pass either path or data_dir, not both")
        self.path = os.path.abspath(path or default_work_path(data_dir=data_dir))
        self._write_lock = sqlite_writer_lock(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_wal_connection(self.path)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="work.before_commit"
        ) as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with sqlite_read_connection(self._connect) as conn:
            yield conn

    def _initialize(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS work_schema_migration (
                        version INTEGER PRIMARY KEY,
                        applied_at REAL NOT NULL,
                        description TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS work_event (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        aggregate_kind TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        aggregate_version INTEGER NOT NULL CHECK(aggregate_version > 0),
                        event_type TEXT NOT NULL,
                        scope_json TEXT NOT NULL,
                        actor_kind TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        correlation_id TEXT NOT NULL DEFAULT '',
                        causation_id TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        request_fingerprint TEXT NOT NULL,
                        payload_ref TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        UNIQUE(aggregate_kind, aggregate_id, aggregate_version)
                    );
                    CREATE INDEX IF NOT EXISTS idx_work_event_aggregate
                        ON work_event(aggregate_kind, aggregate_id, sequence);
                    CREATE INDEX IF NOT EXISTS idx_work_event_type
                        ON work_event(event_type, sequence);
                    CREATE INDEX IF NOT EXISTS idx_work_event_correlation
                        ON work_event(correlation_id, sequence);
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_work_event_idempotency
                        ON work_event(aggregate_kind, aggregate_id, idempotency_key)
                        WHERE idempotency_key <> '';
                    CREATE TRIGGER IF NOT EXISTS trg_work_event_no_update
                    BEFORE UPDATE ON work_event
                    BEGIN
                        SELECT RAISE(ABORT, 'work_event is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_work_event_no_delete
                    BEFORE DELETE ON work_event
                    BEGIN
                        SELECT RAISE(ABORT, 'work_event is append-only');
                    END;

                    CREATE TABLE IF NOT EXISTS work_outbox (
                        outbox_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL UNIQUE,
                        event_sequence INTEGER NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','leased','delivered','dead')),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        available_at REAL NOT NULL,
                        lease_owner TEXT NOT NULL DEFAULT '',
                        lease_epoch INTEGER NOT NULL DEFAULT 0,
                        lease_expires_at REAL,
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        delivered_at REAL,
                        FOREIGN KEY(event_id) REFERENCES work_event(event_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_work_outbox_delivery
                        ON work_outbox(status, available_at, event_sequence);

                    CREATE TABLE IF NOT EXISTS work_job (
                        job_id TEXT PRIMARY KEY,
                        owner_kind TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN ('queued','leased','running','waiting','paused',
                                             'succeeded','failed','cancelled','unknown_effect')),
                        scope_json TEXT NOT NULL,
                        chat_id TEXT NOT NULL DEFAULT '',
                        workspace_id TEXT NOT NULL DEFAULT '',
                        goal_id TEXT NOT NULL DEFAULT '',
                        priority INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        input_manifest_json TEXT NOT NULL DEFAULT '{}',
                        artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                        lease_owner TEXT NOT NULL DEFAULT '',
                        lease_epoch INTEGER NOT NULL DEFAULT 0,
                        lease_expires_at REAL,
                        continuation INTEGER NOT NULL DEFAULT 0,
                        sync_handler INTEGER NOT NULL DEFAULT 0,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 1,
                        retry_policy_json TEXT NOT NULL DEFAULT '{}',
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        event_cursor INTEGER NOT NULL DEFAULT 0,
                        result_ref TEXT NOT NULL DEFAULT '',
                        diagnostics_ref TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        request_fingerprint TEXT NOT NULL,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        cancel_reason TEXT NOT NULL DEFAULT '',
                        available_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        started_at REAL,
                        heartbeat_at REAL,
                        updated_at REAL NOT NULL,
                        completed_at REAL,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_work_job_ready
                        ON work_job(status, available_at, priority DESC, created_at, job_id);
                    CREATE INDEX IF NOT EXISTS idx_work_job_scope
                        ON work_job(chat_id, workspace_id, goal_id, status, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_work_job_owner
                        ON work_job(owner_kind, owner_id, status, updated_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_work_job_idempotency
                        ON work_job(owner_kind, owner_id, kind, idempotency_key)
                        WHERE idempotency_key <> '';

                    CREATE TABLE IF NOT EXISTS work_artifact_object (
                        artifact_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        name TEXT NOT NULL,
                        scope_json TEXT NOT NULL,
                        current_revision INTEGER NOT NULL DEFAULT 0,
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        tombstoned_at REAL
                    );

                    CREATE TABLE IF NOT EXISTS work_artifact_revision (
                        artifact_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        content_ref TEXT NOT NULL,
                        media_type TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        PRIMARY KEY(artifact_id, revision),
                        FOREIGN KEY(artifact_id) REFERENCES work_artifact_object(artifact_id)
                    );

                    CREATE TABLE IF NOT EXISTS work_artifact_link (
                        link_id TEXT PRIMARY KEY,
                        artifact_id TEXT NOT NULL,
                        artifact_revision INTEGER NOT NULL,
                        owner_kind TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(artifact_id, artifact_revision)
                            REFERENCES work_artifact_revision(artifact_id, revision)
                    );
                    CREATE INDEX IF NOT EXISTS idx_work_artifact_link_owner
                        ON work_artifact_link(owner_kind, owner_id, role);

                    CREATE TABLE IF NOT EXISTS work_operation (
                        operation_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN ('planned','running','succeeded','failed',
                                             'cancelled','unknown_effect')),
                        scope_json TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        request_fingerprint TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        response_json TEXT NOT NULL DEFAULT '{}',
                        effect_ref TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        completed_at REAL,
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_work_operation_idempotency
                        ON work_operation(kind, idempotency_key)
                        WHERE idempotency_key <> '';
                    CREATE INDEX IF NOT EXISTS idx_work_operation_recovery
                        ON work_operation(status, updated_at);
                    CREATE INDEX IF NOT EXISTS idx_work_operation_chat_activity
                        ON work_operation(json_extract(scope_json,'$.chat_id'),updated_at DESC);
                    CREATE TABLE IF NOT EXISTS work_operation_clock(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);
                    INSERT OR IGNORE INTO work_operation_clock VALUES (1,0);
                    CREATE TRIGGER IF NOT EXISTS work_operation_insert_clock AFTER INSERT ON work_operation
                    BEGIN UPDATE work_operation_clock SET revision=revision+1 WHERE id=1; END;
                    CREATE TRIGGER IF NOT EXISTS work_operation_update_clock AFTER UPDATE ON work_operation
                    BEGIN UPDATE work_operation_clock SET revision=revision+1 WHERE id=1; END;
                    CREATE TRIGGER IF NOT EXISTS work_operation_delete_clock AFTER DELETE ON work_operation
                    BEGIN UPDATE work_operation_clock SET revision=revision+1 WHERE id=1; END;

                    CREATE TABLE IF NOT EXISTS work_interaction (
                        interaction_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        owner_kind TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'open','answered','skipped','timed_out',
                            'dismissed','cancelled'
                        )),
                        prompt TEXT NOT NULL,
                        schema_json TEXT NOT NULL DEFAULT '{}',
                        response_json TEXT NOT NULL DEFAULT '{}',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        scope_json TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        version INTEGER NOT NULL DEFAULT 1,
                        expires_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        resolved_at REAL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_work_interaction_idempotency
                        ON work_interaction(owner_kind, owner_id, kind, idempotency_key)
                        WHERE idempotency_key <> '';
                    CREATE INDEX IF NOT EXISTS idx_work_interaction_open
                        ON work_interaction(status, created_at, interaction_id);

                    CREATE TABLE IF NOT EXISTS work_projection_state (
                        projection_name TEXT PRIMARY KEY,
                        last_sequence INTEGER NOT NULL DEFAULT 0,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        updated_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO work_schema_migration"
                    "(version, applied_at, description) VALUES (?, ?, ?)",
                    (SCHEMA_VERSION, time.time(), "Fence synchronous Work handlers on lease expiry"),
                )
                job_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(work_job)").fetchall()
                }
                if "continuation" not in job_columns:
                    conn.execute(
                        "ALTER TABLE work_job ADD COLUMN continuation "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                if "sync_handler" not in job_columns:
                    conn.execute(
                        "ALTER TABLE work_job ADD COLUMN sync_handler "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
                    # Older persisted running jobs have no mode evidence.
                    # Treat them conservatively at a future lease expiry;
                    # silently retrying one possible live thread is unsafe.
                    conn.execute(
                        "UPDATE work_job SET sync_handler=1 WHERE status='running'"
                    )
                if "request_fingerprint" not in job_columns:
                    conn.execute(
                        "ALTER TABLE work_job ADD COLUMN "
                        "request_fingerprint TEXT NOT NULL DEFAULT ''"
                    )
                for row in conn.execute(
                    "SELECT * FROM work_job WHERE request_fingerprint=''"
                ).fetchall():
                    conn.execute(
                        "UPDATE work_job SET request_fingerprint=? "
                        "WHERE job_id=? AND request_fingerprint=''",
                        (
                            canonical_request_fingerprint("work.job.create", {
                                "kind": str(row["kind"]),
                                "owner": {
                                    "kind": str(row["owner_kind"]),
                                    "id": str(row["owner_id"]),
                                },
                                "scope": _load_json(
                                    row["scope_json"], expected=dict,
                                    field="work_job.scope_json",
                                ),
                                "priority": int(row["priority"] or 0),
                                "input_manifest": _load_json(
                                    row["input_manifest_json"], expected=dict,
                                    field="work_job.input_manifest_json",
                                ),
                                "artifact_refs": _load_json(
                                    row["artifact_refs_json"], expected=list,
                                    field="work_job.artifact_refs_json",
                                ),
                                "retry_policy": _load_json(
                                    row["retry_policy_json"], expected=dict,
                                    field="work_job.retry_policy_json",
                                ),
                                "max_attempts": int(row["max_attempts"] or 1),
                            }),
                            str(row["job_id"]),
                        ),
                    )
                event_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(work_event)"
                    ).fetchall()
                }
                if "request_fingerprint" not in event_columns:
                    conn.execute(
                        "ALTER TABLE work_event ADD COLUMN "
                        "request_fingerprint TEXT NOT NULL DEFAULT ''"
                    )
                operation_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(work_operation)"
                    ).fetchall()
                }
                if "request_fingerprint" not in operation_columns:
                    conn.execute(
                        "ALTER TABLE work_operation ADD COLUMN "
                        "request_fingerprint TEXT NOT NULL DEFAULT ''"
                    )
                for row in conn.execute(
                    "SELECT operation_id,kind,scope_json,request_json "
                    "FROM work_operation "
                    "WHERE request_fingerprint=''"
                ).fetchall():
                    request_value = _load_json(
                        row["request_json"], expected=dict,
                        field="work_operation.request_json",
                    )
                    conn.execute(
                        "UPDATE work_operation SET request_fingerprint=? "
                        "WHERE operation_id=? AND request_fingerprint=''",
                        (
                            canonical_request_fingerprint(
                                str(row["kind"]),
                                {
                                    **{
                                        key: value for key, value in request_value.items()
                                        if key != "attribution"
                                    },
                                    "scope": _load_json(
                                        row["scope_json"], expected=dict,
                                        field="work_operation.scope_json",
                                    ),
                                },
                            ),
                            str(row["operation_id"]),
                        ),
                    )
            finally:
                conn.close()

    @staticmethod
    def _event_from_row(row: sqlite3.Row | None) -> WorkEvent | None:
        if row is None:
            return None
        return WorkEvent(
            event_id=str(row["event_id"]),
            sequence=int(row["sequence"]),
            aggregate_kind=str(row["aggregate_kind"]),
            aggregate_id=str(row["aggregate_id"]),
            aggregate_version=int(row["aggregate_version"]),
            event_type=str(row["event_type"]),
            scope=WorkScope.from_mapping(_load_json(
                row["scope_json"], expected=dict, field="work_event.scope_json"
            )),
            actor=WorkActor(str(row["actor_kind"]), str(row["actor_id"])),
            correlation_id=str(row["correlation_id"] or ""),
            causation_id=str(row["causation_id"] or ""),
            idempotency_key=str(row["idempotency_key"] or ""),
            request_fingerprint=str(row["request_fingerprint"] or ""),
            payload_ref=str(row["payload_ref"] or ""),
            payload=_load_json(
                row["payload_json"], expected=dict, field="work_event.payload_json"
            ),
            created_at=float(row["created_at"]),
        )

    def _insert_event_tx(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        aggregate_kind: str,
        aggregate_id: str,
        aggregate_version: int | None = None,
        expected_aggregate_version: int | None = None,
        scope: WorkScope | Mapping[str, Any] | None = None,
        actor: WorkActor | None = None,
        correlation_id: str = "",
        causation_id: str = "",
        idempotency_key: str = "",
        payload_ref: str = "",
        payload: Mapping[str, Any] | None = None,
        created_at: float | None = None,
    ) -> WorkEvent:
        kind = _text(aggregate_kind, "aggregate_kind", required=True, limit=100)
        aggregate = _text(aggregate_id, "aggregate_id", required=True, limit=512)
        event_name = _text(event_type, "event_type", required=True, limit=240)
        idem = _text(idempotency_key, "idempotency_key", limit=512)
        resolved_scope = coerce_work_scope(scope)
        raw_actor = actor or WorkActor()
        resolved_actor = WorkActor(
            _text(raw_actor.kind, "actor.kind", required=True, limit=100),
            _text(raw_actor.actor_id, "actor.id", required=True, limit=512),
        )
        clean_correlation = _text(correlation_id, "correlation_id", limit=512)
        clean_causation = _text(causation_id, "causation_id", limit=512)
        clean_payload_ref = _text(payload_ref, "payload_ref", limit=2000)
        clean_payload = dict(payload or {})
        def fingerprint_for(version: int) -> str:
            return canonical_request_fingerprint("work.event.append", {
                "aggregate": {"kind": kind, "id": aggregate, "version": version},
                "event_type": event_name,
                "scope": resolved_scope.to_dict(),
                "actor": resolved_actor.to_dict(),
                "correlation_id": clean_correlation,
                "causation_id": clean_causation,
                "payload_ref": clean_payload_ref,
                "payload": clean_payload,
            })
        if idem:
            existing = conn.execute(
                "SELECT * FROM work_event WHERE aggregate_kind=? AND aggregate_id=? "
                "AND idempotency_key=?",
                (kind, aggregate, idem),
            ).fetchone()
            if existing is not None:
                prior = self._event_from_row(existing)
                same_version = (
                    aggregate_version is None
                    or prior.aggregate_version == int(aggregate_version)
                ) and (
                    expected_aggregate_version is None
                    or prior.aggregate_version - 1
                    == int(expected_aggregate_version)
                )
                candidate_fingerprint = fingerprint_for(prior.aggregate_version)
                if (
                    prior is None
                    or (
                        prior.request_fingerprint
                        and prior.request_fingerprint != candidate_fingerprint
                    )
                    or prior.event_type != event_name
                    or not same_version
                    or prior.scope != resolved_scope
                    or prior.actor != resolved_actor
                    or prior.correlation_id != clean_correlation
                    or prior.causation_id != clean_causation
                    or prior.payload_ref != clean_payload_ref
                    or prior.payload != clean_payload
                ):
                    raise WorkConflict("event idempotency key was reused for another command")
                return prior
        current = int(conn.execute(
            "SELECT COALESCE(MAX(aggregate_version), 0) FROM work_event "
            "WHERE aggregate_kind=? AND aggregate_id=?",
            (kind, aggregate),
        ).fetchone()[0])
        if expected_aggregate_version is not None and current != int(expected_aggregate_version):
            raise WorkConflict(
                f"aggregate version changed for {kind}:{aggregate} "
                f"({current} != {int(expected_aggregate_version)})"
            )
        version = current + 1 if aggregate_version is None else int(aggregate_version)
        if version != current + 1:
            raise WorkConflict(
                f"event version must advance exactly once for {kind}:{aggregate} "
                f"({version} != {current + 1})"
            )
        fingerprint = fingerprint_for(version)
        now = float(created_at if created_at is not None else time.time())
        event_id = _new_id("evt")
        cursor = conn.execute(
            """
            INSERT INTO work_event(
                event_id, aggregate_kind, aggregate_id, aggregate_version,
                event_type, scope_json, actor_kind, actor_id, correlation_id,
                causation_id, idempotency_key, request_fingerprint, payload_ref,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, kind, aggregate, version, event_name,
                _json(resolved_scope.to_dict()),
                resolved_actor.kind,
                resolved_actor.actor_id,
                clean_correlation,
                clean_causation,
                idem,
                fingerprint,
                clean_payload_ref,
                _json(clean_payload),
                now,
            ),
        )
        sequence = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO work_outbox(
                outbox_id, event_id, event_sequence, status, attempts,
                available_at, created_at
            ) VALUES (?, ?, ?, 'pending', 0, ?, ?)
            """,
            (_new_id("out"), event_id, sequence, now, now),
        )
        row = conn.execute("SELECT * FROM work_event WHERE sequence=?", (sequence,)).fetchone()
        event = self._event_from_row(row)
        if event is None:
            raise RepositoryCorrupt("newly inserted work event disappeared")
        return event

    def append_event(self, **kwargs: Any) -> WorkEvent:
        """Append an aggregate event and pending outbox item atomically."""

        with self._write() as conn:
            return self._insert_event_tx(conn, **kwargs)

    def event(self, event_id: str) -> WorkEvent | None:
        with self._read() as conn:
            return self._event_from_row(conn.execute(
                "SELECT * FROM work_event WHERE event_id=?", (str(event_id),)
            ).fetchone())

    def list_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        aggregate_kind: str = "",
        aggregate_id: str = "",
        event_type: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> list[WorkEvent]:
        clauses = ["sequence > ?"]
        params: list[Any] = [max(0, int(after_sequence))]
        if aggregate_kind:
            clauses.append("aggregate_kind=?")
            params.append(str(aggregate_kind))
        if aggregate_id:
            clauses.append("aggregate_id=?")
            params.append(str(aggregate_id))
        if event_type:
            clauses.append("event_type=?")
            params.append(str(event_type))
        append_json_scope_visibility(clauses, params, "scope_json", scope)
        params.append(min(5000, max(1, int(limit))))
        sql = (
            "SELECT * FROM work_event WHERE " + " AND ".join(clauses)
            + " ORDER BY sequence LIMIT ?"
        )
        with self._read() as conn:
            return [
                event for event in (
                    self._event_from_row(row)
                    for row in conn.execute(sql, tuple(params)).fetchall()
                ) if event is not None
            ]

    @staticmethod
    def _outbox_from_row(row: sqlite3.Row | None) -> OutboxItem | None:
        if row is None:
            return None
        event = WorkRepository._event_from_row(row)
        if event is None:
            raise RepositoryCorrupt("outbox row has no event")
        return OutboxItem(
            outbox_id=str(row["outbox_id"]),
            event=event,
            status=str(row["outbox_status"]),
            attempts=int(row["outbox_attempts"] or 0),
            available_at=float(row["outbox_available_at"] or 0),
            lease_owner=str(row["outbox_lease_owner"] or ""),
            lease_epoch=int(row["outbox_lease_epoch"] or 0),
            lease_expires_at=float(row["outbox_lease_expires_at"] or 0),
            last_error=str(row["outbox_last_error"] or ""),
            created_at=float(row["outbox_created_at"] or 0),
            delivered_at=float(row["outbox_delivered_at"] or 0),
        )

    @staticmethod
    def _outbox_select() -> str:
        return (
            "SELECT e.*, o.outbox_id, o.status AS outbox_status, "
            "o.attempts AS outbox_attempts, o.available_at AS outbox_available_at, "
            "o.lease_owner AS outbox_lease_owner, "
            "o.lease_epoch AS outbox_lease_epoch, "
            "o.lease_expires_at AS outbox_lease_expires_at, "
            "o.last_error AS outbox_last_error, "
            "o.created_at AS outbox_created_at, "
            "o.delivered_at AS outbox_delivered_at "
            "FROM work_outbox AS o JOIN work_event AS e ON e.event_id=o.event_id "
        )

    def claim_outbox(
        self,
        consumer_id: str,
        *,
        limit: int = 100,
        lease_ttl_s: float = 30.0,
        now: float | None = None,
    ) -> list[OutboxItem]:
        owner = _text(consumer_id, "consumer_id", required=True, limit=512)
        at = float(now if now is not None else time.time())
        expires = at + max(1.0, float(lease_ttl_s))
        take = min(1000, max(1, int(limit)))
        with self._write() as conn:
            conn.execute(
                "UPDATE work_outbox SET status='pending', lease_owner='', "
                "lease_expires_at=NULL, available_at=? "
                "WHERE status='leased' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at<=?",
                (at, at),
            )
            rows = conn.execute(
                "SELECT outbox_id FROM work_outbox WHERE status='pending' "
                "AND available_at<=? ORDER BY event_sequence LIMIT ?",
                (at, take),
            ).fetchall()
            ids = [str(row["outbox_id"]) for row in rows]
            for outbox_id in ids:
                changed = conn.execute(
                    "UPDATE work_outbox SET status='leased', lease_owner=?, "
                    "lease_epoch=lease_epoch+1, lease_expires_at=?, "
                    "attempts=attempts+1 WHERE outbox_id=? AND status='pending'",
                    (owner, expires, outbox_id),
                )
                if changed.rowcount != 1:
                    raise WorkConflict(f"outbox claim lost for {outbox_id}")
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            claimed = conn.execute(
                self._outbox_select()
                + f"WHERE o.outbox_id IN ({placeholders}) ORDER BY o.event_sequence",
                tuple(ids),
            ).fetchall()
            return [
                item for item in (
                    self._outbox_from_row(row) for row in claimed
                ) if item is not None
            ]

    def acknowledge_outbox(
        self,
        outbox_id: str,
        *,
        consumer_id: str,
        lease_epoch: int,
        now: float | None = None,
    ) -> None:
        at = float(now if now is not None else time.time())
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE work_outbox SET status='delivered', delivered_at=?, "
                "lease_owner='', lease_expires_at=NULL, last_error='' "
                "WHERE outbox_id=? AND status='leased' AND lease_owner=? "
                "AND lease_epoch=?",
                (at, str(outbox_id), str(consumer_id), int(lease_epoch)),
            )
            if changed.rowcount != 1:
                raise LeaseLost(f"outbox lease lost for {outbox_id}")

    def reject_outbox(
        self,
        outbox_id: str,
        *,
        consumer_id: str,
        lease_epoch: int,
        error: str,
        retry_delay_s: float = 1.0,
        max_attempts: int = 20,
        now: float | None = None,
    ) -> str:
        at = float(now if now is not None else time.time())
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM work_outbox WHERE outbox_id=?",
                (str(outbox_id),),
            ).fetchone()
            if row is None:
                raise WorkNotFound(f"unknown outbox item: {outbox_id}")
            if (
                row["status"] != "leased"
                or str(row["lease_owner"] or "") != str(consumer_id)
                or int(row["lease_epoch"] or 0) != int(lease_epoch)
            ):
                raise LeaseLost(f"outbox lease lost for {outbox_id}")
            dead = int(row["attempts"] or 0) >= max(1, int(max_attempts))
            status = "dead" if dead else "pending"
            conn.execute(
                "UPDATE work_outbox SET status=?, available_at=?, lease_owner='', "
                "lease_expires_at=NULL, last_error=? WHERE outbox_id=?",
                (
                    status,
                    at if dead else at + max(0.0, float(retry_delay_s)),
                    _text(error, "outbox error", limit=4000),
                    str(outbox_id),
                ),
            )
            return status

    def recover_expired_outbox(self, *, now: float | None = None) -> int:
        at = float(now if now is not None else time.time())
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE work_outbox SET status='pending', available_at=?, "
                "lease_owner='', lease_expires_at=NULL, "
                "last_error=CASE WHEN last_error='' THEN 'delivery lease expired' "
                "ELSE last_error END WHERE status='leased' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                (at, at),
            )
            return int(changed.rowcount)

    @staticmethod
    def _job_from_row(row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        status = str(row["status"])
        if status not in JOB_STATES:
            raise RepositoryCorrupt(f"invalid persisted job state: {status!r}")
        refs = _load_json(
            row["artifact_refs_json"], expected=list, field="work_job.artifact_refs_json"
        )
        if any(not isinstance(item, str) for item in refs):
            raise RepositoryCorrupt("work_job.artifact_refs_json contains a non-string")
        return JobRecord(
            job_id=str(row["job_id"]),
            owner_kind=str(row["owner_kind"]),
            owner_id=str(row["owner_id"]),
            kind=str(row["kind"]),
            status=status,
            scope=WorkScope.from_mapping(_load_json(
                row["scope_json"], expected=dict, field="work_job.scope_json"
            )),
            priority=int(row["priority"] or 0),
            revision=int(row["revision"] or 0),
            input_manifest=_load_json(
                row["input_manifest_json"], expected=dict,
                field="work_job.input_manifest_json",
            ),
            artifact_refs=tuple(refs),
            lease_owner=str(row["lease_owner"] or ""),
            lease_epoch=int(row["lease_epoch"] or 0),
            lease_expires_at=float(row["lease_expires_at"] or 0),
            continuation=bool(row["continuation"]),
            attempt=int(row["attempt"] or 0),
            max_attempts=int(row["max_attempts"] or 1),
            retry_policy=_load_json(
                row["retry_policy_json"], expected=dict,
                field="work_job.retry_policy_json",
            ),
            progress=_load_json(
                row["progress_json"], expected=dict, field="work_job.progress_json"
            ),
            event_cursor=int(row["event_cursor"] or 0),
            result_ref=str(row["result_ref"] or ""),
            diagnostics_ref=str(row["diagnostics_ref"] or ""),
            idempotency_key=str(row["idempotency_key"] or ""),
            request_fingerprint=str(row["request_fingerprint"] or ""),
            cancel_requested=bool(row["cancel_requested"]),
            cancel_reason=str(row["cancel_reason"] or ""),
            available_at=float(row["available_at"] or 0),
            created_at=float(row["created_at"] or 0),
            started_at=float(row["started_at"] or 0),
            heartbeat_at=float(row["heartbeat_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            completed_at=float(row["completed_at"] or 0),
            error=str(row["error"] or ""),
        )

    @staticmethod
    def _job_payload(row: sqlite3.Row, **extra: Any) -> dict[str, Any]:
        payload = {
            "job_id": str(row["job_id"]),
            "kind": str(row["kind"]),
            "status": str(row["status"]),
            "revision": int(row["revision"]),
            "attempt": int(row["attempt"] or 0),
            "max_attempts": int(row["max_attempts"] or 1),
            "continuation": bool(row["continuation"]),
            "progress": _load_json(
                row["progress_json"], expected=dict, field="work_job.progress_json"
            ),
            "cancel_requested": bool(row["cancel_requested"]),
        }
        payload.update(extra)
        return payload

    def _job_event_tx(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        event_type: str,
        *,
        actor: WorkActor | None = None,
        payload: Mapping[str, Any] | None = None,
        correlation_id: str = "",
        causation_id: str = "",
        idempotency_key: str = "",
    ) -> WorkEvent:
        version = int(row["revision"])
        event = self._insert_event_tx(
            conn,
            event_type=event_type,
            aggregate_kind="job",
            aggregate_id=str(row["job_id"]),
            aggregate_version=version,
            expected_aggregate_version=version - 1,
            scope=WorkScope.from_mapping(_load_json(
                row["scope_json"], expected=dict, field="work_job.scope_json"
            )),
            actor=actor,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            payload=self._job_payload(row, **dict(payload or {})),
        )
        conn.execute(
            "UPDATE work_job SET event_cursor=? WHERE job_id=?",
            (event.sequence, str(row["job_id"])),
        )
        return event

    def create_job(
        self,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        scope: WorkScope | Mapping[str, Any] | None = None,
        priority: int = 0,
        input_manifest: Mapping[str, Any] | None = None,
        artifact_refs: Iterable[str] = (),
        retry_policy: Mapping[str, Any] | None = None,
        max_attempts: int = 1,
        idempotency_key: str = "",
        available_at: float | None = None,
        job_id: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
    ) -> JobRecord:
        resolved_scope = coerce_work_scope(scope)
        clean_kind = _text(kind, "job kind", required=True, limit=240)
        clean_owner_kind = _text(owner_kind, "owner kind", required=True, limit=100)
        clean_owner_id = _text(owner_id, "owner id", required=True, limit=512)
        clean_idem = _text(idempotency_key, "idempotency_key", limit=512)
        refs = tuple(_text(item, "artifact ref", required=True, limit=2000) for item in artifact_refs)
        if len(refs) > 1000:
            raise ValueError("a job cannot reference more than 1000 input artifacts")
        retries = max(1, int(max_attempts))
        now = time.time()
        ready_at = float(available_at if available_at is not None else now)
        requested_identifier = _text(job_id, "job_id", limit=512)
        identifier = requested_identifier or _new_id("job")
        clean_manifest = dict(input_manifest or {})
        clean_retry_policy = dict(retry_policy or {})
        fingerprint = canonical_request_fingerprint("work.job.create", {
            "kind": clean_kind,
            "owner": {"kind": clean_owner_kind, "id": clean_owner_id},
            "scope": resolved_scope.to_dict(),
            "priority": int(priority),
            "input_manifest": clean_manifest,
            "artifact_refs": list(refs),
            "retry_policy": clean_retry_policy,
            "max_attempts": retries,
        })
        with self._write() as conn:
            if clean_idem:
                prior = conn.execute(
                    "SELECT * FROM work_job WHERE owner_kind=? AND owner_id=? "
                    "AND kind=? AND idempotency_key=?",
                    (clean_owner_kind, clean_owner_id, clean_kind, clean_idem),
                ).fetchone()
                if prior is not None:
                    existing = self._job_from_row(prior)
                    if existing is None:
                        raise RepositoryCorrupt("idempotent job row disappeared")
                    if (
                        existing.request_fingerprint != fingerprint
                        or (requested_identifier and existing.job_id != requested_identifier)
                        or existing.scope != resolved_scope
                        or existing.priority != int(priority)
                        or existing.input_manifest != clean_manifest
                        or existing.artifact_refs != refs
                        or existing.retry_policy != clean_retry_policy
                        or existing.max_attempts != retries
                        or (
                            available_at is not None
                            and existing.available_at != ready_at
                        )
                    ):
                        raise WorkConflict(
                            "job idempotency key was reused with different arguments"
                        )
                    return existing
            conn.execute(
                """
                INSERT INTO work_job(
                    job_id, owner_kind, owner_id, kind, status, scope_json,
                    chat_id, workspace_id, goal_id, priority, revision,
                    input_manifest_json, artifact_refs_json, attempt, max_attempts,
                    retry_policy_json, progress_json, idempotency_key, request_fingerprint,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?,
                          '{}', ?, ?, ?, ?, ?)
                """,
                (
                    identifier, clean_owner_kind, clean_owner_id, clean_kind,
                    _json(resolved_scope.to_dict()), resolved_scope.chat_id,
                    resolved_scope.workspace_id, resolved_scope.goal_id,
                    int(priority), _json(clean_manifest), _json(list(refs)),
                    retries, _json(clean_retry_policy), clean_idem, fingerprint,
                    ready_at, now, now,
                ),
            )
            row = conn.execute("SELECT * FROM work_job WHERE job_id=?", (identifier,)).fetchone()
            if row is None:
                raise RepositoryCorrupt("newly inserted work job disappeared")
            self._job_event_tx(
                conn,
                row,
                "job.created",
                actor=actor,
                correlation_id=correlation_id,
                idempotency_key=clean_idem,
            )
            row = conn.execute("SELECT * FROM work_job WHERE job_id=?", (identifier,)).fetchone()
            job = self._job_from_row(row)
            if job is None:
                raise RepositoryCorrupt("newly inserted work job disappeared")
            return job

    def get_job_by_idempotency(
        self, *, owner_kind: str, owner_id: str, kind: str, idempotency_key: str,
    ) -> JobRecord | None:
        with self._read() as conn:
            return self._job_from_row(conn.execute(
                "SELECT * FROM work_job WHERE owner_kind=? AND owner_id=? "
                "AND kind=? AND idempotency_key=?",
                (owner_kind, owner_id, kind, idempotency_key),
            ).fetchone())

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._read() as conn:
            return self._job_from_row(conn.execute(
                "SELECT * FROM work_job WHERE job_id=?", (str(job_id),)
            ).fetchone())

    def require_job(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job is None:
            raise WorkNotFound(f"unknown work job: {job_id}")
        return job

    def list_jobs(
        self,
        *,
        statuses: Iterable[str] = (),
        cancel_requested: bool | None = None,
        kind: str = "",
        owner_kind: str = "",
        owner_id: str = "",
        chat_id: str = "",
        workspace_id: str = "",
        goal_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        clean_statuses = tuple(dict.fromkeys(str(item) for item in statuses))
        unknown = set(clean_statuses).difference(JOB_STATES)
        if unknown:
            raise ValueError(f"unknown job state(s): {', '.join(sorted(unknown))}")
        if clean_statuses:
            clauses.append("status IN (" + ",".join("?" for _ in clean_statuses) + ")")
            params.extend(clean_statuses)
        if cancel_requested is not None:
            clauses.append("cancel_requested=?")
            params.append(int(bool(cancel_requested)))
        for column, value in (
            ("kind", kind), ("owner_kind", owner_kind), ("owner_id", owner_id),
            ("chat_id", chat_id), ("workspace_id", workspace_id), ("goal_id", goal_id),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(str(value))
        append_json_scope_visibility(clauses, params, "scope_json", scope)
        sql = "SELECT * FROM work_job"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, priority DESC, job_id LIMIT ?"
        params.append(min(5000, max(1, int(limit))))
        with self._read() as conn:
            return [
                job for job in (
                    self._job_from_row(row)
                    for row in conn.execute(sql, tuple(params)).fetchall()
                ) if job is not None
            ]

    @staticmethod
    def _job_row_tx(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM work_job WHERE job_id=?", (str(job_id),)
        ).fetchone()
        if row is None:
            raise WorkNotFound(f"unknown work job: {job_id}")
        return row

    @staticmethod
    def _expect_revision(row: sqlite3.Row, expected_revision: int | None) -> None:
        if expected_revision is not None and int(row["revision"]) != int(expected_revision):
            raise WorkConflict(
                f"job revision changed for {row['job_id']} "
                f"({int(row['revision'])} != {int(expected_revision)})"
            )

    @staticmethod
    def _expect_lease(
        row: sqlite3.Row,
        *,
        lease_owner: str,
        lease_epoch: int,
        now: float,
        allowed_states: Iterable[str] = JOB_LEASED_STATES,
    ) -> None:
        if str(row["status"]) not in set(allowed_states):
            raise LeaseLost(
                f"job {row['job_id']} is not in a leased state ({row['status']})"
            )
        if (
            str(row["lease_owner"] or "") != str(lease_owner)
            or int(row["lease_epoch"] or 0) != int(lease_epoch)
        ):
            raise LeaseLost(f"job lease epoch lost for {row['job_id']}")
        expires = float(row["lease_expires_at"] or 0)
        if expires <= now:
            raise LeaseLost(f"job lease expired for {row['job_id']}")

    def _return_job_tx(self, conn: sqlite3.Connection, job_id: str) -> JobRecord:
        job = self._job_from_row(self._job_row_tx(conn, job_id))
        if job is None:
            raise RepositoryCorrupt(f"work job disappeared: {job_id}")
        return job

    def lease_next_job(
        self,
        lease_owner: str,
        *,
        kinds: Iterable[str] = (),
        exclude_job_ids: Iterable[str] = (),
        lease_ttl_s: float = 60.0,
        now: float | None = None,
    ) -> JobRecord | None:
        owner = _text(lease_owner, "lease_owner", required=True, limit=512)
        at = float(now if now is not None else time.time())
        expires = at + max(2.0, float(lease_ttl_s))
        clean_kinds = tuple(dict.fromkeys(
            _text(item, "job kind", required=True, limit=240) for item in kinds
        ))
        excluded = tuple(dict.fromkeys(str(item) for item in exclude_job_ids))
        clauses = [
            "status='queued'", "available_at<=?", "cancel_requested=0",
            "(attempt < max_attempts OR continuation=1)",
        ]
        params: list[Any] = [at]
        if clean_kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in clean_kinds) + ")")
            params.extend(clean_kinds)
        if excluded:
            clauses.append("job_id NOT IN (" + ",".join("?" for _ in excluded) + ")")
            params.extend(excluded)
        sql = (
            "SELECT * FROM work_job WHERE " + " AND ".join(clauses)
            + " ORDER BY priority DESC, available_at, created_at, job_id LIMIT 1"
        )
        with self._write() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            if row is None:
                return None
            old_revision = int(row["revision"])
            changed = conn.execute(
                """
                UPDATE work_job SET
                    status='leased', lease_owner=?, lease_epoch=lease_epoch+1,
                    lease_expires_at=?,
                    attempt=attempt+CASE WHEN continuation=1 THEN 0 ELSE 1 END,
                    revision=revision+1,
                    heartbeat_at=?, updated_at=?,
                    started_at=COALESCE(started_at, ?), error=''
                WHERE job_id=? AND status='queued' AND revision=?
                """,
                (owner, expires, at, at, at, str(row["job_id"]), old_revision),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job claim lost for {row['job_id']}")
            row = self._job_row_tx(conn, str(row["job_id"]))
            self._job_event_tx(
                conn, row, "job.leased", actor=WorkActor("worker", owner),
                payload={"lease_epoch": int(row["lease_epoch"]), "lease_expires_at": expires},
            )
            return self._return_job_tx(conn, str(row["job_id"]))

    def start_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        sync_handler: bool = False,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> JobRecord:
        at = float(now if now is not None else time.time())
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_revision(row, expected_revision)
            self._expect_lease(
                row, lease_owner=lease_owner, lease_epoch=lease_epoch,
                now=at, allowed_states=("leased",),
            )
            changed = conn.execute(
                "UPDATE work_job SET status='running', continuation=0, sync_handler=?, "
                "revision=revision+1, "
                "heartbeat_at=?, updated_at=? WHERE job_id=? AND revision=?",
                (int(bool(sync_handler)), at, at, str(job_id), int(row["revision"])),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job start CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(
                conn, row, "job.started", actor=WorkActor("worker", str(lease_owner))
            )
            return self._return_job_tx(conn, job_id)

    def heartbeat_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        lease_ttl_s: float = 60.0,
        progress: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> JobRecord:
        at = float(now if now is not None else time.time())
        expires = at + max(2.0, float(lease_ttl_s))
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_lease(
                row, lease_owner=lease_owner, lease_epoch=lease_epoch, now=at
            )
            progress_json = (
                _json(dict(progress)) if progress is not None else str(row["progress_json"])
            )
            changed = conn.execute(
                "UPDATE work_job SET lease_expires_at=?, heartbeat_at=?, updated_at=?, "
                "progress_json=? WHERE job_id=? AND revision=?",
                (expires, at, at, progress_json, str(job_id), int(row["revision"])),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job heartbeat CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            return self._return_job_tx(conn, job_id)

    def update_job_progress(
        self,
        job_id: str,
        progress: Mapping[str, Any],
        *,
        lease_owner: str = "",
        lease_epoch: int = 0,
        expected_revision: int | None = None,
        merge: bool = True,
        actor: WorkActor | None = None,
        now: float | None = None,
    ) -> JobRecord:
        at = float(now if now is not None else time.time())
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_revision(row, expected_revision)
            if str(row["status"]) in JOB_LEASED_STATES:
                self._expect_lease(
                    row, lease_owner=lease_owner, lease_epoch=lease_epoch, now=at
                )
            elif str(row["status"]) not in {"waiting", "paused"}:
                raise InvalidTransition(
                    f"cannot update progress for {job_id} in {row['status']}"
                )
            old = _load_json(
                row["progress_json"], expected=dict, field="work_job.progress_json"
            )
            new_progress = {**old, **dict(progress)} if merge else dict(progress)
            changed = conn.execute(
                "UPDATE work_job SET progress_json=?, revision=revision+1, updated_at=? "
                "WHERE job_id=? AND revision=?",
                (_json(new_progress), at, str(job_id), int(row["revision"])),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job progress CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(
                conn, row, "job.progressed",
                actor=actor or WorkActor("worker", str(lease_owner) or "variant1"),
            )
            return self._return_job_tx(conn, job_id)

    def mark_job_waiting(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        progress: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> JobRecord:
        at = float(now if now is not None else time.time())
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_lease(
                row, lease_owner=lease_owner, lease_epoch=lease_epoch,
                now=at, allowed_states=("running",),
            )
            progress_json = (
                _json(dict(progress)) if progress is not None else str(row["progress_json"])
            )
            changed = conn.execute(
                "UPDATE work_job SET status='waiting', progress_json=?, "
                "lease_owner='', lease_expires_at=NULL, continuation=1, "
                "revision=revision+1, "
                "updated_at=? WHERE job_id=? AND revision=?",
                (progress_json, at, str(job_id), int(row["revision"])),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job wait CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(
                conn, row, "job.waiting", actor=WorkActor("worker", str(lease_owner))
            )
            return self._return_job_tx(conn, job_id)

    def wake_job(
        self,
        job_id: str,
        *,
        expected_revision: int | None = None,
        available_at: float | None = None,
        actor: WorkActor | None = None,
    ) -> JobRecord:
        at = time.time()
        ready_at = float(available_at if available_at is not None else at)
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_revision(row, expected_revision)
            if str(row["status"]) not in {"waiting", "paused"}:
                raise InvalidTransition(f"cannot wake {job_id} from {row['status']}")
            changed = conn.execute(
                "UPDATE work_job SET status='queued', available_at=?, "
                "cancel_requested=0, cancel_reason='', revision=revision+1, "
                "updated_at=? WHERE job_id=? AND revision=?",
                (ready_at, at, str(job_id), int(row["revision"])),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job wake CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(conn, row, "job.woken", actor=actor)
            return self._return_job_tx(conn, job_id)

    def pause_job(
        self,
        job_id: str,
        *,
        reason: str = "",
        expected_revision: int | None = None,
        actor: WorkActor | None = None,
    ) -> JobRecord:
        at = time.time()
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_revision(row, expected_revision)
            if str(row["status"]) not in {"queued", "waiting"}:
                raise InvalidTransition(
                    f"only queued or waiting jobs can pause immediately ({row['status']})"
                )
            progress = _load_json(
                row["progress_json"], expected=dict, field="work_job.progress_json"
            )
            if reason:
                progress["pause_reason"] = _text(reason, "pause reason", limit=4000)
            changed = conn.execute(
                "UPDATE work_job SET status='paused', progress_json=?, "
                "revision=revision+1, updated_at=? WHERE job_id=? AND revision=?",
                (_json(progress), at, str(job_id), int(row["revision"])),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job pause CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(conn, row, "job.paused", actor=actor)
            return self._return_job_tx(conn, job_id)

    def request_job_cancel(
        self,
        job_id: str,
        *,
        reason: str = "",
        expected_revision: int | None = None,
        actor: WorkActor | None = None,
    ) -> JobRecord:
        at = time.time()
        clean_reason = _text(reason, "cancel reason", limit=4000)
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_revision(row, expected_revision)
            if str(row["status"]) in JOB_TERMINAL_STATES:
                return self._return_job_tx(conn, job_id)
            if bool(row["cancel_requested"]):
                return self._return_job_tx(conn, job_id)
            immediate = str(row["status"]) in {"queued", "waiting", "paused", "leased"}
            status = "cancelled" if immediate else str(row["status"])
            completed_at = at if immediate else None
            changed = conn.execute(
                "UPDATE work_job SET status=?, cancel_requested=1, cancel_reason=?, "
                "lease_owner=CASE WHEN ? THEN '' ELSE lease_owner END, "
                "lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END, "
                "completed_at=?, revision=revision+1, updated_at=? "
                "WHERE job_id=? AND revision=?",
                (
                    status, clean_reason, int(immediate), int(immediate), completed_at,
                    at, str(job_id), int(row["revision"]),
                ),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job cancellation CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(
                conn, row,
                "job.cancelled" if immediate else "job.cancel_requested",
                actor=actor,
                payload={"reason": clean_reason},
            )
            return self._return_job_tx(conn, job_id)

    def acknowledge_job_cancel(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        now: float | None = None,
    ) -> JobRecord:
        at = float(now if now is not None else time.time())
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            if str(row["status"]) == "cancelled":
                return self._return_job_tx(conn, job_id)
            self._expect_lease(
                row, lease_owner=lease_owner, lease_epoch=lease_epoch, now=at
            )
            if not bool(row["cancel_requested"]):
                raise InvalidTransition(f"job {job_id} has no cancellation request")
            conn.execute(
                "UPDATE work_job SET status='cancelled', lease_owner='', "
                "lease_expires_at=NULL, completed_at=?, revision=revision+1, "
                "updated_at=? WHERE job_id=? AND revision=?",
                (at, at, str(job_id), int(row["revision"])),
            )
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(
                conn, row, "job.cancelled", actor=WorkActor("worker", str(lease_owner))
            )
            return self._return_job_tx(conn, job_id)

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        lease_owner: str,
        lease_epoch: int,
        result_ref: str = "",
        diagnostics_ref: str = "",
        progress: Mapping[str, Any] | None = None,
        error: str = "",
        now: float | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> JobRecord:
        target = str(status)
        if target not in {"succeeded", "failed", "cancelled", "unknown_effect"}:
            raise ValueError(f"invalid terminal job state: {target}")
        at = float(now if now is not None else time.time())
        from contextlib import nullcontext
        with nullcontext(_connection) if _connection is not None else self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_lease(
                row, lease_owner=lease_owner, lease_epoch=lease_epoch, now=at
            )
            if bool(row["cancel_requested"]) and target == "succeeded":
                raise InvalidTransition(
                    f"job {job_id} cannot succeed after cancellation was requested"
                )
            progress_json = (
                _json(dict(progress)) if progress is not None else str(row["progress_json"])
            )
            changed = conn.execute(
                """
                UPDATE work_job SET
                    status=?, result_ref=?, diagnostics_ref=?, progress_json=?,
                    error=?, lease_owner='', lease_expires_at=NULL,
                    completed_at=?, revision=revision+1, updated_at=?
                WHERE job_id=? AND revision=?
                """,
                (
                    target,
                    _text(result_ref, "result_ref", limit=2000),
                    _text(diagnostics_ref, "diagnostics_ref", limit=2000),
                    progress_json,
                    _text(error, "job error", limit=16000),
                    at, at, str(job_id), int(row["revision"]),
                ),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job finish CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(
                conn,
                row,
                f"job.{target}",
                actor=WorkActor("worker", str(lease_owner)),
                payload={
                    "result_ref": str(row["result_ref"] or ""),
                    "diagnostics_ref": str(row["diagnostics_ref"] or ""),
                    "error": str(row["error"] or ""),
                },
            )
            return self._return_job_tx(conn, job_id)

    def release_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        error: str = "",
        retry_delay_s: float = 0.0,
        unknown_effect: bool = False,
        now: float | None = None,
    ) -> JobRecord:
        """Release a failed handler, retrying only when its effect is known."""

        at = float(now if now is not None else time.time())
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_lease(
                row, lease_owner=lease_owner, lease_epoch=lease_epoch, now=at
            )
            if bool(row["cancel_requested"]):
                target = "cancelled"
            elif unknown_effect:
                target = "unknown_effect"
            elif int(row["attempt"] or 0) < int(row["max_attempts"] or 1):
                target = "queued"
            else:
                target = "failed"
            terminal = target in JOB_TERMINAL_STATES
            available = at + max(0.0, float(retry_delay_s))
            changed = conn.execute(
                "UPDATE work_job SET status=?, available_at=?, error=?, "
                "lease_owner='', lease_expires_at=NULL, completed_at=?, "
                "revision=revision+1, updated_at=? WHERE job_id=? AND revision=?",
                (
                    target, available, _text(error, "job error", limit=16000),
                    at if terminal else None, at, str(job_id), int(row["revision"]),
                ),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job release CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            event_type = {
                "queued": "job.retry_scheduled",
                "failed": "job.failed",
                "cancelled": "job.cancelled",
                "unknown_effect": "job.unknown_effect",
            }[target]
            self._job_event_tx(
                conn, row, event_type, actor=WorkActor("worker", str(lease_owner)),
                payload={"error": str(row["error"] or ""), "available_at": available},
            )
            return self._return_job_tx(conn, job_id)

    def retry_job(
        self,
        job_id: str,
        *,
        expected_revision: int | None = None,
        available_at: float | None = None,
        reset_attempts: bool = False,
        actor: WorkActor | None = None,
    ) -> JobRecord:
        at = time.time()
        ready_at = float(available_at if available_at is not None else at)
        with self._write() as conn:
            row = self._job_row_tx(conn, job_id)
            self._expect_revision(row, expected_revision)
            if str(row["status"]) not in {"failed", "unknown_effect"}:
                raise InvalidTransition(f"cannot retry {job_id} from {row['status']}")
            if not reset_attempts and int(row["attempt"]) >= int(row["max_attempts"]):
                raise InvalidTransition(
                    f"job {job_id} exhausted {int(row['max_attempts'])} attempts"
                )
            attempt = 0 if reset_attempts else int(row["attempt"])
            changed = conn.execute(
                "UPDATE work_job SET status='queued', attempt=?, available_at=?, "
                "result_ref='', diagnostics_ref='', error='', cancel_requested=0, "
                "cancel_reason='', continuation=0, completed_at=NULL, "
                "revision=revision+1, "
                "updated_at=? WHERE job_id=? AND revision=?",
                (attempt, ready_at, at, str(job_id), int(row["revision"])),
            )
            if changed.rowcount != 1:
                raise WorkConflict(f"job retry CAS lost for {job_id}")
            row = self._job_row_tx(conn, job_id)
            self._job_event_tx(
                conn, row, "job.retry_scheduled", actor=actor,
                payload={"available_at": ready_at, "attempts_reset": bool(reset_attempts)},
            )
            return self._return_job_tx(conn, job_id)

    def recover_expired_job_leases(
        self,
        *,
        now: float | None = None,
    ) -> list[JobRecord]:
        """Fence expired workers and conservatively reconcile their jobs."""

        at = float(now if now is not None else time.time())
        recovered: list[JobRecord] = []
        with self._write() as conn:
            rows = conn.execute(
                "SELECT * FROM work_job WHERE status IN ('leased','running') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=? "
                "ORDER BY lease_expires_at, job_id",
                (at,),
            ).fetchall()
            for row in rows:
                old_status = str(row["status"])
                policy = _load_json(
                    row["retry_policy_json"], expected=dict,
                    field="work_job.retry_policy_json",
                )
                if bool(row["cancel_requested"]):
                    target = "cancelled"
                elif old_status == "leased":
                    target = (
                        "queued"
                        if int(row["attempt"]) < int(row["max_attempts"])
                        else "failed"
                    )
                elif bool(row["sync_handler"]):
                    # A cancelled or lease-lost to_thread handler can keep
                    # applying effects in its OS thread. Another backend must
                    # not automatically lease a duplicate while it may live.
                    target = "unknown_effect"
                else:
                    expiry_policy = str(
                        policy.get("on_lease_expiry") or "unknown_effect"
                    )
                    if expiry_policy == "retry" and int(row["attempt"]) < int(row["max_attempts"]):
                        target = "queued"
                    elif expiry_policy == "fail":
                        target = "failed"
                    else:
                        target = "unknown_effect"
                terminal = target in JOB_TERMINAL_STATES
                base_delay = max(0.0, float(policy.get("base_delay_s") or 0.0))
                available = at + base_delay
                reason = (
                    "job lease expired before handler start"
                    if old_status == "leased"
                    else "job lease expired while handler was running"
                )
                changed = conn.execute(
                    "UPDATE work_job SET status=?, available_at=?, lease_owner='', "
                    "lease_expires_at=NULL, continuation=?, error=?, completed_at=?, "
                    "revision=revision+1, updated_at=? WHERE job_id=? AND revision=?",
                    (
                        target, available,
                        int(old_status == "leased" and target == "queued"),
                        reason, at if terminal else None,
                        at, str(row["job_id"]), int(row["revision"]),
                    ),
                )
                if changed.rowcount != 1:
                    raise WorkConflict(f"expired job recovery lost for {row['job_id']}")
                updated = self._job_row_tx(conn, str(row["job_id"]))
                event_type = {
                    "queued": "job.lease_recovered",
                    "failed": "job.failed",
                    "cancelled": "job.cancelled",
                    "unknown_effect": "job.unknown_effect",
                }[target]
                self._job_event_tx(
                    conn, updated, event_type,
                    payload={"previous_status": old_status, "reason": reason},
                )
                job = self._job_from_row(self._job_row_tx(conn, str(row["job_id"])))
                if job is not None:
                    recovered.append(job)
        return recovered

    @staticmethod
    def _operation_from_row(row: sqlite3.Row | None) -> OperationRecord | None:
        if row is None:
            return None
        return OperationRecord(
            operation_id=str(row["operation_id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            scope=WorkScope.from_mapping(_load_json(
                row["scope_json"], expected=dict, field="work_operation.scope_json"
            )),
            idempotency_key=str(row["idempotency_key"] or ""),
            request_fingerprint=str(row["request_fingerprint"] or ""),
            request=_load_json(
                row["request_json"], expected=dict, field="work_operation.request_json"
            ),
            response=_load_json(
                row["response_json"], expected=dict, field="work_operation.response_json"
            ),
            effect_ref=str(row["effect_ref"] or ""),
            revision=int(row["revision"] or 0),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            completed_at=float(row["completed_at"] or 0),
            error=str(row["error"] or ""),
        )

    def record_operation_receipt(
        self,
        *,
        operation_id: str,
        kind: str,
        status: str,
        scope: WorkScope | Mapping[str, Any] | None,
        request: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
        effect_ref: str = "",
        error: str = "",
        idempotency_key: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
    ) -> OperationRecord:
        identifier = _text(operation_id, "operation_id", required=True, limit=512)
        clean_kind = _text(kind, "operation kind", required=True, limit=240)
        clean_status = str(status)
        if clean_status not in _TERMINAL_OPERATION_STATES:
            raise ValueError(f"receipt operation must be terminal, got {clean_status!r}")
        resolved = coerce_work_scope(scope)
        idem = _text(idempotency_key, "idempotency_key", limit=512)
        clean_request = dict(request or {})
        fingerprint = canonical_request_fingerprint(clean_kind, {
            **{
                key: value for key, value in clean_request.items()
                if key != "attribution"
            },
            "scope": resolved.to_dict(),
        })
        now = time.time()
        with self._write() as conn:
            prior = conn.execute(
                "SELECT * FROM work_operation WHERE operation_id=?", (identifier,)
            ).fetchone()
            if prior is not None:
                existing = self._operation_from_row(prior)
                if existing is None:
                    raise RepositoryCorrupt("operation receipt row disappeared")
                if existing.kind != clean_kind:
                    raise WorkConflict("operation receipt id was reused for another kind")
                if existing.request_fingerprint != fingerprint:
                    raise WorkConflict(
                        "operation receipt id was reused for a different request"
                    )
                return existing
            if idem:
                prior = conn.execute(
                    "SELECT * FROM work_operation WHERE kind=? AND idempotency_key=?",
                    (clean_kind, idem),
                ).fetchone()
                if prior is not None:
                    existing = self._operation_from_row(prior)
                    if existing is None:
                        raise RepositoryCorrupt("idempotent operation row disappeared")
                    if existing.request_fingerprint != fingerprint:
                        raise WorkConflict(
                            "operation idempotency key was reused with a different request"
                        )
                    return existing
            conn.execute(
                """
                INSERT INTO work_operation(
                    operation_id, kind, status, scope_json, idempotency_key,
                    request_fingerprint, request_json, response_json, effect_ref, revision,
                    created_at, updated_at, completed_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    identifier, clean_kind, clean_status, _json(resolved.to_dict()),
                    idem, fingerprint, _json(clean_request), _json(dict(response or {})),
                    _text(effect_ref, "effect_ref", limit=2000), now, now, now,
                    _text(error, "operation error", limit=16000),
                ),
            )
            self._insert_event_tx(
                conn,
                event_type="capability.receipt.recorded",
                aggregate_kind="operation",
                aggregate_id=identifier,
                aggregate_version=1,
                expected_aggregate_version=0,
                scope=resolved,
                actor=actor,
                correlation_id=correlation_id,
                idempotency_key=identifier,
                payload={
                    "operation_id": identifier,
                    "kind": clean_kind,
                    "status": clean_status,
                    "effect_ref": effect_ref,
                    "error": error,
                },
            )
            operation = self._operation_from_row(conn.execute(
                "SELECT * FROM work_operation WHERE operation_id=?", (identifier,)
            ).fetchone())
            if operation is None:
                raise RepositoryCorrupt("new operation receipt disappeared")
            return operation

    def get_operation(self, operation_id: str) -> OperationRecord | None:
        with self._read() as conn:
            return self._operation_from_row(conn.execute(
                "SELECT * FROM work_operation WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone())

    def operations_for_outer_call(self, chat_id: str, run_id: str, call_id: str) -> list[OperationRecord]:
        """Read all receipts for one exact call; never truncate evidence to a UI page."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM work_operation WHERE json_extract(request_json,'$.attribution.run_id')=? "
                "AND json_extract(request_json,'$.attribution.outer_tool_call_id')=? ORDER BY created_at,operation_id",
                (run_id, call_id),
            ).fetchall()
        records = [self._operation_from_row(row) for row in rows]
        for record in records:
            if (record.scope.chat_id != chat_id
                    or record.request.get("attribution", {}).get("chat_id") != chat_id):
                raise ValueError("outer-call Work receipt ownership mismatch")
        return records

    def chat_operation_activity(self,chat_id: str,*,limit: int=50):
        """Bounded child inspection metadata, without arguments or result bodies."""
        with self._read() as conn:
            conn.execute('BEGIN')
            revision=conn.execute('SELECT revision FROM work_operation_clock WHERE id=1').fetchone()[0]
            cap=max(1,min(100,int(limit)))
            rows=conn.execute('''SELECT operation_id,kind,status,revision,created_at,updated_at,
                completed_at,scope_json,json_extract(request_json,'$.attribution.run_id') AS run_id,
                json_extract(request_json,'$.attribution.cell_execution_id') AS cell_execution_id
                FROM work_operation WHERE json_extract(scope_json,'$.chat_id')=?
                ORDER BY updated_at DESC,operation_id DESC LIMIT ?''',(str(chat_id),cap+1)).fetchall()
        items=[]
        for row in rows[:cap]:
            item=dict(row);item['scope']={**json.loads(item.pop('scope_json')),'run_id':item['run_id']};items.append(item)
        return {'items':items,'revision':int(revision),'truncated':len(rows)>cap}

    def list_operations(
        self,
        *,
        statuses: Iterable[str] = (),
        updated_before: float | None = None,
        limit: int = 200,
    ) -> list[OperationRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        clean_statuses = tuple(dict.fromkeys(str(item) for item in statuses))
        if clean_statuses:
            clauses.append("status IN (" + ",".join("?" for _ in clean_statuses) + ")")
            params.extend(clean_statuses)
        if updated_before is not None:
            clauses.append("updated_at<?")
            params.append(float(updated_before))
        sql = "SELECT * FROM work_operation"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at, operation_id LIMIT ?"
        params.append(min(5000, max(1, int(limit))))
        with self._read() as conn:
            return [
                operation for operation in (
                    self._operation_from_row(row)
                    for row in conn.execute(sql, tuple(params)).fetchall()
                ) if operation is not None
            ]

    def recover_stale_operations(
        self,
        *,
        updated_before: float,
    ) -> list[OperationRecord]:
        """Move abandoned planned/running operations to honest unknown state."""

        recovered: list[OperationRecord] = []
        now = time.time()
        with self._write() as conn:
            rows = conn.execute(
                "SELECT * FROM work_operation WHERE status IN ('planned','running') "
                "AND updated_at<? ORDER BY updated_at, operation_id",
                (float(updated_before),),
            ).fetchall()
            for row in rows:
                changed = conn.execute(
                    "UPDATE work_operation SET status='unknown_effect', "
                    "revision=revision+1, error='operation interrupted before a terminal receipt', "
                    "updated_at=?, completed_at=? WHERE operation_id=? AND revision=?",
                    (now, now, str(row["operation_id"]), int(row["revision"])),
                )
                if changed.rowcount != 1:
                    raise WorkConflict(
                        f"operation recovery CAS lost for {row['operation_id']}"
                    )
                updated = conn.execute(
                    "SELECT * FROM work_operation WHERE operation_id=?",
                    (str(row["operation_id"]),),
                ).fetchone()
                version = int(updated["revision"])
                scope = WorkScope.from_mapping(_load_json(
                    updated["scope_json"], expected=dict,
                    field="work_operation.scope_json",
                ))
                self._insert_event_tx(
                    conn,
                    event_type="operation.unknown_effect",
                    aggregate_kind="operation",
                    aggregate_id=str(updated["operation_id"]),
                    aggregate_version=version,
                    expected_aggregate_version=version - 1,
                    scope=scope,
                    payload={"reason": str(updated["error"])},
                )
                operation = self._operation_from_row(updated)
                if operation is not None:
                    recovered.append(operation)
        return recovered

    def projection(self, name: str) -> ProjectionSnapshot:
        clean_name = _text(name, "projection name", required=True, limit=240)
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM work_projection_state WHERE projection_name=?",
                (clean_name,),
            ).fetchone()
        if row is None:
            return ProjectionSnapshot(clean_name, 0, {}, 0.0)
        return ProjectionSnapshot(
            name=clean_name,
            last_sequence=int(row["last_sequence"] or 0),
            state=_load_json(
                row["state_json"], expected=dict,
                field="work_projection_state.state_json",
            ),
            updated_at=float(row["updated_at"] or 0),
        )

    def reduce_projection(
        self,
        name: str,
        reducer: Callable[[dict[str, Any], WorkEvent], Mapping[str, Any]],
        *,
        initial: Mapping[str, Any] | None = None,
        batch_size: int = 500,
    ) -> ProjectionSnapshot:
        """Atomically reduce an ordered event batch and advance its cursor."""

        if not callable(reducer):
            raise TypeError("projection reducer must be callable")
        clean_name = _text(name, "projection name", required=True, limit=240)
        take = min(5000, max(1, int(batch_size)))
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM work_projection_state WHERE projection_name=?",
                (clean_name,),
            ).fetchone()
            if row is None:
                cursor = 0
                state = dict(initial or {})
            else:
                cursor = int(row["last_sequence"] or 0)
                state = _load_json(
                    row["state_json"], expected=dict,
                    field="work_projection_state.state_json",
                )
            event_rows = conn.execute(
                "SELECT * FROM work_event WHERE sequence>? ORDER BY sequence LIMIT ?",
                (cursor, take),
            ).fetchall()
            for event_row in event_rows:
                event = self._event_from_row(event_row)
                if event is None:
                    raise RepositoryCorrupt("projection encountered an empty event")
                reduced = reducer(dict(state), event)
                if not isinstance(reduced, Mapping):
                    raise TypeError("projection reducer must return a mapping")
                state = dict(reduced)
                cursor = event.sequence
            now = time.time()
            conn.execute(
                """
                INSERT INTO work_projection_state(
                    projection_name, last_sequence, state_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(projection_name) DO UPDATE SET
                    last_sequence=excluded.last_sequence,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (clean_name, cursor, _json(state), now),
            )
            return ProjectionSnapshot(clean_name, cursor, state, now)


__all__ = ["SCHEMA_VERSION", "WorkRepository", "default_work_path"]
