"""Canonical runtime-identity construction for chat and worker surfaces."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import platform
import sys
from typing import Any

from session_catalog.profiles import (
    ACTION_SURFACE,
    CHAT_GRAPH_REVISION,
    canonical_action_surface,
    is_action_surface,
)
from session_runtime import RuntimeIdentity


def environment_digest(host: Any) -> str:
    payload = (
        f"{platform.system()}\0{platform.machine()}\0"
        f"{sys.version_info[:3]}\0{host.version}"
    ).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def identity_for_profile(
    host: Any,
    catalog: Any,
    profile: str,
    *,
    graph_revision: str = "",
    provider_tool_schema_revision: str = "",
) -> RuntimeIdentity:
    """Build one pinned identity without duplicating chat/worker policy.

    The caller owns assignment policy. This function only constructs the exact
    immutable identity for the requested profile and optional graph revision.
    """
    requested = canonical_action_surface(profile or ACTION_SURFACE)
    if not is_action_surface(requested):
        raise ValueError(
            f"unsupported action surface: {requested!r}; "
            f"VARIANT-1 requires {ACTION_SURFACE!r}"
        )
    if catalog is None:
        raise RuntimeError("session catalog is not ready")
    identity = catalog.identity(environment_digest=environment_digest(host))
    return replace(
        identity,
        graph_revision=str(graph_revision or CHAT_GRAPH_REVISION),
        provider_tool_schema_revision=(
            str(provider_tool_schema_revision or "").strip()
            or identity.provider_tool_schema_revision
        ),
    )
