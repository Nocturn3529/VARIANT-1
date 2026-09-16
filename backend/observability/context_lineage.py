"""Metadata-only observability lineage for context presented to a model.

The sidecar in this module is deliberately orthogonal to prompt construction.
It records *why* context was selected and how it was projected without storing
the selected text, paths, URLs, tool arguments/results, image data, or content
hashes.  Provider renderers ignore the reserved message key; the post-adapter
request manifest copies only the allowlisted receipt.
"""

from __future__ import annotations

import re
from typing import Any


SCHEMA = "variant1.context-lineage.v1"
RESERVED_MESSAGE_KEY = "_variant1_context_receipt"
MAX_ITEMS = 64
MAX_SELECTIONS = 32
MAX_TRANSFORMS = 64
MAX_SCAN = 512

_PURPOSES = {
    "main_chat_step", "headless_worker", "context_compression",
    "internal_completion", "resume", "direct", "unknown",
}
_ITEM_KINDS = {
    "system_prompt", "current_user", "conversation_history",
    "retrieved_memory", "profile", "image_context", "image_observation",
    "attachment", "tool_schema", "tool_observation",
    "task_state", "checkpoint_history", "dynamic_catalog", "other",
}
_SOURCES = {
    "application", "user", "conversation_store", "builtin_memory",
    "profile_store", "tool_result", "memory_store",
    "checkpoint", "tool_registry", "project", "attachment", "inferred",
    "unknown",
}
_TRUST = {
    "trusted_application", "user_supplied", "retrieved_personal",
    "untrusted_observation", "tool_output", "checkpointed",
    "local_config", "unknown",
}
_DECISIONS = {"kept", "dropped", "projected", "superseded", "omitted"}
_REASONS = {
    "required", "current_turn", "recent_tail", "top_k", "tool_selection",
    "progressive_disclosure", "user_attachment", "session_projection",
    "tool_execution", "newer_observation",
    "token_threshold", "resume_restore", "provider_projection",
    "size_limit", "first_image", "not_selected", "invalid", "unknown",
}
_RELEVANCE = {"required", "high", "medium", "low", "selected", "unknown"}
_TRANSFORM_KINDS = {
    "history_selection", "memory_selection", "tool_schema_selection",
    "attachment_projection", "tool_output_clipped", "research_excerpted",
    "image_superseded", "summary_input_projected",
    "context_compression", "checkpoint_restored", "budget_evaluated",
    "provider_projection", "other",
}
_ESTIMATORS = {
    "none", "chars_div_3", "utf8_bytes_div_4", "provider_tokenizer",
    "provider_reported", "unknown",
}
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_NUMERIC_FIELDS = {
    "rank", "considered", "kept", "dropped", "chars_before", "chars_after",
    "bytes_before", "bytes_after", "input_count", "output_count",
    "affected_count", "limit", "position_start", "position_end",
    "token_estimate_before", "token_estimate_after", "image_count",
}
_BOOL_FIELDS = {"model_generated", "truncated", "exact"}


def _enum(value: Any, allowed: set[str], default: str) -> str:
    value = str(value or "").strip().lower()
    return value if value in allowed else default


def _uint(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), 2_147_483_647))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_name(value: Any) -> str:
    value = str(value or "").strip()[:160]
    return value if _SAFE_NAME_RE.fullmatch(value) else ""


def new_receipt(purpose: str = "unknown") -> dict:
    return {
        "schema": SCHEMA,
        "purpose": _enum(purpose, _PURPOSES, "unknown"),
        "selection": {"considered": 0, "kept": 0, "dropped": 0},
        "selections": [],
        "items": [],
        "transforms": [],
        "aggregates": {
            "selection_total": 0,
            "item_total": 0,
            "transform_total": 0,
            "items_by_kind": {},
            "transforms_by_kind": {},
        },
        "selections_truncated_count": 0,
        "items_truncated_count": 0,
        "transforms_truncated_count": 0,
    }


def _counter(receipt: dict, group: str, kind: str) -> int:
    aggregates = receipt.setdefault("aggregates", {})
    total_key = f"{group}_total"
    total = _uint(aggregates.get(total_key)) + 1
    aggregates[total_key] = total
    by_key = "items_by_kind" if group == "item" else (
        "transforms_by_kind" if group == "transform" else "")
    if by_key:
        by = aggregates.setdefault(by_key, {})
        by[kind] = _uint(by.get(kind)) + 1
    return total


def _common_fields(values: dict) -> dict:
    out = {}
    for key in _NUMERIC_FIELDS:
        if key in values and values.get(key) is not None:
            out[key] = _uint(values.get(key))
    for key in _BOOL_FIELDS:
        if key in values and isinstance(values.get(key), bool):
            out[key] = bool(values[key])
    producer = _safe_name(values.get("producer"))
    if producer:
        out["producer"] = producer
    estimator = _enum(values.get("estimator"), _ESTIMATORS, "")
    if estimator:
        out["estimator"] = estimator
    return out


def add_selection(
    receipt: dict,
    *,
    kind: str,
    source: str,
    trust: str,
    reason: str,
    considered: int,
    kept: int,
    dropped: int | None = None,
    relevance: str = "unknown",
    **metrics: Any,
) -> dict:
    if not isinstance(receipt, dict):
        return {}
    considered_n = _uint(considered)
    kept_n = min(considered_n, _uint(kept))
    dropped_n = (
        max(0, considered_n - kept_n)
        if dropped is None else min(considered_n, _uint(dropped))
    )
    row = {
        "id": f"cs_{_counter(receipt, 'selection', '')}",
        "kind": _enum(kind, _ITEM_KINDS, "other"),
        "source": _enum(source, _SOURCES, "unknown"),
        "trust": _enum(trust, _TRUST, "unknown"),
        "reason": _enum(reason, _REASONS, "unknown"),
        "relevance": _enum(relevance, _RELEVANCE, "unknown"),
        "considered": considered_n,
        "kept": kept_n,
        "dropped": dropped_n,
        **_common_fields(metrics),
    }
    summary = receipt.setdefault("selection", {})
    for key in ("considered", "kept", "dropped"):
        summary[key] = _uint(summary.get(key)) + row[key]
    rows = receipt.setdefault("selections", [])
    if len(rows) < MAX_SELECTIONS:
        rows.append(row)
    else:
        receipt["selections_truncated_count"] = (
            _uint(receipt.get("selections_truncated_count")) + 1
        )
    return row


def add_item(
    receipt: dict,
    *,
    kind: str,
    source: str,
    trust: str,
    decision: str,
    reason: str,
    relevance: str = "unknown",
    **metrics: Any,
) -> dict:
    if not isinstance(receipt, dict):
        return {}
    safe_kind = _enum(kind, _ITEM_KINDS, "other")
    row = {
        "id": f"ci_{_counter(receipt, 'item', safe_kind)}",
        "kind": safe_kind,
        "source": _enum(source, _SOURCES, "unknown"),
        "trust": _enum(trust, _TRUST, "unknown"),
        "decision": _enum(decision, _DECISIONS, "dropped"),
        "reason": _enum(reason, _REASONS, "unknown"),
        "relevance": _enum(relevance, _RELEVANCE, "unknown"),
        **_common_fields(metrics),
    }
    rows = receipt.setdefault("items", [])
    if len(rows) < MAX_ITEMS:
        rows.append(row)
    else:
        receipt["items_truncated_count"] = (
            _uint(receipt.get("items_truncated_count")) + 1
        )
    return row


def add_transform(
    receipt: dict,
    *,
    kind: str,
    reason: str,
    **metrics: Any,
) -> dict:
    if not isinstance(receipt, dict):
        return {}
    safe_kind = _enum(kind, _TRANSFORM_KINDS, "other")
    row = {
        "id": f"ct_{_counter(receipt, 'transform', safe_kind)}",
        "kind": safe_kind,
        "reason": _enum(reason, _REASONS, "unknown"),
        **_common_fields(metrics),
    }
    rows = receipt.setdefault("transforms", [])
    if len(rows) < MAX_TRANSFORMS:
        rows.append(row)
    else:
        receipt["transforms_truncated_count"] = (
            _uint(receipt.get("transforms_truncated_count")) + 1
        )
    return row


def sanitize_receipt(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    clean = new_receipt(_enum(value.get("purpose"), _PURPOSES, "unknown"))
    for row in list(value.get("selections") or [])[:MAX_SCAN]:
        if not isinstance(row, dict):
            continue
        add_selection(
            clean,
            kind=row.get("kind"),
            source=row.get("source"),
            trust=row.get("trust"),
            reason=row.get("reason"),
            relevance=row.get("relevance"),
            considered=row.get("considered"),
            kept=row.get("kept"),
            dropped=row.get("dropped"),
            **{k: row.get(k) for k in _NUMERIC_FIELDS | _BOOL_FIELDS
               if k not in {"considered", "kept", "dropped"} and k in row},
        )
    for row in list(value.get("items") or [])[:MAX_SCAN]:
        if not isinstance(row, dict):
            continue
        add_item(
            clean,
            kind=row.get("kind"),
            source=row.get("source"),
            trust=row.get("trust"),
            decision=row.get("decision"),
            reason=row.get("reason"),
            relevance=row.get("relevance"),
            **{k: row.get(k) for k in _NUMERIC_FIELDS | _BOOL_FIELDS | {"producer", "estimator"}
               if k in row},
        )
    for row in list(value.get("transforms") or [])[:MAX_SCAN]:
        if not isinstance(row, dict):
            continue
        add_transform(
            clean,
            kind=row.get("kind"),
            reason=row.get("reason"),
            **{k: row.get(k) for k in _NUMERIC_FIELDS | _BOOL_FIELDS | {"producer", "estimator"}
               if k in row},
        )
    # Preserve totals beyond retained arrays without trusting arbitrary
    # by-kind keys supplied by the caller.
    source_agg = value.get("aggregates") if isinstance(value.get("aggregates"), dict) else {}
    for group, cap in (
        ("selection", MAX_SELECTIONS),
        ("item", MAX_ITEMS),
        ("transform", MAX_TRANSFORMS),
    ):
        key = f"{group}_total"
        observed = _uint(clean["aggregates"].get(key))
        claimed = _uint(source_agg.get(key))
        clean["aggregates"][key] = max(observed, claimed)
        trunc_key = f"{group}s_truncated_count"
        clean[trunc_key] = max(
            _uint(clean.get(trunc_key)),
            _uint(value.get(trunc_key)),
            max(0, clean["aggregates"][key] - cap),
        )
    return clean


def attach_to_messages(messages: list, receipt: dict | None) -> dict | None:
    """Attach one sanitized live sidecar to the first system/first message."""
    if not isinstance(messages, list) or not messages or not isinstance(receipt, dict):
        return None
    clean = sanitize_receipt(receipt)
    if clean is None:
        return None
    receipt.clear()
    receipt.update(clean)
    target = next(
        (message for message in messages
         if isinstance(message, dict) and message.get("role") == "system"),
        None,
    )
    if target is None:
        target = next((m for m in messages if isinstance(m, dict)), None)
    if target is None:
        return None
    target[RESERVED_MESSAGE_KEY] = receipt
    return receipt


def receipt_from_messages(
    messages: list,
    *,
    sanitized: bool = True,
) -> dict | None:
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        value = message.get(RESERVED_MESSAGE_KEY)
        if isinstance(value, dict):
            return sanitize_receipt(value) if sanitized else value
    return None


def add_transform_to_messages(messages: list, **fields: Any) -> dict:
    receipt = receipt_from_messages(messages, sanitized=False)
    return add_transform(receipt, **fields) if receipt is not None else {}


def message_metrics(messages: list) -> dict:
    chars = 0
    byte_count = 0
    role_counts: dict[str, int] = {}

    def add_text(value: Any) -> None:
        nonlocal chars, byte_count
        if not isinstance(value, str):
            return
        chars += len(value)
        byte_count += len(value.encode("utf-8", errors="replace"))

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "other").lower()
        role_counts[role] = role_counts.get(role, 0) + 1
        content = message.get("content")
        if isinstance(content, str):
            add_text(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                add_text(part.get("text"))
                add_text(part.get("content"))
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            add_text(fn.get("name"))
            add_text(fn.get("arguments"))
    return {
        "message_count": len([m for m in (messages or []) if isinstance(m, dict)]),
        "chars": chars,
        "utf8_bytes": byte_count,
        "role_counts": role_counts,
    }


def current_run_receipt() -> dict | None:
    try:
        from run_context import current_run_context

        ctx = current_run_context()
    except Exception:
        ctx = None
    receipt = getattr(ctx, "model_input_receipt", None) if ctx is not None else None
    return receipt if isinstance(receipt, dict) else None


def add_current_run_item(**fields: Any) -> dict:
    receipt = current_run_receipt()
    return add_item(receipt, **fields) if receipt is not None else {}


def add_current_run_transform(**fields: Any) -> dict:
    receipt = current_run_receipt()
    return add_transform(receipt, **fields) if receipt is not None else {}


__all__ = [
    "SCHEMA", "RESERVED_MESSAGE_KEY", "MAX_ITEMS", "MAX_SELECTIONS",
    "MAX_TRANSFORMS", "new_receipt", "add_selection", "add_item",
    "add_transform", "sanitize_receipt", "attach_to_messages",
    "receipt_from_messages", "add_transform_to_messages", "message_metrics",
    "current_run_receipt", "add_current_run_item",
    "add_current_run_transform",
]
