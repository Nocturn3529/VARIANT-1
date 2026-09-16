"""Bounded fallback when an active model rejects native image input.

VARIANT-1 is native-first: every provider family receives the same typed image
observation through the canonical message graph.  A genuinely text-only model
cannot inspect pixels, so after a pre-output image rejection we ask one already
configured vision route for a concise description and retry the main model once
with that text.  The accompanying browser/desktop accessibility observation is
left intact.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from core_invariants import cancellation_is_requested
from model_runtime.message_graph import coerce_image_observation


_IMAGE_REJECTION_TERMS = (
    "image input",
    "image_url",
    "input_image",
    "multimodal",
    "vision",
    "media type",
    "image content",
    "image is not supported",
    "images are not supported",
)

_FALLBACK_PROMPT = (
    "Describe the visible interface and any text or visual state needed to "
    "continue the task. Be factual and concise."
)


def looks_like_image_rejection(exc: BaseException) -> bool:
    """Return whether a pre-output provider failure concerns image input."""
    text = str(exc or "").casefold()
    status = int(getattr(exc, "status_code", 0) or 0)
    if status and status not in {400, 404, 413, 415, 422}:
        return False
    return (any(term in text for term in (*_IMAGE_REJECTION_TERMS, "image", "pixels"))
            and any(term in text for term in ("unsupported", "not support", "doesn't support",
                                             "not allowed", "invalid", "reject", "text-only", "text only",
                                             "no endpoints found")))


def _decode_observation(value: Any) -> bytes:
    observation = coerce_image_observation(value)
    if observation is None or not observation.data_b64:
        return b""
    try:
        raw = observation.data_b64
        padded = raw + ("=" * ((4 - len(raw) % 4) % 4))
        return base64.b64decode(padded, validate=True)
    except (ValueError, binascii.Error):
        return b""


def _cached_description(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("_variant1_visual_description") or "").strip()


def _cache_description(value: Any, description: str, route: str) -> None:
    if not isinstance(value, dict):
        return
    value["_variant1_visual_description"] = str(description or "").strip()
    value["_variant1_visual_description_route"] = str(route or "")


async def describe_for_text_retry(
    router,
    images: Any,
    *,
    rejected_route: str,
    should_stop=None,
) -> str:
    """Describe selected images through another configured vision route."""
    from desktop.vision import VisionUnavailable, available_vision_routes, describe

    selected = list(images) if isinstance(images, (list, tuple)) else [images]
    selected = [item for item in selected if item]
    if not selected:
        return ""
    routes = available_vision_routes(
        router,
        exclude=(str(rejected_route or ""),),
    )
    if not routes:
        return ""

    descriptions: list[str] = []
    for index, item in enumerate(selected, start=1):
        if cancellation_is_requested(should_stop):
            return ""
        cached = _cached_description(item)
        if cached:
            descriptions.append(cached)
            continue
        raw = _decode_observation(item)
        if not raw:
            continue
        description = ""
        used_route = ""
        for route in routes:
            try:
                description = str(await describe(
                    router,
                    raw,
                    prompt=_FALLBACK_PROMPT,
                    route=route,
                ) or "").strip()
            except VisionUnavailable:
                continue
            if description:
                used_route = route
                break
        if not description:
            continue
        _cache_description(item, description, used_route)
        label = f"Image {index}: " if len(selected) > 1 else ""
        descriptions.append(label + description)

    if not descriptions:
        return ""
    return "[Visual observation]\n" + "\n".join(descriptions)


def append_visual_description(messages: list, description: str) -> list:
    """Return a request-local history with one factual visual observation."""
    if not str(description or "").strip():
        return list(messages or [])
    return [
        *list(messages or []),
        {"role": "user", "content": str(description).strip()},
    ]


__all__ = [
    "append_visual_description",
    "describe_for_text_retry",
    "looks_like_image_rejection",
]
