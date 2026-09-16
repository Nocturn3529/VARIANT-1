from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

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
    WindowsDesktopAdapter,
    ProcessIdentity,
    WindowRecord,
    bind_desktop_binding,
    create_desktop_fabric,
)
from desktop_fabric.fusion import fuse_elements
from desktop_fabric.service import _AsyncProcessLock
from desktop.runtime import DesktopRuntime
from desktop import targeting as desktop_targeting


def test_exact_window_priming_never_reuses_a_completed_null_rehydrate_state():
    class Catalog:
        @staticmethod
        def validate(_window):
            return {"live": True, "foreground": True}

    window = WindowRecord(
        window_id="win_exact",
        app_id="app_exact",
        hwnd=99,
        pid=7,
        pid_started_at=10.0,
        title="Exact",
    )
    control = type("Control", (), {"runtime": DesktopRuntime()})()
    adapter = WindowsDesktopAdapter(desktop_control=control, catalog=Catalog())
    session = adapter._session(window.window_id)
    session.target_window = object()
    session.rehydrate_state = {"attempted": True, "status": "success"}

    primed = adapter._prime_exact(window)

    assert primed.target_window is None
    assert primed.rehydrate_state == {}
    assert primed.window_stack.get_current().hwnd == window.hwnd
    assert primed.scope["fabric_strict_hwnd"] == window.hwnd
    assert primed.scope["fabric_strict_pid"] == window.pid


def test_source_geometry_change_is_stale_before_pointer_dispatch(monkeypatch):
    class Catalog:
        @staticmethod
        def validate(_window):
            return {"live": True, "foreground": True}

    window = WindowRecord(
        window_id="win_exact", app_id="app_exact", hwnd=99, pid=7,
        pid_started_at=10.0, title="Exact",
    )
    control = type("Control", (), {"runtime": DesktopRuntime()})()
    adapter = WindowsDesktopAdapter(desktop_control=control, catalog=Catalog())
    observation = SimpleNamespace(capture={
        "coordinate_transform": {"window_rect": [0, 0, 800, 600]},
    })
    monkeypatch.setattr(
        "desktop.vision_capture.win32_window_rect",
        lambda _hwnd: (100, 100, 900, 700),
    )

    with pytest.raises(DesktopStaleReference, match="geometry changed"):
        adapter.validate_source_geometry(window, observation)


def test_window_rehydrate_matching_normalizes_invisible_title_characters():
    rectangle = type(
        "Rectangle",
        (),
        {"left": 0, "top": 0, "right": 800, "bottom": 600},
    )()
    window = type(
        "Window",
        (),
        {
            "Name": "WebSocket\u00a0Server\u200b Settings",
            "ClassName": "QtWindow",
            "ProcessId": 42,
            "ControlTypeName": "WindowControl",
            "BoundingRectangle": rectangle,
        },
    )()
    root = type("Root", (), {"GetChildren": lambda self: [window]})()
    auto = type("Auto", (), {"GetRootControl": lambda self: root})()
    context = type(
        "Context",
        (),
        {"_is_skippable": lambda self, _name, _class_name, _pid: False},
    )()

    selected, titles = desktop_targeting.find_window(
        context,
        auto,
        "WebSocket Server Settings",
    )

    assert selected is not None
    assert selected["ctrl"] is window
    assert titles == ["WebSocket\u00a0Server\u200b Settings"]


def test_exact_hwnd_lookup_finds_an_owned_dialog_outside_desktop_children():
    dialog = SimpleNamespace(NativeWindowHandle=22, ProcessId=7)
    parent = SimpleNamespace(NativeWindowHandle=11, GetChildren=lambda: [dialog])
    looked_up = []
    auto = SimpleNamespace(
        GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [parent]),
        ControlFromHandle=lambda hwnd: looked_up.append(hwnd) or dialog,
    )

    assert desktop_targeting.find_window_by_hwnd(None, auto, 22) is dialog
    assert looked_up == [22]


@pytest.mark.parametrize("direct_result", [None, SimpleNamespace(NativeWindowHandle=99)])
def test_exact_hwnd_lookup_rejects_a_different_direct_result(direct_result):
    correct = SimpleNamespace(NativeWindowHandle=22)
    auto = SimpleNamespace(
        ControlFromHandle=lambda _hwnd: direct_result,
        GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [correct]),
    )

    assert desktop_targeting.find_window_by_hwnd(None, auto, 22) is correct
    assert desktop_targeting.find_window_by_hwnd(None, auto, 33) is None


def test_exact_hwnd_lookup_preserves_fallback_when_direct_uia_lookup_fails():
    correct = SimpleNamespace(NativeWindowHandle=22)

    def unavailable(_hwnd):
        raise OSError("provider unavailable")

    auto = SimpleNamespace(
        ControlFromHandle=unavailable,
        GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [correct]),
    )
    assert desktop_targeting.find_window_by_hwnd(None, auto, 22) is correct


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
async def test_legacy_bridge_seeds_hwnd_rebind_instead_of_title_query():
    from desktop.runtime import DesktopRuntime

    class Control:
        def __init__(self):
            self.runtime = DesktopRuntime()
            self.calls = []

        async def focus_window(self, args):
            self.calls.append(dict(args))

    class Catalog:
        def validate(self, window):
            return {"live": True, "foreground": True, "reason": "identity_match"}

        def available(self):
            return True

    control = Control()
    bridge = WindowsDesktopAdapter(desktop_control=control, catalog=Catalog())
    window = FakeDesktopAdapter().window
    session = await bridge._focus_exact(window)
    assert control.calls == [{}]
    assert session.active_window["hwnd"] == window.hwnd


def test_actionable_pattern_probe_accepts_working_chromium_getter():
    from desktop_fabric.adapter import _patterns

    class InvokePattern:
        pass

    class ChromiumButton:
        def IsInvokePatternAvailable(self):
            return False

        def GetInvokePattern(self):
            return InvokePattern()

    assert "invoke" in _patterns(ChromiumButton(), actionable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery", "bounds", "expected"),
    [
        ("auto", (40, 50, 140, 90), "physical"),
        ("auto", None, "semantic"),
        ("semantic", (40, 50, 140, 90), "semantic"),
    ],
)
async def test_click_routing_prefers_a_bounded_physical_target(
    delivery, bounds, expected,
):
    class RoutingAdapter(WindowsDesktopAdapter):
        def __init__(self):
            super().__init__(desktop_control=object(), catalog=object())
            self.foreground_checks = []

        def _assert_live(self, _window, *, foreground=False):
            self.foreground_checks.append(foreground)

        async def _physical_dispatch(self, _window, **_kwargs):
            return AdapterDispatch(True, "physical.pointer")

        async def _semantic_dispatch(self, _window, **_kwargs):
            return AdapterDispatch(True, "uia.invoke")

    adapter = RoutingAdapter()
    window = FakeDesktopAdapter().window
    element = DesktopElement(
        element_ref="element-save",
        observation_id="observation-1",
        window_id=window.window_id,
        window_generation=window.generation,
        element_generation=1,
        role="Button",
        name="Save",
        patterns=("invoke",),
        bounds=bounds,
    )

    result = await adapter.dispatch(
        window,
        action="click",
        delivery=delivery,
        element=element,
        arguments={},
    )

    assert result.metadata["selected_delivery"] == expected
    assert result.method == (
        "physical.pointer" if expected == "physical" else "uia.invoke"
    )
    # Desktop Fabric's strong window identity is the sole authority. Physical
    # delivery may activate that exact HWND but no longer passes through a
    # second foreground-policy veto.
    assert adapter.foreground_checks == [False]


class _PhysicalValueControl:
    def __init__(
        self,
        *,
        control_type="EditControl",
        automation_id="editor",
        runtime_id=(42, 1),
        parent=None,
        keyboard_focusable=True,
        has_keyboard_focus=True,
        enabled=True,
        offscreen=False,
    ):
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.IsKeyboardFocusable = keyboard_focusable
        self.HasKeyboardFocus = has_keyboard_focus
        self.IsEnabled = enabled
        self.IsOffscreen = offscreen
        self._runtime_id = tuple(runtime_id)
        self._parent = parent

    def Exists(self, _max_search=0, _interval=0):
        return True

    def GetRuntimeId(self):
        return list(self._runtime_id)

    def GetParentControl(self):
        return self._parent


def _physical_value_harness(
    monkeypatch,
    *,
    target_control,
    focused_control,
):
    calls = []

    class Auto:
        def Click(self, x, y):
            calls.append(("click", x, y))

        def SendKeys(self, keys, waitTime=0):
            calls.append(("keys", keys, waitTime))

        def KeyboardInput(self, vk, scan, flags):
            return vk, scan, flags

        def SendInput(self, *events):
            value = b"".join(
                scan.to_bytes(2, "little")
                for _vk, scan, flags in events if not flags & 2
            ).decode("utf-16-le")
            if calls and calls[-1][0] == "literal_text":
                calls[-1] = ("literal_text", calls[-1][1] + value)
            else:
                calls.append(("literal_text", value))
            return len(events)

        def GetFocusedControl(self):
            return focused_control

    auto = Auto()
    import desktop.input_primitives as input_primitives
    monkeypatch.setattr(input_primitives, "_send_input_batch", lambda events: auto.SendInput(*events))

    class Context:
        def __init__(self, _session):
            pass

        def _load_uia(self):
            return auto

        async def _uia(self, fn):
            return fn()

    class User32:
        @staticmethod
        def WindowFromPoint(_point):
            return 1234

        @staticmethod
        def GetAncestor(hwnd, _kind):
            return hwnd

        @staticmethod
        def GetForegroundWindow():
            return 1234

        @staticmethod
        def GetWindowThreadProcessId(_hwnd, pointer):
            pointer._obj.value = 42
            return 1

    import ctypes
    import desktop.context as desktop_context

    monkeypatch.setattr("desktop.vision_capture.win32_window_rect", lambda _hwnd: (0, 0, 1000, 800))

    monkeypatch.setattr(desktop_context, "DesktopControlContext", Context)
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=User32()),
    )

    session = SimpleNamespace(last_snapshot={
        1: {
            "id": 1,
            "key": "aid:" + str(target_control.AutomationId),
            "control": target_control,
        },
    })

    class Adapter(WindowsDesktopAdapter):
        def _assert_live(self, _window, *, foreground=False):
            return {"live": True, "foreground": True}

        @contextmanager
        def _bound(self, _window_id):
            yield session

    adapter = Adapter(desktop_control=object(), catalog=object())
    window = FakeDesktopAdapter().window
    element = DesktopElement(
        element_ref="element-editor",
        observation_id="observation-1",
        window_id=window.window_id,
        window_generation=window.generation,
        element_generation=1,
        role=str(target_control.ControlTypeName).removesuffix("Control"),
        name="Document",
        automation_id=str(target_control.AutomationId),
        runtime_id=tuple(target_control.GetRuntimeId()),
        backend_key="1",
        bounds=(30, 60, 700, 550),
    )
    return adapter, window, element, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["literal {value}", ""])
async def test_physical_value_replacement_requires_editable_focus_and_avoids_delete(
    monkeypatch,
    value,
):
    target = _PhysicalValueControl()
    adapter, window, element, calls = _physical_value_harness(
        monkeypatch,
        target_control=target,
        focused_control=target,
    )

    result = await adapter.dispatch(
        window,
        action="set_value",
        delivery="auto",
        element=element,
        arguments={"value": value},
    )

    assert result.delivered is True
    assert result.method == "physical.pointer_keyboard"
    assert result.readback is None
    assert result.metadata["activation_confirmed"] is True
    assert result.metadata["selected_delivery"] == "physical"
    expected = [
        ("click", 365, 305),
        ("keys", "{Ctrl}a", 0),
    ]
    expected.append(
        ("literal_text", value)
        if value
        else ("keys", "{Back}", 0)
    )
    assert calls == expected


@pytest.mark.asyncio
async def test_physical_set_value_accepts_actual_focus_when_focusable_flag_is_false(
    monkeypatch,
):
    # Windows' common Save As dialog can report IsKeyboardFocusable=False for
    # its filename Edit even while that exact control has keyboard focus. The
    # observed focus state is authoritative; the contradictory advisory flag
    # must not turn a valid owner/modal-child target into a false refusal.
    target = _PhysicalValueControl(
        automation_id="1001",
        runtime_id=(42, 33818852),
        keyboard_focusable=False,
        has_keyboard_focus=True,
    )
    adapter, window, element, calls = _physical_value_harness(
        monkeypatch,
        target_control=target,
        focused_control=target,
    )

    result = await adapter.dispatch(
        window,
        action="set_value",
        delivery="physical",
        element=element,
        arguments={"value": "owned-modal-file.txt"},
    )

    assert result.delivered is True
    assert result.method == "physical.pointer_keyboard"
    assert calls == [
        ("click", 365, 305),
        ("keys", "{Ctrl}a", 0),
        ("literal_text", "owned-modal-file.txt"),
    ]


@pytest.mark.asyncio
async def test_physical_set_value_refuses_file_list_item_before_keyboard_input(
    monkeypatch,
):
    list_item = _PhysicalValueControl(
        control_type="ListItemControl",
        automation_id="file-row",
        runtime_id=(42, 2),
        has_keyboard_focus=False,
    )
    target = _PhysicalValueControl(
        automation_id="System.ItemNameDisplay",
        runtime_id=(42, 3),
        parent=list_item,
    )
    adapter, window, element, calls = _physical_value_harness(
        monkeypatch,
        target_control=target,
        focused_control=target,
    )

    with pytest.raises(DesktopUnavailable, match="file/list item"):
        await adapter.dispatch(
            window,
            action="set_value",
            delivery="auto",
            element=element,
            arguments={"value": "C:\\some\\other\\file.txt"},
        )

    assert calls == []


@pytest.mark.asyncio
async def test_physical_set_value_stops_after_click_when_editable_focus_is_unproven(
    monkeypatch,
):
    target = _PhysicalValueControl(has_keyboard_focus=False)
    unrelated = _PhysicalValueControl(
        control_type="ButtonControl",
        automation_id="unrelated",
        runtime_id=(42, 9),
    )
    adapter, window, element, calls = _physical_value_harness(
        monkeypatch,
        target_control=target,
        focused_control=unrelated,
    )

    with pytest.raises(DesktopUnavailable, match="editable keyboard focus"):
        await adapter.dispatch(
            window,
            action="set_value",
            delivery="auto",
            element=element,
            arguments={"value": "replacement"},
        )

    assert calls == [("click", 365, 305)]


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
