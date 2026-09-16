"""Tool execution node for the typed main agent loop."""

from __future__ import annotations

import time

from agent_types import ToolBatchResult
from agent_task import TaskStatus
from assistant_turn import truncated_tool_outcomes
from .active_input import drain_active_input, inject_active_input, wait_for_pause_boundary
from .config import AgentRunConfig
from .agent_runtime import (
    MainTaskRuntime,
    _desktop_snapshot,
    _emit_runtime_event,
    _main_checkpoint_projection,
    _main_state,
    _with_run_bindings,
)
from .nodes_common import _node_live_projection
from .state import RunState


def _refused_outcomes(actions: list, text: str) -> list[dict]:
    return [{
        "tool": str(action.get("tool") or ""),
        "call_id": str(action.get("id") or f"call_{index}"),
        "result": text,
        "model_result": text,
        "ok": False,
        "executed": False,
        "status": "unavailable",
    } for index, action in enumerate(actions)]


def main_tool_node(config: AgentRunConfig, runtime: MainTaskRuntime):
    """Execute one provider tool-call batch, append results, and continue."""

    async def _node(state: RunState) -> dict:
        await wait_for_pause_boundary(runtime.live.loop_ports)
        live = runtime.live
        loop = dict(((state.get("main") or {}).get("loop") or {}))
        messages = list(state.get("messages") or [])
        task = live.task
        ports = live.loop_ports
        actions = list(loop.get("actions") or [])

        bound_names = set(getattr(runtime.tool_catalog, "names", ()) or ())
        outside_names = sorted({
            str(action.get("tool") or "")
            for action in actions
            if isinstance(action, dict)
            and str(action.get("tool") or "") not in bound_names
        })
        executed_actions = actions
        if loop.get("turn_disposition") == "truncated_tools":
            truncated = truncated_tool_outcomes(actions)
            result_text = "\n".join(
                str(row.get("model_result") or "") for row in truncated
            )
            outcome = ToolBatchResult(
                text=result_text,
                outcomes=truncated,
                had_error=True,
            )
            executed_actions = []
            outside_names = []
            await ports.emit(
                "tool:truncated",
                tools=[str(action.get("tool") or "") for action in actions],
                text=ports.clip(result_text, 300),
            )
        elif outside_names:
            result_text = (
                "Tool unavailable: requested tool(s) are outside the "
                f"bound schema {runtime.tool_catalog.schema_hash}: "
                + ", ".join(outside_names)
            )
            outcome = ToolBatchResult(
                text=result_text,
                outcomes=_refused_outcomes(actions, result_text),
                had_error=True,
            )
            await ports.emit(
                "tool:unavailable",
                schema_hash=runtime.tool_catalog.schema_hash,
                tools=outside_names,
                text=ports.clip(result_text, 300),
            )
            executed_actions = []
        else:
            outcome = await ports.run_actions(actions)
            if not isinstance(outcome, ToolBatchResult):
                raise TypeError("interactive tool runner must return ToolBatchResult")

        delivered = None
        input_state = None
        import tool_calling

        messages.extend(tool_calling.format_tool_result_messages(
            actions,
            outcomes=outcome.outcomes,
        ))
        cancelled = bool(ports.should_stop() or outcome.cancelled)
        terminates = bool(getattr(outcome, "terminate", False))
        if cancelled:
            task.status = TaskStatus.FAILED
            loop.update({
                "interrupted": True,
                "reply": "Stopped.",
                "route": "finalize",
                "terminal_reason": "user_cancelled",
            })
        else:
            ports.progressive_disclose(
                executed_actions, live.engaged_groups, live.disclosed, messages)
            # A terminating batch is a would-stop boundary, so steering still
            # wins but a queued follow-up may also continue the same run.
            delivered = drain_active_input(
                ports,
                allow_follow_up=terminates,
            )
            if delivered is not None:
                ports.record_active_input(delivered, loop.get("reply") or "")
                messages, input_state = inject_active_input(
                    state,
                    messages,
                    delivered,
                    receipt=live.context_receipt,
                )
                loop["reply"] = ""
                loop["reply_fragments"] = []
                loop["terminal_reason"] = ""
                loop["consecutive_length_recoveries"] = 0
                task.status = TaskStatus.IN_PROGRESS
                loop["route"] = "model_step"
            elif terminates:
                task.status = TaskStatus.COMPLETED
                if not str(loop.get("reply") or "").strip():
                    loop["reply"] = str(outcome.text or "").strip()
                loop["terminal_reason"] = "concluded_effect"
                loop["route"] = "finalize"
            else:
                loop["terminal_reason"] = ""
                loop["route"] = "model_step"

        live.img_holder = live.img_holder or {}
        live.pending_image = live.img_holder.get("image")
        live.img_holder["image"] = None

        if ports.checkpoint and outcome.executed and not cancelled:
            checkpoint_ref = live.checkpoint_ref
            if isinstance(checkpoint_ref, dict):
                checkpoint_state = dict(state)
                if input_state is not None:
                    checkpoint_state["input"] = input_state
                checkpoint_ref["state"] = _main_checkpoint_projection(
                    checkpoint_state,
                    task=task,
                    messages=messages,
                    disclosed=set(live.disclosed or []),
                    engaged_groups=set(live.engaged_groups or []),
                    event="after_tools",
                    checkpoint_created_at=float(
                        (state.get("task") or {}).get("checkpoint_created_at")
                        or state.get("created_at")
                        or time.time()
                    ),
                    ports=ports,
                    main=_main_state(loop, live),
                    desktop=_desktop_snapshot(
                        state.get("desktop"),
                    ),
                    tools_extra={
                        "last_actions": actions,
                        "last_result_text": outcome.text,
                        "had_error": bool(outcome.had_error),
                        "cancelled": bool(outcome.cancelled),
                        "terminate": terminates,
                    },
                )
            await ports.checkpoint("after_tools")

        events = await _emit_runtime_event(
            state,
            ports,
            "agent_runtime:main_tool_result",
            source=config.source,
            route=loop["route"],
            had_error=outcome.had_error,
            cancelled=cancelled,
            terminate=terminates,
            stop_reason=loop.get("stop_reason") or "",
            terminal_reason=loop.get("terminal_reason") or "",
            length_recoveries=int(loop.get("length_recoveries") or 0),
            active_input_delivery=(delivered or {}).get("delivery", ""),
            active_input_id=(delivered or {}).get("id", ""),
        )
        extra = {}
        if input_state is not None:
            extra["input"] = input_state
        return _node_live_projection(
            loop,
            live,
            runtime,
            messages=messages,
            desktop=_desktop_snapshot(
                state.get("desktop"),
            ),
            tools={
                **dict(state.get("tools") or {}),
                "last_actions": actions,
                "last_result_text": outcome.text,
                "had_error": bool(outcome.had_error),
                "cancelled": bool(outcome.cancelled),
                "terminate": terminates,
                "disclosed_names": sorted(live.disclosed),
                "engaged_groups": sorted(
                    group for group in live.engaged_groups if group),
                "unavailable_names": outside_names,
            },
            observability=events,
            **({"status": "cancelled"} if cancelled else {}),
            **extra,
        )

    return _with_run_bindings(_node)
