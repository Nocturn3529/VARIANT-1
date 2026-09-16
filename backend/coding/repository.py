"""Transactional SQLite authority for coding repositories and reviews."""

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
    CheckRun,
    CodingConflict,
    RepositoryNotFound,
    RepositoryRecord,
    ReviewFile,
    ReviewFinding,
    ReviewNotFound,
    ReviewRecord,
    WorktreeNotFound,
    WorktreeRecord,
    json_value,
)


SCHEMA_VERSION = 1
def default_coding_path(*, data_dir: str | None = None) -> str:
    if data_dir:
        data_root = os.path.abspath(data_dir)
    else:
        app_root = os.path.abspath(
            os.environ.get("VARIANT1_DATA_DIR")
            or os.path.join(os.path.dirname(__file__), os.pardir)
        )
        data_root = os.path.join(app_root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_CODING_DB")
        or os.path.join(data_root, "coding", "coding.sqlite3")
    )


def default_managed_root(*, data_dir: str | None = None) -> str:
    if data_dir:
        data_root = os.path.abspath(data_dir)
    else:
        app_root = os.path.abspath(
            os.environ.get("VARIANT1_DATA_DIR")
            or os.path.join(os.path.dirname(__file__), os.pardir)
        )
        data_root = os.path.join(app_root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_CODING_WORKTREE_ROOT")
        or os.path.join(data_root, "coding", "worktrees")
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return canonical_json(json_value(value))


def _load(value: str | None, expected: type, field: str) -> Any:
    try:
        result = json.loads(value or ("{}" if expected is dict else "[]"))
    except Exception as exc:
        raise RuntimeError(f"corrupt coding {field}: {exc}") from exc
    if not isinstance(result, expected):
        raise RuntimeError(f"corrupt coding {field}: expected {expected.__name__}")
    return result


def path_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


class CodingRepository:
    """Short-connection SQLite store with serialized local writers."""

    def __init__(self, path: str | None = None, *, data_dir: str | None = None) -> None:
        if path and data_dir:
            raise ValueError("pass either path or data_dir, not both")
        self.path = os.path.abspath(path or default_coding_path(data_dir=data_dir))
        self._write_lock = sqlite_writer_lock(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_wal_connection(self.path)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with sqlite_read_connection(self._connect) as connection:
            yield connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="coding.before_commit"
        ) as connection:
            yield connection

    def _initialize(self) -> None:
        # sqlite3.executescript manages its own transaction boundary even when
        # isolation_level=None, so schema creation cannot run inside _write().
        with self._write_lock:
            connection = self._connect()
            try:
                connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS coding_schema_migration (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    description TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS coding_repository (
                    repository_id TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    root_key TEXT NOT NULL,
                    git_dir TEXT NOT NULL,
                    common_dir TEXT NOT NULL,
                    common_key TEXT NOT NULL,
                    object_format TEXT NOT NULL,
                    default_branch TEXT NOT NULL DEFAULT '',
                    head_oid TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL DEFAULT '',
                    remotes_json TEXT NOT NULL DEFAULT '[]',
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(common_key, object_format)
                );

                CREATE TABLE IF NOT EXISTS coding_worktree (
                    worktree_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL REFERENCES coding_repository(repository_id),
                    root TEXT NOT NULL,
                    root_key TEXT NOT NULL UNIQUE,
                    branch TEXT NOT NULL DEFAULT '',
                    base_ref TEXT NOT NULL,
                    base_oid TEXT NOT NULL,
                    head_oid TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL DEFAULT 'coding',
                    state TEXT NOT NULL,
                    dirty INTEGER NOT NULL DEFAULT 0,
                    conflicted INTEGER NOT NULL DEFAULT 0,
                    scope_json TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at REAL NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    retired_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_coding_worktree_repo_state
                    ON coding_worktree(repository_id, state, updated_at);

                CREATE TABLE IF NOT EXISTS coding_operation (
                    operation_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL REFERENCES coding_repository(repository_id),
                    worktree_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    before_oid TEXT NOT NULL DEFAULT '',
                    after_oid TEXT NOT NULL DEFAULT '',
                    scope_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    diagnostic TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(repository_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS coding_review (
                    review_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL REFERENCES coding_repository(repository_id),
                    worktree_id TEXT NOT NULL DEFAULT '',
                    root TEXT NOT NULL,
                    target TEXT NOT NULL,
                    base_ref TEXT NOT NULL DEFAULT '',
                    base_oid TEXT NOT NULL DEFAULT '',
                    head_ref TEXT NOT NULL DEFAULT '',
                    head_oid TEXT NOT NULL DEFAULT '',
                    status_fingerprint TEXT NOT NULL,
                    patch_ref TEXT NOT NULL DEFAULT '',
                    patch_sha256 TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    approved_head_oid TEXT NOT NULL DEFAULT '',
                    scope_json TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_coding_review_repo_time
                    ON coding_review(repository_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS coding_review_file (
                    review_id TEXT NOT NULL REFERENCES coding_review(review_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    original_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    old_mode TEXT NOT NULL DEFAULT '',
                    new_mode TEXT NOT NULL DEFAULT '',
                    old_oid TEXT NOT NULL DEFAULT '',
                    new_oid TEXT NOT NULL DEFAULT '',
                    additions INTEGER,
                    deletions INTEGER,
                    binary INTEGER NOT NULL DEFAULT 0,
                    patch_ref TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(review_id, position),
                    UNIQUE(review_id, path)
                );

                CREATE TABLE IF NOT EXISTS coding_review_finding (
                    finding_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL REFERENCES coding_review(review_id),
                    path TEXT NOT NULL,
                    line INTEGER NOT NULL DEFAULT 0,
                    side TEXT NOT NULL DEFAULT 'new',
                    severity TEXT NOT NULL DEFAULT 'note',
                    title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    scope_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS coding_check_run (
                    check_run_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL REFERENCES coding_review(review_id),
                    recipe TEXT NOT NULL,
                    argv_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    exit_code INTEGER,
                    head_oid TEXT NOT NULL,
                    status_fingerprint TEXT NOT NULL,
                    log_ref TEXT NOT NULL DEFAULT '',
                    log_sha256 TEXT NOT NULL DEFAULT '',
                    duration_ms REAL NOT NULL DEFAULT 0,
                    scope_json TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    created_at REAL NOT NULL,
                    started_at REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL DEFAULT 0,
                    diagnostic TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS coding_event (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    aggregate_kind TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    aggregate_revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS trg_coding_review_evidence_immutable
                BEFORE UPDATE OF
                    repository_id, worktree_id, root, target, base_ref, base_oid,
                    head_ref, head_oid, status_fingerprint, patch_ref, patch_sha256,
                    summary_json, scope_json, created_at
                ON coding_review
                BEGIN
                    SELECT RAISE(ABORT, 'coding review evidence is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_coding_review_file_update_immutable
                BEFORE UPDATE ON coding_review_file
                BEGIN
                    SELECT RAISE(ABORT, 'coding review files are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_coding_review_file_delete_immutable
                BEFORE DELETE ON coding_review_file
                BEGIN
                    SELECT RAISE(ABORT, 'coding review files are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_coding_event_update_immutable
                BEFORE UPDATE ON coding_event
                BEGIN
                    SELECT RAISE(ABORT, 'coding events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_coding_event_delete_immutable
                BEFORE DELETE ON coding_event
                BEGIN
                    SELECT RAISE(ABORT, 'coding events are append-only');
                END;
                """
            )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO coding_schema_migration(version, applied_at, description)
                    VALUES (?, ?, ?)
                    """,
                    (SCHEMA_VERSION, time.time(), "coding repositories, worktrees, reviews, checks"),
                )
            finally:
                connection.close()

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        kind: str,
        aggregate_id: str,
        revision: int,
        event_type: str,
        scope: WorkScope,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO coding_event(
                sequence, event_id, aggregate_kind, aggregate_id,
                aggregate_revision, event_type, scope_json, payload_json, created_at
            ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("cevt"), kind, aggregate_id, int(revision), event_type,
                _json(scope.to_dict()), _json(dict(payload or {})), time.time(),
            ),
        )

    @staticmethod
    def _repository_from_row(row: sqlite3.Row) -> RepositoryRecord:
        return RepositoryRecord(
            repository_id=str(row["repository_id"]),
            root=str(row["root"]),
            git_dir=str(row["git_dir"]),
            common_dir=str(row["common_dir"]),
            object_format=str(row["object_format"]),
            default_branch=str(row["default_branch"]),
            head_oid=str(row["head_oid"]),
            branch=str(row["branch"]),
            remotes=tuple(_load(row["remotes_json"], list, "repository.remotes")),
            scope=coerce_work_scope(_load(row["scope_json"], dict, "repository.scope")),
            revision=int(row["revision"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_error=str(row["last_error"]),
        )

    def upsert_repository(
        self,
        *,
        repository_id: str,
        root: str,
        git_dir: str,
        common_dir: str,
        object_format: str,
        default_branch: str,
        head_oid: str,
        branch: str,
        remotes: Sequence[Mapping[str, Any]],
        scope: WorkScope,
    ) -> RepositoryRecord:
        now = time.time()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM coding_repository WHERE repository_id=?",
                (repository_id,),
            ).fetchone()
            if existing is None:
                revision = 1
                connection.execute(
                    """
                    INSERT INTO coding_repository(
                        repository_id, root, root_key, git_dir, common_dir, common_key,
                        object_format, default_branch, head_oid, branch, remotes_json,
                        scope_json, revision, created_at, updated_at, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        repository_id, root, path_key(root), git_dir, common_dir,
                        path_key(common_dir), object_format, default_branch, head_oid,
                        branch, _json(list(remotes)), _json(scope.to_dict()), revision,
                        now, now,
                    ),
                )
                event_type = "coding.repository.discovered"
            else:
                revision = int(existing["revision"]) + 1
                connection.execute(
                    """
                    UPDATE coding_repository SET
                        root=?, root_key=?, git_dir=?, common_dir=?, common_key=?,
                        object_format=?, default_branch=?, head_oid=?, branch=?,
                        remotes_json=?, scope_json=?, revision=?, updated_at=?, last_error=''
                    WHERE repository_id=?
                    """,
                    (
                        root, path_key(root), git_dir, common_dir, path_key(common_dir),
                        object_format, default_branch, head_oid, branch,
                        _json(list(remotes)), _json(scope.to_dict()), revision, now,
                        repository_id,
                    ),
                )
                event_type = "coding.repository.refreshed"
            self._event(
                connection, kind="repository", aggregate_id=repository_id,
                revision=revision, event_type=event_type, scope=scope,
                payload={"head_oid": head_oid, "branch": branch},
            )
            row = connection.execute(
                "SELECT * FROM coding_repository WHERE repository_id=?", (repository_id,)
            ).fetchone()
        assert row is not None
        return self._repository_from_row(row)

    def get_repository(self, repository_id: str) -> RepositoryRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM coding_repository WHERE repository_id=?", (repository_id,)
            ).fetchone()
        if row is None:
            raise RepositoryNotFound(repository_id)
        return self._repository_from_row(row)

    def get_repository_by_common(self, common_dir: str, object_format: str) -> RepositoryRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM coding_repository WHERE common_key=? AND object_format=?",
                (path_key(common_dir), object_format),
            ).fetchone()
        return self._repository_from_row(row) if row is not None else None

    def list_repositories(self) -> tuple[RepositoryRecord, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM coding_repository ORDER BY updated_at DESC, repository_id"
            ).fetchall()
        return tuple(self._repository_from_row(row) for row in rows)

    @staticmethod
    def _worktree_from_row(row: sqlite3.Row) -> WorktreeRecord:
        return WorktreeRecord(
            worktree_id=str(row["worktree_id"]),
            repository_id=str(row["repository_id"]),
            root=str(row["root"]),
            branch=str(row["branch"]),
            base_ref=str(row["base_ref"]),
            base_oid=str(row["base_oid"]),
            head_oid=str(row["head_oid"]),
            purpose=str(row["purpose"]),
            state=str(row["state"]),
            dirty=bool(row["dirty"]),
            conflicted=bool(row["conflicted"]),
            scope=coerce_work_scope(_load(row["scope_json"], dict, "worktree.scope")),
            lease_owner=str(row["lease_owner"]),
            lease_expires_at=float(row["lease_expires_at"]),
            revision=int(row["revision"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            retired_at=float(row["retired_at"]),
            last_error=str(row["last_error"]),
        )

    def create_worktree_reservation(
        self,
        *,
        worktree_id: str,
        repository_id: str,
        root: str,
        branch: str,
        base_ref: str,
        base_oid: str,
        purpose: str,
        scope: WorkScope,
        lease_owner: str = "",
        lease_expires_at: float = 0.0,
    ) -> WorktreeRecord:
        now = time.time()
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO coding_worktree(
                    worktree_id, repository_id, root, root_key, branch, base_ref,
                    base_oid, head_oid, purpose, state, dirty, conflicted,
                    scope_json, lease_owner, lease_expires_at, revision,
                    created_at, updated_at, retired_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, 'creating', 0, 0, ?, ?, ?, 1, ?, ?, 0, '')
                """,
                (
                    worktree_id, repository_id, root, path_key(root), branch, base_ref,
                    base_oid, purpose, _json(scope.to_dict()), lease_owner,
                    float(lease_expires_at), now, now,
                ),
            )
            self._event(
                connection, kind="worktree", aggregate_id=worktree_id, revision=1,
                event_type="coding.worktree.creating", scope=scope,
                payload={"repository_id": repository_id, "root": root, "base_oid": base_oid},
            )
            row = connection.execute(
                "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
            ).fetchone()
        assert row is not None
        return self._worktree_from_row(row)

    def transition_worktree(
        self,
        worktree_id: str,
        *,
        expected_revision: int,
        allowed_states: Sequence[str],
        state: str,
        head_oid: str | None = None,
        branch: str | None = None,
        dirty: bool | None = None,
        conflicted: bool | None = None,
        last_error: str = "",
        retired: bool = False,
    ) -> WorktreeRecord:
        with self._write() as connection:
            current = connection.execute(
                "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
            ).fetchone()
            if current is None:
                raise WorktreeNotFound(worktree_id)
            if int(current["revision"]) != int(expected_revision):
                raise CodingConflict("worktree revision changed")
            if str(current["state"]) not in set(allowed_states):
                raise CodingConflict(
                    f"cannot transition worktree from {current['state']} to {state}"
                )
            revision = int(current["revision"]) + 1
            now = time.time()
            connection.execute(
                """
                UPDATE coding_worktree SET
                    state=?, head_oid=?, branch=?, dirty=?, conflicted=?,
                    revision=?, updated_at=?, retired_at=?, last_error=?
                WHERE worktree_id=? AND revision=?
                """,
                (
                    state,
                    str(current["head_oid"] if head_oid is None else head_oid),
                    str(current["branch"] if branch is None else branch),
                    int(current["dirty"] if dirty is None else bool(dirty)),
                    int(current["conflicted"] if conflicted is None else bool(conflicted)),
                    revision, now, (now if retired else float(current["retired_at"])),
                    str(last_error or ""), worktree_id, expected_revision,
                ),
            )
            scope = coerce_work_scope(_load(current["scope_json"], dict, "worktree.scope"))
            self._event(
                connection, kind="worktree", aggregate_id=worktree_id,
                revision=revision, event_type=f"coding.worktree.{state}", scope=scope,
                payload={"head_oid": head_oid or current["head_oid"], "error": last_error},
            )
            row = connection.execute(
                "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
            ).fetchone()
        assert row is not None
        return self._worktree_from_row(row)

    def activate_worktree(
        self,
        worktree_id: str,
        *,
        expected_revision: int,
        allowed_states: Sequence[str],
        state: str,
        head_oid: str,
        branch: str,
        dirty: bool,
        conflicted: bool,
        scope: WorkScope,
    ) -> WorktreeRecord:
        """Atomically activate a physical worktree reservation."""

        now = time.time()
        try:
            with self._write() as connection:
                current = connection.execute(
                    "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
                ).fetchone()
                if current is None:
                    raise WorktreeNotFound(worktree_id)
                if int(current["revision"]) != int(expected_revision):
                    raise CodingConflict("worktree revision changed")
                if str(current["state"]) not in set(allowed_states):
                    raise CodingConflict("worktree is not in an activatable state")
                revision = int(current["revision"]) + 1
                connection.execute(
                    """
                    UPDATE coding_worktree SET state=?, head_oid=?, branch=?, dirty=?,
                        conflicted=?, revision=?, updated_at=?, last_error=''
                    WHERE worktree_id=? AND revision=?
                    """,
                    (
                        state, head_oid, branch, int(dirty), int(conflicted),
                        revision, now, worktree_id, expected_revision,
                    ),
                )
                self._event(
                    connection, kind="worktree", aggregate_id=worktree_id,
                    revision=revision, event_type=f"coding.worktree.{state}", scope=scope,
                    payload={"head_oid": head_oid, "recovered": expected_revision > 1},
                )
                row = connection.execute(
                    "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise CodingConflict("worktree activation conflicts with persisted state") from exc
        assert row is not None
        return self._worktree_from_row(row)

    def retire_worktree(
        self,
        worktree_id: str,
        *,
        expected_revision: int,
        scope: WorkScope,
    ) -> WorktreeRecord:
        """Atomically tombstone a removed physical worktree."""

        now = time.time()
        with self._write() as connection:
            current = connection.execute(
                "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
            ).fetchone()
            if current is None:
                raise WorktreeNotFound(worktree_id)
            if int(current["revision"]) != int(expected_revision):
                raise CodingConflict("worktree revision changed")
            if str(current["state"]) != "retiring":
                raise CodingConflict("only a retiring worktree can become retired")
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                UPDATE coding_worktree SET state='retired', revision=?, updated_at=?,
                    retired_at=?, dirty=0, conflicted=0, last_error=''
                WHERE worktree_id=? AND revision=?
                """,
                (revision, now, now, worktree_id, expected_revision),
            )
            self._event(
                connection, kind="worktree", aggregate_id=worktree_id,
                revision=revision, event_type="coding.worktree.retired", scope=scope,
                payload={},
            )
            row = connection.execute(
                "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
            ).fetchone()
        assert row is not None
        return self._worktree_from_row(row)

    def get_worktree(self, worktree_id: str) -> WorktreeRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM coding_worktree WHERE worktree_id=?", (worktree_id,)
            ).fetchone()
        if row is None:
            raise WorktreeNotFound(worktree_id)
        return self._worktree_from_row(row)

    def list_worktrees(
        self,
        *,
        repository_id: str = "",
        include_retired: bool = False,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> tuple[WorktreeRecord, ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if repository_id:
            clauses.append("repository_id=?")
            values.append(repository_id)
        if not include_retired:
            clauses.append("state <> 'retired'")
        if scope is not None:
            append_json_scope_visibility(
                clauses, values, "scope_json", scope,
                omit_fields=("worktree_id",),
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM coding_worktree" + where + " ORDER BY created_at DESC",
                values,
            ).fetchall()
        return tuple(self._worktree_from_row(row) for row in rows)

    def reserve_operation(
        self,
        *,
        repository_id: str,
        worktree_id: str,
        kind: str,
        idempotency_key: str,
        request_fingerprint: str,
        before_oid: str,
        scope: WorkScope,
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        key: str | None = str(idempotency_key or "").strip() or None
        with self._write() as connection:
            if key is not None:
                prior = connection.execute(
                    "SELECT * FROM coding_operation WHERE repository_id=? AND idempotency_key=?",
                    (repository_id, key),
                ).fetchone()
                if prior is not None:
                    if str(prior["request_fingerprint"]) != request_fingerprint:
                        raise CodingConflict("idempotency key was used for a different operation")
                    return self._operation_dict(prior), True
            operation_id = new_id("cop")
            connection.execute(
                """
                INSERT INTO coding_operation(
                    operation_id, repository_id, worktree_id, kind, idempotency_key,
                    request_fingerprint, state, before_oid, after_oid, scope_json,
                    result_json, diagnostic, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, '', ?, '{}', '', ?, ?)
                """,
                (
                    operation_id, repository_id, worktree_id, kind, key,
                    request_fingerprint, before_oid, _json(scope.to_dict()), now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM coding_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
        assert row is not None
        return self._operation_dict(row), False

    def get_operation_by_key(
        self,
        repository_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM coding_operation WHERE repository_id=? AND idempotency_key=?",
                (repository_id, key),
            ).fetchone()
        return self._operation_dict(row) if row is not None else None

    def list_operations(
        self,
        *,
        repository_id: str = "",
        worktree_id: str = "",
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        clauses: list[str] = []
        values: list[Any] = []
        if repository_id:
            clauses.append("repository_id=?")
            values.append(repository_id)
        if worktree_id:
            clauses.append("worktree_id=?")
            values.append(worktree_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit), 1000)))
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM coding_operation" + where
                + " ORDER BY created_at DESC LIMIT ?",
                values,
            ).fetchall()
        return tuple(self._operation_dict(row) for row in rows)

    @staticmethod
    def _operation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": str(row["operation_id"]),
            "repository_id": str(row["repository_id"]),
            "worktree_id": str(row["worktree_id"]),
            "kind": str(row["kind"]),
            "idempotency_key": str(row["idempotency_key"] or ""),
            "request_fingerprint": str(row["request_fingerprint"]),
            "state": str(row["state"]),
            "before_oid": str(row["before_oid"]),
            "after_oid": str(row["after_oid"]),
            "scope": _load(row["scope_json"], dict, "operation.scope"),
            "result": _load(row["result_json"], dict, "operation.result"),
            "diagnostic": str(row["diagnostic"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def finish_operation(
        self,
        operation_id: str,
        *,
        state: str,
        after_oid: str = "",
        result: Mapping[str, Any] | None = None,
        diagnostic: str = "",
    ) -> dict[str, Any]:
        with self._write() as connection:
            current = connection.execute(
                "SELECT * FROM coding_operation WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError("coding operation disappeared")
            if str(current["state"]) != "running":
                if str(current["state"]) == str(state):
                    return self._operation_dict(current)
                raise CodingConflict(
                    "terminal coding operation cannot be overwritten"
                )
            changed = connection.execute(
                """
                UPDATE coding_operation SET state=?, after_oid=?, result_json=?,
                    diagnostic=?, updated_at=?
                WHERE operation_id=? AND state='running'
                """,
                (state, after_oid, _json(dict(result or {})), diagnostic, time.time(), operation_id),
            )
            if changed.rowcount != 1:
                raise CodingConflict("coding operation completion lost its state fence")
            row = connection.execute(
                "SELECT * FROM coding_operation WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("coding operation disappeared")
        return self._operation_dict(row)

    def complete_review_integration(
        self,
        *,
        operation_id: str,
        review_id: str,
        expected_review_revision: int,
        after_oid: str,
        result: Mapping[str, Any] | None = None,
    ) -> ReviewRecord:
        """Commit operation success and integrated review in one transaction."""

        now = time.time()
        with self._write() as connection:
            operation = connection.execute(
                "SELECT * FROM coding_operation WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
            review = connection.execute(
                "SELECT * FROM coding_review WHERE review_id=?",
                (str(review_id),),
            ).fetchone()
            if operation is None or review is None:
                raise CodingConflict("integration operation or review disappeared")
            operation_state = str(operation["state"])
            review_state = str(review["state"])
            if operation_state == "succeeded" and review_state == "integrated":
                return self._review_from_row(review)
            repairing_succeeded = (
                operation_state == "succeeded"
                and review_state == "approved"
                and str(operation["after_oid"]) == str(after_oid)
            )
            if operation_state != "running" and not repairing_succeeded:
                raise CodingConflict("integration operation is not reconcilable")
            if (
                int(review["revision"]) != int(expected_review_revision)
                or str(review["state"]) != "approved"
            ):
                raise CodingConflict("approved review revision changed during integration")
            if str(after_oid or "") != str(review["head_oid"]):
                raise CodingConflict("integration result is not the exact reviewed head")
            if not repairing_succeeded:
                operation_changed = connection.execute(
                    "UPDATE coding_operation SET state='succeeded', after_oid=?, "
                    "result_json=?, diagnostic='', updated_at=? "
                    "WHERE operation_id=? AND state='running'",
                    (
                        str(after_oid), _json(dict(result or {})), now,
                        str(operation_id),
                    ),
                )
                if operation_changed.rowcount != 1:
                    raise CodingConflict("integration operation completion lost its fence")
            next_revision = int(review["revision"]) + 1
            review_changed = connection.execute(
                "UPDATE coding_review SET state='integrated', revision=?, updated_at=? "
                "WHERE review_id=? AND revision=? AND state='approved'",
                (
                    next_revision, now, str(review_id),
                    int(expected_review_revision),
                ),
            )
            if review_changed.rowcount != 1:
                raise CodingConflict("review integration transition lost its fence")
            scope = coerce_work_scope(
                _load(review["scope_json"], dict, "review.scope")
            )
            self._event(
                connection,
                kind="review",
                aggregate_id=str(review_id),
                revision=next_revision,
                event_type="coding.review.integrated",
                scope=scope,
                payload={
                    "approved_head_oid": str(review["approved_head_oid"]),
                    "operation_id": str(operation_id),
                },
            )
            row = connection.execute(
                "SELECT * FROM coding_review WHERE review_id=?",
                (str(review_id),),
            ).fetchone()
        assert row is not None
        return self._review_from_row(row)

    @staticmethod
    def _review_from_row(row: sqlite3.Row) -> ReviewRecord:
        return ReviewRecord(
            review_id=str(row["review_id"]),
            repository_id=str(row["repository_id"]),
            worktree_id=str(row["worktree_id"]),
            root=str(row["root"]),
            target=str(row["target"]),
            base_ref=str(row["base_ref"]),
            base_oid=str(row["base_oid"]),
            head_ref=str(row["head_ref"]),
            head_oid=str(row["head_oid"]),
            status_fingerprint=str(row["status_fingerprint"]),
            patch_ref=str(row["patch_ref"]),
            patch_sha256=str(row["patch_sha256"]),
            summary=_load(row["summary_json"], dict, "review.summary"),
            state=str(row["state"]),
            approved_head_oid=str(row["approved_head_oid"]),
            scope=coerce_work_scope(_load(row["scope_json"], dict, "review.scope")),
            revision=int(row["revision"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def create_review(
        self,
        review: ReviewRecord,
        files: Sequence[ReviewFile],
    ) -> ReviewRecord:
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO coding_review(
                    review_id, repository_id, worktree_id, root, target,
                    base_ref, base_oid, head_ref, head_oid, status_fingerprint,
                    patch_ref, patch_sha256, summary_json, state, approved_head_oid,
                    scope_json, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id, review.repository_id, review.worktree_id,
                    review.root, review.target, review.base_ref, review.base_oid,
                    review.head_ref, review.head_oid, review.status_fingerprint,
                    review.patch_ref, review.patch_sha256, _json(review.summary),
                    review.state, review.approved_head_oid, _json(review.scope.to_dict()),
                    review.revision, review.created_at, review.updated_at,
                ),
            )
            for position, item in enumerate(files):
                connection.execute(
                    """
                    INSERT INTO coding_review_file(
                        review_id, position, path, original_path, status, score,
                        old_mode, new_mode, old_oid, new_oid, additions, deletions,
                        binary, patch_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review.review_id, position, item.path, item.original_path,
                        item.status, item.score, item.old_mode, item.new_mode,
                        item.old_oid, item.new_oid, item.additions, item.deletions,
                        int(item.binary), item.patch_ref,
                    ),
                )
            self._event(
                connection, kind="review", aggregate_id=review.review_id,
                revision=review.revision, event_type="coding.review.created",
                scope=review.scope,
                payload={"head_oid": review.head_oid, "fingerprint": review.status_fingerprint},
            )
        return review

    def get_review(self, review_id: str) -> ReviewRecord:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM coding_review WHERE review_id=?", (review_id,)
            ).fetchone()
        if row is None:
            raise ReviewNotFound(review_id)
        return self._review_from_row(row)

    def list_reviews(
        self, *, repository_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> tuple[ReviewRecord, ...]:
        bounded = max(1, min(int(limit), 500))
        query = "SELECT * FROM coding_review"
        clauses: list[str] = []
        values: list[Any] = []
        if repository_id:
            clauses.append("repository_id=?")
            values.append(repository_id)
        if scope is not None:
            append_json_scope_visibility(clauses, values, "scope_json", scope)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        values.append(bounded)
        with self._read() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(self._review_from_row(row) for row in rows)

    def list_review_files(self, review_id: str) -> tuple[ReviewFile, ...]:
        self.get_review(review_id)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM coding_review_file WHERE review_id=? ORDER BY position",
                (review_id,),
            ).fetchall()
        return tuple(ReviewFile(
            review_id=str(row["review_id"]), path=str(row["path"]),
            original_path=str(row["original_path"]), status=str(row["status"]),
            score=int(row["score"]), old_mode=str(row["old_mode"]),
            new_mode=str(row["new_mode"]), old_oid=str(row["old_oid"]),
            new_oid=str(row["new_oid"]), additions=(None if row["additions"] is None else int(row["additions"])),
            deletions=(None if row["deletions"] is None else int(row["deletions"])),
            binary=bool(row["binary"]), patch_ref=str(row["patch_ref"]),
        ) for row in rows)

    def transition_review(
        self,
        review_id: str,
        *,
        expected_revision: int,
        allowed_states: Sequence[str],
        state: str,
        approved_head_oid: str | None = None,
    ) -> ReviewRecord:
        with self._write() as connection:
            current = connection.execute(
                "SELECT * FROM coding_review WHERE review_id=?", (review_id,)
            ).fetchone()
            if current is None:
                raise ReviewNotFound(review_id)
            if int(current["revision"]) != int(expected_revision):
                raise CodingConflict("review revision changed")
            if str(current["state"]) not in set(allowed_states):
                raise CodingConflict(f"cannot transition review from {current['state']} to {state}")
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                UPDATE coding_review SET state=?, approved_head_oid=?, revision=?, updated_at=?
                WHERE review_id=? AND revision=?
                """,
                (
                    state,
                    str(current["approved_head_oid"] if approved_head_oid is None else approved_head_oid),
                    revision, time.time(), review_id, expected_revision,
                ),
            )
            scope = coerce_work_scope(_load(current["scope_json"], dict, "review.scope"))
            self._event(
                connection, kind="review", aggregate_id=review_id, revision=revision,
                event_type=f"coding.review.{state}", scope=scope,
                payload={"approved_head_oid": approved_head_oid or ""},
            )
            row = connection.execute(
                "SELECT * FROM coding_review WHERE review_id=?", (review_id,)
            ).fetchone()
        assert row is not None
        return self._review_from_row(row)

    def add_finding(
        self,
        *,
        review_id: str,
        path: str,
        line: int,
        side: str,
        severity: str,
        title: str,
        body: str,
        scope: WorkScope,
    ) -> ReviewFinding:
        finding_id = new_id("finding")
        now = time.time()
        with self._write() as connection:
            review = connection.execute(
                "SELECT revision FROM coding_review WHERE review_id=?", (review_id,)
            ).fetchone()
            if review is None:
                raise ReviewNotFound(review_id)
            connection.execute(
                """
                INSERT INTO coding_review_finding(
                    finding_id, review_id, path, line, side, severity, title,
                    body, status, scope_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    finding_id, review_id, path, int(line), side, severity, title,
                    body, _json(scope.to_dict()), now, now,
                ),
            )
            self._event(
                connection, kind="review", aggregate_id=review_id,
                revision=int(review["revision"]), event_type="coding.review.finding_added",
                scope=scope, payload={"finding_id": finding_id, "path": path},
            )
        return ReviewFinding(
            finding_id=finding_id, review_id=review_id, path=path, line=int(line),
            side=side, severity=severity, title=title, body=body, status="open",
            scope=scope, created_at=now, updated_at=now,
        )

    def list_findings(self, review_id: str) -> tuple[ReviewFinding, ...]:
        self.get_review(review_id)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM coding_review_finding WHERE review_id=? ORDER BY created_at, finding_id",
                (review_id,),
            ).fetchall()
        return tuple(ReviewFinding(
            finding_id=str(row["finding_id"]), review_id=str(row["review_id"]),
            path=str(row["path"]), line=int(row["line"]), side=str(row["side"]),
            severity=str(row["severity"]), title=str(row["title"]), body=str(row["body"]),
            status=str(row["status"]),
            scope=coerce_work_scope(_load(row["scope_json"], dict, "finding.scope")),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        ) for row in rows)

    @staticmethod
    def _check_from_row(row: sqlite3.Row) -> CheckRun:
        return CheckRun(
            check_run_id=str(row["check_run_id"]), review_id=str(row["review_id"]),
            recipe=str(row["recipe"]), argv=tuple(_load(row["argv_json"], list, "check.argv")),
            state=str(row["state"]), exit_code=(None if row["exit_code"] is None else int(row["exit_code"])),
            head_oid=str(row["head_oid"]), status_fingerprint=str(row["status_fingerprint"]),
            log_ref=str(row["log_ref"]), log_sha256=str(row["log_sha256"]),
            duration_ms=float(row["duration_ms"]),
            scope=coerce_work_scope(_load(row["scope_json"], dict, "check.scope")),
            revision=int(row["revision"]), created_at=float(row["created_at"]),
            started_at=float(row["started_at"]), completed_at=float(row["completed_at"]),
            diagnostic=str(row["diagnostic"]),
        )

    def create_check(
        self,
        *,
        review_id: str,
        recipe: str,
        argv: Sequence[str],
        head_oid: str,
        status_fingerprint: str,
        scope: WorkScope,
    ) -> CheckRun:
        check_run_id = new_id("check")
        now = time.time()
        with self._write() as connection:
            if connection.execute(
                "SELECT 1 FROM coding_review WHERE review_id=?", (review_id,)
            ).fetchone() is None:
                raise ReviewNotFound(review_id)
            connection.execute(
                """
                INSERT INTO coding_check_run(
                    check_run_id, review_id, recipe, argv_json, state, exit_code,
                    head_oid, status_fingerprint, log_ref, log_sha256, duration_ms,
                    scope_json, revision, created_at, started_at, completed_at, diagnostic
                ) VALUES (?, ?, ?, ?, 'running', NULL, ?, ?, '', '', 0, ?, 1, ?, ?, 0, '')
                """,
                (
                    check_run_id, review_id, recipe, _json(list(argv)), head_oid,
                    status_fingerprint, _json(scope.to_dict()), now, now,
                ),
            )
            self._event(
                connection, kind="check", aggregate_id=check_run_id, revision=1,
                event_type="coding.check.running", scope=scope,
                payload={"review_id": review_id, "recipe": recipe},
            )
            row = connection.execute(
                "SELECT * FROM coding_check_run WHERE check_run_id=?", (check_run_id,)
            ).fetchone()
        assert row is not None
        return self._check_from_row(row)

    def finish_check(
        self,
        check_run_id: str,
        *,
        state: str,
        exit_code: int | None,
        log_ref: str,
        log_sha256: str,
        duration_ms: float,
        diagnostic: str = "",
    ) -> CheckRun:
        with self._write() as connection:
            current = connection.execute(
                "SELECT * FROM coding_check_run WHERE check_run_id=?", (check_run_id,)
            ).fetchone()
            if current is None:
                raise ReviewNotFound(check_run_id)
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                UPDATE coding_check_run SET state=?, exit_code=?, log_ref=?, log_sha256=?,
                    duration_ms=?, diagnostic=?, revision=?, completed_at=?
                WHERE check_run_id=?
                """,
                (
                    state, exit_code, log_ref, log_sha256, float(duration_ms), diagnostic,
                    revision, time.time(), check_run_id,
                ),
            )
            scope = coerce_work_scope(_load(current["scope_json"], dict, "check.scope"))
            self._event(
                connection, kind="check", aggregate_id=check_run_id, revision=revision,
                event_type=f"coding.check.{state}", scope=scope,
                payload={"exit_code": exit_code, "log_ref": log_ref},
            )
            row = connection.execute(
                "SELECT * FROM coding_check_run WHERE check_run_id=?", (check_run_id,)
            ).fetchone()
        assert row is not None
        return self._check_from_row(row)

    def list_checks(self, review_id: str) -> tuple[CheckRun, ...]:
        self.get_review(review_id)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM coding_check_run WHERE review_id=? ORDER BY created_at",
                (review_id,),
            ).fetchall()
        return tuple(self._check_from_row(row) for row in rows)

    def list_running_checks(self) -> tuple[CheckRun, ...]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM coding_check_run WHERE state='running' "
                "ORDER BY created_at"
            ).fetchall()
        return tuple(self._check_from_row(row) for row in rows)

    def events(self, *, after_sequence: int = 0, limit: int = 200) -> tuple[dict[str, Any], ...]:
        bounded = max(1, min(int(limit), 1000))
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM coding_event WHERE sequence>? ORDER BY sequence LIMIT ?
                """,
                (max(0, int(after_sequence)), bounded),
            ).fetchall()
        return tuple({
            "sequence": int(row["sequence"]),
            "event_id": str(row["event_id"]),
            "aggregate": {
                "kind": str(row["aggregate_kind"]),
                "id": str(row["aggregate_id"]),
                "revision": int(row["aggregate_revision"]),
            },
            "type": str(row["event_type"]),
            "scope": _load(row["scope_json"], dict, "event.scope"),
            "payload": _load(row["payload_json"], dict, "event.payload"),
            "created_at": float(row["created_at"]),
        } for row in rows)


__all__ = [
    "CodingRepository",
    "SCHEMA_VERSION",
    "default_coding_path",
    "default_managed_root",
    "new_id",
    "path_key",
]
