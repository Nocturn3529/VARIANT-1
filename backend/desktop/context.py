"""Desktop control execution context for decomposed desktop/ modules.

Submodules receive a ``DesktopControlContext`` with session-scoped state on
``ctx.session``. Process-global injectables (guards and configs) live
only on ``DesktopRuntime`` — not dual-written through a host module.
"""

from __future__ import annotations

import desktop.errors as derr
import desktop.perception_recovery as prec
import tools
from . import vision_capture as vc

from . import action_resolve as daction
from . import control_helpers as chelp
from . import modals as dmodals
from . import tree as dtree
from . import vision_bridge as dvision
from . import service as dservice
from . import targeting as dtarget
from . import uia_batch
from . import uia_worker as duia
from . import constants as dconst
from .runtime import get_runtime
from .session import DesktopSessionState


class DesktopControlContext:
    """Session-backed context passed into desktop/ perception and action helpers."""

    ACTIONABLE_ROLES = dconst.ACTIONABLE_ROLES
    READABLE_ROLES = dconst.READABLE_ROLES
    MAX_READABLE_CONTROLS = dconst.MAX_READABLE_CONTROLS
    MAX_CONTROLS = dconst.MAX_CONTROLS
    MAX_DEPTH = dconst.MAX_DEPTH
    _LABEL_DERIVE_ROLES = dconst._LABEL_DERIVE_ROLES

    def __init__(self, session: DesktopSessionState) -> None:
        self.session = session
        self._runtime = get_runtime()

    # ---- injectables (runtime only) -----------------------------------------

    def perception_config(self):
        return self._runtime.session_scoped_config(
            self.session, "perception_cfg", self._runtime.perception_cfg,
        )

    def recovery_config(self):
        return self._runtime.session_scoped_config(
            self.session, "recovery_cfg", self._runtime.recovery_cfg,
        )

    def observability_config(self):
        return self._runtime.session_scoped_config(
            self.session, "quality_cfg", self._runtime.quality_cfg,
        )

    # ---- runtime / activity -------------------------------------------------

    async def _emit_perception(self, event: str, **fields) -> None:
        await self._runtime.emit_perception(event, **fields)

    async def _emit_perception_quality_metrics(self, **kwargs) -> None:
        await self._runtime.emit_perception_quality_metrics(self, **kwargs)

    async def _emit_desktop_error(self, err: derr.DesktopError) -> None:
        await self._runtime.emit_desktop_error(err)

    def _tagged_tool_error(self, msg: str, err: derr.DesktopError) -> tools.ToolError:
        return self._runtime.tagged_tool_error(msg, err)

    async def _emit_capture_scope(self, meta: vc.CaptureMeta) -> None:
        await self._runtime.emit_capture_scope(meta)

    def _prompt_cap_now(self) -> int:
        return chelp.prompt_cap_now(self._runtime, prompt_cap=self._runtime.prompt_cap)

    # ---- UIA ----------------------------------------------------------------

    def _load_uia(self):
        return duia.load_uia()

    async def _uia(self, fn):
        return await uia_batch.run_uia_batch(self, fn)

    # ---- formatting / ids ---------------------------------------------------

    def _stable_id(self, key: str) -> int:
        return chelp.stable_id(self.session, key)

    def _stable_state_id(self, key: str) -> int:
        return chelp.stable_state_id(self.session, key)

    def _role(self, control_type_name: str) -> str:
        return chelp.role(control_type_name)

    def _derive_label(self, auto, control, max_nodes=30, max_chars=80) -> str:
        return chelp.derive_label(auto, control, max_nodes=max_nodes, max_chars=max_chars)

    def _parse_max_controls(self, args) -> int:
        return chelp.parse_max_controls(args, default=self.MAX_CONTROLS)

    def _window_title(self, win) -> str:
        return chelp.window_title(win)

    def _is_skippable(self, name, classname, pid) -> bool:
        return chelp.is_skippable(name, classname, pid)

    def _target_top(self, auto):
        return chelp.target_top(auto)

    def format_controls(self, controls: list, window_title: str = "", cap: int | None = None) -> str:
        return chelp.format_controls(
            controls,
            window_title,
            cap,
            prompt_cap_fn=self._prompt_cap_now,
        )

    def _deliver_image(self, b64: str) -> None:
        dservice.deliver_image(
            b64,
            media_type="image/png",
            producer="computer",
        )

    def _append_focus_warning(self, text: str) -> str:
        warn = dservice.take_focus_loss_warning(self.session)
        if warn:
            text += warn
        for err in self.session.recent_desktop_errors:
            text += err.tag()
        self.session.recent_desktop_errors = []
        self.session.mark_updated()
        return text

    def _note_result(self, _ok: bool) -> None:
        """Record that a desktop call completed without altering its outcome."""
        self.session.mark_updated()

    # ---- perception ---------------------------------------------------------

    def _collect_controls(self, auto, max_controls=None, max_depth=None, top=None):
        return dtree.collect_controls(
            self, auto, max_controls=max_controls, max_depth=max_depth, top=top,
        )

    def _store_snapshot(self, title, controls):
        dtree.store_snapshot(self, title, controls)

    def _observe_modal_state(self, auto):
        dmodals.observe_modal_state(self, auto)

    async def _format_perception_output(self, title, controls, args, **kwargs) -> str:
        return await dtree.format_perception_output(self, title, controls, args, **kwargs)

    async def _uia_collect(self, max_controls=None):
        max_controls = max_controls or self.MAX_CONTROLS

        def _walk():
            auto = self._load_uia()
            return self._collect_controls(auto, max_controls=max_controls)

        return await self._uia(_walk)

    def _sig(self, rec) -> tuple:
        return dtree.sig(self, rec)

    def _ctrl_desc(self, c) -> str:
        return dtree.ctrl_desc(self, c)

    def _locked_target_alive(self, win) -> tuple[bool, str]:
        return dtarget.locked_target_alive(win)

    def _vision_capture_on_uia_thread(self, prefer_window: bool = True):
        return dvision.vision_capture_on_uia_thread(self, prefer_window)

    async def _vision_capture(self, prefer_window: bool = True):
        return await dvision.vision_capture(self, prefer_window)

    async def _attach_visual_state(self, *, tool: str, controls: int = 0) -> str:
        return await dvision.attach_visual_state(
            self, tool=tool, controls=controls,
        )

    # ---- targeting ----------------------------------------------------------

    def _resolve_target(self, auto):
        return dtarget.resolve_target(self, auto)

    def _resolve_action_target(self, auto, *, action: str = ""):
        return dtarget.resolve_action_target(self, auto, action=action)

    def _record_focus_loss(self, auto, reason: str) -> None:
        sess = self.session
        if sess.pending_focus_loss is not None:
            return
        last_title = sess.target_meta.get("title") or sess.snapshot_title or "(unknown)"
        query = sess.target_meta.get("query") or ""
        fallback = self._target_top(auto)
        fallback_title = self._window_title(fallback)
        sess.pending_focus_loss = prec.focus_loss_activity_fields(
            last_title=last_title,
            reason=reason,
            fallback_title=fallback_title,
            query=query,
        )
        sess.focus_loss_warning = prec.focus_loss_warning_text(
            last_title, reason, fallback_title, query=query,
        )
        dservice.queue_desktop_error(sess, derr.from_lock_reason(
            reason,
            window=last_title,
            query=query,
            fallback=fallback_title,
        ))
        sess.window_stack.invalidate_current_control()
        sess.target_window = None
        sess.mark_updated()

    async def refocus_locked_window(self) -> tuple[bool, str]:
        return await dtarget.refocus_locked_window(self)

    async def _refocus_locked_target(self) -> bool:
        return await dtarget.refocus_locked_target(self)

    def set_target(self, win, *, title: str = "", query: str = "", push: bool = False):
        dtarget.set_target(self.session, win, title=title, query=query, push=push)
        dtree.invalidate_incremental(self.session)

    def pop_window_context(self):
        popped, current = dtarget.pop_window_context(self.session)
        if popped is not None:
            dtree.invalidate_incremental(self.session)
        return popped, current

    def promote_window_context(self, index: int):
        ctx = dtarget.promote_window_context(self.session, index)
        if ctx is not None:
            dtree.invalidate_incremental(self.session)
        return ctx

    # ---- actions ------------------------------------------------------------

    def _ensure_gates(self):
        return daction._ensure_gates(self)
