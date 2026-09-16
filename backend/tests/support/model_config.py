"""Explicit test-only model-route qualification helpers."""

from __future__ import annotations

import copy
from typing import Any

from session_catalog.profiles import ACTION_SURFACE


TEST_SUPPORT_RULE = {
    "profile": ACTION_SURFACE,
    "provider": "*",
    "model": "*",
    "adapter": "*",
    "status": "developer",
    "evidence": "explicit test-only route qualification",
}


def with_test_support(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a copied router config with an explicit test-only route rule."""

    result = copy.deepcopy(dict(config or {}))
    block = dict(result.get("action_surface") or {})
    block.setdefault("support_matrix", [dict(TEST_SUPPORT_RULE)])
    result["action_surface"] = block
    return result


__all__ = ["TEST_SUPPORT_RULE", "with_test_support"]
