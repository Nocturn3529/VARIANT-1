"""Modal state helpers for session-scoped desktop runs."""

from __future__ import annotations

import desktop.modal_detect as mdetect

from typing import Any

from . import service as dservice
from .tree import invalidate_incremental


def modal_context_note(ctx: Any) -> str:
    """Model-facing note appended by ordinary focus/perception output."""
    modal = ctx.session.current_modal
    if not (modal and modal.present):
        return ""
    return "\n\nDialog detected automatically:\n" + mdetect.format_modal_summary(modal)


def observe_modal_state(ctx: Any, auto) -> None:
    """Detect modal transitions while a target is locked. UIA worker thread.

    Modal detection itself lives in modal_detect.scan_modal_uia(); this function
    owns only lifecycle state and transition side effects.
    """
    sess = ctx.session
    locked = sess.target_window
    locked_title = sess.target_meta.get("title") or sess.snapshot_title or ""

    if locked is None:
        if sess.current_modal is not None and sess.current_modal.present:
            sess.pending_modal_events.append(("dismissed", sess.current_modal))
            invalidate_incremental(sess)
        sess.current_modal = None
        sess.modal_signature = ""
        sess.mark_updated()
        return

    snap = mdetect.scan_modal_uia(auto, locked, locked_title)
    if snap is None or not snap.present:
        if sess.current_modal is not None and sess.current_modal.present:
            sess.pending_modal_events.append(("dismissed", sess.current_modal))
            invalidate_incremental(sess)
        sess.current_modal = None
        sess.modal_signature = ""
        sess.mark_updated()
        return

    sig = snap.signature()
    if sig == sess.modal_signature:
        sess.current_modal = snap
        sess.mark_updated()
        return

    sess.modal_signature = sig
    sess.current_modal = snap
    sess.pending_modal_events.append(("detected", snap))
    invalidate_incremental(sess)
    if snap.blocking:
        dservice.queue_desktop_error(sess, mdetect.modal_blocking_error(snap))
    sess.mark_updated()
