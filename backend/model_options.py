"""Session-scoped model catalog for the Main Deck composer.

The picker is a projection of the canonical provider registry and local model
inventory.  It does not own provider configuration or model routing.  Live
provider discovery is cached briefly so opening the composer menu remains
cheap, while an explicit refresh can replace that cache.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from model_runtime.context import session_model_route


_CACHE_TTL_SECONDS = 300.0
_DISCOVERY_TIMEOUT_SECONDS = 18.0
_MAX_MODELS_PER_PROVIDER = 500
_LOOPBACK_PROVIDERS = {"hermes", "lmstudio", "ollama"}
_KEYLESS_REMOTE_PROVIDERS = {"opencode-free", "opencode-zen"}


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _unique_models(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, (list, tuple, set)) else ():
        if isinstance(value, dict):
            value = value.get("id") or value.get("name")
        model = _text(value, 300)
        key = model.casefold()
        if not model or key in seen:
            continue
        seen.add(key)
        out.append(model)
        if len(out) >= _MAX_MODELS_PER_PROVIDER:
            break
    return out


def _merge_models(*groups: Any) -> list[str]:
    return _unique_models([
        item
        for group in groups
        for item in (group if isinstance(group, (list, tuple, set)) else ())
    ])


def _format_bytes(value: Any) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return ""
    if not size:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    digits = 0 if unit in {"B", "KB"} else 1
    return f"{amount:.{digits}f} {unit}"


def _cache(host: Any) -> dict[str, dict[str, Any]]:
    value = getattr(host, "_composer_model_options_cache", None)
    if not isinstance(value, dict):
        value = {}
        host._composer_model_options_cache = value
    return value


def _configured_provider(host: Any, info: dict, current: dict) -> bool:
    name = _text(info.get("name"), 80).lower()
    if not name:
        return False
    if name == _text(current.get("provider"), 80).lower():
        return True
    if name.startswith("custom-") or name in _KEYLESS_REMOTE_PROVIDERS:
        return True
    if bool(info.get("api_key_configured")) or int(info.get("credential_count") or 0) > 0:
        return True
    try:
        if host.router.has_oauth(name):
            return True
    except Exception:
        pass
    # Loopback providers have no bearer to mark them configured. Probe their
    # already-running endpoint without starting another desktop application.
    return name in _LOOPBACK_PROVIDERS


def _seed_models(host: Any, info: dict, current: dict) -> list[str]:
    name = _text(info.get("name"), 80)
    seeds: list[str] = []
    if name == _text(current.get("provider"), 80):
        seeds.append(_text(current.get("model"), 300))
    seeds.extend((
        _text(info.get("model"), 300),
        _text(info.get("default_model"), 300),
    ))
    seeds.extend(info.get("fallback_models") or ())
    if name.startswith("custom-"):
        try:
            from model_providers.custom_endpoints import endpoint_records

            endpoint = next(
                (row for row in endpoint_records(host.router.cfg)
                 if row.get("id") == name),
                None,
            )
            if endpoint:
                seeds.extend(endpoint.get("models") or ())
        except Exception:
            pass
    return _unique_models(seeds)


def _reasoning_efforts(host: Any, provider: str, model: str) -> list[str]:
    """Project optional provider reasoning choices without requiring one router API."""
    resolver = getattr(host.router, "reasoning_efforts", None)
    if callable(resolver):
        try:
            return _unique_models(resolver(provider, model))
        except Exception:
            return []
    profile_getter = getattr(host.router, "provider_profile", None)
    if callable(profile_getter):
        try:
            profile = profile_getter(provider)
            return _unique_models(getattr(profile, "reasoning_efforts", ()))
        except Exception:
            return []
    return []


async def _provider_row(
    host: Any,
    info: dict,
    current: dict,
    *,
    refresh: bool,
    semaphore: asyncio.Semaphore,
) -> dict | None:
    name = _text(info.get("name"), 80)
    display = _text(info.get("display_name"), 120) or name
    seeds = _seed_models(host, info, current)
    cache = _cache(host)
    cached = cache.get(name) if isinstance(cache.get(name), dict) else None
    now = time.monotonic()
    cached_fresh = bool(
        cached
        and now - float(cached.get("captured_at") or 0.0) < _CACHE_TTL_SECONDS
    )
    discovered: list[str] = []
    discovery = "seed"
    warning = ""

    if cached_fresh and not refresh:
        discovered = _unique_models(cached.get("models") or ())
        discovery = "cache"
    else:
        try:
            async with semaphore:
                values = await asyncio.wait_for(
                    host.router.list_cloud_models(
                        name,
                        start_if_needed=False,
                    ),
                    timeout=_DISCOVERY_TIMEOUT_SECONDS,
                )
            discovered = _unique_models(values)
            if discovered:
                cache[name] = {
                    "captured_at": now,
                    "models": discovered,
                }
                discovery = "live"
        except Exception:
            if cached:
                discovered = _unique_models(cached.get("models") or ())
                discovery = "stale_cache"
                warning = "Live model refresh was unavailable; showing the last catalog."
            else:
                warning = "Live model discovery is unavailable."

    models = _merge_models(seeds, discovered)
    is_current = (
        _text(current.get("mode"), 20) == "cloud"
        and name == _text(current.get("provider"), 80)
    )
    explicit = (
        is_current
        or name.startswith("custom-")
        or name in _KEYLESS_REMOTE_PROVIDERS
        or bool(info.get("api_key_configured"))
        or int(info.get("credential_count") or 0) > 0
    )
    try:
        explicit = explicit or bool(host.router.has_oauth(name))
    except Exception:
        pass
    # An unavailable loopback service is not a connected provider. Keep it
    # only when it owns the current route; otherwise the picker would advertise
    # every optional desktop bridge on every installation.
    if name in _LOOPBACK_PROVIDERS and not discovered and not is_current:
        return None
    if not models or (not explicit and not discovered):
        return None

    return {
        "id": name,
        "name": display,
        "mode": "cloud",
        "description": _text(info.get("description"), 300),
        "models": [
            {
                "id": model,
                "label": model,
                "selectable": True,
                "reasoning_efforts": _reasoning_efforts(host, name, model),
            }
            for model in models
        ],
        "supports_reasoning": bool(info.get("supports_reasoning")),
        "supports_vision": bool(info.get("supports_vision")),
        "discovery": discovery,
        "warning": warning,
    }


def _local_provider(host: Any, current: dict) -> dict | None:
    items = host.require_runtime().models.scan_models()
    configured = _text(
        (host.router.cfg.get("local", {}) or {}).get("model"),
        2000,
    )
    rows = []
    seen: set[str] = set()
    scanned_paths: list[str] = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        path = _text(item.get("path"), 2000)
        key = os.path.normcase(os.path.normpath(path)) if path else ""
        if not path or key in seen:
            continue
        seen.add(key)
        scanned_paths.append(path)
        rows.append({
            "id": path,
            "label": _text(item.get("name"), 300) or os.path.basename(path),
            "detail": _format_bytes(item.get("size_bytes")),
            "vision": bool(item.get("vision")),
            "selectable": True,
            "reasoning_efforts": [],
        })
    if configured:
        configured_key = os.path.normcase(os.path.normpath(configured))
        suffix = configured_key.lstrip("\\/")
        matches = [path for path in scanned_paths if (
            os.path.normcase(os.path.normpath(path)) == configured_key
            or os.path.normcase(os.path.normpath(path)).endswith(
                os.sep + suffix.replace("/", os.sep).replace("\\", os.sep)
            )
        )]
        if len(matches) == 1:
            configured = matches[0]
        key = os.path.normcase(os.path.normpath(configured))
        if key not in seen:
            rows.append({
                "id": configured,
                "label": os.path.basename(configured) or configured,
                "detail": "Current local model",
                "vision": bool(getattr(host.router.engine, "mmproj", "")),
                "selectable": True,
                "reasoning_efforts": [],
            })
    if not rows:
        model = _text(getattr(host.router, "model_name", ""), 300)
        if model:
            rows.append({
                "id": model,
                "label": model,
                "detail": "Current local runtime",
                "vision": bool(getattr(host.router.engine, "mmproj", "")),
                "selectable": True,
                "reasoning_efforts": [],
            })
    if not rows:
        return None
    return {
        "id": "local",
        "name": "Local",
        "mode": "local",
        "description": "Models supplied in the VARIANT-1 local model folder.",
        "models": rows,
        "supports_reasoning": False,
        "supports_vision": any(bool(row.get("vision")) for row in rows),
        "discovery": "inventory",
        "warning": "",
    }


async def model_options(
    host: Any,
    session_id: str,
    *,
    request_id: str = "",
    refresh: bool = False,
) -> dict:
    """Return the selectable provider/model graph for one durable chat."""
    sid = _text(session_id, 160)
    sessions = host.require_runtime().sessions
    if not sid or not sessions.has_session(sid):
        raise ValueError("unknown chat session")
    current = session_model_route(sessions, sid, host.router)
    candidates = [
        info for info in host.router.list_provider_info()
        if _configured_provider(host, info, current)
    ]
    semaphore = asyncio.Semaphore(4)
    results = await asyncio.gather(*(
        _provider_row(
            host,
            info,
            current,
            refresh=refresh,
            semaphore=semaphore,
        )
        for info in candidates
    ))
    providers = [row for row in results if row]
    current_provider = _text(current.get("provider"), 80)
    providers.sort(key=lambda row: (
        0 if row.get("id") == current_provider else 1,
        str(row.get("name") or "").casefold(),
    ))
    local = _local_provider(host, current)
    if local:
        providers.insert(0, local)
    return {
        "type": "model:options",
        "schema": "variant1.model-options.v1",
        "request_id": _text(request_id, 160),
        "session_id": sid,
        "current": current,
        "providers": providers,
        "refreshed": bool(refresh),
    }


__all__ = ["model_options"]
