import pytest

from session_catalog.control import ControlPlane
from session_catalog.profiles import ACTION_SURFACE


def test_control_plane_always_assigns_the_single_astb_surface(tmp_path):
    path = str(tmp_path / "astb.sqlite3")
    controls = ControlPlane(path, {
        "new_chat_profile": "native-tools.v1",
        "stop_new_kernels": False,
    })

    assert controls.profile_for_new_chat("chat-a") == ACTION_SURFACE
    assert controls.snapshot()["schema"] == "variant1.astb.controls.v2"
    assert set(controls.snapshot()) >= {
        "stop_new_kernels", "freeze_mutation", "revision", "updated_at",
    }


def test_kernel_and_mutation_kill_switches_are_durable_and_independent(tmp_path):
    path = str(tmp_path / "astb.sqlite3")
    controls = ControlPlane(path, {})
    changed = controls.update({
        "stop_new_kernels": True,
        "freeze_mutation": True,
    })

    assert changed["revision"] == 2
    reopened = ControlPlane(path, {})
    assert reopened.profile_for_new_chat("new-chat") == ACTION_SURFACE
    assert reopened.new_kernel_allowed() is False
    assert reopened.mutation_allowed() is False


def test_retired_rollout_controls_are_rejected(tmp_path):
    controls = ControlPlane(str(tmp_path / "astb.sqlite3"), {})

    with pytest.raises(ValueError, match="unknown session control"):
        controls.update({"default_profile": "native-tools.v1"})
