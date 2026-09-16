import pytest

import desktop.runtime as druntime
import desktop.session as ds
import desktop_control
from desktop_fabric.binding import (
    DesktopBinding,
    bind_desktop_binding,
    create_child_desktop_binding_snapshot,
)


class LiveHandle:
    NativeWindowHandle = 777


def test_driver_state_is_live_only_and_has_no_checkpoint_contract():
    state = ds.DesktopSessionState(session_id="desktop_driver_test")
    state.last_snapshot = {1: {"id": 1, "control": LiveHandle()}}

    assert state.last_snapshot[1]["control"].NativeWindowHandle == 777
    assert not hasattr(state, "to_snapshot")
    assert not hasattr(ds, "DesktopSessionSnapshot")


def test_driver_state_has_no_default_or_registry():
    token = ds.CURRENT_DESKTOP_SESSION.set(None)
    try:
        with pytest.raises(RuntimeError, match="driver state is unbound"):
            ds.current_desktop_session()
    finally:
        ds.CURRENT_DESKTOP_SESSION.reset(token)

    for name in (
        "_DEFAULT_SESSION", "_SESSION_REGISTRY", "ensure_desktop_session",
        "register_desktop_session", "create_child_desktop_session",
    ):
        assert not hasattr(ds, name)


def test_driver_context_is_nested_and_isolated():
    first = ds.DesktopSessionState(session_id="driver_first")
    second = ds.DesktopSessionState(session_id="driver_second")

    with ds.bind_desktop_session(first):
        assert ds.current_desktop_session() is first
        with ds.bind_desktop_session(second):
            assert ds.current_desktop_session() is second
        assert ds.current_desktop_session() is first


def test_desktop_binding_child_inherits_only_durable_window_pointer():
    parent = DesktopBinding(
        binding_id="desktop_binding_parent",
        active_window_id="win_editor",
        focus_history=["win_browser"],
    )
    with bind_desktop_binding(parent):
        child = create_child_desktop_binding_snapshot(inherit_target=True)

    assert child["parent_binding_id"] == "desktop_binding_parent"
    assert child["active_window_id"] == "win_editor"
    assert child["focus_history"] == []
    assert child["binding_id"] != parent.binding_id


def _recovery_config(control: desktop_control.DesktopControl):
    with druntime.bind_runtime(control.runtime):
        return desktop_control._control_context().recovery_config()


def test_driver_config_is_stable_for_an_inflight_window_state():
    control = desktop_control.DesktopControl(druntime.DesktopRuntime())
    state = ds.DesktopSessionState(session_id="driver_run_a")
    with ds.bind_desktop_session(state):
        before = _recovery_config(control)
    control.set_recovery_config({"min_controls_for_uia": 99})
    with ds.bind_desktop_session(state):
        after = _recovery_config(control)

    assert before.min_controls_for_uia == 3
    assert after is before


def test_new_driver_window_state_uses_current_runtime_config():
    control = desktop_control.DesktopControl(druntime.DesktopRuntime())
    control.set_recovery_config({"min_controls_for_uia": 42})
    state = ds.DesktopSessionState(session_id="driver_run_b")
    with ds.bind_desktop_session(state):
        assert _recovery_config(control).min_controls_for_uia == 42
