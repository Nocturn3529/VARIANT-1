"""Approved user memory and its explicit revision lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any

from core_invariants import canonical_digest, canonical_json, sqlite_session_connection
from tool_core import json_safe


_WORDS = re.compile(r"[a-z0-9_]+")
_PROPOSAL_KINDS = frozenset({"add", "update"})


class MemoryConflict(RuntimeError):
    """The expected memory revision no longer matches authoritative state."""


def _stable(value: Any) -> str:
    return canonical_json(json_safe(value))


def _digest(value: Any) -> str:
    return canonical_digest(json_safe(value))


class MemoryStore:
    def __init__(self, database_path: str):
        self.path = os.path.abspath(database_path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_memory_item (
                    item_id TEXT PRIMARY KEY,
                    current_revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    owner_chat_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_memory_revision (
                    item_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    source_proposal_id TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(item_id, revision),
                    FOREIGN KEY(item_id) REFERENCES session_memory_item(item_id)
                );
                CREATE TABLE IF NOT EXISTS session_memory_proposal (
                    proposal_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    expected_revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    proposal_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    reviewed_at REAL
                );
                CREATE INDEX IF NOT EXISTS session_memory_proposal_chat_idx
                ON session_memory_proposal(chat_id, created_at DESC);
                """
            )
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
                    "USING fts5(item_id UNINDEXED, content, tokenize='unicode61')"
                )
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False
        if self.fts_enabled:
            self._rebuild_search_index()

    def _connect(self) -> sqlite3.Connection:
        return sqlite_session_connection(self.path, autocommit=False)

    def _rebuild_search_index(self) -> None:
        if not getattr(self, "fts_enabled", False):
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM memory_fts")
            conn.execute(
                "INSERT INTO memory_fts(item_id,content) "
                "SELECT i.item_id,r.content FROM session_memory_item i "
                "JOIN session_memory_revision r ON r.item_id=i.item_id "
                "AND r.revision=i.current_revision WHERE i.state='active'"
            )

    @staticmethod
    def _metadata(
        metadata: dict[str, Any] | None,
        *,
        provenance: str,
        confidence: float,
        expires_at: float | None,
    ) -> dict[str, Any]:
        clean = dict(json_safe(metadata or {}))
        clean["provenance"] = str(provenance or "model_proposal")[:300]
        clean["confidence"] = min(1.0, max(0.0, float(confidence)))
        clean["expires_at"] = float(expires_at or 0.0)
        return clean

    def propose(
        self,
        chat_id: str,
        *,
        kind: str,
        content: str,
        item_id: str = "",
        expected_revision: int = 0,
        scope: str = "user",
        provenance: str = "model_proposal",
        confidence: float = 0.5,
        expires_at: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        proposal_kind = str(kind or "").casefold()
        if proposal_kind not in _PROPOSAL_KINDS:
            raise ValueError("memory proposal kind must be add or update")
        clean_content = str(content or "").strip()
        if not clean_content or len(clean_content.encode("utf-8")) > 32_768:
            raise ValueError("memory content must be 1..32768 bytes")
        clean_scope = str(scope or "user").casefold()
        if clean_scope not in {"user", "chat"}:
            raise ValueError("memory scope must be user or chat")
        target = str(item_id or "").strip()
        expected = int(expected_revision or 0)
        with self._lock, self._connect() as conn:
            if proposal_kind == "add":
                if target or expected:
                    raise ValueError("add proposals cannot name an existing item/revision")
                target = "mem_" + uuid.uuid4().hex
            else:
                if not target or expected <= 0:
                    raise ValueError("update proposals require item_id and expected_revision")
                current = conn.execute(
                    "SELECT current_revision, state FROM session_memory_item WHERE item_id=?",
                    (target,),
                ).fetchone()
                if current is None or current["state"] != "active":
                    raise LookupError("memory item is unavailable")
                if int(current["current_revision"]) != expected:
                    raise MemoryConflict(
                        f"memory CAS failed ({current['current_revision']} != {expected})"
                    )
            clean_metadata = self._metadata(
                metadata,
                provenance=provenance,
                confidence=confidence,
                expires_at=expires_at,
            )
            clean_metadata["scope"] = clean_scope
            proposal_id = "mprop_" + uuid.uuid4().hex
            payload = {
                "kind": proposal_kind,
                "item_id": target,
                "expected_revision": expected,
                "content": clean_content,
                "metadata": clean_metadata,
            }
            now = time.time()
            conn.execute(
                "INSERT INTO session_memory_proposal(proposal_id, chat_id, kind, item_id, "
                "expected_revision, content, metadata_json, proposal_digest, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    proposal_id, str(chat_id), proposal_kind, target, expected,
                    clean_content, _stable(clean_metadata), _digest(payload), now,
                ),
            )
        return {
            "proposal_id": proposal_id,
            "state": "pending_user_approval",
            "kind": proposal_kind,
            "item_id": target,
            "expected_revision": expected,
            "proposal_digest": _digest(payload),
            "content_sha256": hashlib.sha256(clean_content.encode("utf-8")).hexdigest(),
        }

    def find_duplicate(self, content: str) -> dict[str, Any] | None:
        """Find an exact normalized pending or approved memory value."""

        key = " ".join(str(content or "").casefold().split())
        if not key:
            return None
        with self._lock, self._connect() as conn:
            pending = conn.execute(
                "SELECT proposal_id,item_id,content FROM session_memory_proposal "
                "WHERE state='pending' ORDER BY created_at DESC"
            ).fetchall()
            active = conn.execute(
                "SELECT i.item_id,r.content FROM session_memory_item i "
                "JOIN session_memory_revision r ON r.item_id=i.item_id "
                "AND r.revision=i.current_revision WHERE i.state='active' "
                "ORDER BY i.updated_at DESC"
            ).fetchall()
        for row in pending:
            if " ".join(str(row["content"]).casefold().split()) == key:
                return {
                    "state": "pending", "proposal_id": row["proposal_id"],
                    "item_id": row["item_id"],
                }
        for row in active:
            if " ".join(str(row["content"]).casefold().split()) == key:
                return {"state": "active", "item_id": row["item_id"]}
        return None

    def approve(
        self,
        proposal_id: str,
        *,
        actor: str,
        content_override: str | None = None,
    ) -> dict[str, Any]:
        reviewer = str(actor or "").strip()
        if not reviewer:
            raise ValueError("memory approval requires an actor")
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM session_memory_proposal WHERE proposal_id=?",
                (str(proposal_id),),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError("unknown memory proposal")
            if row["state"] == "approved":
                item = conn.execute(
                    "SELECT * FROM session_memory_item WHERE item_id=?", (row["item_id"],)
                ).fetchone()
                conn.rollback()
                return {
                    "proposal_id": row["proposal_id"], "item_id": row["item_id"],
                    "revision": int(item["current_revision"]), "state": item["state"],
                    "already_approved": True,
                }
            if row["state"] != "pending":
                conn.rollback()
                raise RuntimeError("memory proposal is no longer pending")
            metadata = json.loads(row["metadata_json"])
            content = str(row["content"])
            if content_override is not None:
                content = str(content_override or "").strip()
                if not content or len(content.encode("utf-8")) > 32_768:
                    conn.rollback()
                    raise ValueError("memory content must be 1..32768 bytes")
                payload = {
                    "kind": row["kind"],
                    "item_id": row["item_id"],
                    "expected_revision": int(row["expected_revision"]),
                    "content": content,
                    "metadata": metadata,
                }
                conn.execute(
                    "UPDATE session_memory_proposal SET content=?, proposal_digest=? "
                    "WHERE proposal_id=?",
                    (content, _digest(payload), row["proposal_id"]),
                )
            if row["kind"] == "add":
                revision = 1
                conn.execute(
                    "INSERT INTO session_memory_item(item_id, current_revision, state, scope, "
                    "owner_chat_id, created_at, updated_at) VALUES (?, 1, 'active', ?, ?, ?, ?)",
                    (
                        row["item_id"], str(metadata.get("scope") or "user"),
                        row["chat_id"], now, now,
                    ),
                )
            else:
                current = conn.execute(
                    "SELECT current_revision, state FROM session_memory_item WHERE item_id=?",
                    (row["item_id"],),
                ).fetchone()
                if (
                    current is None or current["state"] != "active"
                    or int(current["current_revision"]) != int(row["expected_revision"])
                ):
                    conn.rollback()
                    actual = "missing" if current is None else current["current_revision"]
                    raise MemoryConflict(
                        f"memory approval CAS failed ({actual} != {row['expected_revision']})"
                    )
                revision = int(current["current_revision"]) + 1
                conn.execute(
                    "UPDATE session_memory_item SET current_revision=?, updated_at=? WHERE item_id=?",
                    (revision, now, row["item_id"]),
                )
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO session_memory_revision(item_id, revision, content, metadata_json, "
                "state, source_proposal_id, approved_by, content_sha256, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    row["item_id"], revision, content, row["metadata_json"],
                    row["proposal_id"], reviewer, content_hash, now,
                ),
            )
            conn.execute(
                "UPDATE session_memory_proposal SET state='approved', reviewed_by=?, "
                "reviewed_at=? WHERE proposal_id=?",
                (reviewer, now, row["proposal_id"]),
            )
            conn.commit()
        self._rebuild_search_index()
        return {
            "proposal_id": str(proposal_id), "item_id": row["item_id"],
            "revision": revision, "state": "active", "content_sha256": content_hash,
            "already_approved": False,
        }

    def reject(self, proposal_id: str, *, actor: str, note: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            result = conn.execute(
                "UPDATE session_memory_proposal SET state='rejected', reviewed_by=?, "
                "review_note=?, reviewed_at=? WHERE proposal_id=? AND state='pending'",
                (str(actor or "user"), str(note or "")[:1000], time.time(), str(proposal_id)),
            )
        if int(result.rowcount or 0) != 1:
            raise RuntimeError("memory proposal is absent or no longer pending")
        return {"proposal_id": str(proposal_id), "state": "rejected"}

    def list_proposals(
        self,
        *,
        state: str = "pending",
        chat_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return reviewable proposal content for the Memory surface."""

        clean_state = str(state or "").strip().casefold()
        if clean_state not in {"", "pending", "approved", "rejected"}:
            raise ValueError("unsupported memory proposal state")
        clauses: list[str] = []
        values: list[Any] = []
        if clean_state:
            clauses.append("state=?")
            values.append(clean_state)
        if chat_id:
            clauses.append("chat_id=?")
            values.append(str(chat_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit or 100), 500)))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT proposal_id,chat_id,kind,item_id,expected_revision,"
                "content,metadata_json,proposal_digest,state,reviewed_by,"
                "review_note,created_at,reviewed_at FROM session_memory_proposal"
                + where + " ORDER BY created_at DESC,proposal_id LIMIT ?",
                tuple(values),
            ).fetchall()
        return [{
            "proposal_id": str(row["proposal_id"]),
            "chat_id": str(row["chat_id"]),
            "kind": str(row["kind"]),
            "item_id": str(row["item_id"]),
            "expected_revision": int(row["expected_revision"]),
            "content": str(row["content"]),
            "metadata": dict(json.loads(str(row["metadata_json"] or "{}"))),
            "proposal_digest": str(row["proposal_digest"]),
            "state": str(row["state"]),
            "reviewed_by": str(row["reviewed_by"] or ""),
            "review_note": str(row["review_note"] or ""),
            "created_at": float(row["created_at"]),
            "reviewed_at": (
                float(row["reviewed_at"])
                if row["reviewed_at"] is not None else None
            ),
        } for row in rows]

    def tombstone(
        self, item_id: str, *, expected_revision: int, actor: str, reason: str = ""
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM session_memory_item WHERE item_id=?", (str(item_id),)
            ).fetchone()
            if current is None or current["state"] != "active":
                conn.rollback()
                raise LookupError("memory item is unavailable")
            if int(current["current_revision"]) != int(expected_revision):
                conn.rollback()
                raise MemoryConflict(
                    f"memory delete CAS failed ({current['current_revision']} != {expected_revision})"
                )
            prior = conn.execute(
                "SELECT * FROM session_memory_revision WHERE item_id=? AND revision=?",
                (str(item_id), int(expected_revision)),
            ).fetchone()
            revision = int(expected_revision) + 1
            metadata = json.loads(prior["metadata_json"])
            metadata["tombstone_reason"] = str(reason or "")[:1000]
            conn.execute(
                "INSERT INTO session_memory_revision(item_id, revision, content, metadata_json, "
                "state, source_proposal_id, approved_by, content_sha256, created_at) "
                "VALUES (?, ?, '', ?, 'tombstoned', '', ?, ?, ?)",
                (
                    str(item_id), revision, _stable(metadata), str(actor or "user"),
                    hashlib.sha256(b"").hexdigest(), now,
                ),
            )
            conn.execute(
                "UPDATE session_memory_item SET current_revision=?, state='tombstoned', "
                "updated_at=? WHERE item_id=?",
                (revision, now, str(item_id)),
            )
            conn.commit()
        self._rebuild_search_index()
        return {"item_id": str(item_id), "revision": revision, "state": "tombstoned"}

    def restore(self, item_id: str, *, expected_revision: int, actor: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute(
                "SELECT * FROM session_memory_item WHERE item_id=?", (str(item_id),)
            ).fetchone()
            if (
                item is None or item["state"] != "tombstoned"
                or int(item["current_revision"]) != int(expected_revision)
            ):
                conn.rollback()
                raise MemoryConflict("memory restore CAS failed")
            prior = conn.execute(
                "SELECT * FROM session_memory_revision WHERE item_id=? AND state='active' "
                "ORDER BY revision DESC LIMIT 1",
                (str(item_id),),
            ).fetchone()
            if prior is None:
                conn.rollback()
                raise LookupError("memory has no restorable revision")
            revision = int(expected_revision) + 1
            conn.execute(
                "INSERT INTO session_memory_revision(item_id, revision, content, metadata_json, "
                "state, source_proposal_id, approved_by, content_sha256, created_at) "
                "VALUES (?, ?, ?, ?, 'active', '', ?, ?, ?)",
                (
                    str(item_id), revision, prior["content"], prior["metadata_json"],
                    str(actor or "user"), prior["content_sha256"], now,
                ),
            )
            conn.execute(
                "UPDATE session_memory_item SET current_revision=?, state='active', updated_at=? "
                "WHERE item_id=?", (revision, now, str(item_id)),
            )
            conn.commit()
        self._rebuild_search_index()
        return {"item_id": str(item_id), "revision": revision, "state": "active"}

    def remember_explicit(
        self,
        chat_id: str,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        scope: str = "user",
        actor: str = "user",
    ) -> dict[str, Any]:
        """Publish user-authored memory through the same revision ledger."""

        proposed = self.propose(
            chat_id,
            kind="add",
            content=content,
            scope=scope,
            provenance=f"explicit:{chat_id}",
            confidence=1.0,
            metadata=dict(metadata or {}),
        )
        return self.approve(str(proposed["proposal_id"]), actor=actor)

    def list_items(
        self,
        *,
        chat_id: str = "",
        limit: int = 200,
        offset: int = 0,
        include_tombstoned: bool = False,
        item_type: str = "",
        exclude_type: str = "",
    ) -> list[dict[str, Any]]:
        clauses = [] if include_tombstoned else ["i.state='active'"]
        params: list[Any] = []
        if chat_id:
            clauses.append("(i.scope='user' OR i.owner_chat_id=?)")
            params.append(str(chat_id))
        if item_type:
            clauses.append("COALESCE(json_extract(r.metadata_json,'$.type'),'fact')=?")
            params.append(str(item_type))
        if exclude_type:
            clauses.append("COALESCE(json_extract(r.metadata_json,'$.type'),'fact')<>?")
            params.append(str(exclude_type))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([
            max(1, min(int(limit), 500)),
            max(0, int(offset)),
        ])
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT i.*,r.content,r.metadata_json,r.content_sha256," 
                "r.created_at AS revision_created_at FROM session_memory_item i "
                "JOIN session_memory_revision r ON r.item_id=i.item_id "
                "AND r.revision=i.current_revision" + where
                + " ORDER BY i.updated_at DESC,i.item_id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> dict[str, Any]:
        metadata = json.loads(row["metadata_json"])
        return {
            "scope": str(row["scope"]),
            "owner_chat_id": str(row["owner_chat_id"]),
            "id": str(row["item_id"]),
            "item_id": str(row["item_id"]),
            "text": str(row["content"]),
            "content": str(row["content"]),
            "type": str(metadata.get("type") or "fact"),
            "context": str(metadata.get("context") or ""),
            "importance": int(metadata.get("importance") or 3),
            "confidence": float(metadata.get("confidence") or 1.0),
            "subject": str(metadata.get("subject") or "user"),
            "source": str(metadata.get("source") or "manual"),
            "tags": list(metadata.get("tags") or ()),
            "source_ids": list(metadata.get("source_ids") or ()),
            "state": str(row["state"]),
            "version": int(row["current_revision"]),
            "created": float(row["created_at"]),
            "updated": float(row["updated_at"]),
            "metadata": metadata,
            "content_sha256": str(row["content_sha256"]),
        }

    def get_item(
        self, item_id: str, *, include_tombstoned: bool = False
    ) -> dict[str, Any] | None:
        state_clause = "" if include_tombstoned else " AND i.state='active'"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT i.*,r.content,r.metadata_json,r.content_sha256,"
                "r.created_at AS revision_created_at FROM session_memory_item i "
                "JOIN session_memory_revision r ON r.item_id=i.item_id "
                "AND r.revision=i.current_revision WHERE i.item_id=?" + state_clause,
                (str(item_id),),
            ).fetchone()
        return self._item_from_row(row) if row is not None else None

    def find_profile(self, content: str) -> dict[str, Any] | None:
        """Find an exact profile fact without imposing a hidden result cap."""

        offset = 0
        while True:
            page = self.list_items(limit=500, offset=offset, item_type="profile")
            for item in page:
                if item["content"] == str(content):
                    return item
            if len(page) < 500:
                return None
            offset += len(page)

    def count_items(self, *, include_tombstoned: bool = False) -> int:
        where = "" if include_tombstoned else " WHERE state='active'"
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM session_memory_item" + where
            ).fetchone()
        return int(row["n"] if row is not None else 0)

    def consolidate_exact_duplicates(self, *, actor: str = "system:dedup") -> int:
        """Tombstone exact normalized duplicates through the revision ledger."""

        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.list_items(limit=500, offset=offset)
            items.extend(page)
            if len(page) < 500:
                break
            offset += len(page)
        seen: set[tuple] = set()
        removed = 0
        for item in reversed(items):
            content_key = " ".join(str(item.get("content") or "").casefold().split())
            key = (content_key, item["scope"], item["owner_chat_id"], item["type"],
                   _stable(item.get("metadata") or {}))
            if not content_key or key in seen:
                if content_key:
                    self.tombstone(
                        str(item["item_id"]),
                        expected_revision=int(item["version"]),
                        actor=actor,
                        reason="exact duplicate",
                    )
                    removed += 1
                continue
            seen.add(key)
        return removed

    def clear(
        self, *, actor: str = "user", preserve_profile: bool = False
    ) -> int:
        cleared = 0
        while True:
            items = self.list_items(
                limit=500,
                include_tombstoned=False,
                exclude_type="profile" if preserve_profile else "",
            )
            for item in items:
                self.tombstone(
                    item["item_id"], expected_revision=int(item["version"]),
                    actor=actor, reason="explicit clear",
                )
                cleared += 1
            if len(items) < 500:
                break
        return cleared

    def export_jsonl(self, destination: str) -> int:
        folder = os.path.dirname(os.path.abspath(destination)) or "."
        os.makedirs(folder, exist_ok=True)
        temporary = destination + ".tmp"
        count = 0
        try:
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                offset = 0
                while True:
                    page = self.list_items(
                        limit=500, offset=offset, include_tombstoned=True
                    )
                    for item in page:
                        handle.write(_stable(item) + "\n")
                    count += len(page)
                    if len(page) < 500:
                        break
                    offset += len(page)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        return count

    def dev_reset(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM session_memory_proposal")
            conn.execute("DELETE FROM session_memory_revision")
            conn.execute("DELETE FROM session_memory_item")
        self._rebuild_search_index()

    def profile_items(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {"text": item["content"], "ts": item["updated"]}
            for item in self.list_items(limit=limit, item_type="profile")
        ]

    @staticmethod
    def _eligible(scope: str, owner: str, metadata: dict, *, chat_id: str, now: float) -> bool:
        if scope != "user" and not (chat_id and scope == "chat" and owner == chat_id):
            return False
        try:
            expiry = float(metadata.get("expires_at") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        return not expiry or expiry > now

    def render_profile(self, *, limit: int = 100, chat_id: str = "") -> str:
        rows, offset, now = [], 0, time.time()
        cap = max(1, min(int(limit), 500))
        while len(rows) < cap:
            page = self.list_items(limit=500, offset=offset, item_type="profile")
            for item in page:
                if self._eligible(item["scope"], item["owner_chat_id"], item["metadata"],
                                  chat_id=str(chat_id), now=now):
                    rows.append(item["content"])
                    if len(rows) >= cap:
                        break
            if len(page) < 500:
                break
            offset += len(page)
        return "\n".join(f"- {text}" for text in rows)

    def retrieve(self, chat_id: str, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        terms = set(_WORDS.findall(str(query or "").casefold()))
        now = time.time()
        cap = max(1, min(int(limit), 50))
        with self._lock, self._connect() as conn:
            if terms and self.fts_enabled:
                rows = conn.execute(
                    "SELECT i.item_id,i.current_revision,i.scope,i.owner_chat_id,"
                    "r.content,r.metadata_json,r.content_sha256,r.created_at "
                    "FROM memory_fts f JOIN session_memory_item i "
                    "ON i.item_id=f.item_id JOIN session_memory_revision r "
                    "ON r.item_id=i.item_id AND r.revision=i.current_revision "
                    "WHERE memory_fts MATCH ? AND i.state='active' "
                    "AND (i.scope='user' OR i.owner_chat_id=?) "
                    "ORDER BY bm25(memory_fts),r.created_at DESC LIMIT ?",
                    (" OR ".join(sorted(terms)), str(chat_id), cap * 4),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT i.item_id,i.current_revision,i.scope,i.owner_chat_id,"
                    "r.content,r.metadata_json,r.content_sha256,r.created_at "
                    "FROM session_memory_item i JOIN session_memory_revision r "
                    "ON r.item_id=i.item_id AND r.revision=i.current_revision "
                    "WHERE i.state='active' AND (i.scope='user' OR i.owner_chat_id=?) "
                    "ORDER BY r.created_at DESC LIMIT ?",
                    (str(chat_id), cap * 4),
                ).fetchall()
        ranked: list[tuple[int, float, dict[str, Any]]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            if not self._eligible(str(row["scope"]), str(row["owner_chat_id"]), metadata,
                                  chat_id=str(chat_id), now=now):
                continue
            haystack = set(_WORDS.findall(str(row["content"]).casefold()))
            score = len(terms & haystack) if terms else 0
            if terms and score <= 0:
                continue
            ranked.append((-score, -float(row["created_at"]), {
                "item_id": row["item_id"],
                "revision": int(row["current_revision"]),
                "content": row["content"],
                "metadata": metadata,
                "content_sha256": row["content_sha256"],
                "created_at": float(row["created_at"]),
                "trust_label": "user_approved_memory_untrusted_payload",
            }))
        ranked.sort(key=lambda item: item[:2])
        return [row for _score, _created, row in ranked[:cap]]

    def status(self, *, chat_id: str = "", limit: int = 50) -> dict[str, Any]:
        cap = max(1, min(int(limit), 200))
        with self._lock, self._connect() as conn:
            if chat_id:
                proposals = conn.execute(
                    "SELECT proposal_id, chat_id, kind, item_id, expected_revision, "
                    "proposal_digest, state, reviewed_by, review_note, created_at, reviewed_at "
                    "FROM session_memory_proposal WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
                    (str(chat_id), cap),
                ).fetchall()
            else:
                proposals = conn.execute(
                    "SELECT proposal_id, chat_id, kind, item_id, expected_revision, "
                    "proposal_digest, state, reviewed_by, review_note, created_at, reviewed_at "
                    "FROM session_memory_proposal ORDER BY created_at DESC LIMIT ?", (cap,)
                ).fetchall()
            items = conn.execute(
                "SELECT item_id, current_revision, state, scope, owner_chat_id, created_at, "
                "updated_at FROM session_memory_item ORDER BY updated_at DESC LIMIT ?", (cap,)
            ).fetchall()
        return {
            "schema": "variant1.memory.v1",
            "proposals": [dict(row) for row in proposals],
            "items": [dict(row) for row in items],
            "writes_require_user_approval": True,
            "deletion_is_reversible_tombstone": True,
        }

    def delete_chat(self, chat_id: str) -> None:
        """Remove unapproved chat-local proposals; approved user memory survives."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM session_memory_proposal WHERE chat_id=? AND state='pending'",
                (str(chat_id),),
            )


__all__ = ["MemoryStore", "MemoryConflict"]
