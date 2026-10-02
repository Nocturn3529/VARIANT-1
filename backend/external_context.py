"""Derived scoped views over canonical session, cell and snapshot owners.

The index is disposable. Exact expansion resolves the captured source owner;
history retrieval neither replays operations nor becomes a new transcript.
"""

from __future__ import annotations

import json
import hashlib
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

from core_invariants import sqlite_session_connection
from tools import ToolError
from work_fabric.handles import remote_handle_envelope

RESULT_SCHEMA = "variant1.session-context-result.v1"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(part.get("text", "")) for part in value
                         if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"})
    if isinstance(value, dict):
        if value.get("type") in {"text", "input_text", "output_text"}:
            return str(value.get("text") or "")
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _visible_messages(messages: list) -> list[dict]:
    result = []
    for raw in messages:
        if not isinstance(raw, dict) or raw.get("role") not in {"user", "assistant", "tool"}:
            continue
        content = raw.get("content", "")
        if isinstance(content, dict):
            # A native structured content block is not arbitrary printable
            # state. Unknown/signed/reasoning blocks have no text projection.
            content = _text([content])
        row = {"role": raw["role"], "content": _text(content)}
        for key in ("tool_call_id", "is_error"):
            if key in raw:
                row[key] = raw[key]
        calls = []
        for call in raw.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                calls.append({"id": call.get("id"), "name": fn.get("name") or call.get("name"),
                              "arguments": fn.get("arguments", call.get("arguments"))})
        if calls:
            row["tool_calls"] = calls
        result.append(row)
    return result


class SessionContextService:
    def __init__(self, *, database_path: str, sessions, kernel, artifacts, runtimes, children=None):
        self.path = os.path.abspath(database_path)
        self.sessions, self.kernel, self.artifacts, self.runtimes = sessions, kernel, artifacts, runtimes
        self.children = children
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS context_view(
                  view_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,watermarks_json TEXT NOT NULL,created_at REAL NOT NULL,
                  source_chat_id TEXT NOT NULL DEFAULT '',child_id TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS context_text(
                  text_key TEXT PRIMARY KEY,search_text TEXT NOT NULL,search_folded TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS context_source(
                  view_id TEXT NOT NULL,ordinal INTEGER NOT NULL,source_id TEXT NOT NULL,kind TEXT NOT NULL,
                  descriptor_json TEXT NOT NULL,text_key TEXT NOT NULL,
                  PRIMARY KEY(view_id,source_id),FOREIGN KEY(view_id) REFERENCES context_view(view_id) ON DELETE CASCADE,
                  FOREIGN KEY(text_key) REFERENCES context_text(text_key));
                CREATE INDEX IF NOT EXISTS context_source_order ON context_source(view_id,ordinal);
                CREATE INDEX IF NOT EXISTS context_source_text ON context_source(text_key);
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite_session_connection(self.path, autocommit=False)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def delete_chat(self, chat_id: str):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM context_view WHERE chat_id=? OR source_chat_id=?", (str(chat_id), str(chat_id)))
            conn.execute("DELETE FROM context_text WHERE NOT EXISTS(SELECT 1 FROM context_source s WHERE s.text_key=context_text.text_key)")

    @staticmethod
    def _index_text(conn, text: str) -> str:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if conn.execute("SELECT 1 FROM context_text WHERE text_key=?", (key,)).fetchone() is None:
            conn.execute("INSERT INTO context_text VALUES(?,?,?)", (key, text, text.casefold()))
        return key

    def _has_owner(self, chat_id: str) -> bool:
        get_runtime = getattr(self.runtimes, "runtime", None)
        record = get_runtime(chat_id) if callable(get_runtime) else None
        if record is not None and record.lifecycle_state != "active":
            return False
        if self.sessions.has_session(chat_id):
            return True
        return bool(record is not None and str(record.creation_saga_state).startswith(("child:", "worker:")))

    def _child_source(self, parent: str, child_id: str) -> str:
        try:
            if self.children is None:
                raise LookupError("child source owner unavailable")
            child = self.children.inspect(parent, child_id)
            if child.get("deletion_state") in {"deleted", "deleting"}:
                raise LookupError("child source has been deleted")
            return str(child["child_chat_id"])
        except (KeyError, LookupError) as exc:
            raise ToolError("Context child is unavailable or outside this parent's scope") from exc

    def _artifact_text(self, ref: str, chat_id: str, expected_sha256: str = "") -> str:
        metadata = self.artifacts.stat(ref, scope=chat_id)
        raw = self.artifacts.read_bytes_scoped(ref, chat_id)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != metadata.sha256 or (expected_sha256 and digest != expected_sha256):
            raise ToolError("Session context artifact failed its source integrity check")
        return raw.decode("utf-8", errors="strict")

    def capture(self, chat_id: str, *, child_id: str = "") -> str:
        chat_id = str(chat_id)
        reader_chat_id = chat_id
        if not self._has_owner(reader_chat_id):
            raise ToolError("Session context requires an existing, live session")
        if child_id:
            chat_id = self._child_source(reader_chat_id, child_id)
        if not self._has_owner(chat_id):
            raise ToolError("Session context source owner is unavailable")
        canonical = self.sessions.context_sources(chat_id)
        cursor = canonical.get("cursor")
        cells = []
        last = self.kernel.cell_ledger.tail(chat_id, limit=1)
        upper = int(last[-1].sequence) if last else 0
        after = 0
        while upper and after < upper:
            rows = self.kernel.execution_history(chat_id, after_sequence=after, through_sequence=upper, limit=500)
            cells.extend(rows["items"])
            next_sequence = int(rows["next_sequence"])
            if next_sequence <= after:
                break
            after = next_sequence
        store = getattr(self.runtimes, "snapshot_store", None)
        snapshots = store.context_cursors_sync(chat_id) if store is not None else []
        snapshot_heads = {}
        for snapshot in snapshots:
            snapshot_heads[snapshot["thread_id"]] = {
                key: snapshot[key] for key in ("sequence", "snapshot_id")
            }
        watermarks = {"canonical": cursor, "ledger_through_sequence": upper, "snapshot_heads": snapshot_heads,
                      "atomic_across_owners": False, "source_chat_id": chat_id,
                      "canonical_available": cursor is not None,
                      "source_gaps": [] if cursor is not None else ["No canonical conversation owner; only retained runtime evidence is available"]}
        sources = []
        for node in canonical["items"]:
            content = json.loads(node["content_json"])
            metadata = json.loads(node["metadata_json"])
            descriptor = {"source_id": "message:" + node["node_id"], "kind": "message", "role": node["role"],
                          "node_id": node["node_id"], "head_node_id": cursor["head_node_id"],
                          "run_id": metadata.get("run_id"), "turn_id": node.get("turn_id"),
                          "timestamp": node["created_at"], "coverage": "captured canonical content; readable-text search only",
                          "sha256": hashlib.sha256(node["content_json"].encode("utf-8")).hexdigest()}
            sources.append((descriptor, _text(content)))
        for cell in cells:
            descriptor = {**cell, "source_id": "cell:" + cell["execution_id"], "kind": "cell",
                          "coverage": "retained cell evidence; omissions reported on expansion"}
            texts = []
            for key in ("source_ref", "result_ref"):
                try:
                    raw = self._artifact_text(cell[key], chat_id, str(cell.get(key.replace("_ref", "_sha256")) or ""))
                    if key == "source_ref":
                        texts.append(raw)
                    else:
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError("Cell result evidence must be an object")
                        texts.append(str(value.get("text") or value.get("error") or ""))
                except (KeyError, ValueError, OSError, ToolError):
                    descriptor["search_incomplete"] = True
            sources.append((descriptor, "\n".join(texts)))
        for snapshot in snapshots:
            descriptor = {**snapshot, "source_id": "snapshot:" + snapshot["snapshot_id"], "kind": "snapshot",
                          "coverage": "retained committed native projection; may repeat earlier records; metadata search only"}
            sources.append((descriptor, json.dumps(snapshot, ensure_ascii=False)))
        incomplete_cells = sum(bool(descriptor.get("search_incomplete")) for descriptor, _ in sources)
        if incomplete_cells:
            watermarks["source_gaps"].append(f"{incomplete_cells} cells have unavailable or invalid search artifacts; exact expansion may fail")
        view_id = "context_" + uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            if not self._has_owner(chat_id) or not self._has_owner(reader_chat_id):
                raise ToolError("Session context owner was deleted during capture")
            if child_id and self._child_source(reader_chat_id, child_id) != chat_id:
                raise ToolError("Session context child changed during capture")
            conn.execute("INSERT INTO context_view(view_id,chat_id,watermarks_json,created_at,source_chat_id,child_id) VALUES(?,?,?,?,?,?)",
                         (view_id, reader_chat_id, json.dumps(watermarks), time.time(), chat_id, child_id))
            for ordinal, (descriptor, text) in enumerate(sources):
                descriptor["source_chat_id"] = chat_id
                key = self._index_text(conn, text)
                conn.execute("INSERT INTO context_source VALUES(?,?,?,?,?,?)",
                             (view_id, ordinal, descriptor["source_id"], descriptor["kind"],
                              json.dumps(descriptor, ensure_ascii=False), key))
        return view_id

    def _view(self, chat_id: str, view_id: str):
        if not self._has_owner(chat_id):
            raise ToolError("Session context owner has been deleted or is unavailable")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM context_view WHERE view_id=? AND chat_id=?", (view_id, chat_id)).fetchone()
        if row is None:
            raise ToolError("Session context view is unavailable or outside the calling session")
        source_chat_id = row["source_chat_id"] or chat_id
        if not self._has_owner(source_chat_id):
            raise ToolError("Session context source owner was deleted or is unavailable")
        if row["child_id"] and self._child_source(chat_id, row["child_id"]) != source_chat_id:
            raise ToolError("Session context child ownership changed")
        return dict(row)

    def status(self, chat_id: str, view_id: str):
        view = self._view(chat_id, view_id)
        with self._lock, self._connect() as conn:
            counts = {row["kind"]: row["n"] for row in conn.execute(
                "SELECT kind,COUNT(*) AS n FROM context_source WHERE view_id=? GROUP BY kind", (view_id,))}
        return {"schema": RESULT_SCHEMA, "view_id": view_id, "counts": counts,
                "watermarks": json.loads(view["watermarks_json"]),
                "coverage": {
                    "messages": "captured canonical readable text",
                    "cells": "retained source/result text; output-event envelopes expand separately and retain omissions",
                    "snapshots": "metadata search; visible messages/calls on expansion, excluding opaque reasoning and media",
                    "limits": "unsaved or discarded payloads cannot be reconstructed; projections may repeat source evidence",
                },
                "ordering": "canonical ancestry, ledger sequence, then snapshot containers; links carry causality",
                "guidance": "Read/search historical evidence on demand. Refresh explicitly for newer commits. Snapshots are projections, not additional actions."}

    def read(self, chat_id: str, view_id: str, *, after: int = 0, limit: int = 50,
             kind: str = "", around: str = "", before: int = 2):
        self._view(chat_id, view_id)
        cap = max(1, min(int(limit), 200))
        with self._lock, self._connect() as conn:
            if around:
                anchor = conn.execute("SELECT ordinal FROM context_source WHERE view_id=? AND source_id=?",
                                      (view_id, around)).fetchone()
                if anchor is None:
                    raise ToolError("Context anchor is outside the captured view")
                after = max(0, int(anchor["ordinal"]) - max(0, min(int(before), 100)))
            rows = conn.execute(
                "SELECT s.ordinal,s.descriptor_json,p.search_text FROM context_source s JOIN context_text p ON p.text_key=s.text_key "
                "WHERE s.view_id=? AND s.ordinal>=? "
                + ("AND s.kind=? " if kind else "") + "ORDER BY s.ordinal LIMIT ?",
                (view_id, max(0, int(after)), *([kind] if kind else []), cap + 1),
            ).fetchall()
        items = [{**json.loads(row["descriptor_json"]), "preview": row["search_text"][:600], "ordinal": row["ordinal"]}
                 for row in rows[:cap]]
        return {"schema": RESULT_SCHEMA, "view_id": view_id, "items": items, "has_more": len(rows) > cap,
                "next_cursor": int(rows[cap - 1]["ordinal"]) + 1 if len(rows) > cap else None}

    def search(self, chat_id: str, view_id: str, *, query: str, limit: int = 20, after: int = 0, kind: str = ""):
        self._view(chat_id, view_id)
        query = str(query).strip()
        if not query or len(query) > 1000:
            raise ToolError("Context search needs a nonempty query of at most 1000 characters; use read for chronology")
        pattern = "%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cap = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT s.ordinal,s.descriptor_json,p.search_text FROM context_source s JOIN context_text p ON p.text_key=s.text_key "
                "WHERE s.view_id=? AND s.ordinal>=? AND p.search_folded LIKE ? ESCAPE '\\' "
                + ("AND s.kind=? " if kind else "") + "ORDER BY s.ordinal LIMIT ?",
                (view_id, max(0, int(after)), pattern, *([kind] if kind else []), cap + 1),
            ).fetchall()
        items = []
        for row in rows[:cap]:
            text = row["search_text"]
            folded_position = text.casefold().find(query.casefold())
            # Case folding can expand characters (e.g. ß -> ss). Map back to
            # the original source so offsets agree with expand().
            folded_length, position = 0, 0
            for position, char in enumerate(text):
                next_length = folded_length + len(char.casefold())
                if next_length > folded_position:
                    break
                folded_length = next_length
            start = max(0, position - 150)
            items.append({**json.loads(row["descriptor_json"]), "ordinal": row["ordinal"],
                          "snippet": text[start:start + 600], "match_offset": position,
                          "offset_unit": "Unicode characters"})
        return {"schema": RESULT_SCHEMA, "view_id": view_id, "items": items, "has_more": len(rows) > cap,
                "next_cursor": int(rows[cap - 1]["ordinal"]) + 1 if len(rows) > cap else None,
                "coverage": "canonical text and retained cell source/result text; snapshot metadata only, not all modalities"}

    def expand(self, chat_id: str, view_id: str, *, source_id: str, offset: int = 0,
               max_chars: int = 12_000, part: str = "result"):
        view = self._view(chat_id, view_id)
        source_chat_id = view["source_chat_id"] or chat_id
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT descriptor_json FROM context_source WHERE view_id=? AND source_id=?",
                               (view_id, str(source_id))).fetchone()
        if row is None:
            raise ToolError("Context source is outside the captured view")
        descriptor = json.loads(row["descriptor_json"])
        if descriptor["kind"] == "message":
            source = self.sessions.context_source(source_chat_id, descriptor["head_node_id"], descriptor["node_id"])
            if hashlib.sha256(source["content_json"].encode("utf-8")).hexdigest() != descriptor["sha256"]:
                raise ToolError("Captured context source changed; inspect source integrity")
            value = json.loads(source["content_json"])
            text = value if isinstance(value, str) else source["content_json"]
        elif descriptor["kind"] == "cell":
            if part not in {"source", "result", "output"}:
                raise ToolError("Cell expansion part must be source, result or output")
            artifact_part = "result" if part == "output" else part
            ref = descriptor[artifact_part + "_ref"]
            text = self._artifact_text(ref, source_chat_id, str(descriptor.get(artifact_part + "_sha256") or ""))
            if part == "output":
                evidence = json.loads(text).get("output_evidence") or {}
                if not evidence.get("ref"):
                    raise ToolError("This cell has no retained output-event evidence; expand its result for recorded omissions")
                text = self._artifact_text(evidence["ref"], source_chat_id, str(evidence.get("sha256") or ""))
        else:
            from agent_engine.snapshot_store import SnapshotCursor
            snapshot = self.runtimes.snapshot_store.load_cursor_sync(SnapshotCursor(
                descriptor["thread_id"], int(descriptor["sequence"]), descriptor["snapshot_id"],
            ))
            if snapshot is None or str(snapshot.state.get("chat_id") or "") != source_chat_id:
                raise ToolError("Captured snapshot is unavailable or belongs to another session")
            text = json.dumps(_visible_messages(snapshot.state.get("messages") or []), ensure_ascii=False)
        start, cap = max(0, int(offset)), max(1, min(int(max_chars), 100_000))
        return {"schema": RESULT_SCHEMA, "view_id": view_id, "source": descriptor, "text": text[start:start + cap],
                "offset": start, "offset_unit": "Unicode characters", "total_chars": len(text),
                "has_more": start + cap < len(text), "next_offset": start + cap if start + cap < len(text) else None}


def context_handle(host, context, view_id: str):
    view = host.require_runtime().session_context._view(context.chat_id, view_id)
    methods = [
        {"name": "status", "params": [], "returns": "dict", "description": "Inspect this frozen source view and its coverage."},
        {"name": "refresh", "params": [], "returns": "context", "description": "Capture a new view of committed sources."},
        {"name": "read", "returns": "dict", "description": "Read bounded descriptors/neighbor pages; expand an ID for exact content.", "params": [
            {"name": "after", "type": "int", "default": 0}, {"name": "limit", "type": "int", "default": 50},
            {"name": "kind", "type": "str", "default": ""}, {"name": "around", "type": "str", "default": ""},
            {"name": "before", "type": "int", "default": 2}]},
        {"name": "search", "returns": "dict", "description": "Literal Unicode search within this view; returns IDs, snippets and search coverage.", "params": [
            {"name": "query", "type": "str", "required": True}, {"name": "limit", "type": "int", "default": 20},
            {"name": "after", "type": "int", "default": 0}, {"name": "kind", "type": "str", "default": ""}]},
        {"name": "expand", "returns": "dict", "description": "Expand source text or a cell part (source/result/output) with character continuation; output retains event/evidence omission metadata.", "params": [
            {"name": "source_id", "type": "str", "required": True}, {"name": "offset", "type": "int", "default": 0},
            {"name": "max_chars", "type": "int", "default": 12000}, {"name": "part", "type": "str", "default": "result"}]},
    ]
    return remote_handle_envelope(service="context", kind="view", handle_id=view_id, generation=1, revision=1,
                                  metadata={"chat_id": context.chat_id, "source_chat_id": view["source_chat_id"] or context.chat_id,
                                            "child_id": view["child_id"] or None, "frozen": True}, methods=methods,
                                  broker=host.require_runtime().broker, context=context)


def register_context_router(host):
    async def route(context, identity, method, arguments):
        if identity.get("kind") != "view" or int(identity.get("generation") or 0) != 1:
            raise ToolError("Invalid session context handle")
        service = host.require_runtime().session_context
        view_id = str(identity.get("id") or "")
        view = service._view(context.chat_id, view_id)
        if method == "refresh":
            if arguments:
                raise ToolError("Context refresh takes no arguments")
            import asyncio
            refreshed = await asyncio.to_thread(service.capture, context.chat_id, child_id=view["child_id"])
            return context_handle(host, context, refreshed)
        if method not in {"status", "read", "search", "expand"}:
            raise ToolError("Unsupported session context operation")
        import asyncio
        return await asyncio.to_thread(getattr(service, method), context.chat_id, view_id, **arguments)
    routers = getattr(host, "remote_handle_routers", None)
    if routers is None:
        routers = {}; host.remote_handle_routers = routers
    routers["context"] = route


def retire_fact_memory(database_path: str) -> None:
    """Remove only the explicitly retired tables, never the shared ASTB DB."""
    conn = sqlite_session_connection(database_path, autocommit=False)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("memory_fts", "session_memory_proposal", "session_memory_revision", "session_memory_item"):
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    finally:
        conn.close()
