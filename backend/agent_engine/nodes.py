"""Shared node policies for VARIANT-1's native agent execution."""

from __future__ import annotations

import copy
import json
import time
from typing import Any, Optional

from agent_types import ToolBatchResult
from observability import context_lineage
import tool_calling
from agent_task import TaskStatus
from assistant_turn import truncated_tool_outcomes

from .snapshot_utils import (
    build_resume_context_from_run_state,
    pending_tool_loop_from_run_state,
    restore_messages_from_run_state,
)
from .active_input import drain_active_input, inject_active_input, wait_for_pause_boundary
from .task_ports import (
    LoopResult,
    TaskTurnResult,
    task_progress_emit_fields,
)
from .config import AgentRunConfig
from .agent_runtime import (
    HeadlessWorkerRuntime,
    MainTaskRuntime,
    _browser_snapshot,
    _desktop_snapshot,
    _emit_runtime_event,
    _main_checkpoint_projection,
    _main_state,
    _port_model_name,
    _serializable_loop,
    _with_run_bindings,
    project_live_task_bundle,
)
from .main_live import MainChatLive
from .prepare_task import main_prepare_task_node
from .state import RunState, queue_event
from run_context import current_run_context
from transcript_economy import ContextWindowExceededError
from session_catalog.profiles import IPYTHON_SCHEMA_REVISION
from .nodes_common import (  # noqa: F401
    _interrupt_main,
    _node_live_projection,
)
from .model_step import (
    append_length_recovery,
    ModelStepCompleted,
    ModelStepFailed,
    ModelStepInterrupted,
    default_model_route,
    execute_model_step,
)
from .model_step_policies import (
    HeadlessModelStepPolicy,
    MainModelStepPolicy,
)


def _record_clean_replays(count: int) -> None:
    value = max(0, int(count or 0))
    if not value:
        return
    context = current_run_context()
    if context is not None:
        context.metadata["_clean_replays"] = int(
            context.metadata.get("_clean_replays") or 0
        ) + value


def init_run_node(config: AgentRunConfig):
    async def _node(state: RunState) -> dict:
        saved_surface = str(
            state.get("action_surface") or "trusted-local.v1"
        )
        saved_schema = str(
            state.get("provider_tool_schema_revision")
            or IPYTHON_SCHEMA_REVISION
        )
        if saved_surface != config.action_surface:
            raise RuntimeError(
                "checkpoint action surface changed "
                f"({saved_surface!r} -> {config.action_surface!r})"
            )
        if saved_schema != config.provider_tool_schema_revision:
            raise RuntimeError(
                "checkpoint provider schema changed "
                f"({saved_schema!r} -> {config.provider_tool_schema_revision!r})"
            )
        now = time.time()
        events = await _emit_runtime_event(
            state,
            None,
            "agent_runtime:init",
            source=config.source,
            config=config.name,
        )
        return {
            "status": "running",
            "updated_at": now,
            "config_name": config.name,
            "action_surface": config.action_surface,
            "provider_tool_schema_revision": config.provider_tool_schema_revision,
            "desktop": _desktop_snapshot(state.get("desktop")),
            "browser": _browser_snapshot(state.get("browser")),
            "observability": events,
        }

    return _with_run_bindings(_node)


def _main_turn_result(state: RunState, live: MainChatLive) -> TaskTurnResult:
    main = state.get("main") or {}
    loop = main.get("loop") or {}
    output = state.get("output") or {}
    return TaskTurnResult(
        loop_result=LoopResult(
            mood=output.get("mood") or loop.get("mood") or "neutral",
            reply=output.get("reply") or loop.get("reply") or "",
            interrupted=bool(output.get("interrupted") or loop.get("interrupted")),
            completion_status=output.get("completion_status") or "ok",
            stop_reason=output.get("stop_reason") or loop.get("stop_reason") or "",
            terminal_reason=(
                output.get("terminal_reason")
                or loop.get("terminal_reason")
                or ""
            ),
            length_recoveries=int(
                output.get("length_recoveries")
                or loop.get("length_recoveries")
                or 0
            ),
        ),
        messages=state.get("messages") or [],
        run=live.run or main.get("run"),
        img_token=live.img_token,
        task=live.task,
        transcript_id=str(
            state.get("run_id")
            or state.get("thread_id")
            or getattr(live.task, "id", "")
            or ""
        ),
    )


def main_loop_init_node(config: AgentRunConfig, runtime: MainTaskRuntime):
    async def _node(state: RunState) -> dict:
        await wait_for_pause_boundary(runtime.live.loop_ports)
        live = runtime.live
        ports = live.loop_ports
        loop = dict(((state.get("main") or {}).get("loop") or {}))
        messages = list(state.get("messages") or [])
        # A resumed model-step checkpoint must execute its saved call batch
        # before any steering/user message is appended to the transcript.
        delivered = (
            None
            if loop.get("route") in {"tools", "finalize"}
            else drain_active_input(ports, allow_follow_up=False)
        )
        input_state = None
        if delivered is not None:
            ports.record_active_input(delivered, None)
            messages, input_state = inject_active_input(
                state,
                messages,
                delivered,
                receipt=live.context_receipt,
            )
        events = await _emit_runtime_event(
            state,
            ports,
            "agent_runtime:main_loop_init",
            source=config.source,
            active_input_delivery=(delivered or {}).get("delivery", ""),
            active_input_id=(delivered or {}).get("id", ""),
        )
        out = {
            "updated_at": time.time(),
            "messages": messages,
            "observability": events,
        }
        if input_state is not None:
            out["input"] = input_state
        return out

    return _with_run_bindings(_node)


def main_model_step_node(config: AgentRunConfig, runtime: MainTaskRuntime):
    async def _node(state: RunState) -> dict:
        main = dict(state.get("main") or {})
        live = runtime.live
        loop = dict(main.get("loop") or {})
        messages = list(state.get("messages") or [])
        task = live.task
        ports = live.loop_ports
        run = live.run

        if ports.should_stop():
            return _interrupt_main(loop, live)

        pending_image = live.pending_image
        live.pending_image = None
        # Chat-completions-style calls are stateless: retain every current-user
        # image across every model/tool step in the live runtime. A screenshot
        # emitted by a tool is added causally for the next request only.
        img_this = list(runtime.images or [])
        if isinstance(pending_image, list):
            img_this.extend(pending_image)
        elif pending_image:
            img_this.append(pending_image)
        image_input = img_this or None
        receipt = live.context_receipt
        if isinstance(receipt, dict):
            context_lineage.attach_to_messages(messages, receipt)
            if isinstance(pending_image, dict) and pending_image.get("origin") == "tool_result":
                encoded_chars = len(str(
                    pending_image.get("data_b64") or pending_image.get("data") or ""))
                context_lineage.add_item(
                    receipt,
                    kind="image_observation",
                    source="tool_result",
                    trust="tool_output",
                    decision="projected",
                    reason="tool_execution",
                    relevance="selected",
                    chars_before=encoded_chars,
                    chars_after=encoded_chars,
                    image_count=1,
                    producer=pending_image.get("tool_name"),
                )
                context_lineage.add_transform(
                    receipt,
                    kind="provider_projection",
                    reason="provider_projection",
                    input_count=1,
                    output_count=1,
                    image_count=1,
                    producer=pending_image.get("tool_name"),
                )
        loop["step"] = int(loop.get("step") or 0) + 1
        if run:
            run["step"] = loop["step"]
            await ports.emit("task:step", **task_progress_emit_fields(step=loop["step"]))

        execution = await execute_model_step(
            messages,
            MainModelStepPolicy(
                ports=ports,
                max_output_tokens=live.max_out,
                image_input=image_input,
            ),
        )
        if isinstance(execution, ModelStepInterrupted):
            return _interrupt_main(loop, live)
        if isinstance(execution, ModelStepFailed):
            messages = execution.messages
            exc = execution.exception
            terminal_reason = str(getattr(exc, 'terminal_reason', '') or 'provider_error')
            error = f"{type(exc).__name__}: {exc}"
            failure_reply = (
                str(exc)
                if isinstance(exc, ContextWindowExceededError)
                else f"Model error: {exc}"
            )
            fragments = "".join(
                str(item)
                for item in loop.get("reply_fragments") or ()
                if item
            )
            reply = (
                fragments + ("\n\n" if fragments else "") + failure_reply
            )
            task.status = TaskStatus.FAILED
            loop.update({
                "mood": "concerned",
                "reply": reply,
                "reply_fragments": [],
                "actions": [],
                "thinking": "",
                "stop_reason": "error",
                "turn_disposition": "error",
                "route": "finalize",
                "error": error,
                "terminal_reason": terminal_reason,
            })
            events = await _emit_runtime_event(
                state,
                ports,
                "agent_runtime:main_model_error",
                source=config.source,
                step=loop["step"],
                error=error,
                clean_replays=execution.clean_replays,
                stop_reason="error",
                terminal_reason=terminal_reason,
            )
            return _node_live_projection(
                loop,
                live,
                runtime,
                messages=messages,
                errors=list(state.get("errors") or []) + [error],
                observability=events,
            )
        if not isinstance(execution, ModelStepCompleted):
            raise RuntimeError("unknown model-step execution outcome")
        messages = execution.messages
        model_step = execution.step
        _record_clean_replays(model_step.clean_replays)
        turn = model_step.turn
        actions = model_step.actions
        disposition = model_step.disposition
        stop_reason = model_step.stop_reason
        reply_fragments = [
            str(item) for item in loop.get("reply_fragments") or () if item
        ]
        accumulated_reply = "".join([*reply_fragments, str(turn.text or "")])
        loop.update({
            "mood": "neutral",
            "reply": accumulated_reply,
            "actions": actions,
            "thinking": turn.thinking,
            "stop_reason": stop_reason,
            "turn_disposition": disposition,
            "clean_replays": model_step.clean_replays,
        })
        if ports.should_stop() and disposition in {"stop", "truncated"}:
            interrupted = _interrupt_main(loop, live)
            interrupted["messages"] = messages
            return interrupted
        delivered = None
        input_state = None
        if disposition == "error":
            task.status = TaskStatus.FAILED
            loop["mood"] = "concerned"
            loop["reply"] = accumulated_reply or "The model reported an error."
            loop["route"] = "finalize"
            loop["terminal_reason"] = "model_error"
        elif disposition == "aborted":
            task.status = TaskStatus.FAILED
            loop["interrupted"] = True
            loop["reply"] = accumulated_reply or "Stopped."
            loop["route"] = "finalize"
            loop["terminal_reason"] = "user_cancelled"
        elif disposition in {"tools", "truncated_tools"}:
            if disposition == "tools":
                loop["consecutive_length_recoveries"] = 0
                loop["route"] = "tools"
            else:
                consecutive = int(
                    loop.get("consecutive_length_recoveries") or 0
                )
                maximum = max(
                    0, int(config.max_consecutive_length_recoveries or 0)
                )
                if consecutive < maximum:
                    if turn.text:
                        reply_fragments.append(str(turn.text))
                    loop["consecutive_length_recoveries"] = consecutive + 1
                    loop["length_recoveries"] = int(
                        loop.get("length_recoveries") or 0
                    ) + 1
                    loop["reply_fragments"] = reply_fragments
                    loop["route"] = "tools"
                else:
                    task.status = TaskStatus.FAILED
                    loop.update({
                        "mood": "concerned",
                        "reply": (
                            accumulated_reply
                            + ("\n\n" if accumulated_reply else "")
                            + "The model repeatedly truncated its tool call "
                              "before supplying complete arguments."
                        ),
                        "actions": [],
                        "route": "finalize",
                        "terminal_reason": "model_output_limit",
                    })
        elif disposition == "truncated":
            consecutive = int(
                loop.get("consecutive_length_recoveries") or 0
            )
            maximum = max(
                0, int(config.max_consecutive_length_recoveries or 0)
            )
            if consecutive < maximum:
                if turn.text:
                    reply_fragments.append(str(turn.text))
                append_length_recovery(messages)
                task.status = TaskStatus.IN_PROGRESS
                loop.update({
                    "reply": "",
                    "reply_fragments": reply_fragments,
                    "actions": [],
                    "route": "model_step",
                    "terminal_reason": "",
                    "consecutive_length_recoveries": consecutive + 1,
                    "length_recoveries": int(
                        loop.get("length_recoveries") or 0
                    ) + 1,
                })
            else:
                task.status = TaskStatus.FAILED
                loop.update({
                    "mood": "concerned",
                    "reply": (
                        accumulated_reply
                        + ("\n\n" if accumulated_reply else "")
                        + "The model reached its output limit repeatedly before "
                          "completing the task."
                    ),
                    "actions": [],
                    "route": "finalize",
                    "terminal_reason": "model_output_limit",
                })
        else:
            # A text-only assistant turn is complete. Steering can now redirect
            # the next model turn; follow-ups are eligible because the loop
            # would otherwise finish.
            delivered = drain_active_input(ports, allow_follow_up=True)
            if delivered is not None:
                ports.record_active_input(delivered, accumulated_reply)
                messages, input_state = inject_active_input(
                    state,
                    messages,
                    delivered,
                    receipt=live.context_receipt,
                )
                task.status = TaskStatus.IN_PROGRESS
                loop.update({
                    "reply": "",
                    "reply_fragments": [],
                    "actions": [],
                    "route": "model_step",
                    "terminal_reason": "",
                    "consecutive_length_recoveries": 0,
                })
            else:
                task.status = TaskStatus.COMPLETED
                loop["reply"] = accumulated_reply
                loop["reply_fragments"] = []
                loop["consecutive_length_recoveries"] = 0
                loop["terminal_reason"] = "completed"
                loop["route"] = "finalize"

        events = await _emit_runtime_event(
            state,
            ports,
            "agent_runtime:main_model_step",
            source=config.source,
            step=loop["step"],
            route=loop["route"],
            stop_reason=stop_reason,
            turn_disposition=disposition,
            clean_replays=model_step.clean_replays,
            terminal_reason=loop.get("terminal_reason") or "",
            length_recoveries=int(loop.get("length_recoveries") or 0),
            active_input_delivery=(delivered or {}).get("delivery", ""),
            active_input_id=(delivered or {}).get("id", ""),
        )
        extra = {"messages": messages, "observability": events}
        if input_state is not None:
            extra["input"] = input_state
        return _node_live_projection(loop, live, runtime, **extra)

    async def _entry(state: RunState) -> dict:
        live = runtime.live
        ports = live.loop_ports
        await wait_for_pause_boundary(ports)
        input_state = dict(state.get("input") or {})
        messages = list(state.get("messages") or [])
        # A ticket may arrive while this model boundary is paused. Poll here
        # before inference, but do not combine it with a ticket already prepared
        # by the previous node: one queued input owns each next model turn.
        if not input_state.get("pending_model_ticket_id") and not ports.should_stop():
            delivered = drain_active_input(ports, allow_follow_up=False)
            if delivered is not None:
                ports.record_active_input(delivered, None)
                messages, input_state = inject_active_input(
                    state, messages, delivered, receipt=live.context_receipt,
                )
        input_state.pop("pending_model_ticket_id", None)
        state = {**state, "messages": messages, "input": input_state}
        output = await _node(state)
        # _node may have prepared a different ticket after the response; retain
        # that new pending marker rather than clearing it with the consumed one.
        output.setdefault("input", input_state)
        return output

    return _with_run_bindings(_entry)


from .nodes_main_tools import main_tool_node  # noqa: F401 — extracted


def main_finalize_node(config: AgentRunConfig, runtime: MainTaskRuntime):
    async def _node(state: RunState) -> dict:
        await wait_for_pause_boundary(runtime.live.loop_ports)
        main = dict(state.get("main") or {})
        live = runtime.live
        loop = dict(main.get("loop") or {})
        ports = live.loop_ports
        if loop.get("interrupted"):
            if ports and ports.checkpoint:
                await ports.checkpoint("cancelled")
            loop["mood"] = "neutral"
            loop["reply"] = loop.get("reply") or "Stopped."
        task = live.task
        if (
            not loop.get("interrupted")
            and getattr(task, "status", None) is TaskStatus.COMPLETED
            and not bool(loop.get("terminal_commit_only"))
        ):
            boundary_events = await _emit_runtime_event(
                state,
                ports,
                "agent_runtime:main_terminal_boundary",
                source=config.source,
            )
            delivered = drain_active_input(ports, allow_follow_up=True)
            if delivered is not None:
                ports.record_active_input(delivered, loop.get("reply") or "")
                messages, input_state = inject_active_input(
                    state,
                    list(state.get("messages") or []),
                    delivered,
                    receipt=live.context_receipt,
                )
                task.status = TaskStatus.IN_PROGRESS
                loop.update({
                    "reply": "",
                    "actions": [],
                    "route": "model_step",
                    "terminal_reason": "",
                    "consecutive_length_recoveries": 0,
                })
                return _node_live_projection(
                    loop,
                    live,
                    runtime,
                    messages=messages,
                    input=input_state,
                    observability=boundary_events,
                )
        if loop.get("interrupted"):
            completion_status = "cancelled"
        elif getattr(task, "status", None) is TaskStatus.FAILED:
            completion_status = (
                "truncated"
                if loop.get("terminal_reason") == "model_output_limit"
                else "failed"
            )
        else:
            completion_status = "ok"
        reply = loop.get("reply") or ""
        output = {
            "mood": loop.get("mood") or "neutral",
            "reply": reply,
            "interrupted": bool(loop.get("interrupted")),
            "completion_status": completion_status,
            "stop_reason": loop.get("stop_reason") or "",
            "terminal_reason": loop.get("terminal_reason") or "",
            "length_recoveries": int(loop.get("length_recoveries") or 0),
        }
        serial_main = _main_state(loop, live)
        turn = _main_turn_result({**state, "main": serial_main, "output": output}, live=live)
        runtime.final_turn = turn
        events = await _emit_runtime_event(
            state,
            ports,
            "agent_runtime:main_finalize",
            source=config.source,
            completion_status=completion_status,
            stop_reason=output["stop_reason"],
            terminal_reason=output["terminal_reason"],
            length_recoveries=output["length_recoveries"],
        )
        status = (
            "cancelled"
            if loop.get("interrupted")
            else "failed"
            if getattr(task, "status", None) is TaskStatus.FAILED
            else "completed"
        )
        if config.source == "chat":
            # The graph has produced its exact terminal answer, but the chat
            # transcript is a separate durable owner. Keep the native head
            # resumable until the outer finalizer acknowledges that append.
            output["snapshot_terminal_status"] = status
            output["transcript_committed"] = False
            status = "awaiting_transcript"
        return {
            "status": status,
            "main": serial_main,
            "output": output,
            **project_live_task_bundle(live),
            "updated_at": time.time(),
            "observability": events,
        }

    return _with_run_bindings(_node)


def route_main(state: RunState) -> str:
    return ((state.get("main") or {}).get("loop") or {}).get("route") or "finalize"


def headless_worker_prepare_node(config: AgentRunConfig, runtime: HeadlessWorkerRuntime):
    """Seed the worker loop's messages and telemetry step for this run.

    On a resumed run (runtime.is_resume with a valid resume_snap), restores
    the checkpointed conversation and step count instead of starting over --
    the counterpart to main_prepare_task_node's resume handling, but without
    the main-chat Task lifecycle headless workers don't have.
    """

    async def _node(state: RunState) -> dict:
        resume_state = runtime.resume_snap if isinstance(runtime.resume_snap, dict) else {}
        resume_active = bool(runtime.is_resume and resume_state)
        if resume_active:
            messages = list(resume_state.get("messages") or runtime.messages)
            step = int(resume_state.get("step") or 0)
            saved_worker = copy.deepcopy(dict(resume_state.get("worker") or {}))
            pending_tool_loop = pending_tool_loop_from_run_state(
                resume_state,
                worker=True,
            )
            print(
                f"[agent_resume] resuming headless worker source={config.source} "
                f"step={step} messages={len(messages)}",
                flush=True,
            )
        else:
            messages = list(runtime.messages)
            step = 0
            saved_worker = {}
            pending_tool_loop = None
        if pending_tool_loop is not None:
            worker_state = pending_tool_loop
        elif resume_active and saved_worker:
            worker_state = saved_worker
        else:
            worker_state = {
                "route": "model_step",
                "interrupted": False,
                "reply_fragments": [],
                "consecutive_length_recoveries": 0,
                "length_recoveries": 0,
                "terminal_reason": "",
            }
        events = queue_event(
            state, "agent_runtime:worker_prepare", source=config.source,
            resumed=resume_active, step=step,
        )
        return {
            "messages": messages,
            "step": step,
            "worker": worker_state,
            "updated_at": time.time(),
            "observability": events,
        }

    return _with_run_bindings(_node)


def headless_worker_step_node(config: AgentRunConfig, runtime: HeadlessWorkerRuntime):
    """One model turn of a headless worker's loop: stream a reply and decide
    whether to route to tool execution or finalize.

    Split from a single monolithic node (the pre-refactor headless_worker_node,
    which ran its entire step loop inside one node call) so durable executors
    can checkpoint after every step instead of only once the whole task
    finishes -- a crash mid-run then loses at most one step's progress.
    """

    async def _node(state: RunState) -> dict:
        worker = dict(state.get("worker") or {})
        if runtime.should_stop():
            events = queue_event(
                state,
                "agent_runtime:worker_interrupted",
                source=config.source,
                stop_reason="aborted",
                terminal_reason="user_cancelled",
            )
            return {
                "worker": {
                    **worker,
                    "route": "finalize",
                    "interrupted": True,
                    "stop_reason": "aborted",
                    "terminal_reason": "user_cancelled",
                },
                "updated_at": time.time(),
                "observability": events,
            }

        messages = list(state.get("messages") or [])
        used = int(state.get("step") or 0)

        used += 1
        await runtime.emit("task:step", step=used)
        execution = await execute_model_step(
            messages,
            HeadlessModelStepPolicy(
                runtime=runtime,
                max_output_tokens=config.max_output_tokens,
            ),
        )
        if isinstance(execution, ModelStepInterrupted):
            events = queue_event(
                state,
                "agent_runtime:worker_interrupted",
                source=config.source,
                stop_reason="aborted",
                terminal_reason="user_cancelled",
            )
            return {
                "messages": execution.messages,
                "worker": {
                    **worker,
                    "route": "finalize",
                    "interrupted": True,
                    "stop_reason": "aborted",
                    "terminal_reason": "user_cancelled",
                },
                "updated_at": time.time(),
                "observability": events,
            }
        if isinstance(execution, ModelStepFailed):
            messages = execution.messages
            error = execution.exception
            terminal_reason = str(getattr(error, 'terminal_reason', '') or 'provider_error')
            fragments = "".join(
                str(item)
                for item in worker.get("reply_fragments") or ()
                if item
            )
            failure_reply = str(error)
            events = queue_event(
                state,
                "agent_runtime:worker_error",
                source=config.source,
                error=execution.detail,
                clean_replays=execution.clean_replays,
                stop_reason="error",
                terminal_reason=terminal_reason,
            )
            return {
                "messages": messages,
                "worker": {
                    **worker,
                    "route": "finalize",
                    "reply": (
                        fragments
                        + (("\n\n" + failure_reply) if failure_reply else "")
                        if fragments else failure_reply
                    ),
                    "reply_fragments": [],
                    "error": failure_reply,
                    "stop_reason": "error",
                    "terminal_reason": terminal_reason,
                },
                "errors": list(state.get("errors") or []) + [str(error)],
                "updated_at": time.time(),
                "observability": events,
            }
        if not isinstance(execution, ModelStepCompleted):
            raise RuntimeError("unknown model-step execution outcome")
        messages = execution.messages
        model_step = execution.step
        _record_clean_replays(model_step.clean_replays)
        turn = model_step.turn
        actions = model_step.actions
        disposition = model_step.disposition
        stop_reason = model_step.stop_reason
        route = default_model_route(disposition)
        reply_fragments = [
            str(item) for item in worker.get("reply_fragments") or () if item
        ]
        accumulated_reply = "".join([*reply_fragments, str(turn.text or "")])

        error = ""
        if disposition == "error":
            error = turn.text or "The model reported an error."
        interrupted = disposition == "aborted"
        consecutive = int(worker.get("consecutive_length_recoveries") or 0)
        length_recoveries = int(worker.get("length_recoveries") or 0)
        terminal_reason = ""
        if disposition == "error":
            terminal_reason = "model_error"
        elif disposition == "aborted":
            terminal_reason = "user_cancelled"
        elif disposition == "truncated":
            maximum = max(
                0, int(config.max_consecutive_length_recoveries or 0)
            )
            if consecutive < maximum:
                if turn.text:
                    reply_fragments.append(str(turn.text))
                append_length_recovery(messages)
                consecutive += 1
                length_recoveries += 1
                route = "model_step"
            else:
                route = "finalize"
                terminal_reason = "model_output_limit"
                error = (
                    "The model reached its output limit repeatedly before "
                    "completing the task."
                )
        elif disposition == "truncated_tools":
            maximum = max(
                0, int(config.max_consecutive_length_recoveries or 0)
            )
            if consecutive < maximum:
                if turn.text:
                    reply_fragments.append(str(turn.text))
                consecutive += 1
                length_recoveries += 1
                route = "tools"
            else:
                route = "finalize"
                terminal_reason = "model_output_limit"
                actions = []
                error = (
                    "The model repeatedly truncated its tool call before "
                    "supplying complete arguments."
                )
        elif disposition in {"tools", "stop"}:
            consecutive = 0
            if disposition == "stop":
                terminal_reason = "completed"
        events = queue_event(
            state,
            "agent_runtime:worker_step",
            source=config.source,
            step=used,
            route=route,
            stop_reason=stop_reason,
            turn_disposition=disposition,
            clean_replays=model_step.clean_replays,
            terminal_reason=terminal_reason,
            length_recoveries=length_recoveries,
        )
        return {
            "step": used,
            "messages": messages,
            "worker": {
                "route": route,
                "mood": "neutral",
                "reply": (
                    accumulated_reply
                    + (("\n\n" + error) if error and accumulated_reply else error)
                    if terminal_reason == "model_output_limit"
                    else accumulated_reply
                ),
                "reply_fragments": (
                    reply_fragments
                    if disposition in {"truncated", "truncated_tools"} and not error
                    else []
                ),
                "thinking": turn.thinking,
                "stop_reason": stop_reason,
                "turn_disposition": disposition,
                "clean_replays": model_step.clean_replays,
                "consecutive_length_recoveries": consecutive,
                "length_recoveries": length_recoveries,
                "terminal_reason": terminal_reason,
                "actions": actions,
                "error": error,
                "interrupted": interrupted,
            },
            "updated_at": time.time(),
            "observability": events,
        }

    return _with_run_bindings(_node)


def headless_worker_tool_node(config: AgentRunConfig, runtime: HeadlessWorkerRuntime):
    """Run the actions a headless worker's step chose, then loop back to
    another step -- or finalize if a stop request landed between the model's
    reply and running its tools."""

    async def _node(state: RunState) -> dict:
        worker = dict(state.get("worker") or {})
        actions = list(worker.get("actions") or [])
        messages = list(state.get("messages") or [])
        desktop = dict(state.get("desktop") or {})
        tools_state = dict(state.get("tools") or {})

        try:
            bound_names = set(getattr(runtime.tool_catalog, "names", ()) or ())
            refused_set = set()
            for action in actions:
                name = str(action.get("tool") or "") if isinstance(action, dict) else ""
                if not name or name not in bound_names:
                    refused_set.add(name)
            refused = sorted(refused_set)
            batch = ToolBatchResult()
            if runtime.should_stop():
                message = "cancelled before execution"
                outcomes = []
                for index, action in enumerate(actions):
                    name = str(action.get("tool") or "")
                    call_id = str(action.get("id") or f"call_{index}")
                    outcomes.append({
                        "tool": name,
                        "args": dict(action.get("args") or {}),
                        "call_id": call_id,
                        "result": message,
                        "model_result": message,
                        "ok": False,
                        "executed": False,
                        "status": "cancelled",
                        "error_class": "cancelled",
                    })
                batch = ToolBatchResult(
                    text="\n".join(
                        f"[{outcome['tool'] or 'tool'}] {message}"
                        for outcome in outcomes
                    ),
                    outcomes=outcomes,
                    cancelled=True,
                )
                result_text = batch.text
            elif worker.get("turn_disposition") == "truncated_tools":
                refused = []
                truncated = truncated_tool_outcomes(actions)
                batch = ToolBatchResult(
                    text="\n".join(
                        str(row.get("model_result") or "")
                        for row in truncated
                    ),
                    outcomes=truncated,
                    had_error=True,
                )
                result_text = batch.text
                await runtime.emit(
                    "tool:truncated",
                    tools=[str(action.get("tool") or "") for action in actions],
                    text=runtime.clip(result_text, 300),
                )
            elif refused:
                result_text = (
                    "Tool unavailable: the requested batch contained tool(s) "
                    f"outside the bound schema {runtime.tool_catalog.schema_hash}: "
                    + ", ".join(name or "(missing name)" for name in refused)
                )
                batch = ToolBatchResult(text=result_text, had_error=True)
                batch.outcomes = [{
                    "tool": str(action.get("tool") or ""),
                    "call_id": str(action.get("id") or f"call_{index}"),
                    "result": result_text,
                    "model_result": result_text,
                    "ok": False,
                    "executed": False,
                    "status": "unavailable",
                } for index, action in enumerate(actions)]
                tools_state["unavailable_names"] = refused
                await runtime.emit(
                    "tool:unavailable",
                    schema_hash=runtime.tool_catalog.schema_hash,
                    tools=refused,
                    text=runtime.clip(result_text, 300),
                )
            else:
                batch = await runtime.run_actions(actions)
                if not isinstance(batch, ToolBatchResult):
                    raise TypeError("headless tool runner must return ToolBatchResult")
                result_text = batch.text
            tools_state["last_actions"] = list(actions)
            tools_state["last_result_text"] = result_text
            cancelled = bool(runtime.should_stop() or batch.cancelled)
            terminates = bool(getattr(batch, "terminate", False))
            tools_state["had_error"] = bool(batch.had_error)
            tools_state["cancelled"] = bool(batch.cancelled)
            tools_state["terminate"] = terminates
            messages.extend(tool_calling.format_tool_result_messages(
                actions,
                outcomes=batch.outcomes,
            ))
            if runtime.approx_tokens(messages) > runtime.ctx_threshold():
                messages = await runtime.compress(messages)
            run_ctx = current_run_context()
            visible_specs = list(
                getattr(run_ctx, "disclosed_tool_specs", None)
                or runtime.disclosed_tool_specs
            )
            runtime.disclosed_tool_specs = visible_specs
            visible_names = sorted(
                str(spec.get("name") or "")
                for spec in visible_specs if isinstance(spec, dict) and spec.get("name")
            )
            tools_state.update({
                "disclosed_names": visible_names,
                "schema_names": visible_names,
                "schema_specs": visible_specs,
            })
        except Exception:
            # Do not commit a provider-invalid assistant tool-call tail as a
            # finalized worker.  The preceding model-step boundary remains the
            # durable head, so an exact resume re-enters the tool node and the
            # capability broker can replay or reconcile every possibly
            # dispatched effect by its original call id.
            raise

        route = "finalize" if cancelled or terminates else "model_step"
        terminal_reason = (
            "user_cancelled" if cancelled
            else "concluded_effect" if terminates
            else ""
        )
        if terminates and not str(worker.get("reply") or "").strip():
            worker["reply"] = str(result_text or "").strip()
        events = queue_event(
            state,
            "agent_runtime:worker_tool_result",
            source=config.source,
            cancelled=cancelled,
            terminate=terminates,
            route=route,
            terminal_reason=terminal_reason,
        )
        return {
            "messages": messages,
            "desktop": desktop,
            "tools": tools_state,
            "worker": {
                **worker,
                "route": route,
                "interrupted": cancelled,
                "terminal_reason": terminal_reason,
            },
            "updated_at": time.time(),
            "observability": events,
        }

    return _with_run_bindings(_node)


def headless_worker_finalize_node(config: AgentRunConfig, runtime: HeadlessWorkerRuntime):
    async def _node(state: RunState) -> dict:
        worker = dict(state.get("worker") or {})
        error = worker.get("error") or ""
        interrupted = bool(worker.get("interrupted"))
        if error:
            truncated = worker.get("terminal_reason") == "model_output_limit"
            status = "truncated" if truncated else "error"
            output = {
                "mood": "concerned",
                "reply": worker.get("reply") or error,
                "interrupted": interrupted,
                "completion_status": "truncated" if truncated else "error",
                "stop_reason": worker.get("stop_reason") or "",
                "terminal_reason": worker.get("terminal_reason") or "",
                "length_recoveries": int(worker.get("length_recoveries") or 0),
            }
        else:
            reply = worker.get("reply", "")
            status = "cancelled" if interrupted else "completed"
            output = {
                "mood": worker.get("mood", "neutral"),
                "reply": reply,
                "interrupted": interrupted,
                "completion_status": "cancelled" if interrupted else "ok",
                "stop_reason": worker.get("stop_reason") or "",
                "terminal_reason": worker.get("terminal_reason") or "",
                "length_recoveries": int(worker.get("length_recoveries") or 0),
            }
        events = queue_event(
            state, "agent_runtime:worker_complete", source=config.source,
            step=int(state.get("step") or 0), interrupted=interrupted, error=bool(error),
            completion_status=output["completion_status"],
            stop_reason=output["stop_reason"],
            terminal_reason=output["terminal_reason"],
            length_recoveries=output["length_recoveries"],
        )
        return {
            "status": status,
            "output": output,
            "updated_at": time.time(),
            "observability": events,
        }

    return _with_run_bindings(_node)


def route_worker(state: RunState) -> str:
    return ((state.get("worker") or {}).get("route")) or "finalize"


def finalize_run_node(config: AgentRunConfig):
    async def _node(state: RunState) -> dict:
        events = queue_event(state, "agent_runtime:finalize", source=config.source, status=state.get("status", ""))
        return {"updated_at": time.time(), "observability": events}

    return _with_run_bindings(_node)
