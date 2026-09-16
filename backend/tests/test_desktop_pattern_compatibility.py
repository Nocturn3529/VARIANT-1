"""Exercise the getter-only API exposed by the installed UIA package."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from desktop_fabric.adapter import WindowsDesktopAdapter, _patterns
from desktop_fabric.models import DesktopElement, DesktopUnavailable, WindowRecord


class Pattern:
    IsReadOnly = False
    Value = "before"
    IsSelected = False
    ToggleState = 0

    def __init__(self):
        self.calls = []

    def SetValue(self, value):
        self.calls.append(("SetValue", value))
        self.Value = value

    def Select(self):
        self.calls.append(("Select",))
        self.IsSelected = True

    def Toggle(self):
        self.calls.append(("Toggle",))
        self.ToggleState = 1

    def Expand(self):
        self.calls.append(("Expand",))

    def Collapse(self):
        self.calls.append(("Collapse",))

    def ScrollIntoView(self):
        self.calls.append(("ScrollIntoView",))


@pytest.fixture
def adapter_for(monkeypatch):
    class Context:
        def __init__(self, _session):
            pass

        def _load_uia(self):
            return None

        async def _uia(self, action):
            return action()

    class Adapter(WindowsDesktopAdapter):
        def _assert_live(self, *_args, **_kwargs):
            return None

        async def _bind_exact(self, *_args, **_kwargs):
            return None

        def _bound(self, _window_id):
            return nullcontext(None)

        def _record(self, _session, _element):
            return {"control": self.native_control}

        async def _physical_dispatch(self, *_args, **_kwargs):
            pytest.fail("working semantic pattern must not fall back to keyboard input")

    monkeypatch.setattr("desktop.context.DesktopControlContext", Context)
    monkeypatch.setattr("desktop.action_resolve._ensure_live", lambda _ctx, _auto, rec: rec["control"])

    def make(control):
        control.GetRuntimeId = lambda: [7, 1]
        adapter = Adapter(desktop_control=object(), catalog=object())
        adapter.native_control = control
        window = WindowRecord(window_id="win", app_id="app", hwnd=99, pid=7,
                              pid_started_at=10.0, title="Fixture")
        element = DesktopElement(
            element_ref="field", observation_id="obs", window_id="win",
            window_generation=window.generation, element_generation=1,
            runtime_id=(7, 1),
            role="Edit", name="Field", patterns=tuple(_patterns(control, actionable=True)),
        )
        return adapter, window, element

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "getter", "pattern_name", "arguments", "call", "readback"), [
    ("set_value", "GetValuePattern", "value", {"value": "after"}, ("SetValue", "after"), "after"),
    ("select", "GetSelectionItemPattern", "selection_item", {}, ("Select",), True),
    ("toggle", "GetTogglePattern", "toggle", {}, ("Toggle",), 1),
    ("expand", "GetExpandCollapsePattern", "expand_collapse", {}, ("Expand",), None),
    ("collapse", "GetExpandCollapsePattern", "expand_collapse", {}, ("Collapse",), None),
    ("scroll_into_view", "GetScrollItemPattern", "scroll_item", {}, ("ScrollIntoView",), None),
])
async def test_getter_only_control_discloses_and_delivers_once(
    adapter_for, action, getter, pattern_name, arguments, call, readback,
):
    pattern = Pattern()
    control = SimpleNamespace(**{getter: lambda: pattern})
    adapter, window, element = adapter_for(control)
    assert element.patterns == (pattern_name,)
    assert pattern.calls == []  # Disclosure only probes; it never performs input.

    result = await adapter.dispatch(
        window, action=action, delivery="auto", element=element, arguments=arguments,
    )

    assert result.metadata["selected_delivery"] == "semantic"
    assert result.readback == readback
    assert pattern.calls == [call]


@pytest.mark.asyncio
async def test_explicit_range_action_uses_getter_when_flag_is_absent(adapter_for):
    pattern = Pattern()
    adapter, window, element = adapter_for(SimpleNamespace(GetRangeValuePattern=lambda: pattern))
    assert element.patterns == ("range_value",)
    result = await adapter.dispatch(
        window, action="set_range_value", delivery="semantic", element=element,
        arguments={"value": 42},
    )
    assert pattern.calls == [("SetValue", 42.0)]
    assert result.readback == 42.0


@pytest.mark.asyncio
@pytest.mark.parametrize(("getter", "action"), [
    ("GetValuePattern", "set_value"), ("GetRangeValuePattern", "set_range_value"),
])
async def test_read_only_pattern_refuses_before_input(adapter_for, getter, action):
    pattern = Pattern()
    pattern.IsReadOnly = True
    adapter, window, element = adapter_for(SimpleNamespace(**{getter: lambda: pattern}))
    with pytest.raises(DesktopUnavailable, match="read-only"):
        await adapter.dispatch(
            window, action=action, delivery="semantic", element=element, arguments={"value": 42},
        )
    assert pattern.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("getter_behavior", ["missing", "none", "raises"])
async def test_absent_pattern_never_dispatches_or_retries(adapter_for, getter_behavior):
    def get_pattern():
        if getter_behavior == "raises":
            raise RuntimeError("provider cannot resolve the pattern")
        return None

    control = SimpleNamespace() if getter_behavior == "missing" else SimpleNamespace(GetValuePattern=get_pattern)
    adapter, window, element = adapter_for(control)
    assert element.patterns == ()
    with pytest.raises(DesktopUnavailable, match="no UIA Value pattern"):
        await adapter.dispatch(
            window, action="set_value", delivery="semantic", element=element, arguments={"value": "after"},
        )


def test_non_actionable_rows_do_not_gain_new_getter_probes():
    def unexpected_probe():
        pytest.fail("read-only observation rows should not need capability getter probes")

    assert _patterns(SimpleNamespace(GetValuePattern=unexpected_probe), actionable=False) == []


def test_existing_false_flag_behavior_and_boolean_properties_remain_supported():
    pattern = Pattern()
    control = SimpleNamespace(IsValuePatternAvailable=False, GetValuePattern=lambda: pattern)
    assert _patterns(control, actionable=True) == []
    control.IsValuePatternAvailable = True
    assert _patterns(control, actionable=False) == ["value"]


@pytest.mark.asyncio
async def test_failed_setter_is_not_replayed_as_physical_input(adapter_for):
    calls = []

    def failing_setter(value):
        calls.append(value)
        raise RuntimeError("provider rejected input")

    pattern = SimpleNamespace(SetValue=failing_setter, IsReadOnly=False)
    adapter, window, element = adapter_for(SimpleNamespace(GetValuePattern=lambda: pattern))
    with pytest.raises(RuntimeError, match="provider rejected"):
        await adapter.dispatch(
            window, action="set_value", delivery="auto", element=element, arguments={"value": "once"},
        )
    assert calls == ["once"]
