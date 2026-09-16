"""Desktop runtime glue: activity emission, config, and error helpers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

import tools
from . import vision_capture as vc

from . import errors as derr
from . import modal_detect as mdetect
from . import perception_delta as pdelta
from . import perception_quality as pqual
from . import perception_recovery as prec
from . import service as dservice
from .session import DesktopSessionState, current_desktop_session


class DesktopRuntime:
    """One explicitly composed desktop runtime's injected configuration."""

    def __init__(self) -> None:
        self.activity_emitter: Callable | None = None
        self.ctx_getter: Callable | None = None
        self.recovery_cfg = prec.RecoveryConfig()
        self.perception_cfg = pdelta.PerceptionConfig()
        self.quality_cfg = pqual.QualityMetricsConfig()
        self.prompt_cap = 100

    def session_scoped_config(self, sess: DesktopSessionState, cached_attr: str, current_default):
        cached = getattr(sess, cached_attr)
        if cached is None:
            cached = current_default
            setattr(sess, cached_attr, cached)
        return cached

    def observability_config(self) -> pqual.QualityMetricsConfig:
        sess = current_desktop_session()
        return self.session_scoped_config(sess, "quality_cfg", self.quality_cfg)

    def recovery_config(self) -> prec.RecoveryConfig:
        sess = current_desktop_session()
        return self.session_scoped_config(sess, "recovery_cfg", self.recovery_cfg)

    def perception_config(self) -> pdelta.PerceptionConfig:
        sess = current_desktop_session()
        return self.session_scoped_config(sess, "perception_cfg", self.perception_cfg)

    def prompt_cap_now(self) -> int:
        try:
            if self.ctx_getter:
                ctx = int(self.ctx_getter() or 0)
                if ctx and ctx < 10000:
                    return 50
                if ctx and ctx < 20000:
                    return 80
                return 120
        except Exception:
            pass
        return self.prompt_cap

    async def emit_perception(self, event: str, **fields) -> None:
        if not self.activity_emitter:
            return
        try:
            driver_state_id = dservice.current_driver_state_id()
            if driver_state_id:
                fields.setdefault("desktop_driver_state_id", driver_state_id)
            result = self.activity_emitter(event, **fields)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    async def emit_desktop_error(self, err: derr.DesktopError) -> None:
        await self.emit_perception("perception:error", **derr.activity_fields(err))

    def tagged_tool_error(self, msg: str, err: derr.DesktopError) -> tools.ToolError:
        return tools.ToolError(derr.tagged_message(msg, err))

    async def emit_perception_quality_metrics(
        self,
        desktop_ctx: Any,
        *,
        tool: str = "",
        window: str = "",
        step: str = "",
        controls: int = 0,
        reread: bool = False,
        recovered: bool = False,
        perception_mode: str = "",
        incremental_savings_pct: int | None = None,
        incremental_changes: int | None = None,
        error: derr.DesktopError | None = None,
        include_capture: bool = False,
        include_modal: bool = False,
        include_stack: bool = False,
    ) -> None:
        qcfg = self.observability_config()
        if not pqual.should_emit(qcfg):
            return

        step_inc = desktop_ctx.session.step_incremental
        mode = perception_mode or step_inc.get("perception_mode") or ""
        savings = (
            incremental_savings_pct
            if incremental_savings_pct is not None
            else step_inc.get("incremental_savings_pct")
        )
        changes = (
            incremental_changes
            if incremental_changes is not None
            else step_inc.get("incremental_changes")
        )

        metrics = pqual.PerceptionQualityInput(
            tool=tool,
            window=window,
            step=step,
            controls=int(controls or 0),
            yield_status=pqual.infer_yield_status(
                int(controls or 0),
                min_controls=self.recovery_config().min_controls_for_uia,
                recovered=recovered,
            ),
            reread=reread,
            perception_mode=mode,
            incremental_savings_pct=savings,
            incremental_changes=changes,
        )
        if include_capture:
            pqual.enrich_from_capture_meta(metrics, desktop_ctx.session.last_vision_meta)
        if error is not None:
            pqual.enrich_from_error(metrics, error)
        elif desktop_ctx.session.recent_desktop_errors:
            pqual.enrich_from_error(metrics, desktop_ctx.session.recent_desktop_errors[-1])
        if include_modal:
            modal = desktop_ctx.session.current_modal
            metrics.modal_blocking = bool(modal and modal.present and modal.blocking)
        if include_stack:
            metrics.stack_depth = desktop_ctx.session.window_stack.depth()

        await self.emit_perception(
            "perception:quality_metrics",
            **pqual.build_payload(metrics, verbosity=qcfg.verbosity),
        )

    async def flush_pending_desktop_signals(self, desktop_ctx: Any) -> None:
        sess = desktop_ctx.session
        pending_fl = sess.pending_focus_loss
        if pending_fl:
            sess.pending_focus_loss = None
            sess.mark_updated()
            await self.emit_perception("perception:focus_lost", **pending_fl)

        modal_events = list(sess.pending_modal_events)
        sess.pending_modal_events = []
        for kind, snap in modal_events:
            if kind == "detected":
                await self.emit_perception(
                    "perception:modal_detected",
                    **mdetect.detected_activity_fields(snap),
                )
            elif kind == "dismissed":
                await self.emit_perception(
                    "perception:modal_dismissed",
                    **mdetect.dismissed_activity_fields(snap),
                )

        stack_events = list(sess.pending_stack_events)
        sess.pending_stack_events = []
        for event, fields in stack_events:
            await self.emit_perception(event, **fields)

        errors = list(sess.pending_desktop_errors)
        sess.pending_desktop_errors = []
        sess.recent_desktop_errors = errors
        sess.mark_updated()
        for err in errors:
            await self.emit_desktop_error(err)

    async def emit_capture_scope(self, meta: vc.CaptureMeta) -> None:
        await self.emit_perception(
            "perception:capture_scope",
            text=vc.capture_event_text(meta),
            mode=meta.mode,
            fallback=meta.fallback_reason or "",
            window=(meta.window_title or "")[:120],
            origin_x=meta.origin[0],
            origin_y=meta.origin[1],
            width=meta.width,
            height=meta.height,
            dpi_scale=round(meta.dpi_scale, 3),
        )
        if meta.mode == "monitor" and meta.fallback_reason and meta.fallback_reason != "no_lock":
            await self.emit_desktop_error(derr.capture_failed_error(
                meta.fallback_reason,
                window=meta.window_title,
                tool="computer",
            ))

# Direct invocation helpers need a deterministic fallback outside an AppHost
# tool call. It is deliberately private: production configuration is available
# only through a DesktopControl instance.
_DEFAULT_RUNTIME = DesktopRuntime()
_BOUND_RUNTIME: ContextVar[DesktopRuntime | None] = ContextVar(
    "variant1_desktop_runtime", default=None)


def get_runtime() -> DesktopRuntime:
    """Return the invocation-bound runtime, or the standalone default."""
    return _BOUND_RUNTIME.get() or _DEFAULT_RUNTIME


@contextmanager
def bind_runtime(runtime: DesktopRuntime):
    """Bind one explicit desktop runtime for the current async invocation."""
    token = _BOUND_RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _BOUND_RUNTIME.reset(token)
