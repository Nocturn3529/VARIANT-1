"""Durable inbound identity and adapter-cursor authority for messaging."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sqlite3
import time
from typing import Any

from core_invariants import (
    request_fingerprint,
    sqlite_read_connection,
    sqlite_unit_of_work,
    sqlite_writer_lock,
)

from .base import MessageEnvelope


TERMINAL_INGRESS_STATES = frozenset({"routed", "rejected"})


class MessagingIngressConflict(RuntimeError):
    """One transport identity was reused with different message content."""


@dataclass(frozen=True, slots=True)
class IngressRecord:
    adapter: str
    conversation_id: str
    message_id: str
    request_fingerprint: str
    status: str
    reply: str = ""
    error: str = ""
    attempts: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    duplicate: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_INGRESS_STATES

    @property
    def needs_reconciliation(self) -> bool:
        return self.status == "routing"


class MessagingIngressStore:
    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._write_lock = sqlite_writer_lock(self.path)
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS messaging_ingress (
                    adapter TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reply TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(adapter, conversation_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messaging_ingress_status
                    ON messaging_ingress(status, updated_at, adapter,
                                         conversation_id, message_id);
                CREATE TABLE IF NOT EXISTS messaging_adapter_cursor (
                    adapter TEXT PRIMARY KEY,
                    cursor INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messaging_adapter_state (
                    adapter TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(adapter, key)
                );
                CREATE TABLE IF NOT EXISTS messaging_outbox (
                    adapter TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    reply TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    available_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(adapter, conversation_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messaging_outbox_pending
                    ON messaging_outbox(status, updated_at, adapter);
                """)
                columns = {str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(messaging_outbox)"
                )}
                if "available_at" not in columns:
                    conn.execute(
                        "ALTER TABLE messaging_outbox ADD COLUMN "
                        "available_at REAL NOT NULL DEFAULT 0"
                    )
                # Round-5 cutover: ``unknown`` used to be terminal even though
                # it represented a turn with no durable ticket. Such rows are
                # safe to admit again; gateway_route checks ticket proof before
                # running any effects.
                conn.execute(
                    "UPDATE messaging_ingress SET status='admitted' "
                    "WHERE status='unknown'"
                )
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _record(row: sqlite3.Row, *, duplicate: bool = False) -> IngressRecord:
        return IngressRecord(
            adapter=str(row["adapter"]),
            conversation_id=str(row["conversation_id"]),
            message_id=str(row["message_id"]),
            request_fingerprint=str(row["request_fingerprint"]),
            status=str(row["status"]),
            reply=str(row["reply"] or ""),
            error=str(row["error"] or ""),
            attempts=int(row["attempts"] or 0),
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            duplicate=bool(duplicate),
        )

    @staticmethod
    def _identity(envelope: MessageEnvelope) -> tuple[str, str, str]:
        return (
            str(envelope.adapter),
            str(envelope.conversation_id),
            str(envelope.message_id),
        )

    @staticmethod
    def _payload(envelope: MessageEnvelope, text: str) -> dict[str, Any]:
        attachments = []
        for item in tuple(envelope.attachments or ())[:8]:
            if not isinstance(item, dict):
                continue
            attachments.append({
                key: item[key]
                for key in (
                    "name", "kind", "mime", "path", "size", "sha256", "source_id"
                )
                if key in item
            })
        return {
            "adapter": envelope.adapter,
            "conversation_id": envelope.conversation_id,
            "message_id": envelope.message_id,
            "user_id": envelope.user_id,
            "text": str(text),
            "user_name": envelope.user_name,
            "conversation_name": envelope.conversation_name,
            "reply_to": envelope.reply_to,
            "metadata": dict(envelope.metadata or {}),
            "attachments": attachments,
        }

    @staticmethod
    def _envelope(payload: dict[str, Any]) -> MessageEnvelope:
        return MessageEnvelope(
            adapter=str(payload.get("adapter") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            user_id=str(payload.get("user_id") or ""),
            text=str(payload.get("text") or ""),
            user_name=str(payload.get("user_name") or ""),
            conversation_name=str(payload.get("conversation_name") or ""),
            reply_to=str(payload.get("reply_to") or ""),
            metadata=(
                dict(payload.get("metadata") or {})
                if isinstance(payload.get("metadata"), dict) else {}
            ),
            attachments=tuple(
                dict(item) for item in (payload.get("attachments") or [])
                if isinstance(item, dict)
            ),
        )

    def admit(self, envelope: MessageEnvelope, text: str) -> IngressRecord:
        payload = self._payload(envelope, text)
        fingerprint = request_fingerprint("messaging.ingress.v1", payload)
        identity = self._identity(envelope)
        now = time.time()
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.admit.before_commit"
        ) as conn:
            row = conn.execute(
                "SELECT * FROM messaging_ingress WHERE adapter=? "
                "AND conversation_id=? AND message_id=?",
                identity,
            ).fetchone()
            if row is not None:
                if str(row["request_fingerprint"]) != fingerprint:
                    raise MessagingIngressConflict(
                        "message identity was reused with different content"
                    )
                return self._record(row, duplicate=True)
            conn.execute(
                "INSERT INTO messaging_ingress(adapter,conversation_id,message_id,"
                "request_fingerprint,payload_json,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'admitted',?,?)",
                (*identity, fingerprint, json.dumps(payload, sort_keys=True), now, now),
            )
            row = conn.execute(
                "SELECT * FROM messaging_ingress WHERE adapter=? "
                "AND conversation_id=? AND message_id=?",
                identity,
            ).fetchone()
            return self._record(row)

    def mark_routing(self, envelope: MessageEnvelope) -> IngressRecord:
        identity = self._identity(envelope)
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.routing.before_commit"
        ) as conn:
            conn.execute(
                "UPDATE messaging_ingress SET status='routing',attempts=attempts+1,"
                "updated_at=? WHERE adapter=? AND conversation_id=? AND message_id=? "
                "AND status='admitted'",
                (time.time(), *identity),
            )
            row = conn.execute(
                "SELECT * FROM messaging_ingress WHERE adapter=? "
                "AND conversation_id=? AND message_id=?",
                identity,
            ).fetchone()
            if row is None:
                raise LookupError("messaging ingress admission is unavailable")
            return self._record(row)

    def release_routing(self, envelope: MessageEnvelope, *, error: str = "") -> IngressRecord:
        """Return an unproven route to the retryable admitted state."""

        identity = self._identity(envelope)
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.release.before_commit"
        ) as conn:
            conn.execute(
                "UPDATE messaging_ingress SET status='admitted',error=?,updated_at=? "
                "WHERE adapter=? AND conversation_id=? AND message_id=? "
                "AND status IN ('routing','unknown')",
                (str(error)[:16000], time.time(), *identity),
            )
            row = conn.execute(
                "SELECT * FROM messaging_ingress WHERE adapter=? "
                "AND conversation_id=? AND message_id=?",
                identity,
            ).fetchone()
            if row is None:
                raise LookupError("messaging ingress admission is unavailable")
            return self._record(row)

    def complete(
        self,
        envelope: MessageEnvelope,
        *,
        status: str,
        reply: str = "",
        error: str = "",
    ) -> IngressRecord:
        if status not in TERMINAL_INGRESS_STATES:
            raise ValueError(f"invalid terminal messaging status: {status!r}")
        identity = self._identity(envelope)
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.complete.before_commit"
        ) as conn:
            conn.execute(
                "UPDATE messaging_ingress SET status=?,reply=?,error=?,updated_at=? "
                "WHERE adapter=? AND conversation_id=? AND message_id=? "
                "AND status NOT IN ('routed','rejected')",
                (status, str(reply), str(error)[:16000], time.time(), *identity),
            )
            row = conn.execute(
                "SELECT * FROM messaging_ingress WHERE adapter=? "
                "AND conversation_id=? AND message_id=?",
                identity,
            ).fetchone()
            if row is None:
                raise LookupError("messaging ingress admission is unavailable")
            return self._record(row)

    def complete_routed(self, envelope: MessageEnvelope, *, reply: str = "") -> IngressRecord:
        """Commit the inbound result and its outbound reply in one transaction."""

        identity = self._identity(envelope)
        now = time.time()
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.routed.before_commit"
        ) as conn:
            conn.execute(
                "UPDATE messaging_ingress SET status='routed',reply=?,error='',updated_at=? "
                "WHERE adapter=? AND conversation_id=? AND message_id=? "
                "AND status NOT IN ('routed','rejected')",
                (str(reply), now, *identity),
            )
            if str(reply):
                conn.execute(
                    "INSERT INTO messaging_outbox(" 
                    "adapter,conversation_id,message_id,reply,status,created_at,updated_at" 
                    ") VALUES (?,?,?,?, 'pending',?,?) "
                    "ON CONFLICT(adapter,conversation_id,message_id) DO UPDATE SET "
                    "reply=excluded.reply,updated_at=excluded.updated_at "
                    "WHERE messaging_outbox.status!='delivered'",
                    (*identity, str(reply), now, now),
                )
            row = conn.execute(
                "SELECT * FROM messaging_ingress WHERE adapter=? "
                "AND conversation_id=? AND message_id=?",
                identity,
            ).fetchone()
            if row is None:
                raise LookupError("messaging ingress admission is unavailable")
            return self._record(row)

    def recoverable_ingress(
        self, *, adapter: str = "", limit: int = 200,
        exclude: tuple[tuple[str, str, str], ...] = (),
        reclassify_before: float | None = None,
    ) -> list[MessageEnvelope]:
        """Re-admit interrupted routes and return their durable envelopes."""

        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.recover.before_commit"
        ) as conn:
            clauses = ["status IN ('routing','unknown')"]
            params: list[Any] = [time.time()]
            if reclassify_before is not None:
                clauses.append("updated_at<=?")
                params.append(reclassify_before)
            if adapter:
                clauses.append("adapter=?")
                params.append(str(adapter))
            self._exclude_ingress(clauses, params, exclude)
            conn.execute(
                f"UPDATE messaging_ingress SET status='admitted',updated_at=? WHERE {' AND '.join(clauses)}",
                tuple(params),
            )
        return self.pending_ingress(adapter=adapter, limit=limit, exclude=exclude)

    @staticmethod
    def _exclude_ingress(clauses, params, exclude) -> None:
        # One JSON parameter avoids SQLite's variable limit for a large live inbox.
        if exclude:
            clauses.append("NOT EXISTS (SELECT 1 FROM json_each(?) AS live WHERE "
                           "json_extract(live.value,'$[0]')=adapter AND "
                           "json_extract(live.value,'$[1]')=conversation_id AND "
                           "json_extract(live.value,'$[2]')=message_id)")
            params.append(json.dumps(exclude))

    def pending_ingress(self, *, adapter: str = "", limit: int = 200,
                        exclude: tuple[tuple[str, str, str], ...] = ()) -> list[MessageEnvelope]:
        """Page admitted work without changing ownership of live routing rows."""
        clauses, params = ["status='admitted'"], []
        if adapter:
            clauses.append("adapter=?")
            params.append(str(adapter))
        self._exclude_ingress(clauses, params, exclude)
        params.append(max(1, min(int(limit or 200), 1000)))
        with sqlite_read_connection(self._connect) as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM messaging_ingress WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at,adapter,conversation_id,message_id LIMIT ?", tuple(params),
            ).fetchall()
        envelopes = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
                if isinstance(payload, dict):
                    envelopes.append(self._envelope(payload))
            except Exception:
                continue
        return envelopes

    def reject_admitted(self, envelope: MessageEnvelope, reason: str) -> None:
        """Retire already-admitted messages that no longer meet routing requirements."""
        with sqlite_unit_of_work(self._connect, self._write_lock) as conn:
            conn.execute(
                "UPDATE messaging_ingress SET status='rejected',error=?,updated_at=? "
                "WHERE adapter=? AND conversation_id=? AND message_id=? AND status='admitted'",
                (str(reason), time.time(), *self._identity(envelope)),
            )

    def pending_outbound(
        self, *, adapter: str = "", limit: int = 200, ready_only: bool = False,
    ) -> list[tuple[MessageEnvelope, str]]:
        clauses = ["o.status='pending'"]
        params: list[Any] = []
        if adapter:
            clauses.append("o.adapter=?")
            params.append(str(adapter))
        if ready_only:
            clauses.append("o.available_at<=?")
            params.append(time.time())
        params.append(max(1, min(int(limit or 200), 1000)))
        with sqlite_read_connection(self._connect) as conn:
            rows = conn.execute(
                "SELECT i.payload_json,o.reply FROM messaging_outbox o "
                "JOIN messaging_ingress i USING(adapter,conversation_id,message_id) "
                f"WHERE {' AND '.join(clauses)} ORDER BY o.created_at ASC LIMIT ?",
                tuple(params),
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"] or "{}"))
                if isinstance(payload, dict):
                    result.append((self._envelope(payload), str(row["reply"] or "")))
            except Exception:
                continue
        return result

    def outbound_pending(self, envelope: MessageEnvelope) -> str:
        with sqlite_read_connection(self._connect) as conn:
            row = conn.execute(
                "SELECT reply FROM messaging_outbox WHERE adapter=? "
                "AND conversation_id=? AND message_id=? AND status='pending'",
                self._identity(envelope),
            ).fetchone()
        return str(row["reply"] or "") if row is not None else ""

    def mark_outbound_delivered(self, envelope: MessageEnvelope) -> None:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.outbox.before_commit"
        ) as conn:
            conn.execute(
                "UPDATE messaging_outbox SET status='delivered',attempts=attempts+1," 
                "last_error='',updated_at=? WHERE adapter=? AND conversation_id=? "
                "AND message_id=? AND status='pending'",
                (time.time(), *self._identity(envelope)),
            )

    def mark_outbound_error(self, envelope: MessageEnvelope, error: str) -> None:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.outbox_error.before_commit"
        ) as conn:
            row = conn.execute(
                "SELECT attempts FROM messaging_outbox WHERE adapter=? "
                "AND conversation_id=? AND message_id=?",
                self._identity(envelope),
            ).fetchone()
            delay = min(60.0, 0.5 * (2 ** min(int(row[0]) if row else 0, 7)))
            conn.execute(
                "UPDATE messaging_outbox SET attempts=attempts+1,last_error=?,updated_at=?,available_at=? "
                "WHERE adapter=? AND conversation_id=? AND message_id=? "
                "AND status='pending'",
                (str(error)[:16000], time.time(), time.time() + delay,
                 *self._identity(envelope)),
            )

    def adapter_state(self, adapter: str, key: str, default: Any = None) -> Any:
        with sqlite_read_connection(self._connect) as conn:
            row = conn.execute(
                "SELECT value_json FROM messaging_adapter_state WHERE adapter=? AND key=?",
                (str(adapter), str(key)),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row["value_json"]))
        except Exception:
            return default

    def set_adapter_state(self, adapter: str, key: str, value: Any) -> None:
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.adapter_state.before_commit"
        ) as conn:
            conn.execute(
                "INSERT INTO messaging_adapter_state(adapter,key,value_json,updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(adapter,key) DO UPDATE SET "
                "value_json=excluded.value_json,updated_at=excluded.updated_at",
                (str(adapter), str(key), json.dumps(value), time.time()),
            )

    def adapter_cursor(self, adapter: str) -> int:
        with sqlite_read_connection(self._connect) as conn:
            row = conn.execute(
                "SELECT cursor FROM messaging_adapter_cursor WHERE adapter=?",
                (str(adapter),),
            ).fetchone()
        return int(row["cursor"] or 0) if row is not None else 0

    def set_adapter_cursor(self, adapter: str, cursor: int) -> int:
        value = max(0, int(cursor))
        with sqlite_unit_of_work(
            self._connect, self._write_lock, fault_name="messaging.cursor.before_commit"
        ) as conn:
            conn.execute(
                "INSERT INTO messaging_adapter_cursor(adapter,cursor,updated_at) "
                "VALUES (?,?,?) ON CONFLICT(adapter) DO UPDATE SET "
                "cursor=MAX(cursor,excluded.cursor),updated_at=excluded.updated_at",
                (str(adapter), value, time.time()),
            )
            row = conn.execute(
                "SELECT cursor FROM messaging_adapter_cursor WHERE adapter=?",
                (str(adapter),),
            ).fetchone()
        return int(row["cursor"] or 0)


__all__ = [
    "IngressRecord",
    "MessagingIngressConflict",
    "MessagingIngressStore",
    "TERMINAL_INGRESS_STATES",
]
