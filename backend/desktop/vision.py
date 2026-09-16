"""Vision description for images captured by Desktop Fabric.

``describe()`` routes the image through the selected main-model path
(``llm_profiles.complete(profile="vision")``): local multimodal llama.cpp with
mmproj, or the selected cloud vision-capable model. This module deliberately
has no screen-capture path; Desktop Fabric is the sole capture authority.
"""

import base64

# Structured description format (spec 4.5).
VISION_PROMPT = (
    "You are the eyes of a desktop assistant. Describe what is on this screen "
    "concisely, in EXACTLY this format, one line each:\n"
    "Active app: [foreground app name]\n"
    "File/document open: [filename and type if visible, else none]\n"
    "Visible content: [one short sentence on what is on screen]\n"
    "Errors visible: [any error messages/warnings/highlighted text, else none]\n"
    "Other context: [browser tabs, taskbar, anything else relevant, else none]"
)


# --------------------------------------------------------------------------
# Describe (image -> text)
# --------------------------------------------------------------------------
class VisionUnavailable(RuntimeError):
    pass


def available_vision_routes(router, *, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return configured routes that can inspect pixels, active route first."""
    excluded = {str(item or "").strip() for item in exclude}
    cfg = router.cfg
    vcfg = cfg.get("vision", {}) or {}
    local_cfg = cfg.get("local", {}) or {}
    engine = getattr(router, "engine", None)
    local_capable = bool(
        getattr(engine, "mmproj", "")
        or local_cfg.get("mmproj")
        or vcfg.get("local_capable", False)
    )
    cloud_capable = bool(router.cloud_route_ready(require_vision=True))
    active = router.mode if router.mode in {"local", "cloud"} else "local"
    ordered = (active, "cloud" if active == "local" else "local")
    return tuple(
        route
        for route in ordered
        if route not in excluded
        and ((route == "local" and local_capable)
             or (route == "cloud" and cloud_capable))
    )


async def describe(router, image_bytes: bytes, prompt: str = VISION_PROMPT,
                   timeout_s: float = 60.0, route: str = "auto") -> str:
    """Return a text description through the selected main-model route.

    The ordinary router owns provider adapters, OAuth refresh, credential pools,
    fallback, URL normalization, and multimodal request shapes. Keeping this on
    that path prevents vision from becoming a subtly different cloud client.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    cfg = router.cfg
    vcfg = cfg.get("vision", {}) or {}
    requested_route = str(route or "auto").strip().lower()
    if requested_route in {"local", "cloud"}:
        routes = available_vision_routes(router)
        selected_route = requested_route if requested_route in routes else ""
    else:
        routes = available_vision_routes(router)
        selected_route = routes[0] if routes else ""
    if not selected_route:
        raise VisionUnavailable(
            "No configured model route can inspect image pixels."
        )
    try:
        from llm_profiles import complete
        return await complete(
            router,
            [{"role": "system", "content": "You convert images into concise factual screen context."},
             {"role": "user", "content": prompt}],
            profile="vision",
            max_tokens=400,
            image_b64=b64,
            route=selected_route,
        )
    except Exception as exc:
        raise VisionUnavailable(f"{selected_route} vision failed: {exc}") from exc
