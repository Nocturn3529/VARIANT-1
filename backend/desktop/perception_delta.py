"""Incremental UIA perception — baseline snapshots and compact control deltas.

Pure helpers (unit-tested). desktop_control orchestrates when to emit full vs delta
output; default remains full perception for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


DEFAULT_MAX_DELTA_LINES = 40
DEFAULT_MAX_DELTA_CHANGES = 40
DEFAULT_MAX_DELTA_RATIO = 0.35


@dataclass(frozen=True)
class PerceptionConfig:
    """Optional ``tools.json`` configuration under ``desktop.perception``."""

    incremental_default: bool = False
    max_delta_lines: int = DEFAULT_MAX_DELTA_LINES
    max_delta_changes: int = DEFAULT_MAX_DELTA_CHANGES
    max_delta_ratio: float = DEFAULT_MAX_DELTA_RATIO
    # Staleness guard: id-based actions re-read the UI incrementally first when
    # the snapshot they'd resolve against is older than this (seconds).
    # 0 disables the guard.
    stale_action_guard_s: float = 30.0


@dataclass(frozen=True)
class PerceptionBaseline:
    title: str
    signatures: dict
    control_count: int
    query: str = ""

    def matches_window(self, title: str) -> bool:
        return (self.title or "").strip() == (title or "").strip()


@dataclass(frozen=True)
class PerceptionDelta:
    changed: tuple
    added: tuple
    removed: tuple
    modified_count: int
    added_count: int
    removed_count: int
    total_changes: int
    control_count: int
    lines: tuple

    @property
    def changed_count(self) -> int:
        return self.modified_count + self.added_count + self.removed_count


def merge_perception_config(raw: dict | None) -> PerceptionConfig:
    raw = raw if isinstance(raw, dict) else {}
    base = PerceptionConfig()
    fields = {}
    if "incremental_default" in raw:
        fields["incremental_default"] = bool(raw["incremental_default"])
    for key, lo, hi in (
        ("max_delta_lines", 5, 200),
        ("max_delta_changes", 1, 500),
    ):
        if key not in raw:
            continue
        try:
            fields[key] = int(raw[key])
        except (TypeError, ValueError):
            pass
        if key in fields:
            fields[key] = max(lo, min(hi, fields[key]))
    if "max_delta_ratio" in raw:
        try:
            fields["max_delta_ratio"] = float(raw["max_delta_ratio"])
        except (TypeError, ValueError):
            pass
        if "max_delta_ratio" in fields:
            fields["max_delta_ratio"] = max(0.05, min(1.0, fields["max_delta_ratio"]))
    if "stale_action_guard_s" in raw:
        try:
            fields["stale_action_guard_s"] = max(0.0, min(600.0, float(raw["stale_action_guard_s"])))
        except (TypeError, ValueError):
            pass
    return PerceptionConfig(**{**base.__dict__, **fields})


def control_signature(rec: dict) -> tuple:
    """Per-control signature for stable-id diffing (matches desktop_control._sig)."""
    return (
        rec.get("role"),
        (rec.get("name") or "").strip(),
        (rec.get("value") or "").strip(),
        (rec.get("state") or "").strip(),
        bool(rec.get("offscreen")),
        bool(rec.get("actionable", True)),
    )


def make_baseline(
    title: str,
    controls: list,
    *,
    sig_fn: Callable[[dict], tuple] | None = None,
    query: str = "",
) -> PerceptionBaseline:
    sig = sig_fn or control_signature
    return PerceptionBaseline(
        title=(title or "").strip(),
        signatures={c["id"]: sig(c) for c in controls},
        control_count=len(controls),
        query=(query or "").strip(),
    )


def compute_delta(
    baseline: PerceptionBaseline,
    controls: list,
    *,
    sig_fn: Callable[[dict], tuple] | None = None,
    line_limit: int = DEFAULT_MAX_DELTA_LINES,
    desc_fn: Callable[[dict], str] | None = None,
) -> PerceptionDelta:
    sig = sig_fn or control_signature
    prior_sig = baseline.signatures
    new_sig = {c["id"]: sig(c) for c in controls}
    new_rec = {c["id"]: c for c in controls}

    changed = [i for i in new_sig if i in prior_sig and new_sig[i] != prior_sig[i]]
    added = [i for i in new_sig if i not in prior_sig]
    removed = [i for i in prior_sig if i not in new_sig]

    lines = []
    for i in sorted(changed):
        lines.append("~ " + (desc_fn(new_rec[i]) if desc_fn else _default_desc(new_rec[i])))
    for i in sorted(added):
        lines.append("+ " + (desc_fn(new_rec[i]) if desc_fn else _default_desc(new_rec[i])))
    for i in sorted(removed):
        ps = prior_sig[i]
        prefix = "[%s] " % i if len(ps) < 6 or ps[5] else "State "
        lines.append('- %s%s "%s" (gone)' % (prefix, ps[0], ps[1]))

    total = len(lines)
    return PerceptionDelta(
        changed=tuple(changed),
        added=tuple(added),
        removed=tuple(removed),
        modified_count=len(changed),
        added_count=len(added),
        removed_count=len(removed),
        total_changes=total,
        control_count=len(controls),
        lines=tuple(lines[:line_limit]),
    )


def _default_desc(c: dict) -> str:
    s = "[%s] %s" % (c["id"], c.get("role", "?"))
    nm = (c.get("name") or "").strip()
    if nm:
        s += ' "%s"' % nm
    val = (c.get("value") or "").strip()
    if val:
        s += ' = "%s"' % val[:40]
    st = (c.get("state") or "").strip()
    if st:
        s += " <%s>" % st
    if c.get("offscreen"):
        s += " (offscreen)"
    return s


def should_force_full(
    delta: PerceptionDelta,
    control_count: int,
    cfg: PerceptionConfig,
) -> bool:
    """Fall back to full tree when the delta is too large to be useful."""
    if delta.total_changes == 0:
        return False
    if delta.total_changes > cfg.max_delta_changes:
        return True
    total = max(int(control_count or 0), 1)
    return (delta.total_changes / total) > cfg.max_delta_ratio


def estimate_savings_pct(total_controls: int, delta: PerceptionDelta) -> int:
    """Rough token savings vs dumping the full formatted list."""
    if total_controls <= 0:
        return 0
    unchanged = max(0, total_controls - delta.changed_count)
    if unchanged <= 0:
        return 0
    # Full list ~1 line/control; delta emits only changed lines + short header.
    full_units = total_controls
    delta_units = max(delta.total_changes, len(delta.lines)) + 3
    saved = max(0, 100 - int(100 * delta_units / max(full_units, 1)))
    return min(99, saved)


def format_delta_output(
    window_title: str,
    controls: list,
    delta: PerceptionDelta,
    *,
    prompt_cap: int = 100,
    cfg: PerceptionConfig | None = None,
) -> str:
    cfg = cfg or PerceptionConfig()
    header = f"Foreground window: {window_title or '(unknown)'} (incremental update)"
    if delta.total_changes == 0:
        return (
            f"{header}\n"
            f"{len(controls)} items tracked; no changes since the last snapshot."
        )

    summary = (
        f"{len(controls)} controls tracked; "
        f"{delta.total_changes} change(s) "
        f"(+{delta.added_count} −{delta.removed_count} ~{delta.modified_count}):"
    )
    body_lines = list(delta.lines)
    if delta.total_changes > len(body_lines):
        body_lines.append(f"(+{delta.total_changes - len(body_lines)} more changes not shown)")
    unchanged = max(0, len(controls) - delta.changed_count)
    footer = f"\n({unchanged} unchanged; mode=incremental.)"
    return header + "\n" + summary + "\n" + "\n".join(body_lines) + footer


def incremental_event_text(fields: dict) -> str:
    changed = fields.get("changed", 0)
    total = fields.get("total_controls", 0)
    savings = fields.get("savings_pct", 0)
    window = fields.get("window") or ""
    return (
        f"Incremental UI update: {changed} change(s) across {total} controls"
        f" (~{savings}% fewer tokens vs full list)"
        + (f" · '{window}'" if window else "")
    )


def incremental_activity_fields(
    delta: PerceptionDelta,
    *,
    savings_pct: int = 0,
    tool: str = "computer",
    window: str = "",
    forced_full: bool = False,
) -> dict:
    text = incremental_event_text({
        "changed": delta.changed_count,
        "total_controls": delta.control_count,
        "savings_pct": savings_pct,
        "window": window,
    })
    if forced_full:
        text += " (auto-upgraded to full — delta too large)"
    return {
        "text": text,
        "changed": delta.changed_count,
        "modified": delta.modified_count,
        "added": delta.added_count,
        "removed": delta.removed_count,
        "total_changes": delta.total_changes,
        "total_controls": delta.control_count,
        "savings_pct": savings_pct,
        "window": (window or "")[:120],
        "tool": tool or "",
        "forced_full": forced_full,
    }


def parse_perception_mode(args: dict | None, *, cfg: PerceptionConfig | None = None) -> str:
    """Return 'full' or 'incremental'. Default is full unless configured otherwise."""
    args = args if isinstance(args, dict) else {}
    cfg = cfg or PerceptionConfig()
    if args.get("force_full") in (True, "true", "1", 1):
        return "full"
    if args.get("full") in (True, "true", "1", 1):
        return "full"
    mode = (args.get("mode") or args.get("perception") or "").strip().lower()
    if mode in ("incremental", "delta", "diff"):
        return "incremental"
    if mode in ("full", "complete", "refresh"):
        return "full"
    if cfg.incremental_default:
        return "incremental"
    return "full"
