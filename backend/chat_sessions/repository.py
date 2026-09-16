"""SQLite authority for VARIANT-1's immutable conversation DAG."""

from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Iterator, Mapping

from core_invariants import (
    canonical_json,
    sqlite_read_connection,
    sqlite_unit_of_work,
    sqlite_wal_connection,
    sqlite_writer_lock,
    strict_json_value,
)
from .models import (
    EDGE_KINDS,
    BranchRecord,
    ConversationConflict,
    ConversationNode,
    ConversationNotFound,
    ConversationRecord,
    ConversationTombstoned,
    InvalidConversationGraph,
    SearchHit,
)


SCHEMA_VERSION = 2
_MAX_JSON_BYTES = 8 * 1024 * 1024
def _backend_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def default_conversation_path(*, data_dir: str | None = None) -> str:
    if data_dir:
        root = os.path.abspath(data_dir)
    else:
        app_root = os.path.abspath(
            os.environ.get("VARIANT1_DATA_DIR") or _backend_root()
        )
        root = os.path.join(app_root, "data")
    return os.path.abspath(
        os.environ.get("VARIANT1_CONVERSATION_DB")
        or os.path.join(root, "conversations", "conversations.sqlite3")
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _text(value: Any, field: str, *, required: bool = False, limit: int = 2000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    if "\x00" in result:
        raise ValueError(f"{field} contains a NUL character")
    return result


def _json_value(value: Any, *, path: str = "$") -> Any:
    return strict_json_value(value, path=path)


def _json(value: Any) -> str:
    encoded = canonical_json(_json_value(value))
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("conversation inline JSON is too large; use a content reference")
    return encoded


def _load_json(value: str | None, *, expected: type, field: str) -> Any:
    try:
        parsed = json.loads(value or ("{}" if expected is dict else "[]"))
    except Exception as exc:
        raise InvalidConversationGraph(f"invalid JSON in {field}: {exc}") from exc
    if not isinstance(parsed, expected):
        raise InvalidConversationGraph(
            f"{field} must contain a {expected.__name__}"
        )
    return parsed


def _content_preview(content: Any, limit: int = 500) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, Mapping):
        text = str(content.get("text") or content.get("content") or "")
        if not text:
            text = json.dumps(_json_value(content), ensure_ascii=False)
    else:
        text = json.dumps(_json_value(content), ensure_ascii=False)
    text = " ".join(text.split())
    return text[:limit]


class ConversationRepository:
    def __init__(self, path: str | None = None, *, data_dir: str | None = None) -> None:
        if path and data_dir:
            raise ValueError("pass either path or data_dir, not both")
        self.path = os.path.abspath(path or default_conversation_path(data_dir=data_dir))
        self._lock = sqlite_writer_lock(self.path)
        self.fts_enabled = False
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_wal_connection(self.path)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with sqlite_unit_of_work(
            self._connect, self._lock, fault_name="conversation.before_commit"
        ) as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with sqlite_read_connection(self._connect) as conn:
            yield conn

    def _initialize(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_schema_migration (
                        version INTEGER PRIMARY KEY,
                        applied_at REAL NOT NULL,
                        description TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS conversation (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        default_branch_id TEXT NOT NULL DEFAULT '',
                        pinned INTEGER NOT NULL DEFAULT 0,
                        archived INTEGER NOT NULL DEFAULT 0,
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        tombstoned_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversation_list
                        ON conversation(tombstoned_at, pinned DESC, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS conversation_branch (
                        branch_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        base_node_id TEXT NOT NULL DEFAULT '',
                        head_node_id TEXT NOT NULL DEFAULT '',
                        parent_branch_id TEXT NOT NULL DEFAULT '',
                        runtime_chat_id TEXT NOT NULL DEFAULT '',
                        runtime_thread_id TEXT NOT NULL DEFAULT '',
                        runtime_run_id TEXT NOT NULL DEFAULT '',
                        runtime_fork_mode TEXT NOT NULL DEFAULT 'fresh'
                            CHECK(runtime_fork_mode IN ('pending','snapshot','fresh')),
                        runtime_fork_reason TEXT NOT NULL DEFAULT '',
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        tombstoned_at REAL,
                        FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversation_branch_list
                        ON conversation_branch(conversation_id, tombstoned_at, created_at);

                    CREATE TABLE IF NOT EXISTS conversation_node (
                        node_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content_json TEXT NOT NULL,
                        content_text TEXT NOT NULL DEFAULT '',
                        content_ref TEXT NOT NULL DEFAULT '',
                        preview TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        turn_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversation_node_time
                        ON conversation_node(conversation_id, created_at, node_id);

                    CREATE TABLE IF NOT EXISTS conversation_edge (
                        edge_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        from_node_id TEXT,
                        to_node_id TEXT NOT NULL,
                        kind TEXT NOT NULL
                            CHECK(kind IN ('continuation','fork','merge','edit','replay')),
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id),
                        FOREIGN KEY(from_node_id) REFERENCES conversation_node(node_id),
                        FOREIGN KEY(to_node_id) REFERENCES conversation_node(node_id),
                        UNIQUE(from_node_id, to_node_id, kind)
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversation_edge_to
                        ON conversation_edge(conversation_id, to_node_id, created_at);

                    CREATE TABLE IF NOT EXISTS conversation_turn (
                        turn_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        branch_id TEXT NOT NULL,
                        user_node_id TEXT NOT NULL DEFAULT '',
                        assistant_node_id TEXT NOT NULL DEFAULT '',
                        run_id TEXT NOT NULL DEFAULT '',
                        receipt_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id),
                        FOREIGN KEY(branch_id) REFERENCES conversation_branch(branch_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversation_turn_branch
                        ON conversation_turn(branch_id, created_at, turn_id);

                    CREATE TABLE IF NOT EXISTS conversation_reflog (
                        reflog_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        branch_id TEXT NOT NULL,
                        old_head_node_id TEXT NOT NULL DEFAULT '',
                        new_head_node_id TEXT NOT NULL DEFAULT '',
                        operation TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id),
                        FOREIGN KEY(branch_id) REFERENCES conversation_branch(branch_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_conversation_reflog_branch
                        ON conversation_reflog(branch_id, created_at DESC);

                    CREATE TRIGGER IF NOT EXISTS trg_conversation_node_no_update
                    BEFORE UPDATE ON conversation_node BEGIN
                        SELECT RAISE(ABORT, 'conversation_node is immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_conversation_node_no_delete
                    BEFORE DELETE ON conversation_node BEGIN
                        SELECT RAISE(ABORT, 'conversation_node is immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_conversation_edge_no_update
                    BEFORE UPDATE ON conversation_edge BEGIN
                        SELECT RAISE(ABORT, 'conversation_edge is immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_conversation_edge_no_delete
                    BEFORE DELETE ON conversation_edge BEGIN
                        SELECT RAISE(ABORT, 'conversation_edge is immutable');
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_conversation_reflog_no_update
                    BEFORE UPDATE ON conversation_reflog BEGIN
                        SELECT RAISE(ABORT, 'conversation_reflog is append-only');
                    END;
                    CREATE TRIGGER IF NOT EXISTS trg_conversation_reflog_no_delete
                    BEFORE DELETE ON conversation_reflog BEGIN
                        SELECT RAISE(ABORT, 'conversation_reflog is append-only');
                    END;
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_schema_migration"
                    "(version, applied_at, description) VALUES (?, ?, ?)",
                    (SCHEMA_VERSION, time.time(), "canonical SQL conversation DAG"),
                )
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(conversation_branch)").fetchall()
                }
                if "runtime_chat_id" not in columns:
                    conn.execute(
                        "ALTER TABLE conversation_branch ADD COLUMN "
                        "runtime_chat_id TEXT NOT NULL DEFAULT ''"
                    )
                conn.execute(
                    "UPDATE conversation_branch SET runtime_chat_id='chat:' || branch_id "
                    "WHERE runtime_chat_id=''"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_branch_runtime_chat "
                    "ON conversation_branch(runtime_chat_id) WHERE runtime_chat_id <> ''"
                )
                try:
                    conn.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS conversation_node_fts "
                        "USING fts5(node_id UNINDEXED, conversation_id UNINDEXED, "
                        "role UNINDEXED, content, preview, tokenize='unicode61')"
                    )
                    self.fts_enabled = True
                except sqlite3.OperationalError:
                    self.fts_enabled = False
            finally:
                conn.close()

    @staticmethod
    def _conversation(row: sqlite3.Row | None) -> ConversationRecord | None:
        if row is None:
            return None
        return ConversationRecord(
            conversation_id=str(row["conversation_id"]),
            title=str(row["title"]),
            default_branch_id=str(row["default_branch_id"] or ""),
            pinned=bool(row["pinned"]), archived=bool(row["archived"]),
            version=int(row["version"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            tombstoned_at=float(row["tombstoned_at"] or 0),
        )

    @staticmethod
    def _branch(row: sqlite3.Row | None) -> BranchRecord | None:
        if row is None:
            return None
        return BranchRecord(
            branch_id=str(row["branch_id"]),
            conversation_id=str(row["conversation_id"]),
            name=str(row["name"]), base_node_id=str(row["base_node_id"] or ""),
            head_node_id=str(row["head_node_id"] or ""),
            parent_branch_id=str(row["parent_branch_id"] or ""),
            runtime_chat_id=str(row["runtime_chat_id"] or ""),
            runtime_thread_id=str(row["runtime_thread_id"] or ""),
            runtime_run_id=str(row["runtime_run_id"] or ""),
            runtime_fork_mode=str(row["runtime_fork_mode"]),
            runtime_fork_reason=str(row["runtime_fork_reason"] or ""),
            version=int(row["version"]), created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            tombstoned_at=float(row["tombstoned_at"] or 0),
        )

    @staticmethod
    def _node(row: sqlite3.Row | None) -> ConversationNode | None:
        if row is None:
            return None
        return ConversationNode(
            node_id=str(row["node_id"]), conversation_id=str(row["conversation_id"]),
            role=str(row["role"]),
            content=json.loads(str(row["content_json"])),
            content_ref=str(row["content_ref"] or ""), preview=str(row["preview"] or ""),
            metadata=_load_json(row["metadata_json"], expected=dict, field="node.metadata"),
            turn_id=str(row["turn_id"] or ""), created_at=float(row["created_at"]),
        )

    def create_conversation(
        self,
        title: str,
        *,
        conversation_id: str = "",
        branch_name: str = "Main",
        created_at: float | None = None,
    ) -> ConversationRecord:
        now = float(created_at if created_at is not None else time.time())
        explicit_identity = bool(str(conversation_id or "").strip())
        cid = _text(conversation_id, "conversation_id", limit=512) or _new_id("conv")
        bid = (
            "branch_" + hashlib.sha256(
                f"{cid}\0{str(branch_name or 'Main')}".encode("utf-8")
            ).hexdigest()[:32]
            if explicit_identity else _new_id("branch")
        )
        clean_title = _text(title, "title", required=True, limit=500)
        clean_branch_name = _text(
            branch_name, "branch name", required=True, limit=240
        )
        with self._write() as conn:
            if explicit_identity:
                prior = conn.execute(
                    "SELECT * FROM conversation WHERE conversation_id=?", (cid,)
                ).fetchone()
                if prior is not None:
                    existing = self._conversation(prior)
                    branch = conn.execute(
                        "SELECT name FROM conversation_branch WHERE branch_id=?",
                        (str(prior["default_branch_id"]),),
                    ).fetchone()
                    if (
                        existing is None
                        or existing.deleted
                        or existing.title != clean_title
                        or branch is None
                        or str(branch["name"]) != clean_branch_name
                    ):
                        raise ConversationConflict(
                            "conversation request identity conflicts with existing content"
                        )
                    return existing
            conn.execute(
                "INSERT INTO conversation(conversation_id, title, "
                "default_branch_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (cid, clean_title, bid, now, now),
            )
            conn.execute(
                "INSERT INTO conversation_branch(branch_id, conversation_id, name, "
                "runtime_chat_id, runtime_thread_id, runtime_run_id, runtime_fork_mode, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'fresh', ?, ?)",
                (bid, cid, clean_branch_name,
                 f"chat:{bid}",
                 f"conversation:{cid}:branch:{bid}", f"run:{bid}", now, now),
            )
            self._reflog_tx(conn, cid, bid, "", "", "branch.created", "system", {})
            return self._conversation(conn.execute(
                "SELECT * FROM conversation WHERE conversation_id=?", (cid,)
            ).fetchone())  # type: ignore[return-value]

    def get_conversation(
        self, conversation_id: str, *, include_deleted: bool = False
    ) -> ConversationRecord | None:
        sql = "SELECT * FROM conversation WHERE conversation_id=?"
        if not include_deleted:
            sql += " AND tombstoned_at IS NULL"
        with self._read() as conn:
            return self._conversation(conn.execute(sql, (str(conversation_id),)).fetchone())

    def require_conversation(self, conversation_id: str) -> ConversationRecord:
        record = self.get_conversation(conversation_id, include_deleted=True)
        if record is None:
            raise ConversationNotFound(f"unknown conversation: {conversation_id}")
        if record.deleted:
            raise ConversationTombstoned(f"conversation is deleted: {conversation_id}")
        return record

    def list_conversations(
        self, *, include_archived: bool = True, include_deleted: bool = False,
        limit: int | None = 200,
    ) -> list[ConversationRecord]:
        clauses = [] if include_deleted else ["tombstoned_at IS NULL"]
        params: list[Any] = []
        if not include_archived:
            clauses.append("archived=0")
        sql = "SELECT * FROM conversation"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY pinned DESC, updated_at DESC, conversation_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(min(5000, max(1, int(limit))))
        with self._read() as conn:
            return [self._conversation(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def update_conversation(
        self, conversation_id: str, *, expected_version: int,
        title: str | None = None, pinned: bool | None = None,
        archived: bool | None = None,
    ) -> ConversationRecord:
        updates = []
        params: list[Any] = []
        if title is not None:
            updates.append("title=?"); params.append(_text(title, "title", required=True, limit=500))
        if pinned is not None:
            updates.append("pinned=?"); params.append(int(bool(pinned)))
        if archived is not None:
            updates.append("archived=?"); params.append(int(bool(archived)))
        if not updates:
            return self.require_conversation(conversation_id)
        now = time.time()
        with self._write() as conn:
            updates.extend(["version=version+1", "updated_at=?"]); params.append(now)
            params.extend([str(conversation_id), int(expected_version)])
            changed = conn.execute(
                "UPDATE conversation SET " + ",".join(updates)
                + " WHERE conversation_id=? AND version=? AND tombstoned_at IS NULL",
                tuple(params),
            )
            if changed.rowcount != 1:
                self._raise_conversation_cas(conn, conversation_id, expected_version)
            return self._conversation(conn.execute(
                "SELECT * FROM conversation WHERE conversation_id=?", (conversation_id,)
            ).fetchone())  # type: ignore[return-value]

    def tombstone_conversation(
        self, conversation_id: str, *, expected_version: int
    ) -> ConversationRecord:
        now = time.time()
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE conversation SET tombstoned_at=?, version=version+1, updated_at=? "
                "WHERE conversation_id=? AND version=? AND tombstoned_at IS NULL",
                (now, now, str(conversation_id), int(expected_version)),
            )
            if changed.rowcount != 1:
                self._raise_conversation_cas(conn, conversation_id, expected_version)
            conn.execute(
                "UPDATE conversation_branch SET tombstoned_at=COALESCE(tombstoned_at, ?), "
                "version=version+1, updated_at=? WHERE conversation_id=?",
                (now, now, str(conversation_id)),
            )
            return self._conversation(conn.execute(
                "SELECT * FROM conversation WHERE conversation_id=?", (conversation_id,)
            ).fetchone())  # type: ignore[return-value]

    @staticmethod
    def _raise_conversation_cas(
        conn: sqlite3.Connection, conversation_id: str, expected: int
    ) -> None:
        row = conn.execute(
            "SELECT version, tombstoned_at FROM conversation WHERE conversation_id=?",
            (str(conversation_id),),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(f"unknown conversation: {conversation_id}")
        if row["tombstoned_at"] is not None:
            raise ConversationTombstoned(f"conversation is deleted: {conversation_id}")
        raise ConversationConflict(
            f"conversation version changed ({int(row['version'])} != {int(expected)})"
        )

    @staticmethod
    def _raise_branch_cas(
        conn: sqlite3.Connection, branch_id: str, expected: int
    ) -> None:
        row = conn.execute(
            "SELECT version, tombstoned_at FROM conversation_branch WHERE branch_id=?",
            (str(branch_id),),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(f"unknown branch: {branch_id}")
        if row["tombstoned_at"] is not None:
            raise ConversationTombstoned(f"branch is deleted: {branch_id}")
        raise ConversationConflict(
            f"branch version changed ({int(row['version'])} != {int(expected)})"
        )

    @staticmethod
    def _reflog_tx(
        conn: sqlite3.Connection,
        conversation_id: str,
        branch_id: str,
        old_head: str,
        new_head: str,
        operation: str,
        actor: str,
        metadata: Mapping[str, Any],
        *,
        created_at: float | None = None,
    ) -> str:
        rid = _new_id("reflog")
        conn.execute(
            "INSERT INTO conversation_reflog(reflog_id, conversation_id, branch_id, "
            "old_head_node_id, new_head_node_id, operation, actor, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, conversation_id, branch_id, old_head, new_head,
             _text(operation, "operation", required=True, limit=120),
             _text(actor, "actor", required=True, limit=240), _json(metadata),
             float(created_at if created_at is not None else time.time())),
        )
        return rid

    def get_branch(
        self, branch_id: str, *, include_deleted: bool = False
    ) -> BranchRecord | None:
        sql = "SELECT * FROM conversation_branch WHERE branch_id=?"
        if not include_deleted:
            sql += " AND tombstoned_at IS NULL"
        with self._read() as conn:
            return self._branch(conn.execute(sql, (str(branch_id),)).fetchone())

    def require_branch(self, branch_id: str) -> BranchRecord:
        branch = self.get_branch(branch_id, include_deleted=True)
        if branch is None:
            raise ConversationNotFound(f"unknown branch: {branch_id}")
        if branch.deleted:
            raise ConversationTombstoned(f"branch is deleted: {branch_id}")
        self.require_conversation(branch.conversation_id)
        return branch

    def list_branches(
        self, conversation_id: str, *, include_deleted: bool = False
    ) -> list[BranchRecord]:
        sql = "SELECT * FROM conversation_branch WHERE conversation_id=?"
        if not include_deleted:
            sql += " AND tombstoned_at IS NULL"
        sql += " ORDER BY created_at, branch_id"
        with self._read() as conn:
            return [self._branch(row) for row in conn.execute(
                sql, (str(conversation_id),)
            ).fetchall()]

    def tombstone_branch(
        self, branch_id: str, *, expected_version: int, actor: str = "system"
    ) -> BranchRecord:
        now = time.time()
        with self._write() as conn:
            branch = self._branch(conn.execute(
                "SELECT * FROM conversation_branch WHERE branch_id=?", (branch_id,)
            ).fetchone())
            if branch is None:
                raise ConversationNotFound(f"unknown branch: {branch_id}")
            conv = self._conversation(conn.execute(
                "SELECT * FROM conversation WHERE conversation_id=?", (branch.conversation_id,)
            ).fetchone())
            if conv and conv.default_branch_id == branch_id:
                raise ConversationConflict("the default branch cannot be deleted")
            changed = conn.execute(
                "UPDATE conversation_branch SET tombstoned_at=?, version=version+1, updated_at=? "
                "WHERE branch_id=? AND version=? AND tombstoned_at IS NULL",
                (now, now, branch_id, int(expected_version)),
            )
            if changed.rowcount != 1:
                self._raise_branch_cas(conn, branch_id, expected_version)
            self._reflog_tx(conn, branch.conversation_id, branch_id,
                            branch.head_node_id, branch.head_node_id,
                            "branch.tombstoned", actor, {}, created_at=now)
            return self._branch(conn.execute(
                "SELECT * FROM conversation_branch WHERE branch_id=?", (branch_id,)
            ).fetchone())  # type: ignore[return-value]

    def _insert_node_tx(
        self, conn: sqlite3.Connection, *, conversation_id: str, role: str,
        content: Any, content_ref: str, metadata: Mapping[str, Any], turn_id: str,
        created_at: float, node_id: str = "",
    ) -> str:
        nid = _text(node_id, "node_id", limit=512) or _new_id("node")
        role_text = _text(role, "role", required=True, limit=80)
        encoded = _json(content)
        preview = _content_preview(content)
        content_text = content if isinstance(content, str) else preview
        conn.execute(
            "INSERT INTO conversation_node(node_id, conversation_id, role, content_json, "
            "content_text, content_ref, preview, metadata_json, turn_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (nid, conversation_id, role_text, encoded, str(content_text),
             _text(content_ref, "content_ref", limit=4000), preview,
             _json(metadata), turn_id, created_at),
        )
        if self.fts_enabled:
            try:
                conn.execute(
                    "INSERT INTO conversation_node_fts(node_id, conversation_id, role, content, preview) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (nid, conversation_id, role_text, str(content_text), preview),
                )
            except sqlite3.OperationalError:
                self.fts_enabled = False
        return nid

    @staticmethod
    def _insert_edge_tx(
        conn: sqlite3.Connection, *, conversation_id: str, from_node_id: str,
        to_node_id: str, kind: str, metadata: Mapping[str, Any], created_at: float,
    ) -> str:
        if kind not in EDGE_KINDS:
            raise ValueError(f"invalid edge kind: {kind}")
        eid = _new_id("edge")
        conn.execute(
            "INSERT INTO conversation_edge(edge_id, conversation_id, from_node_id, "
            "to_node_id, kind, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (eid, conversation_id, from_node_id or None, to_node_id,
             kind, _json(metadata), created_at),
        )
        return eid

    @staticmethod
    def _is_reachable_tx(
        conn: sqlite3.Connection,
        conversation_id: str,
        branch_head_node_id: str,
        target_node_id: str,
    ) -> bool:
        """Check one node against the private transcript ancestry chain."""

        head = str(branch_head_node_id or "")
        target = str(target_node_id or "")
        if not head or not target:
            return False
        row = conn.execute(
            "WITH RECURSIVE ancestry(node_id) AS ("
            " SELECT ? UNION SELECT e.from_node_id FROM conversation_edge e "
            " JOIN ancestry a ON e.to_node_id=a.node_id "
            " WHERE e.conversation_id=? AND e.from_node_id IS NOT NULL"
            ") SELECT 1 FROM ancestry WHERE node_id=? LIMIT 1",
            (head, str(conversation_id), target),
        ).fetchone()
        return row is not None

    def history_nodes(
        self, branch_id: str, *, head_node_id: str = "", limit: int = 5000
    ) -> list[ConversationNode]:
        branch = self.require_branch(branch_id)
        head = str(head_node_id or branch.head_node_id)
        if not head:
            return []
        with self._read() as conn:
            if not self._is_reachable_tx(conn, branch.conversation_id, branch.head_node_id, head):
                raise InvalidConversationGraph(f"node {head!r} is not in branch ancestry")
            rows = conn.execute(
                "WITH RECURSIVE ancestry(node_id) AS ("
                " SELECT ? UNION SELECT e.from_node_id FROM conversation_edge e "
                " JOIN ancestry a ON e.to_node_id=a.node_id "
                " WHERE e.conversation_id=? AND e.from_node_id IS NOT NULL"
                ") SELECT DISTINCT n.* FROM conversation_node n JOIN ancestry a "
                "ON a.node_id=n.node_id ORDER BY n.created_at DESC, n.node_id DESC LIMIT ?",
                (head, branch.conversation_id, min(20000, max(1, limit))),
            ).fetchall()
            return [self._node(row) for row in reversed(rows)]

    def search(
        self, query: str, *, limit: int = 50, include_archived: bool = False
    ) -> list[SearchHit]:
        query_text = _text(query, "query", required=True, limit=1000)
        cap = min(500, max(1, int(limit)))
        tokens = re.findall(r"[\w-]+", query_text, flags=re.UNICODE)
        if self.fts_enabled and tokens:
            expression = " AND ".join('"' + token.replace('"', '""') + '"*' for token in tokens[:20])
            try:
                with self._read() as conn:
                    rows = conn.execute(
                        "SELECT f.conversation_id, f.node_id, n.role, n.preview, n.created_at, "
                        "c.title, bm25(conversation_node_fts) AS rank FROM conversation_node_fts f "
                        "JOIN conversation_node n ON n.node_id=f.node_id "
                        "JOIN conversation c ON c.conversation_id=f.conversation_id "
                        "WHERE conversation_node_fts MATCH ? AND c.tombstoned_at IS NULL "
                        + ("" if include_archived else "AND c.archived=0 ")
                        + "ORDER BY rank, n.created_at DESC LIMIT ?", (expression, cap),
                    ).fetchall()
                    return [SearchHit(
                        conversation_id=str(row["conversation_id"]), node_id=str(row["node_id"]),
                        title=str(row["title"]), role=str(row["role"]),
                        preview=str(row["preview"]), score=-float(row["rank"] or 0),
                        created_at=float(row["created_at"]),
                    ) for row in rows]
            except sqlite3.OperationalError:
                self.fts_enabled = False
        pattern = "%" + query_text.replace("%", "\\%").replace("_", "\\_") + "%"
        with self._read() as conn:
            rows = conn.execute(
                "SELECT n.conversation_id, n.node_id, n.role, n.preview, n.created_at, c.title "
                "FROM conversation_node n JOIN conversation c ON c.conversation_id=n.conversation_id "
                "WHERE c.tombstoned_at IS NULL AND (n.content_text LIKE ? ESCAPE '\\' "
                "OR n.preview LIKE ? ESCAPE '\\' OR c.title LIKE ? ESCAPE '\\') "
                + ("" if include_archived else "AND c.archived=0 ")
                + "ORDER BY n.created_at DESC LIMIT ?", (pattern, pattern, pattern, cap),
            ).fetchall()
            return [SearchHit(
                conversation_id=str(row["conversation_id"]), node_id=str(row["node_id"]),
                title=str(row["title"]), role=str(row["role"]),
                preview=str(row["preview"]), score=0.0,
                created_at=float(row["created_at"]),
            ) for row in rows]

__all__ = ["ConversationRepository", "SCHEMA_VERSION", "default_conversation_path"]
