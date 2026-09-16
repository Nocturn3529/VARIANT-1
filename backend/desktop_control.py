"""Private Windows driver facade used only by Desktop Fabric.

Implementation lives in ``backend/desktop``. This module intentionally exposes
``DesktopControl`` is the sole exported object. Its methods are driver calls,
not model-facing tools; original seeds route through Desktop Fabric first.
"""

from __future__ import annotations

import functools
import platform

import desktop.context as dcontext
import desktop.modals as dmodals
import desktop.perception_delta as pdelta
import desktop.perception_flow as dflow
import desktop.perception_quality as pqual
import desktop.perception_recovery as prec
import desktop.registry as dregistry
import desktop.runtime as druntime
import desktop.service as dservice
import desktop.session as dsession
import desktop.targeting as dtarget
import desktop.tree as dtree
import desktop.uia_batch as duia_batch
import desktop.uia_worker as duia
import desktop.vision_bridge as dvision
import tools

__all__ = ["DesktopControl"]

def _control_context() -> dcontext.DesktopControlContext:
    return dcontext.DesktopControlContext(dsession.current_desktop_session())


async def _rehydrate_bound_session(sess: dsession.DesktopSessionState) -> None:
    ctx = dcontext.DesktopControlContext(sess)
    runtime = druntime.get_runtime()

    async def _run_uia(fn):
        return await duia_batch.run_uia_batch(ctx, fn)

    def _bind_target(win, title: str, query: str) -> None:
        if sess.window_stack.is_empty():
            dtarget.set_target(sess, win, title=title, query=query)
        else:
            dtarget.refocus_stack_top(ctx, win, title=title, query=query)
        dtree.invalidate_incremental(sess)

    async def _emit_fields(event: str, fields: dict) -> None:
        await runtime.emit_perception(
            event,
            desktop_driver_state_id=sess.session_id,
            **dict(fields or {}),
        )

    await dtarget.rehydrate_bound_session(
        sess,
        dtarget.RehydrateHooks(
            run_uia=_run_uia,
            load_uia=duia.load_uia,
            find_window=lambda auto, query: dtarget.find_window(ctx, auto, query),
            find_window_by_hwnd=lambda auto, hwnd: dtarget.find_window_by_hwnd(
                ctx, auto, hwnd,
            ),
            bind_restored_target=_bind_target,
            bring_to_front=dtarget.bring_to_front,
            save_state=lambda state: state,
            emit=lambda event: runtime.emit_perception(event),
            emit_fields=_emit_fields,
            log=lambda message: print(message, flush=True),
        ),
    )


def _session_bound_async(fn):
    @functools.wraps(fn)
    async def _wrapped(*args, **kwargs):
        await _rehydrate_bound_session(dsession.current_desktop_session())
        return await fn(*args, **kwargs)

    return _wrapped


# Registered tools -----------------------------------------------------------

async def _focus_window(args):
    args = dict(args or {})
    ctx = _control_context()
    if bool(args.get("previous")):
        return await dtarget.restore_previous_target(ctx, args)
    query = args.get("name") or args.get("title") or args.get("window")
    if query:
        return await dtarget.focus_window(ctx, args)
    if platform.system() != "Windows":
        raise tools.ToolError(
            "window focus is Windows-only."
        )
    _title, text, _controls = await dflow.perceive_ui(
        ctx, args, tool_name="computer.observe",
    )
    return text + dmodals.modal_context_note(ctx)


async def _capture_raw_b64_async() -> str | None:
    return await dvision.capture_raw_b64_async(_control_context())


_capture_raw_b64_async = _session_bound_async(_capture_raw_b64_async)
_focus_window = _session_bound_async(_focus_window)


class DesktopControl:
    """Explicit production desktop surface bound to one ``DesktopRuntime``.

    Registered handlers enter this surface, which binds its runtime through a
    ContextVar for the complete async tool invocation. Runtime configuration is
    intentionally instance-only.
    """

    def __init__(self, runtime: druntime.DesktopRuntime | None = None) -> None:
        self.runtime = runtime or druntime.DesktopRuntime()

    def set_ctx_getter(self, fn):
        self.runtime.ctx_getter = fn

    def set_recovery_config(self, raw: dict | None):
        self.runtime.recovery_cfg = prec.merge_recovery_config(raw)

    def set_perception_config(self, raw: dict | None):
        self.runtime.perception_cfg = pdelta.merge_perception_config(raw)

    def set_observability_config(self, raw: dict | None):
        self.runtime.quality_cfg = pqual.merge_observability_config(raw)

    def set_activity_emitter(self, fn):
        self.runtime.activity_emitter = fn

    async def _invoke(self, handler, args):
        with druntime.bind_runtime(self.runtime):
            return await handler(args)

    async def focus_window(self, args):
        return await self._invoke(_focus_window, args)

    async def capture_raw_b64_async(self) -> str | None:
        with druntime.bind_runtime(self.runtime):
            return await _capture_raw_b64_async()

    def register(self, registry):
        dregistry.register(registry, self)
