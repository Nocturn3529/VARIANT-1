"""Versioned compatibility contracts for durable VARIANT-1 run state.

Checkpoints can outlive the node topology that produced them. Exact current
surface and graph revisions are part of the resume contract; retired labels
are rejected rather than routed through a compatibility execution path.
"""

from __future__ import annotations

from typing import Any

from session_catalog.profiles import (
    ACTION_SURFACE,
    CHAT_GRAPH_REVISION,
    WORKER_GRAPH_REVISION,
    canonical_action_surface,
    canonical_graph_revision,
    is_action_surface,
)


RUN_STATE_SCHEMA_VERSION = 1

_CHAT_GRAPH_REVISIONS = frozenset({
    CHAT_GRAPH_REVISION,
})
_WORKER_GRAPH_REVISIONS = frozenset({
    WORKER_GRAPH_REVISION,
})


def graph_revision_for_source(source: str) -> str:
    return CHAT_GRAPH_REVISION if str(source or "") == "chat" else WORKER_GRAPH_REVISION


def supported_graph_revisions(source: str) -> frozenset[str]:
    return (
        _CHAT_GRAPH_REVISIONS
        if str(source or "") == "chat"
        else _WORKER_GRAPH_REVISIONS
    )


def infer_checkpoint_revision(state: dict[str, Any]) -> str:
    """Return an explicit revision; unversioned state is unsupported."""
    return str(state.get("graph_revision") or "").strip()


def upgrade_checkpoint_contract(raw: dict[str, Any]) -> dict[str, Any]:
    """Return an in-memory migration of a checkpoint's contract metadata."""
    state = dict(raw or {})
    saved_surface = str(state.get("action_surface") or "").strip()
    if saved_surface:
        state["action_surface"] = canonical_action_surface(saved_surface)
    saved_graph = str(state.get("graph_revision") or "").strip()
    if saved_graph:
        state["graph_revision"] = canonical_graph_revision(saved_graph)
    inferred = infer_checkpoint_revision(state)
    if not state.get("graph_revision") and inferred:
        state["graph_revision"] = inferred
        state["migrated_from_graph_revision"] = "unversioned"
    raw_schema = state.get("state_schema_version")
    if raw_schema in (None, ""):
        state["state_schema_version"] = RUN_STATE_SCHEMA_VERSION
    else:
        try:
            state["state_schema_version"] = int(raw_schema)
        except (TypeError, ValueError):
            state["state_schema_version"] = None
    return state


def validate_checkpoint_contract(
    raw: dict[str, Any],
    *,
    expected_source: str | None = None,
    expected_revision: str = "",
    accept_supported_revision: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Validate state/schema/topology before it enters a live graph run."""
    state = upgrade_checkpoint_contract(raw)
    source = str(expected_source or state.get("source") or "chat")
    saved = str(state.get("graph_revision") or "")
    expected = str(expected_revision or "").strip()
    if not expected:
        if accept_supported_revision and saved in supported_graph_revisions(source):
            expected = canonical_graph_revision(saved)
        else:
            expected = graph_revision_for_source(source)
    else:
        expected = canonical_graph_revision(expected)
    if not saved:
        return None, "checkpoint has no recognizable graph revision"
    surface = str(state.get("action_surface") or "").strip()
    if not is_action_surface(surface):
        return None, f"checkpoint action surface is unsupported ({surface!r})"
    if accept_supported_revision and saved not in supported_graph_revisions(source):
        return None, (
            f"graph revision is unsupported for {source!r} ({saved!r})"
        )
    if saved != expected:
        return None, f"graph revision changed ({saved!r} -> {expected!r})"
    try:
        schema = int(state.get("state_schema_version"))
    except (TypeError, ValueError):
        return None, "checkpoint has an invalid state schema version"
    if schema < 1:
        return None, f"checkpoint has an invalid state schema version ({schema})"
    if schema > RUN_STATE_SCHEMA_VERSION:
        return None, (
            "checkpoint state schema is newer than this runtime "
            f"({schema} > {RUN_STATE_SCHEMA_VERSION})"
        )
    state["state_schema_version"] = RUN_STATE_SCHEMA_VERSION
    return state, ""
