from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import sys

import pytest

from artifacts import ContentAddressedArtifactStore
from desktop_fabric import (
    AdapterCapture,
    AdapterDispatch,
    AdapterFocus,
    AdapterObservation,
    AppRecord,
    DesktopAmbiguousTarget,
    DesktopConflict,
    DesktopBinding,
    DesktopElement,
    DesktopFabricRepository,
    DesktopStaleReference,
    DesktopUnavailable,
    ProcessIdentity,
    WindowRecord,
    bind_desktop_binding,
    create_desktop_fabric,
)
from desktop_fabric.fusion import fuse_elements
from desktop_fabric.service import _AsyncProcessLock


@pytest.mark.asyncio
async def test_cancelled_process_lock_waiter_does_not_leak_lock():
    process_lock = _AsyncProcessLock()
    await process_lock._lock.acquire()

    async def wait_for_lock():
        async with process_lock:
            return True

    waiter = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    process_lock._lock.release()
    async with asyncio.timeout(1.0):
        async with process_lock:
            assert process_lock._lock.locked()


class FakeDesktopAdapter:
    def __init__(self) -> None:
        self.value = "before"
        self.toggle = "off"
        self.generation = 0
        self.dispatches = 0
        self.fail_after_boundary = False
        self.cancel_after_boundary = False
        self.steal_focus_after_dispatch = False
        self.foreground = True
        self.observe_modes = []
        self.live = True
        self.closed = False
        self.app = AppRecord(
            app_id="app_editor", executable=r"c:\apps\editor.exe",
            display_name="Editor", processes=(ProcessIdentity(42, 1000.25),),
            running=True,
        )
        self.window = WindowRecord(
            window_id="win_editor", app_id=self.app.app_id, hwnd=1234,
            pid=42, pid_started_at=1000.25,
            executable=self.app.executable, class_name="EditorWindow",
            title="Untitled - Editor", bounds=(10, 20, 810, 620),
            foreground=True, backend_instance_id="seed",
        )

    def catalog(self, *, backend_instance_id: str):
        return [self.app], [replace(
            self.window, backend_instance_id=backend_instance_id,
            foreground=self.foreground)]

    def validate_window(self, window):
        same = (
            self.live and window.hwnd == self.window.hwnd
            and window.pid == self.window.pid
            and window.pid_started_at == self.window.pid_started_at
        )
        return {"live": same, "reason": "identity_match" if same else "identity_changed",
                "foreground": self.foreground}

    async def observe(self, window, *, mode: str):
        self.observe_modes.append(mode)
        self.generation += 1
        return AdapterObservation(
            uia=(
                {"id": 1, "key": "aid:editor#1", "role": "Edit",
                 "name": "Document", "value": self.value,
                 "state": "", "bounds": [30, 60, 700, 550],
                 "automation_id": "editor", "runtime_id": [1, 2, 3],
                 "patterns": ["value"], "actionable": True},
                {"id": 2, "key": "aid:save#1", "role": "Button",
                 "name": "Save", "value": "", "state": "",
                 "bounds": [700, 25, 790, 55], "automation_id": "save",
                 "runtime_id": [1, 2, 4], "patterns": ["invoke"],
                 "actionable": True},
            ),
            visual=(
                {"id": 9, "key": "det:save#1", "role": "Detected",
                 "name": "Save", "bounds": [702, 26, 789, 56],
                 "confidence": 0.91, "actionable": True},
            ) if mode == "fused" else (),
            uia_generation=self.generation, completeness="uia+vlm",
            metadata={"fake": True},
        )

    async def capture(self, window):
        return AdapterCapture(
            png=b"\x89PNG\r\n\x1a\nFAKE", width=800, height=600,
            provenance="fake_visible_capture", occlusion_independent=False,
            coordinate_transform={"screen_origin": [10, 20],
                                  "capture_size": [800, 600]},
        )

    async def preflight(self, window, *, action, delivery, arguments):
        return None

    async def focus(self, window):
        self.foreground = True
        return AdapterFocus(
            result={"focused": True, "window_id": window.window_id},
            hwnd=window.hwnd,
            observation=await self.observe(window, mode="uia"),
        )

    async def dispatch(self, window, *, action, delivery, element, arguments):
        self.dispatches += 1
        if self.fail_after_boundary:
            self.value = "possibly-delivered"
            raise RuntimeError("injected adapter crash")
        if self.cancel_after_boundary:
            raise asyncio.CancelledError
        if action == "set_value":
            self.value = str(arguments.get("value") or "")
            if self.steal_focus_after_dispatch:
                self.foreground = False
            return AdapterDispatch(True, "fake.uia.value", readback=self.value)
        if action == "toggle":
            self.toggle = "on" if self.toggle == "off" else "off"
            return AdapterDispatch(True, "fake.uia.toggle", readback=self.toggle)
        return AdapterDispatch(True, "fake.uia.invoke")

    def capability_report(self):
        return {"adapter": "fake", "capture": {"occlusion_independent": False}}

    def close(self):
        self.closed = True


class FocusHistoryAdapter(FakeDesktopAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.second = replace(
            self.window,
            window_id="win_browser",
            hwnd=5678,
            class_name="BrowserWindow",
            title="Browser",
            foreground=False,
        )
        self.active_hwnd = self.window.hwnd

    def catalog(self, *, backend_instance_id: str):
        return [self.app], [
            replace(
                self.window,
                backend_instance_id=backend_instance_id,
                foreground=self.active_hwnd == self.window.hwnd,
            ),
            replace(
                self.second,
                backend_instance_id=backend_instance_id,
                foreground=self.active_hwnd == self.second.hwnd,
            ),
        ]

    def validate_window(self, window):
        known = next(
            (item for item in (self.window, self.second) if item.hwnd == window.hwnd),
            None,
        )
        return {
            "live": bool(
                known is not None
                and known.pid == window.pid
                and known.pid_started_at == window.pid_started_at
            ),
            "foreground": self.active_hwnd == window.hwnd,
            "reason": "identity_match",
        }

    async def focus(self, window):
        self.active_hwnd = window.hwnd
        return AdapterFocus(
            result={"focused": True, "window_id": window.window_id},
            hwnd=window.hwnd,
            observation=await self.observe(window, mode="uia"),
        )


@pytest.mark.asyncio
async def test_desktop_binding_owns_focus_history_and_previous(tmp_path):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    adapter = FocusHistoryAdapter()
    fabric = create_desktop_fabric(
        path=str(tmp_path / "desktop.sqlite3"),
        artifact_store=artifacts,
        adapter=adapter,
        backend_instance_id="backend-focus",
    )
    binding = DesktopBinding(binding_id="desktop_binding_focus")

    with bind_desktop_binding(binding):
        assert fabric.current_window().window_id == "win_editor"
        adapter.active_hwnd = adapter.second.hwnd
        assert fabric.current_window().window_id == "win_browser"
        assert binding.focus_history == ["win_editor"]

        result, window, observation = await fabric.focus_session({"previous": True})

        assert result["focused"] is True
        assert window.window_id == "win_editor"
        assert observation.window_id == "win_editor"
        assert binding.active_window_id == "win_editor"
        assert binding.focus_history[0] == "win_browser"

    fabric.close()


def runtime(tmp_path, adapter=None, *, backend="backend_a", reconcile=True):
    fake = adapter or FakeDesktopAdapter()
    fabric = create_desktop_fabric(
        path=str(tmp_path / "desktop.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
        adapter=fake, backend_instance_id=backend, reconcile=reconcile,
    )
    fabric.refresh_catalog()
    return fabric, fake


def test_catalog_uses_strong_identity_and_persists_event_cursor(tmp_path):
    fabric, fake = runtime(tmp_path)
    apps = fabric.apps(query="editor")
    windows = fabric.windows(app=apps[0])
    assert windows[0].strong_identity == {
        "hwnd": 1234, "pid": 42, "pid_started_at": 1000.25,
        "executable": r"c:\apps\editor.exe", "class_name": "EditorWindow",
        "package_family": "",
    }
    assert fabric.bind(windows[0]).window_id == "win_editor"
    assert fabric.events(after=0)
    first_cursor = fabric.repository.event_cursor()
    fabric.refresh_catalog()
    assert fabric.events(after=first_cursor)
    fake.live = False
    with pytest.raises(DesktopStaleReference):
        fabric.bind(windows[0])


def test_catalog_marks_disappeared_apps_and_invalidates_window_generation(tmp_path):
    fabric, fake = runtime(tmp_path)
    original = fabric.bind("win_editor")
    fake.catalog = lambda **_kwargs: ([], [])
    fabric.refresh_catalog()
    stopped = fabric.repository.get_app("app_editor")
    missing = fabric.repository.get_window("win_editor")
    assert stopped.running is False
    assert stopped.processes == ()
    assert missing.live is False
    assert missing.generation == original.generation + 1
    with pytest.raises(DesktopStaleReference):
        fabric.bind(missing)


@pytest.mark.asyncio
async def test_fused_observation_records_capture_artifact_and_provenance(tmp_path):
    fabric, _fake = runtime(tmp_path)
    observation = await fabric.observe("win_editor", mode="fused", include_image=True)
    assert observation.image_ref.startswith("artifact://sha256/")
    assert observation.event_cursor > 0
    assert observation.completeness == "uia+vlm"
    save = observation.one(role="Button", name="Save")
    assert save.provenance == ("uia", "vlm")
    assert save.patterns == ("invoke",)
    capture = fabric.repository.get_capture(observation.capture_id)
    assert capture.provenance == "fake_visible_capture"
    assert capture.occlusion_independent is False
    assert fabric.artifact_store.read_bytes(capture.artifact_ref).startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_semantic_action_has_durable_verified_lifecycle_and_idempotency(tmp_path):
    fabric, fake = runtime(tmp_path)
    initial = await fabric.observe("win_editor", include_image=False)
    editor = initial.one(role="Edit")
    result = await fabric.act(
        "win_editor", "set_value", target=editor, delivery="semantic",
        arguments={"value": "Persistent IPython workflow"},
        expect={"property": "value", "equals": "Persistent IPython workflow"},
        scope={"workspace_id": "ws1", "kernel_generation": 3},
        idempotency_key="set-editor-once", include_image_evidence=False,
    )
    assert result.state == "verified"
    assert result.dispatch["actual_method"] == "fake.uia.value"
    assert result.scope.workspace_id == "ws1"
    assert fake.dispatches == 1
    event_types = [event.event_type for event in fabric.events(
        entity_kind="operation", entity_id=result.operation_id)]
    assert event_types == [
        "operation.prepared", "operation.dispatched", "operation.observed",
        "operation.verified",
    ]

    duplicate = await fabric.act(
        "win_editor", "set_value", target=editor, delivery="semantic",
        arguments={"value": "Persistent IPython workflow"},
        expect={"property": "value", "equals": "Persistent IPython workflow"},
        scope={"workspace_id": "ws1", "kernel_generation": 3},
        idempotency_key="set-editor-once", include_image_evidence=False,
    )
    assert duplicate.operation_id == result.operation_id
    assert duplicate.state == "verified"
    assert fake.dispatches == 1


@pytest.mark.asyncio
async def test_unique_exact_control_name_is_a_valid_string_target(tmp_path):
    fabric, fake = runtime(tmp_path)

    result = await fabric.act(
        "win_editor",
        "set_value",
        target="Document",
        delivery="semantic",
        arguments={"value": "resolved by exact name"},
        expect={"property": "value", "equals": "resolved by exact name"},
        include_image_evidence=False,
    )

    assert result.state == "verified"
    assert fake.dispatches == 1


@pytest.mark.asyncio
async def test_explicit_uia_target_does_not_invoke_visual_observation(tmp_path):
    fabric, fake = runtime(tmp_path)

    result = await fabric.act(
        "win_editor", "set_value", target={"role": "Edit"},
        delivery="semantic", arguments={"value": "UIA only"},
        expect={"property": "value", "equals": "UIA only"},
        include_image_evidence=True,
    )

    assert result.state == "verified"
    assert fake.observe_modes == ["uia", "uia"]


@pytest.mark.asyncio
async def test_changed_idempotent_request_conflicts_without_second_delivery(tmp_path):
    fabric, fake = runtime(tmp_path)
    await fabric.act(
        "win_editor", "set_value", target={"role": "Edit"}, delivery="semantic",
        arguments={"value": "one"}, idempotency_key="same-key",
        include_image_evidence=False,
    )
    with pytest.raises(DesktopConflict):
        await fabric.act(
            "win_editor", "set_value", target={"role": "Edit"}, delivery="semantic",
            arguments={"value": "two"}, idempotency_key="same-key",
            include_image_evidence=False,
        )
    assert fake.dispatches == 1


@pytest.mark.asyncio
async def test_possible_delivery_error_becomes_unknown_and_is_not_retried(tmp_path):
    fake = FakeDesktopAdapter()
    fake.fail_after_boundary = True
    fabric, fake = runtime(tmp_path, fake)
    result = await fabric.act(
        "win_editor", "set_value", target={"role": "Edit"},
        delivery="semantic", arguments={"value": "new"},
        idempotency_key="crash-once", include_image_evidence=False,
    )
    assert result.state == "unknown_effect"
    assert result.evidence["automatic_retry"] is False
    assert fake.dispatches == 1
    again = await fabric.act(
        "win_editor", "set_value", target={"role": "Edit"},
        delivery="semantic", arguments={"value": "new"},
        idempotency_key="crash-once", include_image_evidence=False,
    )
    assert again.operation_id == result.operation_id
    assert fake.dispatches == 1


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_is_persisted_and_propagated(tmp_path):
    fake = FakeDesktopAdapter()
    fake.cancel_after_boundary = True
    fabric, _ = runtime(tmp_path, fake)

    with pytest.raises(asyncio.CancelledError):
        await fabric.act(
            "win_editor", "set_value", target={"role": "Edit"},
            delivery="semantic", arguments={"value": "new"},
            idempotency_key="cancel-once", include_image_evidence=False,
        )

    rows = fabric.operations(limit=10)
    assert len(rows) == 1
    assert rows[0].state == "unknown_effect"
    assert rows[0].evidence["cancelled_during_dispatch"] is True
    assert fake.dispatches == 1


@pytest.mark.asyncio
async def test_physical_focus_steal_cannot_be_reported_as_success(tmp_path):
    fake = FakeDesktopAdapter()
    fake.steal_focus_after_dispatch = True
    fabric, fake = runtime(tmp_path, fake)
    result = await fabric.act(
        "win_editor", "set_value", target={"role": "Edit"},
        delivery="physical", arguments={"value": "new"},
        include_image_evidence=False,
    )
    assert result.state == "unknown_effect"
    assert result.evidence["focus_stolen"] is True
    assert fake.dispatches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("expectation", [{}, {"readback_equals": "saved"}])
async def test_same_process_modal_becomes_the_refreshed_action_window(tmp_path, expectation):
    class ModalAdapter(FakeDesktopAdapter):
        def __init__(self):
            super().__init__()
            self.modal_open = False
            self.modal = replace(
                self.window,
                window_id="win_modal",
                hwnd=4321,
                title="Editor Dialog",
                owner_hwnd=self.window.hwnd,
                root_owner_hwnd=self.window.hwnd,
                foreground=True,
            )

        def catalog(self, *, backend_instance_id: str):
            parent = replace(
                self.window,
                backend_instance_id=backend_instance_id,
                foreground=not self.modal_open,
            )
            rows = [parent]
            if self.modal_open:
                rows.append(replace(
                    self.modal, backend_instance_id=backend_instance_id))
            return [self.app], rows

        def validate_window(self, window):
            live = window.hwnd in {self.window.hwnd, self.modal.hwnd}
            return {
                "live": live,
                "reason": "identity_match" if live else "identity_changed",
                "foreground": (
                    window.hwnd == self.modal.hwnd
                    if self.modal_open else window.hwnd == self.window.hwnd
                ),
            }

        def post_action_target(self, window):
            return {
                "live": True,
                "foreground": not self.modal_open,
                "accepted": True,
                "foreground_hwnd": self.modal.hwnd if self.modal_open else window.hwnd,
                "foreground_pid": window.pid,
                "owned_modal_transition": self.modal_open,
            }

        async def dispatch(self, window, *, action, delivery, element, arguments):
            self.dispatches += 1
            self.modal_open = True
            return AdapterDispatch(
                True, "fake.uia.invoke",
                metadata={"selected_delivery": "semantic"},
            )

    fabric, fake = runtime(tmp_path, ModalAdapter())
    result = await fabric.act(
        "win_editor",
        "click",
        target={"role": "Button", "name": "Save"},
        delivery="auto",
        include_image_evidence=False,
        expect=expectation,
    )

    assert result.state == ("failed" if expectation else "verified")
    assert result.evidence["verification"] == (
        "explicit_expectation_not_satisfied" if expectation else "owned_modal_window_transition")
    after = fabric.repository.get_observation(result.after_observation_id)
    assert after.window_id == "win_modal"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_post_dispatch_window_failure_settles_receipt(tmp_path, monkeypatch, cancelled):
    fabric, fake = runtime(tmp_path)
    async def fail(window):
        if cancelled:
            raise asyncio.CancelledError()
        raise OSError("window disappeared")
    monkeypatch.setattr(fabric, "_post_action_window", fail)
    action = fabric.act("win_editor", "click", target={"role": "Button", "name": "Save"},
                        include_image_evidence=False)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await action
    else:
        assert (await action).state == "unknown_effect"
    assert fake.dispatches == 1
    assert fabric.operations()[0].state == "unknown_effect"


def test_missing_desktop_value_does_not_verify_as_an_empty_string():
    from desktop_fabric.verification import verify_operation
    before = SimpleNamespace(fingerprint="old", elements=())
    after = SimpleNamespace(fingerprint="new", elements=())
    target = SimpleNamespace(element_ref="missing")
    result = verify_operation(action="set_value", before=before, after=after, target=target,
                              expectation={"property": "value", "equals": ""})
    assert result.state == "failed"


def test_restart_reconciliation_discloses_pre_and_post_dispatch(tmp_path):
    fabric_a, _fake = runtime(tmp_path, backend="backend_a")
    window = fabric_a.bind("win_editor")
    prepared = fabric_a.repository.prepare_operation(
        window=window, scope=None, action="click", delivery="semantic",
        backend_instance_id="backend_a",
    )
    dispatched = fabric_a.repository.prepare_operation(
        window=window, scope=None, action="toggle", delivery="semantic",
        backend_instance_id="backend_a",
    )
    dispatched = fabric_a.repository.transition_operation(
        dispatched.operation_id, expected="prepared", state="dispatched")

    fabric_b = create_desktop_fabric(
        path=str(tmp_path / "desktop.sqlite3"),
        artifact_store=fabric_a.artifact_store, adapter=FakeDesktopAdapter(),
        backend_instance_id="backend_b", reconcile=True,
    )
    assert fabric_b.repository.get_operation(prepared.operation_id).state == "failed"
    recovered = fabric_b.repository.get_operation(dispatched.operation_id)
    assert recovered.state == "unknown_effect"
    assert recovered.evidence["automatic_retry"] is False
    report = fabric_b.recovery_report
    assert report.failed_before_dispatch == 1
    assert report.unknown_effect == 1
    assert report.windows_need_rebind == 1


def test_fusion_leaves_ambiguous_visual_node_explicit():
    elements = fuse_elements(
        uia=(
            {"id": 1, "role": "Button", "name": "Run",
             "bounds": [0, 0, 100, 50], "actionable": True},
            {"id": 2, "role": "Button", "name": "Run",
             "bounds": [100, 0, 200, 50], "actionable": True},
        ),
        visual=({"id": 3, "name": "Run", "bounds": [80, 0, 120, 50],
                 "confidence": 0.8, "actionable": True},),
        observation_id="obs", window_id="win", window_generation=1,
        element_generation=1,
    )
    assert len(elements) == 3
    assert elements[-1].provenance == ("vlm",)


def test_capabilities_do_not_claim_unimplemented_native_survival(tmp_path):
    fabric, _fake = runtime(tmp_path)
    report = fabric.capability_report()
    assert report["native_helper"]["wgc"] is False
    assert report["native_helper"]["out_of_process"] is False
    assert report["native_helper"]["live_handle_backend_restart_survival"] is False
    assert report["transactions"]["automatic_retry_after_possible_delivery"] is False
    fabric.close()
    assert fabric.adapter.closed is True


@pytest.mark.asyncio
async def test_action_locks_serialize_semantic_per_window_and_physical_globally(tmp_path):
    class Probe:
        active = 0
        maximum = 0

    class SlowAdapter(FakeDesktopAdapter):
        def __init__(self, probe):
            super().__init__()
            self.probe = probe

        async def dispatch(self, *args, **kwargs):
            self.probe.active += 1
            self.probe.maximum = max(self.probe.maximum, self.probe.active)
            try:
                await asyncio.sleep(0.03)
                return await super().dispatch(*args, **kwargs)
            finally:
                self.probe.active -= 1

    semantic_probe = Probe()
    semantic, _ = runtime(tmp_path / "semantic", SlowAdapter(semantic_probe))
    await asyncio.gather(*(
        semantic.act(
            "win_editor", "set_value", target={"role": "Edit"},
            delivery="semantic", arguments={"value": str(index)},
            include_image_evidence=False,
        ) for index in range(2)
    ))
    assert semantic_probe.maximum == 1

    physical_probe = Probe()
    left, _ = runtime(tmp_path / "left", SlowAdapter(physical_probe))
    right, _ = runtime(tmp_path / "right", SlowAdapter(physical_probe))
    await asyncio.gather(
        left.act(
            "win_editor", "set_value", target={"role": "Edit"},
            delivery="physical", arguments={"value": "left"},
            include_image_evidence=False,
        ),
        right.act(
            "win_editor", "set_value", target={"role": "Edit"},
            delivery="physical", arguments={"value": "right"},
            include_image_evidence=False,
        ),
    )
    assert physical_probe.maximum == 1
