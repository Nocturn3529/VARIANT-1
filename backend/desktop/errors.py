"""Typed desktop failure taxonomy for tool results and observability."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Optional

# Machine-readable tag appended to tool output (one line, parseable by resilience).
_ERROR_TAG_RE = re.compile(r"\[desktop_error:([A-Z][A-Z0-9_]*)\]")


class DesktopErrorType(str, Enum):
    # Perception / lock (P1 #4)
    EMPTY_TREE = "EMPTY_TREE"
    LOW_YIELD = "LOW_YIELD"
    STALE_HANDLE = "STALE_HANDLE"
    HANDLE_INVALID = "HANDLE_INVALID"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    FOCUS_LOST = "FOCUS_LOST"
    ELEVATED_TARGET = "ELEVATED_TARGET"
    MODAL_BLOCKING = "MODAL_BLOCKING"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    # Tool availability
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"


class ErrorSeverity(str, Enum):
    RECOVERABLE = "recoverable"
    FATAL = "fatal"
    UNAVAILABLE = "unavailable"


class RecoveryAction(str, Enum):
    REFOCUS = "refocus"
    REREAD = "reread"
    INTERACT_MODAL = "interact_modal"
    USER_ESCALATE = "user_escalate"


_DEFAULT_SEVERITY: dict[DesktopErrorType, ErrorSeverity] = {
    DesktopErrorType.EMPTY_TREE: ErrorSeverity.RECOVERABLE,
    DesktopErrorType.LOW_YIELD: ErrorSeverity.RECOVERABLE,
    DesktopErrorType.STALE_HANDLE: ErrorSeverity.RECOVERABLE,
    DesktopErrorType.HANDLE_INVALID: ErrorSeverity.FATAL,
    DesktopErrorType.WINDOW_CLOSED: ErrorSeverity.FATAL,
    DesktopErrorType.FOCUS_LOST: ErrorSeverity.RECOVERABLE,
    DesktopErrorType.ELEVATED_TARGET: ErrorSeverity.UNAVAILABLE,
    DesktopErrorType.MODAL_BLOCKING: ErrorSeverity.FATAL,
    DesktopErrorType.CAPTURE_FAILED: ErrorSeverity.RECOVERABLE,
    DesktopErrorType.TOOL_UNAVAILABLE: ErrorSeverity.UNAVAILABLE,
}


@dataclass(frozen=True)
class DesktopError:
    error_type: DesktopErrorType
    detail: str = ""
    tool: str = ""
    window: str = ""
    recovery_action: str = ""
    severity: ErrorSeverity = ErrorSeverity.RECOVERABLE

    def tag(self) -> str:
        return error_tag(self.error_type)

    @property
    def is_recoverable(self) -> bool:
        return self.severity == ErrorSeverity.RECOVERABLE

    def summary(self) -> str:
        parts = [self.error_type.value]
        if self.detail:
            parts.append(self.detail)
        if self.window:
            parts.append(f"window={self.window[:60]}")
        return " · ".join(parts)


def _severity_for(error_type: DesktopErrorType) -> ErrorSeverity:
    return _DEFAULT_SEVERITY.get(error_type, ErrorSeverity.RECOVERABLE)


def _make(
    error_type: DesktopErrorType,
    *,
    detail: str = "",
    tool: str = "",
    window: str = "",
    recovery_action: str = "",
    severity: ErrorSeverity | None = None,
) -> DesktopError:
    sev = severity if severity is not None else _severity_for(error_type)
    return DesktopError(
        error_type=error_type,
        detail=detail,
        tool=tool,
        window=window,
        recovery_action=recovery_action or recovery_action_for(error_type),
        severity=sev,
    )


def error_tag(error_type: DesktopErrorType | str) -> str:
    val = error_type.value if isinstance(error_type, DesktopErrorType) else str(error_type)
    return f"\n[desktop_error:{val}]"


def tagged_message(message: str, err: DesktopError) -> str:
    """Append machine-readable tag without altering the human message body."""
    msg = (message or "").rstrip()
    tag = err.tag().strip()
    if tag and tag not in msg:
        return f"{msg}\n{tag}"
    return msg


def parse_error_tag(text: str) -> Optional[DesktopErrorType]:
    """Extract the last desktop_error tag from tool output."""
    if not text:
        return None
    found = _ERROR_TAG_RE.findall(text)
    if not found:
        return None
    try:
        return DesktopErrorType(found[-1])
    except ValueError:
        return None
def from_lock_reason(
    reason: str,
    *,
    window: str = "",
    query: str = "",
    fallback: str = "",
) -> DesktopError:
    """Map _locked_target_alive / focus-loss reason strings to typed errors."""
    r = (reason or "").strip().lower()
    if r == "window_closed":
        et = DesktopErrorType.WINDOW_CLOSED
    elif r in ("handle_invalid", "no_handle"):
        et = DesktopErrorType.STALE_HANDLE
    else:
        et = DesktopErrorType.FOCUS_LOST
    detail = r or "lock_lost"
    if fallback:
        detail = f"{detail}; fallback={fallback[:40]}"
    return _make(
        et,
        detail=detail,
        window=window,
        tool="computer",
        recovery_action=RecoveryAction.REFOCUS.value,
    )


def from_control_count(
    count: int,
    min_controls: int,
    *,
    tool: str = "computer",
    window: str = "",
) -> DesktopError:
    if int(count or 0) == 0:
        return _make(
            DesktopErrorType.EMPTY_TREE,
            detail="no actionable controls in UIA tree",
            tool=tool,
            window=window,
            recovery_action=RecoveryAction.REREAD.value,
        )
    return _make(
        DesktopErrorType.LOW_YIELD,
        detail=f"{count} controls < threshold {min_controls}",
        tool=tool,
        window=window,
        recovery_action=RecoveryAction.REREAD.value,
    )


def elevated_target_error(
    *,
    window: str = "",
    pid: int = 0,
    tool: str = "computer",
) -> DesktopError:
    """Target window's process is elevated; un-elevated VARIANT-1 is UIPI-blocked."""
    detail = "elevated process — UIA tree invisible and input discarded (UIPI)"
    if pid:
        detail += f" [pid {pid}]"
    return _make(
        DesktopErrorType.ELEVATED_TARGET,
        detail=detail,
        tool=tool,
        window=window,
        recovery_action=RecoveryAction.USER_ESCALATE.value,
        severity=ErrorSeverity.UNAVAILABLE,
    )


def capture_failed_error(
    reason: str,
    *,
    window: str = "",
    tool: str = "computer",
) -> DesktopError:
    return _make(
        DesktopErrorType.CAPTURE_FAILED,
        detail=reason or "crop_unavailable",
        tool=tool,
        window=window,
        recovery_action=RecoveryAction.REREAD.value,
    )


def tool_unavailable_error(
    detail: str,
    *,
    tool: str = "",
) -> DesktopError:
    return _make(
        DesktopErrorType.TOOL_UNAVAILABLE,
        detail=detail[:200],
        tool=tool,
        recovery_action=RecoveryAction.USER_ESCALATE.value,
        severity=ErrorSeverity.UNAVAILABLE,
    )


def recovery_action_for(error_type: DesktopErrorType) -> str:
    mapping = {
        DesktopErrorType.EMPTY_TREE: RecoveryAction.REREAD,
        DesktopErrorType.LOW_YIELD: RecoveryAction.REREAD,
        DesktopErrorType.STALE_HANDLE: RecoveryAction.REFOCUS,
        DesktopErrorType.HANDLE_INVALID: RecoveryAction.REFOCUS,
        DesktopErrorType.WINDOW_CLOSED: RecoveryAction.REFOCUS,
        DesktopErrorType.FOCUS_LOST: RecoveryAction.REFOCUS,
        DesktopErrorType.ELEVATED_TARGET: RecoveryAction.USER_ESCALATE,
        DesktopErrorType.MODAL_BLOCKING: RecoveryAction.INTERACT_MODAL,
        DesktopErrorType.CAPTURE_FAILED: RecoveryAction.REREAD,
        DesktopErrorType.TOOL_UNAVAILABLE: RecoveryAction.USER_ESCALATE,
    }
    return mapping.get(error_type, RecoveryAction.REREAD).value


def modal_blocking_error(
    *,
    modal_type: str = "unknown",
    title: str = "",
    detail: str = "",
    tool: str = "computer",
    window: str = "",
) -> DesktopError:
    blob = detail or title or modal_type or "dialog blocking target"
    return _make(
        DesktopErrorType.MODAL_BLOCKING,
        detail=f"{modal_type}: {blob}"[:120],
        tool=tool,
        window=window or title,
        recovery_action=RecoveryAction.INTERACT_MODAL.value,
    )


def error_event_text(err: DesktopError) -> str:
    action = err.recovery_action or recovery_action_for(err.error_type)
    sev = err.severity.value if err.severity else _severity_for(err.error_type).value
    return (
        f"{err.error_type.value} ({sev}): "
        f"{err.detail or err.error_type.value} → {action}"
    )


def activity_fields(err: DesktopError) -> dict:
    default_sev = _severity_for(err.error_type)
    sev = err.severity if err.severity != ErrorSeverity.RECOVERABLE else default_sev
    if err.error_type in _DEFAULT_SEVERITY and err.severity == ErrorSeverity.RECOVERABLE:
        sev = _DEFAULT_SEVERITY[err.error_type]
    return {
        "error_type": err.error_type.value,
        "detail": (err.detail or "")[:200],
        "tool": err.tool or "",
        "window": (err.window or "")[:120],
        "recovery_action": err.recovery_action or recovery_action_for(err.error_type),
        "severity": sev.value,
        "is_recoverable": sev == ErrorSeverity.RECOVERABLE,
        "text": error_event_text(err),
    }
