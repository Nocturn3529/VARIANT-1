"""Configuration and event helpers for deterministic desktop perception."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_MIN_CONTROLS_FOR_UIA = 3
DEFAULT_REREAD_WAIT_SEC = 0.8


@dataclass(frozen=True)
class RecoveryConfig:
    """Tunable bounded UIA reread settings."""

    reread_enabled: bool = True
    min_controls_for_uia: int = DEFAULT_MIN_CONTROLS_FOR_UIA
    reread_wait_sec: float = DEFAULT_REREAD_WAIT_SEC


def merge_recovery_config(raw: dict | None) -> RecoveryConfig:
    """Build RecoveryConfig from a partial dict (unknown keys ignored)."""
    raw = raw if isinstance(raw, dict) else {}
    base = RecoveryConfig()
    fields = {}
    for key in (
        "reread_enabled",
        "min_controls_for_uia",
        "reread_wait_sec",
    ):
        if key not in raw:
            continue
        val = raw[key]
        if key == "reread_enabled":
            fields[key] = bool(val)
        elif key == "min_controls_for_uia":
            try:
                fields[key] = int(val)
            except (TypeError, ValueError):
                pass
        elif key == "reread_wait_sec":
            try:
                fields[key] = float(val)
            except (TypeError, ValueError):
                pass
    cfg = RecoveryConfig(**{**base.__dict__, **fields})
    return RecoveryConfig(
        reread_enabled=cfg.reread_enabled,
        min_controls_for_uia=max(0, cfg.min_controls_for_uia),
        reread_wait_sec=max(0.0, cfg.reread_wait_sec),
    )


def is_poor_yield(control_count: int, cfg: RecoveryConfig) -> bool:
    """True when UIA returned too few actionable controls to trust."""
    return int(control_count or 0) < int(cfg.min_controls_for_uia)


def reread_note(controls: int, *, recovered: bool) -> str:
    """Neutral evidence that the one bounded reread occurred."""
    state = "recovered" if recovered else "remained sparse"
    return f"\n\n[UIA reread {state}: {int(controls or 0)} controls.]"


def perception_event_text(event: str, fields: dict) -> str:
    """Compact line for Activity Monitor / Task Trace."""
    controls = fields.get("controls")
    tool = fields.get("tool") or "computer"
    if event == "perception:low_yield":
        return (
            f"UIA low yield ({controls} controls < threshold) — "
            "one bounded reread"
        )
    if event == "perception:recovered":
        return f"Perception recovered via {tool} ({controls} controls)"
    if event == "perception:focus_lost":
        last = fields.get("window") or fields.get("last_title") or "(unknown)"
        reason = fields.get("reason") or "invalid"
        fallback = fields.get("fallback") or fields.get("fallback_title") or ""
        tail = f" — now using foreground '{fallback}'" if fallback else " — using foreground window"
        return f"Locked target lost: '{last}' ({reason}){tail}"
    if event == "perception:focus_restored":
        title = fields.get("window") or fields.get("title") or ""
        return f"Locked target restored: '{title}'"
    if event == "perception:capture_scope":
        return fields.get("text") or "vision capture"
    if event == "perception:quality_metrics":
        return fields.get("text") or "perception quality metrics"
    if event == "perception:error":
        et = fields.get("error_type") or "UNKNOWN"
        action = fields.get("recovery_action") or ""
        detail = fields.get("detail") or ""
        tail = f" → {action}" if action else ""
        return f"{et}: {detail or et}{tail}"
    return fields.get("text") or event


def focus_loss_warning_text(
    last_title: str,
    reason: str,
    fallback_title: str,
    *,
    query: str = "",
) -> str:
    """Explicit warning appended to perception/action tool output."""
    last = (last_title or "(unknown)").strip()
    fb = (fallback_title or "(foreground)").strip()
    why = (reason or "no longer valid").replace("_", " ")
    hint = (
        f"Use computer.focus(name={query!r}) to select it again."
        if query
        else "Select the intended window again with computer.focus."
    )
    return (
        f"\n\n[TARGET LOST] The locked window '{last}' is {why}. "
        f"Perception/actions below use '{fb}' instead — do NOT assume you are still "
        f"on the original app. {hint}"
    )


def focus_loss_activity_fields(
    *,
    last_title: str,
    reason: str,
    fallback_title: str = "",
    query: str = "",
    tool: str = "",
) -> dict:
    """Payload for perception:focus_lost activity events."""
    text = perception_event_text("perception:focus_lost", {
        "window": last_title,
        "reason": reason,
        "fallback": fallback_title,
    })
    return {
        "text": text,
        "window": (last_title or "")[:120],
        "last_title": last_title or "",
        "reason": reason or "invalid",
        "fallback": (fallback_title or "")[:120],
        "fallback_title": fallback_title or "",
        "query": (query or "")[:80],
        "tool": tool or "",
    }


def focus_restored_activity_fields(*, title: str, query: str = "") -> dict:
    text = perception_event_text("perception:focus_restored", {"window": title, "title": title})
    return {
        "text": text,
        "window": (title or "")[:120],
        "title": title or "",
        "query": (query or "")[:80],
    }


def should_skip_reread(args: dict | None) -> bool:
    """Bypass the bounded reread for explicit visual-grounding preparation."""
    args = args or {}
    if args.get("_reread_internal"):
        return True
    flag = args.get("skip_reread")
    if flag is True:
        return True
    if isinstance(flag, str) and flag.strip().lower() in ("1", "true", "yes"):
        return True
    return False


def activity_fields(
    event: str,
    *,
    controls: int = 0,
    tool: str = "computer",
    window: str = "",
    attempt: int = 0,
    **extra: Any,
) -> dict:
    """Standard payload for perception:* activity events."""
    text = perception_event_text(event, {
        "controls": controls,
        "tool": tool,
        **extra,
    })
    out = {
        "controls": controls,
        "tool": tool,
        "text": text,
    }
    if window:
        out["window"] = window[:120]
    if attempt:
        out["attempt"] = attempt
    out.update(extra)
    return out
