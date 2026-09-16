"""SQLite ownership for peer endpoints, messages, exchanges, and receipts."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

from .delivery import MESSAGE_KINDS, GROK_DELIVERY_MODES, SENDER_EVIDENCE_FIELDS


MESSAGE_STATES = frozenset({
    "persisted", "queued", "transport_written", "observed", "replied",
    "parked", "failed", "unknown",
})
MESSAGE_DIRECTIONS = frozenset({"incoming", "outgoing", "all"})
CONNECTION_STATES = frozenset({
    "active", "conflicted", "closed", "expired",
})


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    )


def _object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class PeerRepository:
    """One durable message log and external endpoint registry."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(str(path))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS peer_clock(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO peer_clock(singleton,revision) VALUES(1,0);

                CREATE TABLE IF NOT EXISTS peer_delivery_preference(
                    peer_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS peer_endpoint(
                    peer_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    chat_id TEXT NOT NULL DEFAULT '',
                    adapter TEXT NOT NULL DEFAULT '',
                    external_session_id TEXT NOT NULL DEFAULT '',
                    terminal_id TEXT NOT NULL DEFAULT '',
                    process_id TEXT NOT NULL DEFAULT '',
                    process_generation INTEGER NOT NULL DEFAULT 0,
                    connection_epoch INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS peer_endpoint_adapter
                    ON peer_endpoint(adapter,status,peer_id);

                CREATE TABLE IF NOT EXISTS peer_message(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    exchange_id TEXT NOT NULL,
                    sender_peer_id TEXT NOT NULL,
                    target_peer_id TEXT NOT NULL,
                    in_reply_to TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    delivery TEXT NOT NULL,
                    state TEXT NOT NULL,
                    delivery_ticket_id TEXT NOT NULL DEFAULT '',
                    target_run_id TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    claim_adapter TEXT NOT NULL DEFAULT '',
                    claim_connection_id TEXT NOT NULL DEFAULT '',
                    claim_connection_epoch INTEGER NOT NULL DEFAULT 0,
                    claim_at REAL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    replied_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS peer_message_request
                    ON peer_message(sender_peer_id,request_id)
                    WHERE request_id!='';
                CREATE INDEX IF NOT EXISTS peer_message_inbox
                    ON peer_message(target_peer_id,sequence);
                CREATE INDEX IF NOT EXISTS peer_message_outbox
                    ON peer_message(sender_peer_id,sequence);
                CREATE INDEX IF NOT EXISTS peer_message_sender_run
                    ON peer_message(sender_peer_id,json_extract(evidence_json,'$.sender_invocation.run_id'),sequence);
                CREATE INDEX IF NOT EXISTS peer_message_delivery
                    ON peer_message(target_peer_id,state,sequence);

                CREATE TABLE IF NOT EXISTS peer_connection(
                    connection_id TEXT PRIMARY KEY,
                    harness TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    process_id TEXT NOT NULL DEFAULT '',
                    process_started_at REAL NOT NULL DEFAULT 0,
                    runtime_id TEXT NOT NULL,
                    runtime_pid TEXT NOT NULL DEFAULT '',
                    runtime_started_at REAL NOT NULL DEFAULT 0,
                    epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    lease_seconds REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    revision INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_at REAL
                );
                CREATE INDEX IF NOT EXISTS peer_connection_identity
                    ON peer_connection(harness,native_session_id,status,last_seen);
                CREATE INDEX IF NOT EXISTS peer_connection_peer
                    ON peer_connection(peer_id,status,last_seen);
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(peer_message)")
            }
            if "claim_connection_id" not in columns:
                conn.execute(
                    "ALTER TABLE peer_message ADD COLUMN "
                    "claim_connection_id TEXT NOT NULL DEFAULT ''"
                )
            if "message_kind" not in columns:
                # Existing messages retain their originally requested wake semantics.
                conn.execute("ALTER TABLE peer_message ADD COLUMN message_kind TEXT NOT NULL DEFAULT 'request'")

    @staticmethod
    def _tick(conn: sqlite3.Connection) -> int:
        conn.execute(
            "UPDATE peer_clock SET revision=revision+1 WHERE singleton=1"
        )
        return int(conn.execute(
            "SELECT revision FROM peer_clock WHERE singleton=1"
        ).fetchone()["revision"])

    def delivery_preference(self, peer_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT mode FROM peer_delivery_preference WHERE peer_id=?", (peer_id,)).fetchone()
        return str(row["mode"]) if row and row["mode"] in GROK_DELIVERY_MODES else "inbox"

    def set_delivery_preference(self, peer_id: str, mode: str, *, expected: str) -> dict:
        if mode not in GROK_DELIVERY_MODES or expected not in GROK_DELIVERY_MODES:
            raise ValueError("invalid peer delivery mode")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM peer_delivery_preference WHERE peer_id=?", (peer_id,)).fetchone()
            current = str(row["mode"]) if row else "inbox"
            if current != expected and current != mode:
                conn.rollback()
                raise RuntimeError("peer delivery preference changed; refresh before changing it")
            revision = int(row["revision"]) if row else 0
            if current != mode:
                revision = self._tick(conn)
                conn.execute(
                    "INSERT INTO peer_delivery_preference VALUES(?,?,?,?) "
                    "ON CONFLICT(peer_id) DO UPDATE SET mode=excluded.mode,revision=excluded.revision,updated_at=excluded.updated_at",
                    (peer_id, mode, revision, time.time()),
                )
            conn.commit()
        return {"peer_id": peer_id, "preferred_delivery_mode": mode, "revision": revision}

    @staticmethod
    def _endpoint(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "peer_id": str(row["peer_id"]),
            "kind": str(row["kind"]),
            "display_name": str(row["display_name"]),
            "chat_id": str(row["chat_id"] or ""),
            "adapter": str(row["adapter"] or ""),
            "external_session_id": str(row["external_session_id"] or ""),
            "terminal_id": str(row["terminal_id"] or ""),
            "process_id": str(row["process_id"] or ""),
            "process_generation": int(row["process_generation"] or 0),
            "connection_epoch": int(row["connection_epoch"] or 0),
            "status": str(row["status"]),
            "capabilities": _object(row["capabilities_json"]),
            "metadata": _object(row["metadata_json"]),
            "revision": int(row["revision"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _message(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "sequence": int(row["sequence"]),
            "message_id": str(row["message_id"]),
            "exchange_id": str(row["exchange_id"]),
            "sender_peer_id": str(row["sender_peer_id"]),
            "target_peer_id": str(row["target_peer_id"]),
            "in_reply_to": str(row["in_reply_to"] or ""),
            "content": str(row["content"]),
            "message_kind": str(row["message_kind"]),
            "delivery": str(row["delivery"]),
            "state": str(row["state"]),
            "delivery_ticket_id": str(row["delivery_ticket_id"] or ""),
            "target_run_id": str(row["target_run_id"] or ""),
            "request_id": str(row["request_id"] or ""),
            "claim_adapter": str(row["claim_adapter"] or ""),
            "claim_connection_id": str(row["claim_connection_id"] or ""),
            "claim_connection_epoch": int(row["claim_connection_epoch"] or 0),
            "claim_at": float(row["claim_at"] or 0),
            "evidence": _object(row["evidence_json"]),
            "error": str(row["error"] or ""),
            "revision": int(row["revision"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "replied_at": float(row["replied_at"] or 0),
        }

    @staticmethod
    def _connection(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "connection_id": str(row["connection_id"]),
            "harness": str(row["harness"]),
            "native_session_id": str(row["native_session_id"]),
            "peer_id": str(row["peer_id"]),
            "process_id": str(row["process_id"] or ""),
            "process_started_at": float(row["process_started_at"] or 0),
            "runtime_id": str(row["runtime_id"]),
            "runtime_pid": str(row["runtime_pid"] or ""),
            "runtime_started_at": float(row["runtime_started_at"] or 0),
            "epoch": int(row["epoch"]),
            "status": str(row["status"]),
            "last_seen": float(row["last_seen"]),
            "lease_seconds": float(row["lease_seconds"]),
            "metadata": _object(row["metadata_json"]),
            "revision": int(row["revision"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "closed_at": float(row["closed_at"] or 0),
        }

    def revision(self) -> int:
        with self._connect() as conn:
            return int(conn.execute(
                "SELECT revision FROM peer_clock WHERE singleton=1"
            ).fetchone()["revision"])

    def register_external(self, record: Mapping[str, Any]) -> dict[str, Any]:
        peer_id = str(record.get("peer_id") or "").strip()
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM peer_endpoint WHERE peer_id=?", (peer_id,)
            ).fetchone()
            epoch = int(record.get("connection_epoch") or 0)
            if prior is not None and epoch < int(prior["connection_epoch"] or 0):
                conn.rollback()
                raise RuntimeError("stale external peer connection epoch")
            if prior is not None and epoch == int(prior["connection_epoch"] or 0):
                identity = (
                    ("adapter", str(record.get("adapter") or "")),
                    ("external_session_id", str(record.get("external_session_id") or "")),
                    ("terminal_id", str(record.get("terminal_id") or "")),
                    ("process_id", str(record.get("process_id") or "")),
                    ("process_generation", int(record.get("process_generation") or 0)),
                )
                if any(prior[name] != value for name, value in identity):
                    conn.rollback()
                    raise RuntimeError(
                        "external peer identity changed without a new connection epoch"
                    )
            revision = self._tick(conn)
            created = float(prior["created_at"]) if prior is not None else now
            conn.execute(
                "INSERT INTO peer_endpoint(peer_id,kind,display_name,chat_id,adapter,"
                "external_session_id,terminal_id,process_id,process_generation,"
                "connection_epoch,status,capabilities_json,metadata_json,revision,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(peer_id) DO UPDATE SET kind=excluded.kind,"
                "display_name=excluded.display_name,chat_id=excluded.chat_id,"
                "adapter=excluded.adapter,external_session_id=excluded.external_session_id,"
                "terminal_id=excluded.terminal_id,process_id=excluded.process_id,"
                "process_generation=excluded.process_generation,"
                "connection_epoch=excluded.connection_epoch,status=excluded.status,"
                "capabilities_json=excluded.capabilities_json,"
                "metadata_json=excluded.metadata_json,revision=excluded.revision,"
                "updated_at=excluded.updated_at",
                (
                    peer_id, "external_harness", str(record.get("display_name") or peer_id),
                    "", str(record.get("adapter") or ""),
                    str(record.get("external_session_id") or ""),
                    str(record.get("terminal_id") or ""),
                    str(record.get("process_id") or ""),
                    int(record.get("process_generation") or 0), epoch,
                    str(record.get("status") or "connected"),
                    _json(dict(record.get("capabilities") or {})),
                    _json(dict(record.get("metadata") or {})), revision, created, now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM peer_endpoint WHERE peer_id=?", (peer_id,)
            ).fetchone()
        return self._endpoint(row) or {}

    def get_endpoint(self, peer_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._endpoint(conn.execute(
                "SELECT * FROM peer_endpoint WHERE peer_id=?", (str(peer_id),)
            ).fetchone())

    def update_external(
        self, peer_id: str, expected_connection_epoch: int,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM peer_endpoint WHERE peer_id=?", (str(peer_id),)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError(f"unknown external peer: {peer_id}")
            if int(row["connection_epoch"] or 0) != int(expected_connection_epoch):
                conn.rollback()
                raise RuntimeError("external peer connection epoch changed")
            for field in (
                "external_session_id", "terminal_id", "process_id",
                "process_generation",
            ):
                requested = fields.get(field)
                if requested in (None, "", 0):
                    continue
                expected = int(row[field] or 0) if field == "process_generation" else str(row[field] or "")
                value = int(requested) if field == "process_generation" else str(requested)
                if value != expected:
                    conn.rollback()
                    raise RuntimeError(
                        "external peer identity update requires a new connection epoch"
                    )
            capabilities = (
                dict(fields["capabilities"])
                if isinstance(fields.get("capabilities"), Mapping)
                else _object(row["capabilities_json"])
            )
            metadata = (
                dict(fields["metadata"])
                if isinstance(fields.get("metadata"), Mapping)
                else _object(row["metadata_json"])
            )
            values = {
                "status": str(fields.get("status") or row["status"]),
                "external_session_id": str(row["external_session_id"] or ""),
                "terminal_id": str(row["terminal_id"] or ""),
                "process_id": str(row["process_id"] or ""),
                "process_generation": int(row["process_generation"] or 0),
            }
            revision = self._tick(conn)
            conn.execute(
                "UPDATE peer_endpoint SET status=?,external_session_id=?,terminal_id=?,"
                "process_id=?,process_generation=?,capabilities_json=?,metadata_json=?,"
                "revision=?,updated_at=? WHERE peer_id=? AND connection_epoch=?",
                (
                    values["status"], values["external_session_id"],
                    values["terminal_id"], values["process_id"],
                    values["process_generation"], _json(capabilities), _json(metadata),
                    revision, time.time(), str(peer_id), int(expected_connection_epoch),
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM peer_endpoint WHERE peer_id=?", (str(peer_id),)
            ).fetchone()
        return self._endpoint(updated) or {}

    def list_endpoints(self, *, adapter: str = "", limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = [], []
        if adapter:
            clauses.append("adapter=?")
            params.append(str(adapter))
        sql = "SELECT * FROM peer_endpoint"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY display_name,peer_id LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._connect() as conn:
            return [self._endpoint(row) or {} for row in conn.execute(sql, params)]

    def persist_message(self, record: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        kind = str(record.get("message_kind") or "request")
        if kind not in MESSAGE_KINDS:
            raise ValueError("invalid peer message_kind")
        message_id = str(record.get("message_id") or "peer_message_" + uuid.uuid4().hex)
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM peer_message WHERE message_id=?", (message_id,)
            ).fetchone()
            if prior is not None:
                same = all(str(prior[key] or "") == str(record.get(key) or "") for key in (
                    "sender_peer_id", "target_peer_id", "in_reply_to", "content", "delivery",
                ))
                same = same and str(prior["message_kind"]) == kind
                if not same:
                    conn.rollback()
                    raise RuntimeError("peer message identity conflicts with prior content")
                conn.rollback()
                return self._message(prior) or {}, False
            revision = self._tick(conn)
            conn.execute(
                "INSERT INTO peer_message(message_id,exchange_id,sender_peer_id,"
                "target_peer_id,in_reply_to,content,delivery,state,request_id,"
                "evidence_json,revision,created_at,updated_at,message_kind) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id, str(record.get("exchange_id") or message_id),
                    str(record.get("sender_peer_id") or ""),
                    str(record.get("target_peer_id") or ""),
                    str(record.get("in_reply_to") or ""),
                    str(record.get("content") or ""),
                    str(record.get("delivery") or "follow_up"),
                    str(record.get("state") or "persisted"),
                    str(record.get("request_id") or ""),
                    _json(dict(record.get("evidence") or {})), revision, now, now, kind,
                ),
            )
            if record.get("in_reply_to"):
                parent = conn.execute(
                    "SELECT * FROM peer_message WHERE message_id=?",
                    (str(record["in_reply_to"]),),
                ).fetchone()
                if parent is not None:
                    parent_revision = self._tick(conn)
                    conn.execute(
                        "UPDATE peer_message SET state='replied',replied_at=?,"
                        "claim_adapter='',claim_connection_id='',"
                        "claim_connection_epoch=0,claim_at=NULL,"
                        "revision=?,updated_at=? WHERE message_id=?",
                        (now, parent_revision, now, str(record["in_reply_to"])),
                    )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM peer_message WHERE message_id=?", (message_id,)
            ).fetchone()
        return self._message(row) or {}, True

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._message(conn.execute(
                "SELECT * FROM peer_message WHERE message_id=?", (str(message_id),)
            ).fetchone())

    def messages_for_sender_run(self, peer_id: str, run_id: str, *, limit: int = 201) -> list[dict]:
        """Indexed exact-origin lookup for the sender's activity projection."""
        with self._connect() as conn:
            return [self._message(row) or {} for row in conn.execute(
                "SELECT * FROM peer_message WHERE sender_peer_id=? "
                "AND json_extract(evidence_json,'$.sender_invocation.run_id')=? "
                "ORDER BY sequence LIMIT ?", (peer_id, run_id, max(1, min(int(limit), 501))),
            )]

    def get_message_by_request(
        self, sender_peer_id: str, request_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._message(conn.execute(
                "SELECT * FROM peer_message WHERE sender_peer_id=? AND request_id=?",
                (str(sender_peer_id), str(request_id)),
            ).fetchone())

    def find_reply(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._message(conn.execute(
                "SELECT * FROM peer_message WHERE in_reply_to=? "
                "ORDER BY sequence LIMIT 1", (str(message_id),),
            ).fetchone())

    def list_messages(
        self, peer_id: str, *, after: int = 0, limit: int = 50,
        direction: str = "incoming", states: tuple[str, ...] = (),
        message_kind: str = "",
    ) -> list[dict[str, Any]]:
        if direction not in MESSAGE_DIRECTIONS:
            raise ValueError("peer message direction must be incoming, outgoing, or all")
        clauses = ["sequence>?"]
        params: list[Any] = [max(0, int(after))]
        if direction == "incoming":
            clauses.append("target_peer_id=?")
            params.append(str(peer_id))
        elif direction == "outgoing":
            clauses.append("sender_peer_id=?")
            params.append(str(peer_id))
        else:
            clauses.append("(target_peer_id=? OR sender_peer_id=?)")
            params.extend((str(peer_id), str(peer_id)))
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            params.extend(states)
        if message_kind:
            if message_kind not in MESSAGE_KINDS:
                raise ValueError("invalid peer message_kind")
            clauses.append("message_kind=?")
            params.append(message_kind)
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM peer_message WHERE " + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?", params,
            ).fetchall()
        return [self._message(row) or {} for row in rows]

    def list_history(
        self, peer_id: str, other_peer_id: str, *, after: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read one exact pairwise exchange without unrelated-page starvation."""

        first = str(peer_id)
        second = str(other_peer_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM peer_message WHERE sequence>? AND "
                "((sender_peer_id=? AND target_peer_id=?) OR "
                "(sender_peer_id=? AND target_peer_id=?)) "
                "ORDER BY sequence LIMIT ?",
                (
                    max(0, int(after)), first, second, second, first,
                    max(1, min(int(limit), 500)),
                ),
            ).fetchall()
        return [self._message(row) or {} for row in rows]

    def update_message(
        self, message_id: str, *, state: str | None = None,
        delivery_ticket_id: str | None = None, target_run_id: str | None = None,
        evidence: Mapping[str, Any] | None = None, error: str | None = None,
        clear_claim: bool = False,
    ) -> dict[str, Any]:
        if state is not None and state not in MESSAGE_STATES:
            raise ValueError(f"unsupported peer message state: {state}")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM peer_message WHERE message_id=?", (str(message_id),)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError(f"unknown peer message: {message_id}")
            values = {
                "state": str(state if state is not None else row["state"]),
                "delivery_ticket_id": str(
                    delivery_ticket_id if delivery_ticket_id is not None
                    else row["delivery_ticket_id"] or ""
                ),
                "target_run_id": str(
                    target_run_id if target_run_id is not None
                    else row["target_run_id"] or ""
                ),
                "evidence_json": _json(
                    {
                        **{key: value for key, value in (
                            dict(evidence) if evidence is not None else _object(row["evidence_json"])
                        ).items() if key not in SENDER_EVIDENCE_FIELDS},
                        # Sender attribution belongs to the committed send.
                        # Later delivery receipts cannot replace or erase it.
                        **{key: value for key, value in _object(row["evidence_json"]).items()
                           if key in SENDER_EVIDENCE_FIELDS},
                    }
                ),
                "error": str(error if error is not None else row["error"] or ""),
            }
            revision = self._tick(conn)
            conn.execute(
                "UPDATE peer_message SET state=?,delivery_ticket_id=?,target_run_id=?,"
                "evidence_json=?,error=?,claim_adapter=CASE WHEN ? THEN '' ELSE claim_adapter END,"
                "claim_connection_id=CASE WHEN ? THEN '' ELSE claim_connection_id END,"
                "claim_connection_epoch=CASE WHEN ? THEN 0 ELSE claim_connection_epoch END,"
                "claim_at=CASE WHEN ? THEN NULL ELSE claim_at END,revision=?,updated_at=? "
                "WHERE message_id=?",
                (
                    values["state"], values["delivery_ticket_id"],
                    values["target_run_id"], values["evidence_json"], values["error"],
                    int(clear_claim), int(clear_claim), int(clear_claim),
                    int(clear_claim), revision,
                    time.time(), str(message_id),
                ),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM peer_message WHERE message_id=?", (str(message_id),)
            ).fetchone()
        return self._message(updated) or {}

    def claim_external(
        self, *, adapter: str, connection_epoch: int, limit: int,
        peer_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            endpoint_clauses = ["adapter=?", "connection_epoch=?", "status='connected'"]
            endpoint_params: list[Any] = [str(adapter), int(connection_epoch)]
            if peer_id:
                endpoint_clauses.append("peer_id=?")
                endpoint_params.append(str(peer_id))
            endpoints = [str(row["peer_id"]) for row in conn.execute(
                "SELECT peer_id FROM peer_endpoint WHERE "
                + " AND ".join(endpoint_clauses), endpoint_params,
            ).fetchall()]
            if not endpoints:
                conn.rollback()
                return []
            rows = conn.execute(
                "SELECT * FROM peer_message WHERE target_peer_id IN ("
                + ",".join("?" for _ in endpoints)
                + ") AND state='queued' AND claim_connection_epoch=0 "
                "ORDER BY sequence LIMIT ?",
                (*endpoints, max(1, min(int(limit), 100))),
            ).fetchall()
            now = time.time()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                revision = self._tick(conn)
                conn.execute(
                    "UPDATE peer_message SET claim_adapter=?,claim_connection_epoch=?,"
                    "claim_at=?,revision=?,updated_at=? WHERE message_id=? "
                    "AND claim_connection_epoch=0",
                    (str(adapter), int(connection_epoch), now, revision, now, row["message_id"]),
                )
                claimed_row = conn.execute(
                    "SELECT * FROM peer_message WHERE message_id=?", (row["message_id"],)
                ).fetchone()
                claimed.append(self._message(claimed_row) or {})
            conn.commit()
        return claimed

    def _fence_connection_claims_tx(
        self, conn: sqlite3.Connection, connection_ids: list[str], *,
        reason: str, now: float,
    ) -> list[dict[str, Any]]:
        clean = sorted({str(item) for item in connection_ids if str(item)})
        if not clean:
            return []
        rows = conn.execute(
            "SELECT * FROM peer_message WHERE claim_connection_id IN ("
            + ",".join("?" for _ in clean)
            + ") AND state IN ('queued','transport_written','observed') "
            "ORDER BY sequence",
            clean,
        ).fetchall()
        updated: list[dict[str, Any]] = []
        for row in rows:
            revision = self._tick(conn)
            conn.execute(
                "UPDATE peer_message SET state='unknown',claim_adapter='',"
                "claim_connection_id='',claim_connection_epoch=0,claim_at=NULL,"
                "error=?,revision=?,updated_at=? WHERE message_id=?",
                (str(reason)[:1000], revision, now, row["message_id"]),
            )
            current = conn.execute(
                "SELECT * FROM peer_message WHERE message_id=?",
                (row["message_id"],),
            ).fetchone()
            updated.append(self._message(current) or {})
        return updated

    def _endpoint_status_tx(
        self, conn: sqlite3.Connection, peer_id: str, status: str, *, now: float,
    ) -> None:
        row = conn.execute(
            "SELECT status FROM peer_endpoint WHERE peer_id=?", (str(peer_id),)
        ).fetchone()
        if row is None or str(row["status"]) == str(status):
            return
        revision = self._tick(conn)
        conn.execute(
            "UPDATE peer_endpoint SET status=?,revision=?,updated_at=? "
            "WHERE peer_id=?",
            (str(status), revision, now, str(peer_id)),
        )

    def _reconcile_connection_group_tx(
        self, conn: sqlite3.Connection, harness: str, native_session_id: str,
        *, now: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        live = conn.execute(
            "SELECT * FROM peer_connection WHERE harness=? "
            "AND native_session_id=? AND status IN ('active','conflicted') "
            "AND last_seen+lease_seconds>? ORDER BY created_at,connection_id",
            (str(harness), str(native_session_id), now),
        ).fetchall()
        runtime_ids = sorted({str(row["runtime_id"]) for row in live})
        desired = "active" if len(runtime_ids) == 1 else "conflicted"
        changed: list[dict[str, Any]] = []
        for row in live:
            if str(row["status"]) == desired:
                continue
            revision = self._tick(conn)
            conn.execute(
                "UPDATE peer_connection SET status=?,revision=?,updated_at=? "
                "WHERE connection_id=?",
                (desired, revision, now, row["connection_id"]),
            )
            current = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (row["connection_id"],),
            ).fetchone()
            changed.append(self._connection(current) or {})
        fenced: list[dict[str, Any]] = []
        if len(runtime_ids) > 1:
            fenced = self._fence_connection_claims_tx(
                conn, [str(row["connection_id"]) for row in live],
                reason=(
                    "external delivery ownership conflicted across live "
                    "runtime actors"
                ),
                now=now,
            )
        peer_ids = sorted({str(row["peer_id"]) for row in live})
        if not peer_ids:
            prior = conn.execute(
                "SELECT DISTINCT peer_id FROM peer_connection WHERE harness=? "
                "AND native_session_id=?",
                (str(harness), str(native_session_id)),
            ).fetchall()
            peer_ids = [str(row["peer_id"]) for row in prior]
        endpoint_status = (
            "connected" if len(runtime_ids) == 1
            else "conflicted" if len(runtime_ids) > 1
            else "disconnected"
        )
        for peer_id in peer_ids:
            self._endpoint_status_tx(
                conn, peer_id, endpoint_status, now=now,
            )
        return changed, fenced

    def _expire_connections_tx(
        self, conn: sqlite3.Connection, *, now: float,
    ) -> dict[str, list[dict[str, Any]]]:
        stale = conn.execute(
            "SELECT * FROM peer_connection WHERE status IN ('active','conflicted') "
            "AND last_seen+lease_seconds<=? ORDER BY updated_at,connection_id",
            (now,),
        ).fetchall()
        changed: list[dict[str, Any]] = []
        fenced: list[dict[str, Any]] = []
        groups: set[tuple[str, str]] = set()
        for row in stale:
            revision = self._tick(conn)
            conn.execute(
                "UPDATE peer_connection SET status='expired',closed_at=?,"
                "revision=?,updated_at=? WHERE connection_id=?",
                (now, revision, now, row["connection_id"]),
            )
            current = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (row["connection_id"],),
            ).fetchone()
            changed.append(self._connection(current) or {})
            fenced.extend(self._fence_connection_claims_tx(
                conn, [str(row["connection_id"])],
                reason="external delivery connection lease expired", now=now,
            ))
            groups.add((str(row["harness"]), str(row["native_session_id"])))
        for harness, native_session_id in groups:
            promoted, split_fenced = self._reconcile_connection_group_tx(
                conn, harness, native_session_id, now=now,
            )
            changed.extend(promoted)
            fenced.extend(split_fenced)
        return {"connections": changed, "messages": fenced}

    def _decorate_connection(
        self, conn: sqlite3.Connection, row: sqlite3.Row | None, *, now: float,
    ) -> dict[str, Any] | None:
        item = self._connection(row)
        if item is None:
            return None
        conflicts = conn.execute(
            "SELECT DISTINCT runtime_id FROM peer_connection WHERE peer_id=? "
            "AND status='conflicted' AND last_seen+lease_seconds>? "
            "ORDER BY runtime_id",
            (item["peer_id"], now),
        ).fetchall()
        # Ownership is runtime-scoped. Multiple bridge processes under the
        # same proven harness actor may race this SQLite claim safely; the
        # message CAS admits exactly one. Different actors are conflicted.
        item["delivery_owner"] = (
            item["status"] == "active"
            and item["last_seen"] + item["lease_seconds"] > now
        )
        item["conflict_runtime_ids"] = [str(row["runtime_id"]) for row in conflicts]
        return item

    def register_connection(
        self, record: Mapping[str, Any], *, now: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        current = float(time.time() if now is None else now)
        connection_id = str(record.get("connection_id") or "").strip()
        harness = str(record.get("harness") or "").strip()
        native_session_id = str(record.get("native_session_id") or "").strip()
        peer_id = str(record.get("peer_id") or "").strip()
        runtime_id = str(record.get("runtime_id") or "").strip()
        epoch = int(record.get("epoch") or 0)
        lease_seconds = float(record.get("lease_seconds") or 0)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            effects = self._expire_connections_tx(conn, now=current)
            mapped = conn.execute(
                "SELECT DISTINCT peer_id FROM peer_connection WHERE harness=? "
                "AND native_session_id=?",
                (harness, native_session_id),
            ).fetchall()
            if any(str(row["peer_id"]) != peer_id for row in mapped):
                conn.rollback()
                raise RuntimeError(
                    "native harness session is already mapped to another peer"
                )
            endpoint = conn.execute(
                "SELECT * FROM peer_endpoint WHERE peer_id=?", (peer_id,)
            ).fetchone()
            if endpoint is not None and (
                str(endpoint["external_session_id"] or "") not in {"", native_session_id}
            ):
                conn.rollback()
                raise RuntimeError("peer id belongs to another native harness session")
            if endpoint is None:
                revision = self._tick(conn)
                conn.execute(
                    "INSERT INTO peer_endpoint(peer_id,kind,display_name,chat_id,adapter,"
                    "external_session_id,terminal_id,process_id,process_generation,"
                    "connection_epoch,status,capabilities_json,metadata_json,revision,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        peer_id, "external_harness",
                        str(record.get("display_name") or peer_id)[:200], "",
                        str(record.get("endpoint_adapter") or harness),
                        native_session_id, "", "", 0, 0, "disconnected",
                        _json(dict(record.get("capabilities") or {})), "{}",
                        revision, current, current,
                    ),
                )
            else:
                capabilities = (
                    dict(record.get("capabilities") or {})
                    or _object(endpoint["capabilities_json"])
                )
                revision = self._tick(conn)
                conn.execute(
                    "UPDATE peer_endpoint SET display_name=?,adapter=?,"
                    "external_session_id=?,terminal_id='',process_id='',"
                    "process_generation=0,connection_epoch=0,capabilities_json=?,"
                    "revision=?,updated_at=? "
                    "WHERE peer_id=?",
                    (
                        str(record.get("display_name") or endpoint["display_name"])[:200],
                        str(record.get("endpoint_adapter") or endpoint["adapter"]),
                        native_session_id, _json(capabilities), revision, current,
                        peer_id,
                    ),
                )
            prior = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            if prior is not None:
                if epoch < int(prior["epoch"]):
                    conn.rollback()
                    raise RuntimeError("stale peer connection epoch")
                if any(
                    str(prior[name]) != value
                    for name, value in (
                        ("harness", harness),
                        ("native_session_id", native_session_id),
                        ("peer_id", peer_id),
                    )
                ):
                    conn.rollback()
                    raise RuntimeError(
                        "peer connection id belongs to another logical peer"
                    )
                if (
                    epoch == int(prior["epoch"])
                    and str(prior["status"]) in {"closed", "expired"}
                ):
                    conn.rollback()
                    raise RuntimeError(
                        "closed peer connection requires a new epoch"
                    )
                identity = (
                    ("process_id", str(record.get("process_id") or "")),
                    ("process_started_at", float(record.get("process_started_at") or 0)),
                    ("runtime_id", runtime_id),
                    ("runtime_pid", str(record.get("runtime_pid") or "")),
                    ("runtime_started_at", float(record.get("runtime_started_at") or 0)),
                )
                if epoch == int(prior["epoch"]) and any(
                    prior[name] != value for name, value in identity
                ):
                    conn.rollback()
                    raise RuntimeError(
                        "peer connection identity changed without a new epoch"
                    )
                if epoch > int(prior["epoch"]):
                    effects["messages"].extend(self._fence_connection_claims_tx(
                        conn, [connection_id],
                        reason="external delivery connection epoch was replaced",
                        now=current,
                    ))
            revision = self._tick(conn)
            created_at = float(prior["created_at"]) if prior is not None else current
            conn.execute(
                "INSERT INTO peer_connection(connection_id,harness,native_session_id,"
                "peer_id,process_id,process_started_at,runtime_id,runtime_pid,"
                "runtime_started_at,epoch,status,last_seen,lease_seconds,metadata_json,"
                "revision,created_at,updated_at,closed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) "
                "ON CONFLICT(connection_id) DO UPDATE SET harness=excluded.harness,"
                "native_session_id=excluded.native_session_id,peer_id=excluded.peer_id,"
                "process_id=excluded.process_id,process_started_at=excluded.process_started_at,"
                "runtime_id=excluded.runtime_id,runtime_pid=excluded.runtime_pid,"
                "runtime_started_at=excluded.runtime_started_at,epoch=excluded.epoch,"
                "status='active',last_seen=excluded.last_seen,"
                "lease_seconds=excluded.lease_seconds,metadata_json=excluded.metadata_json,"
                "revision=excluded.revision,updated_at=excluded.updated_at,closed_at=NULL",
                (
                    connection_id, harness, native_session_id, peer_id,
                    str(record.get("process_id") or ""),
                    float(record.get("process_started_at") or 0), runtime_id,
                    str(record.get("runtime_pid") or ""),
                    float(record.get("runtime_started_at") or 0), epoch, "active",
                    current, lease_seconds, _json(dict(record.get("metadata") or {})),
                    revision, created_at, current,
                ),
            )
            changed, fenced = self._reconcile_connection_group_tx(
                conn, harness, native_session_id, now=current,
            )
            effects["connections"].extend(changed)
            effects["messages"].extend(fenced)
            row = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            result = self._decorate_connection(conn, row, now=current) or {}
            conn.commit()
        return result, effects

    def touch_connection(
        self, connection_id: str, expected_epoch: int, *,
        metadata: Mapping[str, Any] | None = None, now: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        current = float(time.time() if now is None else now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            effects = self._expire_connections_tx(conn, now=current)
            row = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (str(connection_id),),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError(f"unknown peer connection: {connection_id}")
            if int(row["epoch"]) != int(expected_epoch):
                conn.rollback()
                raise RuntimeError("peer connection epoch changed")
            if str(row["status"]) in {"closed", "expired"}:
                conn.rollback()
                raise RuntimeError("peer connection lease is no longer renewable")
            stored_metadata = (
                dict(metadata) if metadata is not None
                else _object(row["metadata_json"])
            )
            revision = (
                self._tick(conn)
                if metadata is not None else int(row["revision"])
            )
            conn.execute(
                "UPDATE peer_connection SET last_seen=?,metadata_json=?,revision=?,"
                "updated_at=? WHERE connection_id=? AND epoch=?",
                (
                    current, _json(stored_metadata), revision, current,
                    str(connection_id), int(expected_epoch),
                ),
            )
            changed, fenced = self._reconcile_connection_group_tx(
                conn, str(row["harness"]), str(row["native_session_id"]), now=current,
            )
            effects["connections"].extend(changed)
            effects["messages"].extend(fenced)
            updated = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (str(connection_id),),
            ).fetchone()
            result = self._decorate_connection(conn, updated, now=current) or {}
            conn.commit()
        return result, effects

    def close_connection(
        self, connection_id: str, expected_epoch: int, *, reason: str = "",
        status: str = "closed", now: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        if status not in {"closed", "expired"}:
            raise ValueError("connection close status must be closed or expired")
        current = float(time.time() if now is None else now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            effects = self._expire_connections_tx(conn, now=current)
            row = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (str(connection_id),),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError(f"unknown peer connection: {connection_id}")
            if int(row["epoch"]) != int(expected_epoch):
                conn.rollback()
                raise RuntimeError("peer connection epoch changed")
            if str(row["status"]) not in {"closed", "expired"}:
                revision = self._tick(conn)
                metadata = _object(row["metadata_json"])
                if reason:
                    metadata["close_reason"] = str(reason)[:500]
                conn.execute(
                    "UPDATE peer_connection SET status=?,metadata_json=?,closed_at=?,"
                    "revision=?,updated_at=? WHERE connection_id=?",
                    (
                        status, _json(metadata), current, revision, current,
                        str(connection_id),
                    ),
                )
                effects["messages"].extend(self._fence_connection_claims_tx(
                    conn, [str(connection_id)],
                    reason=(
                        "external delivery connection closed"
                        + (f": {reason}" if reason else "")
                    ),
                    now=current,
                ))
            changed, fenced = self._reconcile_connection_group_tx(
                conn, str(row["harness"]), str(row["native_session_id"]), now=current,
            )
            effects["connections"].extend(changed)
            effects["messages"].extend(fenced)
            updated = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (str(connection_id),),
            ).fetchone()
            result = self._decorate_connection(conn, updated, now=current) or {}
            conn.commit()
        return result, effects

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (str(connection_id),),
            ).fetchone()
            return self._decorate_connection(conn, row, now=now)

    def list_connections(
        self, *, peer_id: str = "", harness: str = "", statuses: tuple[str, ...] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if peer_id:
            clauses.append("peer_id=?")
            params.append(str(peer_id))
        if harness:
            clauses.append("harness=?")
            params.append(str(harness))
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(str(item) for item in statuses)
        sql = "SELECT * FROM peer_connection"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC,connection_id LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        now = time.time()
        with self._connect() as conn:
            return [
                self._decorate_connection(conn, row, now=now) or {}
                for row in conn.execute(sql, params).fetchall()
            ]

    def expire_connections(
        self, *, now: float | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        current = float(time.time() if now is None else now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            effects = self._expire_connections_tx(conn, now=current)
            conn.commit()
        return effects

    def claim_connection(
        self, connection_id: str, expected_epoch: int, *, limit: int = 20,
        now: float | None = None,
        requests_only: bool = False,
    ) -> list[dict[str, Any]]:
        current = float(time.time() if now is None else now)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            connection = conn.execute(
                "SELECT * FROM peer_connection WHERE connection_id=?",
                (str(connection_id),),
            ).fetchone()
            if connection is None:
                conn.rollback()
                raise LookupError(f"unknown peer connection: {connection_id}")
            if int(connection["epoch"]) != int(expected_epoch):
                conn.rollback()
                raise RuntimeError("peer connection epoch changed")
            if str(connection["status"]) == "conflicted":
                conn.rollback()
                raise RuntimeError("peer connection has conflicting live runtime actors")
            if str(connection["status"]) != "active":
                conn.rollback()
                raise RuntimeError("peer connection is not active")
            if float(connection["last_seen"]) + float(connection["lease_seconds"]) <= current:
                conn.rollback()
                raise RuntimeError("peer connection lease expired")
            rows = conn.execute(
                "SELECT * FROM peer_message WHERE target_peer_id=? AND state='queued' "
                "AND claim_connection_epoch=0 AND claim_connection_id='' "
                + ("AND message_kind='request' " if requests_only else "") +
                "ORDER BY sequence LIMIT ?",
                (
                    str(connection["peer_id"]),
                    max(1, min(int(limit), 100)),
                ),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                revision = self._tick(conn)
                conn.execute(
                    "UPDATE peer_message SET claim_adapter=?,claim_connection_id=?,"
                    "claim_connection_epoch=?,claim_at=?,revision=?,updated_at=? "
                    "WHERE message_id=? AND claim_connection_epoch=0 "
                    "AND claim_connection_id=''",
                    (
                        str(connection["harness"]), str(connection_id),
                        int(expected_epoch), current, revision, current,
                        row["message_id"],
                    ),
                )
                selected = conn.execute(
                    "SELECT * FROM peer_message WHERE message_id=?",
                    (row["message_id"],),
                ).fetchone()
                claimed.append(self._message(selected) or {})
            conn.commit()
        return claimed

    def pending_native(self, limit: int = 500, *, chat_id: str = '') -> list[dict[str, Any]]:
        target_filter = 'AND target_peer_id=? ' if chat_id else ''
        values = (['chat:' + str(chat_id)] if chat_id else []) + [max(1, min(int(limit), 5000))]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM peer_message WHERE target_peer_id LIKE 'chat:%' "
                "AND message_kind='request' "
                "AND state IN ('persisted','queued') " + target_filter + "ORDER BY sequence LIMIT ?",
                values,
            ).fetchall()
        return [self._message(row) or {} for row in rows]

    def reconcile_stale_external_claims(self) -> list[dict[str, Any]]:
        """Fence external writes whose process ended before settlement."""

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM peer_message WHERE state='queued' "
                "AND claim_connection_epoch>0 ORDER BY sequence"
            ).fetchall()
            now = time.time()
            updated: list[dict[str, Any]] = []
            for row in rows:
                revision = self._tick(conn)
                conn.execute(
                    "UPDATE peer_message SET state='unknown',"
                    "claim_adapter='',claim_connection_id='',"
                    "claim_connection_epoch=0,claim_at=NULL,"
                    "error='external delivery claim was interrupted',"
                    "revision=?,updated_at=? WHERE message_id=?",
                    (revision, now, row["message_id"]),
                )
                current = conn.execute(
                    "SELECT * FROM peer_message WHERE message_id=?",
                    (row["message_id"],),
                ).fetchone()
                updated.append(self._message(current) or {})
            conn.commit()
        return updated

    def reconcile_external_epoch(
        self, peer_id: str, connection_epoch: int,
    ) -> list[dict[str, Any]]:
        """Fence claims owned by an older live external connection."""

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM peer_message WHERE target_peer_id=? "
                "AND claim_connection_epoch>0 AND claim_connection_epoch!=? "
                "AND state IN ('queued','transport_written','observed') "
                "ORDER BY sequence",
                (str(peer_id), int(connection_epoch)),
            ).fetchall()
            now = time.time()
            updated: list[dict[str, Any]] = []
            for row in rows:
                revision = self._tick(conn)
                conn.execute(
                    "UPDATE peer_message SET state='unknown',"
                    "claim_adapter='',claim_connection_id='',"
                    "claim_connection_epoch=0,claim_at=NULL,"
                    "error='external delivery belongs to a prior connection epoch',"
                    "revision=?,updated_at=? WHERE message_id=?",
                    (revision, now, row["message_id"]),
                )
                current = conn.execute(
                    "SELECT * FROM peer_message WHERE message_id=?",
                    (row["message_id"],),
                ).fetchone()
                updated.append(self._message(current) or {})
            conn.commit()
        return updated

    def retire_chat(self, chat_id: str) -> int:
        peer_id = "chat:" + str(chat_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT message_id FROM peer_message WHERE target_peer_id=? "
                "AND state IN ('persisted','queued')", (peer_id,),
            ).fetchall()
            now = time.time()
            for row in rows:
                revision = self._tick(conn)
                conn.execute(
                    "UPDATE peer_message SET state='failed',error='native peer deleted',"
                    "revision=?,updated_at=? WHERE message_id=?",
                    (revision, now, row["message_id"]),
                )
            conn.commit()
        return len(rows)
