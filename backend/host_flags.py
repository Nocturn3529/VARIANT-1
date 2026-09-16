"""Small host policy helpers for agent mode, vision reporting, and workflows.

These pure helpers operate on a config dict + optional save callback so the
composition root can stay thin. Runtime callers receive the current config or
router through ``AppHost``.
"""

from __future__ import annotations

def agent_mode(cfg: dict | None = None) -> str:
    """VARIANT-1 has one interactive agent execution mode."""
    return "default"


def vision_cfg_from_router(router) -> dict:
    """Build the public vision config snapshot from a live LLM router."""
    route = router.mode if router.mode in ("local", "cloud") else "local"
    engine = getattr(router, "engine", None)
    # The managed server may drop an incompatible projector and restart
    # text-only. Report the live projector, not merely a stale configured path.
    local_capable = bool(getattr(engine, "mmproj", ""))
    return {
        "route": route,
        "local_capable": local_capable,
        "local_route": "single",
        "cloud_route": "single",
    }
