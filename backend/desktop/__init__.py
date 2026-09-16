"""Internal Windows UIA, vision, and input drivers for Desktop Fabric.

The public seed API lives in ``desktop.registry`` and always enters Desktop
Fabric. This package owns only explicitly bound live driver scratch and raw
OS primitives; none of it is checkpoint or model-visible state.
"""

from .session import (
    CURRENT_DESKTOP_SESSION,
    DesktopSessionState,
    bind_desktop_session,
    current_desktop_session,
    mark_uia_thread,
)

__all__ = [
    "CURRENT_DESKTOP_SESSION",
    "DesktopSessionState",
    "bind_desktop_session",
    "current_desktop_session",
    "mark_uia_thread",
]
