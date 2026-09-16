"""Shared graph runtime helpers for agent_engine nodes.

JSON-safe RunState projection, main-chat live bag, session binding, and
runtime adapter dataclasses used by prepare/model/tool/finalize nodes.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import time
from typing import Any, Optional

from run_context import current_run_context

from .main_live import MAIN_LIVE_KEY, MainChatLive
from .state import (
    RunState,
    queue_event,
    task_state_from_task,
)

CKPT_CLEAR = frozenset({"completed", "failed", "cancelled", "error"})


def _main_task_state(
    task: Any,
    *,
    checkpoint_event: str = "",
    checkpoint_created_at: float | None = None,
    model_name: str = "",
) -> dict:
    out = dict(task_state_from_task(task))
    if checkpoint_event:
        out["checkpoint_event"] = checkpoint_event
    if checkpoint_created_at is not None:
        out["checkpoint_created_at"] = float(checkpoint_created_at)
    out["model_name"] = model_name or out.get("model_name", "")
    return out


def project_live_task_bundle(
    live: MainChatLive,
    *,
    checkpoint_event: str = "",
    checkpoint_created_at: float | None = None,
    model_name: str = "",
) -> dict:
    """Project the live task into checkpoint-safe state.

    Live Task remains sole authority. Call this at prepare, node-exit durability
    (the native store may persist after each node), finalize, and explicit checkpoint
    hooks — not as a second writable model.
    """
    return {
        "task": _main_task_state(
            live.task,
            checkpoint_event=checkpoint_event,
            checkpoint_created_at=checkpoint_created_at,
            model_name=model_name,
        ),
    }


def _main_checkpoint_projection(
    state: RunState,
    *,
    task: Any,
    messages: list,
    disclosed: set,
    engaged_groups: set,
    event: str,
    checkpoint_created_at: float,
    ports: Any,
    main: dict | None = None,
    output: dict | None = None,
    desktop: dict | None = None,
    tools_extra: dict | None = None,
) -> RunState:
    """Project live runtime objects into serializable RunState first.

    Durable native snapshots own serialized run state. This projection exists so
    agent nodes update JSON-safe RunState before each snapshot boundary; it is
    not a TaskStore write contract.
    """
    current_tools = dict(state.get("tools") or {})
    if main:
        disclosed = set(main.get("disclosed") or disclosed or set())
        engaged_groups = set(main.get("engaged_groups") or engaged_groups or set())
    tools_state = {
        **current_tools,
        "disclosed_names": sorted(str(x) for x in disclosed if x),
        "engaged_groups": sorted(str(x) for x in engaged_groups if x),
        **dict(tools_extra or {}),
    }
    projected: RunState = dict(state)
    projected["messages"] = list(messages or [])
    projected["task"] = _main_task_state(
        task,
        checkpoint_event=event,
        checkpoint_created_at=checkpoint_created_at,
        model_name=_port_model_name(ports, state),
    )
    projected["tools"] = tools_state
    projected["desktop"] = dict(desktop or state.get("desktop") or {})
    if output is not None:
        projected["output"] = dict(output)
    projected["updated_at"] = time.time()
    return projected


def _port_model_name(ports: Any, state: RunState) -> str:
    current_model = getattr(ports, "current_model", None)
    if callable(current_model):
        try:
            return str(current_model() or "")
        except Exception:
            pass
    return str((state.get("task") or {}).get("model_name") or "")


def bind_main_live(live: MainChatLive) -> MainChatLive:
    """Publish the runtime-owned live bag on the current run context (same object).

    Nodes should prefer ``runtime.live``. Context metadata holds the *same*
    instance so tooling that only has Variant1RunContext can still find it —
    not a second copy.
    """
    ctx = current_run_context()
    if ctx is not None:
        ctx.metadata[MAIN_LIVE_KEY] = live
    return live


def _main_state(loop: dict, live: MainChatLive) -> dict:
    """JSON-safe main projection for RunState (checkpointable).

    ``loop`` is the sole source of routing crumbs (route/step/actions/…).
    ``live`` contributes non-loop serializable projections (tool disclosure sets,
    run id summary).
    """
    return {
        "loop": _serializable_loop(loop),
        "max_out": int(live.max_out or 0),
        "engaged_groups": sorted(str(x) for x in (live.engaged_groups or set()) if x),
        "disclosed": sorted(str(x) for x in (live.disclosed or set()) if x),
        "run": dict(live.run or {}),
    }


def _serializable_loop(loop: dict) -> dict:
    out = dict(loop or {})
    out.pop("outcome", None)
    out["full_tspec_names"] = sorted(str(x) for x in (out.get("full_tspec_names") or []) if x)
    return out


@dataclass
class MainTaskRuntime:
    """Server adapters + non-serializable live bag for one main-chat graph run.

    ``live`` is created at graph entry and filled by prepare. Loop routing lives
    only in RunState; ``live`` holds Task, ports, and run-scoped handles.
    """

    text: str
    base_system: str
    full_tspec: list
    convo_tail: list
    # Current-turn image bytes stay only on this non-serializable runtime.
    images: list[dict]
    is_resume: bool
    resume_snap: Any
    ports: Any
    tool_catalog: Any = None
    # Metadata-only provenance for the exact context selected by the outer
    # chat pipeline. Pass it explicitly across the agent execution boundary:
    # ContextVar state is rebound for agent execution and is not a reliable
    # transport between the outer turn and individual nodes.
    context_receipt: dict[str, Any] | None = None
    final_turn: Any = None
    live: MainChatLive = field(default_factory=MainChatLive)


@dataclass
class HeadlessWorkerRuntime:
    """Adapters for subagent/automation-style graph workers."""

    messages: list
    full_tspec: list
    stream: Any
    run_actions: Any
    emit: Any
    compress: Any
    approx_tokens: Any
    ctx_threshold: Any
    should_stop: Any
    clip: Any
    tool_catalog: Any = None
    disclosed_tool_specs: list = field(default_factory=list)
    stream_tools: Any = None
    is_resume: bool = False
    resume_snap: Any = None
    # Optional durable parent-to-child mailbox. Drained only at model-step
    # boundaries; no running stack frame is resumed or mutated.
    drain_inbound: Any = None
    # Acknowledge mailbox IDs only after the model-step boundary containing
    # their projected messages has been committed.
    ack_inbound: Any = None

def _desktop_snapshot(prev: dict | None = None, **extra) -> dict:
    """Checkpoint the authoritative Desktop Fabric binding only."""
    try:
        from desktop_fabric.binding import DesktopBinding, desktop_binding_snapshot

        snap = desktop_binding_snapshot()
        if snap:
            return dict(snap)
        raw = dict(prev or {})
        return DesktopBinding.from_mapping(raw).to_dict() if raw else {}
    except Exception:
        return {}


def _browser_snapshot(prev: dict | None = None, **extra) -> dict:
    """Checkpoint the authoritative Browser Fabric binding only."""
    try:
        from browser_fabric.binding import BrowserBinding, browser_binding_snapshot

        snap = browser_binding_snapshot()
        if snap:
            return dict(snap)
        raw = dict(prev or {})
        raw.update({key: value for key, value in extra.items() if value is not None})
        return BrowserBinding.from_mapping(raw).to_dict() if raw else {}
    except Exception:
        return {}


async def _emit_runtime_event(state: RunState, ports: Any, event: str, **fields: Any) -> list:
    """Emit a graph event to Activity Monitor and mirror it into RunState."""
    ctx = current_run_context()
    if ctx is not None:
        fields.setdefault("run_id", ctx.run_id)
        fields.setdefault("source", ctx.source)
        binding_id = ctx.desktop_binding_id
        if binding_id:
            fields.setdefault("desktop_binding_id", binding_id)
    desktop = state.get("desktop") or {}
    binding_id = desktop.get("binding_id") or ""
    if binding_id:
        fields.setdefault("desktop_binding_id", binding_id)
    try:
        if ports is not None:
            await ports.emit(event, **fields)
    except Exception:
        pass
    return queue_event(state, event, **fields)


def _with_run_bindings(node):
    """Bind agent-node execution to RunState Desktop and Browser Fabric identities.

    Same pattern for both: reuse the live Variant1RunContext binding if
    this run already bound one (so state carries forward across node calls
    within a run), otherwise restore the small pointer from RunState. Durable
    Fabric repositories remain the actual browser/desktop state authorities.
    """
    async def _wrapped(state: RunState) -> dict:
        run_ctx = current_run_context()
        desktop_ctx = None
        try:
            from desktop_fabric.binding import (
                bind_desktop_binding,
                ensure_desktop_binding,
            )

            if run_ctx is not None and run_ctx.desktop_binding is not None:
                desktop_ctx = bind_desktop_binding(run_ctx.desktop_binding)
            else:
                desktop_ctx = bind_desktop_binding(ensure_desktop_binding(
                    state.get("desktop"),
                    source=str(state.get("source") or ""),
                    owner_id=str(state.get("run_id") or ""),
                    scope=state.get("work_scope"),
                ))
        except Exception:
            desktop_ctx = None

        browser_ctx = None
        try:
            from browser_fabric.binding import (
                bind_browser_binding,
                ensure_browser_binding,
            )

            if run_ctx is not None and run_ctx.browser_binding is not None:
                browser_ctx = bind_browser_binding(run_ctx.browser_binding)
            else:
                browser_ctx = bind_browser_binding(ensure_browser_binding(
                    state.get("browser"),
                    source=str(state.get("source") or ""),
                    owner_id=str(state.get("run_id") or ""),
                    scope=state.get("work_scope"),
                ))
        except Exception:
            browser_ctx = None

        with contextlib.ExitStack() as stack:
            bound_desktop = stack.enter_context(desktop_ctx) if desktop_ctx is not None else None
            bound_browser = stack.enter_context(browser_ctx) if browser_ctx is not None else None
            if run_ctx is not None and run_ctx.desktop_binding is None and bound_desktop is not None:
                run_ctx.desktop_binding = bound_desktop
            if run_ctx is not None and run_ctx.browser_binding is None and bound_browser is not None:
                run_ctx.browser_binding = bound_browser
            result = await node(state)
            if isinstance(result, dict):
                merged = dict(state.get("desktop") or {})
                merged.update(result.get("desktop") or {})
                result["desktop"] = _desktop_snapshot(merged)
                if bound_browser is not None:
                    merged_browser = dict(state.get("browser") or {})
                    merged_browser.update(result.get("browser") or {})
                    result["browser"] = _browser_snapshot(merged_browser)
            return result

    return _wrapped


