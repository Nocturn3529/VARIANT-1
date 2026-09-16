"""Durable SQLite registry, event journal, and bounded execution spool."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Iterator, Mapping

from core_invariants import (
    sqlite_read_connection,
    sqlite_unit_of_work,
    sqlite_wal_connection,
    sqlite_writer_lock,
)
from work_fabric.scope import (
    WorkScope,
    append_json_scope_visibility,
    coerce_work_scope,
    work_scope_visible,
)

from .models import (
    ACTIVE_PROCESS_STATES,
    ACTIVE_TERMINAL_STATES,
    ExecutionEvent,
    ExecutionNotFound,
    ExecutionOwner,
    ExecutionScopeMismatch,
    ExecutionValidationError,
    OutputFrame,
    OutputPage,
    ProcessRecipe,
    ProcessRecord,
    PROCESS_STATES,
    TERMINAL_STATES,
    TerminalRecord,
    canonical_json,
)


def default_execution_path(*, data_dir: str | None = None) -> str:
    if data_dir:
        root = os.path.abspath(data_dir)
    else:
        backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        root = os.path.abspath(os.environ.get("VARIANT1_DATA_DIR") or backend_root)
        root = os.path.join(root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_EXECUTION_DB")
        or os.path.join(root, "execution", "execution.sqlite3")
    )


def new_execution_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _load_object(value: str | None, field: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except Exception as exc:
        raise ExecutionValidationError(f"invalid persisted JSON in {field}") from exc
    if not isinstance(result, dict):
        raise ExecutionValidationError(f"persisted {field} must be an object")
    return result


class ExecutionRepository:
    """Short-connection authority with CAS-backed output compaction.

    The live SQLite spool is bounded per terminal/process.  Evicted frames are
    stored as immutable CAS objects and the exact cursor mapping remains in
    ``execution_output_segment``.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        data_dir: str | None = None,
        artifact_store: Any,
        live_spool_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if path and data_dir:
            raise ValueError("pass path or data_dir, not both")
        if artifact_store is None:
            raise ValueError("artifact_store is required for bounded lossless output")
        self.path = os.path.abspath(path or default_execution_path(data_dir=data_dir))
        self.artifact_store = artifact_store
        self.live_spool_bytes = max(64, min(int(live_spool_bytes), 256 * 1024 * 1024))
        self._write_lock = sqlite_writer_lock(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_wal_connection(self.path)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with sqlite_read_connection(self._connect) as conn:
            yield conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="execution.before_commit"
        ) as conn:
            yield conn

    def _initialize(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS execution_terminal (
                      terminal_id TEXT PRIMARY KEY,
                      owner_kind TEXT NOT NULL,
                      owner_id TEXT NOT NULL,
                      scope_json TEXT NOT NULL,
                      profile TEXT NOT NULL,
                      cwd TEXT NOT NULL,
                      cols INTEGER NOT NULL,
                      rows INTEGER NOT NULL,
                      state TEXT NOT NULL,
                      transport TEXT NOT NULL,
                      capabilities_json TEXT NOT NULL,
                      pid INTEGER NOT NULL DEFAULT 0,
                      pid_started_at REAL NOT NULL DEFAULT 0,
                      backend_instance_id TEXT NOT NULL,
                      output_cursor INTEGER NOT NULL DEFAULT 0,
                      live_start_cursor INTEGER NOT NULL DEFAULT 0,
                      attachments INTEGER NOT NULL DEFAULT 0,
                      exit_code INTEGER,
                      recovery_json TEXT NOT NULL DEFAULT '{}',
                      revision INTEGER NOT NULL DEFAULT 1,
                      created_at REAL NOT NULL,
                      updated_at REAL NOT NULL,
                      exited_at REAL NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS execution_terminal_owner
                      ON execution_terminal(owner_kind, owner_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS execution_terminal_state
                      ON execution_terminal(state, updated_at);

                    CREATE TABLE IF NOT EXISTS execution_process (
                      process_id TEXT PRIMARY KEY,
                      owner_kind TEXT NOT NULL,
                      owner_id TEXT NOT NULL,
                      scope_json TEXT NOT NULL,
                      recipe_json TEXT NOT NULL,
                      state TEXT NOT NULL,
                      pid INTEGER NOT NULL DEFAULT 0,
                      pid_started_at REAL NOT NULL DEFAULT 0,
                      backend_instance_id TEXT NOT NULL,
                      attempt INTEGER NOT NULL DEFAULT 1,
                      output_cursor INTEGER NOT NULL DEFAULT 0,
                      live_start_cursor INTEGER NOT NULL DEFAULT 0,
                      exit_code INTEGER,
                      health_json TEXT NOT NULL DEFAULT '{}',
                      recovery_json TEXT NOT NULL DEFAULT '{}',
                      revision INTEGER NOT NULL DEFAULT 1,
                      created_at REAL NOT NULL,
                      updated_at REAL NOT NULL,
                      exited_at REAL NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS execution_process_owner
                      ON execution_process(owner_kind, owner_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS execution_process_state
                      ON execution_process(state, updated_at);

                    CREATE TABLE IF NOT EXISTS execution_output_chunk (
                      entity_kind TEXT NOT NULL,
                      entity_id TEXT NOT NULL,
                      start_cursor INTEGER NOT NULL,
                      end_cursor INTEGER NOT NULL,
                      stream TEXT NOT NULL,
                      payload BLOB NOT NULL,
                      created_at REAL NOT NULL,
                      PRIMARY KEY(entity_kind, entity_id, start_cursor),
                      CHECK(end_cursor > start_cursor)
                    );
                    CREATE INDEX IF NOT EXISTS execution_output_chunk_read
                      ON execution_output_chunk(entity_kind, entity_id, end_cursor);

                    CREATE TABLE IF NOT EXISTS execution_output_segment (
                      entity_kind TEXT NOT NULL,
                      entity_id TEXT NOT NULL,
                      start_cursor INTEGER NOT NULL,
                      end_cursor INTEGER NOT NULL,
                      stream TEXT NOT NULL,
                      artifact_ref TEXT NOT NULL,
                      artifact_bytes INTEGER NOT NULL,
                      created_at REAL NOT NULL,
                      PRIMARY KEY(entity_kind, entity_id, start_cursor),
                      CHECK(end_cursor > start_cursor)
                    );
                    CREATE INDEX IF NOT EXISTS execution_output_segment_read
                      ON execution_output_segment(entity_kind, entity_id, end_cursor);

                    CREATE TABLE IF NOT EXISTS execution_event (
                      sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                      event_id TEXT NOT NULL UNIQUE,
                      entity_kind TEXT NOT NULL,
                      entity_id TEXT NOT NULL,
                      event_type TEXT NOT NULL,
                      revision INTEGER NOT NULL,
                      payload_json TEXT NOT NULL,
                      created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS execution_event_entity
                      ON execution_event(entity_kind, entity_id, sequence);
                    """
                )
            finally:
                conn.close()

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        *,
        entity_kind: str,
        entity_id: str,
        event_type: str,
        revision: int,
        payload: Mapping[str, Any] | None = None,
        at: float | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO execution_event(
              event_id, entity_kind, entity_id, event_type, revision,
              payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                f"evt_{uuid.uuid4().hex}", entity_kind, entity_id, event_type,
                int(revision), canonical_json(dict(payload or {})),
                float(at if at is not None else time.time()),
            ),
        )

    @staticmethod
    def _terminal_from_row(row: sqlite3.Row) -> TerminalRecord:
        scope = _load_object(row["scope_json"], "terminal.scope")
        return TerminalRecord(
            terminal_id=str(row["terminal_id"]),
            owner=ExecutionOwner(
                kind=str(row["owner_kind"]), owner_id=str(row["owner_id"]),
                scope=scope,
            ),
            profile=str(row["profile"]), cwd=str(row["cwd"]),
            cols=int(row["cols"]), rows=int(row["rows"]), state=str(row["state"]),
            transport=str(row["transport"]),
            capabilities=_load_object(row["capabilities_json"], "terminal.capabilities"),
            pid=int(row["pid"] or 0), pid_started_at=float(row["pid_started_at"] or 0),
            backend_instance_id=str(row["backend_instance_id"] or ""),
            output_cursor=int(row["output_cursor"] or 0),
            live_start_cursor=int(row["live_start_cursor"] or 0),
            attachments=int(row["attachments"] or 0),
            exit_code=None if row["exit_code"] is None else int(row["exit_code"]),
            recovery=_load_object(row["recovery_json"], "terminal.recovery"),
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]), exited_at=float(row["exited_at"] or 0),
        )

    @staticmethod
    def _process_from_row(row: sqlite3.Row) -> ProcessRecord:
        scope = _load_object(row["scope_json"], "process.scope")
        return ProcessRecord(
            process_id=str(row["process_id"]),
            owner=ExecutionOwner(
                kind=str(row["owner_kind"]), owner_id=str(row["owner_id"]),
                scope=scope,
            ),
            recipe=ProcessRecipe.from_mapping(
                _load_object(row["recipe_json"], "process.recipe")),
            state=str(row["state"]), pid=int(row["pid"] or 0),
            pid_started_at=float(row["pid_started_at"] or 0),
            backend_instance_id=str(row["backend_instance_id"] or ""),
            attempt=int(row["attempt"]), output_cursor=int(row["output_cursor"] or 0),
            live_start_cursor=int(row["live_start_cursor"] or 0),
            exit_code=None if row["exit_code"] is None else int(row["exit_code"]),
            health=_load_object(row["health_json"], "process.health"),
            recovery=_load_object(row["recovery_json"], "process.recovery"),
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]), exited_at=float(row["exited_at"] or 0),
        )

    def create_terminal(
        self,
        *,
        terminal_id: str,
        owner: ExecutionOwner,
        profile: str,
        cwd: str,
        cols: int,
        rows: int,
        transport: str,
        capabilities: Mapping[str, Any],
        pid: int,
        pid_started_at: float,
        backend_instance_id: str,
    ) -> TerminalRecord:
        now = time.time()
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO execution_terminal(
                  terminal_id, owner_kind, owner_id, scope_json, profile, cwd,
                  cols, rows, state, transport, capabilities_json, pid,
                  pid_started_at, backend_instance_id, attachments,
                  created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    terminal_id, owner.kind, owner.owner_id,
                    canonical_json(owner.scope.to_dict()), profile, cwd,
                    int(cols), int(rows), "running", transport,
                    canonical_json(dict(capabilities)), int(pid),
                    float(pid_started_at), backend_instance_id, 1, now, now,
                ),
            )
            self._event(
                conn, entity_kind="terminal", entity_id=terminal_id,
                event_type="terminal.started", revision=1,
                payload={
                    "pid": int(pid), "profile": profile, "cwd": cwd,
                    "transport": transport, "capabilities": dict(capabilities),
                    "owner": owner.to_dict(),
                }, at=now,
            )
        return self.get_terminal(terminal_id)

    def create_process(
        self,
        *,
        process_id: str,
        owner: ExecutionOwner,
        recipe: ProcessRecipe,
        pid: int,
        pid_started_at: float,
        backend_instance_id: str,
    ) -> ProcessRecord:
        now = time.time()
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO execution_process(
                  process_id, owner_kind, owner_id, scope_json, recipe_json,
                  state, pid, pid_started_at, backend_instance_id, attempt,
                  health_json, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    process_id, owner.kind, owner.owner_id,
                    canonical_json(owner.scope.to_dict()),
                    canonical_json(recipe.to_dict()), "running", int(pid),
                    float(pid_started_at), backend_instance_id, 1,
                    canonical_json({"status": "unchecked"}), now, now,
                ),
            )
            self._event(
                conn, entity_kind="process", entity_id=process_id,
                event_type="process.started", revision=1,
                payload={
                    "pid": int(pid), "attempt": 1,
                    "recipe": recipe.to_dict(), "owner": owner.to_dict(),
                }, at=now,
            )
        return self.get_process(process_id)

    def reserve_process(
        self,
        *,
        process_id: str,
        owner: ExecutionOwner,
        recipe: ProcessRecipe,
        backend_instance_id: str,
    ) -> ProcessRecord:
        """Persist process identity before any OS child can be spawned."""

        now = time.time()
        with self._write() as conn:
            prior = conn.execute(
                "SELECT * FROM execution_process WHERE process_id=?",
                (str(process_id),),
            ).fetchone()
            if prior is not None:
                record = self._process_from_row(prior)
                if record.owner != owner or record.recipe != recipe:
                    raise ExecutionValidationError(
                        "process_id is reserved for a different recipe or owner"
                    )
                return record
            conn.execute(
                """
                INSERT INTO execution_process(
                  process_id, owner_kind, owner_id, scope_json, recipe_json,
                  state, pid, pid_started_at, backend_instance_id, attempt,
                  health_json, recovery_json, created_at, updated_at
                ) VALUES(?,?,?,?,?,'starting',0,0,?,1,?,?,?,?)
                """,
                (
                    process_id, owner.kind, owner.owner_id,
                    canonical_json(owner.scope.to_dict()),
                    canonical_json(recipe.to_dict()), backend_instance_id,
                    canonical_json({"status": "unchecked"}),
                    canonical_json({"dispatch_reserved": True}), now, now,
                ),
            )
            self._event(
                conn, entity_kind="process", entity_id=process_id,
                event_type="process.dispatch_reserved", revision=1,
                payload={"recipe": recipe.to_dict(), "owner": owner.to_dict()},
                at=now,
            )
        return self.get_process(process_id)

    def activate_reserved_process(
        self,
        process_id: str,
        *,
        pid: int,
        pid_started_at: float,
    ) -> ProcessRecord:
        record = self.get_process(process_id)
        if record.state != "starting" or record.pid:
            raise ExecutionValidationError(
                "process dispatch reservation is no longer activatable"
            )
        return self.transition_process(
            process_id,
            "running",
            pid=int(pid),
            pid_started_at=float(pid_started_at),
            recovery={"dispatch_reserved": True, "dispatch_activated": True},
            event_type="process.started",
            payload={"pid": int(pid), "attempt": int(record.attempt)},
        )

    @staticmethod
    def _assert_scope(
        owner: WorkScope,
        supplied: WorkScope | Mapping[str, Any] | None,
    ) -> None:
        if supplied is not None and not work_scope_visible(owner, supplied):
            raise ExecutionScopeMismatch(
                "execution record is outside the owning WorkScope"
            )

    def get_terminal(
        self,
        terminal_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> TerminalRecord:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM execution_terminal WHERE terminal_id=?",
                (str(terminal_id),),
            ).fetchone()
        if row is None:
            raise ExecutionNotFound(f"terminal not found: {terminal_id}")
        record = self._terminal_from_row(row)
        self._assert_scope(record.owner.scope, scope)
        return record

    def get_process(
        self,
        process_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> ProcessRecord:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM execution_process WHERE process_id=?",
                (str(process_id),),
            ).fetchone()
        if row is None:
            raise ExecutionNotFound(f"process not found: {process_id}")
        record = self._process_from_row(row)
        self._assert_scope(record.owner.scope, scope)
        return record

    def list_terminals(
        self, *, owner_kind: str = "", owner_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> list[TerminalRecord]:
        clauses: list[str] = []
        args: list[Any] = []
        if owner_kind:
            clauses.append("owner_kind=?")
            args.append(owner_kind)
        if owner_id:
            clauses.append("owner_id=?")
            args.append(owner_id)
        self._append_scope_filter(clauses, args, "scope_json", scope)
        sql = "SELECT * FROM execution_terminal"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._read() as conn:
            return [self._terminal_from_row(row) for row in conn.execute(sql, args)]

    @staticmethod
    def _append_operator_chat_filter(
        clauses: list[str], args: list[Any], chat_id: str, prefix: str = "",
    ) -> None:
        """Host UI authority follows durable chat ownership, not live generation."""
        clean = str(chat_id or "").strip()
        if not clean:
            clauses.append("0")
            return
        clauses.extend([
            f"COALESCE(json_extract({prefix}scope_json, '$.chat_id'),'')=?",
            f"({prefix}owner_kind!='chat' OR {prefix}owner_id=?)",
        ])
        args.extend((clean, clean))

    def _get_for_operator_chat(self, kind: str, identity: str, chat_id: str):
        record = getattr(self, f"get_{kind}")(identity)
        clean = str(chat_id or "").strip()
        if (not clean or record.owner.scope.chat_id != clean
                or (record.owner.kind == "chat" and record.owner.owner_id != clean)):
            raise ExecutionScopeMismatch("execution record is outside the selected chat")
        return record

    def get_terminal_for_chat(self, terminal_id: str, chat_id: str) -> TerminalRecord:
        return self._get_for_operator_chat("terminal", terminal_id, chat_id)

    def get_process_for_chat(self, process_id: str, chat_id: str) -> ProcessRecord:
        return self._get_for_operator_chat("process", process_id, chat_id)

    def _list_for_operator_chat(self, kind: str, chat_id: str, limit: int):
        table, _key = self._entity_table(kind)
        clauses, args = [], []
        self._append_operator_chat_filter(clauses, args, chat_id)
        args.append(max(1, min(int(limit), 1000)))
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT ?", args,
            ).fetchall()
        return [getattr(self, f"_{kind}_from_row")(row) for row in rows]

    def list_terminals_for_chat(self, chat_id: str, *, limit: int = 200):
        return self._list_for_operator_chat("terminal", chat_id, limit)

    def list_processes_for_chat(self, chat_id: str, *, limit: int = 200):
        return self._list_for_operator_chat("process", chat_id, limit)

    def list_processes(
        self, *, owner_kind: str = "", owner_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> list[ProcessRecord]:
        clauses: list[str] = []
        args: list[Any] = []
        if owner_kind:
            clauses.append("owner_kind=?")
            args.append(owner_kind)
        if owner_id:
            clauses.append("owner_id=?")
            args.append(owner_id)
        self._append_scope_filter(clauses, args, "scope_json", scope)
        sql = "SELECT * FROM execution_process"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._read() as conn:
            return [self._process_from_row(row) for row in conn.execute(sql, args)]

    def has_live_worktree_owner(self, worktree_id: str) -> bool:
        clean = str(worktree_id or "").strip()
        if not clean:
            return False
        terminal_states = sorted(ACTIVE_TERMINAL_STATES)
        process_states = sorted(ACTIVE_PROCESS_STATES)
        with self._read() as conn:
            terminal = conn.execute(
                f"""SELECT 1 FROM execution_terminal
                    WHERE state IN ({','.join('?' for _ in terminal_states)})
                      AND COALESCE(json_extract(scope_json, '$.worktree_id'),'')=?
                    LIMIT 1""",
                (*terminal_states, clean),
            ).fetchone()
            if terminal is not None:
                return True
            process = conn.execute(
                f"""SELECT 1 FROM execution_process
                    WHERE state IN ({','.join('?' for _ in process_states)})
                      AND COALESCE(json_extract(scope_json, '$.worktree_id'),'')=?
                    LIMIT 1""",
                (*process_states, clean),
            ).fetchone()
            if process is not None:
                return True
            ambiguous = [
                (int(row["pid"] or 0), float(row["pid_started_at"] or 0.0))
                for row in conn.execute(
                    "SELECT pid, pid_started_at FROM execution_terminal "
                    "WHERE state='unknown_effect' "
                    "AND COALESCE(json_extract(scope_json, '$.worktree_id'),'')=? "
                    "AND COALESCE(json_extract(recovery_json, '$.status'),'')="
                    "'pid_alive_unattached'",
                    (clean,),
                ).fetchall()
            ]
            ambiguous.extend(
                (int(row["pid"] or 0), float(row["pid_started_at"] or 0.0))
                for row in conn.execute(
                    "SELECT pid, pid_started_at FROM execution_process "
                    "WHERE state='unknown_effect' "
                    "AND COALESCE(json_extract(scope_json, '$.worktree_id'),'')=? "
                    "AND COALESCE(json_extract(recovery_json, '$.status'),'')="
                    "'pid_alive_unattached'",
                    (clean,),
                ).fetchall()
            )
        # A failed reconcile kill remains an owner while that exact PID
        # generation still exists. Once it disappears, worktree GC may proceed.
        for pid, started_at in ambiguous:
            observation = _probe_pid(pid, started_at)
            if observation.get("exists") and observation.get("identity_matches"):
                return True
        return False

    def live_ids_for_chat(self, chat_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        clean = str(chat_id or "").strip()
        if not clean:
            return (), ()
        terminal_states = sorted(ACTIVE_TERMINAL_STATES)
        process_states = sorted(ACTIVE_PROCESS_STATES)
        with self._read() as conn:
            terminals = conn.execute(
                f"""SELECT terminal_id FROM execution_terminal
                    WHERE state IN ({','.join('?' for _ in terminal_states)})
                      AND COALESCE(json_extract(scope_json, '$.chat_id'),'')=?
                    ORDER BY terminal_id""",
                (*terminal_states, clean),
            ).fetchall()
            processes = conn.execute(
                f"""SELECT process_id FROM execution_process
                    WHERE state IN ({','.join('?' for _ in process_states)})
                      AND COALESCE(json_extract(scope_json, '$.chat_id'),'')=?
                    ORDER BY process_id""",
                (*process_states, clean),
            ).fetchall()
        return (
            tuple(str(row["terminal_id"]) for row in terminals),
            tuple(str(row["process_id"]) for row in processes),
        )

    @staticmethod
    def _append_scope_filter(
        clauses: list[str], args: list[Any], column: str,
        scope: WorkScope | Mapping[str, Any] | None,
    ) -> None:
        append_json_scope_visibility(clauses, args, column, scope)

    def record_action(
        self, entity_kind: str, entity_id: str, event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        table, key = self._entity_table(entity_kind)
        with self._write() as conn:
            row = conn.execute(
                f"SELECT revision FROM {table} WHERE {key}=?", (entity_id,)
            ).fetchone()
            if row is None:
                raise ExecutionNotFound(f"{entity_kind} not found: {entity_id}")
            self._event(
                conn, entity_kind=entity_kind, entity_id=entity_id,
                event_type=event_type, revision=int(row["revision"]),
                payload=payload,
            )

    @staticmethod
    def _entity_table(entity_kind: str) -> tuple[str, str]:
        if entity_kind == "terminal":
            return "execution_terminal", "terminal_id"
        if entity_kind == "process":
            return "execution_process", "process_id"
        raise ExecutionValidationError(f"invalid execution entity kind: {entity_kind}")

    def transition_terminal(
        self,
        terminal_id: str,
        state: str,
        *,
        exit_code: int | None = None,
        recovery: Mapping[str, Any] | None = None,
        event_type: str = "terminal.state_changed",
        payload: Mapping[str, Any] | None = None,
    ) -> TerminalRecord:
        if state not in TERMINAL_STATES:
            raise ExecutionValidationError(f"invalid terminal state: {state}")
        now = time.time()
        terminal = self.get_terminal(terminal_id)
        revision = terminal.revision + 1
        exited_at = now if state in {"exited", "terminated", "failed", "unknown_effect"} else 0.0
        with self._write() as conn:
            result = conn.execute(
                """
                UPDATE execution_terminal SET state=?, exit_code=?, recovery_json=?,
                  revision=?, updated_at=?, exited_at=?
                WHERE terminal_id=? AND revision=?
                """,
                (
                    state, exit_code,
                    canonical_json(dict(recovery if recovery is not None else terminal.recovery)),
                    revision, now, exited_at, terminal_id, terminal.revision,
                ),
            )
            if result.rowcount != 1:
                raise ExecutionValidationError("terminal transition lost a revision race")
            event_payload = {"from": terminal.state, "to": state, "exit_code": exit_code}
            event_payload.update(dict(payload or {}))
            self._event(
                conn, entity_kind="terminal", entity_id=terminal_id,
                event_type=event_type, revision=revision, payload=event_payload, at=now,
            )
        return self.get_terminal(terminal_id)

    def resize_terminal(self, terminal_id: str, *, cols: int, rows: int) -> TerminalRecord:
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM execution_terminal WHERE terminal_id=?", (terminal_id,),
            ).fetchone()
            if row is None:
                raise ExecutionNotFound(f"terminal not found: {terminal_id}")
            terminal = self._terminal_from_row(row)
            if (terminal.cols, terminal.rows) == (int(cols), int(rows)):
                return terminal
            revision = terminal.revision + 1
            result = conn.execute(
                """UPDATE execution_terminal SET cols=?, rows=?, revision=?, updated_at=?
                   WHERE terminal_id=? AND revision=?""",
                (int(cols), int(rows), revision, now, terminal_id, terminal.revision),
            )
            if result.rowcount != 1:
                raise ExecutionValidationError("terminal resize lost a revision race")
            self._event(
                conn, entity_kind="terminal", entity_id=terminal_id,
                event_type="terminal.resized", revision=revision,
                payload={"cols": int(cols), "rows": int(rows)}, at=now,
            )
        return self.get_terminal(terminal_id)

    def adjust_terminal_attachments(self, terminal_id: str, delta: int) -> TerminalRecord:
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT attachments, revision FROM execution_terminal WHERE terminal_id=?",
                (terminal_id,),
            ).fetchone()
            if row is None:
                raise ExecutionNotFound(f"terminal not found: {terminal_id}")
            attachments = max(0, int(row["attachments"]) + int(delta))
            revision = int(row["revision"]) + 1
            conn.execute(
                """UPDATE execution_terminal SET attachments=?, revision=?, updated_at=?
                   WHERE terminal_id=?""",
                (attachments, revision, now, terminal_id),
            )
            self._event(
                conn, entity_kind="terminal", entity_id=terminal_id,
                event_type="terminal.attached" if delta > 0 else "terminal.detached",
                revision=revision, payload={"attachments": attachments}, at=now,
            )
        return self.get_terminal(terminal_id)

    def transition_process(
        self,
        process_id: str,
        state: str,
        *,
        exit_code: int | None = None,
        health: Mapping[str, Any] | None = None,
        recovery: Mapping[str, Any] | None = None,
        pid: int | None = None,
        pid_started_at: float | None = None,
        attempt: int | None = None,
        event_type: str = "process.state_changed",
        payload: Mapping[str, Any] | None = None,
    ) -> ProcessRecord:
        if state not in PROCESS_STATES:
            raise ExecutionValidationError(f"invalid process state: {state}")
        now = time.time()
        process = self.get_process(process_id)
        revision = process.revision + 1
        exited_at = now if state in {"exited", "terminated", "failed", "unknown_effect"} else 0.0
        with self._write() as conn:
            result = conn.execute(
                """
                UPDATE execution_process SET state=?, exit_code=?, health_json=?,
                  recovery_json=?, pid=?, pid_started_at=?, attempt=?, revision=?,
                  updated_at=?, exited_at=?
                WHERE process_id=? AND revision=?
                """,
                (
                    state, exit_code,
                    canonical_json(dict(health if health is not None else process.health)),
                    canonical_json(dict(recovery if recovery is not None else process.recovery)),
                    int(process.pid if pid is None else pid),
                    float(process.pid_started_at if pid_started_at is None else pid_started_at),
                    int(process.attempt if attempt is None else attempt), revision, now,
                    exited_at, process_id, process.revision,
                ),
            )
            if result.rowcount != 1:
                raise ExecutionValidationError("process transition lost a revision race")
            event_payload = {
                "from": process.state, "to": state, "exit_code": exit_code,
                "pid": process.pid if pid is None else int(pid),
                "attempt": process.attempt if attempt is None else int(attempt),
            }
            event_payload.update(dict(payload or {}))
            self._event(
                conn, entity_kind="process", entity_id=process_id,
                event_type=event_type, revision=revision, payload=event_payload, at=now,
            )
        return self.get_process(process_id)

    def append_output(
        self, entity_kind: str, entity_id: str, stream: str, payload: bytes,
    ) -> tuple[int, int]:
        raw = bytes(payload)
        if not raw:
            table, key = self._entity_table(entity_kind)
            with self._read() as conn:
                row = conn.execute(
                    f"SELECT output_cursor FROM {table} WHERE {key}=?", (entity_id,)
                ).fetchone()
            if row is None:
                raise ExecutionNotFound(f"{entity_kind} not found: {entity_id}")
            cursor = int(row["output_cursor"])
            return cursor, cursor
        if len(raw) > 1024 * 1024:
            raise ExecutionValidationError("one output frame cannot exceed 1 MiB")
        stream_name = str(stream or "output")[:32]
        table, key = self._entity_table(entity_kind)
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                f"SELECT output_cursor, live_start_cursor FROM {table} WHERE {key}=?", (entity_id,)
            ).fetchone()
            if row is None:
                raise ExecutionNotFound(f"{entity_kind} not found: {entity_id}")
            start = int(row["output_cursor"])
            end = start + len(raw)
            conn.execute(
                """INSERT INTO execution_output_chunk(
                     entity_kind, entity_id, start_cursor, end_cursor, stream,
                     payload, created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (entity_kind, entity_id, start, end, stream_name, raw, now),
            )
            conn.execute(
                f"UPDATE {table} SET output_cursor=?, updated_at=? WHERE {key}=?",
                (end, now, entity_id),
            )
            self._compact_locked(
                conn, entity_kind, entity_id, table, key,
                live_bytes=end - int(row["live_start_cursor"]),
            )
        return start, end

    def _compact_locked(
        self, conn: sqlite3.Connection, entity_kind: str, entity_id: str,
        table: str, key: str, *, live_bytes: int,
    ) -> None:
        # Entity cursors are contiguous and updated in this transaction. Avoid
        # scanning every retained frame for every short TUI output write.
        total = live_bytes
        if total <= self.live_spool_bytes:
            return
        target = max(32, self.live_spool_bytes * 3 // 4)
        rows = conn.execute(
            """SELECT start_cursor, end_cursor, stream, payload, created_at
               FROM execution_output_chunk
               WHERE entity_kind=? AND entity_id=? ORDER BY start_cursor""",
            (entity_kind, entity_id),
        ).fetchall()
        scope = f"execution:{entity_kind}:{entity_id}"
        live_start = 0
        for row in rows:
            if total <= target:
                live_start = int(row["start_cursor"])
                break
            raw = bytes(row["payload"])
            ref = self.artifact_store.put_bytes(
                raw,
                media_type="application/octet-stream",
                kind="execution_output_segment",
                scope=scope,
            )
            conn.execute(
                """INSERT OR IGNORE INTO execution_output_segment(
                     entity_kind, entity_id, start_cursor, end_cursor, stream,
                     artifact_ref, artifact_bytes, created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    entity_kind, entity_id, int(row["start_cursor"]),
                    int(row["end_cursor"]), str(row["stream"]), ref.ref,
                    len(raw), float(row["created_at"]),
                ),
            )
            conn.execute(
                """DELETE FROM execution_output_chunk
                   WHERE entity_kind=? AND entity_id=? AND start_cursor=?""",
                (entity_kind, entity_id, int(row["start_cursor"])),
            )
            total -= len(raw)
        else:
            row = conn.execute(
                f"SELECT output_cursor FROM {table} WHERE {key}=?", (entity_id,)
            ).fetchone()
            live_start = int(row["output_cursor"] if row else 0)
        conn.execute(
            f"UPDATE {table} SET live_start_cursor=? WHERE {key}=?",
            (live_start, entity_id),
        )

    def read_output(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        after_cursor: int = 0,
        max_bytes: int = 64 * 1024,
        max_frames: int = 200,
        prefer_artifact_refs: bool = False,
    ) -> OutputPage:
        cursor = int(after_cursor)
        if cursor < 0:
            raise ExecutionValidationError("after_cursor cannot be negative")
        byte_limit = max(1, min(int(max_bytes), 1024 * 1024))
        frame_limit = max(1, min(int(max_frames), 1000))
        table, key = self._entity_table(entity_kind)
        with self._read() as conn:
            # A writer can move chunks to the archive between SELECTs. Read
            # cursor, archived segments and live frames from one WAL snapshot.
            conn.execute("BEGIN")
            owner_row = conn.execute(
                f"SELECT output_cursor FROM {table} WHERE {key}=?", (entity_id,)
            ).fetchone()
            if owner_row is None:
                raise ExecutionNotFound(f"{entity_kind} not found: {entity_id}")
            end_cursor = int(owner_row["output_cursor"])
            if cursor > end_cursor:
                raise ExecutionValidationError(
                    f"after_cursor {cursor} exceeds output cursor {end_cursor}")
            segment_rows = conn.execute(
                """SELECT start_cursor, end_cursor, stream, artifact_ref,
                          artifact_bytes, NULL AS payload, 1 AS archived
                   FROM execution_output_segment
                   WHERE entity_kind=? AND entity_id=? AND end_cursor>?
                   ORDER BY start_cursor LIMIT ?
                """,
                (entity_kind, entity_id, cursor, frame_limit),
            ).fetchall()
            chunk_rows = conn.execute(
                """SELECT start_cursor, end_cursor, stream, '' AS artifact_ref,
                          (end_cursor-start_cursor) AS artifact_bytes,
                          payload, 0 AS archived
                   FROM execution_output_chunk
                   WHERE entity_kind=? AND entity_id=? AND end_cursor>?
                   ORDER BY start_cursor LIMIT ?
                """,
                (entity_kind, entity_id, cursor, frame_limit),
            ).fetchall()
        rows = sorted([*segment_rows, *chunk_rows], key=lambda row: int(row["start_cursor"]))
        frames: list[OutputFrame] = []
        remaining = byte_limit
        next_cursor = cursor
        scope = f"execution:{entity_kind}:{entity_id}"
        for row in rows:
            if len(frames) >= frame_limit or remaining <= 0:
                break
            start = int(row["start_cursor"])
            end = int(row["end_cursor"])
            take_start = max(next_cursor, start)
            if take_start >= end:
                continue
            offset = take_start - start
            available = end - take_start
            archived = bool(row["archived"])
            if archived and prefer_artifact_refs:
                frames.append(OutputFrame(
                    start_cursor=take_start,
                    end_cursor=end,
                    stream=str(row["stream"]),
                    artifact_ref=str(row["artifact_ref"]),
                    artifact_offset=offset,
                ))
                next_cursor = end
                continue
            if archived:
                raw = self.artifact_store.read_bytes_scoped(
                    str(row["artifact_ref"]), scope)
            else:
                raw = bytes(row["payload"])
            take = min(available, remaining)
            piece = raw[offset:offset + take]
            frames.append(OutputFrame(
                start_cursor=take_start,
                end_cursor=take_start + len(piece),
                stream=str(row["stream"]), data=piece,
            ))
            remaining -= len(piece)
            next_cursor = take_start + len(piece)
            if next_cursor < end:
                break
        return OutputPage(
            entity_kind=entity_kind, entity_id=entity_id,
            after_cursor=cursor, next_cursor=next_cursor, end_cursor=end_cursor,
            frames=tuple(frames), more=next_cursor < end_cursor,
        )

    def input_receipts(self, entity_kind: str, entity_id: str) -> list[dict]:
        self._entity_table(entity_kind)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM execution_event WHERE entity_kind=? "
                "AND entity_id=? AND event_type LIKE ? ORDER BY sequence DESC LIMIT 128",
                (entity_kind, entity_id, f"{entity_kind}.input_%"),
            ).fetchall()
        latest = {}
        for row in rows:
            value = json.loads(row["payload_json"])
            identity = value.get("write_id")
            if identity and identity not in latest:
                latest[identity] = value
        return list(latest.values())[:16]

    def list_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        entity_kind: str = "",
        entity_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        _operator_chat_id: str | None = None,
    ) -> list[ExecutionEvent]:
        clauses = ["sequence>?"]
        args: list[Any] = [max(0, int(after_sequence))]
        if entity_kind:
            clauses.append("entity_kind=?")
            args.append(entity_kind)
        if entity_id:
            clauses.append("entity_id=?")
            args.append(entity_id)
        if scope is not None or _operator_chat_id is not None:
            terminal_scope: list[str] = []
            terminal_args: list[Any] = []
            process_scope: list[str] = []
            process_args: list[Any] = []
            self._append_scope_filter(
                terminal_scope, terminal_args, "t.scope_json", scope
            )
            self._append_scope_filter(
                process_scope, process_args, "p.scope_json", scope
            )
            if _operator_chat_id is not None:
                self._append_operator_chat_filter(terminal_scope, terminal_args, _operator_chat_id, "t.")
                self._append_operator_chat_filter(process_scope, process_args, _operator_chat_id, "p.")
            clauses.append(
                "((entity_kind='terminal' AND EXISTS (SELECT 1 FROM "
                "execution_terminal t WHERE t.terminal_id=entity_id AND "
                + " AND ".join(terminal_scope)
                + ")) OR (entity_kind='process' AND EXISTS (SELECT 1 FROM "
                "execution_process p WHERE p.process_id=entity_id AND "
                + " AND ".join(process_scope)
                + ")))"
            )
            args.extend(terminal_args)
            args.extend(process_args)
        args.append(max(1, min(int(limit), 1000)))
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_event WHERE " + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?", args,
            ).fetchall()
        return [ExecutionEvent(
            sequence=int(row["sequence"]), event_id=str(row["event_id"]),
            entity_kind=str(row["entity_kind"]), entity_id=str(row["entity_id"]),
            event_type=str(row["event_type"]), revision=int(row["revision"]),
            payload=_load_object(row["payload_json"], "event.payload"),
            created_at=float(row["created_at"]),
        ) for row in rows]

    def list_events_for_chat(self, chat_id: str, *, after_sequence: int = 0, limit: int = 200):
        return self.list_events(
            after_sequence=after_sequence, limit=limit, _operator_chat_id=chat_id,
        )

    def reconcile_stale_backends(
        self,
        backend_instance_id: str,
        *,
        pid_probe: Any | None = None,
        pid_terminator: Any | None = None,
    ) -> dict[str, list[str]]:
        """Fence records owned by a previous Python backend instance.

        PID observation is disclosure only: pipe/ConPTY handles cannot be
        reconstructed from a PID, so even a matching live PID is never claimed
        as controllable.
        """
        result = {"terminals": [], "processes": []}
        probe = pid_probe or _probe_pid
        terminate = pid_terminator or _terminate_pid_tree
        # Public list APIs are intentionally capped for UI callers. Recovery
        # must scan every stale active owner, including records older than the
        # newest 1000, before any PID can be treated as fenced.
        with self._read() as conn:
            terminal_rows = conn.execute(
                "SELECT * FROM execution_terminal WHERE state IN ("
                + ",".join("?" for _ in ACTIVE_TERMINAL_STATES)
                + ") AND backend_instance_id!=? ORDER BY terminal_id",
                (*sorted(ACTIVE_TERMINAL_STATES), str(backend_instance_id)),
            ).fetchall()
            process_rows = conn.execute(
                "SELECT * FROM execution_process WHERE state IN ("
                + ",".join("?" for _ in ACTIVE_PROCESS_STATES)
                + ") AND backend_instance_id!=? ORDER BY process_id",
                (*sorted(ACTIVE_PROCESS_STATES), str(backend_instance_id)),
            ).fetchall()
        for row in terminal_rows:
            terminal = self._terminal_from_row(row)
            observation = probe(terminal.pid, terminal.pid_started_at)
            terminated = bool(
                observation.get("identity_matches")
                and terminate(terminal.pid, terminal.pid_started_at)
            )
            recovery = {
                "status": "pid_terminated_on_reconcile" if terminated
                else "pid_alive_unattached" if observation.get("identity_matches")
                else "pid_not_observed",
                "process_observation": observation,
                "terminated": terminated,
                "survives_backend_restart": False,
                "controllable": False,
                "reason": (
                    "the Python backend owned this terminal's OS handles; "
                    "a PID alone cannot reconstruct its PTY or streams"
                ),
            }
            self.transition_terminal(
                terminal.terminal_id, "unknown_effect", recovery=recovery,
                event_type="terminal.recovery_unavailable",
            )
            result["terminals"].append(terminal.terminal_id)
        for row in process_rows:
            process = self._process_from_row(row)
            observation = probe(process.pid, process.pid_started_at)
            terminated = bool(
                observation.get("identity_matches")
                and terminate(process.pid, process.pid_started_at)
            )
            recovery = {
                "status": "pid_terminated_on_reconcile" if terminated
                else "pid_alive_unattached" if observation.get("identity_matches")
                else "pid_not_observed",
                "process_observation": observation,
                "terminated": terminated,
                "survives_backend_restart": False,
                "controllable": False,
                "automatic_restart_suppressed": True,
                "reason": (
                    "the prior Python backend owned the process pipes; restart is "
                    "not attempted because the previous effect may still be alive"
                ),
            }
            self.transition_process(
                process.process_id, "unknown_effect", recovery=recovery,
                event_type="process.recovery_unavailable",
            )
            result["processes"].append(process.process_id)
        return result


def _probe_pid(pid: int, started_at: float) -> dict[str, Any]:
    if int(pid or 0) <= 0:
        return {"pid": int(pid or 0), "exists": False, "identity_matches": False}
    try:
        import psutil
        process = psutil.Process(int(pid))
        observed = float(process.create_time())
        matches = not started_at or abs(observed - float(started_at)) < 1.0
        return {
            "pid": int(pid), "exists": bool(process.is_running()),
            "observed_started_at": observed, "identity_matches": bool(matches),
        }
    except Exception as exc:
        return {
            "pid": int(pid), "exists": False, "identity_matches": False,
            "probe_error": type(exc).__name__,
        }


def _terminate_pid_tree(pid: int, started_at: float) -> bool:
    """Terminate only the exact prior process identity, including descendants."""

    if int(pid or 0) <= 0 or float(started_at or 0.0) <= 0:
        return False
    try:
        import psutil

        parent = psutil.Process(int(pid))
        if abs(float(parent.create_time()) - float(started_at)) >= 1.0:
            return False
        processes = [*parent.children(recursive=True), parent]
        for process in reversed(processes):
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
        _, alive = psutil.wait_procs(processes, timeout=5)
        return not alive
    except Exception:
        return False


__all__ = [
    "ExecutionRepository", "default_execution_path", "new_execution_id",
]
