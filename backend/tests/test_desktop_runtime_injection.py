"""Production desktop controls bind an explicit runtime per invocation."""

import pytest

import desktop_control
from desktop import runtime as desktop_runtime


def test_module_has_no_ambient_runtime_configuration_api():
    assert not hasattr(desktop_control, "_RUNTIME")
    assert not hasattr(desktop_control, "register")
    assert not hasattr(desktop_runtime, "default_runtime")
    for name in (
        "set_ctx_getter",
        "set_control_guard",
        "set_screen_getter",
        "set_recovery_config",
        "set_perception_config",
        "set_observability_config",
        "set_activity_emitter",
        "set_killed",
        "status",
    ):
        assert not hasattr(desktop_control, name)


@pytest.mark.asyncio
async def test_explicit_runtime_is_bound_for_complete_async_invocation():
    runtime = desktop_runtime.DesktopRuntime()
    control = desktop_control.DesktopControl(runtime)

    async def observe(_args):
        assert desktop_runtime.get_runtime() is runtime
        return "ok"

    assert await control._invoke(observe, {}) == "ok"
    assert desktop_runtime.get_runtime() is not runtime
