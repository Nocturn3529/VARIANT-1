"""VARIANT-1-owned execution for the explicit agent state machines.

The loops call the shared VARIANT-1 node functions directly and synchronously
commit validated boundaries through the native snapshot contract when a run is
durable.
"""

from __future__ import annotations

import copy
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import tool_discovery
from message_context_extents import HOST_CONTEXT_EXTENTS_REVISION
from run_context import bind_run_context, current_run_context
from tool_discovery import ToolCatalogSnapshot
from work_fabric.scope import coerce_work_scope

from .config import AgentRunConfig, validate_config
from .errors import DurableCheckpointUnavailable
from .execution_context import run_context_from_state
from .agent_runtime import HeadlessWorkerRuntime, MainTaskRuntime, bind_main_live
from .nodes import (
    finalize_run_node,
    headless_worker_finalize_node,
    headless_worker_prepare_node,
    headless_worker_step_node,
    headless_worker_tool_node,
    init_run_node,
    main_finalize_node,
    main_loop_init_node,
    main_model_step_node,
    main_prepare_task_node,
    main_tool_node,
    route_main,
    route_worker,
)
from .snapshot_store import (
    RunSnapshotStore,
    SnapshotBoundaryCommitter,
    StoredRunSnapshot,
)
from .sqlite_snapshot_store import SQLiteRunSnapshotStore
from .state import RunState, new_run_state


BoundaryCommit = Callable[[str, str, RunState], Awaitable[None]]
_LOG = logging.getLogger(__name__)


def _merge_live_run_observability(parent: Any, child: Any) -> None:
    """Return bounded inner-run metrics to the enclosing interactive turn."""

    if parent is None or child is None or parent is child:
        return
    parent_metadata = getattr(parent, "metadata", None)
    child_metadata = getattr(child, "metadata", None)
    if not isinstance(parent_metadata, dict) or not isinstance(child_metadata, dict):
        return
    calls = [
        str(name)[:80]
        for name in child_metadata.get("_tool_call_names") or ()
        if str(name or "").strip()
    ]
    if calls:
        existing = [
            str(name)[:80]
            for name in parent_metadata.get("_tool_call_names") or ()
            if str(name or "").strip()
        ]
        parent_metadata["_tool_call_names"] = (existing + calls)[:256]
    if "_tool_result_observations" in child_metadata:
        child_observations_all = [
            {
                "status": str(row.get("status") or "unknown")[:80],
                "error_code": str(row.get("error_code") or "")[:80],
            }
            for row in child_metadata.get("_tool_result_observations") or ()
            if isinstance(row, dict)
        ]
        child_input_overflow = max(0, len(child_observations_all) - 256)
        child_observations = child_observations_all[:256]
        existing_observations_all = [
            {
                "status": str(row.get("status") or "unknown")[:80],
                "error_code": str(row.get("error_code") or "")[:80],
            }
            for row in parent_metadata.get("_tool_result_observations") or ()
            if isinstance(row, dict)
        ]
        existing_input_overflow = max(0, len(existing_observations_all) - 256)
        existing_observations = existing_observations_all[:256]
        merged_observations = existing_observations + child_observations
        parent_metadata["_tool_result_observations"] = merged_observations[:256]
        overflow = max(0, len(merged_observations) - 256)
        parent_metadata["_tool_result_observations_truncated"] = min(
            1_000_000,
            int(parent_metadata.get("_tool_result_observations_truncated") or 0)
            + int(child_metadata.get("_tool_result_observations_truncated") or 0)
            + child_input_overflow
            + existing_input_overflow
            + overflow,
        )
    used = [
        str(name)[:80]
        for name in child_metadata.get("tools_used") or ()
        if str(name or "").strip()
    ]
    if used:
        existing_used = [
            str(name)[:80]
            for name in parent_metadata.get("tools_used") or ()
            if str(name or "").strip()
        ]
        parent_metadata["tools_used"] = list(dict.fromkeys(
            [*existing_used, *used]
        ))[:256]
    terminal_status = str(child_metadata.get("_terminal_status") or "").strip()
    if terminal_status:
        parent_metadata["_terminal_status"] = terminal_status[:24]
    for key, limit in (
        ("_terminal_stop_reason", 40),
        ("_terminal_reason", 80),
    ):
        value = str(child_metadata.get(key) or "").strip()
        if value:
            parent_metadata[key] = value[:limit]
    parent_metadata["_length_recoveries"] = int(
        parent_metadata.get("_length_recoveries") or 0
    ) + int(child_metadata.get("_length_recoveries") or 0)
    parent_metadata["_provider_attempts"] = int(
        parent_metadata.get("_provider_attempts") or 0
    ) + int(child_metadata.get("_provider_attempts") or 0)
    parent_metadata["_clean_replays"] = int(
        parent_metadata.get("_clean_replays") or 0
    ) + int(child_metadata.get("_clean_replays") or 0)


def _initial_work_scope(resume_snap: Any = None) -> dict[str, Any]:
    """Project inherited or authoritative WorkScope into fresh native state."""
    raw: Any = None
    if isinstance(resume_snap, dict):
        raw = resume_snap.get("work_scope")
    if not raw:
        parent = current_run_context()
        raw = getattr(parent, "work_scope", None) if parent is not None else None
    return coerce_work_scope(raw).to_dict(include_empty=False)


class NativeRunnerScopeError(RuntimeError):
    """The requested run is outside the deliberately staged native rollout."""


def _require_single_ipython_catalog(tool_catalog: ToolCatalogSnapshot) -> None:
    specs = tuple(tool_catalog.specs)
    if len(specs) != 1 or tool_catalog.names != frozenset({"ipython"}):
        raise NativeRunnerScopeError(
            "VARIANT-1 agent runs require exactly one provider action: 'ipython'"
        )


def _validate_native_scope(config: AgentRunConfig) -> None:
    validate_config(config)
    if config.source == "chat":
        raise NativeRunnerScopeError(
            "native headless execution cannot use source='chat'"
        )


def prepare_main_chat_run(
    *,
    config: AgentRunConfig,
    text: str,
    base_system: str,
    full_tspec: list,
    convo_tail: list,
    images: list[dict] | None,
    is_resume: bool,
    resume_snap: Any,
    ports: Any,
    context_receipt: Optional[dict[str, Any]] = None,
    graph_revision: str = "",
    session_capabilities: Optional[dict[str, Any]] = None,
) -> tuple[RunState, MainTaskRuntime, str]:
    """Create the exact main-chat state/runtime pair used by either executor."""
    validate_config(config)
    if config.source != "chat":
        raise ValueError(f"main-chat preparation requires source='chat' (got {config.source!r})")
    parent_ctx = current_run_context()
    chat_session = getattr(parent_ctx, "chat_session", None) if parent_ctx else None
    active = getattr(chat_session, "active", None) if chat_session is not None else None
    tool_catalog = getattr(active, "tool_catalog", None) if active is not None else None
    source_specs = (
        list(tool_catalog.specs)
        if isinstance(tool_catalog, ToolCatalogSnapshot)
        else list(full_tspec or [])
    )
    tool_catalog = ToolCatalogSnapshot.from_specs(source_specs)
    _require_single_ipython_catalog(tool_catalog)
    runtime = MainTaskRuntime(
        text=text,
        base_system=base_system,
        full_tspec=list(tool_catalog.specs),
        convo_tail=convo_tail,
        images=list(images or []),
        is_resume=is_resume,
        resume_snap=resume_snap,
        ports=ports,
        tool_catalog=tool_catalog,
        context_receipt=context_receipt,
    )
    resume_thread_id = ""
    resume_goal = ""
    if is_resume and isinstance(resume_snap, dict):
        resume_task = dict(resume_snap.get("task") or {})
        resume_thread_id = str(
            resume_snap.get("thread_id")
            or resume_task.get("task_id")
            or resume_snap.get("run_id")
            or ""
        ).strip()
        resume_goal = str(
            resume_task.get("goal")
            or resume_snap.get("goal")
            or resume_snap.get("title")
            or ""
        ).strip()
    inherit_run_id = ""
    if not resume_thread_id:
        try:
            parent_ctx = current_run_context()
            if parent_ctx is not None and getattr(parent_ctx, "run_id", None):
                inherit_run_id = str(parent_ctx.run_id).strip()
        except Exception:
            inherit_run_id = ""
    effective_thread_id = resume_thread_id or inherit_run_id
    resume_run_id = (
        str(resume_snap.get("run_id") or "").strip()
        if is_resume and isinstance(resume_snap, dict)
        else ""
    )
    state = new_run_state(
        source=config.source,
        title=resume_goal or text,
        goal=resume_goal or text,
        config_name=config.name,
        thread_id=effective_thread_id or None,
        run_id=resume_run_id or effective_thread_id or None,
        graph_revision=str(graph_revision or config.graph_revision or "").strip(),
        action_surface=config.action_surface,
        provider_tool_schema_revision=config.provider_tool_schema_revision,
        session_capabilities=session_capabilities,
        work_scope=_initial_work_scope(resume_snap if is_resume else None),
        desktop=(
            dict(resume_snap.get("desktop") or {})
            if is_resume and isinstance(resume_snap, dict)
            else None
        ),
        browser=(
            dict(resume_snap.get("browser") or {})
            if is_resume and isinstance(resume_snap, dict)
            else None
        ),
    )
    resume_chat_id = (
        str(resume_snap.get("chat_id") or "").strip()
        if is_resume and isinstance(resume_snap, dict)
        else ""
    )
    context_chat_id = str(
        ((getattr(parent_ctx, "metadata", {}) or {}).get("chat_id") or "")
        if parent_ctx is not None
        else ""
    ).strip()
    if resume_chat_id or context_chat_id:
        resolved_chat_id = resume_chat_id or context_chat_id
        state["chat_id"] = resolved_chat_id
        scope = coerce_work_scope(state.get("work_scope"))
        if not scope.chat_id:
            state["work_scope"] = scope.with_updates(
                chat_id=resolved_chat_id
            ).to_dict(include_empty=False)
    state["tools"] = {
        "enabled_names": sorted(tool_catalog.names),
        "disclosed_names": sorted(tool_catalog.names),
        "schema_hash": tool_catalog.schema_hash,
        "schema_mode": "searchable",
        "schema_names": sorted(tool_catalog.names),
        "schema_specs": list(tool_catalog.specs),
    }
    return state, runtime, str(state.get("thread_id") or effective_thread_id)


def prepare_headless_worker_run(
    *,
    config: AgentRunConfig,
    title: str,
    goal: str,
    messages: list,
    full_tspec: list,
    stream: Any,
    run_actions: Any,
    emit: Any,
    compress: Any,
    approx_tokens: Any,
    ctx_threshold: Any,
    should_stop: Any,
    clip: Any,
    tool_catalog: ToolCatalogSnapshot | None = None,
    stream_tools: Any = None,
    desktop: dict | None = None,
    browser: dict | None = None,
    orchestration: dict | None = None,
    thread_id: Optional[str] = None,
    is_resume: bool = False,
    resume_snap: Any = None,
    drain_inbound: Any = None,
    ack_inbound: Any = None,
) -> tuple[RunState, HeadlessWorkerRuntime]:
    """Create the exact state/runtime pair consumed by either executor."""
    if not isinstance(tool_catalog, ToolCatalogSnapshot):
        tool_catalog = ToolCatalogSnapshot.from_specs(full_tspec)
    _require_single_ipython_catalog(tool_catalog)
    initial_specs = tool_discovery.initial_tools(tool_catalog.specs)
    if is_resume and isinstance(resume_snap, dict):
        prior_names = set((resume_snap.get("tools") or {}).get("disclosed_names") or ())
        prior_names.update(spec["name"] for spec in initial_specs)
        restored = [
            spec for spec in tool_catalog.specs if spec.get("name") in prior_names
        ]
        if restored:
            initial_specs = restored
    runtime = HeadlessWorkerRuntime(
        messages=messages,
        full_tspec=list(tool_catalog.specs),
        stream=stream,
        run_actions=run_actions,
        emit=emit,
        compress=compress,
        approx_tokens=approx_tokens,
        ctx_threshold=ctx_threshold,
        should_stop=should_stop,
        clip=clip,
        tool_catalog=tool_catalog,
        disclosed_tool_specs=list(initial_specs),
        stream_tools=stream_tools,
        is_resume=is_resume,
        resume_snap=resume_snap,
        drain_inbound=drain_inbound,
        ack_inbound=ack_inbound,
    )
    resume_run_id = (
        str(resume_snap.get("run_id") or "").strip()
        if is_resume and isinstance(resume_snap, dict)
        else ""
    )
    state = new_run_state(
        source=config.source,
        title=title,
        goal=goal,
        config_name=config.name,
        thread_id=thread_id,
        run_id=resume_run_id or None,
        graph_revision=str(config.graph_revision or "").strip(),
        action_surface=config.action_surface,
        provider_tool_schema_revision=config.provider_tool_schema_revision,
        work_scope=_initial_work_scope(resume_snap if is_resume else None),
    )
    state["tools"] = {
        "enabled_names": sorted(tool_catalog.names),
        "disclosed_names": sorted(
            str(spec.get("name") or "") for spec in initial_specs if spec.get("name")
        ),
        "schema_hash": tool_catalog.schema_hash,
        "schema_mode": "searchable",
        "schema_names": sorted(
            str(spec.get("name") or "") for spec in initial_specs if spec.get("name")
        ),
        "schema_specs": list(initial_specs),
    }
    restored_desktop = (
        dict(resume_snap.get("desktop") or {})
        if is_resume and isinstance(resume_snap, dict)
        else {}
    )
    restored_browser = (
        dict(resume_snap.get("browser") or {})
        if is_resume and isinstance(resume_snap, dict)
        else {}
    )
    if desktop or restored_desktop:
        state["desktop"] = dict(desktop or restored_desktop)
    if browser or restored_browser:
        state["browser"] = dict(browser or restored_browser)
    restored_orchestration = (
        dict(resume_snap.get("orchestration") or {})
        if is_resume and isinstance(resume_snap, dict)
        else {}
    )
    if orchestration or restored_orchestration:
        state["orchestration"] = {
            **dict(state.get("orchestration") or {}),
            **restored_orchestration,
            **dict(orchestration or {}),
        }
    return state, runtime


async def _apply_node(state: RunState, node: Any) -> RunState:
    """Apply one node's top-level patch, matching RunState LastValue semantics."""
    patch = await node(state)
    if not isinstance(patch, dict):
        raise TypeError(f"agent node returned {type(patch).__name__}, expected dict")
    updated: RunState = dict(state)
    updated.update(patch)
    return updated


async def _commit_boundary(
    commit: Optional[BoundaryCommit],
    *,
    completed_node: str,
    next_node: str,
    state: RunState,
) -> None:
    """Synchronously persist one validated boundary before execution advances."""
    if commit is not None:
        await commit(completed_node, next_node, state)


def _main_node_for_route(route: str, *, after_finalize: bool = False) -> str:
    if route == "model_step":
        return "model_step"
    if route == "tools":
        return "tool"
    if route == "finalize":
        return "finalize" if after_finalize else "main_finalize"
    raise ValueError(f"unknown main route: {route!r}")


def _worker_node_for_route(route: str) -> str:
    if route == "model_step":
        return "worker_step"
    if route == "tools":
        return "worker_tool"
    if route == "finalize":
        return "worker_finalize"
    raise ValueError(f"unknown worker route: {route!r}")


def _validate_resume_boundary(
    snapshot: StoredRunSnapshot,
    *,
    worker: bool,
) -> None:
    """Validate a stored boundary before rehydration can advance its cursor."""
    if str(snapshot.status or "").lower() in {
        "completed", "failed", "cancelled", "error", "truncated",
    }:
        raise DurableCheckpointUnavailable("terminal native snapshot cannot be resumed")
    state = snapshot.state
    completed = snapshot.completed_node
    actual_next = snapshot.next_node
    try:
        if worker:
            if completed == "init":
                expected_next = "prepare"
            elif completed in {"prepare", "worker_step", "worker_tool"}:
                expected_next = _worker_node_for_route(str(route_worker(state) or "finalize"))
            elif completed == "worker_finalize":
                expected_next = "finalize"
            elif completed == "finalize":
                expected_next = "end"
            else:
                raise ValueError(f"unknown worker boundary node: {completed!r}")
            tool_node = "worker_tool"
        else:
            if completed == "init":
                expected_next = "prepare"
            elif completed == "prepare":
                expected_next = "loop_init"
            elif completed in {"loop_init", "model_step", "tool"}:
                expected_next = _main_node_for_route(str(route_main(state) or "finalize"))
            elif completed == "main_finalize":
                expected_next = _main_node_for_route(
                    str(route_main(state) or "finalize"),
                    after_finalize=True,
                )
            elif completed == "finalize":
                expected_next = "end"
            else:
                raise ValueError(f"unknown main boundary node: {completed!r}")
            tool_node = "tool"
    except Exception as exc:
        raise DurableCheckpointUnavailable(f"native resume boundary is invalid: {exc}") from exc
    if actual_next != expected_next:
        raise DurableCheckpointUnavailable(
            "native resume boundary route mismatch "
            f"({completed!r} -> {actual_next!r}, expected {expected_next!r})"
        )

    try:
        from .snapshot_utils import pending_tool_loop_from_run_state

        pending = pending_tool_loop_from_run_state(state, worker=worker)
    except Exception as exc:
        raise DurableCheckpointUnavailable(
            f"native resume pending-tool boundary is invalid: {exc}"
        ) from exc
    if pending is not None and actual_next != tool_node:
        raise DurableCheckpointUnavailable(
            "native snapshot has unmatched tool calls but does not resume into tools"
        )
    if actual_next == tool_node and pending is None:
        raise DurableCheckpointUnavailable(
            "native snapshot resumes into tools without a matching provider call batch"
        )


async def _managed_boundary_committer(
    *,
    config: AgentRunConfig,
    state: RunState,
    is_resume: bool,
    worker: bool,
    snapshot_store: Optional[RunSnapshotStore],
    resume_head: Optional[StoredRunSnapshot] = None,
) -> SnapshotBoundaryCommitter:
    store = snapshot_store or SQLiteRunSnapshotStore()
    thread_id = str(state.get("thread_id") or state.get("run_id") or "").strip()
    if not thread_id:
        raise DurableCheckpointUnavailable("durable native run has no thread identity")
    head = resume_head or await store.load_head(thread_id)
    if is_resume:
        if head is None:
            raise DurableCheckpointUnavailable(
                f"native resume snapshot not found for thread {thread_id}"
            )
        if head.source and head.source != config.source:
            raise DurableCheckpointUnavailable(
                f"native resume source changed ({head.source!r} -> {config.source!r})"
            )
        _validate_resume_boundary(head, worker=worker)
        return SnapshotBoundaryCommitter(store=store, cursor=head.cursor)
    if head is not None:
        if str(head.status or "").lower() in {
            "completed", "failed", "cancelled", "error", "truncated",
        }:
            # Stable automation/subagent thread IDs may host multiple runs. A
            # new generation appends after the prior terminal boundary while
            # retaining the same optimistic writer fence.
            return SnapshotBoundaryCommitter(store=store, cursor=head.cursor)
        raise DurableCheckpointUnavailable(
            f"fresh native run would fork incomplete thread {thread_id}"
        )
    return SnapshotBoundaryCommitter(store=store)


async def _authoritative_resume_snapshot(
    *,
    config: AgentRunConfig,
    is_resume: bool,
    resume_snap: Any,
    worker: bool,
    snapshot_store: Optional[RunSnapshotStore],
) -> tuple[Any, Optional[RunSnapshotStore], Optional[StoredRunSnapshot]]:
    """Replace caller state with the fenced native head before runtime creation."""
    if not config.checkpoints:
        return resume_snap, snapshot_store, None
    store = snapshot_store or SQLiteRunSnapshotStore()
    if not is_resume:
        return resume_snap, store, None
    if not isinstance(resume_snap, dict):
        raise DurableCheckpointUnavailable("native resume requires a snapshot state")
    requested_thread_id = str(
        resume_snap.get("thread_id") or resume_snap.get("run_id") or ""
    ).strip()
    if not requested_thread_id:
        raise DurableCheckpointUnavailable("native resume state has no thread identity")
    head = await store.load_head(requested_thread_id)
    if head is None:
        raise DurableCheckpointUnavailable(
            f"native resume snapshot not found for thread {requested_thread_id}"
        )
    if head.source and head.source != config.source:
        raise DurableCheckpointUnavailable(
            f"native resume source changed ({head.source!r} -> {config.source!r})"
        )
    _validate_resume_boundary(head, worker=worker)
    authoritative = dict(head.state)
    if worker:
        # Headless callers refresh only the system prefix before execution.
        # Preserve that refresh while proving the provider-visible tail is the
        # exact authoritative transcript.
        requested_messages = list(resume_snap.get("messages") or [])
        stored_messages = list(authoritative.get("messages") or [])
        if requested_messages != stored_messages:
            safe_system_refresh = (
                requested_messages
                and isinstance(requested_messages[0], dict)
                and requested_messages[0].get("role") == "system"
                and (
                    (stored_messages and requested_messages[1:] == stored_messages[1:])
                    or (not stored_messages and len(requested_messages) == 1)
                )
            )
            if not safe_system_refresh:
                raise DurableCheckpointUnavailable(
                    "headless resume transcript differs from the authoritative snapshot"
                )
            authoritative["messages"] = requested_messages
    return authoritative, store, head


def _next_main_phase(
    state: RunState,
    *,
    after: str,
    allowed: frozenset[str],
) -> str:
    phase = str(route_main(state) or "finalize")
    if phase not in allowed:
        expected = ", ".join(sorted(allowed))
        raise RuntimeError(
            f"invalid main route {phase!r} after {after}; expected {expected}"
        )
    return phase


def _next_worker_phase(
    state: RunState,
    *,
    after: str,
    allowed: frozenset[str],
) -> str:
    phase = str(route_worker(state) or "finalize")
    if phase not in allowed:
        expected = ", ".join(sorted(allowed))
        raise RuntimeError(
            f"invalid worker route {phase!r} after {after}; expected {expected}"
        )
    return phase


async def _run_headless_state_machine_body(
    *,
    config: AgentRunConfig,
    initial_state: RunState,
    runtime: HeadlessWorkerRuntime,
    thread_id: Optional[str] = None,
    commit: Optional[BoundaryCommit] = None,
    _context: Any = None,
) -> RunState:
    """Execute the current headless topology without a graph framework."""
    _validate_native_scope(config)
    if config.checkpoints and commit is None:
        raise DurableCheckpointUnavailable(
            "native durable headless execution requires a boundary commit sink"
        )
    if not isinstance(runtime, HeadlessWorkerRuntime):
        raise TypeError(
            "native headless execution requires HeadlessWorkerRuntime context "
            f"(got {type(runtime).__name__})"
        )

    init = init_run_node(config)
    prepare = headless_worker_prepare_node(config, runtime)
    model_step = headless_worker_step_node(config, runtime)
    tool = headless_worker_tool_node(config, runtime)
    worker_finalize = headless_worker_finalize_node(config, runtime)
    finalize = finalize_run_node(config)

    context = _context or run_context_from_state(
        config, initial_state, runtime, thread_id=thread_id,
    )
    with bind_run_context(context):
        state = await _apply_node(initial_state, init)
        if not runtime.is_resume:
            await _commit_boundary(
                commit,
                completed_node="init",
                next_node="prepare",
                state=state,
            )
        state = await _apply_node(state, prepare)
        phase = _next_worker_phase(
            state,
            after="prepare",
            allowed=frozenset({"model_step", "tools", "finalize"}),
        )
        await _commit_boundary(
            commit,
            completed_node="prepare",
            next_node=_worker_node_for_route(phase),
            state=state,
        )
        while phase != "finalize":
            if phase == "model_step":
                state = await _apply_node(state, model_step)
                phase = _next_worker_phase(
                    state,
                    after="model_step",
                    allowed=frozenset({"model_step", "tools", "finalize"}),
                )
                await _commit_boundary(
                    commit,
                    completed_node="worker_step",
                    next_node=_worker_node_for_route(phase),
                    state=state,
                )
                ack = getattr(runtime, "ack_inbound", None)
                if callable(ack):
                    message_ids = [
                        str(item.get("_variant1_inbound_message_id") or "")
                        for item in list(state.get("messages") or ())
                        if isinstance(item, dict)
                        and item.get("_variant1_inbound_message_id")
                    ]
                    if message_ids:
                        acknowledged = ack(tuple(dict.fromkeys(message_ids)))
                        if inspect.isawaitable(acknowledged):
                            await acknowledged
                continue
            if phase == "tools":
                state = await _apply_node(state, tool)
                phase = _next_worker_phase(
                    state,
                    after="tools",
                    allowed=frozenset({"model_step", "finalize"}),
                )
                await _commit_boundary(
                    commit,
                    completed_node="worker_tool",
                    next_node=_worker_node_for_route(phase),
                    state=state,
                )
                continue
            raise AssertionError(f"unhandled worker phase: {phase}")

        state = await _apply_node(state, worker_finalize)
        await _commit_boundary(
            commit,
            completed_node="worker_finalize",
            next_node="finalize",
            state=state,
        )
        state = await _apply_node(state, finalize)
        await _commit_boundary(
            commit,
            completed_node="finalize",
            next_node="end",
            state=state,
        )
        return state


async def run_headless_state_machine(
    *,
    config: AgentRunConfig,
    initial_state: RunState,
    runtime: HeadlessWorkerRuntime,
    thread_id: Optional[str] = None,
    commit: Optional[BoundaryCommit] = None,
) -> RunState:
    """Run a headless worker while owning its browser lease to termination."""

    context = run_context_from_state(
        config, initial_state, runtime, thread_id=thread_id,
    )
    browser_binding = getattr(context, "browser_binding", None)
    try:
        return await _run_headless_state_machine_body(
            config=config,
            initial_state=initial_state,
            runtime=runtime,
            thread_id=thread_id,
            commit=commit,
            _context=context,
        )
    finally:
        if browser_binding is not None:
            from browser_fabric.binding import close_browser_binding

            await close_browser_binding(browser_binding)


async def run_main_state_machine(
    *,
    config: AgentRunConfig,
    initial_state: RunState,
    runtime: MainTaskRuntime,
    thread_id: Optional[str] = None,
    commit: Optional[BoundaryCommit] = None,
) -> RunState:
    """Execute the current main-chat topology without a graph framework.

    This path is not selected for durable chat yet. It exists so execution
    parity can be established before native snapshots become authoritative.
    """
    validate_config(config)
    if config.source != "chat":
        raise NativeRunnerScopeError(
            f"native main execution requires source='chat' (got {config.source!r})"
        )
    if config.checkpoints and commit is None:
        raise DurableCheckpointUnavailable(
            "native durable main execution requires a boundary commit sink"
        )
    if not isinstance(runtime, MainTaskRuntime):
        raise TypeError(
            "native main execution requires MainTaskRuntime context "
            f"(got {type(runtime).__name__})"
        )

    init = init_run_node(config)
    prepare = main_prepare_task_node(config, runtime)
    loop_init = main_loop_init_node(config, runtime)
    model_step = main_model_step_node(config, runtime)
    tool = main_tool_node(config, runtime)
    main_finalize = main_finalize_node(config, runtime)
    finalize = finalize_run_node(config)

    parent_context = current_run_context()
    context = run_context_from_state(
        config,
        initial_state,
        runtime,
        thread_id=thread_id,
    )
    try:
        with bind_run_context(context):
            bind_main_live(runtime.live)
            state = await _apply_node(initial_state, init)
            if not runtime.is_resume:
                await _commit_boundary(
                    commit,
                    completed_node="init",
                    next_node="prepare",
                    state=state,
                )
            state = await _apply_node(state, prepare)
            if not runtime.is_resume:
                await _commit_boundary(
                    commit,
                    completed_node="prepare",
                    next_node="loop_init",
                    state=state,
                )
            state = await _apply_node(state, loop_init)
            phase = _next_main_phase(
                state,
                after="loop_init",
                allowed=frozenset({"model_step", "tools", "finalize"}),
            )
            await _commit_boundary(
                commit,
                completed_node="loop_init",
                next_node=_main_node_for_route(phase),
                state=state,
            )

            while True:
                if phase == "model_step":
                    state = await _apply_node(state, model_step)
                    phase = _next_main_phase(
                        state,
                        after="model_step",
                        allowed=frozenset({"model_step", "tools", "finalize"}),
                    )
                    await _commit_boundary(
                        commit,
                        completed_node="model_step",
                        next_node=_main_node_for_route(phase),
                        state=state,
                    )
                    continue
                if phase == "tools":
                    state = await _apply_node(state, tool)
                    phase = _next_main_phase(
                        state,
                        after="tools",
                        allowed=frozenset({"model_step", "finalize"}),
                    )
                    await _commit_boundary(
                        commit,
                        completed_node="tool",
                        next_node=_main_node_for_route(phase),
                        state=state,
                    )
                    continue
                if phase != "finalize":
                    raise AssertionError(f"unhandled main phase: {phase}")

                state = await _apply_node(state, main_finalize)
                phase = _next_main_phase(
                    state,
                    after="main_finalize",
                    allowed=frozenset({"model_step", "finalize"}),
                )
                await _commit_boundary(
                    commit,
                    completed_node="main_finalize",
                    next_node=_main_node_for_route(phase, after_finalize=True),
                    state=state,
                )
                if phase == "model_step":
                    continue

                state = await _apply_node(state, finalize)
                await _commit_boundary(
                    commit,
                    completed_node="finalize",
                    next_node="end",
                    state=state,
                )
                return state
    finally:
        # Chat-owned Browser Fabric sessions persist with the conversation and
        # kernel binding. Only headless run-owned bindings close at their
        # terminal edge.
        _merge_live_run_observability(parent_context, context)


async def run_main_chat_task(
    *,
    config: AgentRunConfig,
    text: str,
    base_system: str,
    full_tspec: list,
    convo_tail: list,
    images: list[dict] | None,
    is_resume: bool,
    resume_snap: Any,
    ports: Any,
    context_receipt: Optional[dict[str, Any]] = None,
    graph_revision: str = "",
    session_capabilities: Optional[dict[str, Any]] = None,
    snapshot_store: Optional[RunSnapshotStore] = None,
    commit: Optional[BoundaryCommit] = None,
):
    """Build and execute one main-chat turn on VARIANT-1's native runner."""
    owned_store = getattr(ports, "snapshot_store", None)
    if snapshot_store is not None and owned_store is not None and snapshot_store is not owned_store:
        raise ValueError("snapshot store must match the composed chat lifecycle store")
    snapshot_store = snapshot_store if snapshot_store is not None else owned_store
    resume_snap, snapshot_store, resume_head = await _authoritative_resume_snapshot(
        config=config,
        is_resume=is_resume,
        resume_snap=resume_snap,
        worker=False,
        snapshot_store=snapshot_store,
    )
    state, runtime, thread_id = prepare_main_chat_run(
        config=config,
        text=text,
        base_system=base_system,
        full_tspec=full_tspec,
        convo_tail=convo_tail,
        images=images,
        is_resume=is_resume,
        resume_snap=resume_snap,
        ports=ports,
        context_receipt=context_receipt,
        graph_revision=graph_revision,
        session_capabilities=session_capabilities,
    )
    if config.checkpoints and commit is None:
        commit = await _managed_boundary_committer(
            config=config,
            state=state,
            is_resume=is_resume,
            worker=False,
            snapshot_store=snapshot_store,
            resume_head=resume_head,
        )
    final_state = await run_main_state_machine(
        config=config,
        initial_state=state,
        runtime=runtime,
        thread_id=thread_id or None,
        commit=commit,
    )
    turn = runtime.final_turn
    if turn is None:
        return None
    if config.checkpoints and commit is not None:
        committed = False

        def _compacted_cursor() -> dict | None:
            from transcript_economy import COMPACT_SUMMARY_MARKER

            cursor = getattr(commit, "cursor", None)
            if cursor is None or not any(
                isinstance(row, dict) and row.get("role") == "assistant"
                and row.get("variant1_compaction") is True
                and str(row.get("content") or "").startswith(COMPACT_SUMMARY_MARKER)
                for row in final_state.get("messages") or []
            ):
                return None
            reference = {
                "thread_id": cursor.thread_id,
                "sequence": cursor.sequence,
                "snapshot_id": cursor.snapshot_id,
                "run_id": final_state["run_id"],
            }
            extent_revision = final_state.get("host_context_extents_revision")
            if (
                type(extent_revision) is int
                and extent_revision == HOST_CONTEXT_EXTENTS_REVISION
            ):
                reference["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
            return reference

        async def _commit_transcript_terminal() -> dict | None:
            nonlocal committed
            if committed:
                return _compacted_cursor()
            terminal_state = copy.deepcopy(final_state)
            output = dict(terminal_state.get("output") or {})
            terminal_status = str(
                output.get("snapshot_terminal_status") or "completed"
            ).strip().lower()
            if terminal_status not in {"completed", "failed", "cancelled", "error"}:
                terminal_status = "completed"
            output["transcript_committed"] = True
            terminal_state["output"] = output
            terminal_state["status"] = terminal_status
            await commit("finalize", "end", terminal_state)
            committed = True
            return _compacted_cursor()

        turn.commit_transcript_terminal = _commit_transcript_terminal
    return turn


async def run_headless_worker(
    *,
    config: AgentRunConfig,
    title: str,
    goal: str,
    messages: list,
    full_tspec: list,
    stream: Any,
    run_actions: Any,
    emit: Any,
    compress: Any,
    approx_tokens: Any,
    ctx_threshold: Any,
    should_stop: Any,
    clip: Any,
    tool_catalog: ToolCatalogSnapshot | None = None,
    stream_tools: Any = None,
    desktop: dict | None = None,
    browser: dict | None = None,
    orchestration: dict | None = None,
    thread_id: Optional[str] = None,
    is_resume: bool = False,
    resume_snap: Any = None,
    drain_inbound: Any = None,
    ack_inbound: Any = None,
    snapshot_store: Optional[RunSnapshotStore] = None,
    commit: Optional[BoundaryCommit] = None,
) -> RunState:
    """Build and execute one headless worker on VARIANT-1's native runner."""
    _validate_native_scope(config)
    resume_snap, snapshot_store, resume_head = await _authoritative_resume_snapshot(
        config=config,
        is_resume=is_resume,
        resume_snap=resume_snap,
        worker=True,
        snapshot_store=snapshot_store,
    )
    state, runtime = prepare_headless_worker_run(
        config=config,
        title=title,
        goal=goal,
        messages=messages,
        full_tspec=full_tspec,
        stream=stream,
        run_actions=run_actions,
        emit=emit,
        compress=compress,
        approx_tokens=approx_tokens,
        ctx_threshold=ctx_threshold,
        should_stop=should_stop,
        clip=clip,
        tool_catalog=tool_catalog,
        stream_tools=stream_tools,
        desktop=desktop,
        browser=browser,
        orchestration=orchestration,
        thread_id=thread_id,
        is_resume=is_resume,
        resume_snap=resume_snap,
        drain_inbound=drain_inbound,
        ack_inbound=ack_inbound,
    )
    if config.checkpoints and commit is None:
        commit = await _managed_boundary_committer(
            config=config,
            state=state,
            is_resume=is_resume,
            worker=True,
            snapshot_store=snapshot_store,
            resume_head=resume_head,
        )
    return await run_headless_state_machine(
        config=config,
        initial_state=state,
        runtime=runtime,
        thread_id=thread_id,
        commit=commit,
    )
