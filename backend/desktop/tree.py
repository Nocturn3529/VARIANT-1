"""UIA tree walk, snapshot, incremental format, fingerprints.

Extracted from perception.py (issue #10)."""
from __future__ import annotations

import tools
import desktop.perception_delta as pdelta
import time
from typing import Any

from .session import DesktopSessionState


_ITEM_CONTAINER_TYPES = {
    "dataitemcontrol", "listitemcontrol", "treeitemcontrol",
}
_VALUE_ROLES = {"Edit", "Document", "ComboBox"}


def _uia_text(control: Any, name: str) -> str:
    try:
        value = getattr(control, name)
        value = value() if callable(value) else value
        return str(value or "")
    except Exception:
        return ""


def _uia_bool(control: Any, name: str, *, default: bool = False) -> bool:
    try:
        value = getattr(control, name)
        return bool(value() if callable(value) else value)
    except Exception:
        return default


def _parent_control(control: Any) -> Any | None:
    try:
        return control.GetParentControl()
    except Exception:
        return None


def _control_identity(control: Any) -> tuple[Any, ...]:
    if control is None:
        return ("none",)
    try:
        runtime_id = tuple(int(item) for item in (control.GetRuntimeId() or ()))
    except Exception:
        runtime_id = ()
    if runtime_id:
        return ("runtime", *runtime_id)
    return ("object", id(control))


def _value_details(control: Any, role: str) -> tuple[str, bool, bool | None]:
    """Read a UIA value even when its availability property has drifted.

    Windows and Chromium controls can expose a working ``GetValuePattern``
    while omitting or falsely reporting ``IsValuePatternAvailable``. The
    getter is safe to probe for value-bearing roles and gives us the actual
    post-action value needed for deterministic verification.
    """

    advertised = _uia_bool(control, "IsValuePatternAvailable")
    if not advertised and role not in _VALUE_ROLES:
        return "", False, None
    try:
        pattern = control.GetValuePattern()
    except Exception:
        return "", False, None
    if pattern is None:
        return "", False, None
    try:
        value = str(getattr(pattern, "Value", "") or "")
    except Exception:
        value = ""
    try:
        marker = getattr(pattern, "IsReadOnly")
        read_only = bool(marker() if callable(marker) else marker)
    except Exception:
        read_only = None
    return value, True, read_only


def _typed_pattern(control: Any, getter: str) -> Any | None:
    """Resolve a role-appropriate pattern without convenience flags.

    The installed UIA binding omits several ``Is*PatternAvailable`` helpers
    while the typed getters work. Callers invoke this only for roles whose
    state is defined by the requested pattern.
    """

    try:
        return getattr(control, getter)()
    except Exception:
        return None


def _pattern_profile(control: Any, *, has_value: bool) -> frozenset[str]:
    names = {"value"} if has_value else set()
    for name, getter in (
        ("legacy", "GetLegacyIAccessiblePattern"),
        ("invoke", "GetInvokePattern"),
        ("selection", "GetSelectionItemPattern"),
        ("toggle", "GetTogglePattern"),
        ("range", "GetRangeValuePattern"),
        ("expand", "GetExpandCollapsePattern"),
        ("scroll", "GetScrollItemPattern"),
    ):
        try:
            if getattr(control, getter)() is not None:
                names.add(name)
        except Exception:
            pass
    return frozenset(names)


def _same_bounds(left: Any, right: Any) -> bool:
    if not left or not right or len(left) != 4 or len(right) != 4:
        return False
    try:
        return all(abs(int(a) - int(b)) <= 1 for a, b in zip(left, right))
    except (TypeError, ValueError, OverflowError):
        return False


def _merge_readable_column(parent: dict[str, Any], name: str, value: str) -> None:
    cleaned_name = str(name or "").strip()
    cleaned_value = str(value or "").strip()
    if not cleaned_value:
        return
    if (
        cleaned_name.casefold() == "name"
        and cleaned_value.casefold() == str(parent.get("name") or "").strip().casefold()
    ):
        return
    fragment = (
        f"{cleaned_name}: {cleaned_value}" if cleaned_name else cleaned_value
    )
    existing = [
        item.strip() for item in str(parent.get("text") or "").split(" · ")
        if item.strip()
    ]
    if fragment not in existing:
        existing.append(fragment)
    parent["text"] = " · ".join(existing)

def invalidate_incremental(session: DesktopSessionState, *, force_full_next: bool = True) -> None:
    session.incremental_baseline = None
    session.step_incremental = {}
    if force_full_next:
        session.incremental_force_full = True
    session.mark_updated()


def collect_controls(ctx: Any, auto, max_controls=None, max_depth=None, top=None):
    """Walk the target window's UIA tree. MUST run on the UIA worker thread.
    If `top` is given, walk that specific window; otherwise pick the foreground."""
    max_controls = max_controls if max_controls is not None else ctx.MAX_CONTROLS
    max_depth = max_depth if max_depth is not None else ctx.MAX_DEPTH
    if top is None:
        top = ctx._resolve_target(auto)
    ctx._observe_modal_state(auto)
    modal = ctx.session.current_modal
    if (
        top is not None
        and modal is not None
        and modal.present
        and modal.blocking
        and modal.confidence in {"high", "medium"}
    ):
        # A related foreground dialog is the safe input/perception target. The
        # resolver rejects unrelated windows and otherwise returns the lock.
        try:
            top = ctx._resolve_action_target(auto, action="inspect") or top
        except Exception:
            pass
    if not top:
        return "", []
    try:
        title = top.Name or ""
    except Exception:
        title = ""
    out = []
    rows_by_control: dict[tuple[Any, ...], dict[str, Any]] = {}
    value_meta_by_control: dict[
        tuple[Any, ...], tuple[bool, bool | None]
    ] = {}
    count = 0
    readable_count = 0
    occ = {}
    try:
        for control, depth in auto.WalkControl(top, includeTop=False, maxDepth=max_depth):
            try:
                role = ctx._role(control.ControlTypeName)
            except Exception:
                continue
            actionable = role in ctx.ACTIONABLE_ROLES
            if not actionable:
                # Drawing surfaces are not "actionable" by role, but drag
                # targets need their rect: Paint's canvas is a Group named
                # "Using Brush tool on Canvas" that the role filter dropped —
                # so the model guessed stroke coordinates and pressed on the
                # ribbon, where a stroke draws nothing (observed live
                # 2026-07-08). Surface it as role "Canvas".
                canvas = False
                if role in ("Group", "Image", "Pane", "Custom"):
                    try:
                        hint = (control.Name or "") + (control.AutomationId or "")
                    except Exception:
                        hint = ""
                    canvas = "canvas" in hint.lower()
                if canvas:
                    role = "Canvas"
                    actionable = True
                elif role in ctx.READABLE_ROLES:
                    if readable_count >= ctx.MAX_READABLE_CONTROLS:
                        continue
                else:
                    continue
            try:
                name = control.Name or ""
            except Exception:
                name = ""
            try:
                offscreen = bool(control.IsOffscreen)
            except Exception:
                offscreen = False
            if not name and not offscreen and role in ctx._LABEL_DERIVE_ROLES:
                name = ctx._derive_label(auto, control)
            if not name and role not in ("Edit", "ComboBox") and offscreen:
                continue
            value, has_value_pattern, value_read_only = _value_details(
                control, role
            )
            if not actionable and not (name or value):
                continue
            state = ""
            try:
                if role in ("ListItem", "TabItem", "TreeItem", "RadioButton"):
                    pattern = _typed_pattern(
                        control, "GetSelectionItemPattern"
                    )
                    if pattern is not None:
                        state = (
                            "selected" if pattern.IsSelected else "unselected"
                        )
                elif role in ("CheckBox", "MenuItem"):
                    pattern = _typed_pattern(control, "GetTogglePattern")
                    if pattern is not None:
                        state = {0: "off", 1: "on", 2: "mixed"}.get(
                            int(pattern.ToggleState), ""
                        )
            except Exception:
                state = ""
            bounds = None
            try:
                r = control.BoundingRectangle
                bounds = [r.left, r.top, r.right, r.bottom]
            except Exception:
                bounds = None
            try:
                aid = control.AutomationId or ""
            except Exception:
                aid = ""

            parent_control = _parent_control(control)
            parent_identity = _control_identity(parent_control)
            parent_row = rows_by_control.get(parent_identity)
            if role == "Edit" and parent_control is not None and parent_row is not None:
                parent_type = _uia_text(
                    parent_control, "ControlTypeName"
                ).casefold()
                parent_value_meta = value_meta_by_control.get(
                    parent_identity, (False, None)
                )

                # File/grid providers often expose each read-only column as an
                # Edit child of one actionable item. Fold only structurally
                # proved read-only cells into their parent row. Writable Name
                # fields remain separate even when they repeat the row label;
                # UIA focusability flags are not reliable enough to distinguish
                # an idle inline rename editor.
                if (
                    parent_type in _ITEM_CONTAINER_TYPES
                    and has_value_pattern
                    and value_read_only is True
                ):
                    _merge_readable_column(parent_row, name, value)
                    continue

                # WinUI can emit the same logical field twice: an identified
                # Edit parent and a blank-ID Edit child with identical bounds,
                # value and capabilities. Keep the identified ancestor only.
                # Same-name siblings and controls with any unique capability
                # or value remain separate.
                parent_aid = _uia_text(parent_control, "AutomationId")
                same_field = bool(
                    parent_row.get("actionable")
                    and parent_row.get("role") == role
                    and str(parent_row.get("name") or "").strip().casefold()
                    == str(name or "").strip().casefold()
                    and _same_bounds(parent_row.get("bounds"), bounds)
                    and parent_aid
                    and not aid
                    and parent_value_meta[0]
                    and has_value_pattern
                    and parent_value_meta[1] == value_read_only
                    and str(parent_row.get("value") or "") == value
                )
                if same_field:
                    parent_profile = _pattern_profile(
                        parent_control, has_value=parent_value_meta[0]
                    )
                    child_profile = _pattern_profile(
                        control, has_value=has_value_pattern
                    )
                    if child_profile.issubset(parent_profile):
                        if value and not parent_row.get("value"):
                            parent_row["value"] = value
                        continue

            if actionable and count >= max_controls:
                continue
            base = ("aid:" + aid) if aid else ("rn:%s|%s" % (role, name))
            occ[base] = occ.get(base, 0) + 1
            key = "%s#%d" % (base, occ[base])
            cid = (
                ctx._stable_id(key)
                if actionable
                else ctx._stable_state_id(key)
            )
            if actionable:
                count += 1
            else:
                readable_count += 1
            row = {"id": cid, "key": key, "role": role, "name": name,
                   "text": "", "value": value, "state": state,
                   "offscreen": offscreen, "bounds": bounds,
                   "control": control, "actionable": actionable}
            out.append(row)
            control_identity = _control_identity(control)
            rows_by_control[control_identity] = row
            value_meta_by_control[control_identity] = (
                has_value_pattern, value_read_only
            )
    except Exception as e:
        raise tools.ToolError(f"failed to walk the UI tree ({e})")
    return title, out


def store_snapshot(ctx: Any, title, controls):
    sess = ctx.session
    sess.last_snapshot = {c["id"]: c for c in controls}
    sess.snapshot_title = title or ""
    sess.snapshot_taken_at = time.time()
    sess.mark_updated()


def set_incremental_baseline(ctx: Any, title, controls):
    sess = ctx.session
    sess.incremental_baseline = pdelta.make_baseline(
        title,
        controls,
        sig_fn=ctx._sig,
        query=sess.target_meta.get("query") or "",
    )
    sess.incremental_force_full = False
    sess.mark_updated()


async def format_perception_output(
    ctx: Any,
    title: str,
    controls: list,
    args,
    *,
    tool_name: str = "computer",
    suffix: str = "",
    force_full: bool = False,
) -> str:
    """Full control list or compact incremental delta for model consumption."""

    store_snapshot(ctx, title, controls)
    pcfg = ctx.perception_config()
    mode = pdelta.parse_perception_mode(args, cfg=pcfg)
    sess = ctx.session
    sess.step_incremental = {
        "perception_mode": mode,
        "incremental_savings_pct": None,
        "incremental_changes": None,
    }
    sess.mark_updated()
    use_full = bool(force_full or sess.incremental_force_full or mode == "full")
    baseline = sess.incremental_baseline
    delta = None

    if (
        not use_full
        and baseline is not None
        and baseline.matches_window(title)
        and baseline.control_count > 0
    ):
        delta = pdelta.compute_delta(
            baseline,
            controls,
            sig_fn=ctx._sig,
            desc_fn=ctx._ctrl_desc,
            line_limit=pcfg.max_delta_lines,
        )
        if pdelta.should_force_full(delta, len(controls), pcfg):
            use_full = True
            await ctx._emit_perception(
                "perception:incremental_update",
                **pdelta.incremental_activity_fields(
                    delta,
                    savings_pct=0,
                    tool=tool_name,
                    window=title,
                    forced_full=True,
                ),
            )

    if not use_full and delta is not None:
        savings = pdelta.estimate_savings_pct(len(controls), delta)
        sess.step_incremental["incremental_savings_pct"] = savings
        sess.step_incremental["incremental_changes"] = delta.changed_count
        sess.mark_updated()
        body = pdelta.format_delta_output(
            title,
            controls,
            delta,
            prompt_cap=ctx._prompt_cap_now(),
            cfg=pcfg,
        )
        await ctx._emit_perception(
            "perception:incremental_update",
            **pdelta.incremental_activity_fields(
                delta,
                savings_pct=savings,
                tool=tool_name,
                window=title,
            ),
        )
        set_incremental_baseline(ctx, title, controls)
        if suffix:
            body += suffix
        return body

    body = ctx.format_controls(controls, title)
    set_incremental_baseline(ctx, title, controls)
    if suffix:
        body += suffix
    return body
def sig(_ctx: Any, rec) -> tuple:
    """Per-control signature for diffing one perception against the next."""
    return (rec.get("role"), (rec.get("name") or "").strip(),
            (rec.get("value") or "").strip(), (rec.get("state") or ""),
            bool(rec.get("offscreen")), bool(rec.get("actionable", True)))


def ctrl_desc(_ctx: Any, c) -> str:
    s = (
        '[%s] %s' % (c["id"], c.get("role"))
        if c.get("actionable", True)
        else 'State %s' % c.get("role")
    )
    nm = (c.get("name") or "").strip()
    if nm:
        s += ' "%s"' % nm
    v = (c.get("value") or "").strip()
    if v:
        s += ' = "%s"' % v[:40]
    st = (c.get("state") or "").strip()
    if st:
        s += " <%s>" % st
    if c.get("offscreen"):
        s += " (offscreen)"
    return s


def diff_lines(ctx: Any, prior_sig: dict, controls: list, limit: int = 40):
    """Compact diff of the new control list vs the prior snapshot's signatures."""
    new_sig = {c["id"]: ctx._sig(c) for c in controls}
    new_rec = {c["id"]: c for c in controls}
    changed = [i for i in new_sig if i in prior_sig and new_sig[i] != prior_sig[i]]
    added = [i for i in new_sig if i not in prior_sig]
    removed = [i for i in prior_sig if i not in new_sig]
    lines = []
    for i in sorted(changed):
        lines.append("~ " + ctx._ctrl_desc(new_rec[i]))
    for i in sorted(added):
        lines.append("+ " + ctx._ctrl_desc(new_rec[i]))
    for i in sorted(removed):
        ps = prior_sig[i]
        prefix = "[%s] " % i if len(ps) < 6 or ps[5] else "State "
        lines.append('- %s%s "%s" (gone)' % (prefix, ps[0], ps[1]))
    return lines[:limit], len(lines)
