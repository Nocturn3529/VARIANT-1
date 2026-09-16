"""Dialog/modal detection helpers for desktop perception.

UIA-first detection when a window is locked; lightweight foreground-shift fallback
when dialog semantics are uncertain. Pure helpers are unit-tested; desktop_control
orchestrates I/O and activity emission.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Optional

from . import errors as derr


class ModalType(str, Enum):
    CONFIRMATION = "confirmation"
    SAVE_AS = "save_as"
    ERROR = "error"
    PERMISSION = "permission"
    UNKNOWN = "unknown"


_SAVE_AS_RE = re.compile(
    r"\b(save\s*as|save\s*file|export\s*as|choose\s*location|file\s*name)\b", re.I)
_ERROR_RE = re.compile(
    r"\b(error|failed|failure|warning|exception|problem|unable\s*to|could\s*not)\b", re.I)
_PERMISSION_RE = re.compile(
    r"\b(permission|allow|deny|access|authorize|authentication|uac|"
    r"do\s*you\s*want\s*to\s*allow|administrator)\b", re.I)
_CONFIRM_RE = re.compile(
    r"\b(confirm|are\s*you\s*sure|continue\?|proceed\?|delete\?|overwrite)\b", re.I)

_CANCEL_NAMES = frozenset({"cancel", "no", "close", "dismiss", "not now", "skip"})
_OK_NAMES = frozenset({"ok", "yes", "save", "open", "continue", "allow", "retry", "apply"})


@dataclass(frozen=True)
class ModalSnapshot:
    present: bool
    modal_type: str = ModalType.UNKNOWN.value
    title: str = ""
    summary: str = ""
    blocking: bool = False
    confidence: str = "low"  # high | medium | low
    source: str = "uia"  # uia | foreground_shift | vision_hint
    actions: tuple = ()  # button labels

    def signature(self) -> str:
        if not self.present:
            return ""
        acts = ",".join(self.actions[:4])
        return f"{self.modal_type}|{self.title}|{self.blocking}|{acts}"


def infer_modal_type(
    title: str,
    summary: str,
    actions: list | tuple,
) -> str:
    """Heuristic modal classification from title, body text, and button labels."""
    blob = f"{title or ''} {summary or ''}".strip()
    acts = [str(a).strip().lower() for a in (actions or []) if str(a).strip()]

    if _SAVE_AS_RE.search(blob) or (
        any("save" in a for a in acts) and any(a in _CANCEL_NAMES for a in acts)
    ):
        return ModalType.SAVE_AS.value
    if _PERMISSION_RE.search(blob) or any("allow" in a or "deny" in a for a in acts):
        return ModalType.PERMISSION.value
    if _ERROR_RE.search(blob):
        return ModalType.ERROR.value
    if _CONFIRM_RE.search(blob) or (
        any(a in _OK_NAMES for a in acts) and any(a in _CANCEL_NAMES for a in acts)
    ):
        return ModalType.CONFIRMATION.value
    if acts and all(a in _OK_NAMES for a in acts):
        return ModalType.ERROR.value
    return ModalType.UNKNOWN.value


def format_modal_summary(snap: ModalSnapshot) -> str:
    if not snap.present:
        return "No modal detected."
    parts = [
        f"Modal ({snap.modal_type}, {snap.confidence} confidence, source={snap.source})",
        f"Title: {snap.title or '(untitled)'}",
    ]
    if snap.summary:
        parts.append(f"Summary: {snap.summary[:200]}")
    if snap.actions:
        parts.append("Actions: " + ", ".join(snap.actions))
    parts.append(f"Blocking: {'yes' if snap.blocking else 'no (uncertain)'}")
    if snap.confidence == "low":
        parts.append(
            "Note: detection is uncertain; call computer.observe again if needed."
        )
    return "\n".join(parts)


def modal_event_text(event: str, fields: dict) -> str:
    if event == "perception:modal_detected":
        mt = fields.get("modal_type") or ModalType.UNKNOWN.value
        title = fields.get("title") or fields.get("window") or "(untitled)"
        blocking = fields.get("blocking")
        conf = fields.get("confidence") or ""
        acts = fields.get("actions") or ""
        block_s = "blocking" if blocking else "non-blocking/uncertain"
        tail = f" [{conf}]" if conf else ""
        act_s = f" — buttons: {acts}" if acts else ""
        return f"Modal detected ({mt}): '{title}' ({block_s}){tail}{act_s}"
    if event == "perception:modal_dismissed":
        title = fields.get("title") or fields.get("window") or "(modal)"
        return f"Modal dismissed: '{title}'"
    return fields.get("text") or event


def detected_activity_fields(snap: ModalSnapshot, *, tool: str = "") -> dict:
    acts = ", ".join(snap.actions[:8])
    text = modal_event_text("perception:modal_detected", {
        "modal_type": snap.modal_type,
        "title": snap.title,
        "blocking": snap.blocking,
        "confidence": snap.confidence,
        "actions": acts,
    })
    return {
        "text": text,
        "modal_type": snap.modal_type,
        "title": (snap.title or "")[:120],
        "window": (snap.title or "")[:120],
        "summary": (snap.summary or "")[:200],
        "actions": acts,
        "blocking": bool(snap.blocking),
        "confidence": snap.confidence,
        "source": snap.source,
        "tool": tool or "",
    }


def dismissed_activity_fields(snap: ModalSnapshot, *, tool: str = "") -> dict:
    text = modal_event_text("perception:modal_dismissed", {
        "title": snap.title,
    })
    return {
        "text": text,
        "title": (snap.title or "")[:120],
        "window": (snap.title or "")[:120],
        "modal_type": snap.modal_type,
        "tool": tool or "",
    }


def modal_blocking_error(snap: ModalSnapshot, *, tool: str = "") -> derr.DesktopError:
    detail = snap.title or snap.summary or snap.modal_type
    return derr.modal_blocking_error(
        modal_type=snap.modal_type,
        title=snap.title,
        detail=f"{snap.modal_type}: {detail[:80]}",
        tool=tool or "computer.observe",
        window=(snap.title or "")[:120],
    )


def _role(control_type_name: str) -> str:
    name = control_type_name or ""
    return name[:-7] if name.endswith("Control") else name


def _window_title(ctrl) -> str:
    if ctrl is None:
        return ""
    try:
        return (ctrl.Name or "").strip()
    except Exception:
        return ""


def _same_native_handle(a, b) -> bool:
    try:
        ha = int(getattr(a, "NativeWindowHandle", 0) or 0)
        hb = int(getattr(b, "NativeWindowHandle", 0) or 0)
        if ha and hb:
            return ha == hb
    except Exception:
        pass
    return a is b


def _top_level(ctrl):
    if ctrl is None:
        return None
    try:
        return ctrl.GetTopLevelControl() or ctrl
    except Exception:
        return ctrl


def _is_dialog_control(ctrl) -> bool:
    if ctrl is None:
        return False
    try:
        if (ctrl.ClassName or "").lower() == "#32770":
            return True
    except Exception:
        pass
    try:
        ctn = ctrl.ControlTypeName or ""
        if "Dialog" in ctn:
            return True
    except Exception:
        pass
    try:
        if ctrl.IsWindowPatternAvailable():
            if bool(ctrl.GetWindowPattern().IsModal):
                return True
    except Exception:
        pass
    return False


def _looks_like_modal_overlay(fg, locked) -> bool:
    """Return true only for a semantically modal, related foreground HWND."""
    if fg is None or locked is None:
        return False
    try:
        if int(fg.ProcessId or 0) != int(locked.ProcessId or 0):
            return False
    except Exception:
        return False
    try:
        if fg.IsWindowPatternAvailable() and bool(
            fg.GetWindowPattern().IsModal
        ):
            return True
    except Exception:
        pass
    try:
        child_hwnd = int(getattr(fg, "NativeWindowHandle", 0) or 0)
        parent_hwnd = int(getattr(locked, "NativeWindowHandle", 0) or 0)
    except Exception:
        child_hwnd = parent_hwnd = 0
    if not child_hwnd or not parent_hwnd or not _is_dialog_control(fg):
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        current = child_hwnd
        seen: set[int] = set()
        for _ in range(16):
            if current in seen:
                break
            seen.add(current)
            current = int(user32.GetWindow(current, 4) or 0)  # GW_OWNER
            if not current:
                return False
            if current == parent_hwnd:
                return True
    except Exception:
        return False
    return False


def _derive_summary(auto, root, max_nodes: int = 12) -> str:
    try:
        parts, seen = [], 0
        for child, _depth in auto.WalkControl(root, includeTop=False, maxDepth=4):
            seen += 1
            if seen > max_nodes:
                break
            try:
                role = _role(child.ControlTypeName)
            except Exception:
                continue
            if role not in ("Text", "Edit", "Document"):
                continue
            nm = (child.Name or "").strip()
            if not nm:
                try:
                    if child.IsValuePatternAvailable():
                        nm = (child.GetValuePattern().Value or "").strip()
                except Exception:
                    nm = ""
            if nm and nm not in parts:
                parts.append(nm)
        return " · ".join(parts)[:200]
    except Exception:
        return ""


def collect_modal_actions(auto, root, max_buttons: int = 12) -> list:
    actions = []
    try:
        for control, depth in auto.WalkControl(root, includeTop=False, maxDepth=8):
            if depth > 6:
                break
            try:
                role = _role(control.ControlTypeName)
            except Exception:
                continue
            if role not in ("Button", "Hyperlink", "SplitButton"):
                continue
            name = (control.Name or "").strip()
            if name and name not in actions:
                actions.append(name)
            if len(actions) >= max_buttons:
                break
    except Exception:
        pass
    return actions


def _build_snapshot(
    ctrl,
    *,
    source: str,
    confidence: str,
    auto=None,
    locked=None,
) -> ModalSnapshot:
    title = _window_title(ctrl)
    summary = _derive_summary(auto, ctrl) if auto is not None else ""
    actions = tuple(collect_modal_actions(auto, ctrl)) if auto is not None else ()
    modal_type = infer_modal_type(title, summary, actions)
    blocking = confidence in ("high", "medium") and (
        _is_dialog_control(ctrl)
        or source in ("uia_foreground", "uia_child_modal")
        or _looks_like_modal_overlay(ctrl, locked)
    )
    if confidence == "low":
        blocking = False
    return ModalSnapshot(
        present=True,
        modal_type=modal_type,
        title=title,
        summary=summary,
        blocking=blocking,
        confidence=confidence,
        source=source if source != "uia_foreground_shift" else "foreground_shift",
        actions=actions,
    )


def scan_modal_uia(auto, locked_win, locked_title: str = "") -> Optional[ModalSnapshot]:
    """UIA-based modal scan while a target window is locked. UIA worker thread."""
    if locked_win is None:
        return None

    locked_top = _top_level(locked_win)
    candidates: list[tuple] = []

    fg = auto.GetForegroundControl()
    fg_top = _top_level(fg)
    if fg_top and not _same_native_handle(fg_top, locked_top):
        if _looks_like_modal_overlay(fg_top, locked_top):
            candidates.append((fg_top, "uia_foreground", "high"))
        else:
            fg_name = _window_title(fg_top)
            if fg_name and fg_name != (locked_title or _window_title(locked_top)):
                candidates.append((fg_top, "uia_foreground_shift", "low"))

    try:
        for child in locked_top.GetChildren():
            if _is_dialog_control(child):
                candidates.append((child, "uia_child_modal", "high"))
    except Exception:
        pass

    if not candidates:
        return ModalSnapshot(present=False)

    rank = {"high": 3, "medium": 2, "low": 1}
    ctrl, source, confidence = max(candidates, key=lambda c: rank.get(c[2], 0))
    return _build_snapshot(
        ctrl,
        source=source,
        confidence=confidence,
        auto=auto,
        locked=locked_top,
    )
