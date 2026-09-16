"""The one mounted computer object enters the one Desktop Fabric core."""

from __future__ import annotations

import pytest

import desktop_control
from desktop import registry as desktop_registry
import desktop_fabric.access as desktop_access
import desktop_fabric.capabilities as desktop_capabilities
import tools


def _registry():
    registry = tools.ToolRegistry()
    desktop_registry.register(registry, desktop_control.DesktopControl())
    return registry


def _bind_fake_host(monkeypatch, *, result="fabric-result"):
    host = object()
    calls = []
    monkeypatch.setattr(desktop_access, "current_desktop_host", lambda _control: host)

    async def computer(bound_host, arguments):
        calls.append((bound_host, dict(arguments)))
        return result

    monkeypatch.setattr(
        desktop_capabilities, "desktop_computer_operation", computer)
    return host, calls


@pytest.mark.asyncio
async def test_computer_object_method_enters_fabric_unchanged(monkeypatch):
    host, calls = _bind_fake_host(monkeypatch)
    arguments = {
        "operation": "press_key",
        "view": {"schema": "variant1.desktop-view-result.v2", "id": "obs-1"},
        "keys": "Ctrl+Shift+N",
    }

    assert await _registry().get("computer").run(arguments) == "fabric-result"
    assert calls == [(host, arguments)]


@pytest.mark.asyncio
async def test_computer_has_no_host_absent_backend(monkeypatch):
    monkeypatch.setattr(desktop_access, "current_desktop_host", lambda _control: None)
    with pytest.raises(tools.ToolError, match="Desktop Fabric is unavailable"):
        await _registry().get("computer").run({"operation": "list_windows"})


def test_computer_object_contract_contains_only_task_verbs():
    computer = _registry().get("computer")
    methods = {row["name"] for row in computer.object_methods}
    assert methods == {
        "list_windows", "focus", "observe", "click", "type_text",
        "press_key", "set_value", "scroll", "drag",
    }
    assert not {
        "delivery", "expect", "idempotency_key", "include_image_evidence",
        "steps", "states", "after", "entity_kind", "entity_id",
    }.intersection(computer.params)


@pytest.mark.asyncio
async def test_computer_rejects_unknown_operation():
    with pytest.raises(tools.ToolError, match="must be one of"):
        await _registry().get("computer").run({"operation": "teleport"})


@pytest.mark.asyncio
async def test_click_owns_mouse_and_semantic_target_actions(monkeypatch):
    host, calls = _bind_fake_host(monkeypatch)
    computer = _registry().get('computer')
    common = {'view': {'schema': 'variant1.desktop-view-result.v2'}, 'target': 'Menu'}
    await computer.run({'operation': 'click', 'button': 'right', **common})
    await computer.run({'operation': 'click', 'action': 'invoke', **common})
    assert [row[1]['operation'] for row in calls] == ['click', 'click']
    assert calls[-1][1]['action'] == 'invoke'
    with pytest.raises(tools.ToolError, match='must be one of'):
        await computer.run({'operation': 'click', 'action': 'right_click', **common})
    assert len(calls) == 2


def test_replaced_desktop_seeds_are_not_registered():
    registered = {tool.name for tool in _registry().all()}
    assert "computer" in registered
    assert not registered.intersection({
        "focus_window", "see_ui", "ground_ui", "read_ui",
        "ui_click", "ui_set_text", "ui_select", "ui_keys", "ui_scroll",
        "ui_steps", "ui_click_xy", "ui_drag_xy", "window_stack",
    })
