"""Ports builder implementations for the AppHost composition root.

Builders take an ``AppHost`` instance and close over its stable services and
installed typed runtime. They never re-read ``server`` module globals.
"""

from __future__ import annotations

from contextlib import aclosing

import asyncio
import os
import time
import uuid
import background_tasks
from automation import runner as automation_runner
import chat_pipeline
from session_catalog import child_worker
import llm_router
import memory_tools
from observability import system_info
import tools
from observability.activity import args_preview as _args_preview
from assistant_turn import AssistantTurn, normalized_stop_reason
from reasoning_summaries import ReasoningBuffer, SUMMARY_STEP_LIMIT, summary_text
from agent_engine.task_ports import (
    TaskLoopCorePorts,
    TaskSetupPorts,
    TaskTurnPorts,
)
from agent_engine.shared_ports import (
    AgentContextPorts,
    HeadlessAgentPorts,
    ToolSurfacePorts,
)
from run_context import current_run_context
from tool_runner import ToolRunnerPorts
import tool_discovery


def agent_context_ports(h, *, compress_messages=None) -> AgentContextPorts:
    """Build VARIANT-1's shared transcript-economy contract once."""
    chat = h.require_runtime().chat
    return AgentContextPorts(
        approx_tokens=chat.approx_tokens,
        ctx_compress_threshold=chat.ctx_compress_threshold,
        compress_messages=compress_messages or chat.compress_messages,
        count_tokens=chat.count_prompt_tokens,
        context_limit=lambda: h.router.projection_budget_tokens(),
    )


def headless_agent_ports(h) -> HeadlessAgentPorts:
    """Build the common capabilities used by all headless agent products."""
    from session_catalog import worker_surface
    runtime = h.require_runtime()

    return HeadlessAgentPorts(
        context=agent_context_ports(h),
        tools=ToolSurfacePorts(
            tools_prompt_block=h.tools_prompt_block,
            run_actions_headless=runtime.actions.run_headless,
        ),
        make_run_context=h.make_run_context,
        snapshot_store=getattr(h.require_runtime().session_runtimes, "snapshot_store", None),
        prepare_worker_surface=lambda **kwargs: worker_surface.prepare_worker_surface(
            h, **kwargs
        ),
        begin_worker_run=lambda assignment, **kwargs: worker_surface.begin_worker_run(
            h, assignment, **kwargs
        ),
        finish_worker_run=lambda admission_id, **kwargs: worker_surface.finish_worker_run(
            h, admission_id, **kwargs
        ),
        delete_worker_runtime=lambda **kwargs: worker_surface.delete_worker_runtime(
            h, **kwargs
        ),
    )


def child_worker_ports(h) -> child_worker.ChildWorkerPorts:
    return child_worker.ChildWorkerPorts(
        agent=headless_agent_ports(h),
        router=h.router,
        emit=h.emit_activity,
        new_run=h.new_run,
        active_session=lambda: (current_run_context().chat_session
                                if current_run_context() else None),
        template_dirs=(
            os.path.join(h.config_dir, "prompts"),
            os.path.join(h.app_root, "config", "prompts"),
        ),
    )


def memory_ports(h) -> memory_tools.MemoryPorts:
    runtime = h.require_runtime()
    return memory_tools.MemoryPorts(
        store=runtime.memory.store,
        sessions=runtime.sessions,
        router=h.router,
        hub=h.hub,
        engine_status_msg=h.engine_status_message,
    )


async def _send_turn_frame(h, websocket, session, payload: dict) -> None:
    """Send to the owner socket, falling through to surviving chat Decks."""

    try:
        await websocket.send_json(payload)
        return
    except Exception as owner_error:
        runtime = h.require_runtime().session_runtimes
        chat_id = str(
            getattr(getattr(session, "active", None), "runtime_chat_id", "")
            or getattr(getattr(session, "active", None), "turn_session_id", "")
            or getattr(session, "viewed_session_id", "")
            or ""
        )
        delivered = False
        for target in runtime.attached_transports(chat_id) if chat_id else ():
            if target is websocket:
                continue
            try:
                await target.send_json(payload)
                delivered = True
            except Exception:
                continue
        if not delivered:
            raise owner_error


def tool_runner_ports(h, websocket, session=None) -> ToolRunnerPorts:
    async def _send_running(name, raw_args, call_id):
        preview = _args_preview(raw_args)
        payload = {
            "type": "tool:activity",
            "event": "tool:start",
            "tool": name,
            "call_id": str(call_id or ""),
            "status": "running",
            "args_preview": preview,
            "text": f"Running {name}…",
        }
        payload.update(chat_pipeline.stream_meta(session))
        await _send_turn_frame(h, websocket, session, payload)
        await h.emit_activity("tool:start", tool=name, status="running",
                              args_preview=preview)

    return ToolRunnerPorts(
        emit=h.emit_activity,
        send_running=_send_running,
        clip=h.clip,
        max_result_chars=tools.MAX_RESULT_CHARS,
    )


def headless_tool_runner_ports(h) -> ToolRunnerPorts:
    async def _send_running(name, raw_args, _call_id):
        await h.emit_activity("tool:start", tool=name, status="running",
                              args_preview=_args_preview(raw_args))

    return ToolRunnerPorts(
        emit=h.emit_activity,
        send_running=_send_running,
        clip=h.clip,
        max_result_chars=tools.MAX_RESULT_CHARS,
    )


def automation_ports(h) -> automation_runner.AutomationPorts:
    """Capture live server dependencies for one automation run.

    Built from the live AppHost.
    """
    runtime = h.require_runtime()
    chat = runtime.chat
    workflows = runtime.workflows
    return automation_runner.AutomationPorts(
        agent=headless_agent_ports(h),
        router=h.router,
        hub=h.hub,
        history=h.automation_history,
        emit=h.emit_activity,
        new_run=h.new_run,
        mem_query=h.mem_query,
        build_system_prompt=chat.build_system_prompt,
        require_bound_run_context=h.require_bound_run_context,
        prior_incomplete_run=workflows.prior_incomplete_automation_run,
        notify_proactive=workflows.notify_proactive,
    )


def chat_ports(h) -> chat_pipeline.ChatPorts:
    """Capture live server dependencies for one chat turn.

    Built from the live AppHost.
    """
    from kernel_runtime import integration as kernel_integration

    runtime = h.require_runtime()
    chat = runtime.chat
    voice = runtime.voice
    workflows = runtime.workflows
    return chat_pipeline.ChatPorts(
        io=chat_pipeline.ChatIoPorts(
            router=h.router,
            hub=h.hub,
            sessions=runtime.sessions,
            emit=h.emit_activity,
            runtime_registry=runtime.session_runtimes,
            children=runtime.catalog.children,
        ),
        memory=chat_pipeline.ChatMemoryPorts(
            mem_query=h.mem_query,
            mem_add=h.mem_add,
            remember_explicit=chat.remember_explicit,
            extract_and_store=chat.extract_and_store,
            silent_prefetch=h.mem_prefetch,
        ),
        vision=chat_pipeline.ChatVisionPorts(
            vision_state=chat.vision_state,
        ),
        tools=chat_pipeline.ChatToolsPorts(
            prompt_context=chat.prompt_context,
            build_task_turn_ports=chat.build_task_turn_ports,
            provider_specs=lambda session: kernel_integration.provider_specs(
                runtime.session_runtimes, session
            ),
            runtime_prompt_block=lambda session, query="": kernel_integration.runtime_prompt(
                runtime.catalog, runtime.session_runtimes, session, query
            ),
            runtime_prompt_projection=lambda session, query="": (
                kernel_integration.runtime_prompt_projection(
                    runtime.catalog, runtime.session_runtimes, session, query
                )
            ),
            graph_revision=lambda session: kernel_integration.graph_revision(
                runtime.session_runtimes, session
            ),
            runtime_identity=lambda session: kernel_integration.runtime_identity(
                runtime.session_runtimes, session
            ),
        ),
        session=chat_pipeline.ChatSessionPorts(
            handle_chat=chat.handle_chat,
            make_run_context=h.make_run_context,
            snapshot_resume_state=chat.snapshot_resume_state,
            set_last_user_text=h.set_last_user_text,
            compress_messages=chat.compress_messages,
            snapshot_follow_up_state=chat.snapshot_follow_up_state,
            accept_follow_up_evidence=chat.accept_follow_up_evidence,
        ),
        tts=chat_pipeline.ChatTtsPorts(
            tts_enabled=h.tts_enabled,
            tts_speed=h.tts_speed,
            tts_voice=voice.voice,
            tts_available=voice.available,
            tts_synthesize=voice.synthesize,
            tts_mime_type=voice.mime_type,
        ),
        commands=chat_pipeline.ChatCommandPorts(
            system_status=lambda: system_info.status_text(
                dict(h.tools_cfg.web_search or {})
            ),
        ),
    )


def build_task_turn_ports(h, websocket, session) -> TaskTurnPorts:
    from desktop.service import install_image_sink

    """Wire server callbacks into TaskTurnPorts for one task-classified chat turn."""

    async def _loop_stream(msgs, max_tokens, img_this):
        """Stream one typed assistant turn with the Python provider schema."""
        import tool_calling
        import tool_discovery
        from llm_stream_diagnostics import StreamDiagnostics

        # Provider tools = disclosed ∩ bound (never outside the run snapshot).
        disclosed = getattr(session.active, "disclosed_tool_specs", None)
        tool_specs = tool_discovery.provider_tool_specs(disclosed)
        use_provider_tools = tool_calling.should_send_provider_tools(
            mode=getattr(h.router, "mode", "local"),
            tool_specs=tool_specs,
        )
        attempt_state: dict = {}

        async def _attempt(call_messages, call_images):
            parts: list[str] = []
            accum = (
                tool_calling.ToolCallAccumulator()
                if use_provider_tools else None
            )
            routing = chat_pipeline.stream_meta(session)
            active = session.active

            class SummarySink:
                async def summary_event(self, event):
                    identity = event["summary_id"]
                    row = next((item for item in active.provider_summaries if item["id"] == identity), None)
                    if row is not None and event["summary_revision"] <= row.get("summary_revision", 0):
                        return
                    if row is None:
                        row = {"id": identity, "kind": "thinking", "label": "Reasoning summary",
                               "source": "provider_summary", "ts": event["ts"]}
                        active.provider_summaries.append(row)
                        active.provider_summaries[:] = active.provider_summaries[-SUMMARY_STEP_LIMIT:]
                    row.update(detail=summary_text(event["text"]), status=event["status"],
                               summary_revision=event["summary_revision"])
                    try:
                        await _send_turn_frame(h, websocket, session, {
                            "type": "thinking", "text": row["detail"], "summary_id": identity,
                            "summary_source": "provider_summary", "status": row["status"],
                            "summary_revision": row["summary_revision"], "ts": row["ts"], **routing,
                        })
                    except Exception:
                        pass  # Canonical metadata remains even if display delivery fails.

            summaries = SummarySink()
            reasoning = ReasoningBuffer(summary_sink=summaries)
            diagnostics = StreamDiagnostics()
            attempt_state.clear()
            attempt_state.update({
                "parts": parts,
                "diagnostics": diagnostics,
            })
            stream_kw = {
                "sampling": {"max_tokens": max_tokens},
                "json_mode": False,
                "image_b64": call_images,
                # Provider reasoning is a separate channel. It never becomes
                # assistant text, a tool result, or training transcript content.
                "reasoning_sink": reasoning,
                "stream_diagnostics": diagnostics,
            }
            if use_provider_tools:
                stream_kw["tools"] = tool_specs
                stream_kw["tool_call_sink"] = accum
            if call_images:
                stream_kw["route"] = h.router.mode
            try:
                async with aclosing(h.router.stream(call_messages, **stream_kw)) as owned_stream:
                    async for tok in owned_stream:
                        if session.interrupt:
                            break
                        if not tok:
                            continue
                        parts.append(tok)
                        # Model tokens remain on the owning chat's transport.
                        await _send_turn_frame(h, websocket, session, {
                            "type": "token", "token": tok, **routing,
                        })
            except BaseException as error:
                await reasoning.finish("cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "discarded")
                raise
            await reasoning.finish("cancelled" if session.interrupt else "done")
            text = "".join(parts)
            summary = reasoning.public_text().strip()
            if summary and not session.interrupt and not reasoning.events:
                # Some providers supply only a completed public summary.
                await summaries.summary_event({"summary_id": "summary_" + uuid.uuid4().hex,
                    "text": summary, "status": "done", "summary_revision": 1, "ts": time.time() * 1000})
            actions = accum.actions() if accum is not None else []
            if actions:
                print(f"[tools] provider tool_calls n={len(actions)} "
                      f"names={[a.get('tool') for a in actions]}", flush=True)
            return AssistantTurn(
                text=text,
                thinking=reasoning.text(),
                tool_calls=tuple(actions),
                stop_reason=normalized_stop_reason(
                    diagnostics.finish_reason,
                    has_tool_calls=bool(actions),
                ),
            )

        try:
            return await _attempt(msgs, img_this)
        except Exception as exc:
            from model_runtime.image_fallback import (
                append_visual_description,
                describe_for_text_retry,
                looks_like_image_rejection,
            )

            diagnostics = attempt_state.get("diagnostics")
            observable_output = bool(attempt_state.get("parts")) or bool(
                getattr(diagnostics, "tool_deltas", 0)
            )
            if (
                not img_this
                or observable_output
                or not looks_like_image_rejection(exc)
            ):
                raise
            description = await describe_for_text_retry(
                h.router,
                img_this,
                rejected_route=str(getattr(h.router, "mode", "") or ""),
                should_stop=lambda: session.interrupt,
            )
            retry_messages = append_visual_description(msgs, description)
            try:
                from observability import context_lineage

                context_lineage.add_current_run_transform(
                    kind="image_text_fallback",
                    reason="native_image_rejected",
                    input_count=(
                        len(img_this)
                        if isinstance(img_this, (list, tuple)) else 1
                    ),
                    output_count=1 if description else 0,
                    image_count=(
                        len(img_this)
                        if isinstance(img_this, (list, tuple)) else 1
                    ),
                )
            except Exception:
                pass
            await h.emit_activity(
                "vision:image_fallback",
                status="ok" if description else "degraded",
                reason="native_image_rejected",
                description_available=bool(description),
            )
            return await _attempt(retry_messages, None)

    chat = h.require_runtime().chat

    async def _compress_with_cancel(msgs):
        return await chat.compress_messages(msgs, should_stop=lambda: session.interrupt)

    runtime_registry = h.require_runtime().session_runtimes

    def _runtime_chat_id() -> str:
        return str(
            getattr(session.active, "runtime_chat_id", "")
            or getattr(session.active, "turn_session_id", "")
            or getattr(session, "viewed_session_id", "")
            or ""
        )

    def _runtime_run_id() -> str:
        ctx = current_run_context()
        return str(getattr(ctx, "run_id", "") or "")

    def _drain(delivery: str):
        chat_id = _runtime_chat_id()
        if not chat_id:
            return None
        return runtime_registry.claim_input(
            chat_id, delivery, run_id=_runtime_run_id()
        )

    def _record_active_input(row, assistant_text):
        chat_id = _runtime_chat_id()
        if not chat_id:
            raise RuntimeError("active input delivery has no runtime chat identity")
        result = runtime_registry.record_input_delivery(
            chat_id, session, row, assistant_text
        )
        ticket_id = str((row or {}).get("id") or "")
        if ticket_id:
            background_tasks.spawn(
                h.hub.broadcast({
                    "type": "chat:queue_progress",
                    "id": ticket_id,
                    "delivery": str((row or {}).get("delivery") or "steer"),
                    "state": "delivered",
                    "session_id": chat_id,
                    "queue_size": runtime_registry.queued_input_count(chat_id),
                    "queue": runtime_registry.queue_snapshot(chat_id),
                }),
                name=f"chat-input-delivered:{ticket_id[:48]}",
            )
        return result

    def _continuation_context(text: str) -> str:
        from observability.run_receipts import render_previous_execution_evidence

        runtime = h.require_runtime()
        chat_id = _runtime_chat_id()
        mutation_enabled = False
        if chat_id:
            try:
                authority = runtime.catalog.mutation.authority_status(chat_id)
                mutation_enabled = bool(authority.get('effective_write_enabled'))
            except Exception:
                # Unresolved authority must not advertise writing methods.
                # Native tools and ordinary Python remain available.
                pass
        note = runtime.kernel.continuation_context(
            chat_id, current_user_text=text,
            mutation_enabled=mutation_enabled,
        )
        receipt_id = str(getattr(session.active, 'turn_session_id', '') or chat_id)
        evidence = render_previous_execution_evidence(runtime.sessions.get_last_run_receipt(receipt_id))
        return '\n\n'.join(part for part in (note, evidence) if part)

    async def _wait_if_paused():
        registry = h.require_runtime().session_runtimes
        admission_id = str(getattr(session.active, "runtime_admission_id", "") or "")
        if not admission_id:
            return
        async def publish(payload):
            transports = list(registry.attached_transports(_runtime_chat_id()))
            if websocket not in transports:
                transports.append(websocket)
            await asyncio.gather(*(transport.send_json(payload) for transport in transports),
                                 return_exceptions=True)
        await registry.wait_if_paused(_runtime_chat_id(), admission_id, publish)

    return TaskTurnPorts(
        snapshot_store=getattr(h.require_runtime().session_runtimes, "snapshot_store", None),
        setup=TaskSetupPorts(
            build_tools_block=h.tools_prompt_block,
            tool_lines=h.tool_lines,
            registry_get=h.require_runtime().registry.get,
            make_task=lambda goal: h.Task(goal=goal),
            new_run=h.new_run,
            install_image_sink=install_image_sink,
            use_reasoning=lambda: h.router.reasoning,
            current_model=h.active_model_name,
            continuation_context=_continuation_context,
        ),
        loop=TaskLoopCorePorts(
            stream=_loop_stream,
            run_actions=lambda acts: h.require_runtime().actions.run_interactive(
                websocket, acts, should_stop=lambda: session.interrupt),
            emit=h.emit_activity,
            should_stop=lambda: session.interrupt,
            clip=h.clip,
            state_block=h.state_block,
            drain_steering=lambda: _drain("steer"),
            drain_follow_up=lambda: _drain("follow_up"),
            record_active_input=_record_active_input,
            wait_if_paused=_wait_if_paused,
        ),
        context=agent_context_ports(h, compress_messages=_compress_with_cancel),
    )
