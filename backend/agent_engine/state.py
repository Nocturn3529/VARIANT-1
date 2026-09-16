"""Serializable state model for VARIANT-1's unified agent turn machine.

Execution state stays explicit and JSON-shaped. Main-chat loop routing lives in
``main.loop``; live Task, ports, callbacks, ContextVar
tokens, and tool-result objects live on the runtime-owned ``MainChatLive`` bag,
not in checkpoints.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, TypedDict

from .run_contract import RUN_STATE_SCHEMA_VERSION, graph_revision_for_source
from session_catalog.profiles import IPYTHON_SCHEMA_REVISION


# Detailed operational events live in the append-only trace stream.  Keep only
# a compact causal tail in checkpoint state so long autonomous runs do not
# repeatedly serialize an ever-growing observability history.
OBSERVABILITY_TAIL_LIMIT = 64


class DesktopRunState(TypedDict, total=False):
    """Checkpoint-safe pointer to authoritative Desktop Fabric state."""

    schema: str
    binding_id: str
    active_window_id: str | None
    focus_history: List[str]
    parent_binding_id: str | None
    owner_kind: str
    owner_id: str | None
    scope: Dict[str, Any]
    created_at: float
    updated_at: float


class BrowserRunState(TypedDict, total=False):
    """Checkpoint-safe pointer to the authoritative Browser Fabric session."""

    schema: str
    binding_id: str
    fabric_session_id: str | None
    parent_binding_id: str | None
    owner_kind: str
    owner_id: str | None
    scope: Dict[str, Any]
    resume_url: str | None
    created_at: float
    updated_at: float


class VisionRunState(TypedDict, total=False):
    """Vision and image-sink state scoped to one agent run."""

    initial_image_count: int
    initial_image_media_types: List[str]
    initial_image_variants: List[str]
    degraded: bool
    last_capture_error: str


class TaskRunState(TypedDict, total=False):
    """Serializable task state for runs that use the main Task lifecycle."""

    task_id: str
    goal: str
    status: str
    context: Dict[str, Any]
    checkpoint_event: str
    checkpoint_created_at: float
    model_name: str


class ActiveInputRunState(TypedDict, total=False):
    """Queued user inputs already delivered into this run's transcript."""

    delivered: List[Dict[str, Any]]


class ToolRunState(TypedDict, total=False):
    enabled_names: List[str]
    disclosed_names: List[str]
    engaged_groups: List[str]
    schema_hash: str
    schema_mode: str
    schema_names: List[str]
    schema_specs: List[Dict[str, Any]]
    unavailable_names: List[str]
    last_actions: List[Dict[str, Any]]
    last_result_text: str
    had_error: bool
    cancelled: bool
    terminate: bool


class OrchestrationRunState(TypedDict, total=False):
    automation_id: str
    automation_name: str
    trigger_source: str


class OutputRunState(TypedDict, total=False):
    mood: str
    reply: str
    completion_status: str
    interrupted: bool
    proactive: bool
    final_summary: str
    stop_reason: str
    terminal_reason: str
    length_recoveries: int


class SessionCapabilitiesRunState(TypedDict, total=False):
    """Mutable session authority snapshotted at run admission."""

    mutation_write_enabled: bool
    mutation_authority_revision: int


class WorkScopeRunState(TypedDict, total=False):
    """Bounded durable attribution projected into a native run snapshot."""

    chat_id: str
    conversation_id: str
    branch_id: str
    workspace_id: str
    workspace_revision: int
    goal_id: str
    goal_run_id: str
    step_id: str
    attempt: int
    worktree_id: str
    kernel_generation: int
    catalog_release_id: str


class ObservabilityEvent(TypedDict, total=False):
    event: str
    fields: Dict[str, Any]


class WorkerRunState(TypedDict, total=False):
    """Loop-routing state for headless workers (subagent/automation).

    Scoped separately from `main` (main-chat-specific Task and desktop state) --
    headless workers are a simpler message loop, so they get their own
    lighter routing state instead of overloading `main`.
    """

    route: str
    mood: str
    reply: str
    reply_fragments: List[str]
    actions: List[Dict[str, Any]]
    thinking: str
    stop_reason: str
    turn_disposition: str
    clean_replays: int
    consecutive_length_recoveries: int
    length_recoveries: int
    terminal_reason: str
    interrupted: bool
    error: str


class RunState(TypedDict, total=False):
    """Top-level serializable state shared by VARIANT-1's execution adapters."""

    run_id: str
    state_schema_version: int
    graph_revision: str
    action_surface: str
    provider_tool_schema_revision: str
    host_context_extents_revision: int
    session_capabilities: SessionCapabilitiesRunState
    work_scope: WorkScopeRunState
    migrated_from_graph_revision: str
    source: str
    title: str
    goal: str
    status: str
    step: int
    created_at: float
    updated_at: float
    thread_id: str
    chat_id: str
    messages: List[Dict[str, Any]]
    task: TaskRunState
    input: ActiveInputRunState
    desktop: DesktopRunState
    browser: BrowserRunState
    vision: VisionRunState
    tools: ToolRunState
    orchestration: OrchestrationRunState
    output: OutputRunState
    observability: List[ObservabilityEvent]
    errors: List[str]
    config_name: str
    worker: WorkerRunState
    # Loop routing + disclosure mirrors only (JSON-serializable). Live Task is
    # sole authority on MainChatLive; task top-level fields are
    # checkpoint projections via project_live_task_bundle.
    main: Any


def _new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:12]


def new_run_state(
    *,
    source: str,
    title: str,
    goal: str,
    config_name: str = "",
    thread_id: Optional[str] = None,
    run_id: Optional[str] = None,
    graph_revision: str = "",
    action_surface: str = "trusted-local.v1",
    provider_tool_schema_revision: str = IPYTHON_SCHEMA_REVISION,
    session_capabilities: Dict[str, Any] | None = None,
    work_scope: Dict[str, Any] | None = None,
    desktop: Dict[str, Any] | None = None,
    browser: Dict[str, Any] | None = None,
) -> RunState:
    """Create a clean state object for one native state-machine run.

    Prefer an existing chat-turn ``run_id`` (from ``Variant1RunContext``) when the
    main-chat machine starts so ``[turn]``, ``[activity]``, and snapshots share
    one identity. Fresh headless workers still mint ``run_…`` ids.
    """
    now = time.time()
    rid = (str(run_id).strip() if run_id else "") or _new_id("run")
    from desktop_fabric.binding import DesktopBinding

    desktop_state = DesktopBinding.from_mapping(
        dict(desktop or {}),
        source=source,
        owner_id=rid,
        scope=work_scope,
    ).to_dict()
    from browser_fabric.binding import BrowserBinding

    browser_state = BrowserBinding.from_mapping(
        dict(browser or {}),
        source=source,
        owner_id=rid,
        scope=work_scope,
    ).to_dict()
    from .session_capabilities import session_capabilities as project_capabilities

    return RunState(
        run_id=rid,
        state_schema_version=RUN_STATE_SCHEMA_VERSION,
        graph_revision=(
            str(graph_revision).strip() or graph_revision_for_source(source)
        ),
        action_surface=str(action_surface or "trusted-local.v1").strip(),
        provider_tool_schema_revision=str(
            provider_tool_schema_revision or IPYTHON_SCHEMA_REVISION
        ).strip(),
        session_capabilities=SessionCapabilitiesRunState(**project_capabilities(
            session_capabilities,
            action_surface=str(action_surface or "trusted-local.v1"),
        )),
        work_scope=WorkScopeRunState(**{
            key: value
            for key, value in dict(work_scope or {}).items()
            if key in WorkScopeRunState.__annotations__
            and value not in (None, "")
        }),
        source=source,
        title=(title or "").strip()[:200],
        goal=(goal or "").strip(),
        status="pending",
        step=0,
        created_at=now,
        updated_at=now,
        thread_id=thread_id or rid,
        messages=[],
        task=TaskRunState(),
        input=ActiveInputRunState(delivered=[]),
        desktop=DesktopRunState(**desktop_state),
        browser=BrowserRunState(**browser_state),
        vision=VisionRunState(degraded=False),
        tools=ToolRunState(),
        orchestration=OrchestrationRunState(),
        output=OutputRunState(mood="neutral", reply="", completion_status="ok"),
        observability=[],
        errors=[],
        config_name=config_name,
    )


def queue_event(state: RunState, event: str, **fields: Any) -> List[ObservabilityEvent]:
    """Record one trace event and return the bounded checkpoint causal tail."""
    try:
        from observability.trace_events import record_trace_event

        record_trace_event(event, **fields)
    except Exception:
        # Tracing is operational evidence, never a graph control dependency.
        pass
    keep_prior = max(0, OBSERVABILITY_TAIL_LIMIT - 1)
    events = list(state.get("observability") or [])[-keep_prior:]
    events.append(ObservabilityEvent(event=event, fields=dict(fields)))
    return events


def task_state_from_task(task: Any) -> TaskRunState:
    """Best-effort serializable projection of agent_task.Task."""
    if task is None:
        return TaskRunState()
    return TaskRunState(
        task_id=str(getattr(task, "id", "") or ""),
        goal=str(getattr(task, "goal", "") or ""),
        status=str(getattr(getattr(task, "status", ""), "value", getattr(task, "status", "")) or ""),
        context=dict(getattr(task, "context", {}) or {}),
    )
