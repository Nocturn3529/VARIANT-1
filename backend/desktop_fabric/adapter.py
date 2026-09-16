"""Injectable live-desktop adapters; production wraps VARIANT-1's existing stack."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
from contextlib import contextmanager
from typing import Any, Mapping, Protocol, runtime_checkable

from .catalog import Win32DesktopCatalog
from .models import (
    AppRecord,
    DesktopElement,
    DesktopStaleReference,
    DesktopUnavailable,
    WindowRecord,
)


@dataclass(frozen=True, slots=True)
class AdapterObservation:
    uia: tuple[dict[str, Any], ...] = ()
    visual: tuple[dict[str, Any], ...] = ()
    ocr: tuple[dict[str, Any], ...] = ()
    uia_generation: int = 0
    completeness: str = "uia-only"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterCapture:
    png: bytes
    width: int
    height: int
    provenance: str
    occlusion_independent: bool = False
    minimized: bool = False
    stale: bool = False
    coordinate_transform: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterDispatch:
    delivered: bool
    method: str
    readback: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterFocus:
    result: Any
    hwnd: int
    observation: AdapterObservation


@runtime_checkable
class DesktopLiveAdapter(Protocol):
    def catalog(self, *, backend_instance_id: str) -> tuple[list[AppRecord], list[WindowRecord]]: ...
    def validate_window(self, window: WindowRecord) -> Mapping[str, Any]: ...
    async def observe(self, window: WindowRecord, *, mode: str) -> AdapterObservation: ...
    async def capture(self, window: WindowRecord) -> AdapterCapture: ...
    async def preflight(
        self, window: WindowRecord, *, action: str, delivery: str,
        arguments: Mapping[str, Any],
    ) -> None: ...
    async def dispatch(
        self, window: WindowRecord, *, action: str, delivery: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch: ...
    async def focus(self, window: WindowRecord) -> AdapterFocus: ...
    async def focus_session(self, arguments: Mapping[str, Any]) -> AdapterFocus: ...
    def capability_report(self) -> Mapping[str, Any]: ...
    def close(self) -> None: ...


def _patterns(control: Any, *, actionable: bool = False) -> list[str]:
    checks = {
        "invoke": "IsInvokePatternAvailable",
        "legacy_action": "IsLegacyIAccessiblePatternAvailable",
        "value": "IsValuePatternAvailable",
        "selection_item": "IsSelectionItemPatternAvailable",
        "toggle": "IsTogglePatternAvailable",
        "expand_collapse": "IsExpandCollapsePatternAvailable",
        "range_value": "IsRangeValuePatternAvailable",
        "scroll_item": "IsScrollItemPatternAvailable",
    }
    getters = {
        "invoke": "GetInvokePattern",
        "legacy_action": "GetLegacyIAccessiblePattern",
        "value": "GetValuePattern",
        "selection_item": "GetSelectionItemPattern",
        "toggle": "GetTogglePattern",
        "expand_collapse": "GetExpandCollapsePattern",
        "range_value": "GetRangeValuePattern",
        "scroll_item": "GetScrollItemPattern",
    }
    result: list[str] = []
    for name, attr in checks.items():
        advertised = None
        try:
            value = getattr(control, attr)
            advertised = bool(value() if callable(value) else value)
        except Exception:
            pass
        if advertised:
            result.append(name)
        # The installed uiautomation package exposes typed getters, but not
        # Is*PatternAvailable convenience methods. Missing metadata must not
        # hide working capabilities. Keep probes bounded to actionable rows;
        # retain the existing Chromium invoke/legacy false-flag workaround.
        elif actionable and (
            advertised is None or name in {"invoke", "legacy_action"}
        ):
            try:
                if getattr(control, getters[name])() is not None:
                    result.append(name)
            except Exception:
                pass
    return result


def _require_pattern(control: Any, getter: str, label: str) -> Any:
    """Resolve the actual pattern instead of relying on optional UIA flags."""
    try:
        pattern = getattr(control, getter)()
    except Exception as exc:
        raise DesktopUnavailable(f"control has no UIA {label} pattern") from exc
    if pattern is None:
        raise DesktopUnavailable(f"control has no UIA {label} pattern")
    return pattern


def _require_writable_pattern(pattern: Any, label: str) -> None:
    try:
        marker = getattr(pattern, "IsReadOnly", False)
        read_only = bool(marker() if callable(marker) else marker)
    except Exception:
        # If a provider cannot report this optional property, its SetValue
        # implementation remains the authority. Never retry failed input.
        read_only = False
    if read_only:
        raise DesktopUnavailable(f"control's UIA {label} pattern is read-only")


class WindowsDesktopAdapter:
    """Desktop Fabric's one Windows driver over STA UIA, MSS, and SendInput."""

    def __init__(self, desktop_control: Any | None = None, *, catalog: Any | None = None) -> None:
        if desktop_control is None:
            from desktop_control import DesktopControl
            desktop_control = DesktopControl()
        self.control = desktop_control
        self.win32 = catalog or Win32DesktopCatalog()
        self._sessions: dict[str, Any] = {}
        self._generations: dict[str, int] = {}

    def _session(self, window_id: str) -> Any:
        from desktop.session import DesktopSessionState
        session = self._sessions.get(window_id)
        if session is None:
            session = DesktopSessionState(session_id=f"fabric_{window_id}")
            self._sessions[window_id] = session
        return session

    @contextmanager
    def _bound(self, window_id: str):
        from desktop.runtime import bind_runtime
        from desktop.session import bind_desktop_session
        with bind_runtime(self.control.runtime):
            with bind_desktop_session(self._session(window_id)) as session:
                yield session

    def catalog(self, *, backend_instance_id: str) -> tuple[list[AppRecord], list[WindowRecord]]:
        return self.win32.scan(backend_instance_id=backend_instance_id)

    def validate_window(self, window: WindowRecord) -> Mapping[str, Any]:
        return self.win32.validate(window)

    def _assert_live(self, window: WindowRecord, *, foreground: bool = False) -> Mapping[str, Any]:
        result = dict(self.validate_window(window))
        if not result.get("live"):
            raise DesktopStaleReference(
                f"window {window.window_id} no longer has the recorded HWND/process identity")
        if foreground and not result.get("foreground"):
            raise DesktopStaleReference(
                f"window {window.window_id} is not the verified foreground target")
        return result

    @staticmethod
    def _session_hwnd(session: Any) -> int:
        try:
            return int((session.active_window or {}).get("hwnd") or 0)
        except (TypeError, ValueError):
            return 0

    def _assert_session_hwnd(
        self, window: WindowRecord, session: Any, *, operation: str,
    ) -> None:
        active_hwnd = self._session_hwnd(session)
        if active_hwnd != int(window.hwnd):
            raise DesktopStaleReference(
                f"UIA {operation} bound HWND {active_hwnd}, expected strong "
                f"HWND {window.hwnd}"
            )

    @staticmethod
    def _owned_hwnd(user32: Any, child_hwnd: int, parent_hwnd: int) -> bool:
        """Return whether ``child_hwnd`` is in ``parent_hwnd``'s owner chain."""

        child = int(child_hwnd or 0)
        parent = int(parent_hwnd or 0)
        if not child or not parent or child == parent:
            return False
        seen: set[int] = set()
        current = child
        GW_OWNER = 4
        for _ in range(16):
            if current in seen:
                break
            seen.add(current)
            try:
                current = int(user32.GetWindow(current, GW_OWNER) or 0)
            except Exception:
                return False
            if not current:
                return False
            if current == parent:
                return True
        return False

    def _prime_exact(self, window: WindowRecord) -> Any:
        self._assert_live(window)
        with self._bound(window.window_id) as session:
            # Seed driver scratch with the durable HWND before invoking its
            # rehydration path. This makes HWND the primary lookup; title is
            # descriptive fallback only. Post-validation still fences reuse.
            from desktop.window_context import WindowContext
            session.window_stack.clear()
            session.target_window = None
            session.rehydrate_state = {}
            session.window_stack.push(WindowContext(
                hwnd=window.hwnd, title=window.title, query=window.title,
                metadata={
                    "desktop_fabric_window_id": window.window_id,
                    "pid": window.pid, "pid_started_at": window.pid_started_at,
                    "executable": window.executable,
                    "class_name": window.class_name,
                },
            ), ctrl=None)
            session.target_meta = {"title": window.title, "query": window.title}
            session.scope = {
                **dict(getattr(session, "scope", {}) or {}),
                "fabric_strict_hwnd": int(window.hwnd),
                "fabric_strict_pid": int(window.pid),
            }
            session.mark_updated()
            return session

    async def _bind_exact(
        self, window: WindowRecord, *, activate: bool,
    ) -> Any:
        """Re-acquire the durable HWND without title/foreground fallback."""

        self._prime_exact(window)
        with self._bound(window.window_id) as session:
            from desktop.context import DesktopControlContext
            from desktop import targeting as dtarget

            ctx = DesktopControlContext(session)

            def bind() -> None:
                auto = ctx._load_uia()
                target = dtarget.find_window_by_hwnd(
                    ctx, auto, int(window.hwnd)
                )
                if target is None:
                    raise DesktopStaleReference(
                        f"durable HWND {window.hwnd} could not be rebound in UIA"
                    )
                try:
                    target_pid = int(getattr(target, "ProcessId", 0) or 0)
                except Exception:
                    target_pid = 0
                if target_pid and target_pid != int(window.pid):
                    raise DesktopStaleReference(
                        f"durable HWND {window.hwnd} rebound to PID {target_pid}, "
                        f"expected {window.pid}"
                    )
                dtarget.refocus_stack_top(
                    ctx,
                    target,
                    title=str(getattr(target, "Name", "") or window.title),
                    query=window.title,
                )
                if activate:
                    dtarget.bring_to_front(target)

            await ctx._uia(bind)
            self._assert_session_hwnd(
                window, session, operation="rehydration"
            )
            self._assert_live(window)
            return session

    def post_action_target(self, window: WindowRecord) -> Mapping[str, Any]:
        """Describe the foreground reached by input without refocusing it.

        A same-process modal is an expected application transition, not focus
        theft. The service can bind that HWND for the returned fresh view.
        """

        state = dict(self.validate_window(window))
        foreground_hwnd = 0
        foreground_pid = 0
        try:
            import ctypes

            foreground_hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
            pid = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(
                foreground_hwnd, ctypes.byref(pid))
            foreground_pid = int(pid.value or 0)
        except Exception:
            foreground_hwnd = 0
            foreground_pid = 0
        same_window = bool(foreground_hwnd and foreground_hwnd == window.hwnd)
        same_process = bool(
            foreground_hwnd
            and foreground_pid
            and foreground_pid == window.pid
        )
        owned_modal = False
        returned_to_owner = False
        try:
            import ctypes

            owned_modal = bool(
                same_process
                and self._owned_hwnd(
                    ctypes.windll.user32, foreground_hwnd, int(window.hwnd)
                )
            )
            returned_to_owner = bool(
                same_process and window.owner_hwnd
                and foreground_hwnd == window.owner_hwnd
                and not ctypes.windll.user32.IsWindow(int(window.hwnd))
            )
        except Exception:
            owned_modal = False
        return {
            **state,
            "accepted": bool(state.get("foreground") or same_window or owned_modal or returned_to_owner),
            "foreground_hwnd": foreground_hwnd,
            "foreground_pid": foreground_pid,
            "owned_modal_transition": bool(owned_modal and not same_window),
            "returned_to_owner": returned_to_owner,
        }

    async def _focus_exact(self, window: WindowRecord) -> Any:
        self._prime_exact(window)
        with self._bound(window.window_id) as session:
            await self.control.focus_window({})
            active = session.active_window
            try:
                active_hwnd = int(active.get("hwnd") or 0)
            except Exception:
                active_hwnd = 0
            if active_hwnd != window.hwnd:
                raise DesktopStaleReference(
                    f"UIA focused HWND {active_hwnd}, expected strong HWND {window.hwnd}")
            self._assert_live(window)
            return session

    async def _extract_observation(
        self, window: WindowRecord, session: Any, *, completeness: str,
    ) -> AdapterObservation:
        return await self._extract_observation_key(
            window.window_id, session, completeness=completeness,
        )

    async def _extract_observation_key(
        self, identity_key: str, session: Any, *, completeness: str,
    ) -> AdapterObservation:
        from desktop.context import DesktopControlContext

        ctx = DesktopControlContext(session)

        def enrich() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int] | None]:
            uia: list[dict[str, Any]] = []
            visual: list[dict[str, Any]] = []
            for cid, original in list(session.last_snapshot.items()):
                rec = {k: v for k, v in dict(original).items() if k != "control"}
                rec["backend_key"] = str(cid)
                control = original.get("control")
                if control is None:
                    rec.setdefault("provenance", ["vlm"])
                    rec.setdefault("actionable", True)
                    visual.append(rec)
                    continue
                try:
                    rec["automation_id"] = str(control.AutomationId or "")
                except Exception:
                    rec["automation_id"] = ""
                try:
                    runtime_id = control.GetRuntimeId()
                    rec["runtime_id"] = [int(v) for v in runtime_id] if runtime_id else []
                except Exception:
                    rec["runtime_id"] = []
                rec["patterns"] = _patterns(
                    control, actionable=bool(rec.get("actionable"))
                )
                rec["provenance"] = ["uia"]
                uia.append(rec)
            # A UIA-only view still needs the live geometry used to project
            # control bounds and later validate its relative pointer points.
            from desktop.vision_capture import win32_window_rect

            hwnd = self._session_hwnd(session)
            try:
                rect = win32_window_rect(hwnd) if hwnd else None
            except Exception:
                rect = None
            return uia, visual, list(rect) if rect else None

        uia, visual, window_rect = await ctx._uia(enrich)
        generation = self._generations.get(identity_key, 0) + 1
        self._generations[identity_key] = generation
        modal = session.current_modal
        modal_payload = {
            "present": bool(modal and getattr(modal, "present", False)),
            "type": str(getattr(modal, "modal_type", "") or "") if modal else "",
            "title": str(getattr(modal, "title", "") or "") if modal else "",
            "summary": str(getattr(modal, "summary", "") or "") if modal else "",
            "blocking": bool(getattr(modal, "blocking", False)) if modal else False,
        }
        return AdapterObservation(
            uia=tuple(uia), visual=tuple(visual),
            uia_generation=generation, completeness=completeness,
            metadata={
                "driver_state_id": session.session_id,
                "snapshot_title": session.snapshot_title,
                "modal": modal_payload,
                "window_rect": window_rect,
            },
        )

    async def observe(self, window: WindowRecord, *, mode: str) -> AdapterObservation:
        await self._bind_exact(window, activate=True)
        with self._bound(window.window_id) as session:
            await self.control.focus_window({})
            self._assert_session_hwnd(window, session, operation="observation")
            self._assert_live(window)
            return await self._extract_observation(
                window, session, completeness="uia-only",
            )

    async def capture(self, window: WindowRecord) -> AdapterCapture:
        await self._bind_exact(window, activate=False)
        with self._bound(window.window_id) as session:
            encoded = await self.control.capture_raw_b64_async()
            self._assert_session_hwnd(window, session, operation="capture")
            if not encoded:
                raise DesktopUnavailable("desktop capture returned no image")
            try:
                png = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise DesktopUnavailable("desktop capture returned invalid base64") from exc
            meta = session.last_vision_meta
            origin = tuple(getattr(meta, "origin", (0, 0)) or (0, 0))
            width = int(getattr(meta, "width", 0) or 0)
            height = int(getattr(meta, "height", 0) or 0)
            mode = str(getattr(meta, "mode", "monitor") or "monitor")
            fallback = str(getattr(meta, "fallback_reason", "") or "")
            from desktop.vision_capture import win32_window_rect

            live_rect = win32_window_rect(int(window.hwnd))
            return AdapterCapture(
                png=png, width=width, height=height,
                provenance="mss_window_crop" if mode == "window" else "mss_monitor_fallback",
                # MSS sees composed visible pixels. It is not WGC/PrintWindow
                # and therefore cannot promise occluded/minimized content.
                occlusion_independent=False, minimized=window.minimized,
                stale=bool(fallback),
                coordinate_transform={
                    "screen_origin": [int(origin[0]), int(origin[1])],
                    "capture_size": [width, height], "dpi": window.dpi,
                    "mode": mode, "fallback_reason": fallback,
                    "window_rect": list(live_rect) if live_rect else None,
                },
            )

    def validate_source_geometry(
        self, window: WindowRecord, observation: Any,
    ) -> None:
        """Reject physical input when its screenshot/window has moved."""

        capture = dict(getattr(observation, "capture", {}) or {})
        transform = dict(capture.get("coordinate_transform") or {})
        expected = transform.get("window_rect")
        if not isinstance(expected, (list, tuple)) or len(expected) != 4:
            return
        from desktop.vision_capture import win32_window_rect

        current = win32_window_rect(int(window.hwnd))
        if current is None:
            raise DesktopStaleReference(
                f"window {window.window_id} has no current screen geometry"
            )
        expected_rect = tuple(int(value) for value in expected)
        if tuple(current) != expected_rect:
            raise DesktopStaleReference(
                "desktop view geometry changed after observation; observe again "
                "before pointer input"
            )

    @staticmethod
    def _record(session: Any, element: DesktopElement | None) -> dict[str, Any]:
        if element is None or not element.backend_key:
            raise DesktopUnavailable("this action requires a current UIA element")
        try:
            key = int(element.backend_key)
        except (TypeError, ValueError) as exc:
            raise DesktopUnavailable("element is not backed by a UIA control") from exc
        record = session.last_snapshot.get(key)
        if not record or record.get("control") is None:
            raise DesktopStaleReference("UIA element is absent from the current live snapshot")
        return record

    def _ensure_exact_element(
        self, ctx: Any, auto: Any, session: Any,
        element: DesktopElement, record: dict[str, Any],
    ) -> Any:
        """Fence a stable positional key to the observed UIA identity.

        A snapshot walk may reuse the same role/name occurrence for a different
        control. Positional re-resolution is safe only when its runtime ID still
        equals the one captured in the supplied observation.
        """
        from desktop.action_resolve import _ensure_live

        original = record.get("control")
        control = _ensure_live(ctx, auto, record)
        expected = tuple(element.runtime_id)
        if expected:
            if self._uia_runtime_id(control) != expected:
                raise DesktopStaleReference(
                    "UIA control identity changed since the supplied view; observe again"
                )
        elif (
            element.element_generation != self._generations.get(element.window_id, 0)
            or control is not original
        ):
            # Without a runtime ID, a current snapshot's exact held control is
            # the only identity we can prove. Do not accept a same-name proxy
            # from a later walk as the original target.
            raise DesktopStaleReference(
                "UIA control identity cannot be proven from this view; observe again"
            )
        return control

    async def preflight(
        self, window: WindowRecord, *, action: str, delivery: str,
        arguments: Mapping[str, Any],
    ) -> None:
        """Apply the driver's kill/config gates before a durable dispatch receipt."""

        await self._bind_exact(window, activate=True)
        self._assert_live(window)
        with self._bound(window.window_id) as session:
            from desktop.action_resolve import _ensure_gates
            from desktop.context import DesktopControlContext

            _ensure_gates(DesktopControlContext(session))

    async def validate_target_identity(
        self, window: WindowRecord, element: DesktopElement | None,
    ) -> None:
        """Reject an obsolete UIA view before minting a dispatch receipt."""
        if element is None or "uia" not in element.provenance:
            return
        with self._bound(window.window_id) as session:
            from desktop.context import DesktopControlContext

            ctx = DesktopControlContext(session)

            def check() -> None:
                auto = ctx._load_uia()
                record = self._record(session, element)
                self._ensure_exact_element(ctx, auto, session, element, record)

            await ctx._uia(check)

    async def _semantic_dispatch(
        self, window: WindowRecord, *, action: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch:
        await self._bind_exact(window, activate=False)
        with self._bound(window.window_id) as session:
            from desktop.context import DesktopControlContext
            ctx = DesktopControlContext(session)
            record = self._record(session, element)

            def run() -> AdapterDispatch:
                auto = ctx._load_uia()
                control = self._ensure_exact_element(ctx, auto, session, element, record)
                normalized = action.casefold()
                if normalized in {"invoke", "click"}:
                    try:
                        invoke = control.GetInvokePattern()
                    except Exception:
                        invoke = None
                    if invoke is not None:
                        invoke.Invoke()
                        return AdapterDispatch(True, "uia.invoke")
                    try:
                        legacy = control.GetLegacyIAccessiblePattern()
                    except Exception:
                        legacy = None
                    if legacy is not None:
                        legacy.DoDefaultAction()
                        return AdapterDispatch(True, "uia.legacy_default_action")
                    raise DesktopUnavailable("control has no semantic invoke pattern")
                if normalized in {"set_value", "type"}:
                    pattern = _require_pattern(control, "GetValuePattern", "Value")
                    _require_writable_pattern(pattern, "Value")
                    value = str(arguments.get("value", arguments.get("text", "")))
                    pattern.SetValue(value)
                    return AdapterDispatch(True, "uia.value", readback=str(pattern.Value or ""))
                if normalized == "select":
                    pattern = _require_pattern(
                        control, "GetSelectionItemPattern", "SelectionItem",
                    )
                    pattern.Select()
                    return AdapterDispatch(True, "uia.selection_item",
                                           readback=bool(pattern.IsSelected))
                if normalized == "toggle":
                    pattern = _require_pattern(control, "GetTogglePattern", "Toggle")
                    pattern.Toggle()
                    return AdapterDispatch(True, "uia.toggle",
                                           readback=int(pattern.ToggleState))
                if normalized in {"expand", "collapse"}:
                    pattern = _require_pattern(
                        control, "GetExpandCollapsePattern", "ExpandCollapse",
                    )
                    (pattern.Expand() if normalized == "expand" else pattern.Collapse())
                    return AdapterDispatch(True, f"uia.{normalized}")
                if normalized == "set_range_value":
                    pattern = _require_pattern(
                        control, "GetRangeValuePattern", "RangeValue",
                    )
                    _require_writable_pattern(pattern, "RangeValue")
                    pattern.SetValue(float(arguments["value"]))
                    return AdapterDispatch(True, "uia.range_value", readback=float(pattern.Value))
                if normalized == "scroll_into_view":
                    _require_pattern(
                        control, "GetScrollItemPattern", "ScrollItem",
                    ).ScrollIntoView()
                    return AdapterDispatch(True, "uia.scroll_item")
                if normalized == "focus":
                    control.SetFocus()
                    return AdapterDispatch(True, "uia.focus")
                raise DesktopUnavailable(f"unsupported semantic action: {action}")

            return await ctx._uia(run)

    @staticmethod
    def _point(element: DesktopElement | None, arguments: Mapping[str, Any]) -> tuple[int, int]:
        if arguments.get("x") is not None and arguments.get("y") is not None:
            try:
                return int(arguments["x"]), int(arguments["y"])
            except (TypeError, ValueError) as exc:
                raise DesktopUnavailable("physical x/y coordinates must be integers") from exc
        if element is None or not element.bounds:
            raise DesktopUnavailable("physical target has no current screen bounds")
        left, top, right, bottom = element.bounds
        return (left + right) // 2, (top + bottom) // 2

    @staticmethod
    def _has_physical_point(
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> bool:
        """Return whether a click has a usable explicit or observed point."""

        if arguments.get("x") is not None and arguments.get("y") is not None:
            return True
        if element is None or element.bounds is None:
            return False
        left, top, right, bottom = element.bounds
        return right > left and bottom > top

    @staticmethod
    def _uia_runtime_id(control: Any) -> tuple[int, ...]:
        try:
            value = control.GetRuntimeId()
            return tuple(int(item) for item in (value or ()))
        except Exception:
            return ()

    @classmethod
    def _same_uia_control(cls, left: Any, right: Any) -> bool:
        if left is right:
            return True
        left_id = cls._uia_runtime_id(left)
        right_id = cls._uia_runtime_id(right)
        return bool(left_id and right_id and left_id == right_id)

    @classmethod
    def _uia_ancestors(cls, control: Any, *, limit: int = 12) -> list[Any]:
        rows: list[Any] = []
        current = control
        for _ in range(limit):
            if current is None:
                break
            rows.append(current)
            try:
                parent = current.GetParentControl()
            except Exception:
                break
            if parent is None or cls._same_uia_control(parent, current):
                break
            current = parent
        return rows

    @staticmethod
    def _uia_text(control: Any, name: str) -> str:
        try:
            return str(getattr(control, name) or "")
        except Exception:
            return ""

    @staticmethod
    def _uia_flag(control: Any, name: str) -> bool:
        value = getattr(control, name)
        return bool(value() if callable(value) else value)

    @classmethod
    def _inside_item_container(cls, control: Any) -> bool:
        for current in cls._uia_ancestors(control):
            control_type = cls._uia_text(
                current, "ControlTypeName"
            ).casefold()
            automation_id = cls._uia_text(
                current, "AutomationId"
            ).casefold()
            if automation_id == "system.itemnamedisplay":
                return True
            if control_type in {
                "dataitemcontrol", "listitemcontrol", "treeitemcontrol",
            }:
                return True
        return False

    @classmethod
    def _require_physical_value_focus(
        cls, auto: Any, target: Any,
    ) -> Any:
        try:
            focused = auto.GetFocusedControl()
        except Exception:
            focused = None
        if focused is None:
            raise DesktopUnavailable(
                "physical set_value target did not acquire editable keyboard "
                "focus; no selection or text keys were sent. Observe and "
                "target a current editable control"
            )
        related = any(
            cls._same_uia_control(current, target)
            for current in cls._uia_ancestors(focused)
        )
        control_type = cls._uia_text(
            focused, "ControlTypeName"
        ).casefold()
        try:
            has_keyboard_focus = cls._uia_flag(
                focused, "HasKeyboardFocus"
            )
            enabled = cls._uia_flag(focused, "IsEnabled")
            offscreen = cls._uia_flag(focused, "IsOffscreen")
        except Exception as exc:
            raise DesktopUnavailable(
                "physical set_value could not prove editable keyboard focus; "
                "no selection or text keys were sent. Observe and target a "
                "current editable control"
            ) from exc
        if (
            not related
            or control_type not in {"editcontrol", "documentcontrol"}
            or not has_keyboard_focus
            or not enabled
            or offscreen
            or cls._inside_item_container(focused)
        ):
            raise DesktopUnavailable(
                "physical set_value target did not acquire editable keyboard "
                "focus; no selection or text keys were sent. Observe and "
                "target a current editable control"
            )
        return focused

    async def _physical_dispatch(
        self, window: WindowRecord, *, action: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch:
        """Deliver directly against Desktop Fabric's exact HWND authority.

        The legacy driver session remains the UIA observation engine, but it
        is not a second input authority.  In particular, an already-bound
        WindowRecord must never be rejected as ``NO_DESKTOP_TARGET`` while a
        model is acting on the view produced from that same record.
        """
        from desktop.context import DesktopControlContext
        from desktop.input_primitives import (
            _click_direct,
            _drag_direct,
            _scroll_direct,
            _send_keys_direct,
            _type_text_direct,
        )

        normalized = action.casefold()
        with self._bound(window.window_id) as session:
            ctx = DesktopControlContext(session)

            def deliver() -> tuple[str, Any, dict[str, Any]]:
                import ctypes

                auto = ctx._load_uia()
                user32 = ctypes.windll.user32
                foreground_hwnd = int(user32.GetForegroundWindow() or 0)
                foreground_pid = ctypes.c_ulong(0)
                if foreground_hwnd:
                    user32.GetWindowThreadProcessId(
                        foreground_hwnd, ctypes.byref(foreground_pid))
                same_process = bool(
                    foreground_pid.value
                    and int(foreground_pid.value) == int(window.pid)
                )
                owned_modal = bool(
                    same_process
                    and self._owned_hwnd(
                        user32, foreground_hwnd, int(window.hwnd)
                    )
                )
                activation_attempted = False
                if foreground_hwnd != int(window.hwnd) and not owned_modal:
                    activation_attempted = True
                    try:
                        if bool(user32.IsIconic(int(window.hwnd))):
                            user32.ShowWindow(int(window.hwnd), 9)  # SW_RESTORE
                        user32.BringWindowToTop(int(window.hwnd))
                        user32.SetForegroundWindow(int(window.hwnd))
                    except Exception:
                        # Input is intentionally not vetoed by focus policy.
                        # The durable operation receipt and post-observation
                        # retain the actual outcome.
                        pass
                active_hwnd = int(user32.GetForegroundWindow() or 0)
                active_pid = ctypes.c_ulong(0)
                if active_hwnd:
                    user32.GetWindowThreadProcessId(
                        active_hwnd, ctypes.byref(active_pid))
                active_owned_modal = bool(
                    active_pid.value
                    and int(active_pid.value) == int(window.pid)
                    and self._owned_hwnd(
                        user32, active_hwnd, int(window.hwnd)
                    )
                )
                if active_hwnd != int(window.hwnd) and not active_owned_modal:
                    raise DesktopStaleReference(
                        f"physical input target HWND {window.hwnd} is not the "
                        f"foreground window (actual {active_hwnd})"
                    )
                activation = {
                    "activation_attempted": activation_attempted,
                    "activation_confirmed": bool(
                        active_hwnd == int(window.hwnd)
                        or active_owned_modal
                    ),
                    "foreground_hwnd": active_hwnd,
                    "owned_modal_foreground": bool(active_owned_modal),
                }

                def guard(point=None):
                    # Once delivery starts, never refocus a displaced target:
                    # the caller must observe any partial effect before retry.
                    self._assert_live(window)
                    current = int(user32.GetForegroundWindow() or 0)
                    pid = ctypes.c_ulong(0)
                    if current:
                        user32.GetWindowThreadProcessId(current, ctypes.byref(pid))
                    if (int(pid.value) != int(window.pid) or (
                        current != int(window.hwnd)
                        and not self._owned_hwnd(user32, current, int(window.hwnd))
                    )):
                        raise DesktopStaleReference("foreground changed during input; inspect any partial effect before retrying")
                    if point is not None:
                        from desktop.vision_capture import win32_window_rect

                        rect = win32_window_rect(current)
                        x, y = point
                        if not rect or not (rect[0] <= x < rect[2] and rect[1] <= y < rect[3]):
                            raise DesktopStaleReference("pointer target is outside the foreground window")
                        from ctypes import wintypes

                        hit = int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)
                        root = int(user32.GetAncestor(hit, 2) or hit)  # GA_ROOT
                        if root != current:
                            raise DesktopStaleReference("pointer target is covered by another window")

                if element is not None and "uia" in element.provenance:
                    record = self._record(session, element)
                    self._ensure_exact_element(ctx, auto, session, element, record)

                if normalized in {
                    "click", "double_click", "invoke",
                    "select", "toggle",
                }:
                    x, y = self._point(element, arguments)
                    click_args = {"x": x, "y": y, **dict(arguments)}
                    if normalized == "double_click":
                        click_args["double"] = True
                    return "physical.pointer", _click_direct(auto, click_args, guard=guard), activation
                if normalized in {"set_value", "type"}:
                    if element is None:
                        raise DesktopUnavailable("physical set_value requires a UIA target")
                    record = self._record(session, element)
                    control = self._ensure_exact_element(ctx, auto, session, element, record)
                    if (
                        str(element.automation_id or "").casefold()
                        == "system.itemnamedisplay"
                        or self._inside_item_container(control)
                    ):
                        raise DesktopUnavailable(
                            "physical set_value refused a file/list item target; "
                            "no keyboard input was sent. Observe and target the "
                            "actual editable field"
                        )
                    x, y = self._point(element, arguments)
                    _click_direct(auto, {"x": x, "y": y}, guard=guard)
                    self._require_physical_value_focus(auto, control)
                    value = str(arguments.get("value", arguments.get("text", "")))
                    def value_guard(point=None):
                        guard(point)
                        self._require_physical_value_focus(auto, control)

                    _send_keys_direct(auto, "{Ctrl}a", guard=value_guard)
                    if value:
                        activation["delivery_receipt"] = _type_text_direct(
                            auto, value, guard=value_guard
                        )
                    else:
                        activation["delivery_receipt"] = _send_keys_direct(
                            auto, "{Back}", guard=value_guard
                        )
                    return (
                        "physical.pointer_keyboard",
                        None,
                        activation,
                    )
                if normalized == "send_keys":
                    return (
                        "physical.keyboard",
                        _send_keys_direct(auto, arguments.get("keys"), guard=guard),
                        activation,
                    )
                if normalized == "type_text":
                    activation["delivery_receipt"] = _type_text_direct(
                        auto, arguments.get("text"), guard=guard)
                    return (
                        "physical.keyboard_text",
                        None,
                        activation,
                    )
                if normalized == "scroll":
                    return "physical.wheel", _scroll_direct(auto, arguments, guard=guard), activation
                if normalized == "drag":
                    return (
                        "physical.pointer_drag",
                        _drag_direct(auto, arguments, guard=guard),
                        activation,
                    )
                raise DesktopUnavailable(f"unsupported physical action: {action}")

            method, result, activation = await ctx._uia(deliver)
            return AdapterDispatch(
                True,
                method,
                readback=result,
                metadata=activation,
            )

    async def dispatch(
        self, window: WindowRecord, *, action: str, delivery: str,
        element: DesktopElement | None, arguments: Mapping[str, Any],
    ) -> AdapterDispatch:
        self._assert_live(window)
        selected = str(delivery or "auto").casefold()
        if selected == "auto":
            normalized = action.casefold()
            # An advertised UIA Invoke pattern is not proof that invoking it is
            # safe. Qt and other desktop applications can expose the pattern
            # while blocking indefinitely inside the COM call. A normal click
            # is therefore pointer-first whenever the fresh observation gives
            # us a usable point. Explicit ``delivery="semantic"`` and controls
            # without usable bounds retain semantic invocation.
            if normalized == "click" and self._has_physical_point(
                element, arguments,
            ):
                selected = "physical"
            elif (
                normalized == "set_value"
                and window.class_name.casefold().startswith("qt")
                and element is not None and element.role.casefold() == "edit"
                and self._has_physical_point(element, arguments)
            ):
                # Qt ValuePattern can change QLineEdit text without emitting
                # the user-edit signal that commits the application's value.
                # Choose guarded real typing before dispatch, never retry an
                # already-applied semantic edit. Other providers retain Value.
                selected = "physical"
            else:
                required = {
                    "click": {"invoke", "legacy_action"},
                    "invoke": {"invoke", "legacy_action"},
                    "set_value": {"value"},
                    "select": {"selection_item"},
                    "toggle": {"toggle"},
                    "expand": {"expand_collapse"},
                    "collapse": {"expand_collapse"},
                    "scroll_into_view": {"scroll_item"},
                }.get(normalized, set())
                selected = (
                    "semantic"
                    if element is not None and required.intersection(element.patterns)
                    else "physical"
                )
        if selected == "semantic":
            result = await self._semantic_dispatch(
                window, action=action, element=element, arguments=arguments)
        else:
            result = await self._physical_dispatch(
                window, action=action, element=element, arguments=arguments)
        return AdapterDispatch(
            result.delivered,
            result.method,
            readback=result.readback,
            metadata={**dict(result.metadata), "selected_delivery": selected},
        )

    async def focus(self, window: WindowRecord) -> AdapterFocus:
        session = await self._focus_exact(window)
        state = self._assert_live(window, foreground=True)
        with self._bound(window.window_id):
            observation = await self._extract_observation(
                window, session, completeness="uia-only",
            )
        return AdapterFocus(
            result={
                "focused": bool(state.get("foreground")),
                "window_id": window.window_id,
                "hwnd": window.hwnd,
                "title": window.title,
            },
            hwnd=window.hwnd,
            observation=observation,
        )

    async def focus_session(self, arguments: Mapping[str, Any]) -> AdapterFocus:
        """Resolve a named window with ephemeral UIA driver scratch."""

        key = "__locator__"
        try:
            with self._bound(key) as session:
                result = await self.control.focus_window(dict(arguments))
                try:
                    hwnd = int((session.active_window or {}).get("hwnd") or 0)
                except Exception:
                    hwnd = 0
                observation = await self._extract_observation_key(
                    f"hwnd:{hwnd}", session, completeness="uia-only",
                )
                return AdapterFocus(
                    result=result, hwnd=hwnd, observation=observation,
                )
        finally:
            self._sessions.pop(key, None)

    def capability_report(self) -> Mapping[str, Any]:
        return {
            "adapter": "windows_uia_mss_sendinput",
            "driver_state": {
                "explicitly_bound_per_window": True,
                "checkpointed": False,
                "process_default": False,
                "run_registry": False,
            },
            "windows_available": self.win32.available(),
            "catalog": {"strong_hwnd_process_identity": True,
                        "strong_identity_required_for_bind": True,
                        "unavailable_process_start_is_not_treated_as_strong": True,
                        "package_enrichment": False,
                        "virtual_desktop_enrichment": False},
            "perception": {"uia": True, "screenshot": True,
                           "ocr": False, "secondary_grounder": False},
            "capture": {"mss_visible_pixels": True, "wgc": False,
                        "print_window": False, "occlusion_independent": False},
            "actions": {"host_selected_semantic_or_physical": True,
                        "physical_input": True,
                        "semantic_supported": [
                            "invoke", "set_value", "focus", "toggle", "select",
                            "expand", "collapse", "set_range_value",
                            "scroll_into_view",
                        ],
                        "physical_supported": [
                            "click", "set_value",
                            "send_keys", "type_text", "scroll", "drag",
                        ]},
        }

    def close(self) -> None:
        self._sessions.clear()


__all__ = [
    "AdapterCapture", "AdapterDispatch", "AdapterFocus", "AdapterObservation",
    "DesktopLiveAdapter", "WindowsDesktopAdapter",
]
