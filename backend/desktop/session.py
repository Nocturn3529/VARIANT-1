"""Explicitly bound scratch state for VARIANT-1's legacy Windows UIA driver.

This module is not a desktop/session authority. Desktop Fabric owns durable
window, observation, capture, operation, event, and run-binding identity. The
state below may contain live COM/UIA handles and therefore exists only inside a
``WindowsDesktopAdapter`` call. It has no default instance, registry, child
session mechanism, or checkpoint representation.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, TYPE_CHECKING

from . import vision_capture as vc
from . import window_context as wctx

if TYPE_CHECKING:
    from . import perception_delta as pdelta
    from . import perception_quality as pqual
    from . import perception_recovery as prec


def _new_session_id() -> str:
    return "desktop_driver_" + uuid.uuid4().hex[:12]


def _window_context_to_dict(ctx: wctx.WindowContext | None) -> Dict[str, Any]:
    if ctx is None:
        return {}
    return {
        "hwnd": int(getattr(ctx, "hwnd", 0) or 0),
        "title": str(getattr(ctx, "title", "") or ""),
        "query": str(getattr(ctx, "query", "") or ""),
        "metadata": dict(getattr(ctx, "metadata", {}) or {}),
    }


@dataclass
class DesktopSessionState:
    """Live-only UIA/perception scratch for one durable Fabric window."""

    session_id: str = field(default_factory=_new_session_id)
    last_snapshot: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    snapshot_title: str = ""
    snapshot_taken_at: float = 0.0
    id_registry: Dict[str, int] = field(default_factory=dict)
    next_id: int = 0
    target_window: Any = None
    target_meta: Dict[str, Any] = field(
        default_factory=lambda: {"title": "", "query": ""}
    )
    window_stack: wctx.WindowContextStack = field(
        default_factory=wctx.WindowContextStack
    )
    pending_stack_events: List[Any] = field(default_factory=list)
    pending_focus_loss: Any = None
    focus_loss_warning: str = ""
    current_modal: Any = None
    modal_signature: str = ""
    pending_modal_events: List[Any] = field(default_factory=list)
    pending_desktop_errors: List[Any] = field(default_factory=list)
    recent_desktop_errors: List[Any] = field(default_factory=list)
    incremental_baseline: Any = None
    incremental_force_full: bool = False
    step_incremental: Dict[str, Any] = field(default_factory=dict)
    last_vision_meta: vc.CaptureMeta = field(default_factory=vc.CaptureMeta)
    recovery_state: Dict[str, Any] = field(default_factory=dict)
    rehydrate_state: Dict[str, Any] = field(default_factory=dict)
    # Adapter-local acquisition hints only; never a run/work authorization.
    scope: Dict[str, Any] = field(default_factory=dict)
    uia_thread_id: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    recovery_cfg: "prec.RecoveryConfig | None" = None
    perception_cfg: "pdelta.PerceptionConfig | None" = None
    quality_cfg: "pqual.QualityMetricsConfig | None" = None

    @property
    def active_window(self) -> Dict[str, Any]:
        return _window_context_to_dict(self.window_stack.get_current())

    def mark_updated(self) -> None:
        self.updated_at = time.time()

    def assert_uia_thread(self) -> None:
        if self.uia_thread_id is None:
            return
        if threading.get_ident() != self.uia_thread_id:
            raise RuntimeError(
                "desktop UIA state accessed outside the dedicated UIA worker thread"
            )


CURRENT_DESKTOP_SESSION: contextvars.ContextVar[DesktopSessionState | None] = (
    contextvars.ContextVar("variant1_current_desktop_driver_state", default=None)
)


def current_desktop_session() -> DesktopSessionState:
    current = CURRENT_DESKTOP_SESSION.get()
    if current is None:
        raise RuntimeError(
            "desktop driver state is unbound; use Desktop Fabric/WindowsDesktopAdapter"
        )
    return current


@contextmanager
def bind_desktop_session(
    session: DesktopSessionState,
) -> Iterator[DesktopSessionState]:
    token = CURRENT_DESKTOP_SESSION.set(session)
    try:
        yield session
    finally:
        CURRENT_DESKTOP_SESSION.reset(token)


def mark_uia_thread(session: DesktopSessionState | None = None) -> int:
    """Record the dedicated UIA worker thread for bound driver scratch."""
    thread_id = threading.get_ident()
    target = session or current_desktop_session()
    target.uia_thread_id = thread_id
    return thread_id


__all__ = [
    "CURRENT_DESKTOP_SESSION",
    "DesktopSessionState",
    "bind_desktop_session",
    "current_desktop_session",
    "mark_uia_thread",
]
