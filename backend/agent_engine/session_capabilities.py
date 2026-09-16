"""Checkpoint-safe session capability projections.

Runtime identity is intentionally stable across a chat's history.  Mutable
session authority therefore travels beside that identity and is copied into
RunState at admission so an interrupted tool boundary cannot resume with more
authority than it originally had.
"""

from __future__ import annotations

from typing import Any

from session_catalog.profiles import ACTION_SURFACE

ACTION_SURFACES = frozenset({ACTION_SURFACE})


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        nested = value.get("session_capabilities")
        if isinstance(nested, dict):
            return {**value, **nested}
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            raw = to_dict()
        except Exception:
            raw = None
        if isinstance(raw, dict):
            return dict(raw)
    return {}


def session_capabilities(
    value: Any = None,
    *,
    action_surface: str = "",
) -> dict[str, Any]:
    """Return the stable RunState projection for one runtime/capability value.

    Missing authority values fail closed. The former mutable profile is
    migrated in the runtime repository rather than inferred inside a run.
    """

    raw = _mapping(value)
    surface = str(
        action_surface
        or raw.get("action_surface")
        or getattr(getattr(value, "identity", None), "action_surface", "")
        or ""
    ).strip()
    if "mutation_write_enabled" in raw:
        enabled = bool(raw.get("mutation_write_enabled"))
    elif hasattr(value, "mutation_write_enabled"):
        enabled = bool(getattr(value, "mutation_write_enabled"))
    else:
        enabled = False
    revision_value = raw.get("mutation_authority_revision")
    if revision_value is None and hasattr(value, "mutation_authority_revision"):
        revision_value = getattr(value, "mutation_authority_revision")
    try:
        revision = max(0, int(revision_value or 0))
    except (TypeError, ValueError, OverflowError):
        revision = 0
    return {
        "mutation_write_enabled": enabled,
        "mutation_authority_revision": revision,
    }


def session_capabilities_from_run_state(state: Any) -> dict[str, Any]:
    raw = dict(state or {}) if isinstance(state, dict) else {}
    return session_capabilities(
        raw.get("session_capabilities"),
        action_surface=str(raw.get("action_surface") or ""),
    )


def effective_action_surface(
    action_surface: str,
    capabilities: Any = None,
) -> str:
    """Return the support-matrix profile for a Python-cell admission."""

    surface = str(action_surface or "").strip()
    del capabilities
    return ACTION_SURFACE if surface == ACTION_SURFACE else surface


def mutation_write_elevation(saved: Any, current: Any) -> bool:
    """Whether the current session allows writes denied by the saved snapshot."""

    old = session_capabilities_from_run_state(saved)
    new = session_capabilities(current)
    return bool(
        not old["mutation_write_enabled"]
        and new["mutation_write_enabled"]
    )


__all__ = [
    "ACTION_SURFACES",
    "effective_action_surface",
    "mutation_write_elevation",
    "session_capabilities",
    "session_capabilities_from_run_state",
]
