"""Durable user-defined OpenAI-compatible provider profiles.

Custom endpoints are provider records, not a second inference route.  Their
non-secret metadata lives in ``cloud.custom_endpoints``; optional bearer keys
remain in the ordinary encrypted credential pool for the generated provider
id.  Registering the records into ``ProviderRegistry`` lets model discovery,
session routing, failover, usage, and request manifests use the same path as a
built-in provider.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from .base import ProviderProfile


CUSTOM_PREFIX = "custom-"
MAX_CUSTOM_ENDPOINTS = 64


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def endpoint_id(value: Any, name: Any = "") -> str:
    raw = _text(value or name, 120).lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if raw.startswith(CUSTOM_PREFIX):
        raw = raw[len(CUSTOM_PREFIX):]
    if not raw:
        raw = "endpoint"
    return f"{CUSTOM_PREFIX}{raw[:72].rstrip('-')}"


def endpoint_url(value: Any) -> str:
    raw = _text(value, 2000).rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("endpoint URL must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("endpoint URL must not contain embedded credentials")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def normalize_endpoint(
    value: Any,
    *,
    require_model: bool = True,
) -> dict:
    raw = dict(value) if isinstance(value, dict) else {}
    name = _text(raw.get("name"), 120)
    if not name:
        raise ValueError("custom endpoint name is required")
    base_url = endpoint_url(raw.get("base_url"))
    model = _text(raw.get("model"), 300)
    if require_model and not model:
        raise ValueError("custom endpoint model is required")
    try:
        context_length = max(0, int(raw.get("context_length") or 0))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("context length must be a positive integer") from None
    if context_length and context_length < 1024:
        raise ValueError("context length must be at least 1024 tokens")
    models = []
    raw_models = raw.get("models")
    for item in raw_models if isinstance(raw_models, (list, tuple)) else ():
        text = _text(item, 300)
        if text and text not in models:
            models.append(text)
        if len(models) >= 500:
            break
    return {
        "id": endpoint_id(raw.get("id"), name),
        "name": name,
        "base_url": base_url,
        "model": model,
        "context_length": context_length,
        "discover_models": raw.get("discover_models", True) is not False,
        "models": models,
    }


def endpoint_records(cfg: dict) -> list[dict]:
    cloud = cfg.get("cloud") if isinstance(cfg, dict) else None
    raw = cloud.get("custom_endpoints") if isinstance(cloud, dict) else None
    rows = []
    seen = set()
    for item in raw if isinstance(raw, list) else ():
        try:
            row = normalize_endpoint(item)
        except (TypeError, ValueError):
            continue
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
        if len(rows) >= MAX_CUSTOM_ENDPOINTS:
            break
    return rows


def profile_for_endpoint(row: dict) -> ProviderProfile:
    base = row["base_url"].rstrip("/")
    return ProviderProfile(
        name=row["id"],
        display_name=row["name"],
        api_style="openai",
        base_url=base,
        models_url=f"{base}/models",
        auth_style="optional",
        default_model=row["model"],
        supports_vision=True,
        supports_reasoning=True,
        description="User-managed OpenAI-compatible endpoint.",
        completion_token_field="auto",
        sampling_fields=("temperature", "top_p"),
    )


def register_endpoints(registry, rows: Iterable[dict]) -> None:
    for row in rows:
        registry.register(
            profile_for_endpoint(row),
            origin={"kind": "custom_endpoint", "name": row["name"]},
        )


__all__ = [
    "CUSTOM_PREFIX",
    "MAX_CUSTOM_ENDPOINTS",
    "endpoint_id",
    "endpoint_records",
    "endpoint_url",
    "normalize_endpoint",
    "profile_for_endpoint",
    "register_endpoints",
]
