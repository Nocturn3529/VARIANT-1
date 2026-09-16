"""Session-scoped context-window accounting derived from request receipts.

The context meter deliberately consumes the metadata-only model request
manifest. It never reads or stores prompt text. Provider-reported input tokens
are authoritative for the total when available; category shares remain an
estimate derived from structural byte counts and context-lineage sizes.
"""

from __future__ import annotations

import math
from typing import Any

from model_runtime.context import context_limit_tokens as _context_limit_tokens
from transcript_economy import approx_tokens


CATEGORY_ORDER = (
    ("messages", "Messages"),
    ("tools", "Tools"),
    ("skills", "Skills"),
    ("mcps", "MCPs"),
    ("plugins", "Plugins"),
    ("memory", "Memory"),
    ("other", "Other"),
)


def _uint(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _tokens_from_bytes(value: Any) -> int:
    return int(math.ceil(_uint(value) / 4.0))


def _tokens_from_chars(value: Any) -> int:
    # Lineage sometimes knows only Python character counts. Three characters
    # per token is a deliberately conservative attribution estimate.
    return int(math.ceil(_uint(value) / 3.0))


def context_limit_for_router(router: Any, route: dict | None = None) -> int:
    return _context_limit_tokens(router, route)


def _tool_categories(manifest: dict) -> dict[str, str]:
    tools = manifest.get("tools") if isinstance(manifest.get("tools"), dict) else {}
    rows = list(tools.get("requested") or []) + list(tools.get("rendered") or [])
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            out[name] = str(row.get("category") or "").strip().lower()
    return out


def _bucket_for_tool(name: str, categories: dict[str, str]) -> str:
    clean = str(name or "").strip()
    category = categories.get(clean, "")
    if category.startswith("mcp:") or clean.startswith("mcp_"):
        return "mcps"
    if clean == "skill" or category in {"skill", "skills"}:
        return "skills"
    return "tools"


def _split_tool_tokens(
    counts: dict[str, int],
    tokens: int,
    names: list,
    tool_categories: dict[str, str],
) -> None:
    tokens = _uint(tokens)
    if not tokens:
        return
    buckets = []
    for value in names or []:
        bucket = _bucket_for_tool(str(value or ""), tool_categories)
        if bucket not in buckets:
            buckets.append(bucket)
    if not buckets:
        buckets = ["tools"]
    base, remainder = divmod(tokens, len(buckets))
    for index, bucket in enumerate(buckets):
        counts[bucket] += base + (1 if index < remainder else 0)


def _move(counts: dict[str, int], source: str, target: str, amount: int) -> None:
    amount = min(_uint(amount), counts.get(source, 0))
    if amount <= 0 or source == target:
        return
    counts[source] -= amount
    counts[target] += amount


def _lineage_attribution(counts: dict[str, int], manifest: dict) -> None:
    lineage = manifest.get("context_lineage")
    rows = lineage.get("items") if isinstance(lineage, dict) else []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("decision") not in {
            "kept", "projected",
        }:
            continue
        producer = str(row.get("producer") or "").strip().lower()
        if producer not in {
            "prompt_profile", "prompt_memory", "skills_catalog", "app_catalog",
        }:
            continue
        token_count = _tokens_from_bytes(row.get("bytes_after"))
        if not token_count:
            token_count = _tokens_from_chars(row.get("chars_after"))
        if producer == "prompt_profile":
            _move(counts, "other", "memory", token_count)
        elif producer == "prompt_memory":
            _move(counts, "messages", "memory", token_count)
        elif producer == "skills_catalog":
            _move(counts, "other", "skills", token_count)
        elif producer == "app_catalog":
            _move(counts, "other", "plugins", token_count)


def _estimated_category_weights(manifest: dict) -> dict[str, int]:
    counts = {key: 0 for key, _ in CATEGORY_ORDER}
    categories = _tool_categories(manifest)
    messages = manifest.get("messages") if isinstance(manifest.get("messages"), dict) else {}
    rows = messages.get("ordered_rendered") if isinstance(messages, dict) else []
    accounted_message_tokens = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "other").lower()
        text_tokens = _tokens_from_bytes(row.get("text_utf8_bytes"))
        overhead = 4
        base_bucket = "other" if role in {"system", "developer", "other"} else "messages"
        counts[base_bucket] += text_tokens + overhead
        accounted_message_tokens += text_tokens + overhead
        tool_tokens = _tokens_from_bytes(row.get("tool_argument_utf8_bytes"))
        tool_tokens += _tokens_from_bytes(row.get("tool_result_utf8_bytes"))
        accounted_message_tokens += tool_tokens
        _split_tool_tokens(
            counts,
            tool_tokens,
            row.get("tool_names") if isinstance(row.get("tool_names"), list) else [],
            categories,
        )

    budget = manifest.get("budget") if isinstance(manifest.get("budget"), dict) else {}
    estimated_messages = _uint(budget.get("estimated_message_tokens"))
    if estimated_messages > accounted_message_tokens:
        # Detail arrays are bounded head/tail windows. Keep omitted transcript
        # accounting visible without pretending to know a finer category.
        counts["messages"] += estimated_messages - accounted_message_tokens

    tools = manifest.get("tools") if isinstance(manifest.get("tools"), dict) else {}
    accounted_schema_tokens = 0
    schema_metrics = {
        str(row.get("name") or ""): _uint(row.get("estimated_schema_tokens"))
        for row in (tools.get("rendered_schema_metrics") or [])
        if isinstance(row, dict) and row.get("name")
    }
    for row in tools.get("rendered") or []:
        if not isinstance(row, dict):
            continue
        tokens = schema_metrics.get(str(row.get("name") or ""), 0)
        accounted_schema_tokens += tokens
        bucket = _bucket_for_tool(str(row.get("name") or ""), categories)
        counts[bucket] += tokens
    schema_total = _uint(tools.get("estimated_schema_tokens"))
    if schema_total > accounted_schema_tokens:
        counts["tools"] += schema_total - accounted_schema_tokens

    _lineage_attribution(counts, manifest)
    return counts


def _reconcile(counts: dict[str, int], used_tokens: int) -> dict[str, int]:
    used = _uint(used_tokens)
    total = sum(max(0, int(value)) for value in counts.values())
    if used <= 0:
        return {key: 0 for key, _ in CATEGORY_ORDER}
    if total <= 0:
        return {
            key: used if key == "other" else 0
            for key, _ in CATEGORY_ORDER
        }
    exact = {
        key: max(0, counts.get(key, 0)) * used / total
        for key, _ in CATEGORY_ORDER
    }
    out = {key: int(math.floor(value)) for key, value in exact.items()}
    remainder = used - sum(out.values())
    ranked = sorted(
        exact,
        key=lambda key: (exact[key] - out[key], counts.get(key, 0)),
        reverse=True,
    )
    for key in ranked[:remainder]:
        out[key] += 1
    return out


def empty_session_context(
    session_id: str,
    *,
    context_limit_tokens: int = 0,
    route: dict | None = None,
) -> dict:
    limit = _uint(context_limit_tokens)
    selected = route if isinstance(route, dict) else {}
    return {
        "type": "chat:context",
        "schema": "variant1.session-context.v1",
        "session_id": str(session_id or "")[:160],
        "status": "empty",
        "captured_at": None,
        "route": str(selected.get("mode") or ""),
        "provider": str(selected.get("provider") or ""),
        "model": str(selected.get("model") or ""),
        "reasoning_effort": str(selected.get("reasoning_effort") or ""),
        "reasoning_efforts": list(selected.get("reasoning_efforts") or ()),
        "measurement": "pending",
        "category_measurement": "estimated",
        "used_tokens": 0,
        "context_limit_tokens": limit or None,
        "available_tokens": limit or None,
        "output_reserve_tokens": 0,
        "cached_input_tokens": 0,
        "percent_used": 0.0,
        "categories": [
            {"id": key, "label": label, "tokens": 0, "percent": 0.0}
            for key, label in CATEGORY_ORDER
        ],
    }


def session_context_from_manifest(manifest: dict) -> dict | None:
    if not isinstance(manifest, dict):
        return None
    run = manifest.get("run") if isinstance(manifest.get("run"), dict) else {}
    session_id = str(run.get("session_id") or "").strip()
    if not session_id:
        return None
    budget = manifest.get("budget") if isinstance(manifest.get("budget"), dict) else {}
    usage = manifest.get("usage") if isinstance(manifest.get("usage"), dict) else {}
    estimated = _uint(budget.get("estimated_input_tokens_lower_bound"))
    used = _uint(usage.get("input_tokens")) if usage else estimated
    measurement = str(usage.get("measurement") or "estimated") if usage else "estimated"
    limit = _uint(budget.get("context_limit_tokens"))
    weights = _reconcile(_estimated_category_weights(manifest), used)
    percent_used = round(min(100.0, used * 100.0 / limit), 1) if limit else 0.0
    return {
        "type": "chat:context",
        "schema": "variant1.session-context.v1",
        "session_id": session_id[:160],
        "status": "ready",
        "captured_at": manifest.get("captured_at"),
        "manifest_id": str(manifest.get("manifest_id") or "")[:160],
        "route": str(((manifest.get("route") or {}).get("selected_mode") or ""))[:20],
        "provider": str(((manifest.get("route") or {}).get("provider") or ""))[:80],
        "model": str(((manifest.get("route") or {}).get("model") or ""))[:300],
        "reasoning_effort": str(
            ((manifest.get("route") or {}).get("reasoning_effort") or "")
        )[:24],
        "reasoning_efforts": list(
            ((manifest.get("route") or {}).get("reasoning_efforts") or ())
        ),
        "measurement": measurement,
        "category_measurement": "estimated",
        "used_tokens": used,
        "context_limit_tokens": limit or None,
        "available_tokens": max(0, limit - used) if limit else None,
        "output_reserve_tokens": _uint(budget.get("output_reserve_tokens")),
        "cached_input_tokens": _uint(usage.get("cached_input_tokens")),
        "percent_used": percent_used,
        "categories": [
            {
                "id": key,
                "label": label,
                "tokens": weights[key],
                "percent": round(weights[key] * 100.0 / limit, 1) if limit else 0.0,
            }
            for key, label in CATEGORY_ORDER
        ],
    }


def estimated_session_context(
    session_id: str,
    messages: list,
    *,
    context_limit_tokens: int = 0,
    route: dict | None = None,
) -> dict:
    """Immediate post-switch meter before the target model reports usage."""
    limit = _uint(context_limit_tokens)
    used = _uint(approx_tokens(messages or []))
    selected = route if isinstance(route, dict) else {}
    percent = round(min(100.0, used * 100.0 / limit), 1) if limit else 0.0
    return {
        "type": "chat:context",
        "schema": "variant1.session-context.v1",
        "session_id": str(session_id or "")[:160],
        "status": "ready" if messages else "empty",
        "captured_at": None,
        "route": str(selected.get("mode") or ""),
        "provider": str(selected.get("provider") or ""),
        "model": str(selected.get("model") or ""),
        "reasoning_effort": str(selected.get("reasoning_effort") or ""),
        "reasoning_efforts": list(selected.get("reasoning_efforts") or ()),
        "measurement": "estimated_projection",
        "category_measurement": "estimated",
        "used_tokens": used,
        "context_limit_tokens": limit or None,
        "available_tokens": max(0, limit - used) if limit else None,
        "output_reserve_tokens": 0,
        "cached_input_tokens": 0,
        "percent_used": percent,
        "categories": [
            {
                "id": key,
                "label": label,
                "tokens": used if key == "messages" else 0,
                "percent": percent if key == "messages" else 0.0,
            }
            for key, label in CATEGORY_ORDER
        ],
    }


def latest_session_context(
    snapshot: dict,
    session_id: str,
    *,
    context_limit_tokens: int = 0,
    route: dict | None = None,
    projected_messages: list | None = None,
) -> dict:
    wanted = str(session_id or "").strip()
    expected = route if isinstance(route, dict) else {}
    items = snapshot.get("items") if isinstance(snapshot, dict) else []
    for manifest in reversed(items or []):
        if not isinstance(manifest, dict):
            continue
        run = manifest.get("run") if isinstance(manifest.get("run"), dict) else {}
        if str(run.get("session_id") or "").strip() != wanted:
            continue
        actual = manifest.get("route") if isinstance(manifest.get("route"), dict) else {}
        if expected:
            if str(actual.get("selected_mode") or "") != str(expected.get("mode") or ""):
                continue
            if str(expected.get("provider") or "") and str(actual.get("provider") or "") != str(expected.get("provider") or ""):
                continue
            if str(expected.get("model") or "") and str(actual.get("model") or "") != str(expected.get("model") or ""):
                continue
        context = session_context_from_manifest(manifest)
        if context is not None:
            context["reasoning_effort"] = str(
                expected.get("reasoning_effort") or ""
            )
            context["reasoning_efforts"] = list(
                expected.get("reasoning_efforts") or ()
            )
            limit = _uint(context_limit_tokens) or _uint(context.get("context_limit_tokens"))
            if limit:
                used = _uint(context.get("used_tokens"))
                context["context_limit_tokens"] = limit
                context["available_tokens"] = max(0, limit - used)
                context["percent_used"] = round(min(100.0, used * 100.0 / limit), 1)
                for row in context.get("categories") or []:
                    if isinstance(row, dict):
                        row["percent"] = round(_uint(row.get("tokens")) * 100.0 / limit, 1)
            return context
    if projected_messages is not None:
        return estimated_session_context(
            wanted,
            projected_messages,
            context_limit_tokens=context_limit_tokens,
            route=expected,
        )
    return empty_session_context(
        wanted,
        context_limit_tokens=context_limit_tokens,
        route=expected,
    )


__all__ = [
    "CATEGORY_ORDER",
    "context_limit_for_router",
    "empty_session_context",
    "estimated_session_context",
    "latest_session_context",
    "session_context_from_manifest",
]
