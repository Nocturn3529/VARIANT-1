"""Compact, model-safe observability receipts for the preceding chat run."""

from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any, Iterable


RECEIPT_VERSION = 2
MAX_TOOL_SEQUENCE = 64
MAX_TOOL_RESULT_OBSERVATIONS = 256
MAX_TOOL_OUTCOME_KEYS = 32
MAX_TOOL_OUTCOME_VALUE_CHARS = 80
_SUCCESS_TOOL_RESULT_STATUSES = frozenset({"ok", "success", "succeeded", "completed"})
_CALL_CATEGORIES = ("agent", "memory", "compaction")
_METRIC_RE = re.compile(
    r"\b(?:tool|llm|model|api)\s*calls?\b|"
    r"\b(?:tokens?|cost|latency|inference|wall\s*time|trace|receipt)\b",
    re.IGNORECASE,
)
_PRIOR_RE = re.compile(
    r"\b(?:previous|prior|last|earlier|above|same)\b|"
    r"\b(?:that|those)\s+(?:run|task|turn|calls?)\b",
    re.IGNORECASE,
)
_PAST_AUDIT_RE = re.compile(
    r"\b(?:why|how|what)\b.{0,80}\b(?:did|was|were|used|made|took|resulted)\b|"
    r"\b(?:confusing|confused)\b",
    re.IGNORECASE,
)


def _outcome_value(value: Any, *, default: str = "") -> str:
    text = " ".join(str(value or "").split()).strip().lower()
    return text[:MAX_TOOL_OUTCOME_VALUE_CHARS] or default


def _bounded_outcome_counter(value: Any) -> tuple[dict[str, int], bool]:
    """Sanitize a bounded status/code counter without retaining result text."""

    if not isinstance(value, dict):
        return {}, False
    result: Counter[str] = Counter()
    distribution_truncated = False
    for index, (raw_name, raw_count) in enumerate(value.items()):
        if index >= MAX_TOOL_OUTCOME_KEYS * 4:
            distribution_truncated = True
            break
        name = _outcome_value(raw_name)
        if not name:
            continue
        if name not in result and len(result) >= MAX_TOOL_OUTCOME_KEYS:
            distribution_truncated = True
            continue
        try:
            count = max(0, min(10_000, int(raw_count or 0)))
        except (TypeError, ValueError, OverflowError):
            count = 0
        if count:
            result[name] += count
    return dict(sorted(result.items())), distribution_truncated


def _normalize_tool_result_observations(
    value: Any,
) -> tuple[list[dict[str, str]] | None, int]:
    """Return bounded schema observations; ``None`` means legacy/unknown."""

    if value is None:
        return None, 0
    result = []
    omitted = 0
    for row in value:
        if not isinstance(row, dict):
            continue
        if len(result) >= MAX_TOOL_RESULT_OBSERVATIONS:
            omitted += 1
            continue
        result.append({
            "status": _outcome_value(row.get("status"), default="unknown"),
            "error_code": _outcome_value(row.get("error_code")),
        })
    return result, omitted


def wants_previous_run_receipt(text: str) -> bool:
    """Return true only for questions auditing an already completed run."""
    value = " ".join(str(text or "").split())
    if not value:
        return False
    explicit_run = bool(re.search(
        r"\b(?:previous|prior|last|earlier|above)\s+(?:run|task|turn)\b",
        value,
        re.IGNORECASE,
    ))
    audit_question = bool(re.search(
        r"\b(?:what|why|how|did|was|were|explain|audit|confus(?:e|ed|ing))\b",
        value,
        re.IGNORECASE,
    ))
    return bool(
        (explicit_run and audit_question)
        or (_METRIC_RE.search(value) and _PRIOR_RE.search(value))
        or (_METRIC_RE.search(value) and _PAST_AUDIT_RE.search(value))
    )


def _call_category(value: Any) -> str:
    category = str(value or "agent").strip().lower()
    if category in ("memory_extraction", "post_turn_memory"):
        return "memory"
    return category if category in _CALL_CATEGORIES else "agent"


def terminal_cause_class(terminal_reason: str) -> str:
    return (
        "provider" if terminal_reason == "provider_error"
        else "harness" if terminal_reason in {
            "harness_error", "host_preflight_error", "cancellation_authority_failed"
        }
        else "user" if terminal_reason == "user_cancelled"
        else "model" if terminal_reason in {
            "completed", "concluded_effect", "model_output_limit", "model_error"
        }
        else "unknown"
    )


def build_run_receipt(
    *,
    run_id: str,
    status: str,
    tool_names: Iterable[Any],
    usage_events: Iterable[dict],
    wall_time_s: float,
    stop_reason: str = "",
    terminal_reason: str = "",
    length_recoveries: int = 0,
    provider_attempts: int = 0,
    clean_replays: int = 0,
    tool_result_observations: Iterable[dict] | None = None,
    tool_result_observations_truncated: int = 0,
    descendant_usage: dict[str, Any] | None = None,
    descendants: dict[str, Any] | None = None,
    settled_at: float = 0.0,
) -> dict:
    """Build one bounded receipt from authoritative runtime events."""
    tools = [
        str(name).strip()[:80]
        for name in (tool_names or [])
        if str(name or "").strip()
    ]
    events = [dict(event) for event in (usage_events or []) if isinstance(event, dict)]
    tool_counts = Counter(tools)
    category_counts = Counter(_call_category(event.get("call_category")) for event in events)
    llm_by_category = {
        category: int(category_counts.get(category, 0))
        for category in _CALL_CATEGORIES
        if category_counts.get(category, 0)
    }
    direct_usage = {
        "llm_calls": len(events),
        "prompt_tokens": sum(int(event.get("prompt_tokens") or 0) for event in events),
        "completion_tokens": sum(int(event.get("completion_tokens") or 0) for event in events),
        "cached_input_tokens": sum(
            int(event.get("cached_prompt_tokens") or 0) for event in events
        ),
        "total_tokens": sum(int(event.get("total_tokens") or 0) for event in events),
        "inference_time_s": round(sum(
            float(event.get("inference_time_s") or 0) for event in events
        ), 3),
        "cost_usd": round(sum(float(event.get("cost_usd") or 0) for event in events), 8),
    }
    cause_class = terminal_cause_class(terminal_reason)
    receipt = {
        "version": RECEIPT_VERSION,
        "run_id": str(run_id or "")[:64],
        "status": str(status or "unknown")[:24],
        "tool_calls": len(tools),
        "tool_calls_by_name": dict(sorted(tool_counts.items())),
        "tool_sequence": tools[:MAX_TOOL_SEQUENCE],
        "tool_sequence_truncated": len(tools) > MAX_TOOL_SEQUENCE,
        "llm_calls": direct_usage["llm_calls"],
        "llm_calls_by_category": llm_by_category,
        "prompt_tokens": direct_usage["prompt_tokens"],
        "completion_tokens": direct_usage["completion_tokens"],
        "cached_input_tokens": direct_usage["cached_input_tokens"],
        "total_tokens": direct_usage["total_tokens"],
        "inference_time_s": direct_usage["inference_time_s"],
        "wall_time_s": round(max(0.0, float(wall_time_s or 0)), 3),
        "cost_usd": direct_usage["cost_usd"],
        "cost_known": any(event.get("cost_usd") is not None for event in events),
        "routes": sorted({str(event.get("provider") or "unknown") for event in events}),
        "models": sorted({str(event.get("model") or "unknown") for event in events}),
        "stop_reason": str(stop_reason or "")[:40],
        "terminal_reason": str(terminal_reason or "")[:80],
        "cause_class": cause_class,
        "length_recoveries": max(0, int(length_recoveries or 0)),
        "provider_attempts": max(0, int(provider_attempts or 0)),
        "clean_replays": max(0, int(clean_replays or 0)),
        "direct_usage": direct_usage,
        "descendant_usage": dict(descendant_usage or {}),
        "descendants": dict(descendants or {}),
        "settled": bool(float(settled_at or 0) > 0),
        "settled_at": round(max(0.0, float(settled_at or 0)), 6),
    }
    observations, directly_omitted = _normalize_tool_result_observations(
        tool_result_observations
    )
    if observations is not None:
        status_counts = Counter(row["status"] for row in observations)
        error_code_counts = Counter(
            row["error_code"] for row in observations if row["error_code"]
        )
        without_code = sum(
            not row["error_code"]
            and row["status"] not in _SUCCESS_TOOL_RESULT_STATUSES
            for row in observations
        )
        truncated = max(0, min(
            1_000_000,
            int(tool_result_observations_truncated or 0) + directly_omitted,
        ))
        receipt.update({
            "tool_results_observed": True,
            "tool_result_count": len(observations) + truncated,
            "tool_results_by_status": dict(sorted(status_counts.items())),
            "tool_error_codes": dict(sorted(error_code_counts.items())),
            "tool_results_without_error_code": without_code,
            "tool_result_observations_truncated": truncated,
            "tool_result_status_distribution_truncated": bool(truncated),
            "tool_error_code_distribution_truncated": bool(truncated),
        })
    return receipt


def sanitize_run_receipt(raw: Any) -> dict:
    """Allowlist and bound a receipt before it enters durable chat state."""
    if not isinstance(raw, dict):
        return {}

    def bounded_int(value: Any, limit: int = 1_000_000_000_000) -> int:
        try:
            return max(0, min(limit, int(value or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    def bounded_float(value: Any, digits: int) -> float:
        try:
            return round(max(0.0, float(value or 0)), digits)
        except (TypeError, ValueError, OverflowError):
            return 0.0

    raw_sequence = raw.get("tool_sequence")
    sequence = [
        str(name).strip()[:80]
        for name in (raw_sequence if isinstance(raw_sequence, list) else [])
        if str(name or "").strip()
    ][:MAX_TOOL_SEQUENCE]
    raw_tool_counts = raw.get("tool_calls_by_name")
    tool_counts = {}
    if isinstance(raw_tool_counts, dict):
        for name, count in list(raw_tool_counts.items())[:256]:
            key = str(name or "").strip()[:80]
            if key:
                tool_counts[key] = bounded_int(count, 10_000)
    if not tool_counts and sequence:
        tool_counts = dict(Counter(sequence))
    raw_categories = raw.get("llm_calls_by_category")
    categories = {}
    if isinstance(raw_categories, dict):
        for category in _CALL_CATEGORIES:
            count = bounded_int(raw_categories.get(category), 1000)
            if count:
                categories[category] = count
    receipt = {
        "version": RECEIPT_VERSION,
        "run_id": str(raw.get("run_id") or "")[:64],
        "status": str(raw.get("status") or "unknown")[:24],
        "tool_calls": bounded_int(raw.get("tool_calls"), 10_000),
        "tool_calls_by_name": dict(sorted(tool_counts.items())),
        "tool_sequence": sequence,
        "tool_sequence_truncated": bool(raw.get("tool_sequence_truncated")),
        "llm_calls": bounded_int(raw.get("llm_calls"), 3000),
        "llm_calls_by_category": categories,
        "prompt_tokens": bounded_int(raw.get("prompt_tokens")),
        "completion_tokens": bounded_int(raw.get("completion_tokens")),
        "cached_input_tokens": bounded_int(raw.get("cached_input_tokens")),
        "total_tokens": bounded_int(raw.get("total_tokens")),
        "inference_time_s": bounded_float(raw.get("inference_time_s"), 3),
        "wall_time_s": bounded_float(raw.get("wall_time_s"), 3),
        "cost_usd": bounded_float(raw.get("cost_usd"), 8),
        "cost_known": bool(raw.get("cost_known")),
        "stop_reason": str(raw.get("stop_reason") or "")[:40],
        "terminal_reason": str(raw.get("terminal_reason") or "")[:80],
        "cause_class": str(raw.get("cause_class") or "unknown")[:24],
        "length_recoveries": bounded_int(raw.get("length_recoveries"), 1000),
        "provider_attempts": bounded_int(raw.get("provider_attempts"), 100_000),
        "clean_replays": bounded_int(raw.get("clean_replays"), 1000),
        "settled": bool(raw.get("settled")),
        "settled_at": bounded_float(raw.get("settled_at"), 6),
    }
    outcome_fields_present = (
        raw.get("tool_results_observed") is True
        or "tool_results_by_status" in raw
        or "tool_error_codes" in raw
    )
    if outcome_fields_present:
        status_counts, status_keys_truncated = _bounded_outcome_counter(
            raw.get("tool_results_by_status")
        )
        error_code_counts, error_code_keys_truncated = _bounded_outcome_counter(
            raw.get("tool_error_codes")
        )
        truncated = bounded_int(
            raw.get("tool_result_observations_truncated"), 1_000_000
        )
        retained_status_count = sum(status_counts.values())
        observed_count = (
            bounded_int(raw.get("tool_result_count"), 1_000_000)
            if "tool_result_count" in raw
            else retained_status_count + truncated
        )
        observed_count = max(observed_count, retained_status_count + truncated)
        status_distribution_truncated = bool(
            raw.get("tool_result_status_distribution_truncated")
            or status_keys_truncated
            or retained_status_count < observed_count
        )
        error_code_distribution_truncated = bool(
            raw.get("tool_error_code_distribution_truncated")
            or error_code_keys_truncated
            or truncated
        )
        receipt.update({
            "tool_results_observed": True,
            "tool_result_count": observed_count,
            "tool_results_by_status": status_counts,
            "tool_error_codes": error_code_counts,
            "tool_results_without_error_code": bounded_int(
                raw.get("tool_results_without_error_code"), 10_000
            ),
            "tool_result_observations_truncated": truncated,
            "tool_result_status_distribution_truncated": (
                status_distribution_truncated
            ),
            "tool_error_code_distribution_truncated": (
                error_code_distribution_truncated
            ),
        })
    raw_direct = raw.get("direct_usage")
    receipt["direct_usage"] = {
        "llm_calls": bounded_int((raw_direct or {}).get("llm_calls"), 100_000),
        "prompt_tokens": bounded_int((raw_direct or {}).get("prompt_tokens")),
        "completion_tokens": bounded_int(
            (raw_direct or {}).get("completion_tokens")
        ),
        "cached_input_tokens": bounded_int(
            (raw_direct or {}).get("cached_input_tokens")
        ),
        "total_tokens": bounded_int((raw_direct or {}).get("total_tokens")),
        "inference_time_s": bounded_float(
            (raw_direct or {}).get("inference_time_s"), 3
        ),
        "cost_usd": bounded_float((raw_direct or {}).get("cost_usd"), 8),
    } if isinstance(raw_direct, dict) else {}
    raw_descendant = raw.get("descendant_usage")
    receipt["descendant_usage"] = {
        "llm_calls": bounded_int(
            (raw_descendant or {}).get("llm_calls"), 100_000
        ),
        "total_tokens": bounded_int(
            (raw_descendant or {}).get("total_tokens")
        ),
        "wall_time_s": bounded_float(
            (raw_descendant or {}).get("wall_time_s"), 3
        ),
        "cost_usd": bounded_float(
            (raw_descendant or {}).get("cost_usd"), 8
        ),
    } if isinstance(raw_descendant, dict) else {}
    raw_descendants = raw.get("descendants")
    receipt["descendants"] = {
        "total": bounded_int((raw_descendants or {}).get("total"), 10_000),
        "active": bounded_int((raw_descendants or {}).get("active"), 10_000),
        "terminal": bounded_int((raw_descendants or {}).get("terminal"), 10_000),
        "ids": [
            str(value)[:96]
            for value in (
                (raw_descendants or {}).get("ids")
                if isinstance((raw_descendants or {}).get("ids"), list)
                else []
            )[:32]
            if str(value or "")
        ],
        "truncated": bool((raw_descendants or {}).get("truncated")),
    } if isinstance(raw_descendants, dict) else {}
    if not receipt["tool_calls"] and tool_counts:
        receipt["tool_calls"] = sum(tool_counts.values())
    if not receipt["llm_calls"] and categories:
        receipt["llm_calls"] = sum(categories.values())
    for key in ("routes", "models"):
        values = raw.get(key)
        receipt[key] = sorted({
            str(value).strip()[:120]
            for value in (values if isinstance(values, list) else [])
            if str(value or "").strip()
        })[:12]
    return receipt


def render_previous_execution_evidence(receipt: Any) -> str:
    """Small preceding-run evidence for ordinary warm-chat prompt construction."""
    if not isinstance(receipt, dict) or not receipt.get('run_id'):
        return ''
    row = sanitize_run_receipt(receipt)
    counts = row.get('tool_calls_by_name') or {}
    result_counts = row.get("tool_results_by_status") or {}
    error_codes = row.get("tool_error_codes") or {}
    non_success = sum(
        int(count or 0) for status, count in result_counts.items()
        if status not in _SUCCESS_TOOL_RESULT_STATUSES
    )
    distribution_truncated = bool(
        row.get("tool_result_status_distribution_truncated")
        or row.get("tool_error_code_distribution_truncated")
        or row.get("tool_result_observations_truncated")
    )
    if non_success or error_codes or distribution_truncated:
        data = {
            'prior_run_id': row['run_id'],
            'run_terminal_status': row['status'],
            'tool_calls': row['tool_calls'] if 'tool_calls' in receipt else None,
            'tools': dict(list(counts.items())[:8]),
            'tool_result_count': int(row.get("tool_result_count") or 0),
            'tool_results_by_status': result_counts,
        }
        if len(counts) > 8:
            data['tool_names_omitted'] = len(counts) - 8
        if error_codes:
            data['tool_error_codes'] = error_codes
        without_code = int(row.get("tool_results_without_error_code") or 0)
        if without_code:
            data['tool_results_without_error_code'] = without_code
        truncated = int(row.get("tool_result_observations_truncated") or 0)
        if truncated:
            data['tool_result_observations_truncated'] = truncated
        if row.get("tool_result_status_distribution_truncated"):
            data['tool_result_status_distribution_truncated'] = True
        if row.get("tool_error_code_distribution_truncated"):
            data['tool_error_code_distribution_truncated'] = True
        return (
            '[Host previous-run execution]\n'
            + json.dumps(data, ensure_ascii=False)
            + '\nrun_terminal_status records run settlement; '
            'tool_results_by_status records observed tool outcomes.'
        )
    data = {'prior_run_id': row['run_id'], 'status': row['status'],
            'tool_calls': row['tool_calls'] if 'tool_calls' in receipt else None,
            'tools': dict(list(counts.items())[:8])}
    if len(counts) > 8:
        data['tool_names_omitted'] = len(counts) - 8
    return ('[Host previous-run execution]\n' + json.dumps(data, ensure_ascii=False) +
            '\nThese counts/status record execution, not whether intended task effects succeeded.')


def render_previous_run_receipt(receipt: Any) -> str:
    """Render a compact factual block; it contains no behavioral instructions."""
    row = sanitize_run_receipt(receipt)
    if not row:
        return ""
    tools = row.get("tool_calls_by_name") or {}
    categories = row.get("llm_calls_by_category") or {}
    tool_detail = ", ".join(f"{name} x{count}" for name, count in tools.items()) or "none"
    llm_detail = ", ".join(
        f"{name} x{count}" for name, count in categories.items()
    ) or "none"
    lines = [
        "## Previous run receipt",
        f"Run: {row.get('run_id') or 'unknown'}; status: {row.get('status') or 'unknown'}.",
        f"Tool calls: {row.get('tool_calls', 0)} ({tool_detail}).",
        f"LLM calls: {row.get('llm_calls', 0)} ({llm_detail}).",
    ]
    if row.get("tool_results_observed") is True:
        outcome_counts = row.get("tool_results_by_status") or {}
        outcome_detail = ", ".join(
            f"{name} x{count}" for name, count in outcome_counts.items()
        ) or "none"
        lines.append(
            f"Observed tool results: {row.get('tool_result_count', 0)} "
            f"({outcome_detail})."
        )
        error_codes = row.get("tool_error_codes") or {}
        if error_codes:
            lines.append("Observed error codes: " + ", ".join(
                f"{name} x{count}" for name, count in error_codes.items()
            ) + ".")
        without_code = int(row.get("tool_results_without_error_code") or 0)
        if without_code:
            lines.append(
                f"Non-success tool results without an emitted error code: "
                f"{without_code}."
            )
        truncated = int(row.get("tool_result_observations_truncated") or 0)
        if truncated:
            lines.append(f"Tool-result observations omitted by bound: {truncated}.")
        if row.get("tool_result_status_distribution_truncated"):
            lines.append("Tool-result status distribution is partial.")
        if row.get("tool_error_code_distribution_truncated"):
            lines.append("Tool-result error-code distribution is partial.")
    sequence = list(row.get("tool_sequence") or [])
    if sequence:
        label = "Tool sequence (partial)" if row.get("tool_sequence_truncated") else "Tool sequence"
        lines.append(label + ": " + " -> ".join(sequence) + ".")
    resource = (
        f"Resources: {int(row.get('total_tokens') or 0):,} tokens; "
        f"{float(row.get('inference_time_s') or 0):.1f}s inference; "
        f"{float(row.get('wall_time_s') or 0):.1f}s wall"
    )
    if row.get("cost_known"):
        resource += f"; ${float(row.get('cost_usd') or 0):.6f}"
    lines.append(resource + ".")
    return "\n".join(lines)
