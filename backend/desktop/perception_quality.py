"""Compact perception quality metrics for Activity Monitor / Task Trace.

Pure helpers (unit-tested). desktop_control emits perception:quality_metrics
after significant perception steps — observability only, no perception logic changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

VERBOSITY_OFF = "off"
VERBOSITY_COMPACT = "compact"
VERBOSITY_NORMAL = "normal"
VERBOSITY_VERBOSE = "verbose"
_VALID_VERBOSITY = frozenset({
    VERBOSITY_OFF,
    VERBOSITY_COMPACT,
    VERBOSITY_NORMAL,
    VERBOSITY_VERBOSE,
})


@dataclass(frozen=True)
class QualityMetricsConfig:
    """tools.json → desktop.observability.quality_metrics*"""

    enabled: bool = True
    verbosity: str = VERBOSITY_NORMAL


@dataclass
class PerceptionQualityInput:
    """Signals for one perception quality metrics emission."""

    tool: str = ""
    window: str = ""
    step: str = ""               # perceive | capture
    controls: int = 0
    yield_status: str = ""       # ok | low | recovered
    reread: bool = False
    perception_mode: str = ""    # full | incremental
    incremental_savings_pct: Optional[int] = None
    incremental_changes: Optional[int] = None
    error_type: str = ""
    error_severity: str = ""
    error_recoverable: Optional[bool] = None
    capture_mode: str = ""
    capture_fallback: str = ""
    capture_size: str = ""
    modal_blocking: Optional[bool] = None
    stack_depth: Optional[int] = None
    extra_warnings: list[str] = field(default_factory=list)


def merge_observability_config(raw: dict | None) -> QualityMetricsConfig:
    raw = raw if isinstance(raw, dict) else {}
    qm = raw.get("quality_metrics")
    if isinstance(qm, bool):
        enabled = qm
        verbosity = VERBOSITY_NORMAL
    elif isinstance(qm, dict):
        enabled = bool(qm.get("enabled", True))
        verbosity = str(qm.get("verbosity") or VERBOSITY_NORMAL).strip().lower()
    else:
        enabled = bool(raw.get("quality_metrics_enabled", True))
        verbosity = str(raw.get("quality_metrics_verbosity") or VERBOSITY_NORMAL).strip().lower()
    if verbosity not in _VALID_VERBOSITY:
        verbosity = VERBOSITY_NORMAL
    if not enabled or verbosity == VERBOSITY_OFF:
        return QualityMetricsConfig(enabled=False, verbosity=VERBOSITY_OFF)
    return QualityMetricsConfig(enabled=True, verbosity=verbosity)


def should_emit(cfg: QualityMetricsConfig) -> bool:
    return bool(cfg.enabled and cfg.verbosity != VERBOSITY_OFF)


def infer_yield_status(
    controls: int,
    *,
    min_controls: int = 3,
    recovered: bool = False,
) -> str:
    if recovered:
        return "recovered"
    if controls < min_controls:
        return "low"
    return "ok"


def format_metrics_line(ctx: PerceptionQualityInput, *, verbosity: str = VERBOSITY_NORMAL) -> str:
    """One human-readable line summarizing perception quality."""
    parts: list[str] = []

    if ctx.tool:
        parts.append(ctx.tool)
    if ctx.step and verbosity != VERBOSITY_COMPACT:
        parts.append(ctx.step)

    if ctx.controls or ctx.controls == 0:
        ctrl = f"{ctx.controls} ctrl"
        ys = ctx.yield_status or "ok"
        if verbosity == VERBOSITY_COMPACT:
            parts.append(ctrl)
        else:
            parts.append(f"{ctrl} yield:{ys}")

    if ctx.reread:
        parts.append("reread")

    if ctx.perception_mode:
        parts.append(ctx.perception_mode)

    if ctx.incremental_savings_pct is not None and ctx.perception_mode == "incremental":
        inc = f"Δ:{ctx.incremental_savings_pct}%"
        if ctx.incremental_changes is not None and verbosity != VERBOSITY_COMPACT:
            inc += f" ({ctx.incremental_changes} chg)"
        parts.append(inc)

    if ctx.error_type:
        err = f"err:{ctx.error_type}"
        if verbosity != VERBOSITY_COMPACT and ctx.error_severity:
            err += f" [{ctx.error_severity}]"
        parts.append(err)

    if ctx.capture_mode:
        cap = f"cap:{ctx.capture_mode}"
        if ctx.capture_size and verbosity != VERBOSITY_COMPACT:
            cap += f" {ctx.capture_size}"
        if ctx.capture_fallback and verbosity == VERBOSITY_VERBOSE:
            cap += f" ({ctx.capture_fallback})"
        parts.append(cap)

    if ctx.modal_blocking and verbosity != VERBOSITY_COMPACT:
        parts.append("modal:blocking" if ctx.modal_blocking else "modal:clear")

    if ctx.stack_depth is not None and ctx.stack_depth > 1:
        parts.append(f"stack:{ctx.stack_depth}")

    if verbosity == VERBOSITY_VERBOSE and ctx.extra_warnings:
        parts.append("warn:" + ";".join(ctx.extra_warnings[:2]))

    body = " · ".join(parts) if parts else "no signals"
    return f"Perception quality: {body}"


def build_payload(
    ctx: PerceptionQualityInput,
    *,
    verbosity: str = VERBOSITY_NORMAL,
) -> dict[str, Any]:
    """Structured activity payload for perception:quality_metrics."""
    text = format_metrics_line(ctx, verbosity=verbosity)
    return {
        "text": text,
        "tool": ctx.tool or "",
        "window": (ctx.window or "")[:120],
        "step": ctx.step or "",
        "controls": ctx.controls,
        "yield_status": ctx.yield_status or "",
        "reread": ctx.reread,
        "perception_mode": ctx.perception_mode or "",
        "incremental_savings_pct": ctx.incremental_savings_pct,
        "incremental_changes": ctx.incremental_changes,
        "error_type": ctx.error_type or "",
        "error_severity": ctx.error_severity or "",
        "error_recoverable": ctx.error_recoverable,
        "capture_mode": ctx.capture_mode or "",
        "capture_fallback": ctx.capture_fallback or "",
        "capture_size": ctx.capture_size or "",
        "modal_blocking": ctx.modal_blocking,
        "stack_depth": ctx.stack_depth,
        "verbosity": verbosity,
        "warnings": list(ctx.extra_warnings),
    }


def perception_event_text(fields: dict) -> str:
    return fields.get("text") or "Perception quality metrics"


def enrich_from_capture_meta(ctx: PerceptionQualityInput, meta) -> PerceptionQualityInput:
    if meta is None:
        return ctx
    ctx.capture_mode = getattr(meta, "mode", "") or ""
    ctx.capture_fallback = getattr(meta, "fallback_reason", "") or ""
    w = getattr(meta, "width", 0) or 0
    h = getattr(meta, "height", 0) or 0
    if w and h:
        ctx.capture_size = f"{w}×{h}"
    return ctx


def enrich_from_error(ctx: PerceptionQualityInput, err) -> PerceptionQualityInput:
    if err is None:
        return ctx
    ctx.error_type = getattr(getattr(err, "error_type", None), "value", "") or str(
        getattr(err, "error_type", "") or ""
    )
    sev = getattr(err, "severity", None)
    ctx.error_severity = getattr(sev, "value", "") or str(sev or "")
    try:
        ctx.error_recoverable = bool(err.is_recoverable())
    except Exception:
        ctx.error_recoverable = None
    return ctx
