"""UIA worker batch runner with pending-queue reset and signal flush."""

from __future__ import annotations

from typing import Any, Callable

from . import session as dsession
from . import uia_worker as duia


def _clear_pending(sess: dsession.DesktopSessionState) -> None:
    sess.pending_focus_loss = None
    sess.pending_desktop_errors = []
    sess.pending_modal_events = []
    sess.pending_stack_events = []
    sess.mark_updated()


async def flush_pending_desktop_signals(ctx: Any) -> None:
    await ctx._runtime.flush_pending_desktop_signals(ctx)


async def run_uia_batch(ctx: Any, fn: Callable[[], Any]) -> Any:
    sess = ctx.session
    _clear_pending(sess)

    def _on_thread_start():
        dsession.mark_uia_thread(sess)

    return await duia.run(
        fn,
        on_thread_start=_on_thread_start,
        after_batch=lambda: flush_pending_desktop_signals(ctx),
    )
