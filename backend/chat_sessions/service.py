"""Durable chat sessions, transcripts, compaction state, and search."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from typing import Any, Callable, Mapping
from observability.run_receipts import sanitize_run_receipt
from message_context_extents import HOST_CONTEXT_EXTENTS_REVISION
from project_context import canonical_project_binding, stored_project_binding
from reasoning_summaries import SUMMARY_TEXT_LIMIT, SUMMARY_STEP_LIMIT

from .models import (
    BranchRecord,
    ConversationNotFound,
    ConversationRecord,
    ConversationTombstoned,
)
from .repository import ConversationRepository, _json, _new_id
from .projection import coverage, digest_rows, history_rows, verified_prefix


DEFAULT_TITLE = "New chat"
MAX_TITLE_CHARS = 80
MAX_MESSAGES = 1000
MAX_TURN_STEPS = 40


def _title_from(text: str) -> str:
    value = " ".join(str(text or "").split())
    if len(value) > MAX_TITLE_CHARS:
        value = value[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    return value or DEFAULT_TITLE


def _state_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _load_state(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        candidate = value.get("text")
        if candidate is None:
            candidate = value.get("content")
        if isinstance(candidate, str):
            return candidate
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _peer_origin(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or str(value.get("kind") or "") != "peer":
        return None
    peer_id = str(value.get("peer_id") or "").strip()[:512]
    message_id = str(value.get("message_id") or "").strip()[:512]
    if not peer_id or not message_id:
        return None
    return {"kind": "peer", "peer_id": peer_id, "message_id": message_id}


def _compact_evidence(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    output: list[dict[str, Any]] = []
    allowed = {"file", "folder", "url", "screen", "search", "command"}
    for item in raw[:8]:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in allowed:
            continue
        value = str(item.get("value") or "").strip()[:400]
        if not value and kind != "screen":
            continue
        row = {
            "id": str(item.get("id") or "")[:80],
            "kind": kind,
            "label": str(item.get("label") or "").strip()[:120],
            "value": value,
        }
        tool = str(item.get("tool") or "").strip()[:80]
        if tool:
            row["tool"] = tool
        output.append(row)
    return output


def _compact_steps(raw_steps: Any, *, limit: int = MAX_TURN_STEPS) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        return []
    output: list[dict[str, Any]] = []
    for raw in raw_steps[:limit]:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or "").strip()[:160]
        if not label:
            continue
        kind = str(raw.get("kind") or "note").strip().lower() or "note"
        if kind not in {"tool", "note", "step", "thinking"}:
            kind = "note"
        status = str(raw.get("status") or "done").strip().lower() or "done"
        public_summary = kind == "thinking" and raw.get("source") == "provider_summary"
        if public_summary and status == "running":
            status = "cancelled"  # A persisted unfinished snapshot is not a live provider stream.
        if status not in ({"ok", "error", "done", "cancelled", "discarded"} if public_summary else {"ok", "error", "done"}):
            status = "done"
        row: dict[str, Any] = {
            "id": str(raw.get("id") or "")[:40],
            "kind": kind,
            "label": label,
            "status": status,
            "ts": float(raw.get("ts") or 0) or 0,
        }
        if public_summary and type(raw.get("summary_revision")) is int:
            row["summary_revision"] = max(0, raw["summary_revision"])
        for key, aliases, limit in (
            ("detail", ("detail",), SUMMARY_TEXT_LIMIT if kind == "thinking" else 400),
            ("tool", ("tool",), 80),
            ("key", ("key",), 160),
            ("call_id", ("call_id", "callId"), 128),
            ("raw_status", ("raw_status", "rawStatus"), 64),
            ("args_preview", ("args_preview", "argsPreview"), 600),
            ("result_preview", ("result_preview", "resultPreview"), 800),
        ):
            value = str(next(
                (raw.get(alias) for alias in aliases if raw.get(alias)), ""
            ) or "").strip()[:limit]
            if value:
                row[key] = value
        for key, aliases in (
            ("started_at", ("started_at", "startedAt")),
            ("completed_at", ("completed_at", "completedAt")),
        ):
            try:
                value = float(next(
                    (raw.get(alias) for alias in aliases if raw.get(alias) is not None), 0
                ) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                row[key] = value
        for key, aliases in (
            ("duration_ms", ("duration_ms", "durationMs")),
            ("admission_ms", ("admission_ms", "admissionMs")),
        ):
            try:
                value = max(0, min(int(next(
                    (raw.get(alias) for alias in aliases if raw.get(alias) is not None), 0
                ) or 0), 86_400_000))
            except (TypeError, ValueError):
                value = 0
            if value:
                row[key] = value
        evidence = _compact_evidence(raw.get("evidence"))
        if evidence:
            row["evidence"] = evidence
        output.append(row)
    return output


def _provider_summary_steps(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [{**row, "source": "provider_summary"} for row in _compact_steps([
        item for item in value if isinstance(item, Mapping) and item.get("kind") == "thinking"
        and item.get("source") == "provider_summary" and str(item.get("id") or "").strip()
    ], limit=SUMMARY_STEP_LIMIT)]


def _compact_receipt(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    route = str(raw.get("route") or "")
    if route not in {"local", "cloud", ""}:
        route = ""
    try:
        duration = max(0, int(float(
            raw.get("durationMs", raw.get("duration_ms")) or 0
        )))
    except (TypeError, ValueError):
        duration = 0
    try:
        tool_count = max(0, int(
            raw.get("toolCount", raw.get("tool_count")) or 0
        ))
    except (TypeError, ValueError):
        tool_count = 0
    output: dict[str, Any] = {
        "model": str(raw.get("model") or "")[:120],
        "provider": str(raw.get("provider") or "")[:80],
        "route": route,
        "durationMs": duration,
        "toolCount": tool_count,
        "measurement": str(raw.get("measurement") or "estimated")[:40],
    }
    for primary, alternate, output_key in (
        ("promptTokens", "prompt_tokens", "promptTokens"),
        ("cachedInputTokens", "cached_input_tokens", "cachedInputTokens"),
    ):
        value = raw.get(primary)
        if value is None:
            value = raw.get(alternate)
        if value is not None:
            try:
                output[output_key] = max(0, int(float(value)))
            except (TypeError, ValueError):
                pass
    if not output["model"] and not duration and not tool_count:
        return None
    return output


class ChatSessionService:
    """Session selection and transcript projection over one SQL DAG.

    ``active_id`` is deliberately instance-local.  A Deck window can select a
    different branch without changing another window's selection.  Durable
    identity is the branch ``runtime_chat_id``; it is never confused with the
    native agent thread id.
    """

    def __init__(
        self,
        repository: ConversationRepository,
        *,
        max_messages: int = MAX_MESSAGES,
        active_id: str = "",
    ) -> None:
        self.repository = repository
        self.max_messages = max(1, int(max_messages))
        self.active_id: str | None = str(active_id or "").strip() or None
        self._runtime_ensure: Callable[..., Any] | None = None
        self._peer_display: Callable[..., Any] | None = None
        self._peer_sent_display: Callable[..., Any] | None = None
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self.repository._lock:
            conn = self.repository._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_session_state (
                        runtime_chat_id TEXT PRIMARY KEY,
                        branch_id TEXT NOT NULL UNIQUE,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        version INTEGER NOT NULL DEFAULT 1,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(branch_id) REFERENCES conversation_branch(branch_id)
                    );
                    """
                )
            finally:
                conn.close()

    def bind_runtime_lifecycle(self, ensure_runtime: Callable[..., Any]) -> None:
        self._runtime_ensure = ensure_runtime

    def bind_peer_display(self, resolver: Callable[..., Any], sent_resolver: Callable[..., Any]) -> None:
        self._peer_display = resolver
        self._peer_sent_display = sent_resolver

    def project_messages_for_display(self, messages: list[dict], *, chat_id: str = "") -> list[dict]:
        """Add canonical peer display data without rewriting model history.

        Resolving older rows uses their explicit origin, never text matching.
        A fork can display an inherited message with its original provenance.
        """
        projected = copy.deepcopy(messages)
        run_rows = {}
        for row in projected:
            if row.get("role") == "assistant":
                summaries = _provider_summary_steps(row.get("provider_summaries"))
                if summaries:
                    identities = {item["id"] for item in summaries}
                    steps = [item for item in row.get("steps") or [] if item.get("id") not in identities]
                    row["steps"] = sorted([*steps, *summaries], key=lambda item: float(item.get("ts") or 0))
            if row.get("role") == "assistant" and row.get("run_id"):
                owner = str(row.get("run_chat_id") or chat_id)
                if owner:
                    run_rows[(owner, str(row["run_id"]))] = row
            origin = _peer_origin(row.get("origin"))
            if row.get("role") != "user" or origin is None:
                row.pop("peer_display", None)
                continue
            if isinstance(row.get("peer_display"), dict):
                continue
            if callable(self._peer_display):
                display = self._peer_display(origin)
                if display is not None:
                    row["peer_display"] = display
        if callable(self._peer_sent_display):
            # One projection on the last assistant row of each exact run; a
            # steer can create several transcript pairs within that run.
            for (owner, run_id), row in run_rows.items():
                sent = self._peer_sent_display(owner, run_id)
                if sent and sent["messages"]:
                    row["peer_sent"] = sent["messages"]
                    if sent["has_more"]:
                        row["peer_sent_more"] = True
        return projected

    def _ensure_runtime(self, sid: str, *, is_new: bool) -> None:
        callback = self._runtime_ensure
        if callable(callback) and sid:
            callback(sid, is_new=is_new)

    def _resolve(
        self, sid: str, *, include_deleted: bool = False
    ) -> tuple[ConversationRecord, BranchRecord] | None:
        clean = str(sid or "").strip()
        if not clean:
            return None
        sql = (
            "SELECT c.*, b.branch_id AS b_branch_id, "
            "b.conversation_id AS b_conversation_id, b.name AS b_name, "
            "b.base_node_id AS b_base_node_id, b.head_node_id AS b_head_node_id, "
            "b.parent_branch_id AS b_parent_branch_id, "
            "b.runtime_chat_id AS b_runtime_chat_id, "
            "b.runtime_thread_id AS b_runtime_thread_id, "
            "b.runtime_run_id AS b_runtime_run_id, "
            "b.runtime_fork_mode AS b_runtime_fork_mode, "
            "b.runtime_fork_reason AS b_runtime_fork_reason, "
            "b.version AS b_version, b.created_at AS b_created_at, "
            "b.updated_at AS b_updated_at, b.tombstoned_at AS b_tombstoned_at "
            "FROM conversation_branch b JOIN conversation c "
            "ON c.conversation_id=b.conversation_id WHERE b.runtime_chat_id=?"
        )
        if not include_deleted:
            sql += " AND b.tombstoned_at IS NULL AND c.tombstoned_at IS NULL"
        with self.repository._read() as conn:
            row = conn.execute(sql, (clean,)).fetchone()
        if row is None:
            return None
        conversation = self.repository._conversation(row)
        if conversation is None:
            return None
        branch = BranchRecord(
            branch_id=str(row["b_branch_id"]),
            conversation_id=str(row["b_conversation_id"]),
            name=str(row["b_name"]),
            base_node_id=str(row["b_base_node_id"] or ""),
            head_node_id=str(row["b_head_node_id"] or ""),
            parent_branch_id=str(row["b_parent_branch_id"] or ""),
            runtime_chat_id=str(row["b_runtime_chat_id"] or ""),
            runtime_thread_id=str(row["b_runtime_thread_id"] or ""),
            runtime_run_id=str(row["b_runtime_run_id"] or ""),
            runtime_fork_mode=str(row["b_runtime_fork_mode"]),
            runtime_fork_reason=str(row["b_runtime_fork_reason"] or ""),
            version=int(row["b_version"]),
            created_at=float(row["b_created_at"]),
            updated_at=float(row["b_updated_at"]),
            tombstoned_at=float(row["b_tombstoned_at"] or 0),
        )
        return conversation, branch

    def _read_state(self, sid: str) -> dict[str, Any]:
        with self.repository._read() as conn:
            row = conn.execute(
                "SELECT state_json FROM conversation_session_state "
                "WHERE runtime_chat_id=?", (str(sid),)
            ).fetchone()
        return _load_state(row["state_json"] if row is not None else None)

    def _change_state(
        self,
        sid: str,
        change: Callable[[dict[str, Any]], bool],
        *, expected_context: Mapping | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        clean = str(sid or "").strip()
        now = time.time()
        with self.repository._write() as conn:
            owner = conn.execute(
                "SELECT b.branch_id, b.conversation_id, b.head_node_id FROM conversation_branch b "
                "JOIN conversation c ON c.conversation_id=b.conversation_id "
                "WHERE b.runtime_chat_id=? AND b.tombstoned_at IS NULL "
                "AND c.tombstoned_at IS NULL", (clean,)
            ).fetchone()
            if owner is None:
                return False, {}
            if expected_context is not None and verified_prefix(
                conn, owner, expected_context,
            ) is None:
                return False, {}
            row = conn.execute(
                "SELECT state_json, version FROM conversation_session_state "
                "WHERE runtime_chat_id=?", (clean,)
            ).fetchone()
            state = _load_state(row["state_json"] if row is not None else None)
            changed = bool(change(state))
            if changed:
                if row is None:
                    conn.execute(
                        "INSERT INTO conversation_session_state("
                        "runtime_chat_id, branch_id, state_json, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (clean, str(owner["branch_id"]), _state_json(state), now),
                    )
                else:
                    conn.execute(
                        "UPDATE conversation_session_state SET state_json=?, "
                        "version=version+1, updated_at=? WHERE runtime_chat_id=?",
                        (_state_json(state), now, clean),
                    )
            return changed, state

    def _messages(
        self, conversation: ConversationRecord, branch: BranchRecord,
        state: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        annotations = state.get("message_annotations")
        overlays = annotations if isinstance(annotations, Mapping) else {}
        messages: list[dict[str, Any]] = []
        for node in self.repository.history_nodes(branch.branch_id, limit=20_000):
            if node.role not in {"user", "assistant"}:
                continue
            message: dict[str, Any] = {
                "role": node.role,
                "text": _content_text(node.content),
                "ts": float(node.created_at),
            }
            for key in ("ticket_id", "attachments", "mood", "steps", "receipt", "origin",
                        "peer_display", "run_id", "run_chat_id", "provider_summaries"):
                if key in node.metadata:
                    message[key] = copy.deepcopy(node.metadata[key])
            overlay = overlays.get(node.node_id)
            if isinstance(overlay, Mapping):
                for key in ("steps", "receipt"):
                    if key in overlay:
                        message[key] = copy.deepcopy(overlay[key])
                    elif overlay.get(f"clear_{key}"):
                        message.pop(key, None)
            messages.append(message)
        # The application view is bounded; durable transcript rows remain
        # available for rehydration and local search.
        return self.project_messages_for_display(messages[-self.max_messages:])

    def _full_session(
        self, conversation: ConversationRecord, branch: BranchRecord
    ) -> dict[str, Any]:
        state = self._read_state(branch.runtime_chat_id)
        session: dict[str, Any] = {
            "id": branch.runtime_chat_id,
            "title": conversation.title,
            "created_at": conversation.created_at,
            "updated_at": max(conversation.updated_at, branch.updated_at),
            "messages": self._messages(conversation, branch, state),
            "project": stored_project_binding(state.get("project")),
        }
        if conversation.pinned:
            session["pinned"] = True
        if conversation.archived:
            session["archived"] = True
        for key in (
            "model_route", "context_projection", "last_run_receipt",
        ):
            if key in state:
                session[key] = copy.deepcopy(state[key])
        return session

    def _summary(
        self, conversation: ConversationRecord, branch: BranchRecord
    ) -> dict[str, Any]:
        session = self._full_session(conversation, branch)
        summary = {
            "id": branch.runtime_chat_id,
            "title": conversation.title,
            "created_at": conversation.created_at,
            "updated_at": max(conversation.updated_at, branch.updated_at),
            "message_count": len(session["messages"]),
        }
        for key in ("pinned", "archived", "project"):
            if key in session:
                summary[key] = copy.deepcopy(session[key])
        return summary

    def list_sessions(self) -> list[dict[str, Any]]:
        pairs: list[tuple[ConversationRecord, BranchRecord]] = []
        for conversation in self.repository.list_conversations(limit=None):
            for branch in self.repository.list_branches(conversation.conversation_id):
                pairs.append((conversation, branch))
        pairs.sort(key=lambda item: (
            0 if item[0].pinned else 1,
            -max(item[0].updated_at, item[1].updated_at),
            item[1].runtime_chat_id,
        ))
        return [self._summary(conversation, branch) for conversation, branch in pairs]

    def get_session(self, sid: str) -> dict[str, Any] | None:
        resolved = self._resolve(sid)
        return self._full_session(*resolved) if resolved is not None else None

    def has_session(self, sid: str) -> bool:
        return self._resolve(sid) is not None

    def create_session(self, title: str = DEFAULT_TITLE, *, make_active: bool = True,
                       request_id: str = "") -> str:
        request = str(request_id or "").strip()
        if len(request) > 512:
            raise ValueError("new chat request_id exceeds 512 characters")
        conversation_id = ("conv_" + hashlib.sha256(
            ("chat:new\0" + request).encode("utf-8")
        ).hexdigest()[:32]) if request else ""
        with self.repository._lock:
            prior = self.repository.get_conversation(conversation_id, include_deleted=True) if request else None
            if prior is not None and prior.deleted:
                raise ConversationTombstoned("the chat created by this request was deleted")
            conversation = prior or self.repository.create_conversation(
                title or DEFAULT_TITLE, conversation_id=conversation_id)
            branch = self.repository.require_branch(conversation.default_branch_id)
        sid = branch.runtime_chat_id
        with self._lock:
            if make_active:
                self.active_id = sid
        self._ensure_runtime(sid, is_new=prior is None)
        return sid

    def ensure_active(self) -> str:
        with self._lock:
            if self.active_id and self._resolve(self.active_id) is not None:
                sid = self.active_id
                self._ensure_runtime(sid, is_new=False)
                return sid
            sessions = self.list_sessions()
            if sessions:
                live = [row for row in sessions if not row.get("archived")]
                self.active_id = str((live[0] if live else sessions[0])["id"])
                sid = self.active_id
                self._ensure_runtime(sid, is_new=False)
                return sid
            return self.create_session()

    def get_active(self) -> str:
        return self.ensure_active()

    def set_active(self, sid: str) -> str:
        clean = str(sid or "").strip()
        with self._lock:
            if self._resolve(clean) is not None:
                self.active_id = clean
                self._ensure_runtime(clean, is_new=False)
                return clean
            return self.ensure_active()

    def get_model_route(self, sid: str) -> dict[str, Any]:
        session = self.get_session(sid)
        raw = (session or {}).get("model_route")
        if not isinstance(raw, Mapping):
            return {}
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in {"local", "cloud"}:
            return {}
        route = {
            "mode": mode,
            "provider": str(raw.get("provider") or "").strip()[:80],
            "model": str(raw.get("model") or "").strip()[:300],
        }
        effort = str(raw.get("reasoning_effort") or "").strip().lower()[:24]
        if effort:
            route["reasoning_effort"] = effort
        return route

    def set_model_route(self, sid: str, route: dict | None) -> bool:
        raw = route if isinstance(route, dict) else {}
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in {"local", "cloud"}:
            return False

        def change(state: dict[str, Any]) -> bool:
            state["model_route"] = {
                "mode": mode,
                "provider": str(raw.get("provider") or "").strip()[:80],
                "model": str(raw.get("model") or "").strip()[:300],
                "reasoning_effort": str(
                    raw.get("reasoning_effort") or ""
                ).strip().lower()[:24],
                "updated_at": time.time(),
            }
            return True

        return self._change_state(sid, change)[0]

    def get_project(self, sid: str) -> dict[str, str] | None:
        if self._resolve(sid) is None:
            return None
        raw = self._read_state(sid).get("project")
        if raw is None:
            return None
        project = stored_project_binding(raw)
        if project is None:
            from project_context import ProjectBindingError
            raise ProjectBindingError(
                "project_binding_corrupt",
                f"stored project binding is invalid for chat {sid}",
            )
        return project

    def set_project(self, sid: str, root: str | None) -> dict[str, str] | None:
        if self._resolve(sid) is None:
            raise ConversationNotFound(f"unknown chat session: {sid}")
        project = canonical_project_binding(root)

        def change(state: dict[str, Any]) -> bool:
            current = stored_project_binding(state.get("project"))
            if current == project:
                return False
            if project is None:
                state.pop("project", None)
            else:
                state["project"] = copy.deepcopy(project)
            return True

        self._change_state(sid, change)
        return copy.deepcopy(project)

    def get_context_projection(self, sid: str) -> dict[str, Any]:
        with self.repository._read() as conn:
            if self._context_owner(conn, sid) is None:
                return {}
            row = conn.execute("SELECT state_json FROM conversation_session_state WHERE runtime_chat_id=?", (sid,)).fetchone()
            raw = _load_state(row["state_json"] if row is not None else None).get("context_projection")
        if not isinstance(raw, Mapping):
            return {}
        if raw.get("kind") == "native_snapshot":
            return copy.deepcopy(dict(raw))
        messages = []
        for row in raw.get("messages") or []:
            if not isinstance(row, Mapping):
                continue
            role = str(row.get("role") or "").strip().lower()
            content = row.get("content")
            if role in {"user", "assistant", "system"} and isinstance(content, str):
                message = {"role": role, "content": content}
                if row.get("variant1_compaction") is True:
                    message["variant1_compaction"] = True
                    if row.get("variant1_compaction_revision") == 2:
                        message["variant1_compaction_revision"] = 2
                origin = _peer_origin(row.get("origin"))
                if role == "user" and origin is not None:
                    message["origin"] = origin
                messages.append(message)
        return {
            "messages": messages,
            "coverage": copy.deepcopy(raw.get("coverage")),
            "source_message_count": max(0, int(raw.get("source_message_count") or 0)),
            "context_limit_tokens": max(0, int(raw.get("context_limit_tokens") or 0)),
            "updated_at": float(raw.get("updated_at") or 0),
        }

    def set_context_projection(
        self, sid: str, messages: list, *, source_message_count: int,
        context_limit_tokens: int = 0,
        source_cursor: dict | None = None,
    ) -> bool:
        clean = []
        for row in messages or []:
            if not isinstance(row, Mapping):
                continue
            role = str(row.get("role") or "").strip().lower()
            content = row.get("content")
            if role in {"user", "assistant", "system"} and isinstance(content, str):
                message = {"role": role, "content": content}
                if row.get("variant1_compaction") is True:
                    message["variant1_compaction"] = True
                    if row.get("variant1_compaction_revision") == 2:
                        message["variant1_compaction_revision"] = 2
                origin = _peer_origin(row.get("origin"))
                if role == "user" and origin is not None:
                    message["origin"] = origin
                clean.append(message)

        source = self.canonical_context(sid)
        cursor = source_cursor or source.get("cursor")
        if not cursor or source_message_count != len(source.get("messages") or []):
            return False

        def change(state: dict[str, Any]) -> bool:
            state["context_projection"] = {
                "messages": clean,
                "source_message_count": max(0, int(source_message_count or 0)),
                "context_limit_tokens": max(0, int(context_limit_tokens or 0)),
                "updated_at": time.time(),
                "coverage": copy.deepcopy(cursor),
            }
            return True

        return self._change_state(sid, change, expected_context=cursor)[0]

    @staticmethod
    def _projection_messages(rows: list) -> list[dict]:
        projected: list[dict[str, Any]] = []
        for row in rows:
            role = str(row["role"] or "")
            if role not in {"user", "assistant"}:
                continue
            content = _content_text(json.loads(row["content_json"]))
            if not content:
                continue
            message: dict[str, Any] = {"role": role, "content": content}
            if role == "user":
                metadata = _load_state(row["metadata_json"])
                origin = _peer_origin(metadata.get("origin"))
                if origin is not None:
                    message["origin"] = origin
            projected.append(message)
        return projected

    @staticmethod
    def _context_owner(conn, sid: str):
        return conn.execute(
            "SELECT b.branch_id,b.conversation_id,b.head_node_id FROM conversation_branch b "
            "JOIN conversation c ON c.conversation_id=b.conversation_id "
            "WHERE b.runtime_chat_id=? AND b.tombstoned_at IS NULL AND c.tombstoned_at IS NULL",
            (sid,),
        ).fetchone()

    def canonical_context(self, sid: str, *, head_node_id: str = "") -> dict:
        """Unbounded canonical text and its exact ancestry/content coverage."""
        with self.repository._read() as conn:
            owner = self._context_owner(conn, sid)
            if owner is None:
                return {}
            if head_node_id:
                if not self.repository._is_reachable_tx(
                    conn, owner["conversation_id"], owner["head_node_id"], head_node_id,
                ):
                    return {}
                owner = {**dict(owner), "head_node_id": head_node_id}
            rows = history_rows(conn, owner["conversation_id"], owner["head_node_id"])
            return {"cursor": coverage(owner, rows), "messages": self._projection_messages(rows)}

    def context_projection_view(self, sid: str, covered: dict | None = None) -> dict:
        """One history scan yields verified suffix/cursor or canonical fallback."""
        with self.repository._read() as conn:
            owner = self._context_owner(conn, sid)
            if owner is None:
                return {"valid": False, "messages": [], "count": 0, "cursor": None}
            rows = history_rows(conn, owner["conversation_id"], owner["head_node_id"])
            valid = False
            prefix_count = 0
            if isinstance(covered, Mapping):
                count = covered.get("node_count")
                if (isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= len(rows)
                        and covered.get("conversation_id") == owner["conversation_id"]
                        and covered.get("branch_id") == owner["branch_id"]):
                    prefix = rows[:count]
                    valid = (covered.get("head_node_id") in {r["node_id"] for r in prefix}
                             or (not count and not covered.get("head_node_id")))
                    valid = valid and digest_rows(prefix) == covered.get("prefix_sha256")
                    prefix_count = count if valid else 0
            # Exact digest comparison makes unusual/nonchronological ancestry
            # conservative: it falls back rather than attaching a wrong prefix.
            cursor = dict(covered) if valid and prefix_count == len(rows) else coverage(owner, rows)
            count = 0
            for row in rows:
                if row["role"] not in {"user", "assistant"}:
                    continue
                raw = row["content_json"]
                count += int(raw != '""' if raw.startswith('"') else bool(_content_text(json.loads(raw))))
            canonical_users = []
            if valid:
                for row in rows[:prefix_count]:
                    if row["role"] != "user":
                        continue
                    content = _content_text(json.loads(row["content_json"]))
                    if not content:
                        continue
                    message: dict[str, Any] = {
                        "role": "user", "content": content,
                    }
                    metadata = _load_state(row["metadata_json"])
                    origin = _peer_origin(metadata.get("origin"))
                    if origin is not None:
                        message["origin"] = origin
                    canonical_users.append(message)
            return {"valid": valid, "count": count, "cursor": cursor,
                    "canonical_users": canonical_users,
                    "suffix": self._projection_messages(rows[prefix_count:]) if valid else [],
                    "messages": [] if valid else self._projection_messages(rows)}

    def clear_context_projection(self, sid: str, *, expected: dict) -> bool:
        def change(state: dict[str, Any]) -> bool:
            if state.get("context_projection") != expected:
                return False
            state.pop("context_projection", None)
            return True
        return self._change_state(sid, change)[0]

    def set_native_context_projection(self, sid: str, reference: dict, *,
                                      source_cursor: dict, model_route: dict,
                                      expected_projection: dict | None = None,
                                      expected_head_cursor: dict | None = None,
                                      evidence: dict | None = None) -> bool:
        """CAS a small checkpoint reference after transcript and snapshot commit."""
        if not isinstance(source_cursor, Mapping) or not source_cursor.get("prefix_sha256"):
            return False
        if not all(reference.get(k) for k in ("thread_id", "snapshot_id", "run_id")):
            return False
        if (not isinstance(reference.get("sequence"), int) or isinstance(reference["sequence"], bool)
                or reference["sequence"] < 1):
            return False
        clean_ref = {k: reference[k] for k in ("thread_id", "sequence", "snapshot_id", "run_id")}
        extent_revision = reference.get("host_context_extents_revision")
        if extent_revision is not None and (
            type(extent_revision) is not int
            or extent_revision != HOST_CONTEXT_EXTENTS_REVISION
        ):
            return False
        if extent_revision == HOST_CONTEXT_EXTENTS_REVISION:
            clean_ref["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
        def change(state: dict[str, Any]) -> bool:
            if expected_projection is not None and (state.get("context_projection") or {}) != expected_projection:
                return False
            state["context_projection"] = {
                "kind": "native_snapshot", "snapshot": clean_ref,
                "schema": "variant1.context-projection.native.v1",
                "coverage": copy.deepcopy(source_cursor),
                "model_route": {k: str(model_route.get(k) or "") for k in ("mode", "provider", "model", "wire_identity")},
                "updated_at": time.time(),
            }
            if extent_revision == HOST_CONTEXT_EXTENTS_REVISION:
                state["context_projection"]["host_context_extents_revision"] = (
                    HOST_CONTEXT_EXTENTS_REVISION
                )
            if evidence is not None:
                state["context_projection"].update(
                    schema="variant1.context-projection.native.v2", purpose="stopped_evidence",
                    evidence=copy.deepcopy(evidence),
                )
            return True
        return self._change_state(sid, change, expected_context=expected_head_cursor or source_cursor)[0]

    def get_last_run_receipt(self, sid: str) -> dict[str, Any]:
        session = self.get_session(sid)
        return sanitize_run_receipt((session or {}).get("last_run_receipt"))

    def set_last_run_receipt(
        self, sid: str, receipt: dict | None, *, expected_run_id: str = "",
    ) -> bool:
        expected = str(expected_run_id or "").strip()
        clean = sanitize_run_receipt(receipt)
        stopped_cursor = (
            self.canonical_context(sid).get("cursor")
            if clean.get("status") == "cancelled" and clean.get("settled") else None
        )

        def change(state: dict[str, Any]) -> bool:
            current = sanitize_run_receipt(state.get("last_run_receipt"))
            if expected and str(current.get("run_id") or "") != expected:
                return False
            if clean:
                state["last_run_receipt"] = clean
                if stopped_cursor and (state.get("stopped_context") or {}).get("run_id") != clean["run_id"]:
                    state["stopped_context"] = {"run_id": clean["run_id"], "coverage": stopped_cursor}
            else:
                state.pop("last_run_receipt", None)
            return True

        return self._change_state(sid, change, expected_context=stopped_cursor)[0]

    def get_stopped_context_coverage(self, sid: str, run_id: str) -> dict | None:
        with self.repository._read() as conn:
            row = conn.execute("SELECT state_json FROM conversation_session_state WHERE runtime_chat_id=?", (sid,)).fetchone()
        record = _load_state(row["state_json"] if row else None).get("stopped_context") or {}
        return copy.deepcopy(record.get("coverage")) if record.get("run_id") == run_id else None

    @staticmethod
    def _clean_message(raw: Mapping[str, Any]) -> dict[str, Any] | None:
        role = str(raw.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            return None
        text = str(raw.get("text") or "")
        metadata: dict[str, Any] = {}
        if role == "user":
            text = text.strip()
            attachments = []
            for item in (raw.get("attachments") or [])[:24]:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("name") or "").strip()[:200]
                if not name:
                    continue
                kind = str(item.get("kind") or "text").strip().lower() or "text"
                if kind not in {"image", "text", "path", "folder"}:
                    kind = "text"
                attachments.append({"name": name, "kind": kind})
            if not text and not attachments:
                return None
            ticket = str(raw.get("ticket_id") or "").strip()[:96]
            if ticket:
                metadata["ticket_id"] = ticket
            transcript_id = str(raw.get("transcript_id") or "").strip()[:160]
            if transcript_id:
                metadata["transcript_id"] = transcript_id
            if attachments:
                metadata["attachments"] = attachments
            origin = _peer_origin(raw.get("origin"))
            if origin is not None:
                metadata["origin"] = origin
        else:
            run_id = str(raw.get("run_id") or "").strip()[:512]
            if run_id:
                metadata["run_id"] = run_id
            mood = str(raw.get("mood") or "").strip()
            if mood:
                metadata["mood"] = mood
            steps = _compact_steps(raw.get("steps"))
            if steps:
                metadata["steps"] = steps
            summaries = _provider_summary_steps(raw.get("provider_summaries"))
            if summaries:
                metadata["provider_summaries"] = summaries
        return {"role": role, "content": text, "metadata": metadata}

    def append_messages(self, sid: str, messages: list[dict]) -> dict[str, Any] | None:
        clean = [
            row for row in (
                self._clean_message(raw) for raw in (messages or [])
                if isinstance(raw, Mapping)
            ) if row is not None
        ]
        if not clean:
            resolved = self._resolve(sid)
            return self._summary(*resolved) if resolved is not None else None

        # Snapshot display names/body at append time; the canonical peer store
        # also resolves historical rows that predate this metadata.
        if callable(self._peer_display):
            for row in clean:
                origin = _peer_origin(row["metadata"].get("origin"))
                if row["role"] == "user" and origin is not None:
                    display = self._peer_display(origin)
                    if display is not None:
                        row["metadata"]["peer_display"] = display
        for row in clean:
            if row["role"] == "assistant" and row["metadata"].get("run_id"):
                row["metadata"]["run_chat_id"] = str(sid)

        appended = self._append_segment_atomic(str(sid), clean)
        if appended is None:
            return None
        conversation, branch, inserted = appended
        meta = self._summary(conversation, branch)
        if inserted:
            # Exact head returned by the append transaction, not a later read
            # of a mutable branch. The finalizer consumes this local receipt.
            meta["_canonical_head"] = branch.head_node_id
        return meta

    def _append_segment_atomic(
        self, sid: str, clean: list[dict[str, Any]]
    ) -> tuple[ConversationRecord, BranchRecord, bool] | None:
        """Append every cleaned message, title, edge and reflog in one commit."""

        now = time.time()
        with self.repository._write() as conn:
            branch_row = conn.execute(
                "SELECT b.* FROM conversation_branch b JOIN conversation c "
                "ON c.conversation_id=b.conversation_id "
                "WHERE b.runtime_chat_id=? AND b.tombstoned_at IS NULL "
                "AND c.tombstoned_at IS NULL", (sid,)
            ).fetchone()
            branch = self.repository._branch(branch_row)
            if branch is None:
                return None
            conversation = self.repository._conversation(conn.execute(
                "SELECT * FROM conversation WHERE conversation_id=? "
                "AND tombstoned_at IS NULL", (branch.conversation_id,)
            ).fetchone())
            if conversation is None:
                raise ConversationNotFound(
                    f"conversation owner disappeared for runtime chat: {sid}"
                )

            # A native run keeps the same transcript identity across crash and
            # resume. If its append committed before the snapshot terminal CAS,
            # replay is an acknowledgement—not a second conversation turn.
            transcript_ids = {
                str((row.get("metadata") or {}).get("transcript_id") or "")
                for row in clean
                if row.get("role") == "user"
                and str((row.get("metadata") or {}).get("transcript_id") or "")
            }
            for transcript_id in transcript_ids:
                existing = conn.execute(
                    "SELECT 1 FROM conversation_turn t "
                    "JOIN conversation_node u ON u.node_id=t.user_node_id "
                    "WHERE t.branch_id=? "
                    "AND json_extract(u.metadata_json,'$.transcript_id')=? LIMIT 1",
                    (branch.branch_id, transcript_id),
                ).fetchone()
                if existing is not None:
                    return conversation, branch, False

            head = branch.head_node_id
            first_user = ""
            index = 0
            turn_number = 0
            while index < len(clean):
                current = clean[index]
                user = current if current["role"] == "user" else None
                assistant = current if current["role"] == "assistant" else None
                if user is not None and not first_user and str(user["content"]).strip():
                    first_user = str(user["content"])
                if user is not None and index + 1 < len(clean):
                    following = clean[index + 1]
                    if following["role"] == "assistant":
                        assistant = following
                        index += 1

                turn_number += 1
                turn_time = now + turn_number * 0.00001
                turn_id = _new_id("turn")
                old_head = head
                user_node_id = ""
                assistant_node_id = ""
                if user is not None:
                    user_node_id = self.repository._insert_node_tx(
                        conn,
                        conversation_id=conversation.conversation_id,
                        role="user",
                        content=user["content"],
                        content_ref="",
                        metadata=user["metadata"],
                        turn_id=turn_id,
                        created_at=turn_time,
                    )
                    self.repository._insert_edge_tx(
                        conn,
                        conversation_id=conversation.conversation_id,
                        from_node_id=head,
                        to_node_id=user_node_id,
                        kind="continuation",
                        metadata={},
                        created_at=turn_time,
                    )
                    head = user_node_id
                if assistant is not None:
                    assistant_node_id = self.repository._insert_node_tx(
                        conn,
                        conversation_id=conversation.conversation_id,
                        role="assistant",
                        content=assistant["content"],
                        content_ref="",
                        metadata=assistant["metadata"],
                        turn_id=turn_id,
                        created_at=turn_time + 0.000001,
                    )
                    self.repository._insert_edge_tx(
                        conn,
                        conversation_id=conversation.conversation_id,
                        from_node_id=head,
                        to_node_id=assistant_node_id,
                        kind="continuation",
                        metadata={},
                        created_at=turn_time + 0.000001,
                    )
                    head = assistant_node_id

                conn.execute(
                    "INSERT INTO conversation_turn(turn_id, conversation_id, "
                    "branch_id, user_node_id, assistant_node_id, run_id, "
                    "receipt_json, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)",
                    (
                        turn_id, conversation.conversation_id, branch.branch_id,
                        user_node_id, assistant_node_id,
                        str((assistant or {}).get("metadata", {}).get("run_id") or ""),
                        _json({}), turn_time,
                        turn_time,
                    ),
                )
                self.repository._reflog_tx(
                    conn, conversation.conversation_id, branch.branch_id,
                    old_head, head, "turn.appended", "assistant",
                    {"turn_id": turn_id, "status": "completed"},
                    created_at=turn_time,
                )
                index += 1

            final_time = now + max(1, turn_number) * 0.00001
            changed = conn.execute(
                "UPDATE conversation_branch SET head_node_id=?, version=version+1, "
                "updated_at=? WHERE branch_id=? AND version=? "
                "AND tombstoned_at IS NULL",
                (head, final_time, branch.branch_id, branch.version),
            )
            if changed.rowcount != 1:
                self.repository._raise_branch_cas(
                    conn, branch.branch_id, branch.version
                )
            title = (
                _title_from(first_user)
                if first_user and conversation.title == DEFAULT_TITLE
                else conversation.title
            )
            changed = conn.execute(
                "UPDATE conversation SET title=?, version=version+1, updated_at=? "
                "WHERE conversation_id=? AND version=? AND tombstoned_at IS NULL",
                (
                    title, final_time, conversation.conversation_id,
                    conversation.version,
                ),
            )
            if changed.rowcount != 1:
                self.repository._raise_conversation_cas(
                    conn, conversation.conversation_id, conversation.version
                )
            final_conversation = self.repository._conversation(conn.execute(
                "SELECT * FROM conversation WHERE conversation_id=?",
                (conversation.conversation_id,),
            ).fetchone())
            final_branch = self.repository._branch(conn.execute(
                "SELECT * FROM conversation_branch WHERE branch_id=?",
                (branch.branch_id,),
            ).fetchone())
            if final_conversation is None or final_branch is None:
                raise ConversationNotFound("appended conversation disappeared")
            return final_conversation, final_branch, True

    def has_message_ticket(self, sid: str, ticket_id: str) -> bool:
        target = str(ticket_id or "").strip()
        if not target:
            return False
        resolved = self._resolve(sid)
        if resolved is None:
            return False
        _conversation, branch = resolved
        with self.repository._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM conversation_turn t "
                "JOIN conversation_node u ON u.node_id=t.user_node_id "
                "WHERE t.branch_id=? "
                "AND json_extract(u.metadata_json,'$.ticket_id')=? LIMIT 1",
                (branch.branch_id, target[:96]),
            ).fetchone()
        return row is not None

    def reply_for_message_ticket(self, sid: str, ticket_id: str) -> str:
        """Return the assistant paired with one exact durable ingress ticket."""

        target = str(ticket_id or "").strip()[:96]
        resolved = self._resolve(sid)
        if not target or resolved is None:
            return ""
        _conversation, branch = resolved
        with self.repository._read() as conn:
            row = conn.execute(
                "SELECT a.* FROM conversation_turn t "
                "JOIN conversation_node u ON u.node_id=t.user_node_id "
                "JOIN conversation_node a ON a.node_id=t.assistant_node_id "
                "WHERE t.branch_id=? "
                "AND json_extract(u.metadata_json,'$.ticket_id')=? "
                "ORDER BY t.created_at,t.turn_id LIMIT 1",
                (branch.branch_id, target),
            ).fetchone()
        node = self.repository._node(row)
        return _content_text(node.content) if node is not None else ""

    def append_if_absent(
        self, sid: str, ticket_id: str, message: dict,
    ) -> tuple[dict[str, Any] | None, bool]:
        target = str(ticket_id or "").strip()[:96]
        if not target:
            raise ValueError("ticket_id is required")
        with self.repository._lock:
            if self.has_message_ticket(sid, target):
                resolved = self._resolve(sid)
                return (
                    self._summary(*resolved) if resolved is not None else None,
                    False,
                )
            row = dict(message or {})
            row["ticket_id"] = target
            return self.append_messages(sid, [row]), True

    def annotate_last_assistant(
        self, sid: str, *, steps: list = None, receipt: dict = None,
        run_id: str = "",
    ) -> dict[str, Any] | None:
        resolved = self._resolve(sid)
        if resolved is None:
            return None
        _, branch = resolved
        assistant_nodes = [
            node for node in self.repository.history_nodes(branch.branch_id, limit=20_000)
            if node.role == "assistant"
            and (not run_id or str(node.metadata.get("run_id") or "") == run_id)
        ]
        if not assistant_nodes:
            return None
        node_id = assistant_nodes[-1].node_id

        def change(state: dict[str, Any]) -> bool:
            raw = state.get("message_annotations")
            annotations = dict(raw) if isinstance(raw, Mapping) else {}
            overlay_raw = annotations.get(node_id)
            overlay = dict(overlay_raw) if isinstance(overlay_raw, Mapping) else {}
            if steps is not None:
                compact = _compact_steps(steps)
                if compact:
                    overlay["steps"] = compact
                    overlay.pop("clear_steps", None)
                else:
                    overlay.pop("steps", None)
                    overlay["clear_steps"] = True
            if receipt is not None:
                compact_receipt = _compact_receipt(receipt)
                if compact_receipt:
                    overlay["receipt"] = compact_receipt
                    overlay.pop("clear_receipt", None)
                else:
                    overlay.pop("receipt", None)
                    overlay["clear_receipt"] = True
            annotations[node_id] = overlay
            state["message_annotations"] = dict(
                list(annotations.items())[-self.max_messages:]
            )
            return True

        changed, _ = self._change_state(sid, change)
        return self.get_session(sid) if changed else None

    def set_flag(
        self, sid: str, *, pinned: bool | None = None,
        archived: bool | None = None,
    ) -> bool:
        resolved = self._resolve(sid)
        if resolved is None:
            return False
        conversation = resolved[0]
        self.repository.update_conversation(
            conversation.conversation_id,
            expected_version=conversation.version,
            pinned=pinned,
            archived=archived,
        )
        return True

    def rename(self, sid: str, title: str) -> bool:
        resolved = self._resolve(sid)
        if resolved is None:
            return False
        clean = " ".join(str(title or "").split())[:MAX_TITLE_CHARS] or DEFAULT_TITLE
        conversation = resolved[0]
        self.repository.update_conversation(
            conversation.conversation_id,
            expected_version=conversation.version,
            title=clean,
        )
        return True

    def delete(self, sid: str) -> str:
        """Tombstone the SQL owner; runtime purge remains a separate decision."""

        resolved = self._resolve(sid)
        if resolved is not None:
            conversation, branch = resolved
            if branch.branch_id == conversation.default_branch_id:
                self.repository.tombstone_conversation(
                    conversation.conversation_id,
                    expected_version=conversation.version,
                )
            else:
                self.repository.tombstone_branch(
                    branch.branch_id, expected_version=branch.version, actor="user"
                )
        with self._lock:
            if self.active_id == str(sid):
                self.active_id = None
        return self.ensure_active()

    def recent_convo(self, sid: str, n: int | None = 8) -> list[dict[str, str]]:
        if n is None:
            return self.canonical_context(sid).get("messages", [])
        session = self.get_session(sid)
        rows = list((session or {}).get("messages") or [])
        if n is not None:
            rows = rows[-max(1, int(n)):]
        return [
            {"role": str(row["role"]), "content": str(row["text"])}
            for row in rows
            if isinstance(row, Mapping)
            and row.get("role") in {"user", "assistant"}
            and row.get("text")
        ]

    def search(
        self, query: str, *, limit: int = 8, max_sessions: int = 200,
    ) -> list[dict[str, Any]]:
        if not str(query or "").strip():
            return []
        hits = self.repository.search(str(query), limit=max(1, int(limit)))
        out = []
        for hit in hits:
            conversation = self.repository.get_conversation(hit.conversation_id)
            if conversation is None:
                continue
            branch = self.repository.get_branch(conversation.default_branch_id)
            if branch is None:
                continue
            out.append({
                "session_id": branch.runtime_chat_id,
                "title": conversation.title,
                "ts": hit.created_at,
                "role": hit.role,
                "snippet": hit.preview,
                "score": hit.score,
            })
        return out[:max(1, int(limit))]

__all__ = ["ChatSessionService"]
