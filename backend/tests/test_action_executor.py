"""Smoke tests for action_executor runnable assembly (no live tools)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import action_executor
from agent_types import ToolBatchResult
import tools


def test_action_executor_has_no_parallel_enable_plane():
    assert set(action_executor.ActionExecutorDeps.__dataclass_fields__) == {
        "registry", "tool_runner_ports", "headless_tool_runner_ports",
        "uses_desktop_surface", "desktop_action_lock", "capability_broker",
    }


def _deps(**overrides):
    registry = MagicMock()
    registry.get = MagicMock(return_value=None)
    base = dict(
        registry=registry,
        tool_runner_ports=MagicMock(),
        headless_tool_runner_ports=MagicMock(),
        uses_desktop_surface=lambda n: False,
        desktop_action_lock=MagicMock(),
        capability_broker=MagicMock(),
    )
    base.update(overrides)
    return action_executor.ActionExecutorDeps(**base)


def test_build_runnable_marks_unavailable():
    deps = _deps()
    runnable = action_executor._build_runnable(
        [{"tool": "missing", "args": {}}], deps, normalize=True)
    assert runnable[0]["status"] == "unavailable"
    assert runnable[0]["tool"] is None


def test_build_runnable_keeps_every_native_call():
    deps = _deps()
    actions = [{"tool": f"t{i}", "args": {}} for i in range(5)]
    runnable = action_executor._build_runnable(actions, deps, normalize=True)
    assert len(runnable) == 5


def test_headless_normalization_preserves_native_call_id():
    deps = _deps()
    runnable = action_executor._build_runnable(
        [{"tool": "missing", "args": {}, "id": "call_headless_7"}],
        deps,
        normalize=True,
    )
    assert runnable[0]["a"]["id"] == "call_headless_7"


def test_build_runnable_rejects_invalid_arguments_before_execution():
    tool = tools.Tool(
        "computer",
        "desktop",
        AsyncMock(return_value="should not run"),
        params={
            "action": {
                "type": "string",
                "required": True,
                "enum": ["click", "move"],
            },
        },
    )
    registry = MagicMock()
    registry.get.return_value = tool
    deps = _deps(registry=registry)

    runnable = action_executor._build_runnable(
        [{"tool": "computer", "args": {"action": "bogus"}}],
        deps,
        normalize=True,
    )

    assert runnable[0]["status"] == "invalid_arguments"
    assert "must be one of" in runnable[0]["validation_error"]


def test_parser_argument_error_blocks_even_a_no_argument_tool():
    tool = tools.Tool(
        "browser_screenshot",
        "screenshot",
        AsyncMock(return_value="should not run"),
        params={},
    )
    registry = MagicMock()
    registry.get.return_value = tool
    deps = _deps(registry=registry)

    runnable = action_executor._build_runnable(
        [{
            "tool": "browser_screenshot",
            "args": {},
            "argument_error": "malformed tool arguments",
        }],
        deps,
        normalize=True,
    )

    assert runnable[0]["status"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_interactive_desktop_batch_uses_global_lock(monkeypatch):
    lock = asyncio.Lock()
    tool = SimpleNamespace(name="computer")
    registry = MagicMock()
    registry.get.return_value = tool
    deps = _deps(
        registry=registry,
        uses_desktop_surface=lambda name: name == "computer",
        desktop_action_lock=lock,
        tool_runner_ports=lambda _ws: object(),
    )

    async def fake_run(*_args, **_kwargs):
        assert lock.locked()
        return ToolBatchResult(text="ok")

    monkeypatch.setattr(action_executor, "execute_tool_batch", fake_run)
    result = await action_executor.run_actions_interactive(
        deps,
        websocket=object(),
        actions=[{"tool": "computer", "args": {"operation": "focus", "name": "Notepad"}}],
    )
    assert result.text == "ok"
    assert not lock.locked()
