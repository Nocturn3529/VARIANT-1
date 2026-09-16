"""Small, privacy-bounded operational log projected onto backend stdout.

The full event trajectory remains in the structured trace and domain stores.
This module emits only the lifecycle facts needed to understand a live run from
``main0.log``: model calls, tool/capability outcomes, mutation, kernel, Work,
and meaningful recovery/error transitions.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


_LEVELS = frozenset({"debug", "info", "warn", "error"})
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|"
    r"\b(?:sk|xai|ghp|github_pat|AIza)[-_A-Za-z0-9]{12,}\b|"
    r"([?&](?:token|key|secret|password)=)[^&\s]+"
)
_PRIVATE_KEYS = frozenset({
    "api_key", "authorization", "body", "code", "content", "cookie",
    "headers", "input", "messages", "output", "password", "payload",
    "prompt", "response", "secret", "source", "text", "token",
})
_ERROR_STATUSES = frozenset({
    "error", "failed", "failure", "unknown_effect", "needs_reconciliation",
    "transcript_failed",
})
_WARN_STATUSES = frozenset({
    "blocked", "cancelled", "degraded", "disabled", "recovered", "retry",
    "skipped", "stale", "timed_out", "unavailable",
})


def _one_line(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    text = _SECRET.sub(
        lambda match: (match.group(1) or match.group(2) or "") + "<redacted>",
        text,
    )
    return text[:limit] + ("…" if len(text) > limit else "")


def _safe_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _one_line(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _one_line(",".join(str(item) for item in list(value)[:16]), 300)
    return _one_line(value, 300)


def level_for_status(status: Any, *, default: str = "info") -> str:
    value = str(status or "").strip().casefold()
    if value in _ERROR_STATUSES:
        return "error"
    if value in _WARN_STATUSES:
        return "warn"
    return default if default in _LEVELS else "info"


def emit(
    source: str,
    event: str,
    *,
    level: str = "info",
    **fields: Any,
) -> None:
    """Write one parseable line; never raise or retain payload bodies."""

    try:
        clean_source = re.sub(r"[^a-z0-9_.-]+", "_", str(source or "runtime").lower())[:80]
        clean_event = re.sub(r"[^a-z0-9_.:-]+", "_", str(event or "event").lower())[:100]
        clean_level = str(level or "info").lower()
        if clean_level not in _LEVELS:
            clean_level = "info"
        parts = [
            "[op]",
            f"level={clean_level}",
            f"source={clean_source or 'runtime'}",
            f"event={clean_event or 'event'}",
        ]
        for raw_key, raw_value in fields.items():
            key = str(raw_key or "").strip().lower()
            if not _KEY.fullmatch(key) or key in _PRIVATE_KEYS:
                continue
            value = _safe_value(raw_value)
            if value in (None, ""):
                continue
            if isinstance(value, str):
                rendered = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, bool):
                rendered = "true" if value else "false"
            else:
                rendered = str(value)
            parts.append(f"{key}={rendered}")
        print(" ".join(parts), flush=True)
    except Exception:
        pass


def mirror_activity(message: Mapping[str, Any]) -> None:
    event = str(message.get("event") or "")
    status = str(message.get("status") or "")
    source = "run"
    selected = False
    if event in {"task:start", "task:step", "task:done", "task:resources"}:
        selected = True
    elif event in {"tool:start", "tool:result"}:
        source = "tool"
        selected = True
    elif event.startswith("perception:"):
        source = "desktop"
        selected = event in {
            "perception:error", "perception:focus_lost",
            "perception:low_yield", "perception:modal_detected",
            "perception:modal_dismissed", "perception:recovered",
        }
    elif event == "vision:image_fallback":
        source = "vision"
        selected = status not in {"", "ok"}
    if not selected:
        return
    emit(
        source,
        event,
        level=level_for_status(status),
        run_id=message.get("run_id"),
        source_kind=message.get("source"),
        step=message.get("step"),
        tool=message.get("tool"),
        call_id=message.get("call_id"),
        status=status,
        attempt=message.get("attempt"),
        admission_ms=message.get("admission_ms"),
        execution_ms=message.get("execution_ms"),
        receipt_id=message.get("receipt_id"),
        reason=message.get("reason"),
        mode=message.get("mode"),
    )


def mirror_trace(event: str, fields: Mapping[str, Any]) -> None:
    name = str(event or "")
    status = str(fields.get("status") or "")
    source = "runtime"
    selected = False
    if name in {"runtime:mutation_authority"}:
        source, selected = "mutation", True
    elif name in {"runtime:kernel_generation"} or name.startswith("kernel:"):
        source, selected = "kernel", True
    elif name == "runtime:startup_reconciled":
        selected = True
    elif name == "broker:result":
        source, selected = "capability", True
    elif name == "tool:start":
        source, selected = "tool", True
    elif name in {
        "broker:facility_blocked", "broker:harness_fault",
        "broker:receipt_sink_error", "broker:stale_reacquisition_required",
    }:
        source, selected = "capability", True
    elif name == "broker:batch_result" and status not in {"", "ok"}:
        source, selected = "capability", True
    elif name.startswith("connector:"):
        source, selected = "connector", True
    if not selected:
        return
    emit(
        source,
        name,
        level=level_for_status(status),
        status=status,
        chat_id=fields.get("chat_id"),
        run_id=fields.get("run_id"),
        call_id=fields.get("call_id"),
        receipt_id=fields.get("receipt_id"),
        capability_id=fields.get("capability_id"),
        error_code=fields.get("error_code"),
        cause_class=fields.get("cause_class"),
        retryable=fields.get("retryable"),
        kernel_generation=fields.get("kernel_generation"),
        attempt=fields.get("attempt"),
        revision=fields.get("revision"),
        server_id=fields.get("server_id"),
        capability_kind=fields.get("capability_kind"),
        capability_name=fields.get("capability_name"),
        tool=fields.get("tool"),
        chats=fields.get("chats"),
        tickets_requeued=fields.get("tickets_requeued"),
    )


def mirror_snapshot(event: str, fields: Mapping[str, Any]) -> None:
    name = str(event or "")
    result = str(fields.get("result") or "")
    reason = str(fields.get("reason") or "")
    if name in {"orphan_scan", "resume_scan", "lookup_miss"} and (
        result in {"", "none", "no_snapshot"}
        or "no snapshot" in reason.casefold()
    ):
        return
    if name in {"restore_messages"}:
        return
    level = "error" if "fail" in name else "warn" if "blocked" in name or "miss" in name else "info"
    emit(
        "snapshot",
        name,
        level=level,
        run_id=fields.get("run_id"),
        thread_id=fields.get("thread_id"),
        status=fields.get("status"),
        result=result,
        reason=reason,
        step=fields.get("step"),
        msgs=fields.get("msgs"),
    )


def mirror_model_manifest(manifest: Mapping[str, Any]) -> None:
    run = manifest.get("run") if isinstance(manifest.get("run"), Mapping) else {}
    route = manifest.get("route") if isinstance(manifest.get("route"), Mapping) else {}
    generation = manifest.get("generation") if isinstance(manifest.get("generation"), Mapping) else {}
    tools = manifest.get("tools") if isinstance(manifest.get("tools"), Mapping) else {}
    emit(
        "model",
        "request_start",
        run_id=run.get("run_id"),
        session_id=run.get("session_id"),
        manifest_id=manifest.get("manifest_id"),
        logical_call_id=manifest.get("logical_call_id"),
        provider=route.get("provider"),
        model=route.get("model"),
        transport=route.get("transport"),
        attempt=manifest.get("attempt"),
        max_output_tokens=generation.get("max_output_tokens"),
        tool_count=tools.get("rendered_count"),
    )


def mirror_model_usage(manifest: Mapping[str, Any]) -> None:
    run = manifest.get("run") if isinstance(manifest.get("run"), Mapping) else {}
    route = manifest.get("route") if isinstance(manifest.get("route"), Mapping) else {}
    usage = manifest.get("usage") if isinstance(manifest.get("usage"), Mapping) else {}
    emit(
        "model",
        "request_complete",
        run_id=run.get("run_id"),
        session_id=run.get("session_id"),
        manifest_id=manifest.get("manifest_id"),
        logical_call_id=manifest.get("logical_call_id"),
        provider=route.get("provider"),
        model=route.get("model"),
        input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
        output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
        cached_tokens=usage.get("cached_input_tokens") or usage.get("cached_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
        latency_ms=usage.get("latency_ms") or usage.get("inference_time_ms"),
        retries=usage.get("retries"),
    )


def mirror_work_terminal(event: Any, job: Any = None) -> None:
    event_type = str(getattr(event, "event_type", "") or "")
    if event_type not in {
        "job.succeeded", "job.failed", "job.cancelled", "job.unknown_effect",
    }:
        return
    status = event_type.split(".", 1)[-1]
    emit(
        "work",
        event_type,
        level=level_for_status(status),
        job_id=getattr(event, "aggregate_id", ""),
        job_kind=getattr(job, "kind", "") if job is not None else "",
        owner_kind=getattr(job, "owner_kind", "") if job is not None else "",
        owner_id=getattr(job, "owner_id", "") if job is not None else "",
        status=status,
        attempt=getattr(job, "attempt", 0) if job is not None else 0,
    )


__all__ = [
    "emit", "level_for_status", "mirror_activity", "mirror_model_manifest",
    "mirror_model_usage", "mirror_snapshot", "mirror_trace",
    "mirror_work_terminal",
]
