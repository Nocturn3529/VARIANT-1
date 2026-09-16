from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import desktop_control
import tools
from desktop import registry as desktop_registry


@pytest.mark.asyncio
async def test_focus_window_without_name_uses_bounded_perception(monkeypatch):
    perceive = AsyncMock(return_value=("Notepad", "controls", []))
    monkeypatch.setattr(desktop_control.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        desktop_control.dflow, "perceive_ui", perceive)

    out = await desktop_control._focus_window.__wrapped__(
        {"mode": "incremental", "max": 25})

    assert out == "controls"
    assert perceive.await_args.kwargs["tool_name"] == "computer.observe"
    assert perceive.await_args.args[1] == {"mode": "incremental", "max": 25}


@pytest.mark.asyncio
async def test_focus_window_previous_uses_internal_history_restore(monkeypatch):
    restore = AsyncMock(return_value="restored")
    monkeypatch.setattr(
        desktop_control.dtarget, "restore_previous_target", restore)

    out = await desktop_control._focus_window.__wrapped__({"previous": True})

    assert out == "restored"
    assert restore.await_args.args[1] == {"previous": True}


def test_private_focus_driver_is_not_registered_as_a_second_model_surface():
    registry = tools.ToolRegistry()
    desktop_registry.register(registry, desktop_control.DesktopControl())

    assert registry.get("computer") is not None
    assert registry.get("focus_window") is None
    assert registry.get("see_ui") is None
    assert registry.get("ground_ui") is None
    assert registry.get("read_ui") is None
    assert registry.get("read_ui_incremental") is None
    assert registry.get("detect_modal") is None
    assert registry.get("dismiss_modal") is None
