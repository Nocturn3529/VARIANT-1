"""Durable Windows Desktop Fabric orchestration and stable factory surface."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import time
import uuid
import weakref
from typing import Any, Mapping

from .adapter import (
    AdapterCapture,
    AdapterDispatch,
    AdapterFocus,
    AdapterObservation,
    DesktopLiveAdapter,
    WindowsDesktopAdapter,
)
from .fusion import fuse_elements, observation_fingerprint
from .models import (
    AppRecord,
    DesktopAmbiguousTarget,
    DesktopCapture,
    DesktopElement,
    DesktopNotFound,
    DesktopObservation,
    DesktopOperation,
    DesktopStaleReference,
    DesktopUnavailable,
    DesktopValidationError,
    OBSERVATION_MODES,
    WindowRecord,
    coerce_scope,
)
from .repository import DesktopFabricRepository, new_desktop_id
from .verification import VerificationResult, verify_operation


# SendInput/legacy coordinate delivery is desktop-global, including across two
# runtime instances in the same backend process. Production owns one asyncio
# loop, and asyncio.Lock removes the uncancellable worker-thread acquisition
# window that could otherwise strand a threading.Lock forever.
class _AsyncProcessLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, _type, _value, _traceback) -> None:
        self._lock.release()


_PHYSICAL_INPUT_LOCK = _AsyncProcessLock()


def _media_type(png: bytes) -> str:
    return "image/png" if png.startswith(b"\x89PNG\r\n\x1a\n") else "application/octet-stream"


def _action_observation_mode(target: Any) -> str:
    """Desktop actions use one UIA state path; screenshots are separate evidence."""

    del target
    return "uia"


@dataclass(frozen=True, slots=True)
class DesktopRecoveryReport:
    backend_instance_id: str
    failed_before_dispatch: int = 0
    unknown_effect: int = 0
    windows_need_rebind: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_instance_id": self.backend_instance_id,
            "failed_before_dispatch": self.failed_before_dispatch,
            "unknown_effect": self.unknown_effect,
            "windows_need_rebind": self.windows_need_rebind,
            "automatic_replay": False,
            "exactly_once_delivery": False,
            "contract": (
                "prepared operations are failed as not delivered; operations that "
                "may have crossed the input boundary become unknown_effect and are never replayed"
            ),
        }


class DesktopFabric:
    """Stable service facade for catalog, observation, capture, action, and events."""

    def __init__(
        self, *, repository: DesktopFabricRepository,
        artifact_store: Any, adapter: DesktopLiveAdapter,
        backend_instance_id: str, recovery_report: DesktopRecoveryReport,
        owns_artifact_store: bool = False,
    ) -> None:
        self.repository = repository
        self.artifact_store = artifact_store
        self.adapter = adapter
        self.backend_instance_id = str(backend_instance_id)
        self.recovery_report = recovery_report
        self.owns_artifact_store = bool(owns_artifact_store)
        # Owners and waiters retain the lock. Idle closed-window entries need
        # no strong reference for the rest of the backend lifetime.
        self._semantic_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._lock_guard = asyncio.Lock()
        self._closed = False

    async def _semantic_lock(self, window_id: str) -> asyncio.Lock:
        async with self._lock_guard:
            return self._semantic_locks.setdefault(window_id, asyncio.Lock())

    async def _post_action_window(
        self, record: WindowRecord,
    ) -> tuple[WindowRecord, dict[str, Any]]:
        """Keep an expected same-process modal as the next observation target."""

        inspect = getattr(self.adapter, "post_action_target", None)
        state = dict(
            inspect(record) if callable(inspect)
            else self.adapter.validate_window(record)
        )
        state.setdefault("accepted", bool(state.get("foreground")))
        foreground_hwnd = int(state.get("foreground_hwnd") or 0)
        if not (
            (state.get("owned_modal_transition") or state.get("returned_to_owner"))
            and foreground_hwnd
            and foreground_hwnd != record.hwnd
        ):
            return record, state
        await asyncio.to_thread(self.refresh_catalog)
        matches = [
            item for item in self.repository.list_windows(include_missing=False)
            if item.live and item.hwnd == foreground_hwnd and item.pid == record.pid
            and item.pid_started_at == record.pid_started_at
        ]
        if len(matches) != 1:
            state["related_window_unresolved"] = True
            return record, state
        related = self.bind(matches[0])
        self._attach_binding(related)
        state["related_window_id"] = related.window_id
        return related, state

    @staticmethod
    def _binding() -> Any:
        from .binding import current_desktop_binding

        return current_desktop_binding()

    def _attach_binding(self, window: WindowRecord) -> None:
        binding = self._binding()
        if binding is not None:
            binding.attach(window.window_id)

    def refresh_catalog(self) -> tuple[list[AppRecord], list[WindowRecord]]:
        self._ensure_open()
        apps, windows = self.adapter.catalog(
            backend_instance_id=self.backend_instance_id)
        return self.repository.upsert_catalog(
            apps, windows, backend_instance_id=self.backend_instance_id)

    def apps(self, *, query: str = "", running: bool | None = None) -> list[AppRecord]:
        records = self.repository.list_apps(running=running)
        needle = str(query or "").casefold()
        if not needle:
            return records
        return [record for record in records if needle in " ".join((
            record.display_name, record.executable, record.package_family,
            record.app_user_model_id,
        )).casefold()]

    def windows(
        self, *, app: str | AppRecord | None = None, query: str = "",
        include_missing: bool = False, include_occluded: bool = True,
    ) -> list[WindowRecord]:
        app_id = app.app_id if isinstance(app, AppRecord) else str(app or "")
        records = self.repository.list_windows(
            app_id=app_id, include_missing=include_missing)
        needle = str(query or "").casefold()
        return [record for record in records if (
            (include_occluded or not record.occluded)
            and (not needle or needle in " ".join((
                record.title, record.class_name, record.executable,
                record.package_family,
            )).casefold())
        )]

    def current_window(self, *, refresh: bool = True) -> WindowRecord:
        if refresh:
            self.refresh_catalog()
        foreground = [
            record for record in self.repository.list_windows(
                include_missing=False
            )
            if record.live and record.foreground
        ]
        if len(foreground) == 1:
            record = self.bind(foreground[0])
            self._attach_binding(record)
            return record
        binding = self._binding()
        if binding is not None and binding.active_window_id:
            try:
                return self.bind(binding.active_window_id)
            except (DesktopNotFound, DesktopStaleReference):
                binding.forget(binding.active_window_id)
        raise DesktopUnavailable(
            "desktop has no single strongly identified foreground or bound "
            "window"
        )

    def bind(self, window: str | WindowRecord) -> WindowRecord:
        self._ensure_open()
        record = window if isinstance(window, WindowRecord) else self.repository.get_window(str(window))
        if not record.live:
            raise DesktopStaleReference(
                f"window {record.window_id} is absent from the latest catalog")
        if record.pid_started_at <= 0:
            raise DesktopUnavailable(
                f"window {record.window_id} lacks a process start time and cannot "
                "be bound with strong identity")
        live = dict(self.adapter.validate_window(record))
        if not live.get("live"):
            raise DesktopStaleReference(
                f"window {record.window_id} failed strong live identity validation: "
                f"{live.get('reason') or 'unknown'}")
        return record

    async def capture(self, window: str | WindowRecord) -> DesktopCapture:
        self._ensure_open()
        record = self.bind(window)
        self._attach_binding(record)
        live_capture: AdapterCapture = await self.adapter.capture(record)
        if not live_capture.png:
            raise DesktopUnavailable("desktop capture produced an empty payload")
        artifact = self.artifact_store.put_bytes(
            live_capture.png, media_type=_media_type(live_capture.png),
            kind="desktop_capture", scope=f"desktop:window:{record.window_id}",
        )
        capture = DesktopCapture(
            capture_id=new_desktop_id("cap"), window_id=record.window_id,
            window_generation=record.generation, artifact_ref=artifact.ref,
            bytes=int(artifact.bytes), width=int(live_capture.width),
            height=int(live_capture.height), provenance=live_capture.provenance,
            occlusion_independent=bool(live_capture.occlusion_independent),
            minimized=bool(live_capture.minimized), stale=bool(live_capture.stale),
            coordinate_transform=dict(live_capture.coordinate_transform),
            created_at=time.time(),
        )
        return self.repository.record_capture(capture)

    async def observe(
        self, window: str | WindowRecord, *, mode: str = "fused",
        include_image: bool = True, scope: Any = None,
    ) -> DesktopObservation:
        self._ensure_open()
        normalized_mode = str(mode or "fused").casefold()
        if normalized_mode not in OBSERVATION_MODES:
            raise DesktopValidationError(
                f"observation mode must be one of {sorted(OBSERVATION_MODES)}")
        record = self.bind(window)
        self._attach_binding(record)
        live = await self.adapter.observe(record, mode=normalized_mode)
        return await self._record_observation(
            record, live, mode=normalized_mode, include_image=include_image,
            scope=scope,
        )

    async def _record_observation(
        self, record: WindowRecord, live: AdapterObservation, *,
        mode: str, include_image: bool, scope: Any = None,
    ) -> DesktopObservation:
        """Persist one already-produced driver walk without perceiving twice."""

        # Recheck after UIA/capture work so HWND reuse or process death during
        # observation cannot mint a current-looking element generation.
        self.bind(record)
        capture = None
        capture_error = ""
        if include_image:
            try:
                capture = await self.capture(record)
            except DesktopUnavailable as exc:
                capture_error = str(exc)
        observation_id = new_desktop_id("obs")
        visual = live.visual if mode in {"fused", "visual"} else ()
        uia = live.uia if mode in {"fused", "uia"} else ()
        ocr = live.ocr if mode in {"fused", "visual"} else ()
        elements = fuse_elements(
            uia=uia, visual=visual, ocr=ocr,
            observation_id=observation_id, window_id=record.window_id,
            window_generation=record.generation,
            element_generation=max(1, int(live.uia_generation)),
        )
        fingerprint = observation_fingerprint(
            elements, window_generation=record.generation)
        capture_dict = capture.to_dict() if capture else {}
        if capture is None:
            # A screenshot may be omitted or unavailable while the UIA walk is
            # still usable. Keep its live HWND rect as the view's coordinate
            # authority instead of later consulting stale catalog bounds.
            raw_rect = live.metadata.get("window_rect")
            if isinstance(raw_rect, (list, tuple)) and len(raw_rect) == 4:
                rect = [int(value) for value in raw_rect]
                capture_dict["coordinate_transform"] = {
                    "window_rect": rect,
                    "screen_origin": rect[:2],
                    "capture_size": [rect[2] - rect[0], rect[3] - rect[1]],
                    "mode": "window",
                }
        if capture_error:
            capture_dict["error"] = capture_error
        capture_dict["adapter"] = dict(live.metadata)
        observation = DesktopObservation(
            observation_id=observation_id, window_id=record.window_id,
            window_generation=record.generation, mode=mode,
            elements=elements, capture_id=capture.capture_id if capture else "",
            image_ref=capture.artifact_ref if capture else "",
            capture=capture_dict, completeness=live.completeness,
            uia_generation=int(live.uia_generation), fingerprint=fingerprint,
            created_at=time.time(),
            scope=coerce_scope(scope),
        )
        return self.repository.record_observation(observation)

    @staticmethod
    def _resolve_target(
        observation: DesktopObservation,
        target: DesktopElement | Mapping[str, Any] | str | None,
    ) -> DesktopElement | None:
        if target is None:
            return None
        if isinstance(target, DesktopElement):
            if target.window_id != observation.window_id:
                raise DesktopStaleReference("element belongs to a different window")
            if target.window_generation != observation.window_generation:
                raise DesktopStaleReference("element belongs to a stale window generation")
            reference = target.element_ref
            matches = [item for item in observation.elements
                       if item.element_ref == reference]
        elif isinstance(target, str):
            matches = [
                item for item in observation.elements
                if item.element_ref == target
            ]
            if not matches:
                requested = target.strip().casefold()
                if requested:
                    matches = [
                        item for item in observation.elements
                        if str(item.name or "").strip().casefold() == requested
                    ]
        elif isinstance(target, Mapping):
            expected_window = str(target.get("window_id") or "")
            if expected_window and expected_window != observation.window_id:
                raise DesktopStaleReference(
                    "target belongs to a different window")
            expected_generation = target.get("window_generation")
            if (expected_generation is not None
                    and int(expected_generation) != observation.window_generation):
                raise DesktopStaleReference("target belongs to a stale window generation")
            reference = str(target.get("element_ref") or target.get("ref") or "")
            if reference:
                matches = [item for item in observation.elements
                           if item.element_ref == reference]
            elif target.get("backend_key") is not None or target.get("id") is not None:
                backend_key = str(
                    target.get("backend_key", target.get("id")) or "")
                matches = [
                    item for item in observation.elements
                    if str(item.backend_key or "") == backend_key
                ]
            else:
                criteria = {
                    key: target[key] for key in
                    ("role", "name", "text", "value", "actionable", "provenance")
                    if key in target
                }
                matches = observation.find(**criteria)
        else:
            raise DesktopValidationError("target must be an element, reference, selector, or null")
        if not matches:
            raise DesktopNotFound(
                "target did not resolve in the fresh observation. Pass a control "
                "from view.controls, its raw id/element_ref, or its exact name. "
                "The 'id=...' string is not selector syntax. Refresh with "
                "computer.observe(window=view.window, include_text=True)."
            )
        if len(matches) != 1:
            raise DesktopAmbiguousTarget(
                f"target resolved to {len(matches)} current elements")
        return matches[0]

    @staticmethod
    def _preflight_action(
        action: str, delivery: str, element: DesktopElement | None,
    ) -> None:
        if not action.strip():
            raise DesktopValidationError("desktop action is required")
        if delivery != "semantic":
            return
        normalized = action.casefold()
        required = {
            "click": {"invoke", "legacy_action"},
            "invoke": {"invoke", "legacy_action"},
            "set_value": {"value"}, "type": {"value"},
            "select": {"selection_item"}, "toggle": {"toggle"},
            "expand": {"expand_collapse"}, "collapse": {"expand_collapse"},
            "set_range_value": {"range_value"},
            "scroll_into_view": {"scroll_item"},
        }
        if normalized == "focus":
            if element is None or "uia" not in element.provenance:
                raise DesktopUnavailable("semantic focus requires a current UIA element")
            return
        patterns = required.get(normalized)
        if patterns is None:
            raise DesktopUnavailable(f"unsupported semantic action: {action}")
        if element is None:
            raise DesktopUnavailable(f"semantic {action} requires an element target")
        if not patterns.intersection(element.patterns):
            raise DesktopUnavailable(
                f"target does not expose a semantic pattern for {action}")

    @staticmethod
    def _durable_target(element: DesktopElement | None) -> dict[str, Any]:
        if element is None:
            return {}
        # Observation IDs and UIA walk counters are evidence provenance, not
        # request identity. Keeping them out makes an idempotency key stable
        # whether the caller supplies its visible source view or requests a
        # host-created pre-action observation.
        return {
            "element_ref": element.element_ref,
            "window_id": element.window_id,
            "window_generation": element.window_generation,
            "element_generation": element.element_generation,
            "runtime_id": list(element.runtime_id),
            "automation_id": element.automation_id,
            "semantic_path": element.semantic_path,
            "resolved_fingerprint": element.fingerprint,
            "role": element.role,
            "name": element.name,
            "backend_key": element.backend_key,
            "provenance": list(element.provenance),
        }

    async def act(
        self, window: str | WindowRecord, action: str, *,
        target: DesktopElement | Mapping[str, Any] | str | None = None,
        delivery: str = "auto", arguments: Mapping[str, Any] | None = None,
        expect: Mapping[str, Any] | None = None,
        scope: Any = None, idempotency_key: str = "",
        include_image_evidence: bool = True,
        source_observation: DesktopObservation | None = None,
    ) -> DesktopOperation:
        self._ensure_open()
        record = self.bind(window)
        self._attach_binding(record)
        normalized_delivery = str(delivery or "auto").casefold()
        if normalized_delivery not in {"auto", "semantic", "physical"}:
            raise DesktopValidationError("delivery must be auto, semantic, or physical")
        arguments_value = dict(arguments or {})
        expectation_value = dict(expect or {})
        observation_mode = _action_observation_mode(target)
        lock = (
            await self._semantic_lock(record.window_id)
            if normalized_delivery == "semantic" else _PHYSICAL_INPUT_LOCK
        )
        async with lock:
            if source_observation is None:
                before = await self.observe(
                    record, mode=observation_mode,
                    include_image=include_image_evidence, scope=scope)
            else:
                before = source_observation
                before.require_scope(scope)
                if before.window_id != record.window_id:
                    raise DesktopStaleReference(
                        "source view belongs to a different window")
                if before.window_generation != record.generation:
                    raise DesktopStaleReference(
                        "source view belongs to a stale window generation")
                # Revalidate exact HWND/process identity immediately before
                # input without repeating the UIA walk or screenshot capture.
                self.bind(record)
            geometry_check = getattr(
                self.adapter, "validate_source_geometry", None
            )
            if callable(geometry_check):
                geometry_check(record, before)
            element = self._resolve_target(before, target)
            self._preflight_action(str(action), normalized_delivery, element)
            await self.adapter.preflight(
                record, action=str(action), delivery=normalized_delivery,
                arguments=arguments_value,
            )
            identity_check = getattr(self.adapter, "validate_target_identity", None)
            if callable(identity_check):
                await identity_check(record, element)
            operation = self.repository.prepare_operation(
                window=record, scope=coerce_scope(scope),
                idempotency_key=str(idempotency_key or ""), action=str(action),
                delivery=normalized_delivery,
                target=self._durable_target(element),
                arguments=arguments_value, expectation=expectation_value,
                before_observation_id=before.observation_id,
                backend_instance_id=self.backend_instance_id,
            )
            # Repeated idempotent requests return their durable receipt and do
            # not resume or replay any intermediate state.
            if operation.state != "prepared" or operation.backend_instance_id != self.backend_instance_id:
                return operation
            operation = self.repository.transition_operation(
                operation.operation_id, expected="prepared", state="dispatched",
                dispatch={"selected_delivery": normalized_delivery,
                          "delivery_possible": True,
                          "automatic_retry": False},
            )
            try:
                dispatch: AdapterDispatch = await self.adapter.dispatch(
                    record, action=str(action), delivery=normalized_delivery,
                    element=element, arguments=arguments_value)
            except asyncio.CancelledError:
                # The input boundary may have been crossed. Persist the
                # conservative receipt, but preserve asyncio cancellation for
                # the owning CPython cell instead of converting it to a tool
                # result that lets execution continue.
                try:
                    self.repository.transition_operation(
                        operation.operation_id, expected="dispatched",
                        state="unknown_effect",
                        evidence={
                            "delivery_possible": True,
                            "automatic_retry": False,
                            "cancelled_during_dispatch": True,
                        },
                        error="desktop operation cancelled after dispatch boundary",
                    )
                finally:
                    raise
            except BaseException as exc:
                # Once the boundary is marked dispatched, adapter errors are
                # conservatively unknown. No automatic alternate/retry occurs.
                return self.repository.transition_operation(
                    operation.operation_id, expected="dispatched",
                    state="unknown_effect",
                    evidence={"delivery_possible": True, "automatic_retry": False,
                              "adapter_error": f"{type(exc).__name__}: {exc}"[:1000]},
                    error=f"desktop delivery outcome is unknown: {exc}"[:1000],
                )
            if not dispatch.delivered:
                return self.repository.transition_operation(
                    operation.operation_id, expected="dispatched", state="no_effect",
                    evidence={"delivery_possible": False, "method": dispatch.method,
                              "automatic_retry": False},
                )
            try:
                after_record, focus_state = await self._post_action_window(record)
            except BaseException as exc:
                failed = self.repository.transition_operation(
                    operation.operation_id, expected="dispatched", state="unknown_effect",
                    evidence={"delivery_possible": True, "automatic_retry": False,
                              "input_sent": True, "method": dispatch.method,
                              "post_action_window_error": f"{type(exc).__name__}: {exc}"[:1000]},
                    error="input was delivered but window ownership could not be resolved",
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return failed
            actual_delivery = str(
                dispatch.metadata.get("selected_delivery")
                or (
                    normalized_delivery
                    if normalized_delivery != "auto"
                    else "semantic"
                    if ".uia" in dispatch.method or dispatch.method.startswith("uia.")
                    else "physical"
                )
            )
            if actual_delivery != "semantic" and not focus_state.get("accepted"):
                return self.repository.transition_operation(
                    operation.operation_id, expected="dispatched",
                    state="unknown_effect",
                    evidence={"method": dispatch.method, "focus_stolen": True,
                              "delivery_possible": True, "automatic_retry": False,
                              "input_sent": True, "live_state": focus_state},
                    error="foreground ownership changed during physical input delivery",
                )
            owner_rechecked = False
            try:
                try:
                    after = await self.observe(
                        after_record, mode=observation_mode,
                        include_image=include_image_evidence, scope=scope)
                except DesktopStaleReference:
                    # A native dialog may close between the ownership check
                    # and UIA observation. Recheck that exact owner once;
                    # never repeat input or redirect to an arbitrary window.
                    if after_record.window_id != record.window_id or not record.owner_hwnd:
                        raise
                    owner, owner_state = await self._post_action_window(record)
                    if not owner_state.get('returned_to_owner') or owner.window_id == record.window_id:
                        raise
                    after = await self.observe(
                        owner, mode=observation_mode,
                        include_image=include_image_evidence, scope=scope)
                    after_record = owner
                    owner_rechecked = True
            except asyncio.CancelledError:
                try:
                    self.repository.transition_operation(
                        operation.operation_id, expected="dispatched",
                        state="unknown_effect",
                        evidence={
                            "method": dispatch.method,
                            "delivery_possible": True,
                            "automatic_retry": False,
                            "cancelled_during_observation": True,
                            "input_sent": True,
                        },
                        error="desktop operation cancelled after input delivery",
                    )
                finally:
                    raise
            except BaseException as exc:
                return self.repository.transition_operation(
                    operation.operation_id, expected="dispatched",
                    state="unknown_effect",
                    evidence={"method": dispatch.method, "delivery_possible": True,
                              "automatic_retry": False,
                              "input_sent": True,
                              "observation_error": f"{type(exc).__name__}: {exc}"[:1000]},
                    error=f"input was delivered but fresh observation failed: {exc}"[:1000],
                )
            operation = self.repository.transition_operation(
                operation.operation_id, expected="dispatched", state="observed",
                after_observation_id=after.observation_id,
                dispatch={"selected_delivery": actual_delivery,
                          "input_sent": True,
                          "actual_method": dispatch.method,
                          "delivery_possible": True,
                          "metadata": dict(dispatch.metadata),
                          "post_action_owner_recheck": owner_rechecked,
                          "automatic_retry": False},
            )
            if after_record.window_id != record.window_id and not expectation_value:
                verification = VerificationResult("verified", {
                    "verification": "owned_modal_window_transition",
                    "before_fingerprint": before.fingerprint,
                    "after_fingerprint": after.fingerprint,
                    "observation_changed": True,
                    "readback": dispatch.readback,
                    "checks": [],
                    "related_window_id": after_record.window_id,
                })
            else:
                verification = verify_operation(
                    action=str(action), before=before, after=after, target=element,
                    expectation=expectation_value, readback=dispatch.readback,
                )
            return self.repository.transition_operation(
                operation.operation_id, expected="observed", state=verification.state,
                evidence=verification.evidence,
                error=("postcondition was not verified"
                       if verification.state == "failed" else ""),
            )

    def operations(self, **kwargs: Any) -> list[DesktopOperation]:
        return self.repository.list_operations(**kwargs)

    def events(
        self, *, after: int = 0, limit: int = 200,
        entity_kind: str = "", entity_id: str = "",
        scope: Any = None,
    ):
        return self.repository.events(
            after=after, limit=limit, entity_kind=entity_kind,
            entity_id=entity_id, scope=scope,
        )

    def event_cursor(self) -> int:
        return self.repository.event_cursor()

    async def focus_session(
        self, arguments: Mapping[str, Any], *, scope: Any = None,
    ) -> tuple[Any, WindowRecord, DesktopObservation]:
        """Bind or inspect a focused session through this Fabric's live adapter."""

        self._ensure_open()
        raw = dict(arguments)
        binding = self._binding()
        selected: WindowRecord | None = None
        if raw.get("previous"):
            for window_id in list(getattr(binding, "focus_history", ()) or ()):
                try:
                    selected = self.bind(window_id)
                    break
                except (DesktopNotFound, DesktopStaleReference):
                    if binding is not None:
                        binding.forget(window_id)
            if selected is None:
                raise DesktopNotFound("no previous durable desktop window is available")
        elif raw.get("window_id"):
            selected = self.bind(str(raw.get("window_id") or ""))
        elif not any(raw.get(key) for key in ("name", "title", "window")):
            selected = self.current_window()

        if selected is not None:
            handler = getattr(self.adapter, "focus", None)
            if not callable(handler):
                raise DesktopUnavailable("desktop adapter does not provide exact window focus")
            focused = await handler(selected)
            if not isinstance(focused, AdapterFocus):
                raise DesktopUnavailable("desktop adapter returned an invalid focus result")
            result = focused.result
        else:
            handler = getattr(self.adapter, "focus_session", None)
            if not callable(handler):
                raise DesktopUnavailable(
                    "desktop adapter does not provide named window discovery"
                )
            focused = await handler(raw)
            if not isinstance(focused, AdapterFocus):
                raise DesktopUnavailable("desktop adapter returned an invalid focus result")
            result = focused.result
        await asyncio.to_thread(self.refresh_catalog)
        focused_hwnd = (
            selected.hwnd if selected is not None
            else int(focused.hwnd or 0)
        )
        matches = [
            record for record in self.repository.list_windows(
                include_missing=False
            )
            if record.live and record.hwnd == focused_hwnd
        ]
        if len(matches) != 1:
            raise DesktopUnavailable(
                "focused window did not resolve to one durable HWND/process identity"
            )
        window_record = self.bind(matches[0])
        self._attach_binding(window_record)
        if selected is None:
            # Named discovery used disposable locator scratch. Produce the
            # returned view from the durable per-window session so its control
            # identities/generation remain usable by subsequent actions.
            focused = await self.adapter.focus(window_record)
            result = focused.result
        observation = await self._record_observation(
            window_record, focused.observation, mode="uia", include_image=False,
            scope=scope,
        )
        return result, window_record, observation

    def capability_report(self) -> dict[str, Any]:
        adapter = dict(self.adapter.capability_report())
        return {
            "schema": "variant1.desktop-fabric-capabilities.v1",
            "backend_instance_id": self.backend_instance_id,
            "durable_registry": {
                "apps": True, "windows": True, "observations": True,
                "captures": True, "operations": True, "event_cursors": True,
            },
            "identity": {"title_only": False,
                         "hwnd_pid_process_start": True,
                         "uia_runtime_automation_path": True,
                         "ambiguity_fails": True},
            "transactions": {
                "states": ["prepared", "dispatched", "observed", "verified",
                           "no_effect", "failed", "unknown_effect"],
                "physical_global_serialization": True,
                "semantic_per_window_serialization": True,
                "automatic_retry_after_possible_delivery": False,
                "exactly_once_across_backend_restart": False,
            },
            "recovery": self.recovery_report.to_dict(),
            "adapter": adapter,
            "native_helper": {
                "out_of_process": False, "uia_hang_isolation": False,
                "wgc": False, "live_handle_backend_restart_survival": False,
            },
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise DesktopUnavailable("desktop fabric is closed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.adapter.close()
        self._semantic_locks.clear()


def create_desktop_fabric(
    *, path: str | None = None, data_dir: str | None = None,
    artifact_store: Any | None = None, adapter: DesktopLiveAdapter | None = None,
    desktop_control: Any | None = None, backend_instance_id: str = "",
    reconcile: bool = True,
) -> DesktopFabric:
    """Compose Desktop Fabric without scanning or focusing the desktop."""
    owns_artifacts = artifact_store is None
    if artifact_store is None:
        from artifacts import ContentAddressedArtifactStore
        if data_dir:
            artifact_root = os.path.join(os.path.abspath(data_dir), "artifacts")
        elif path:
            artifact_root = os.path.join(os.path.dirname(os.path.abspath(path)), "artifacts")
        else:
            artifact_root = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), os.pardir,
                "data", "desktop", "artifacts",
            )
        artifact_store = ContentAddressedArtifactStore(os.path.abspath(artifact_root))
    repository = DesktopFabricRepository(path=path, data_dir=data_dir)
    instance_id = str(backend_instance_id or f"backend_{uuid.uuid4().hex}")
    raw_recovery = repository.reconcile_backend(instance_id) if reconcile else {
        "failed_before_dispatch": 0, "unknown_effect": 0, "windows_need_rebind": 0,
    }
    recovery = DesktopRecoveryReport(
        backend_instance_id=instance_id,
        failed_before_dispatch=int(raw_recovery["failed_before_dispatch"]),
        unknown_effect=int(raw_recovery["unknown_effect"]),
        windows_need_rebind=int(raw_recovery["windows_need_rebind"]),
    )
    live_adapter = adapter or WindowsDesktopAdapter(desktop_control=desktop_control)
    return DesktopFabric(
        repository=repository, artifact_store=artifact_store,
        adapter=live_adapter, backend_instance_id=instance_id,
        recovery_report=recovery, owns_artifact_store=owns_artifacts,
    )


__all__ = [
    "DesktopFabric", "DesktopRecoveryReport", "create_desktop_fabric",
]
