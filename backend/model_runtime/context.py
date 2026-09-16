"""Session model-route and context-window policy.

The configured router route is the default for *new* conversations. Existing
branches may pin a route/provider/model in their SQL session state; one turn
binds that immutable snapshot through ``LLMRouter`` so a Settings change cannot
split a request across providers.

Context limits are deliberately fail-closed.  User/provider overrides win,
then a small conservative family catalogue is used.  Unknown cloud models are
reported as unknown to the UI while projection uses a conservative internal
budget instead of pretending the model has a particular public limit.
"""

from __future__ import annotations

from typing import Any


UNKNOWN_CLOUD_BUDGET_TOKENS = 32768
_REASONING_EFFORT_ORDER = (
    "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)


def _uint(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _cloud_override(router: Any, provider: str, model: str) -> int:
    cfg = getattr(router, "cfg", None)
    cloud = cfg.get("cloud") if isinstance(cfg, dict) else None
    cloud = cloud if isinstance(cloud, dict) else {}
    provider_options = cloud.get("provider_options")
    provider_options = provider_options if isinstance(provider_options, dict) else {}
    options = provider_options.get(provider)
    options = options if isinstance(options, dict) else {}

    candidates = []
    for table in (options.get("context_windows"), cloud.get("context_windows")):
        if not isinstance(table, dict):
            continue
        candidates.extend((
            table.get(model),
            table.get(f"{provider}/{model}"),
            table.get(provider),
        ))
    candidates.extend((
        options.get("context_window_tokens"),
        options.get("context_window"),
    ))
    for value in candidates:
        parsed = _uint(value)
        if parsed:
            return parsed
    return 0


def _known_cloud_limit(provider: str, model: str) -> int:
    """Conservative input-window catalogue for bundled provider defaults.

    Matching is intentionally family based because dated provider aliases are
    common.  Install-specific values belong in ``cloud.context_windows`` and
    override this table.
    """
    name = str(model or "").strip().lower()
    if not name:
        return 0
    if str(provider or "").strip().lower() == "openai-codex":
        # The ChatGPT subscription catalog advertises a smaller effective
        # window than the public API model pages. The live Spark entry is
        # 128K; installation-specific discoveries can raise this via the
        # ordinary cloud.context_windows override.
        if name.startswith("gpt-5.6") or name == "gpt-5.5":
            return 272_000
        return 128_000
    if "gemini" in name:
        return 1_048_576
    if "claude" in name:
        return 200_000
    if "gpt-4.1" in name:
        return 1_047_576
    if "gpt-4o" in name or "gpt-4-turbo" in name:
        return 128_000
    if name.startswith("gpt-5"):
        return 400_000
    if "grok" in name:
        return 131_072
    if "deepseek" in name:
        return 65_536
    if any(part in name for part in ("qwen", "kimi", "minimax", "glm-")):
        return 131_072
    return 0


def _reasoning_route_fields(
    router: Any,
    provider: str,
    model: str,
    raw: dict,
) -> dict:
    profile_for = getattr(router, "provider_profile", None)
    profile = profile_for(provider) if callable(profile_for) else None
    declared = tuple(getattr(profile, "reasoning_efforts", ()) or ())
    efforts = tuple(
        value for value in _REASONING_EFFORT_ORDER if value in declared
    )
    if provider == "openai-codex" and not str(model or "").lower().startswith("gpt-5.6"):
        efforts = tuple(value for value in efforts if value != "max")
    if not efforts:
        return {}
    default = (
        "low" if provider == "xai"
        else "max" if provider == "openai-codex" and "max" in efforts
        else "xhigh" if provider == "openai-codex" and "xhigh" in efforts
        else "medium" if "medium" in efforts
        else efforts[0]
    )
    requested = str(raw.get("reasoning_effort") or "").strip().lower()
    return {
        "reasoning_effort": requested if requested in efforts else default,
        "reasoning_efforts": list(efforts),
    }


def normalize_model_route(
    router: Any,
    route: dict | None = None,
    *,
    mode: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict:
    raw = route if isinstance(route, dict) else {}
    selected_mode = str(mode or raw.get("mode") or getattr(router, "mode", "local"))
    selected_mode = selected_mode.strip().lower()
    if selected_mode not in {"local", "cloud"}:
        selected_mode = "local"
    if selected_mode == "local":
        # VARIANT-1 runs one physical local engine. A chat pins the local route,
        # while the loaded GGUF remains the process-wide Hardware selection.
        local_model = str(model or getattr(router, "model_name", "") or raw.get("model") or "")
        return {
            "mode": "local",
            "provider": "local",
            "model": local_model.strip(),
        }

    selected_provider = str(
        provider or raw.get("provider") or getattr(router, "cloud_provider", "")
    ).strip()
    canonical = getattr(router, "_kn", None)
    if callable(canonical):
        try:
            selected_provider = canonical(selected_provider)
        except Exception:
            pass
    selected_model = str(model or raw.get("model") or "").strip()
    if not selected_model:
        getter = getattr(router, "get_cloud_model", None)
        if callable(getter):
            try:
                selected_model = str(getter(selected_provider) or "").strip()
            except Exception:
                selected_model = ""
    selected = {
        "mode": "cloud",
        "provider": selected_provider,
        "model": selected_model,
    }
    selected.update(_reasoning_route_fields(
        router, selected_provider, selected_model, raw,
    ))
    return selected


def session_model_route(sessions: Any, session_id: str, router: Any) -> dict:
    stored = {}
    getter = getattr(sessions, "get_model_route", None)
    if callable(getter):
        try:
            stored = getter(session_id) or {}
        except Exception:
            stored = {}
    return normalize_model_route(router, stored)


def validate_worker_model_route(router: Any, route: dict) -> None:
    """A saved local worker may wait for its model, but must not use different weights."""
    if route.get("mode") != "local":
        return
    wanted = str(route.get("model") or "")
    loaded = str(getattr(router, "model_name", "") or "")
    if wanted and loaded and wanted != loaded:
        raise ValueError(f"Worker requires local model {wanted!r}; loaded model is {loaded!r}")


def model_route_support_coordinates(router: Any, route: dict | None = None) -> dict:
    """Translate a selected route into support-matrix coordinates."""

    selected = normalize_model_route(router, route)
    if selected["mode"] == "local":
        engine = getattr(router, "engine", None)
        adapter = str(
            getattr(engine, "request_adapter", "")
            or f"{getattr(router, 'inference_runtime_id', 'local')}.openai_chat_completions"
        )
        return {
            "provider": "local",
            "model": str(selected.get("model") or ""),
            "adapter": adapter,
        }
    provider = str(selected.get("provider") or "")
    profile_for = getattr(router, "provider_profile", None)
    profile = profile_for(provider) if callable(profile_for) else None
    adapter = f"{getattr(profile, 'api_style', 'unknown')}.*"
    if provider == "openai-codex":
        adapter = "openai_codex.responses"
    if provider == "xai":
        cloud = getattr(router, "cfg", {}).get("cloud", {}) or {}
        policy = str(
            cloud.get("xai_credential_policy") or "subscription_first"
        ).strip().lower()
        has_oauth = getattr(router, "has_oauth", None)
        oauth_configured = bool(
            callable(has_oauth)
            and policy != "api_key_only"
            and has_oauth("xai")
        )
        if oauth_configured:
            adapter = "xai.responses"
    return {
        "provider": provider,
        "model": str(selected.get("model") or ""),
        "adapter": adapter,
    }


def context_limit_tokens(router: Any, route: dict | None = None) -> int:
    selected = normalize_model_route(router, route)
    if selected["mode"] == "local":
        engine = getattr(router, "engine", None)
        limit = _uint(getattr(engine, "ctx_size", 0))
        if limit:
            return limit
        cfg = getattr(router, "cfg", None)
        local = cfg.get("local") if isinstance(cfg, dict) else None
        return _uint((local or {}).get("ctx_size")) or 8192
    provider = selected["provider"]
    model = selected["model"]
    return _cloud_override(router, provider, model) or _known_cloud_limit(provider, model)


def projection_budget_tokens(router: Any, route: dict | None = None) -> int:
    selected = normalize_model_route(router, route)
    limit = context_limit_tokens(router, selected)
    if limit:
        return limit
    return UNKNOWN_CLOUD_BUDGET_TOKENS if selected["mode"] == "cloud" else 8192


__all__ = [
    "UNKNOWN_CLOUD_BUDGET_TOKENS",
    "context_limit_tokens",
    "normalize_model_route",
    "model_route_support_coordinates",
    "projection_budget_tokens",
    "session_model_route",
]
