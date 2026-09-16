from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import desktop_control
from artifacts import ContentAddressedArtifactStore
from capability_broker import CapabilityBroker, InvocationContext
from desktop import registry as desktop_registry
from desktop_fabric import (
    AdapterCapture,
    AdapterDispatch,
    AdapterFocus,
    AdapterObservation,
    AppRecord,
    ProcessIdentity,
    WindowRecord,
    create_desktop_fabric,
    install_desktop_fabric,
)
from tools import ToolRegistry
from work_fabric.scope import WorkScope
from tests.support.astb_runtime import StaticRuntimeRegistry


def test_compact_control_preserves_observed_empty_value_and_selection_state():
    from desktop_fabric.capabilities import _compact_element
    from desktop_fabric.models import DesktopElement

    element = DesktopElement(
        element_ref="field", observation_id="obs", window_id="win",
        window_generation=1, element_generation=1, role="Edit", name="Query",
        value="", patterns=("value",), states={"offscreen": False, "state": "selected"},
        backend_key="7",
    )
    control = _compact_element(element)
    assert control["value"] == ""
    assert control["offscreen"] is False
    assert control["state"] == "selected"
    assert control["id"] == 7 and control["ref"] == "field"
    assert _compact_element(replace(element, patterns=(), states={}))["value"] is None


class FakeAdapter:
    def __init__(self) -> None:
        self.value = "before"
        self.generation = 0
        self.observes = 0
        self.captures = 0
        self.dispatches: list[dict] = []
        self.app = AppRecord(
            app_id="app_editor",
            executable=r"c:\apps\editor.exe",
            display_name="Editor",
            running=True,
            processes=(ProcessIdentity(41, 100.5),),
        )
        self.window = WindowRecord(
            window_id="win_editor",
            app_id=self.app.app_id,
            hwnd=123,
            pid=41,
            pid_started_at=100.5,
            executable=self.app.executable,
            class_name="EditorWindow",
            title="Editor",
            bounds=(100, 200, 900, 800),
            foreground=True,
        )

    def catalog(self, *, backend_instance_id: str):
        return [self.app], [replace(
            self.window, backend_instance_id=backend_instance_id)]

    def validate_window(self, window):
        return {
            "live": (
                window.hwnd == self.window.hwnd
                and window.pid == self.window.pid
                and window.pid_started_at == self.window.pid_started_at
            ),
            "foreground": True,
            "reason": "identity_match",
        }

    def post_action_target(self, window):
        return {**self.validate_window(window), "accepted": True}

    async def observe(self, window, *, mode: str):
        self.observes += 1
        self.generation += 1
        return AdapterObservation(
            uia=(
                {
                    "id": 1,
                    "key": "aid:editor#1",
                    "role": "Edit",
                    "name": "Document",
                    "value": self.value,
                    "bounds": [110, 240, 880, 780],
                    "automation_id": "editor",
                    "runtime_id": [1, 2, 3],
                    "patterns": ["value"],
                    "actionable": True,
                },
                {
                    "id": 2,
                    "key": "aid:save#1",
                    "role": "Button",
                    "name": "Save",
                    "bounds": [800, 205, 890, 235],
                    "automation_id": "save",
                    "runtime_id": [1, 2, 4],
                    "patterns": ["invoke"],
                    "actionable": True,
                },
            ),
            uia_generation=self.generation,
            completeness="uia-only",
            metadata={"window_rect": list(self.window.bounds)},
        )

    async def capture(self, window):
        self.captures += 1
        return AdapterCapture(
            png=b"\x89PNG\r\n\x1a\nCAP",
            width=800,
            height=600,
            provenance="fake_visible",
            coordinate_transform={
                "screen_origin": [100, 200],
                "capture_size": [800, 600],
            },
        )

    async def preflight(self, window, *, action, delivery, arguments):
        return None

    async def dispatch(self, window, *, action, delivery, element, arguments):
        actual = "semantic" if element is not None else "physical"
        self.dispatches.append({
            "action": action,
            "delivery": delivery,
            "actual": actual,
            "element": element,
            "arguments": dict(arguments),
        })
        if action == "set_value":
            self.value = str(arguments.get("value") or "")
            readback = self.value
        elif action == "click":
            self.value = "clicked"
            readback = None
        else:
            readback = None
        return AdapterDispatch(
            True,
            f"fake.{actual}",
            readback=readback,
            metadata={"selected_delivery": actual},
        )

    async def focus(self, window):
        return AdapterFocus(
            result={"focused": True},
            hwnd=window.hwnd,
            observation=await self.observe(window, mode="uia"),
        )

    async def focus_session(self, arguments):
        return await self.focus(self.window)

    def capability_report(self):
        return {"adapter": "fake"}

    def close(self):
        return None


def _stack(tmp_path, *, with_artifacts=False):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    adapter = FakeAdapter()
    fabric = create_desktop_fabric(
        path=str(tmp_path / "desktop.sqlite3"),
        artifact_store=artifacts,
        adapter=adapter,
        backend_instance_id="backend-test",
    )
    registry = ToolRegistry()
    enabled: set[str] = set()
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: set(enabled),
        artifact_store=artifacts,
    )
    runtime = SimpleNamespace(
        desktop=fabric,
        registry=registry,
        broker=broker,
        session_artifacts=artifacts,
    )
    control = desktop_control.DesktopControl()
    host = SimpleNamespace(
        desktop_control=control,
        require_runtime=lambda: runtime,
    )
    install_desktop_fabric(fabric, host)
    desktop_registry.register(registry, control)
    if with_artifacts:
        from artifacts.blob_service import ArtifactBlobService
        from artifacts.capabilities import register_artifact_tools
        from work_fabric.capabilities import register_work_fabric_tools

        runtime.artifacts = SimpleNamespace(blobs=ArtifactBlobService(artifacts))
        host.remote_handle_routers = {}
        register_work_fabric_tools(host)
        register_artifact_tools(host)
    enabled.update(tool.name for tool in registry.all())
    context = InvocationContext(
        chat_id="chat-1",
        run_id="run-1",
        outer_tool_call_id="outer-1",
        cell_execution_id="cell-1",
        nested_call_id="nested-1",
        surface="ipython",
        catalog_release_id="catalog-1",
        work_scope=WorkScope(
            chat_id="chat-1",
            workspace_id="workspace-1",
            kernel_generation=2,
        ),
    )
    return fabric, adapter, broker, context


async def _call(broker, context, operation, **arguments):
    _call.sequence = getattr(_call, "sequence", 0) + 1
    receipt = await broker.invoke_name(
        "computer",
        {"operation": operation, **arguments},
        replace(context, nested_call_id=f"nested-{operation}-{_call.sequence}"),
    )
    assert receipt.ok, receipt.to_dict()
    return receipt.result_value


@pytest.mark.asyncio
async def test_window_listing_and_focus_return_plain_mapping_views(tmp_path):
    _fabric, _adapter, broker, context = _stack(tmp_path)

    windows = await _call(broker, context, "list_windows")
    assert windows == [{
        "window_id": "win_editor",
        "app": "app_editor",
        "title": "Editor",
        "hwnd": 123,
        "pid": 41,
        "bounds": [100, 200, 900, 800],
        "foreground": True,
        "minimized": False,
        "live": True,
    }]

    view = await _call(broker, context, "focus", window=windows[0])
    assert view["schema"] == "variant1.desktop-view-result.v2"
    assert view["window"] == windows[0]
    assert view["text_included"] is True
    assert [item["name"] for item in view["controls"]] == ["Document", "Save"]
    assert view["coordinate_space"] == "window"
    assert view["controls"][0]["bounds"] == [10, 40, 780, 580]
    assert "$variant1_handle" not in str(view)


@pytest.mark.asyncio
async def test_observe_selects_screenshot_and_text_without_grounding_api(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path, with_artifacts=True)
    focused = await _call(broker, context, "focus", name="Editor")

    visual = await _call(
        broker, context, "observe",
        window=focused["window"], include_screenshot=True, include_text=False,
    )
    assert visual["screenshot_included"] is True
    assert visual["screenshot"]["width"] == 800
    assert visual["controls"] == []
    assert 'include_text=True' in visual['controls_hint']
    assert _fabric.artifact_store.read_bytes_scoped(visual['screenshot']['image_ref'], context.chat_id).startswith(b'\x89PNG')
    image = visual["image"]["$variant1_handle"]
    assert image["metadata"]["ref"] == visual["screenshot"]["image_ref"]
    assert image["metadata"]["size"] == len(b"\x89PNG\r\n\x1a\nCAP")
    saved = await broker.invoke_name(
        "remote_handle_dispatch",
        {"handle": {key: image[key] for key in ("service", "kind", "id", "generation", "revision")},
         "method": "save", "arguments": {"path": str(tmp_path / "desktop-original.png")}},
        replace(context, nested_call_id="desktop-original-save"),
    )
    assert saved.ok and saved.result_value["verified"], saved.to_dict()
    assert (tmp_path / "desktop-original.png").read_bytes() == b"\x89PNG\r\n\x1a\nCAP"

    destination = tmp_path / 'desktop-original.png'
    destination.write_bytes(b'preserve existing destination')
    collision = await broker.invoke_name(
        'remote_handle_dispatch',
        {'handle': {key: image[key] for key in ('service', 'kind', 'id', 'generation', 'revision')},
         'method': 'save', 'arguments': {'path': str(destination)}},
        replace(context, nested_call_id='desktop-save-collision'),
    )
    assert collision.error.code == 'artifact_destination_exists'
    assert 'FileExistsError' in collision.error.message
    assert 'overwrite=True' in collision.error.message
    assert collision.error.may_have_applied is False
    assert destination.read_bytes() == b'preserve existing destination'
    retried = await broker.invoke_name(
        'remote_handle_dispatch',
        {'handle': {key: image[key] for key in ('service', 'kind', 'id', 'generation', 'revision')},
         'method': 'save', 'arguments': {'path': str(destination), 'overwrite': True}},
        replace(context, nested_call_id='desktop-save-overwrite'),
    )
    assert retried.ok and retried.result_value['verified'], retried.to_dict()
    assert destination.read_bytes() == b'\x89PNG\r\n\x1a\nCAP'

    textual = await _call(
        broker, context, "observe",
        window=focused, include_screenshot=False, include_text=True,
    )
    assert textual["screenshot"] is None
    assert len(textual["controls"]) == 2
    assert adapter.captures == 1


@pytest.mark.asyncio
async def test_closed_window_retains_delivery_receipt_without_stale_controls(tmp_path):
    from desktop_fabric.models import DesktopUnavailable
    fabric, adapter, broker, context = _stack(tmp_path)
    view = await _call(broker, context, 'focus', name='Editor')
    original = adapter.dispatch
    async def close(*args, **kwargs):
        result = await original(*args, **kwargs)
        adapter.validate_window = lambda _window: {'live':False, 'foreground':False, 'reason':'closed'}
        adapter.post_action_target = lambda _window: (_ for _ in ()).throw(DesktopUnavailable('window closed'))
        return result
    adapter.dispatch = close
    result = await _call(broker, context, 'press_key', view=view, keys='ctrl+w')
    assert result['input_sent'] is True
    assert result['action_status'] == 'unknown_effect'
    assert result['observation_status'] == 'unavailable'
    assert result['controls'] == [] and result['observation_id'] is None
    assert result['action']['receipt_id']
    assert len(adapter.dispatches) == 1


@pytest.mark.asyncio
async def test_control_action_returns_refreshed_view_not_operation_handle(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path)
    view = await _call(broker, context, "focus", name="Editor")

    result = await _call(
        broker, context, "set_value",
        view=view,
        target=view["controls"][0],
        value="after",
    )

    assert result["schema"] == "variant1.desktop-view-result.v2"
    assert result["action"] == {
        "name": "set_value", "status": "verified", "input_sent": True,
    }
    assert result["input_sent"] is True
    assert result["action_status"] == "verified"
    assert result["action_error"] is None
    assert result["controls"][0]["value"] == "after"
    assert "operation_id" not in result
    assert adapter.dispatches[-1]["actual"] == "semantic"
    # Focus supplies the source view; the action performs only its one
    # authoritative post-action observation.
    assert adapter.observes == 2


@pytest.mark.asyncio
async def test_click_routes_explicit_semantic_action_without_a_second_public_method(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path)
    view = await _call(broker, context, "focus", name="Editor")

    result = await _call(
        broker, context, "click",
        view=view,
        target=view["controls"][1],
        action="invoke",
    )

    assert adapter.dispatches[-1]["action"] == "invoke"
    assert adapter.dispatches[-1]["element"].name == "Save"
    assert result["action"]["name"] == "invoke"


@pytest.mark.asyncio
async def test_click_rejects_mixed_semantic_and_mouse_modes_before_dispatch(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path)
    view = await _call(broker, context, "focus", name="Editor")
    receipt = await broker.invoke_name(
        "computer",
        {
            "operation": "click",
            "view": view,
            "target": view["controls"][1],
            "action": "invoke",
            "button": "right",
        },
        replace(context, nested_call_id="nested-mixed-click"),
    )

    assert receipt.ok is False
    assert "does not accept button or count" in receipt.error.message
    assert adapter.dispatches == []


@pytest.mark.asyncio
async def test_coordinate_click_is_relative_to_the_supplied_screenshot(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path)
    focused = await _call(broker, context, "focus", name="Editor")
    view = await _call(
        broker, context, "observe",
        window=focused, include_screenshot=True, include_text=True,
    )

    assert view["coordinate_space"] == "screenshot"
    assert view["controls"][0]["bounds"] == [10, 40, 780, 580]

    result = await _call(
        broker, context, "click",
        view=view,
        x=25,
        y=35,
    )

    assert result["action"]["input_sent"] is True
    assert result["screenshot_included"] is True
    assert adapter.dispatches[-1]["arguments"]["x"] == 125
    assert adapter.dispatches[-1]["arguments"]["y"] == 235
    assert adapter.dispatches[-1]["actual"] == "physical"
    assert adapter.observes == 3


@pytest.mark.asyncio
async def test_coordinate_action_uses_window_space_without_a_screenshot(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path)
    view = await _call(broker, context, "focus", name="Editor")
    result = await _call(
        broker, context, "click", view=view, x=1, y=2,
    )

    assert result["input_sent"] is True
    assert adapter.dispatches[-1]["arguments"]["x"] == 101
    assert adapter.dispatches[-1]["arguments"]["y"] == 202
    assert result["coordinate_space"] == "window"


@pytest.mark.asyncio
async def test_coordinate_action_rejects_a_point_outside_its_view(tmp_path):
    _fabric, _adapter, broker, context = _stack(tmp_path)
    view = await _call(broker, context, "focus", name="Editor")

    receipt = await broker.invoke_name(
        "computer",
        {"operation": "click", "view": view, "x": 801, "y": 2},
        replace(context, nested_call_id="nested-outside-window"),
    )

    assert receipt.ok is False
    assert "outside the window" in str(receipt.error)


def test_only_one_desktop_broker_handler_is_registered(tmp_path):
    _fabric, _adapter, broker, _context = _stack(tmp_path)
    names = {tool.name for tool in broker.registry.all()}
    assert "computer" in names
    assert not names.intersection({"focus_window", "see_ui", "ground_ui"})


@pytest.mark.asyncio
async def test_invalid_target_recovery_preserves_input_boundary(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path)
    view = await _call(broker, context, 'focus', name='Editor')
    before = len(adapter.dispatches)
    control = view['controls'][1]
    mixed = await broker.invoke_name('computer',
        {'operation': 'click', 'view': view, 'target': control, 'x': 1, 'y': 2},
        replace(context, nested_call_id='mixed-target'))
    assert mixed.error.code == 'desktop_invalid_target'
    assert 'target=view.controls[0]' in mixed.error.message
    assert mixed.error.may_have_applied is False
    invalid = await broker.invoke_name('computer',
        {'operation': 'click', 'view': view, 'target': f"id={control['id']}"},
        replace(context, nested_call_id='invalid-selector'))
    assert 'DesktopNotFound' in invalid.error.message
    assert "'id=...' string is not selector syntax" in invalid.error.message
    assert len(adapter.dispatches) == before
    recovered = await _call(broker, context, 'click', view=view, target=control)
    assert recovered['input_sent'] is True
    assert len(adapter.dispatches) == before + 1


@pytest.mark.asyncio
async def test_window_listing_omits_blank_zero_size_shell_windows(tmp_path):
    _fabric, adapter, broker, context = _stack(tmp_path)
    ordinary_catalog = adapter.catalog
    blank = replace(
        adapter.window,
        window_id="win_blank_shell",
        hwnd=456,
        title="",
        bounds=(0, 0, 0, 0),
    )

    def catalog(*, backend_instance_id):
        apps, windows = ordinary_catalog(backend_instance_id=backend_instance_id)
        return apps, [*windows, replace(blank, backend_instance_id=backend_instance_id)]

    adapter.catalog = catalog
    windows = await _call(broker, context, "list_windows")

    assert [item["window_id"] for item in windows] == ["win_editor"]
