"""Canonical grant-scope encoding shared by artifact runtime projections."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from work_fabric.scope import WorkScope, coerce_work_scope


def cas_scope_id(
    scope: str | WorkScope | Mapping[str, Any] | None,
    artifact_id: str = "",
) -> str:
    if isinstance(scope, str):
        return scope.strip()
    resolved = coerce_work_scope(scope)
    if resolved.chat_id:
        return resolved.chat_id
    if resolved.workspace_id:
        return f"workspace:{resolved.workspace_id}"
    if resolved.goal_id:
        return f"goal:{resolved.goal_id}"
    return f"artifact:{str(artifact_id or 'unscoped')}"


__all__ = ["cas_scope_id"]
