"""The one headless persistent-Python worker used by durable children."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import llm_router
import prompt_builder
from agent_engine.presets import subagent_v1
from agent_engine.shared_ports import HeadlessAgentPorts
from core_invariants import cancellation_is_requested
from observability.activity import clip as _clip
from run_context import current_run_context


@dataclass
class ChildWorkerPorts:
    agent: HeadlessAgentPorts
    router: Any
    emit: Callable[..., Awaitable[None]]
    new_run: Callable[[str, str], Optional[dict]]
    active_session: Callable[[], Any]
    template_dirs: tuple[str, ...]
    bind_execution_run: Callable[[str],None] | None = None


def _status(reply: str) -> tuple[str, str]:
    value = str(reply or "").strip()
    for tag, status in (
        ("DONE", "done"),
        ("PARTIAL", "partial"),
        ("FAILED", "failed"),
    ):
        if value[:len(tag)].upper() != tag:
            continue
        remainder = value[len(tag):]
        # The worker contract asks for ``DONE:`` et al. Some OpenAI-compatible
        # models emit the same unambiguous status followed by whitespace/newline
        # instead of a colon. Accept either delimiter without matching words
        # such as ``DONEISH``.
        if remainder.startswith(":"):
            return status, remainder[1:].strip()
        if not remainder or remainder[:1].isspace():
            return status, remainder.strip()
    return "unknown", value


async def run_child_worker(
    ports: ChildWorkerPorts,
    task: str,
    instructions: str = "",
    *,
    inbound_messages=None,
    ack_inbound=None,
    cancellation_requested=None,
) -> str:
    """Execute one already-admitted child on VARIANT-1's single action surface."""

    ctx = current_run_context()
    if not (ctx and ctx.metadata.get("_server_bound_kind") == "subagent"):
        raise RuntimeError("child worker requires a ChildSessionManager run context")
    agent = ports.agent
    context = agent.context
    tool_surface = agent.tools
    session = ports.active_session()
    ports.new_run("subagent", task)
    await ports.emit("task:start", title=task[:200], text="Child")

    runtime_identity = dict((ctx.metadata or {}).get("runtime_identity") or {})
    from session_catalog.profiles import (
        ACTION_SURFACE,
        IPYTHON_SCHEMA_REVISION,
        WORKER_GRAPH_REVISION,
    )
    from session_catalog.service import IPYTHON_PROVIDER_SPEC

    action_surface = str(
        runtime_identity.get("action_surface") or ACTION_SURFACE
    )
    if action_surface != ACTION_SURFACE:
        raise RuntimeError(
            f"child inherited unsupported action surface {action_surface!r}"
        )
    worker_config = subagent_v1().with_overrides(
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=str(
            runtime_identity.get("provider_tool_schema_revision")
            or IPYTHON_SCHEMA_REVISION
        ),
        graph_revision=WORKER_GRAPH_REVISION,
    )
    ctx.run_config = worker_config
    complete_specs = [dict(IPYTHON_PROVIDER_SPEC)]
    tools_block = tool_surface.tools_prompt_block(
        {str(spec.get("name") or "") for spec in complete_specs},
        complete_specs,
    )

    context_parts: list[str] = []
    active = getattr(session, "active", None)
    parent_task = getattr(active, "task", None)
    parent_goal = getattr(parent_task, "goal", "")
    if parent_goal:
        context_parts.append("OVERALL GOAL (from the main agent): " + str(parent_goal))
    if instructions:
        context_parts.append("BACKGROUND / CONTEXT:\n" + instructions)
    runtime_prompt = str((ctx.metadata or {}).get("runtime_prompt") or "")
    if runtime_prompt:
        context_parts.append(runtime_prompt)
    context_parts.append("YOUR SUB-GOAL:\n" + task)
    system_prompt = prompt_builder.build_subagent_system(
        "\n\n".join(context_parts), tools_block,
        template_dirs=ports.template_dirs,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Begin working on the sub-goal above."},
    ]

    from agent_engine.snapshot_utils import (
        prior_incomplete_run_for_thread,
        restore_messages_from_run_state,
        subagent_thread_id,
    )

    parent_thread_id = str((ctx.metadata or {}).get("parent_thread_id") or "")
    thread_id = subagent_thread_id(
        parent_thread_id=parent_thread_id,
        task=task,
        instructions=instructions,
    )
    prior_run = prior_incomplete_run_for_thread(
        thread_id,
        expected_revision=worker_config.graph_revision,
    )
    resume_snapshot = None
    if prior_run:
        resume_snapshot = dict(prior_run)
        resume_snapshot["messages"] = restore_messages_from_run_state(
            prior_run,
            system_prompt,
            resume_context=(
                "[INTERRUPTED PRIOR RUN] Continue from your saved progress on "
                "this same sub-goal; do not restart from scratch."
            ),
        )
        await ports.emit(
            "child:resume",
            task=task[:200],
            thread_id=thread_id,
            step=int(prior_run.get("step") or 0),
            text="Resuming interrupted child from its durable checkpoint.",
        )

    def should_stop() -> bool:
        if session and getattr(session, "interrupt", False):
            return True
        return cancellation_is_requested(cancellation_requested)

    try:
        from desktop_fabric.binding import create_child_desktop_binding_snapshot
        child_desktop = create_child_desktop_binding_snapshot(scope=ctx.work_scope)
    except Exception:
        child_desktop = None
    try:
        from browser_fabric.binding import create_child_browser_binding_snapshot
        child_browser = create_child_browser_binding_snapshot(scope=ctx.work_scope)
    except Exception:
        child_browser = None

    async def stream_worker(worker_messages, max_tokens, bound_specs=None):
        return await llm_router.complete_turn(
            ports.router,
            worker_messages,
            profile="agent_turn",
            max_tokens=max_tokens,
            tools=list(bound_specs or []) or None,
            should_stop=should_stop,
        )

    async def run_worker_actions(actions):
        if ports.bind_execution_run is not None:
            execution=current_run_context()
            ports.bind_execution_run(execution.run_id if execution else '')
        return await tool_surface.run_actions_headless(
            actions, should_stop=should_stop,
        )

    from agent_engine.executor import execute_headless_worker
    run_state = await execute_headless_worker(
        snapshot_store=ports.agent.snapshot_store,
        config=worker_config,
        title=task,
        goal=task,
        messages=messages,
        full_tspec=complete_specs,
        stream=stream_worker,
        stream_tools=stream_worker,
        run_actions=run_worker_actions,
        emit=ports.emit,
        compress=context.compress_messages,
        approx_tokens=context.approx_tokens,
        ctx_threshold=context.ctx_compress_threshold,
        should_stop=should_stop,
        clip=_clip,
        desktop=child_desktop,
        browser=child_browser,
        thread_id=thread_id,
        is_resume=bool(prior_run),
        resume_snap=resume_snapshot,
        drain_inbound=inbound_messages,
        ack_inbound=ack_inbound,
    )
    output = run_state.get("output") or {}
    reply = str(output.get("reply") or "")
    native_status = str(run_state.get("status") or "").strip().lower()
    completion_status = str(
        output.get("completion_status") or ""
    ).strip().lower()
    native_cancelled = (
        native_status in {"cancelled", "canceled"}
        or completion_status in {"cancelled", "canceled"}
        or bool(output.get("interrupted"))
        or should_stop()
    )
    if native_cancelled:
        await ports.emit("task:done", status="cancelled", text="interrupted")
        progress = _clip(reply.strip(), 300)
        return "[child INTERRUPTED] stopped before finishing" + (
            f"; progress so far: {progress}" if progress else "."
        )

    native_truncated = (
        native_status == "truncated" or completion_status == "truncated"
    )
    if native_truncated:
        _, partial = _status(reply)
        progress = _clip(partial or reply.strip(), 300)
        detail = "native output ended at its limit before completion"
        if progress:
            detail += f"; partial output: {progress}"
        await ports.emit("task:done", status="error", text=_clip(detail, 300))
        # ChildSessionManager's durable schema has completed/failed/cancelled,
        # not a fourth partial terminal state. Keep the partial output explicit
        # while using the existing failed marker so it cannot be persisted as
        # completed or injected into the parent as a successful child result.
        return f"[child FAILED] {detail}"

    native_failed = (
        native_status in {"error", "failed"}
        or completion_status in {"error", "failed"}
        or native_status not in {"completed", "ok"}
    )
    if native_failed:
        error = _clip(
            "; ".join(run_state.get("errors") or [])
            or reply
            or f"native worker ended with status {native_status or 'missing'}",
            200,
        )
        await ports.emit("task:done", status="error", text=error)
        return f"[child FAILED] hit a model error: {error}"

    status, body = _status(reply)
    await ports.emit(
        "task:done",
        status="ok" if status == "done" else "error",
        text=_clip(body or status, 300),
    )
    if not body:
        return "[child FINISHED] produced no summary; treat the outcome as uncertain."
    label = status.upper() if status != "unknown" else "FINISHED"
    return (
        f"[child {label}] {body}\n"
        "(Self-reported by the child; verify critical results.)"
    )


__all__ = ["ChildWorkerPorts", "run_child_worker"]
