"""Memory, capability, project, prompt, and tool projection chat stage."""

from __future__ import annotations

import logging
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import prompt_builder
import tool_discovery
from chat_context_lineage import ChatContextEvidence, build_chat_context_receipt
from chat_setup_stage import PreparedChatTurn
from chat_stage_result import ChatStageContinue, ChatStageFail, ChatStageResult
from chat_turn_plan import apply_chat_turn_plan, with_tool_catalog
from observability.run_receipts import (
    render_previous_run_receipt,
    wants_previous_run_receipt,
)
from run_context import current_run_context
from session_catalog.profiles import (
    ACTION_SURFACE,
    CHAT_GRAPH_REVISION,
    IPYTHON_SCHEMA_REVISION,
)

if TYPE_CHECKING:
    from chat_pipeline import ChatPorts


_LOG = logging.getLogger(__name__)


def _call_runtime_prompt(callback: Any, session: Any, query: str) -> Any:
    """Call new query-aware ports while preserving older one-arg fixtures."""
    try:
        inspect.signature(callback).bind(session, query)
    except (TypeError, ValueError):
        return callback(session)
    return callback(session, query)


def _runtime_prompt_projection(
    tools_ports: Any,
    session: Any,
    query: str,
) -> prompt_builder.PromptProjection:
    """Resolve split production prompts with a combined-string fixture fallback."""

    callback = getattr(tools_ports, "runtime_prompt_projection", None)
    if callable(callback):
        raw = _call_runtime_prompt(callback, session, query)
        if isinstance(raw, prompt_builder.PromptProjection):
            projection = raw
        elif isinstance(raw, dict):
            projection = prompt_builder.PromptProjection(
                stable=str(raw.get("stable") or "").strip(),
                current=str(raw.get("current") or "").strip(),
            )
        else:
            projection = prompt_builder.PromptProjection(
                stable=str(getattr(raw, "stable", "") or "").strip(),
                current=str(getattr(raw, "current", "") or "").strip(),
            )
        if projection.stable:
            return projection
        raise RuntimeError("ASTB runtime prompt projection is unavailable")

    # Older tests and embedders expose only the historical combined callback.
    combined = str(
        _call_runtime_prompt(tools_ports.runtime_prompt_block, session, query)
        or ""
    ).strip()
    if not combined:
        raise RuntimeError("ASTB runtime prompt projection is unavailable")
    return prompt_builder.PromptProjection(stable=combined)


@dataclass(frozen=True)
class ChatAgentRequest:
    prepared: PreparedChatTurn
    agent_text: str
    base_system: str
    full_tool_specs: list
    model_images: list[dict]
    context_receipt: dict
    turn_ports: Any
    graph_revision: str = CHAT_GRAPH_REVISION
    action_surface: str = ACTION_SURFACE
    provider_tool_schema_revision: str = IPYTHON_SCHEMA_REVISION
    session_capabilities: dict[str, Any] = field(default_factory=dict)


def append_dynamic_memory_context(
    text: str,
    memory_block: str = "",
    run_receipt_block: str = "",
    *,
    current_context: str = "",
) -> str:
    """Place fresh host context beside the request, outside instructions."""
    parts = []
    current = str(current_context or "").strip()
    memory = str(memory_block or "").strip()[:2000]
    previous_run = str(run_receipt_block or "").strip()[:2400]
    if current:
        parts.append("## Current host context\n" + current)
    if memory:
        parts.append("## Relevant memory\n" + memory)
    if previous_run:
        parts.append(
            previous_run
            if previous_run.startswith("##")
            else "## Previous run receipt\n" + previous_run
        )
    if not parts:
        return text
    suffix = (
        "Harness context for this turn (use only when relevant; the current "
        "user request takes precedence):\n\n"
        "<variant1_current_context>\n"
        + "\n\n".join(parts)
        + "\n</variant1_current_context>"
    )
    return str(text or "").rstrip() + "\n\n---\n" + suffix


def project_chat_prompt(
    prompt_context: prompt_builder.PromptContext,
    runtime_projection: prompt_builder.PromptProjection,
    text: str,
    *,
    memory_block: str = "",
    run_receipt_block: str = "",
) -> tuple[str, str]:
    """Return stable instructions and the current model-visible user item."""

    host_projection = prompt_builder.chat_prompt_projection(prompt_context)
    base_system = prompt_builder.assemble(
        host_projection.stable,
        runtime_projection.stable,
    )
    current_context = prompt_builder.assemble(
        host_projection.current,
        runtime_projection.current,
    )
    agent_text = append_dynamic_memory_context(
        text,
        memory_block,
        run_receipt_block,
        current_context=current_context,
    )
    return base_system, agent_text


async def build_chat_context_stage(
    ports: "ChatPorts",
    websocket,
    session,
    prepared: PreparedChatTurn,
) -> ChatStageResult[ChatAgentRequest]:
    """Build the exact graph input and its content-free lineage receipt."""
    plan = prepared.plan
    text = prepared.text
    resume_plan = plan.resume
    prefetch = ports.memory.silent_prefetch or ports.memory.mem_query
    memories = await prefetch(text, 2)

    catalog_specs = ports.tools.provider_specs(session)
    snapshot = tool_discovery.ToolCatalogSnapshot.from_specs(catalog_specs)
    full_tool_specs = [dict(spec) for spec in snapshot.specs]
    run_ctx = current_run_context()
    if run_ctx is not None:
        run_ctx.metadata["tool_catalog"] = snapshot.public_dict()
        run_ctx.metadata["tool_names"] = sorted(snapshot.names)
    plan = with_tool_catalog(plan, snapshot=snapshot)
    prepared = PreparedChatTurn(
        plan,
        attachment_text=prepared.attachment_text,
    )
    apply_chat_turn_plan(session, plan)

    if resume_plan.is_resume:
        resume_task = dict((resume_plan.resume_state or {}).get("task") or {})
        resume_task_id = (
            resume_task.get("task_id")
            or (resume_plan.resume_state or {}).get("run_id")
            or "checkpoint"
        )
        print(
            "[route] conversation resume -- restoring "
            f"{resume_plan.resume_source or 'checkpoint'} {resume_task_id}",
            flush=True,
        )
        try:
            from agent_engine.snapshot_utils import (
                log_snapshot_event,
                summarize_run_state_for_log,
            )

            log_snapshot_event(
                "resume_start",
                source=resume_plan.resume_source or "native_snapshot",
                **summarize_run_state_for_log(resume_plan.resume_state),
            )
        except Exception:
            _LOG.exception("resume-start checkpoint telemetry failed")
    else:
        print("[route] unified conversation", flush=True)

    attachments = plan.attachments
    model_images = list(attachments.user_images or ())
    attachment_context = (
        "The user attached one or more images to this message. Look at all of "
        "them directly to answer."
        if model_images
        else ""
    )
    prompt_context = ports.tools.prompt_context(
        memories,
        attachment_context=attachment_context,
    )
    project_root = str(getattr(prompt_context, "cwd", "") or "").strip()
    project_roots = tuple(getattr(prompt_context, "project_roots", ()) or ())
    run_ctx = current_run_context()
    if run_ctx is not None and project_root:
        run_ctx.metadata["working_directory"] = project_root
        run_ctx.metadata["project_root"] = project_root
        run_ctx.metadata["project_roots"] = list(project_roots or (project_root,))

    previous_run_block = ""
    if wants_previous_run_receipt(attachments.composer_text):
        session_id = str(
            getattr(session.active, "turn_session_id", "") or ""
        ).strip()
        get_receipt = getattr(
            ports.io.sessions, "get_last_run_receipt", None)
        if session_id and callable(get_receipt):
            try:
                previous_run_block = render_previous_run_receipt(
                    get_receipt(session_id))
            except Exception:
                _LOG.exception("previous run receipt could not be rendered")

    dynamic_memory = str(
        getattr(prompt_context, "memory_block", "") or ""
    ).strip()
    prompt_context.memory_block = ""
    diagnostic_ctx = current_run_context()
    diagnostic_run_id = str(
        getattr(diagnostic_ctx, "run_id", "") or "-")
    diagnostic_session_id = str(
        getattr(session.active, "turn_session_id", "") or "-")
    print(
        f"[turn] context run_id={diagnostic_run_id} "
        f"session={diagnostic_session_id} "
        f"cwd={project_root or '-'} "
        f"history={len(session.convo or [])} memories={len(memories or [])} "
        f"images={len(model_images)} "
        f"available_tools={len(full_tool_specs or [])}",
        flush=True,
    )

    runtime_projection = _runtime_prompt_projection(
        ports.tools,
        session,
        text,
    )
    base_system, agent_text = project_chat_prompt(
        prompt_context,
        runtime_projection,
        text,
        memory_block=dynamic_memory,
        run_receipt_block=previous_run_block,
    )
    receipt = build_chat_context_receipt(ChatContextEvidence(
        is_resume=resume_plan.is_resume,
        conversation=list(session.convo or []),
        base_system=base_system,
        prompt_context=prompt_context,
        composer_text=attachments.composer_text,
        model_text=agent_text,
        attachment_suffix=attachments.attach_suffix,
        display_attachments=list(attachments.display_attachments),
        attachment_text=prepared.attachment_text,
        memories=memories,
        catalog_specs=catalog_specs,
        disclosed_tool_specs=full_tool_specs,
        previous_run_block=previous_run_block,
        attachment_context=attachment_context,
        model_images=model_images,
        evidence_snapshot=resume_plan.evidence_ref,
    ))
    if agent_text != text and agent_text.startswith(str(text).rstrip()):
        # This extent comes from the builder's own append, not from searching
        # user-authored text for something that resembles a harness marker.
        receipt["host_context_suffix_chars"] = len(agent_text) - len(str(text).rstrip())
    lineage_ctx = current_run_context()
    if lineage_ctx is not None:
        lineage_ctx.model_input_receipt = receipt

    runtime_identity = dict(ports.tools.runtime_identity(session) or {})
    if not runtime_identity:
        raise RuntimeError("ASTB runtime identity is unavailable")
    from agent_engine.session_capabilities import session_capabilities

    capability_snapshot = session_capabilities(
        runtime_identity,
        action_surface=str(runtime_identity.get("action_surface") or ""),
    )
    resolved_graph_revision = str(
        runtime_identity.get("graph_revision") or ""
    ).strip()
    if not resolved_graph_revision:
        resolved_graph_revision = str(
            ports.tools.graph_revision(session) or ""
        ).strip()
    if not resolved_graph_revision:
        raise RuntimeError("ASTB graph revision is unavailable")
    action_surface = str(runtime_identity.get("action_surface") or "").strip()
    provider_schema_revision = str(
        runtime_identity.get("provider_tool_schema_revision") or ""
    ).strip()
    if not action_surface or not provider_schema_revision:
        raise RuntimeError("ASTB runtime identity is incomplete")
    return ChatStageContinue(ChatAgentRequest(
        prepared=prepared,
        agent_text=agent_text,
        base_system=base_system,
        full_tool_specs=full_tool_specs,
        model_images=model_images,
        context_receipt=receipt,
        turn_ports=ports.tools.build_task_turn_ports(websocket, session),
        graph_revision=resolved_graph_revision,
        action_surface=action_surface,
        provider_tool_schema_revision=provider_schema_revision,
        session_capabilities=capability_snapshot,
    ))
