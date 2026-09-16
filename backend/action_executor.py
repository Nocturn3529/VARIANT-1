"""Shared interactive and background tool action execution.

``ToolActionRuntime`` supplies dependencies from the installed ``HostRuntime``.
Every model-requested action follows the same path: resolve the registered
handler, validate its arguments, then let the mandatory broker apply the
chat's catalog grant and execute it in request order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import tools
from agent_types import ToolBatchResult
from tool_runner import execute_tool_batch


@dataclass
class ActionExecutorDeps:
    """Explicit dependencies for one action batch."""

    registry: Any
    tool_runner_ports: Callable[[Any], Any]
    headless_tool_runner_ports: Callable[[], Any]
    uses_desktop_surface: Callable[[str], bool]
    desktop_action_lock: Any
    capability_broker: Any


def _build_runnable(actions: list, deps: ActionExecutorDeps, *, normalize: bool) -> list:
    runnable = []
    for a in actions:
        if normalize:
            name = a.get("tool")
            supplied_args = a.get("args")
            raw = supplied_args if isinstance(supplied_args, dict) else {}
            action = {"tool": name, "args": raw}
            argument_error = str(a.get("argument_error") or "").strip()
            if not argument_error and supplied_args is not None and not isinstance(supplied_args, dict):
                argument_error = "tool arguments must be one JSON object"
            if argument_error:
                action["argument_error"] = argument_error
            call_id = str(a.get("id") or "").strip()
            if call_id:
                action["id"] = call_id
            replay = a.get("provider_replay")
            if isinstance(replay, dict) and replay:
                action["provider_replay"] = dict(replay)
        else:
            name = a["tool"]
            action = a
        tool = deps.registry.get(name)
        if not tool:
            runnable.append({"a": action, "tool": None, "status": "unavailable"})
        else:
            argument_error = str(action.get("argument_error") or "").strip()
            if not argument_error:
                try:
                    validator = getattr(tool, "validate_args", None)
                    if callable(validator):
                        action["args"] = validator(action.get("args"))
                    elif not isinstance(action.get("args"), dict):
                        raise tools.ToolError("tool arguments must be one JSON object")
                except Exception as exc:
                    argument_error = str(exc) or "invalid tool arguments"
            if argument_error:
                action["argument_error"] = argument_error
                runnable.append({
                    "a": action,
                    "tool": tool,
                    "status": "invalid_arguments",
                    "validation_error": argument_error,
                })
            else:
                runnable.append({
                    "a": action,
                    "tool": tool,
                    "status": "ok",
                })
    return runnable


async def run_actions_interactive(
    deps: ActionExecutorDeps,
    websocket,
    actions: list,
    should_stop=None,
) -> ToolBatchResult:
    """Validate and execute actions for an interactive chat turn."""
    runnable = _build_runnable(actions, deps, normalize=False)

    touches_desktop = any(
        row.get("tool") is not None
        and deps.uses_desktop_surface(getattr(row.get("tool"), "name", ""))
        for row in runnable
    )

    async def _dispatch(*, desktop_lock_held: bool = False):
        return await execute_tool_batch(
            runnable,
            should_stop=should_stop,
            ports=deps.tool_runner_ports(websocket),
            broker=deps.capability_broker,
            desktop_lock_held=desktop_lock_held,
        )

    # The desktop is one physical side-effect surface. Interactive turns must
    # participate in the same lock as automations and app agents;
    # otherwise two sessions can interleave focus/click/type operations even
    # though each session individually holds a target lock.
    if touches_desktop:
        async with deps.desktop_action_lock:
            return await _dispatch(desktop_lock_held=True)
    return await _dispatch()


async def run_actions_headless_batch(
    deps: ActionExecutorDeps,
    actions: list,
    should_stop=None,
) -> ToolBatchResult:
    """Execute a background tool batch through the same validated path."""
    runnable = _build_runnable(actions, deps, normalize=True)

    touches_desktop = any(
        row.get("tool") is not None
        and deps.uses_desktop_surface(getattr(row.get("tool"), "name", ""))
        for row in runnable
    )

    async def _dispatch(*, desktop_lock_held: bool = False):
        return await execute_tool_batch(
            runnable,
            should_stop=should_stop,
            ports=deps.headless_tool_runner_ports(),
            broker=deps.capability_broker,
            desktop_lock_held=desktop_lock_held,
        )

    if touches_desktop:
        async with deps.desktop_action_lock:
            br = await _dispatch(desktop_lock_held=True)
    else:
        br = await _dispatch()
    if br.cancelled and not br.text:
        br.text = "[stopped] cancelled before finishing the remaining actions."
    return br


async def run_actions_headless(
    deps: ActionExecutorDeps,
    actions: list,
    should_stop=None,
) -> ToolBatchResult:
    """Background tool batch for automations and subagents.

    Returns call-bound outcomes for model-facing observation projection.
    """
    return await run_actions_headless_batch(deps, actions, should_stop=should_stop)
