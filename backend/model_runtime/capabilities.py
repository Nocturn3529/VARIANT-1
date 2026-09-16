"""Deterministic capabilities of VARIANT-1's active model route.

This projection never sends inference. Route qualification belongs to the
ASTB support matrix and its saved evaluation evidence; this module reports
only properties already known from the selected provider profile or running
local engine.
"""

import os


class ModelCapabilities:
    def __init__(self, vision=False, tools=True, thinking=False, ctx_size=0,
                 source="local", model=""):
        self.vision = bool(vision)
        self.tools = bool(tools)
        self.thinking = bool(thinking)
        self.ctx_size = int(ctx_size or 0)
        self.source = source          # "local" | "cloud"
        self.model = model            # path or provider:model label

    def to_dict(self):
        return {
            "vision": self.vision, "tools": self.tools, "thinking": self.thinking,
            "ctx_size": self.ctx_size, "source": self.source,
            "model": self.model,
        }


def cloud_capabilities(
    provider="",
    model="",
    *,
    supports_vision=False,
    supports_reasoning=False,
    ctx_size=0,
) -> ModelCapabilities:
    """Describe a configured cloud route without probing it."""
    return ModelCapabilities(
        vision=bool(supports_vision),
        tools=True,
        thinking=bool(supports_reasoning),
        ctx_size=ctx_size,
        source="cloud",
        model=(model or provider or "cloud"),
    )


def resolve_local(engine) -> ModelCapabilities:
    """Describe a running local engine without sending inference."""
    mmproj = getattr(engine, "mmproj", "") or ""
    vision = bool(mmproj) and os.path.isfile(mmproj)
    thinking = bool(getattr(engine, "supports_reasoning", True))
    ctx = int(getattr(engine, "ctx_size", 0) or 0)
    model = getattr(engine, "model", "") or ""
    return ModelCapabilities(vision=vision, tools=True, thinking=thinking,
                             ctx_size=ctx, source="local", model=model)


def resolve(router) -> ModelCapabilities:
    """Resolve the active route entirely from configured/runtime facts."""
    if getattr(router, "mode", "local") == "cloud":
        prov_attr = getattr(router, "cloud_provider", "")
        prov = prov_attr() if callable(prov_attr) else prov_attr
        model_attr = getattr(router, "get_cloud_model", "")
        model = model_attr() if callable(model_attr) else model_attr
        profile_getter = getattr(router, "provider_profile", None)
        profile = profile_getter(prov) if callable(profile_getter) else None
        context_getter = getattr(router, "context_limit_tokens", None)
        if callable(context_getter):
            try:
                ctx_size = int(context_getter({
                    "mode": "cloud", "provider": prov, "model": model,
                }) or 0)
            except (TypeError, ValueError):
                ctx_size = 0
        else:
            ctx_size = 0
        return cloud_capabilities(
            prov,
            model,
            supports_vision=bool(getattr(profile, "supports_vision", False)),
            supports_reasoning=bool(
                getattr(profile, "supports_reasoning", False)
            ),
            ctx_size=ctx_size,
        )
    return resolve_local(getattr(router, "engine", None))
