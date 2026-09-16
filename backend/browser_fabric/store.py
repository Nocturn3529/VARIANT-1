"""Transactional SQLite authority for Browser Fabric identity and evidence."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Iterator, Mapping, Sequence

from core_invariants import (
    canonical_json,
    sqlite_read_connection,
    sqlite_unit_of_work,
    sqlite_wal_connection,
    sqlite_writer_lock,
)
from work_fabric.scope import (
    WorkScope,
    append_json_scope_visibility,
    coerce_work_scope,
)

from .models import (
    OPERATION_STATES,
    SESSION_STATES,
    TARGET_STATES,
    BrowserConflict,
    BrowserNotFound,
    BrowserValidationError,
    DownloadRecord,
    EventRecord,
    ObservationRecord,
    OperationRecord,
    ProfileRecord,
    SessionRecord,
    TargetRecord,
    TraceRecord,
    element_records,
    json_value,
)


SCHEMA_VERSION = 3
_SCOPE_KEY_FIELDS = (
    "chat_id", "conversation_id", "branch_id", "workspace_id", "goal_id",
    "goal_run_id", "step_id", "worktree_id",
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def default_browser_fabric_path(*, data_dir: str | None = None) -> str:
    if data_dir:
        data_root = os.path.abspath(data_dir)
    else:
        app_root = os.path.abspath(
            os.environ.get("VARIANT1_DATA_DIR")
            or os.path.join(os.path.dirname(__file__), os.pardir)
        )
        data_root = os.path.join(app_root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_BROWSER_FABRIC_DB")
        or os.path.join(data_root, "browser", "browser-fabric.sqlite3")
    )


def default_profile_root(*, data_dir: str | None = None) -> str:
    if data_dir:
        data_root = os.path.abspath(data_dir)
    else:
        app_root = os.path.abspath(
            os.environ.get("VARIANT1_DATA_DIR")
            or os.path.join(os.path.dirname(__file__), os.pardir)
        )
        data_root = os.path.join(app_root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_BROWSER_PROFILE_ROOT")
        or os.path.join(data_root, "browser", "profiles")
    )


def _json(value: Any) -> str:
    return canonical_json(json_value(value))


def _profile_scope_key(scope: WorkScope | Mapping[str, Any] | None) -> str:
    resolved = coerce_work_scope(scope)
    return _json({
        field: getattr(resolved, field)
        for field in _SCOPE_KEY_FIELDS
        if getattr(resolved, field)
    })


def _load(value: str | None, expected: type, field: str) -> Any:
    try:
        result = json.loads(value or ("{}" if expected is dict else "[]"))
    except Exception as exc:
        raise RuntimeError(f"corrupt browser {field}: {exc}") from exc
    if not isinstance(result, expected):
        raise RuntimeError(f"corrupt browser {field}: expected {expected.__name__}")
    return result


class BrowserFabricStore:
    """Short-connection store with serialized local writers and durable CAS rows."""

    def __init__(self, path: str | None = None, *, data_dir: str | None = None) -> None:
        if path and data_dir:
            raise ValueError("pass either path or data_dir, not both")
        self.path = os.path.abspath(path or default_browser_fabric_path(data_dir=data_dir))
        self._write_lock = sqlite_writer_lock(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_wal_connection(self.path)

    @staticmethod
    def _append_scope_filter(
        clauses: list[str],
        values: list[Any],
        column: str,
        scope: WorkScope | Mapping[str, Any] | None,
    ) -> None:
        append_json_scope_visibility(clauses, values, column, scope)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with sqlite_read_connection(self._connect) as connection:
            yield connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="browser.before_commit"
        ) as connection:
            yield connection

    def _initialize(self) -> None:
        with self._write_lock:
            connection = self._connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS browser_schema_migration (
                        version INTEGER PRIMARY KEY,
                        applied_at REAL NOT NULL,
                        description TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS browser_preference (
                        owner_id TEXT PRIMARY KEY,
                        selection_json TEXT NOT NULL,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS browser_profile (
                        profile_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        persistent INTEGER NOT NULL,
                        user_data_dir TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK(revision > 0),
                        scope_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_error TEXT NOT NULL DEFAULT '',
                        UNIQUE(kind, name, scope_key)
                    );

                    CREATE TABLE IF NOT EXISTS browser_session (
                        session_id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL REFERENCES browser_profile(profile_id),
                        kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        generation INTEGER NOT NULL CHECK(generation > 0),
                        revision INTEGER NOT NULL CHECK(revision > 0),
                        current_target_id TEXT NOT NULL DEFAULT '',
                        headless INTEGER NOT NULL,
                        capabilities_json TEXT NOT NULL,
                        scope_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        closed_at REAL NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_session_state
                        ON browser_session(state, updated_at);

                    CREATE TABLE IF NOT EXISTS browser_target (
                        target_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES browser_session(session_id),
                        backend_target_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        url TEXT NOT NULL DEFAULT '',
                        document_epoch INTEGER NOT NULL CHECK(document_epoch > 0),
                        observation_revision INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL CHECK(revision > 0),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        closed_at REAL NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        UNIQUE(session_id, backend_target_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_target_session_state
                        ON browser_target(session_id, state, updated_at);

                    CREATE TABLE IF NOT EXISTS browser_observation (
                        observation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES browser_session(session_id),
                        target_id TEXT NOT NULL REFERENCES browser_target(target_id),
                        generation INTEGER NOT NULL,
                        document_epoch INTEGER NOT NULL,
                        revision INTEGER NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        url TEXT NOT NULL DEFAULT '',
                        text_excerpt TEXT NOT NULL DEFAULT '',
                        elements_json TEXT NOT NULL,
                        text_artifact_ref TEXT NOT NULL DEFAULT '',
                        html_artifact_ref TEXT NOT NULL DEFAULT '',
                        screenshot_artifact_ref TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        UNIQUE(target_id, revision)
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_observation_target
                        ON browser_observation(target_id, revision DESC);

                    CREATE TABLE IF NOT EXISTS browser_operation (
                        operation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES browser_session(session_id),
                        target_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        idempotency_key TEXT,
                        request_fingerprint TEXT NOT NULL,
                        before_generation INTEGER NOT NULL,
                        before_document_epoch INTEGER NOT NULL,
                        before_observation_revision INTEGER NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        diagnostic TEXT NOT NULL DEFAULT '',
                        scope_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(session_id, idempotency_key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_operation_session
                        ON browser_operation(session_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS browser_event (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL,
                        target_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        scope_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_event_session_sequence
                        ON browser_event(session_id, sequence);

                    CREATE TABLE IF NOT EXISTS browser_download (
                        download_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES browser_session(session_id),
                        target_id TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        suggested_filename TEXT NOT NULL DEFAULT '',
                        url TEXT NOT NULL DEFAULT '',
                        artifact_ref TEXT NOT NULL DEFAULT '',
                        sha256 TEXT NOT NULL DEFAULT '',
                        bytes INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL,
                        revision INTEGER NOT NULL CHECK(revision > 0),
                        scope_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_download_session
                        ON browser_download(session_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS browser_trace (
                        trace_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES browser_session(session_id),
                        name TEXT NOT NULL,
                        state TEXT NOT NULL,
                        artifact_ref TEXT NOT NULL DEFAULT '',
                        sha256 TEXT NOT NULL DEFAULT '',
                        bytes INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL CHECK(revision > 0),
                        scope_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        diagnostic TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_browser_trace_session
                        ON browser_trace(session_id, created_at DESC);
                    """
                )
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(browser_profile)"
                    ).fetchall()
                }
                if "scope_key" not in columns:
                    # Version 1 made profile names process-global even though
                    # every profile carried a WorkScope. Rebuild only this
                    # authority table so identical friendly names can exist in
                    # independent kernel scopes. Session foreign keys continue
                    # to reference the preserved profile IDs.
                    connection.execute("PRAGMA foreign_keys=OFF")
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(
                            """CREATE TABLE browser_profile_scoped (
                                profile_id TEXT PRIMARY KEY,
                                name TEXT NOT NULL,
                                kind TEXT NOT NULL,
                                scope_key TEXT NOT NULL,
                                persistent INTEGER NOT NULL,
                                user_data_dir TEXT NOT NULL DEFAULT '',
                                state TEXT NOT NULL,
                                revision INTEGER NOT NULL CHECK(revision > 0),
                                scope_json TEXT NOT NULL,
                                metadata_json TEXT NOT NULL,
                                created_at REAL NOT NULL,
                                updated_at REAL NOT NULL,
                                last_error TEXT NOT NULL DEFAULT '',
                                UNIQUE(kind, name, scope_key)
                            )"""
                        )
                        rows = connection.execute(
                            "SELECT * FROM browser_profile"
                        ).fetchall()
                        for row in rows:
                            raw_scope = _load(
                                row["scope_json"], dict, "profile scope"
                            )
                            connection.execute(
                                """INSERT INTO browser_profile_scoped(
                                    profile_id,name,kind,scope_key,persistent,
                                    user_data_dir,state,revision,scope_json,
                                    metadata_json,created_at,updated_at,last_error
                                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    row["profile_id"], row["name"], row["kind"],
                                    _profile_scope_key(raw_scope), row["persistent"],
                                    row["user_data_dir"], row["state"],
                                    row["revision"], row["scope_json"],
                                    row["metadata_json"], row["created_at"],
                                    row["updated_at"], row["last_error"],
                                ),
                            )
                        connection.execute("DROP TABLE browser_profile")
                        connection.execute(
                            "ALTER TABLE browser_profile_scoped RENAME TO browser_profile"
                        )
                        connection.execute("COMMIT")
                    except BaseException:
                        connection.execute("ROLLBACK")
                        raise
                    finally:
                        connection.execute("PRAGMA foreign_keys=ON")
                    violations = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    if violations:
                        raise RuntimeError(
                            "browser profile scope migration broke foreign keys"
                        )
                for table in ('browser_target', 'browser_observation'):
                    columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
                    if 'viewport_json' not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN viewport_json TEXT NOT NULL DEFAULT '{{}}'")
                    if table == 'browser_observation' and 'document_json' not in columns:
                        connection.execute("ALTER TABLE browser_observation ADD COLUMN document_json TEXT NOT NULL DEFAULT '{}'")
                connection.execute(
                    "INSERT OR IGNORE INTO browser_schema_migration(version, applied_at, description) VALUES (?, ?, ?)",
                    (SCHEMA_VERSION, time.time(), "durable Browser Fabric authority"),
                )
            finally:
                connection.close()

    @staticmethod
    def _profile(row: sqlite3.Row) -> ProfileRecord:
        return ProfileRecord(
            profile_id=row["profile_id"], name=row["name"], kind=row["kind"],
            persistent=bool(row["persistent"]), user_data_dir=row["user_data_dir"],
            state=row["state"], revision=int(row["revision"]),
            scope=WorkScope.from_mapping(_load(row["scope_json"], dict, "profile scope")),
            metadata=_load(row["metadata_json"], dict, "profile metadata"),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            last_error=row["last_error"],
        )

    def browser_preference(self, owner_id: str) -> dict[str, Any]:
        with self._read() as connection:
            row = connection.execute('SELECT * FROM browser_preference WHERE owner_id=?', (str(owner_id),)).fetchone()
        if row is None:
            return {'selection': None, 'state': {}, 'revision': 0}
        return {'selection': _load(row['selection_json'], dict, 'selection'),
                'state': _load(row['state_json'], dict, 'readiness'), 'revision': int(row['revision'])}

    def update_browser_preference(
        self, owner_id: str, *, selection: Mapping[str, Any] | None = None,
        state: Mapping[str, Any] | None = None, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._write() as connection:
            row = connection.execute('SELECT * FROM browser_preference WHERE owner_id=?', (str(owner_id),)).fetchone()
            revision = int(row['revision']) if row is not None else 0
            if expected_revision is not None and int(expected_revision) != revision:
                raise BrowserConflict('Browser selection changed; refresh its state and retry.')
            chosen = dict(selection) if selection is not None else (_load(row['selection_json'], dict, 'selection') if row else {})
            status = dict(state) if state is not None else (_load(row['state_json'], dict, 'readiness') if row else {})
            revision += 1
            connection.execute('''INSERT INTO browser_preference(owner_id,selection_json,state_json,revision,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET selection_json=excluded.selection_json,
                state_json=excluded.state_json,revision=excluded.revision,updated_at=excluded.updated_at''',
                (str(owner_id), _json(chosen), _json(status), revision, time.time()))
        return {'selection': chosen, 'state': status, 'revision': revision}

    def delete_browser_preference(self, owner_id: str) -> None:
        with self._write() as connection:
            connection.execute('DELETE FROM browser_preference WHERE owner_id=?', (str(owner_id),))

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"], profile_id=row["profile_id"], kind=row["kind"],
            state=row["state"], generation=int(row["generation"]), revision=int(row["revision"]),
            current_target_id=row["current_target_id"], headless=bool(row["headless"]),
            capabilities=tuple(_load(row["capabilities_json"], list, "session capabilities")),
            scope=WorkScope.from_mapping(_load(row["scope_json"], dict, "session scope")),
            metadata=_load(row["metadata_json"], dict, "session metadata"),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            closed_at=float(row["closed_at"]), last_error=row["last_error"],
        )

    @staticmethod
    def _target(row: sqlite3.Row) -> TargetRecord:
        return TargetRecord(
            target_id=row["target_id"], session_id=row["session_id"],
            backend_target_id=row["backend_target_id"], state=row["state"],
            title=row["title"], url=row["url"],
            document_epoch=int(row["document_epoch"]),
            observation_revision=int(row["observation_revision"]),
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]), closed_at=float(row["closed_at"]),
            last_error=row["last_error"],
            viewport=_load(row['viewport_json'], dict, 'target viewport'),
        )

    @staticmethod
    def _observation(row: sqlite3.Row) -> ObservationRecord:
        raw_elements = _load(row["elements_json"], list, "observation elements")
        return ObservationRecord(
            observation_id=row["observation_id"], session_id=row["session_id"],
            target_id=row["target_id"], generation=int(row["generation"]),
            document_epoch=int(row["document_epoch"]), revision=int(row["revision"]),
            title=row["title"], url=row["url"], text_excerpt=row["text_excerpt"],
            elements=element_records(raw_elements),
            text_artifact_ref=row["text_artifact_ref"], html_artifact_ref=row["html_artifact_ref"],
            screenshot_artifact_ref=row["screenshot_artifact_ref"],
            created_at=float(row["created_at"]),
            viewport=_load(row['viewport_json'], dict, 'observation viewport'),
            document=_load(row['document_json'], dict, 'observation document'),
        )

    @staticmethod
    def _operation(row: sqlite3.Row) -> OperationRecord:
        return OperationRecord(
            operation_id=row["operation_id"], session_id=row["session_id"],
            target_id=row["target_id"], kind=row["kind"], state=row["state"],
            idempotency_key=row["idempotency_key"] or "",
            request_fingerprint=row["request_fingerprint"],
            before_generation=int(row["before_generation"]),
            before_document_epoch=int(row["before_document_epoch"]),
            before_observation_revision=int(row["before_observation_revision"]),
            result=_load(row["result_json"], dict, "operation result"),
            diagnostic=row["diagnostic"],
            scope=WorkScope.from_mapping(_load(row["scope_json"], dict, "operation scope")),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            sequence=int(row["sequence"]), event_id=row["event_id"],
            session_id=row["session_id"], target_id=row["target_id"], kind=row["kind"],
            payload=_load(row["payload_json"], dict, "event payload"),
            scope=WorkScope.from_mapping(_load(row["scope_json"], dict, "event scope")),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _download(row: sqlite3.Row) -> DownloadRecord:
        return DownloadRecord(
            download_id=row["download_id"], session_id=row["session_id"],
            target_id=row["target_id"], operation_id=row["operation_id"],
            suggested_filename=row["suggested_filename"], url=row["url"],
            artifact_ref=row["artifact_ref"], sha256=row["sha256"], bytes=int(row["bytes"]),
            state=row["state"], revision=int(row["revision"]),
            scope=WorkScope.from_mapping(_load(row["scope_json"], dict, "download scope")),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _trace(row: sqlite3.Row) -> TraceRecord:
        return TraceRecord(
            trace_id=row["trace_id"], session_id=row["session_id"], name=row["name"],
            state=row["state"], artifact_ref=row["artifact_ref"], sha256=row["sha256"],
            bytes=int(row["bytes"]), revision=int(row["revision"]),
            scope=WorkScope.from_mapping(_load(row["scope_json"], dict, "trace scope")),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            diagnostic=row["diagnostic"],
        )

    def create_profile(
        self,
        *,
        profile_id: str,
        name: str,
        kind: str,
        persistent: bool,
        user_data_dir: str,
        scope: WorkScope,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProfileRecord:
        now = time.time()
        with self._write() as connection:
            try:
                connection.execute(
                    """INSERT INTO browser_profile(
                        profile_id,name,kind,scope_key,persistent,user_data_dir,state,revision,
                        scope_json,metadata_json,created_at,updated_at,last_error
                    ) VALUES(?,?,?,?,?,?,'active',1,?,?,?,?, '')""",
                    (profile_id, name, kind, _profile_scope_key(scope),
                     int(persistent), user_data_dir,
                     _json(scope.to_dict()), _json(metadata or {}), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise BrowserConflict(f"browser profile {name!r} already exists for {kind}") from exc
        return self.get_profile(profile_id)

    def get_profile(self, profile_id: str) -> ProfileRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_profile WHERE profile_id=?", (profile_id,)
            ).fetchone()
        if row is None:
            raise BrowserNotFound(f"browser profile {profile_id!r} does not exist")
        return self._profile(row)

    def find_profile(
        self,
        name: str,
        kind: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> ProfileRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_profile WHERE name=? AND kind=? AND scope_key=?",
                (name, kind, _profile_scope_key(scope)),
            ).fetchone()
        return self._profile(row) if row is not None else None

    def list_profiles(
        self,
        *,
        kind: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> tuple[ProfileRecord, ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        self._append_scope_filter(clauses, values, "scope_json", scope)
        query = "SELECT * FROM browser_profile"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        values.append(max(1, min(int(limit), 1000)))
        with self._read() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(self._profile(row) for row in rows)

    def count_profiles(
        self,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> int:
        clauses: list[str] = []
        values: list[Any] = []
        self._append_scope_filter(clauses, values, "scope_json", scope)
        query = "SELECT COUNT(*) FROM browser_profile"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._read() as connection:
            return int(connection.execute(query, values).fetchone()[0])

    def create_session(
        self,
        *,
        session_id: str,
        profile_id: str,
        kind: str,
        headless: bool,
        capabilities: Sequence[str],
        scope: WorkScope,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        now = time.time()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO browser_session(
                    session_id,profile_id,kind,state,generation,revision,current_target_id,
                    headless,capabilities_json,scope_json,metadata_json,created_at,updated_at,
                    closed_at,last_error
                ) VALUES(?,?,?,'opening',1,1,'',?,?,?,?,?,?,0,'')""",
                (session_id, profile_id, kind, int(headless), _json(list(capabilities)),
                 _json(scope.to_dict()), _json(metadata or {}), now, now),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> SessionRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_session WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise BrowserNotFound(f"browser session {session_id!r} does not exist")
        return self._session(row)

    def find_session_for_owner(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> SessionRecord | None:
        clauses = [
            "state <> 'closed'",
            "COALESCE(json_extract(metadata_json, '$.owner_kind'),'')=?",
            "COALESCE(json_extract(metadata_json, '$.owner_id'),'')=?",
        ]
        values: list[Any] = [str(owner_kind), str(owner_id)]
        self._append_scope_filter(clauses, values, "scope_json", scope)
        query = (
            "SELECT * FROM browser_session WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC, session_id DESC LIMIT 1"
        )
        with self._read() as connection:
            row = connection.execute(query, values).fetchone()
        return self._session(row) if row is not None else None

    def session_ids_for_chat(self, chat_id: str, *, include_closed: bool = False) -> tuple[str, ...]:
        clean = str(chat_id or "").strip()
        if not clean:
            return ()
        with self._read() as connection:
            rows = connection.execute(
                """SELECT session_id FROM browser_session
                   WHERE (state <> 'closed' OR ?)
                     AND COALESCE(json_extract(scope_json, '$.chat_id'),'')=?
                   ORDER BY session_id""",
                (int(include_closed), clean),
            ).fetchall()
        return tuple(str(row["session_id"]) for row in rows)

    def list_sessions(
        self,
        *,
        state: str = "",
        include_closed: bool = False,
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> tuple[SessionRecord, ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if state:
            clauses.append("state=?")
            values.append(state)
        elif not include_closed:
            clauses.append("state <> 'closed'")
        self._append_scope_filter(clauses, values, "scope_json", scope)
        query = "SELECT * FROM browser_session"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        values.append(max(1, min(int(limit), 1000)))
        with self._read() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(self._session(row) for row in rows)

    def count_sessions(
        self,
        *,
        include_closed: bool = False,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> int:
        clauses: list[str] = []
        values: list[Any] = []
        if not include_closed:
            clauses.append("state <> 'closed'")
        self._append_scope_filter(clauses, values, "scope_json", scope)
        query = "SELECT COUNT(*) FROM browser_session"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._read() as connection:
            return int(connection.execute(query, values).fetchone()[0])

    def update_session(
        self,
        session_id: str,
        *,
        state: str | None = None,
        current_target_id: str | None = None,
        capabilities: Sequence[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        last_error: str | None = None,
        bump_generation: bool = False,
        expected_revision: int | None = None,
    ) -> SessionRecord:
        if state is not None and state not in SESSION_STATES:
            raise BrowserValidationError(f"invalid browser session state {state!r}")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM browser_session WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise BrowserNotFound(f"browser session {session_id!r} does not exist")
            if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                raise BrowserConflict("browser session revision changed")
            next_state = state if state is not None else row["state"]
            closed_at = time.time() if next_state == "closed" else float(row["closed_at"])
            connection.execute(
                """UPDATE browser_session SET
                    state=?, generation=?, revision=revision+1, current_target_id=?,
                    capabilities_json=?, metadata_json=?, updated_at=?, closed_at=?, last_error=?
                    WHERE session_id=?""",
                (next_state, int(row["generation"]) + int(bool(bump_generation)),
                 current_target_id if current_target_id is not None else row["current_target_id"],
                 _json(list(capabilities)) if capabilities is not None else row["capabilities_json"],
                 _json(metadata) if metadata is not None else row["metadata_json"],
                 time.time(), closed_at,
                 str(last_error)[:4000] if last_error is not None else row["last_error"],
                 session_id),
            )
        return self.get_session(session_id)

    def create_target(
        self,
        *,
        target_id: str,
        session_id: str,
        backend_target_id: str,
        title: str = "",
        url: str = "",
        state: str = "opening",
    ) -> TargetRecord:
        if state not in TARGET_STATES:
            raise BrowserValidationError(f"invalid browser target state {state!r}")
        now = time.time()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO browser_target(
                    target_id,session_id,backend_target_id,state,title,url,document_epoch,
                    observation_revision,revision,created_at,updated_at,closed_at,last_error
                ) VALUES(?,?,?,?,?,?,1,0,1,?,?,0,'')""",
                (target_id, session_id, backend_target_id, state, str(title)[:2000],
                 str(url)[:16000], now, now),
            )
        return self.get_target(target_id)

    def get_target(self, target_id: str) -> TargetRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_target WHERE target_id=?", (target_id,)
            ).fetchone()
        if row is None:
            raise BrowserNotFound(f"browser target {target_id!r} does not exist")
        return self._target(row)

    def list_targets(
        self, session_id: str, *, include_closed: bool = False,
    ) -> tuple[TargetRecord, ...]:
        query = "SELECT * FROM browser_target WHERE session_id=?"
        values: list[Any] = [session_id]
        if not include_closed:
            query += " AND state <> 'closed'"
        query += " ORDER BY created_at ASC"
        with self._read() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(self._target(row) for row in rows)

    def update_target(
        self,
        target_id: str,
        *,
        backend_target_id: str | None = None,
        state: str | None = None,
        title: str | None = None,
        url: str | None = None,
        navigated: bool = False,
        invalidate_observation: bool = False,
        last_error: str | None = None,
        expected_revision: int | None = None,
        viewport: Mapping[str, Any] | None = None,
    ) -> TargetRecord:
        if state is not None and state not in TARGET_STATES:
            raise BrowserValidationError(f"invalid browser target state {state!r}")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM browser_target WHERE target_id=?", (target_id,)
            ).fetchone()
            if row is None:
                raise BrowserNotFound(f"browser target {target_id!r} does not exist")
            if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                raise BrowserConflict("browser target revision changed")
            next_state = state if state is not None else row["state"]
            closed_at = time.time() if next_state == "closed" else float(row["closed_at"])
            connection.execute(
                """UPDATE browser_target SET backend_target_id=?, state=?, title=?, url=?,
                    document_epoch=document_epoch+?, observation_revision=?, revision=revision+1,
                    updated_at=?, closed_at=?, last_error=?, viewport_json=? WHERE target_id=?""",
                (backend_target_id if backend_target_id is not None else row["backend_target_id"],
                 next_state, str(title)[:2000] if title is not None else row["title"],
                 str(url)[:16000] if url is not None else row["url"], int(bool(navigated)),
                 int(row["observation_revision"])
                 + int(bool(invalidate_observation)),
                 time.time(), closed_at,
                 str(last_error)[:4000] if last_error is not None else row["last_error"],
                 _json(viewport) if viewport is not None else row['viewport_json'],
                 target_id),
            )
        return self.get_target(target_id)

    def append_observation(
        self,
        *,
        session_id: str,
        target_id: str,
        generation: int,
        title: str,
        url: str,
        text_excerpt: str,
        elements: Sequence[Mapping[str, Any]],
        text_artifact_ref: str = "",
        html_artifact_ref: str = "",
        screenshot_artifact_ref: str = "",
        viewport: Mapping[str, Any] | None = None,
        document: Mapping[str, Any] | None = None,
    ) -> ObservationRecord:
        # Validate before acquiring the writer lock so malformed observations
        # cannot partially advance the target's monotonic revision.
        normalized_elements = [item.to_dict() for item in element_records(elements)]
        observation_id = new_id("observation")
        now = time.time()
        with self._write() as connection:
            session = connection.execute(
                "SELECT generation,scope_json FROM browser_session WHERE session_id=?",
                (session_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM browser_target WHERE target_id=? AND session_id=?",
                (target_id, session_id),
            ).fetchone()
            if session is None or target is None:
                raise BrowserNotFound("browser observation target does not exist")
            if int(session["generation"]) != int(generation):
                raise BrowserConflict("browser generation changed while observing")
            revision = int(target["observation_revision"]) + 1
            connection.execute(
                """INSERT INTO browser_observation(
                    observation_id,session_id,target_id,generation,document_epoch,revision,
                    title,url,text_excerpt,elements_json,text_artifact_ref,html_artifact_ref,
                    screenshot_artifact_ref,created_at,viewport_json,document_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (observation_id, session_id, target_id, int(generation),
                 int(target["document_epoch"]), revision, str(title)[:2000], str(url)[:16000],
                 str(text_excerpt)[:65536], _json(normalized_elements), text_artifact_ref,
                 html_artifact_ref, screenshot_artifact_ref, now, _json(viewport or {}), _json(document or {})),
            )
            connection.execute(
                """UPDATE browser_target SET title=?,url=?,observation_revision=?,
                    revision=revision+1,updated_at=?,viewport_json=? WHERE target_id=?""",
                (str(title)[:2000], str(url)[:16000], revision, now,
                 _json(viewport) if viewport else target['viewport_json'], target_id),
            )
            self._append_event_tx(
                connection,
                session_id=session_id,
                target_id=target_id,
                kind="observation.committed",
                payload={
                    "observation_id": observation_id,
                    "revision": revision,
                    "document_epoch": int(target["document_epoch"]),
                },
                scope=WorkScope.from_mapping(
                    _load(session["scope_json"], dict, "session scope")
                ),
                created_at=now,
            )
        return self.get_observation(observation_id)

    def get_observation(self, observation_id: str) -> ObservationRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_observation WHERE observation_id=?", (observation_id,)
            ).fetchone()
        if row is None:
            raise BrowserNotFound(f"browser observation {observation_id!r} does not exist")
        return self._observation(row)

    def latest_observation(self, target_id: str) -> ObservationRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_observation WHERE target_id=? ORDER BY revision DESC LIMIT 1",
                (target_id,),
            ).fetchone()
        return self._observation(row) if row is not None else None

    def observation_at_revision(
        self, target_id: str, revision: int,
    ) -> ObservationRecord | None:
        """Return the durable observation that minted an element reference."""

        with self._read() as connection:
            row = connection.execute(
                """SELECT * FROM browser_observation
                   WHERE target_id=? AND revision=? LIMIT 1""",
                (target_id, int(revision)),
            ).fetchone()
        return self._observation(row) if row is not None else None

    def prepare_operation(
        self,
        *,
        session_id: str,
        target_id: str,
        kind: str,
        idempotency_key: str,
        request_fingerprint: str,
        generation: int,
        document_epoch: int,
        observation_revision: int,
        scope: WorkScope,
    ) -> tuple[OperationRecord, bool]:
        """Insert a prepared operation, or return an exact idempotent replay."""
        key_value = idempotency_key or None
        with self._write() as connection:
            if key_value:
                row = connection.execute(
                    "SELECT * FROM browser_operation WHERE session_id=? AND idempotency_key=?",
                    (session_id, key_value),
                ).fetchone()
                if row is not None:
                    existing = self._operation(row)
                    if existing.request_fingerprint != request_fingerprint:
                        raise BrowserConflict("idempotency key was used for a different browser request")
                    return existing, True
            now = time.time()
            operation_id = new_id("browserop")
            connection.execute(
                """INSERT INTO browser_operation(
                    operation_id,session_id,target_id,kind,state,idempotency_key,
                    request_fingerprint,before_generation,before_document_epoch,
                    before_observation_revision,result_json,diagnostic,scope_json,created_at,updated_at
                ) VALUES(?,?,?,?,'prepared',?,?,?,?,?,'{}','',?,?,?)""",
                (operation_id, session_id, target_id, kind, key_value, request_fingerprint,
                 generation, document_epoch, observation_revision, _json(scope.to_dict()), now, now),
            )
            row = connection.execute(
                "SELECT * FROM browser_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            assert row is not None
            return self._operation(row), False

    def get_operation(self, operation_id: str) -> OperationRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise BrowserNotFound(f"browser operation {operation_id!r} does not exist")
        return self._operation(row)

    def latest_navigation(self, session_id: str, target_id: str) -> OperationRecord | None:
        """Exact latest document-changing attempt, including failed attempts."""
        with self._read() as connection:
            row = connection.execute(
                """SELECT * FROM browser_operation WHERE session_id=?
                AND kind IN ('new_page','navigate','back','forward','reload')
                AND (target_id=? OR (kind='new_page' AND json_extract(result_json,'$.target_id')=?))
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (session_id, target_id, target_id),
            ).fetchone()
        return self._operation(row) if row is not None else None

    def find_operation(
        self, session_id: str, idempotency_key: str,
    ) -> OperationRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_operation WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
        return self._operation(row) if row is not None else None

    def transition_operation(
        self,
        operation_id: str,
        state: str,
        *,
        result: Mapping[str, Any] | None = None,
        diagnostic: str = "",
    ) -> OperationRecord:
        if state not in OPERATION_STATES:
            raise BrowserValidationError(f"invalid browser operation state {state!r}")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM browser_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise BrowserNotFound(f"browser operation {operation_id!r} does not exist")
            previous = str(row["state"])
            legal = {
                "prepared": {"dispatched", "failed"},
                "dispatched": {"observed", "failed", "unknown_effect"},
                "observed": {"committed", "failed", "unknown_effect"},
            }
            if state not in legal.get(previous, set()):
                raise BrowserConflict(
                    f"illegal browser operation transition: {previous} -> {state}"
                )
            now = time.time()
            connection.execute(
                """UPDATE browser_operation SET state=?,result_json=?,diagnostic=?,updated_at=?
                   WHERE operation_id=?""",
                (state, _json(result) if result is not None else row["result_json"],
                 str(diagnostic)[:8000], now, operation_id),
            )
            if state in {"committed", "failed", "unknown_effect"}:
                self._append_event_tx(
                    connection,
                    session_id=str(row["session_id"]),
                    target_id=str(row["target_id"]),
                    kind=f"operation.{state}",
                    payload={
                        "operation_id": operation_id,
                        "kind": str(row["kind"]),
                        "previous_state": previous,
                        "result": dict(result or {}),
                        "diagnostic": str(diagnostic)[:4000],
                    },
                    scope=WorkScope.from_mapping(
                        _load(row["scope_json"], dict, "operation scope")
                    ),
                    created_at=now,
                )
        return self.get_operation(operation_id)

    def list_operations(self, session_id: str, *, limit: int = 200) -> tuple[OperationRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_operation WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return tuple(self._operation(row) for row in rows)

    def reconcile_interrupted_operations(self) -> dict[str, int]:
        """Classify crash-window ledger rows without replaying browser effects."""

        now = time.time()
        counts = {"failed_before_dispatch": 0, "unknown_effect": 0}
        with self._write() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_operation "
                "WHERE state IN ('prepared','dispatched','observed') "
                "ORDER BY operation_id"
            ).fetchall()
            for row in rows:
                previous = str(row["state"])
                state = "failed" if previous == "prepared" else "unknown_effect"
                diagnostic = (
                    "browser host restarted before adapter dispatch"
                    if previous == "prepared"
                    else "browser host restarted after adapter dispatch; observe before retry"
                )
                changed = connection.execute(
                    "UPDATE browser_operation SET state=?,diagnostic=?,updated_at=? "
                    "WHERE operation_id=? AND state=?",
                    (state, diagnostic, now, str(row["operation_id"]), previous),
                )
                if changed.rowcount != 1:
                    raise BrowserConflict("browser recovery lost its operation state fence")
                self._append_event_tx(
                    connection,
                    session_id=str(row["session_id"]),
                    target_id=str(row["target_id"]),
                    kind=f"operation.{state}",
                    payload={
                        "operation_id": str(row["operation_id"]),
                        "kind": str(row["kind"]),
                        "previous_state": previous,
                        "diagnostic": diagnostic,
                        "recovery": True,
                    },
                    scope=WorkScope.from_mapping(
                        _load(row["scope_json"], dict, "operation scope")
                    ),
                    created_at=now,
                )
                counts[
                    "failed_before_dispatch"
                    if state == "failed" else "unknown_effect"
                ] += 1
        return counts

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        target_id: str,
        kind: str,
        payload: Mapping[str, Any],
        scope: WorkScope,
        created_at: float | None = None,
    ) -> EventRecord:
        event_id = new_id("browserevent")
        cursor = connection.execute(
            """INSERT INTO browser_event(
                event_id,session_id,target_id,kind,payload_json,scope_json,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (event_id, session_id, target_id, kind, _json(payload),
             _json(scope.to_dict()), float(created_at or time.time())),
        )
        sequence = int(cursor.lastrowid)
        row = connection.execute(
            "SELECT * FROM browser_event WHERE sequence=?", (sequence,)
        ).fetchone()
        assert row is not None
        return self._event(row)

    def append_event(
        self,
        *,
        session_id: str,
        target_id: str,
        kind: str,
        payload: Mapping[str, Any],
        scope: WorkScope,
    ) -> EventRecord:
        with self._write() as connection:
            return self._append_event_tx(
                connection,
                session_id=session_id,
                target_id=target_id,
                kind=kind,
                payload=payload,
                scope=scope,
            )

    def events(
        self, session_id: str, *, after_sequence: int = 0, limit: int = 200,
    ) -> tuple[EventRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                """SELECT * FROM browser_event WHERE session_id=? AND sequence>?
                   ORDER BY sequence ASC LIMIT ?""",
                (session_id, max(0, int(after_sequence)), max(1, min(int(limit), 2000))),
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def record_download(
        self,
        *,
        session_id: str,
        target_id: str,
        operation_id: str,
        suggested_filename: str,
        url: str,
        artifact_ref: str,
        sha256: str,
        bytes_count: int,
        scope: WorkScope,
        state: str = "completed",
        download_id: str = "",
    ) -> DownloadRecord:
        download_id = download_id or new_id("download")
        now = time.time()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM browser_download WHERE download_id=?", (download_id,)
            ).fetchone()
            if existing is not None:
                record = self._download(existing)
                if (record.session_id, record.target_id, record.artifact_ref,
                    record.sha256, record.bytes, record.scope) != (
                    session_id, target_id, artifact_ref, sha256, int(bytes_count), scope
                ):
                    raise BrowserConflict("download ID already belongs to different content or scope")
                return record
            connection.execute(
                """INSERT INTO browser_download(
                    download_id,session_id,target_id,operation_id,suggested_filename,url,
                    artifact_ref,sha256,bytes,state,revision,scope_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (download_id, session_id, target_id, operation_id,
                 str(suggested_filename)[:1000], str(url)[:16000], artifact_ref, sha256,
                 max(0, int(bytes_count)), state, _json(scope.to_dict()), now, now),
            )
            self._append_event_tx(
                connection,
                session_id=session_id,
                target_id=target_id,
                kind="download.completed",
                payload={
                    "download_id": download_id,
                    "operation_id": operation_id,
                    "artifact_ref": artifact_ref,
                    "sha256": sha256,
                    "bytes": max(0, int(bytes_count)),
                    "state": state,
                },
                scope=scope,
                created_at=now,
            )
        return self.get_download(download_id)

    def get_download(self, download_id: str) -> DownloadRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_download WHERE download_id=?", (download_id,)
            ).fetchone()
        if row is None:
            raise BrowserNotFound(f"browser download {download_id!r} does not exist")
        return self._download(row)

    def list_downloads(self, session_id: str, *, limit: int = 100, operation_id: str = "") -> tuple[DownloadRecord, ...]:
        query = "SELECT * FROM browser_download WHERE session_id=?"
        params: list[Any] = [session_id]
        if operation_id:
            query += " AND operation_id=?"
            params.append(operation_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._read() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._download(row) for row in rows)

    def create_trace(self, *, session_id: str, name: str, scope: WorkScope) -> TraceRecord:
        trace_id = new_id("trace")
        now = time.time()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO browser_trace(
                    trace_id,session_id,name,state,artifact_ref,sha256,bytes,revision,
                    scope_json,created_at,updated_at,diagnostic
                ) VALUES(?,?,?,'prepared','','',0,1,?,?,?,'')""",
                (trace_id, session_id, str(name)[:500], _json(scope.to_dict()), now, now),
            )
        return self.get_trace(trace_id)

    def mark_trace_recording(self, trace_id: str) -> TraceRecord:
        now = time.time()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM browser_trace WHERE trace_id=?", (trace_id,)
            ).fetchone()
            if row is None:
                raise BrowserNotFound(f"browser trace {trace_id!r} does not exist")
            if str(row["state"]) != "prepared":
                raise BrowserConflict("browser trace is not prepared")
            connection.execute(
                "UPDATE browser_trace SET state='recording',revision=revision+1,"
                "updated_at=? WHERE trace_id=? AND state='prepared'",
                (now, trace_id),
            )
            self._append_event_tx(
                connection,
                session_id=str(row["session_id"]),
                target_id="",
                kind="trace.started",
                payload={"trace_id": trace_id},
                scope=WorkScope.from_mapping(
                    _load(row["scope_json"], dict, "trace scope")
                ),
                created_at=now,
            )
        return self.get_trace(trace_id)

    def get_trace(self, trace_id: str) -> TraceRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM browser_trace WHERE trace_id=?", (trace_id,)
            ).fetchone()
        if row is None:
            raise BrowserNotFound(f"browser trace {trace_id!r} does not exist")
        return self._trace(row)

    def finish_trace(
        self,
        trace_id: str,
        *,
        state: str,
        artifact_ref: str = "",
        sha256: str = "",
        bytes_count: int = 0,
        diagnostic: str = "",
    ) -> TraceRecord:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM browser_trace WHERE trace_id=?", (trace_id,)
            ).fetchone()
            if row is None:
                raise BrowserNotFound(f"browser trace {trace_id!r} does not exist")
            legal = {
                "prepared": {"failed"},
                "recording": {"completed", "failed"},
            }
            if state not in legal.get(str(row["state"]), set()):
                raise BrowserConflict(
                    f"illegal browser trace transition: {row['state']} -> {state}"
                )
            now = time.time()
            cursor = connection.execute(
                """UPDATE browser_trace SET state=?,artifact_ref=?,sha256=?,bytes=?,
                    revision=revision+1,updated_at=?,diagnostic=? WHERE trace_id=?""",
                (state, artifact_ref, sha256, max(0, int(bytes_count)), now,
                 str(diagnostic)[:8000], trace_id),
            )
            if cursor.rowcount != 1:
                raise BrowserNotFound(f"browser trace {trace_id!r} does not exist")
            self._append_event_tx(
                connection,
                session_id=str(row["session_id"]),
                target_id="",
                kind=f"trace.{state}",
                payload={
                    "trace_id": trace_id,
                    "artifact_ref": artifact_ref,
                    "sha256": sha256,
                    "bytes": max(0, int(bytes_count)),
                    "diagnostic": str(diagnostic)[:4000],
                },
                scope=WorkScope.from_mapping(
                    _load(row["scope_json"], dict, "trace scope")
                ),
                created_at=now,
            )
        return self.get_trace(trace_id)

    def list_traces(self, session_id: str, *, limit: int = 100) -> tuple[TraceRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_trace WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return tuple(self._trace(row) for row in rows)

    def reconcile_recording_traces(self) -> int:
        """Orphan every trace interrupted by host restart without a capped scan."""

        now = time.time()
        count = 0
        with self._write() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_trace WHERE state IN ('prepared','recording') "
                "ORDER BY trace_id"
            ).fetchall()
            for row in rows:
                previous = str(row["state"])
                state = "failed" if previous == "prepared" else "orphaned"
                diagnostic = (
                    "browser host restarted before trace recording began"
                    if previous == "prepared"
                    else "browser host restarted while trace was recording"
                )
                changed = connection.execute(
                    "UPDATE browser_trace SET state=?,revision=revision+1,"
                    "updated_at=?,diagnostic=? WHERE trace_id=? AND state=?",
                    (state, now, diagnostic, str(row["trace_id"]), previous),
                )
                if changed.rowcount != 1:
                    raise BrowserConflict("browser trace recovery lost its state fence")
                self._append_event_tx(
                    connection,
                    session_id=str(row["session_id"]),
                    target_id="",
                    kind=f"trace.{state}",
                    payload={
                        "trace_id": str(row["trace_id"]),
                        "previous_state": previous,
                        "diagnostic": diagnostic,
                        "recovery": True,
                    },
                    scope=WorkScope.from_mapping(
                        _load(row["scope_json"], dict, "trace scope")
                    ),
                    created_at=now,
                )
                count += 1
        return count


__all__ = [
    "BrowserFabricStore", "default_browser_fabric_path", "default_profile_root", "new_id",
]
