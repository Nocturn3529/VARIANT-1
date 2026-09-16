"""VARIANT-1's durable Windows Desktop Fabric backend foundation.

Importing this package performs no desktop scan, UIA call, capture, or input.
Use :func:`create_desktop_fabric` for explicit composition.
"""

from __future__ import annotations

from .adapter import (
    AdapterCapture,
    AdapterDispatch,
    AdapterFocus,
    AdapterObservation,
    DesktopLiveAdapter,
    WindowsDesktopAdapter,
)
from .access import (
    bind_desktop_fabric,
    current_desktop_fabric,
    current_desktop_host,
    install_desktop_fabric,
    uninstall_desktop_fabric,
)
from .binding import (
    DesktopBinding,
    bind_desktop_binding,
    create_child_desktop_binding_snapshot,
    current_desktop_binding,
    desktop_binding_snapshot,
    ensure_desktop_binding,
)
from .models import (
    AppRecord,
    DesktopAmbiguousTarget,
    DesktopCapture,
    DesktopConflict,
    DesktopElement,
    DesktopEvent,
    DesktopFabricError,
    DesktopNotFound,
    DesktopObservation,
    DesktopOperation,
    DesktopScopeMismatch,
    DesktopStaleReference,
    DesktopUnavailable,
    DesktopValidationError,
    ProcessIdentity,
    WindowRecord,
)
from .repository import DesktopFabricRepository
from .service import DesktopFabric, DesktopRecoveryReport


def create_desktop_fabric(**kwargs):
    from .service import create_desktop_fabric as create
    return create(**kwargs)


__all__ = [
    "AdapterCapture", "AdapterDispatch", "AdapterFocus", "AdapterObservation",
    "AppRecord",
    "bind_desktop_binding", "bind_desktop_fabric",
    "current_desktop_binding", "current_desktop_fabric", "current_desktop_host",
    "DesktopAmbiguousTarget", "DesktopCapture", "DesktopConflict",
    "DesktopBinding", "DesktopElement", "DesktopEvent", "DesktopFabric", "DesktopFabricError",
    "DesktopFabricRepository", "DesktopLiveAdapter", "DesktopNotFound",
    "DesktopObservation", "DesktopOperation", "DesktopRecoveryReport",
    "DesktopScopeMismatch",
    "DesktopStaleReference", "DesktopUnavailable", "DesktopValidationError",
    "WindowsDesktopAdapter", "ProcessIdentity", "WindowRecord",
    "create_desktop_fabric",
    "create_child_desktop_binding_snapshot", "desktop_binding_snapshot",
    "ensure_desktop_binding",
    "install_desktop_fabric", "uninstall_desktop_fabric",
]
