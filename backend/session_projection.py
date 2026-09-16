"""Project the canonical chat transcript into one model-sized prompt view."""

from __future__ import annotations

import inspect
import copy
import logging
import hashlib
import json
from typing import Any, Awaitable, Callable

from message_context_extents import (
    HOST_CONTEXT_EXTENTS_REVISION,
    canonical_user_overlay,
)
from model_runtime.message_graph import build_message_graph
from transcript_economy import approx_tokens, ctx_compress_threshold

_LOG = logging.getLogger(__name__)


def _legacy_compaction(messages: list) -> bool:
    """Old accepted projections may have omitted intermediate user turns."""
    return any(isinstance(row, dict) and row.get("variant1_compaction") is True
               and row.get("variant1_compaction_revision") != 2 for row in messages)


def projection_model_route(router, sessions, sid: str, *, selected: dict | None = None) -> dict:
    bound = getattr(router, "bound_model_route", None)
    selected = selected or (bound() if callable(bound) else None)
    if not isinstance(selected, dict):
        from model_runtime.context import session_model_route

        selected = session_model_route(sessions, sid, router)
    route = {key: str(selected.get(key) or "") for key in ("mode", "provider", "model")}
    identity = {}
    profile_for = getattr(router, "provider_profile", None)
    if callable(profile_for):
        from model_runtime.context import model_route_support_coordinates

        profile = profile_for(route["provider"])
        base_for = getattr(router, "provider_base_url", None)
        account_for = getattr(router, "oauth_account_id", None)
        identity = {"api_style": str(getattr(profile, "api_style", "") or ""),
                    "adapter": str(model_route_support_coordinates(router, selected).get("adapter") or ""),
                    "endpoint": str(base_for(route["provider"]) if callable(base_for) else ""),
                    "account": str(account_for(route["provider"]) if callable(account_for) else "")}
    if route["mode"] == "local":
        engine = getattr(router, "engine", None)
        identity = {"adapter": str(getattr(engine, "request_adapter", "") or ""),
                    "endpoint": str(getattr(engine, "base_url", "") or "")}
    # No raw endpoint/account value is persisted. Effort alone does not change
    # replay compatibility; endpoint or native wire changes do.
    if identity:
        route["wire_identity"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return route


def promote_native_projection(sessions, sid: str, reference: dict, *,
                              canonical_head: str, router, transcript_id: str = "") -> bool:
    """Publish only after the exact canonical append and terminal snapshot exist."""
    if not transcript_id or reference.get("run_id") != transcript_id:
        return False
    capture = getattr(sessions, "canonical_context", None)
    setter = getattr(sessions, "set_native_context_projection", None)
    if not callable(capture) or not callable(setter):
        return False
    source = capture(sid, head_node_id=canonical_head)
    cursor = source.get("cursor")
    if not cursor:
        return False
    return setter(sid, reference, source_cursor=cursor, model_route=projection_model_route(router, sessions, sid))


def _cursor(state: dict):
    from agent_engine.snapshot_store import SnapshotCursor

    ref = state.get("snapshot") or {}
    return SnapshotCursor(str(ref["thread_id"]), int(ref["sequence"]), str(ref["snapshot_id"]))


def _snapshot_tail(snapshot, state: dict, sid: str, model_route: dict | None) -> list | None:
    """Reuse verified native evidence with fresh per-turn system/context/images."""
    from transcript_economy import COMPACT_SUMMARY_MARKER

    if snapshot is None:
        return None
    if state.get("purpose") == "stopped_evidence":
        from stopped_evidence import validate_accepted_snapshot
        return validate_accepted_snapshot(snapshot, state, sid)
    if (state.get("schema") != "variant1.context-projection.native.v1"
            or snapshot.source != "chat" or snapshot.status != "completed"):
        raise ValueError("invalid terminal projection contract")
    graph = snapshot.state
    extent_revision = state.get("host_context_extents_revision")
    if extent_revision is not None and (
        type(extent_revision) is not int
        or extent_revision != HOST_CONTEXT_EXTENTS_REVISION
    ):
        raise ValueError("invalid host-context extent revision")
    if graph.get("host_context_extents_revision") != extent_revision:
        raise ValueError("host-context extent revision does not match snapshot")
    if (str(graph.get("chat_id") or (graph.get("work_scope") or {}).get("chat_id") or "") != sid
            or (graph.get("output") or {}).get("transcript_committed") is not True
            or snapshot.run_id != (state.get("snapshot") or {}).get("run_id")):
        raise ValueError("invalid terminal projection ownership")
    if model_route is None or state.get("model_route") != {
        key: str(model_route.get(key) or "") for key in ("mode", "provider", "model", "wire_identity")
    }:
        # Opaque replay belongs to the original provider/model. Route changes
        # use canonical text rather than replaying another model's internals.
        return None
    messages = copy.deepcopy(graph.get("messages") or [])
    if _legacy_compaction(messages):
        # Rebuild the disposable view from canonical user history once after
        # upgrading. Never label an old lossy projection lossless retroactively.
        return None
    if not any(isinstance(row, dict) and row.get("role") == "assistant"
               and row.get("variant1_compaction") is True
               and str(row.get("content") or "").startswith(COMPACT_SUMMARY_MARKER)
               for row in messages):
        raise ValueError("checkpoint has no accepted compaction")
    if messages and messages[0].get("role") == "system":
        messages.pop(0)
    for row in messages:
        content = row.get("content")
        if isinstance(content, list):
            content = [part for part in content if not isinstance(part, dict)
                       or part.get("type") not in {"image", "image_url", "input_image"}]
            row["content"] = content
    return messages


def _covered_canonical_messages(sessions, session_id: str, covered: dict) -> list[dict]:
    capture = getattr(sessions, "canonical_context", None)
    if not callable(capture) or not isinstance(covered, dict):
        raise ValueError("canonical context authority is unavailable")
    canonical = capture(
        session_id,
        head_node_id=str(covered.get("head_node_id") or ""),
    )
    if canonical.get("cursor") != covered:
        raise ValueError("canonical context coverage changed during projection")
    messages = canonical.get("messages")
    if not isinstance(messages, list):
        raise ValueError("canonical context messages are unavailable")
    return messages


def repair_run_state_user_context(
    sessions,
    session_id: str,
    run_state: dict,
    *,
    covered: dict,
) -> dict:
    """Repair a disposable resume state from verified canonical user authority.

    The input snapshot remains immutable.  A legacy state is deliberately not
    upgraded to the new extent revision merely because current code read it.
    """

    repaired = copy.deepcopy(dict(run_state))
    canonical = _covered_canonical_messages(sessions, session_id, covered)
    repaired["messages"] = canonical_user_overlay(
        list(repaired.get("messages") or []),
        canonical,
        extent_revision=repaired.get("host_context_extents_revision"),
    )
    build_message_graph(repaired["messages"])
    return repaired


def canonical_conversation(sessions: Any, session_id: str) -> list[dict]:
    """Return every durable user/assistant text message in provider shape."""
    getter = getattr(sessions, "recent_convo", None)
    if not callable(getter):
        return []
    try:
        return list(getter(session_id, None) or [])
    except TypeError:
        return list(getter(session_id, 1000000) or [])


def _clear_invalid(sessions, session_id, state) -> None:
    clear = getattr(sessions, "clear_context_projection", None)
    if callable(clear) and state:
        try:
            clear(session_id, expected=state)
        except Exception:
            pass  # Optional cache cleanup cannot block canonical history.


def _apply_projection(sessions, session_id, state, snapshot=None, model_route=None):
    if state.get("kind") == "native_snapshot":
        projected = _snapshot_tail(snapshot, state, session_id, model_route)
    else:
        projected = state.get("messages")
        if isinstance(projected, list) and _legacy_compaction(projected):
            projected = None
    view_for = getattr(sessions, "context_projection_view", None)
    if not callable(view_for):
        raw = canonical_conversation(sessions, session_id)
        return raw, len(raw), None
    covered = state.get("coverage") if isinstance(projected, list) else None
    view = view_for(session_id, covered)
    if isinstance(projected, list) and view["valid"]:
        canonical = view.get("canonical_users")
        if not isinstance(canonical, list):
            raise ValueError("verified canonical user history is unavailable")
        projected = canonical_user_overlay(
            projected,
            canonical,
            extent_revision=(
                state.get("host_context_extents_revision")
                if state.get("kind") == "native_snapshot"
                else None
            ),
        )
        # The canonical graph parser proves call/result adjacency, IDs, JSON
        # arguments and a settled terminal boundary without executing any
        # action. Text caches use the same parser even though their accepted
        # form cannot contain native tool history.
        build_message_graph(projected)
        return copy.deepcopy(projected) + view["suffix"], view["count"], view["cursor"]
    if isinstance(covered, dict) and not view["valid"]:
        _clear_invalid(sessions, session_id, state)
    return view["messages"], view["count"], view["cursor"]


def _projection_error(sessions, sid, state, exc):
    from agent_engine.errors import DurableCheckpointUnavailable

    if isinstance(exc, (KeyError, ValueError, TypeError, DurableCheckpointUnavailable)):
        _clear_invalid(sessions, sid, state)
    _LOG.info("Ignoring unusable context projection: %s", type(exc).__name__)
    return _apply_projection(sessions, sid, {})


def current_projection(sessions: Any, session_id: str, *, snapshot_store=None,
                       model_route: dict | None = None) -> tuple[list[dict], int]:
    getter = getattr(sessions, "get_context_projection", None)
    state = getter(session_id) if callable(getter) else {}
    state = state if isinstance(state, dict) else {}
    snapshot = None
    try:
        if state.get("kind") == "native_snapshot":
            loader = getattr(snapshot_store, "load_cursor_sync", None)
            snapshot = loader(_cursor(state)) if callable(loader) else None
            if callable(loader) and snapshot is None:
                _clear_invalid(sessions, session_id, state)
        return _apply_projection(sessions, session_id, state, snapshot, model_route)[:2]
    except Exception as exc:
        return _projection_error(sessions, session_id, state, exc)[:2]


async def _call_compressor(
    compress_messages: Callable[..., Awaitable[list]] | None,
    messages: list[dict],
) -> list[dict]:
    if not callable(compress_messages):
        return messages
    try:
        result = compress_messages(messages, protect_first=0, protect_last=8)
    except TypeError:
        result = compress_messages(messages)
    if inspect.isawaitable(result):
        result = await result
    return list(result) if isinstance(result, list) else messages


async def project_session_conversation(
    sessions: Any,
    session_id: str,
    *,
    mode: str,
    context_limit_tokens: int,
    compress_messages: Callable[..., Awaitable[list]] | None = None,
    snapshot_store=None,
    model_route: dict | None = None,
) -> list[dict]:
    """Return and, when needed, persist a compact model projection.

    Canonical messages remain authoritative. Text summaries and native terminal
    checkpoint references carry verified prefix coverage; later messages append
    exactly once. A reference never resolves a mutable snapshot head.
    """
    getter = getattr(sessions, "get_context_projection", None)
    state = getter(session_id) if callable(getter) else {}
    state = state if isinstance(state, dict) else {}
    try:
        snapshot = (await snapshot_store.load_cursor(_cursor(state))
                    if snapshot_store is not None and state.get("kind") == "native_snapshot" else None)
        if snapshot_store is not None and state.get("kind") == "native_snapshot" and snapshot is None:
            _clear_invalid(sessions, session_id, state)
        projected, source_count, source_cursor = _apply_projection(sessions, session_id, state, snapshot, model_route)
    except Exception as exc:
        projected, source_count, source_cursor = _projection_error(sessions, session_id, state, exc)
    threshold = ctx_compress_threshold(mode=mode, ctx_size=context_limit_tokens)
    if approx_tokens(projected) <= threshold:
        return projected
    compacted = await _call_compressor(compress_messages, projected)
    if len(compacted) >= len(projected):
        # A missing/offline summarizer must not make a route switch destructive.
        # Keep the canonical projection and let the normal turn surface a model
        # capacity error instead of silently dropping context.
        return projected
    setter = getattr(sessions, "set_context_projection", None)
    # Native tool history is already owned by its checkpoint. A new accepted
    # graph is promoted after the normal terminal commit, never flattened here.
    has_native_history = any(row.get("role") == "tool" or row.get("tool_calls") for row in compacted)
    if callable(setter) and not has_native_history:
        stored = setter(
            session_id,
            compacted,
            source_message_count=source_count,
            context_limit_tokens=context_limit_tokens,
            source_cursor=source_cursor,
        )
        if stored is False:
            return canonical_conversation(sessions, session_id)
    return compacted


__all__ = [
    "canonical_conversation",
    "current_projection",
    "project_session_conversation",
]
