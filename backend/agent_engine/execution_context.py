"""Framework-neutral run-context construction for VARIANT-1 agent execution."""

from __future__ import annotations

from typing import Any, Optional

from run_context import Variant1RunContext, current_run_context
from tool_discovery import ToolCatalogSnapshot
from work_fabric.scope import coerce_work_scope

from .config import AgentRunConfig
from .agent_runtime import HeadlessWorkerRuntime, MainTaskRuntime
from .state import RunState


def run_context_from_state(
    config: AgentRunConfig,
    state: RunState,
    runtime: Any,
    *,
    thread_id: Optional[str] = None,
) -> Variant1RunContext:
    """Build the live context shared by VARIANT-1's native runners."""
    parent = current_run_context()
    desktop_snapshot = dict(state.get("desktop") or {})
    desktop_binding = None
    try:
        from desktop_fabric.binding import ensure_desktop_binding

        desktop_binding = ensure_desktop_binding(
            desktop_snapshot,
            source=config.source,
            owner_id=str(state.get("run_id") or thread_id or ""),
            scope=state.get("work_scope"),
        )
    except Exception:
        desktop_binding = None
    browser_snapshot = dict(state.get("browser") or {})
    browser_binding = None
    try:
        from browser_fabric.binding import ensure_browser_binding

        browser_binding = ensure_browser_binding(
            browser_snapshot,
            source=config.source,
            owner_id=str(state.get("run_id") or thread_id or ""),
            scope=state.get("work_scope"),
        )
    except Exception:
        browser_binding = None
    image_sink = None
    activity_sink = None
    cancellation = None
    if isinstance(runtime, MainTaskRuntime):
        ports = runtime.ports
        activity_sink = getattr(ports, "emit", None)
        cancellation = getattr(ports, "should_stop", None)
    elif isinstance(runtime, HeadlessWorkerRuntime):
        activity_sink = runtime.emit
        cancellation = runtime.should_stop
    meta: dict[str, Any] = {
        "config_name": config.name,
        "session_capabilities": dict(state.get("session_capabilities") or {}),
        "work_scope": dict(state.get("work_scope") or {}),
    }
    if parent is not None:
        parent_meta = dict(getattr(parent, "metadata", None) or {})
        for key in (
            "working_directory",
            "project_root",
            "project_roots",
            "project_environment",
            "chat_id",
            "runtime_identity",
        ):
            if parent_meta.get(key):
                meta[key] = parent_meta[key]
    tool_catalog = getattr(runtime, "tool_catalog", None)
    if isinstance(tool_catalog, ToolCatalogSnapshot):
        meta["tool_catalog"] = tool_catalog.public_dict()
        meta["tool_names"] = sorted(tool_catalog.names)
    if isinstance(runtime, MainTaskRuntime):
        # Same object as runtime.live - not a second bag.
        from .main_live import MAIN_LIVE_KEY

        meta[MAIN_LIVE_KEY] = runtime.live
    state_run_id = str(state.get("run_id") or "").strip()
    raw_scope: Any = state.get("work_scope")
    if not raw_scope and parent is not None:
        raw_scope = getattr(parent, "work_scope", None)
    scope = coerce_work_scope(raw_scope)
    state_chat_id = str(state.get("chat_id") or meta.get("chat_id") or "").strip()
    if state_chat_id and not scope.chat_id:
        scope = scope.with_updates(chat_id=state_chat_id)
    state["work_scope"] = scope.to_dict(include_empty=False)
    meta["work_scope"] = scope.to_dict(include_empty=False)
    if desktop_binding is not None:
        desktop_binding.set_scope(scope)
        if desktop_binding.owner_kind == "run" and not desktop_binding.owner_id:
            desktop_binding.owner_id = state_run_id or str(thread_id or "")
    if browser_binding is not None:
        browser_binding.set_scope(scope)
        if browser_binding.owner_kind == "run" and not browser_binding.owner_id:
            browser_binding.owner_id = state_run_id or str(thread_id or "")
    parent_id = ""
    if parent is not None and getattr(parent, "run_id", None):
        # Same id as the outer chat turn means this is not a nested child.
        if str(parent.run_id) != state_run_id:
            parent_id = str(parent.run_id)
    return Variant1RunContext.create(
        run_id=state_run_id or None,
        source=config.source,
        work_scope=scope,
        title=state.get("title") or state.get("goal") or "",
        thread_id=thread_id or state.get("thread_id") or "",
        parent_run_id=parent_id,
        session_id=(
            state_chat_id
            or str(getattr(parent, "session_id", "") or "") if parent
            else state_chat_id
        ),
        # Prefer the live chat session from the outer turn when rebinding.
        chat_session=(getattr(parent, "chat_session", None) if parent else None),
        chat_transport=(getattr(parent, "chat_transport", None) if parent else None),
        desktop_binding=desktop_binding,
        browser_binding=browser_binding,
        image_sink=image_sink,
        activity_sink=activity_sink,
        cancellation=cancellation,
        run_config=config,
        available_tool_specs=(
            list(tool_catalog.specs)
            if isinstance(tool_catalog, ToolCatalogSnapshot)
            else ()
        ),
        disclosed_tool_specs=(
            list(getattr(runtime, "disclosed_tool_specs", None) or ())
            if isinstance(runtime, HeadlessWorkerRuntime)
            else list(tool_catalog.specs)
            if isinstance(tool_catalog, ToolCatalogSnapshot)
            else ()
        ),
        model_input_receipt=(
            runtime.context_receipt
            if isinstance(runtime, MainTaskRuntime)
            and isinstance(runtime.context_receipt, dict)
            else None
        ),
        metadata=meta,
    )
