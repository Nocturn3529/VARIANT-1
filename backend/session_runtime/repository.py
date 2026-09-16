"""Transactional SQLite repository for chat runtime identity and input."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Iterable, Iterator

from core_invariants import (
    canonical_json,
    sqlite_session_connection,
    sqlite_transaction,
    sqlite_writer_lock,
)
from .models import (
    ChatRuntimeRecord,
    InputTicket,
    RuntimeIdentity,
    TICKET_STATES,
    TICKET_TERMINAL_STATES,
)


SCHEMA_VERSION = 5
_BUDGET_KEYS = ("provider_calls", "tokens", "cost_usd", "wall_time_s")
_MUTATION_AUTHORITY_COLUMNS = frozenset({
    "mutation_write_enabled",
    "mutation_authority_revision",
    "mutation_authority_updated_at",
    "mutation_authority_actor",
})


def _json_dict(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json(value: Any) -> str:
    return canonical_json(value)


def chat_id_value(value: str) -> str:
    chat_id = str(value or "").strip()
    if not chat_id or len(chat_id) > 256:
        raise ValueError("chat_id must contain 1-256 characters")
    if any(ord(char) < 32 for char in chat_id):
        raise ValueError("chat_id contains control characters")
    return chat_id


class SessionRuntimeRepository:
    """One authoritative repository; connections are short and thread-safe."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._lock = sqlite_writer_lock(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_session_connection(self.path)

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                with sqlite_transaction(
                    conn,
                    immediate=immediate,
                    fault_name=("session_runtime.before_commit" if immediate else ""),
                ):
                    yield conn
            finally:
                conn.close()

    @contextmanager
    def _write_connection(
        self, connection: sqlite3.Connection | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Join an owning ASTB transaction without committing its partial work."""
        if connection is None:
            with self._transaction(immediate=True) as conn:
                yield conn
            return
        databases = connection.execute("PRAGMA database_list").fetchall()
        main_path = next((str(row[2]) for row in databases if row[1] == "main"), "")
        if (not connection.in_transaction or not main_path
                or os.path.normcase(os.path.realpath(main_path))
                != os.path.normcase(os.path.realpath(self.path))):
            raise RuntimeError("runtime writes require an active transaction in the shared ASTB database")
        yield connection

    def _initialize(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migration (
                        version INTEGER PRIMARY KEY,
                        applied_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS astb_chat_runtime (
                        chat_id TEXT PRIMARY KEY,
                        lifecycle_state TEXT NOT NULL,
                        action_surface TEXT NOT NULL,
                        provider_tool_schema_revision TEXT NOT NULL,
                        graph_revision TEXT NOT NULL,
                        catalog_release_id TEXT NOT NULL,
                        environment_digest TEXT NOT NULL,
                        trust_profile TEXT NOT NULL,
                        kernel_generation INTEGER NOT NULL DEFAULT 0,
                        disclosure_profile_id TEXT NOT NULL,
                        disclosure_profile_revision TEXT NOT NULL,
                        discovery_state_ref TEXT NOT NULL DEFAULT '',
                        mount_revision INTEGER,
                        selected_category_id TEXT NOT NULL DEFAULT '',
                        overlay_revision INTEGER NOT NULL DEFAULT 0,
                        continuation_state TEXT NOT NULL DEFAULT 'ready',
                        mutation_write_enabled INTEGER NOT NULL DEFAULT 0,
                        mutation_authority_revision INTEGER NOT NULL DEFAULT 0,
                        mutation_authority_updated_at REAL NOT NULL DEFAULT 0,
                        mutation_authority_actor TEXT NOT NULL DEFAULT '',
                        budget_limits_json TEXT NOT NULL DEFAULT '{}',
                        budget_used_json TEXT NOT NULL DEFAULT '{}',
                        creation_saga_state TEXT NOT NULL DEFAULT 'complete',
                        deletion_saga_state TEXT NOT NULL DEFAULT '',
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        tombstoned_at REAL
                    );

                    CREATE TABLE IF NOT EXISTS astb_input_ticket (
                        ticket_id TEXT PRIMARY KEY,
                        chat_id TEXT NOT NULL,
                        delivery TEXT NOT NULL,
                        text TEXT NOT NULL,
                        state TEXT NOT NULL,
                        client_id TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        attachment_id TEXT NOT NULL DEFAULT '',
                        run_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        selected_at REAL,
                        transcript_committed_at REAL,
                        completed_at REAL,
                        proof_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT NOT NULL DEFAULT '',
                        FOREIGN KEY(chat_id) REFERENCES astb_chat_runtime(chat_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_astb_input_queue
                        ON astb_input_ticket(chat_id, state, delivery, created_at, ticket_id);

                    CREATE TABLE IF NOT EXISTS astb_input_queue_revision (
                        chat_id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TRIGGER IF NOT EXISTS input_queue_insert_revision
                    AFTER INSERT ON astb_input_ticket BEGIN
                        INSERT INTO astb_input_queue_revision(chat_id,revision) VALUES(NEW.chat_id,1)
                        ON CONFLICT(chat_id) DO UPDATE SET revision=revision+1;
                    END;
                    CREATE TRIGGER IF NOT EXISTS input_queue_update_revision
                    AFTER UPDATE ON astb_input_ticket BEGIN
                        INSERT INTO astb_input_queue_revision(chat_id,revision) VALUES(NEW.chat_id,1)
                        ON CONFLICT(chat_id) DO UPDATE SET revision=revision+1;
                    END;
                    CREATE TRIGGER IF NOT EXISTS input_queue_delete_revision
                    AFTER DELETE ON astb_input_ticket BEGIN
                        INSERT INTO astb_input_queue_revision(chat_id,revision) VALUES(OLD.chat_id,1)
                        ON CONFLICT(chat_id) DO UPDATE SET revision=revision+1;
                    END;

                    CREATE TABLE IF NOT EXISTS astb_outer_tool_call (
                        chat_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        call_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL,
                        outcome_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(chat_id, run_id, call_id),
                        FOREIGN KEY(chat_id) REFERENCES astb_chat_runtime(chat_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_astb_outer_tool_state
                        ON astb_outer_tool_call(chat_id, state, updated_at);

                    CREATE TABLE IF NOT EXISTS chat_thread_ref (
                        chat_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        deleted_at REAL,
                        PRIMARY KEY(chat_id, thread_id),
                        FOREIGN KEY(chat_id) REFERENCES astb_chat_runtime(chat_id)
                    );

                    CREATE TABLE IF NOT EXISTS astb_budget_event (
                        event_id TEXT PRIMARY KEY,
                        chat_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        usage_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(chat_id) REFERENCES astb_chat_runtime(chat_id)
                    );

                    CREATE TABLE IF NOT EXISTS astb_budget_charge (
                        chat_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        usage_json TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(chat_id, run_id),
                        FOREIGN KEY(chat_id) REFERENCES astb_chat_runtime(chat_id)
                    );

                    INSERT OR IGNORE INTO astb_budget_charge(
                        chat_id,run_id,usage_json,event_id,created_at
                    )
                    SELECT chat_id,run_id,usage_json,event_id,created_at
                    FROM astb_budget_event
                    WHERE run_id<>''
                    ORDER BY created_at,event_id;

                    CREATE TABLE IF NOT EXISTS astb_runtime_migration (
                        migration_id TEXT PRIMARY KEY,
                        chat_id TEXT NOT NULL,
                        from_identity_json TEXT NOT NULL,
                        to_identity_json TEXT NOT NULL,
                        compatibility_json TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(chat_id) REFERENCES astb_chat_runtime(chat_id)
                    );
                    CREATE INDEX IF NOT EXISTS astb_runtime_migration_chat_idx
                        ON astb_runtime_migration(chat_id, created_at DESC);
                    """
                )
                runtime_columns = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(astb_chat_runtime)"
                    ).fetchall()
                }
                missing_authority_columns = (
                    _MUTATION_AUTHORITY_COLUMNS - runtime_columns
                )
                additions = {
                    "mutation_write_enabled": (
                        "INTEGER NOT NULL DEFAULT 0"
                    ),
                    "mutation_authority_revision": (
                        "INTEGER NOT NULL DEFAULT 0"
                    ),
                    "mutation_authority_updated_at": (
                        "REAL NOT NULL DEFAULT 0"
                    ),
                    "mutation_authority_actor": (
                        "TEXT NOT NULL DEFAULT ''"
                    ),
                }
                for column in sorted(missing_authority_columns):
                    conn.execute(
                        f"ALTER TABLE astb_chat_runtime ADD COLUMN {column} "  # noqa: S608
                        + additions[column]
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, time.time()),
                )
            finally:
                conn.close()

    @staticmethod
    def _runtime_from_row(row: sqlite3.Row | None) -> ChatRuntimeRecord | None:
        if row is None:
            return None
        identity = RuntimeIdentity(
            action_surface=row["action_surface"],
            provider_tool_schema_revision=row["provider_tool_schema_revision"],
            graph_revision=row["graph_revision"],
            catalog_release_id=row["catalog_release_id"],
            environment_digest=row["environment_digest"],
            trust_profile=row["trust_profile"],
            disclosure_profile_id=row["disclosure_profile_id"],
            disclosure_profile_revision=row["disclosure_profile_revision"],
            discovery_state_ref=row["discovery_state_ref"],
            mount_revision=row["mount_revision"],
            selected_category_id=row["selected_category_id"],
            overlay_revision=int(row["overlay_revision"] or 0),
        )
        return ChatRuntimeRecord(
            chat_id=row["chat_id"],
            lifecycle_state=row["lifecycle_state"],
            identity=identity,
            kernel_generation=int(row["kernel_generation"] or 0),
            continuation_state=row["continuation_state"],
            mutation_write_enabled=bool(row["mutation_write_enabled"]),
            mutation_authority_revision=int(
                row["mutation_authority_revision"] or 0
            ),
            mutation_authority_updated_at=float(
                row["mutation_authority_updated_at"] or 0
            ),
            mutation_authority_actor=row["mutation_authority_actor"],
            budget_limits=_json_dict(row["budget_limits_json"]),
            budget_used=_json_dict(row["budget_used_json"]),
            creation_saga_state=row["creation_saga_state"],
            deletion_saga_state=row["deletion_saga_state"],
            version=int(row["version"] or 0),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            tombstoned_at=float(row["tombstoned_at"] or 0),
        )

    @staticmethod
    def _ticket_from_row(row: sqlite3.Row | None) -> InputTicket | None:
        if row is None:
            return None
        return InputTicket(
            ticket_id=row["ticket_id"],
            chat_id=row["chat_id"],
            delivery=row["delivery"],
            text=row["text"],
            state=row["state"],
            client_id=row["client_id"],
            source=row["source"],
            attachment_id=row["attachment_id"],
            run_id=row["run_id"],
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            selected_at=float(row["selected_at"] or 0),
            transcript_committed_at=float(row["transcript_committed_at"] or 0),
            completed_at=float(row["completed_at"] or 0),
            proof=_json_dict(row["proof_json"]),
            error=row["error"],
        )

    def ensure_runtime(
        self,
        chat_id: str,
        identity: RuntimeIdentity,
        *,
        creation_saga_state: str = "complete",
        connection: sqlite3.Connection | None = None,
    ) -> ChatRuntimeRecord:
        now = time.time()
        with self._write_connection(connection) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO astb_chat_runtime(
                    chat_id, lifecycle_state, action_surface,
                    provider_tool_schema_revision, graph_revision,
                    catalog_release_id, environment_digest, trust_profile,
                    disclosure_profile_id, disclosure_profile_revision,
                    discovery_state_ref, mount_revision, selected_category_id,
                    overlay_revision, creation_saga_state, created_at, updated_at
                ) VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    identity.action_surface,
                    identity.provider_tool_schema_revision,
                    identity.graph_revision,
                    identity.catalog_release_id,
                    identity.environment_digest,
                    identity.trust_profile,
                    identity.disclosure_profile_id,
                    identity.disclosure_profile_revision,
                    identity.discovery_state_ref,
                    identity.mount_revision,
                    identity.selected_category_id,
                    int(identity.overlay_revision),
                    creation_saga_state,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return self._runtime_from_row(row)

    def get_runtime(
        self, chat_id: str, *, connection: sqlite3.Connection | None = None,
    ) -> ChatRuntimeRecord | None:
        if connection is not None:
            with self._write_connection(connection) as conn:
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
                ).fetchone()
            return self._runtime_from_row(row)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
                ).fetchone()
            finally:
                conn.close()
        return self._runtime_from_row(row)

    def mutation_authority_cas(
        self,
        chat_id: str,
        enabled: bool,
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> ChatRuntimeRecord:
        """CAS one chat's write authority without changing pinned identity."""

        if not isinstance(enabled, bool):
            raise TypeError("mutation write authority must be boolean")
        clean_actor = str(actor or "").strip()
        if not clean_actor:
            raise ValueError("mutation write authority requires an actor")
        if len(clean_actor) > 200:
            raise ValueError("mutation write authority actor exceeds 200 characters")
        if expected_revision is not None and int(expected_revision) < 0:
            raise ValueError("mutation authority revision cannot be negative")
        now = time.time()
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown chat runtime: {chat_id}")
            if row["lifecycle_state"] != "active":
                raise RuntimeError(f"chat runtime is not active: {chat_id}")
            marker = str(row["creation_saga_state"] or "")
            if enabled and marker.startswith(("child:", "worker:")):
                raise RuntimeError(
                    "mutation write authority is unavailable to child or worker runtimes"
                )
            current_revision = int(row["mutation_authority_revision"] or 0)
            if (
                expected_revision is not None
                and current_revision != int(expected_revision)
            ):
                raise RuntimeError(
                    "mutation authority CAS failed "
                    f"({current_revision} != {int(expected_revision)})"
                )
            if bool(row["mutation_write_enabled"]) == enabled:
                record = self._runtime_from_row(row)
                if record is None:  # pragma: no cover - guarded by the row check
                    raise LookupError(f"unknown chat runtime: {chat_id}")
                return record
            updated = conn.execute(
                "UPDATE astb_chat_runtime SET mutation_write_enabled=?, "
                "mutation_authority_revision=mutation_authority_revision+1, "
                "mutation_authority_updated_at=?, mutation_authority_actor=?, "
                "updated_at=?, version=version+1 WHERE chat_id=? "
                "AND lifecycle_state='active' AND mutation_authority_revision=?",
                (
                    int(enabled),
                    now,
                    clean_actor,
                    now,
                    chat_id,
                    current_revision,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    "mutation authority CAS lost a concurrent update"
                )
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
        record = self._runtime_from_row(row)
        if record is None:  # pragma: no cover - guarded by the update
            raise LookupError(f"unknown chat runtime: {chat_id}")
        return record

    def list_runtimes(self, *, include_deleted: bool = False) -> list[ChatRuntimeRecord]:
        where = "" if include_deleted else "WHERE lifecycle_state != 'deleted'"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT * FROM astb_chat_runtime {where} ORDER BY created_at"  # noqa: S608
                ).fetchall()
            finally:
                conn.close()
        return [self._runtime_from_row(row) for row in rows]

    def set_lifecycle(
        self,
        chat_id: str,
        state: str,
        *,
        deletion_saga_state: str = "",
        connection: sqlite3.Connection | None = None,
    ) -> ChatRuntimeRecord | None:
        if state not in {"creating", "active", "deleting", "deleted"}:
            raise ValueError(f"invalid runtime lifecycle state: {state}")
        now = time.time()
        tombstone = now if state in {"deleting", "deleted"} else None
        with self._write_connection(connection) as conn:
            conn.execute(
                """
                UPDATE astb_chat_runtime
                SET lifecycle_state=?, deletion_saga_state=?, updated_at=?,
                    tombstoned_at=COALESCE(tombstoned_at, ?), version=version+1
                WHERE chat_id=?
                """,
                (state, deletion_saga_state, now, tombstone, chat_id),
            )
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return self._runtime_from_row(row)

    def update_continuation(
        self,
        chat_id: str,
        state: str,
        *,
        limits: dict[str, float] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> ChatRuntimeRecord | None:
        now = time.time()
        with self._write_connection(connection) as conn:
            if limits is None:
                conn.execute(
                    "UPDATE astb_chat_runtime SET continuation_state=?, updated_at=?, "
                    "version=version+1 WHERE chat_id=?",
                    (state, now, chat_id),
                )
            else:
                clean = {
                    key: max(0.0, float(limits[key]))
                    for key in _BUDGET_KEYS if key in limits and limits[key] is not None
                }
                conn.execute(
                    "UPDATE astb_chat_runtime SET continuation_state=?, "
                    "budget_limits_json=?, budget_used_json='{}', updated_at=?, "
                    "version=version+1 WHERE chat_id=?",
                    (state, _json(clean), now, chat_id),
                )
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return self._runtime_from_row(row)

    def discard_unstarted_child_runtime(self, chat_id: str, *, parent_chat_id: str) -> bool:
        """Forget a fully drained, unpublished birth so its deterministic ID can retry.

        Used only after the child owner established that no handle exists. A used
        kernel or any remaining foreign-key history keeps its tombstone instead.
        """
        try:
            with self._transaction(immediate=True) as conn:
                changed = conn.execute(
                    "DELETE FROM astb_chat_runtime WHERE chat_id=? "
                    "AND creation_saga_state=? AND lifecycle_state='deleted' "
                    "AND deletion_saga_state='orphan_child_creation:complete' AND kernel_generation=0",
                    (chat_id, f"child:{parent_chat_id}"),
                )
                return changed.rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def advance_kernel_generation(self, chat_id: str) -> ChatRuntimeRecord:
        """Atomically fence every prior kernel before a replacement boots."""
        now = time.time()
        with self._transaction(immediate=True) as conn:
            current = conn.execute(
                "SELECT lifecycle_state FROM astb_chat_runtime WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if current is None or current["lifecycle_state"] != "active":
                raise RuntimeError(f"chat runtime is not active: {chat_id}")
            conn.execute(
                "UPDATE astb_chat_runtime SET kernel_generation=kernel_generation+1, "
                "updated_at=?, version=version+1 WHERE chat_id=?",
                (now, chat_id),
            )
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
        record = self._runtime_from_row(row)
        if record is None:
            raise LookupError(f"unknown chat runtime: {chat_id}")
        return record

    def assign_identity(
        self,
        chat_id: str,
        identity: RuntimeIdentity,
        *,
        expected_action_surface: str,
        expected_version: int | None = None,
        migration: dict[str, Any] | None = None,
    ) -> ChatRuntimeRecord:
        """CAS a developer/canary profile before that chat has a live run."""
        now = time.time()
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown chat runtime: {chat_id}")
            if row["lifecycle_state"] != "active":
                raise RuntimeError(f"chat runtime is not active: {chat_id}")
            if row["action_surface"] != str(expected_action_surface):
                raise RuntimeError(
                    "chat action-surface CAS failed "
                    f"({row['action_surface']!r} != {expected_action_surface!r})"
                )
            expected_runtime_version = int(
                row["version"] if expected_version is None else expected_version
            )
            if int(row["version"]) != expected_runtime_version:
                raise RuntimeError(
                    "chat runtime version CAS failed "
                    f"({row['version']} != {expected_runtime_version})"
                )
            conn.execute(
                """
                UPDATE astb_chat_runtime
                SET action_surface=?, provider_tool_schema_revision=?,
                    graph_revision=?, catalog_release_id=?, environment_digest=?,
                    trust_profile=?, disclosure_profile_id=?,
                    disclosure_profile_revision=?, discovery_state_ref=?,
                    mount_revision=?, selected_category_id=?, overlay_revision=?,
                    updated_at=?, version=version+1
                WHERE chat_id=? AND action_surface=? AND version=?
                """,
                (
                    identity.action_surface,
                    identity.provider_tool_schema_revision,
                    identity.graph_revision,
                    identity.catalog_release_id,
                    identity.environment_digest,
                    identity.trust_profile,
                    identity.disclosure_profile_id,
                    identity.disclosure_profile_revision,
                    identity.discovery_state_ref,
                    identity.mount_revision,
                    identity.selected_category_id,
                    int(identity.overlay_revision),
                    now,
                    chat_id,
                    expected_action_surface,
                    expected_runtime_version,
                ),
            )
            if int(conn.execute("SELECT changes()").fetchone()[0]) != 1:
                raise RuntimeError("chat runtime identity CAS lost a concurrent update")
            if migration is not None:
                migration_id = str(
                    migration.get("migration_id") or "migration_" + uuid.uuid4().hex
                )
                conn.execute(
                    "INSERT INTO astb_runtime_migration(migration_id, chat_id, "
                    "from_identity_json, to_identity_json, compatibility_json, actor, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        migration_id,
                        chat_id,
                        _json(RuntimeIdentity(
                            action_surface=row["action_surface"],
                            provider_tool_schema_revision=row["provider_tool_schema_revision"],
                            graph_revision=row["graph_revision"],
                            catalog_release_id=row["catalog_release_id"],
                            environment_digest=row["environment_digest"],
                            trust_profile=row["trust_profile"],
                            disclosure_profile_id=row["disclosure_profile_id"],
                            disclosure_profile_revision=row["disclosure_profile_revision"],
                            discovery_state_ref=row["discovery_state_ref"],
                            mount_revision=row["mount_revision"],
                            selected_category_id=row["selected_category_id"],
                            overlay_revision=int(row["overlay_revision"] or 0),
                        ).to_dict()),
                        _json(identity.to_dict()),
                        _json(dict(migration.get("compatibility") or {})),
                        str(migration.get("actor") or "operator")[:200],
                        now,
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
        record = self._runtime_from_row(updated)
        if record is None:
            raise LookupError(f"unknown chat runtime: {chat_id}")
        return record

    def migration_history(self, chat_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        cap = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM astb_runtime_migration WHERE chat_id=? "
                "ORDER BY created_at DESC LIMIT ?", (str(chat_id), cap),
            ).fetchall()
        return [{
            **dict(row),
            "from_identity": json.loads(row["from_identity_json"]),
            "to_identity": json.loads(row["to_identity_json"]),
            "compatibility": json.loads(row["compatibility_json"]),
        } for row in rows]

    def record_usage(self, chat_id: str, run_id: str, usage: dict[str, Any]) -> ChatRuntimeRecord:
        clean = {
            key: max(0.0, float(usage.get(key) or 0.0))
            for key in _BUDGET_KEYS
        }
        now = time.time()
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown chat runtime: {chat_id}")
            stable_run_id = str(run_id or "").strip() or (
                "unkeyed:" + uuid.uuid4().hex
            )
            clean_json = _json(clean)
            prior = conn.execute(
                "SELECT usage_json FROM astb_budget_charge "
                "WHERE chat_id=? AND run_id=?",
                (chat_id, stable_run_id),
            ).fetchone()
            if prior is not None:
                if str(prior["usage_json"]) != clean_json:
                    raise RuntimeError(
                        "budget run identity was reused with different usage"
                    )
                current = self._runtime_from_row(row)
                if current is None:
                    raise LookupError(f"unknown chat runtime: {chat_id}")
                return current
            used = _json_dict(row["budget_used_json"])
            for key, value in clean.items():
                used[key] = float(used.get(key) or 0.0) + value
            limits = _json_dict(row["budget_limits_json"])
            exhausted = any(
                float(limit or 0) > 0 and float(used.get(key) or 0) >= float(limit)
                for key, limit in limits.items()
                if key in _BUDGET_KEYS
            )
            continuation = "paused_budget_exhausted" if exhausted else row["continuation_state"]
            conn.execute(
                "UPDATE astb_chat_runtime SET budget_used_json=?, continuation_state=?, "
                "updated_at=?, version=version+1 WHERE chat_id=?",
                (_json(used), continuation, now, chat_id),
            )
            event_id = "budget_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO astb_budget_event(event_id, chat_id, run_id, usage_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, chat_id, stable_run_id, clean_json, now),
            )
            conn.execute(
                "INSERT INTO astb_budget_charge(chat_id,run_id,usage_json,event_id,created_at) "
                "VALUES (?,?,?,?,?)",
                (chat_id, stable_run_id, clean_json, event_id, now),
            )
            updated = conn.execute(
                "SELECT * FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return self._runtime_from_row(updated)

    def create_ticket(
        self,
        chat_id: str,
        text: str,
        *,
        delivery: str,
        client_id: str = "",
        source: str = "",
        attachment_id: str = "",
        ticket_id: str = "",
    ) -> InputTicket:
        if delivery not in {"steer", "follow_up", "clarification"}:
            raise ValueError(f"invalid input delivery: {delivery}")
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("input ticket text is required")
        tid = str(ticket_id or "ticket_" + uuid.uuid4().hex)
        now = time.time()
        with self._transaction(immediate=True) as conn:
            runtime = conn.execute(
                "SELECT lifecycle_state FROM astb_chat_runtime WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if runtime is None or runtime["lifecycle_state"] != "active":
                raise RuntimeError(f"chat runtime is not active: {chat_id}")
            existing = conn.execute(
                "SELECT * FROM astb_input_ticket WHERE ticket_id=?", (tid,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["chat_id"] == chat_id
                    and existing["delivery"] == delivery
                    and existing["text"] == clean_text
                )
                if not same:
                    raise ValueError("duplicate input ticket ID conflicts with prior input")
                return self._ticket_from_row(existing)
            conn.execute(
                """
                INSERT INTO astb_input_ticket(
                    ticket_id, chat_id, delivery, text, state, client_id,
                    source, attachment_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (tid, chat_id, delivery, clean_text, client_id, source,
                 attachment_id, now, now),
            )
            row = conn.execute(
                "SELECT * FROM astb_input_ticket WHERE ticket_id=?", (tid,)
            ).fetchone()
        return self._ticket_from_row(row)

    def claim_ticket(
        self,
        chat_id: str,
        delivery: str,
        *,
        run_id: str,
    ) -> InputTicket | None:
        choices = (delivery,) if delivery != "follow_up" else ("follow_up", "clarification")
        placeholders = ",".join("?" for _ in choices)
        now = time.time()
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                f"SELECT * FROM astb_input_ticket WHERE chat_id=? AND state='queued' "
                f"AND delivery IN ({placeholders}) ORDER BY created_at, ticket_id LIMIT 1",  # noqa: S608
                (chat_id, *choices),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE astb_input_ticket SET state='selected', run_id=?, "
                "selected_at=?, updated_at=? WHERE ticket_id=? AND state='queued'",
                (run_id, now, now, row["ticket_id"]),
            )
            selected = conn.execute(
                "SELECT * FROM astb_input_ticket WHERE ticket_id=?", (row["ticket_id"],)
            ).fetchone()
        return self._ticket_from_row(selected)

    def transition_ticket(
        self,
        ticket_id: str,
        state: str,
        *,
        expected: Iterable[str] | None = None,
        proof: dict[str, Any] | None = None,
        error: str = "",
    ) -> InputTicket | None:
        if state not in TICKET_STATES:
            raise ValueError(f"invalid input ticket state: {state}")
        allowed = tuple(expected or ())
        now = time.time()
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM astb_input_ticket WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if row is None:
                return None
            if allowed and row["state"] not in allowed:
                return self._ticket_from_row(row)
            committed = now if state in {"transcript_committing", "completed"} else row["transcript_committed_at"]
            completed = now if state in TICKET_TERMINAL_STATES else row["completed_at"]
            conn.execute(
                """
                UPDATE astb_input_ticket
                SET state=?, updated_at=?, transcript_committed_at=?, completed_at=?,
                    proof_json=?, error=? WHERE ticket_id=?
                """,
                (state, now, committed, completed, _json(proof or _json_dict(row["proof_json"])),
                 str(error or ""), ticket_id),
            )
            updated = conn.execute(
                "SELECT * FROM astb_input_ticket WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        return self._ticket_from_row(updated)

    @staticmethod
    def _outer_tool_call_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "chat_id": str(row["chat_id"]),
            "run_id": str(row["run_id"]),
            "call_id": str(row["call_id"]),
            "tool_name": str(row["tool_name"]),
            "request_fingerprint": str(row["request_fingerprint"]),
            "state": str(row["state"]),
            "outcome": _json_dict(row["outcome_json"]),
            "error": str(row["error"] or ""),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def reserve_outer_tool_call(
        self,
        *,
        chat_id: str,
        run_id: str,
        call_id: str,
        tool_name: str,
        request_fingerprint: str,
    ) -> tuple[dict[str, Any], bool]:
        """Durably cross the outer-call dispatch fence before an effect."""

        clean_chat = chat_id_value(chat_id)
        clean_run = str(run_id or "").strip()
        clean_call = str(call_id or "").strip()
        clean_tool = str(tool_name or "").strip()
        fingerprint = str(request_fingerprint or "").strip()
        if not clean_run or len(clean_run) > 512:
            raise ValueError("outer tool run_id is invalid")
        if not clean_call or len(clean_call) > 512:
            raise ValueError("outer tool call_id is invalid")
        if not clean_tool or len(clean_tool) > 512:
            raise ValueError("outer tool name is invalid")
        if not fingerprint or len(fingerprint) > 128:
            raise ValueError("outer tool request fingerprint is invalid")
        now = time.time()
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM astb_outer_tool_call "
                "WHERE chat_id=? AND run_id=? AND call_id=?",
                (clean_chat, clean_run, clean_call),
            ).fetchone()
            if row is not None:
                if (
                    str(row["tool_name"]) != clean_tool
                    or str(row["request_fingerprint"]) != fingerprint
                ):
                    raise RuntimeError(
                        "outer tool call ID conflicts with a different request"
                    )
                record = self._outer_tool_call_from_row(row)
                assert record is not None
                return record, True
            conn.execute(
                "INSERT INTO astb_outer_tool_call(chat_id,run_id,call_id,"
                "tool_name,request_fingerprint,state,outcome_json,error,"
                "created_at,updated_at) VALUES (?,?,?,?,?,'dispatched','{}','',?,?)",
                (
                    clean_chat, clean_run, clean_call, clean_tool,
                    fingerprint, now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM astb_outer_tool_call "
                "WHERE chat_id=? AND run_id=? AND call_id=?",
                (clean_chat, clean_run, clean_call),
            ).fetchone()
        record = self._outer_tool_call_from_row(row)
        assert record is not None
        return record, False

    def finish_outer_tool_call(
        self,
        *,
        chat_id: str,
        run_id: str,
        call_id: str,
        request_fingerprint: str,
        state: str,
        outcome: dict[str, Any],
        error: str = "",
    ) -> dict[str, Any]:
        terminal = str(state or "").strip()
        if terminal not in {"succeeded", "failed", "unknown_effect"}:
            raise ValueError("outer tool terminal state is invalid")
        now = time.time()
        with self._transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM astb_outer_tool_call "
                "WHERE chat_id=? AND run_id=? AND call_id=?",
                (chat_id, run_id, call_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("outer tool dispatch reservation is absent")
            if str(row["request_fingerprint"]) != str(request_fingerprint):
                raise RuntimeError("outer tool request fingerprint changed")
            if str(row["state"]) in {"succeeded", "failed", "unknown_effect"}:
                record = self._outer_tool_call_from_row(row)
                assert record is not None
                return record
            conn.execute(
                "UPDATE astb_outer_tool_call SET state=?, outcome_json=?, "
                "error=?, updated_at=? WHERE chat_id=? AND run_id=? AND call_id=? "
                "AND state='dispatched'",
                (
                    terminal, _json(dict(outcome or {})), str(error or "")[:4000],
                    now, chat_id, run_id, call_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM astb_outer_tool_call "
                "WHERE chat_id=? AND run_id=? AND call_id=?",
                (chat_id, run_id, call_id),
            ).fetchone()
        record = self._outer_tool_call_from_row(row)
        assert record is not None
        return record

    def get_outer_tool_call(self, chat_id: str, run_id: str, call_id: str) -> dict | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM astb_outer_tool_call WHERE chat_id=? AND run_id=? AND call_id=?",
                (chat_id, run_id, call_id),
            ).fetchone()
        return self._outer_tool_call_from_row(row)

    def get_ticket(self, ticket_id: str) -> InputTicket | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM astb_input_ticket WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
            finally:
                conn.close()
        return self._ticket_from_row(row)

    @staticmethod
    def _queue_revision(conn, chat_id: str) -> int:
        row = conn.execute("SELECT revision FROM astb_input_queue_revision WHERE chat_id=?", (chat_id,)).fetchone()
        return int(row[0]) if row else 0

    def queue_snapshot(self, chat_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            revision = self._queue_revision(conn, chat_id)
            rows = conn.execute(
                "SELECT * FROM astb_input_ticket WHERE chat_id=? "
                "AND state IN ('queued','resume_queued','parked','selected','preparing') "
                "ORDER BY created_at,ticket_id", (chat_id,),
            ).fetchall()
        return {"type": "chat:queue_snapshot", "schema": "variant1.input-queue.v1",
                "session_id": chat_id, "revision": revision,
                "items": [self._ticket_from_row(row).to_dict() for row in rows]}

    def park_tickets(self, chat_id: str, *, reason: str) -> list[InputTicket]:
        with self._transaction(immediate=True) as conn:
            rows = conn.execute(
                "UPDATE astb_input_ticket SET state='parked',updated_at=?,error=? "
                "WHERE chat_id=? AND state IN ('queued','resume_queued','selected','preparing') RETURNING *",
                (time.time(), str(reason), chat_id),
            ).fetchall()
        return sorted((self._ticket_from_row(row) for row in rows), key=lambda t: (t.created_at,t.ticket_id))

    def queued_ticket_command(self, chat_id: str, ticket_id: str, *, expected_revision: int,
                              operation: str) -> InputTicket:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid_queue_revision")
        with self._transaction(immediate=True) as conn:
            if expected_revision != self._queue_revision(conn, chat_id):
                raise RuntimeError("stale_queue_revision")
            row = conn.execute("SELECT * FROM astb_input_ticket WHERE chat_id=? AND ticket_id=?",
                               (chat_id,ticket_id)).fetchone()
            if row is None:
                raise LookupError("queue_ticket_not_found")
            if operation == "continue":
                if row["state"] != "parked":
                    raise RuntimeError("ticket_not_parked")
                conn.execute("UPDATE astb_input_ticket SET state='preparing',delivery='follow_up',"
                             "run_id='',selected_at=?,updated_at=?,error='' WHERE ticket_id=?",
                             (time.time(),time.time(),ticket_id))
            elif operation == "remove":
                if row["state"] not in {"queued","resume_queued","parked"}:
                    raise RuntimeError("ticket_not_removable")
                conn.execute("UPDATE astb_input_ticket SET state='cancelled',completed_at=?,updated_at=?,"
                             "error='removed_by_user' WHERE ticket_id=?", (time.time(),time.time(),ticket_id))
            else:
                raise ValueError("unknown_queue_operation")
            return self._ticket_from_row(conn.execute(
                "SELECT * FROM astb_input_ticket WHERE ticket_id=?", (ticket_id,)).fetchone())

    def list_tickets(
        self,
        chat_id: str,
        *,
        states: Iterable[str] | None = None,
    ) -> list[InputTicket]:
        selected = tuple(states or ())
        params: tuple[Any, ...] = (chat_id,)
        clause = ""
        if selected:
            clause = " AND state IN (" + ",".join("?" for _ in selected) + ")"
            params += selected
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM astb_input_ticket WHERE chat_id=?" + clause
                    + " ORDER BY created_at, ticket_id",
                    params,
                ).fetchall()
            finally:
                conn.close()
        return [self._ticket_from_row(row) for row in rows]

    def link_thread(self, chat_id: str, thread_id: str, *, source: str) -> None:
        if not thread_id:
            return
        with self._transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO chat_thread_ref(chat_id, thread_id, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, thread_id, source, time.time()),
            )

    def thread_refs(self, chat_id: str, *, include_deleted: bool = False) -> list[str]:
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT thread_id FROM chat_thread_ref WHERE chat_id=?" + clause
                    + " ORDER BY created_at, thread_id",
                    (chat_id,),
                ).fetchall()
            finally:
                conn.close()
        return [str(row["thread_id"]) for row in rows]

    def mark_threads_deleted(self, chat_id: str) -> None:
        with self._transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE chat_thread_ref SET deleted_at=COALESCE(deleted_at, ?) WHERE chat_id=?",
                (time.time(), chat_id),
            )
