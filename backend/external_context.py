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
import base64
import codecs

import context_index as index
import context_capture as ingestion
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

            self._search_indexed = index.initialize(conn)

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
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("DELETE FROM context_view WHERE chat_id=? OR source_chat_id=?", (str(chat_id), str(chat_id)))
            conn.execute('DELETE FROM context_canonical_head WHERE source_chat_id=?', (str(chat_id),))
            conn.execute('DELETE FROM context_stream WHERE source_chat_id=?', (str(chat_id),))
            conn.execute('DELETE FROM context_record WHERE source_chat_id=?', (str(chat_id),))
            conn.execute("DELETE FROM context_text WHERE NOT EXISTS(SELECT 1 FROM context_record s WHERE s.text_key=context_text.text_key)")

    @staticmethod
    def _index_text(conn, text: str) -> str:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if conn.execute("SELECT 1 FROM context_text WHERE text_key=?", (key,)).fetchone() is None:
            conn.execute("INSERT INTO context_text(text_key,search_text,search_folded) VALUES(?,?,?)", (key, text, text.casefold()))
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
        try:
            metadata = self.artifacts.stat(ref, scope=chat_id)
            raw = self.artifacts.read_bytes_scoped(ref, chat_id)
        except (FileNotFoundError, PermissionError) as exc:
            raise ToolError('Retained context artifact or scoped grant is unavailable', code='context_source_unavailable') from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest != metadata.sha256 or (expected_sha256 and digest != expected_sha256):
            raise ToolError("Session context artifact failed its source integrity check")
        return raw.decode("utf-8", errors="strict")

    def capture(self, chat_id: str, *, child_id: str = "") -> str:
        reader = str(chat_id)
        if not self._has_owner(reader):
            raise ToolError('Session context requires an existing, live session')
        owner = self._child_source(reader, child_id) if child_id else reader
        if not self._has_owner(owner):
            raise ToolError('Session context source owner is unavailable')
        view_id = 'context_' + uuid.uuid4().hex
        with self._lock, self._connect() as conn:
            # Serialize captures across service instances before examining shared
            # stream bounds. A failed capture publishes neither rows nor a view.
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('PRAGMA temp_store=FILE')
            head = self.sessions.context_head(owner)
            canonical, cursor = ingestion.canonical(self, conn, owner, head, _text)
            last = self.kernel.cell_ledger.tail(owner, limit=1)
            upper = int(last[-1].sequence) if last else 0
            cells = ingestion.cells(self, conn, owner, upper)
            watermarks = {'canonical': cursor, 'ledger_through_sequence': upper, 'snapshot_heads': {},
                          'atomic_across_owners': False, 'source_chat_id': owner,
                          'canonical_available': cursor is not None,
                          'source_gaps': [] if cursor is not None else ['No canonical conversation owner; only retained runtime evidence is available']}
            conn.execute('INSERT INTO context_view(view_id,chat_id,watermarks_json,created_at,source_chat_id,child_id) VALUES(?,?,?,?,?,?)',
                         (view_id, reader, '{}', time.time(), owner, child_id))
            position, offset = 0, 0
            for target in (canonical, cells):
                if target and target['count']:
                    offset = index.span(conn, view_id, target, position, offset)
                    position += 1
            native, metadata, boundary = ingestion.snapshots(self, conn, owner)
            if native and native['count']:
                index.span(conn, view_id, native, position, offset)
            watermarks.update(metadata)
            watermarks['native_commits'] = boundary
            watermarks['cell_search_coverage'] = 'Per-source search_incomplete marks unavailable or invalid artifacts; exact expansion retains omissions'
            conn.execute('UPDATE context_view SET watermarks_json=? WHERE view_id=?', (json.dumps(watermarks), view_id))
            if not self._has_owner(owner) or not self._has_owner(reader):
                raise ToolError('Session context owner was deleted during capture')
            if child_id and self._child_source(reader, child_id) != owner:
                raise ToolError('Session context child changed during capture')
            current = self.sessions.context_head(owner)
            if head and (not current or current['conversation_id'] != head['conversation_id']):
                raise ToolError('Session context conversation owner changed during capture')
        return view_id

    def list_views(self, chat_id: str, *, child_id: str = '', after: str = '', limit: int = 20):
        if not self._has_owner(chat_id):
            raise ToolError('Session context owner has been deleted or is unavailable')
        owner = self._child_source(chat_id, child_id) if child_id else chat_id
        if not self._has_owner(owner):
            raise ToolError('Session context source owner has been deleted or is unavailable')
        cursor = None
        if after:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(str(after).encode('ascii')))
                if len(cursor) != 4 or cursor[:2] != [chat_id, child_id] or not isinstance(cursor[2], (int, float)) or not isinstance(cursor[3], str):
                    raise ValueError('Invalid scope')
            except (ValueError, TypeError, UnicodeError) as exc:
                raise ToolError('Context view cursor is invalid or outside this scope') from exc
        cap = max(1, min(int(limit), 100))
        params = [chat_id, child_id, owner]
        where = "WHERE chat_id=? AND child_id=? AND (source_chat_id=? OR (source_chat_id='' AND child_id='')) "
        if cursor:
            where += 'AND (created_at,view_id)<(?,?) '
            params.extend(cursor[2:])
        params.append(cap + 1)
        with self._lock, self._connect() as conn:
            rows = conn.execute('SELECT view_id,created_at,source_chat_id,child_id FROM context_view ' + where + 'ORDER BY created_at DESC,view_id DESC LIMIT ?', params).fetchall()
        items = [{**dict(row), 'source_chat_id': row['source_chat_id'] or owner} for row in rows[:cap]]
        next_cursor = None
        if len(rows) > cap:
            last = rows[cap-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([chat_id, child_id, last['created_at'], last['view_id']]).encode('utf-8')).decode('ascii')
        return {'schema': RESULT_SCHEMA, 'items': items, 'has_more': len(rows) > cap, 'next_cursor': next_cursor}

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
                "SELECT s.kind,SUM(v.count) AS n FROM context_view_span v JOIN context_stream s ON s.stream_id=v.stream_id WHERE v.view_id=? GROUP BY s.kind", (view_id,))}
        return {"schema": RESULT_SCHEMA, "view_id": view_id, "counts": counts,
                "watermarks": json.loads(view["watermarks_json"]),
                "coverage": {
                    "messages": "captured canonical readable text",
                    "cells": "retained source/result text; output-event envelopes expand separately and retain omissions",
                    "snapshots": "metadata search; visible messages/calls on expansion, excluding opaque reasoning and media",
                    "limits": "unsaved or discarded payloads cannot be reconstructed; projections may repeat source evidence",
                },
                "ordering": "stable canonical causal prefix, ledger sequence, then native commit storage order; links carry causality",
                "guidance": "Read/search historical evidence on demand. Refresh explicitly for newer commits. Snapshots are projections, not additional actions."}

    def read(self, chat_id: str, view_id: str, *, after: int = 0, limit: int = 50,
             kind: str = '', around: str = '', before: int = 2):
        self._view(chat_id, view_id)
        cap = max(1, min(int(limit), 200))
        with self._lock, self._connect() as conn:
            if around:
                anchor = index.rows(conn, view_id, source_id=around, limit=1)
                if not anchor:
                    raise ToolError('Context anchor is outside the captured view')
                after = max(0, anchor[0]['ordinal'] - max(0, min(int(before), 100)))
            rows = index.rows(conn, view_id, after=max(0, int(after)), limit=cap+1, kind=kind)
        items = [{**json.loads(row['descriptor_json']), 'preview': row['search_text'][:600], 'ordinal': row['ordinal']} for row in rows[:cap]]
        return {'schema': RESULT_SCHEMA, 'view_id': view_id, 'items': items, 'has_more': len(rows)>cap,
                'next_cursor': int(rows[cap-1]['ordinal'])+1 if len(rows)>cap else None}

    def iter_records(self, chat_id: str, view_id: str, *, page_size: int = 100):
        after = 0
        while True:
            page = self.read(chat_id, view_id, after=after, limit=page_size)
            yield from page['items']
            if not page['has_more']:
                break
            after = page['next_cursor']

    def search(self, chat_id: str, view_id: str, *, query: str, limit: int = 20, after: int = 0, kind: str = ""):
        self._view(chat_id, view_id)
        query = str(query).strip()
        if not query or len(query) > 1000:
            raise ToolError("Context search needs a nonempty query of at most 1000 characters; use read for chronology")
        cap = max(1, min(int(limit), 100))
        with self._lock, self._connect() as conn:
            rows = index.rows(conn, view_id, after=max(0, int(after)), limit=cap+1, kind=kind,
                              folded=query.casefold(), indexed=self._search_indexed)
        items = []
        for row in rows[:cap]:
            text = row["search_text"]
            folded_position = text.casefold().find(query.casefold())
            # Case folding can expand characters (e.g. Ăź -> ss). Map back to
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
                          "offset_unit": "Unicode characters", "match_offset_scope": "indexed search projection"})
        return {"schema": RESULT_SCHEMA, "view_id": view_id, "items": items, "has_more": len(rows) > cap,
                "next_cursor": int(rows[cap - 1]["ordinal"]) + 1 if len(rows) > cap else None,
                "coverage": "canonical text and retained cell source/result text; snapshot metadata only, not all modalities",
                "search_backend": "fts5-trigram+literal-verification" if self._search_indexed and len(query.casefold()) >= 3 and '\0' not in query else "literal-scan"}

    def _descriptor(self, chat_id, view_id, source_id):
        view = self._view(chat_id, view_id)
        with self._lock, self._connect() as conn:
            rows = index.rows(conn, view_id, source_id=str(source_id), limit=1)
        if not rows:
            raise ToolError('Context source is outside the captured view')
        return view, json.loads(rows[0]['descriptor_json'])

    def _expansion_text(self, view, descriptor, part):
        owner = view['source_chat_id'] or view['chat_id']
        if descriptor['kind'] == 'message':
            cursor = json.loads(view['watermarks_json'])['canonical']
            source = self.sessions.context_source_record(owner, cursor['conversation_id'], descriptor['node_id'])
            if hashlib.sha256(source['content_json'].encode('utf-8')).hexdigest() != descriptor['sha256']:
                raise ToolError('Captured context source changed; inspect source integrity')
            value = json.loads(source['content_json'])
            return value if isinstance(value, str) else source['content_json']
        if descriptor['kind'] == 'cell':
            ref, expected = self._cell_artifact(descriptor, owner, part)
            return self._artifact_text(ref, owner, expected)
        from agent_engine.snapshot_store import SnapshotCursor
        snapshot = self.runtimes.snapshot_store.load_cursor_sync(SnapshotCursor(
            descriptor['thread_id'], int(descriptor['sequence']), descriptor['snapshot_id']))
        if snapshot is None or str(snapshot.state.get('chat_id') or '') != owner:
            raise ToolError('Captured snapshot is unavailable or belongs to another session', code='context_source_unavailable')
        return json.dumps(_visible_messages(snapshot.state.get('messages') or []), ensure_ascii=False)

    def _cell_artifact(self, descriptor, owner, part):
        if part not in {'source', 'result', 'output'}:
            raise ToolError('Cell expansion part must be source, result or output')
        artifact_part = 'result' if part == 'output' else part
        ref = descriptor.get(artifact_part + '_ref')
        if not ref:
            raise ToolError('This cell has no retained ' + artifact_part + ' evidence', code='context_source_unavailable')
        expected = str(descriptor.get(artifact_part + '_sha256') or '')
        if part == 'output':
            evidence = descriptor.get('output_evidence') if 'output_evidence' in descriptor else json.loads(self._artifact_text(ref, owner, expected)).get('output_evidence')
            evidence = evidence or {}
            if not evidence.get('ref'):
                raise ToolError('This cell has no retained output-event evidence; expand its result for recorded omissions', code='context_source_unavailable')
            ref, expected = evidence['ref'], str(evidence.get('sha256') or '')
        return ref, expected

    def iter_expansion(self, chat_id, view_id, source_id, *, part='result', chunk_chars=16384):
        view, descriptor = self._descriptor(chat_id, view_id, source_id)
        cap, offset = max(1, min(int(chunk_chars), 100000)), 0
        owner = view['source_chat_id'] or chat_id
        if descriptor['kind'] == 'cell':
            ref, expected = self._cell_artifact(descriptor, owner, part)
            try:
                metadata = self.artifacts.stat(ref, scope=owner)
            except (FileNotFoundError, PermissionError) as exc:
                raise ToolError('Retained context artifact or scoped grant is unavailable', code='context_source_unavailable') from exc
            if expected and metadata.sha256 != expected:
                raise ToolError('Session context artifact failed its source integrity check')
            decoder = codecs.getincrementaldecoder('utf-8')(errors='strict')
            buffer = ''
            for raw in self._artifact_chunks(ref, owner):
                self._view(chat_id, view_id)
                buffer += decoder.decode(raw)
                while len(buffer) >= cap:
                    yield {'text': buffer[:cap], 'offset': offset, 'offset_unit': 'Unicode characters',
                           'eof': False, 'integrity': 'pending'}
                    offset += cap
                    buffer = buffer[cap:]
            buffer += decoder.decode(b'', final=True)
        else:
            # Native codecs and structured canonical records currently decode
            # one retained payload. Never materialize the complete archive.
            text = self._expansion_text(view, descriptor, part)
            for start in range(0, max(0, len(text)-cap), cap):
                self._view(chat_id, view_id)
                yield {'text': text[start:start+cap], 'offset': start, 'offset_unit': 'Unicode characters',
                       'eof': False, 'integrity': 'verified'}
                offset = start + cap
            buffer = text[offset:]
        self._view(chat_id, view_id)
        yield {'text': buffer, 'offset': offset, 'offset_unit': 'Unicode characters',
               'eof': True, 'integrity': 'verified', 'total_chars': offset+len(buffer)}

    def _artifact_chunks(self, ref, owner):
        try:
            yield from self.artifacts.iter_bytes(ref, scope=owner, chunk_size=65536, verify=True)
        except (FileNotFoundError, PermissionError) as exc:
            raise ToolError('Retained context artifact or scoped grant is unavailable', code='context_source_unavailable') from exc

    def expand(self, chat_id: str, view_id: str, *, source_id: str, offset: int = 0,
               max_chars: int = 12000, part: str = 'result'):
        view, descriptor = self._descriptor(chat_id, view_id, source_id)
        start, cap = max(0, int(offset)), max(1, min(int(max_chars), 100000))
        text = self._expansion_text(view, descriptor, part)
        return {'schema': RESULT_SCHEMA, 'view_id': view_id, 'source': descriptor, 'text': text[start:start+cap],
                'offset': start, 'offset_unit': 'Unicode characters', 'total_chars': len(text),
                'has_more': start+cap<len(text), 'next_offset': start+cap if start+cap<len(text) else None}


def context_handle(host, context, view_id: str):
    view = host.require_runtime().session_context._view(context.chat_id, view_id)
    methods = [
        {"name": "status", "params": [], "returns": "dict", "description": "Inspect this frozen source view and its coverage."},
        {"name": "refresh", "params": [], "returns": "context", "description": "Capture a new view of committed sources."},
        {"name": "read", "returns": "dict", "description": "Read bounded descriptor items with source_id and preview; expand an ID for exact content.", "params": [
            {"name": "after", "type": "int", "default": 0}, {"name": "limit", "type": "int", "default": 50},
            {"name": "kind", "type": "str", "default": ""}, {"name": "around", "type": "str", "default": ""},
            {"name": "before", "type": "int", "default": 2}]},
        {"name": "search", "returns": "dict", "description": "Literal Unicode search; items contain source_id, snippet and match_offset in the indexed projection, plus search coverage. JSON/snapshot expansion offsets can differ.", "params": [
            {"name": "query", "type": "str", "required": True}, {"name": "limit", "type": "int", "default": 20},
            {"name": "after", "type": "int", "default": 0}, {"name": "kind", "type": "str", "default": ""}]},
        {"name": "expand", "returns": "dict", "description": "Read ['text'] for exact source or cell part (source/result/output), following next_offset for continuation. Output retains event/evidence omission metadata.", "params": [
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
