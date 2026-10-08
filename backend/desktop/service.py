"""Desktop session helpers for the legacy Windows driver.

``DesktopSessionState`` is the sole mutable desktop state container. Image
delivery lives in ``tool_images``.
"""

from __future__ import annotations

from typing import Any

from tool_images import deliver_image
from .session import (
    DesktopSessionState,
    current_desktop_session,
)

__all__ = [
    "current_driver_state_id",
    "deliver_image",
    "queue_desktop_error",
    "take_focus_loss_warning",
]


def current_driver_state_id() -> str:
    try:
        return current_desktop_session().session_id
    except Exception:
        return ""


def queue_desktop_error(session: DesktopSessionState, err: Any) -> None:
    session.pending_desktop_errors.append(err)
    session.mark_updated()


def take_focus_loss_warning(session: DesktopSessionState) -> str:
    warning = session.focus_loss_warning or ""
    session.focus_loss_warning = ""
    session.mark_updated()
    return warning
