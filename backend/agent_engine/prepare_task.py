"""Main-chat task prepare node: tools, messages, and live loop state."""

from __future__ import annotations

import copy
import json
import time
from typing import Any

from observability import context_lineage
import prompt_builder
import tool_discovery
from message_context_extents import (
    HOST_CONTEXT_EXTENTS_REVISION,
    HOST_CONTEXT_PREFIX_KEY,
    HOST_CONTEXT_SUFFIX_KEY,
)
from agent_task import TaskStatus
from run_context import current_run_context
from transcript_economy import (
    clamp_output_reserve,
    enforce_prompt_admission,
    prompt_input_limit,
)

from .snapshot_utils import (
    build_resume_context_from_run_state,
    infer_disclosed_tools_from_run_state,
    is_terminal_commit_only_state,
    pending_tool_loop_from_run_state,
    restore_messages_from_run_state,
    task_from_run_state,
)
from .task_ports import loop_ports_from_task_turn, task_progress_emit_fields
from .config import (
    AgentRunConfig,
    MAIN_PLAIN_MAX_OUTPUT_TOKENS,
    MAIN_REASONING_MAX_OUTPUT_TOKENS,
)
from .errors import DurableCheckpointUnavailable
from .session_capabilities import mutation_write_elevation
from .agent_runtime import (
    MainTaskRuntime,
    _browser_snapshot,
    _desktop_snapshot,
    _emit_runtime_event,
    _main_checkpoint_projection,
    _main_state,
    _with_run_bindings,
    bind_main_live,
    project_live_task_bundle,
    CKPT_CLEAR,
)
from .state import RunState

def main_prepare_task_node(config: AgentRunConfig, runtime: MainTaskRuntime):
    """Prepare tool disclosure, task state, messages, and checkpoints."""

    async def _node(state: RunState) -> dict:
        ports = runtime.ports
        text = runtime.text
        full_tspec = runtime.full_tspec
        resume_state = runtime.resume_snap if isinstance(runtime.resume_snap, dict) else {}
        resume_task_state = dict(resume_state.get("task") or {}) if resume_state else {}
        resume_goal = str(resume_task_state.get("goal") or resume_state.get("goal") or "").strip()
        resume_active = bool(runtime.is_resume and resume_state)
        terminal_commit_only = bool(
            resume_active and is_terminal_commit_only_state(resume_state)
        )
        pending_tool_loop = None
        if resume_active:
            try:
                pending_tool_loop = pending_tool_loop_from_run_state(resume_state)
            except ValueError as exc:
                raise DurableCheckpointUnavailable(
                    f"main-chat checkpoint tool boundary is inconsistent: {exc}"
                ) from exc
            if (
                pending_tool_loop is not None
                and mutation_write_elevation(
                    resume_state,
                    dict(state.get("session_capabilities") or {}),
                )
            ):
                raise DurableCheckpointUnavailable(
                    "mutation write authority increased after this pending tool call "
                    "was checkpointed; turn mutation off or abandon the interrupted "
                    "run before continuing"
                )
        selectable_tspec = list(full_tspec or [])
        enabled_names = {sp["name"] for sp in selectable_tspec} if selectable_tspec else set()
        engaged_groups: set = set()
        vis0: list[dict] = []

        if resume_active:
            print(
                f"[native_snapshot] resuming task id={resume_task_state.get('task_id') or resume_state.get('run_id')} "
                f"goal={resume_goal[:80]!r} "
                f"(model={resume_task_state.get('model_name') or 'unknown'})",
                flush=True,
            )

        if selectable_tspec:
            prior_disclosed = (
                infer_disclosed_tools_from_run_state(resume_state, enabled_names=enabled_names)
                if resume_active
                else []
            )
            if resume_active and prior_disclosed:
                disclosed = set(prior_disclosed)
                disclosed.update(
                    spec["name"] for spec in tool_discovery.initial_tools(selectable_tspec)
                )
                vis0 = [sp for sp in selectable_tspec if sp["name"] in disclosed]
                disclosed = {sp["name"] for sp in vis0}
                info0 = {"mode": "resume"}
            else:
                if resume_active and not prior_disclosed:
                    print("[tools] resume: no disclosed_tools in checkpoint; re-selecting tools", flush=True)
                vis0 = tool_discovery.initial_tools(selectable_tspec)
                info0 = {"mode": "searchable"}
                disclosed = {sp["name"] for sp in vis0}
            tools_block = ports.setup.build_tools_block(disclosed, vis0)
            schema_hash = tool_discovery.schema_hash(selectable_tspec)
            print(
                f"[tools] disclosed {len(disclosed)}/{len(selectable_tspec)} "
                f"schema_hash={schema_hash} tools={','.join(sorted(disclosed))}",
                flush=True,
            )
        else:
            disclosed = set()
            tools_block = ""
            info0 = {}

        def _publish_disclosed_specs(specs) -> None:
            """Publish the full catalog and current provider-visible subset."""
            ctx = current_run_context()
            session = getattr(ctx, "chat_session", None) if ctx is not None else None
            active = getattr(session, "active", None) if session is not None else None
            if ctx is not None:
                ctx.available_tool_specs = tuple(
                    dict(spec) for spec in (selectable_tspec or ())
                )
                ctx.disclosed_tool_specs = tuple(dict(spec) for spec in (specs or ()))
            if active is not None:
                active.available_tool_specs = tuple(
                    dict(spec) for spec in (selectable_tspec or ())
                )
                active.disclosed_tool_specs = tuple(dict(spec) for spec in (specs or ()))

        _publish_disclosed_specs(vis0 if selectable_tspec else ())
        lineage_ctx = current_run_context()
        lineage_receipt = (
            runtime.context_receipt
            if isinstance(runtime.context_receipt, dict)
            else (
                getattr(lineage_ctx, "model_input_receipt", None)
                if lineage_ctx is not None else None
            )
        )
        if not isinstance(lineage_receipt, dict):
            lineage_receipt = context_lineage.new_receipt(
                "resume" if resume_active else "main_chat_step")
        runtime.context_receipt = lineage_receipt
        if lineage_ctx is not None:
            lineage_ctx.model_input_receipt = lineage_receipt
        context_lineage.add_selection(
            lineage_receipt,
            kind="tool_schema",
            source="tool_registry",
            trust="trusted_application",
            reason="resume_restore" if resume_active else "tool_selection",
            considered=len(selectable_tspec or []),
            kept=len(vis0 if selectable_tspec else ()),
            dropped=max(
                0,
                len(selectable_tspec or [])
                - len(vis0 if selectable_tspec else ()),
            ),
            relevance="selected",
        )
        task = None
        ckpt_created_at = time.time()

        if resume_active:
            try:
                task = task_from_run_state(resume_state)
                if not terminal_commit_only:
                    task.status = TaskStatus.IN_PROGRESS
                ckpt_created_at = float(resume_task_state.get("checkpoint_created_at") or resume_state.get("created_at") or time.time())
                print(
                    f"[native_snapshot] resume restore ok id={task.id}",
                    flush=True,
                )
            except Exception as e:
                raise DurableCheckpointUnavailable(
                    f"main-chat checkpoint could not be restored safely: {e}"
                ) from e

        if task is None:
            task = ports.setup.make_task(text)

        run_ctx = current_run_context()
        if run_ctx is not None:
            cs = getattr(run_ctx, "chat_session", None)
            active = getattr(cs, "active", None) if cs is not None else None
            if active is not None:
                active.task = task
                if resume_active:
                    restored_input = dict(resume_state.get("input") or {})
                    active.delivered_inputs = [
                        dict(row)
                        for row in (restored_input.get("delivered") or ())
                        if isinstance(row, dict)
                    ]

        structured_resume = not config.unified_conversation
        static_system = (
            prompt_builder.assemble(
                runtime.base_system, task.render_static(), tools_block)
            if structured_resume
            else prompt_builder.assemble(runtime.base_system, tools_block)
        )
        if resume_active and resume_state and resume_state.get("messages"):
            resume_ctx = build_resume_context_from_run_state(resume_state, task)
            messages = restore_messages_from_run_state(resume_state, static_system, resume_context=resume_ctx)
            # A checkpoint immediately after model_step ends at an assistant
            # tool-call boundary.  Its call-bound results must be appended
            # before any user message (including the refreshed task state).
            if pending_tool_loop is None and not terminal_commit_only:
                state_block = prompt_builder.assemble(ports.loop.state_block(task))
                messages.append({
                    "role": "user",
                    "content": state_block,
                    HOST_CONTEXT_PREFIX_KEY: len(state_block),
                })
        else:
            continuation = getattr(ports.setup, 'continuation_context', None)
            continuation_note = continuation(text) if callable(continuation) else ''
            state_note = (
                ports.loop.state_block(task)
                if structured_resume
                else continuation_note
            )
            normalized_note = str(state_note or "").strip()
            normalized_text = str(text or "").strip()
            user_content = prompt_builder.assemble(normalized_note, normalized_text)
            messages = (
                [{"role": "system", "content": static_system}]
                + list(runtime.convo_tail)
                + [{"role": "user", "content": user_content}]
            )
            if normalized_note:
                messages[-1][HOST_CONTEXT_PREFIX_KEY] = (
                    len(normalized_note) + (2 if normalized_text else 0)
                )
            suffix_chars = (runtime.context_receipt or {}).get("host_context_suffix_chars", 0)
            if (isinstance(suffix_chars, int) and not isinstance(suffix_chars, bool)
                    and 0 < suffix_chars <= len(messages[-1]["content"])):
                messages[-1][HOST_CONTEXT_SUFFIX_KEY] = suffix_chars
        context_lineage.attach_to_messages(messages, lineage_receipt)

        checkpoint_ref = {"state": state}

        async def _task_checkpoint(event: str):
            if task is None:
                return
            try:
                if event in CKPT_CLEAR:
                    # Terminal state is persisted by the agent node that handles
                    # finalization. TaskStore is not updated from graph paths.
                    return
                projected = _main_checkpoint_projection(
                    checkpoint_ref.get("state") or state,
                    task=task,
                    messages=messages,
                    disclosed=disclosed,
                    engaged_groups=engaged_groups,
                    event=event,
                    checkpoint_created_at=ckpt_created_at,
                    ports=ports,
                    main={"disclosed": disclosed, "engaged_groups": engaged_groups},
                    desktop=_desktop_snapshot((checkpoint_ref.get("state") or state).get("desktop")),
                )
                checkpoint_ref["state"] = projected
            except Exception as e:
                print(f"[agent_runtime] snapshot projection {event} failed: {e}", flush=True)

        await _task_checkpoint("plan_ready")

        img_holder = {"image": None}
        # Adjacent native nodes may execute in separate asyncio contexts.
        # Keep the holder on the shared run context as well as the local
        # ContextVar so a screenshot captured by a later tool node reaches the
        # following model node.
        if run_ctx is not None:
            run_ctx.image_sink = img_holder
        img_token = ports.setup.install_image_sink(img_holder)
        run_title = (task.goal if resume_active else text) or ""
        run = ports.setup.new_run("chat", run_title)
        if run:
            await ports.loop.emit(
                "task:start",
                title=run_title.strip()[:200],
                text=("Resuming interrupted task" if resume_active else "Working on your request"),
                **task_progress_emit_fields(),
            )

        # Do not re-compress until the estimate exceeds the last incompressible
        # floor by the configured growth margin.
        compress_state = {"floor": None}

        def _provider_tools_for_step() -> list:
            ctx = current_run_context()
            session = getattr(ctx, "chat_session", None) if ctx is not None else None
            active = getattr(session, "active", None) if session is not None else None
            visible = list(
                getattr(active, "disclosed_tool_specs", None)
                or getattr(ctx, "disclosed_tool_specs", None)
                or vis0
                or ()
            )
            return tool_discovery.provider_tool_specs(visible)

        def _image_count(image_input) -> int:
            if isinstance(image_input, (list, tuple)):
                return len(image_input)
            return 1 if image_input else 0

        def _approx_provider_prompt(msgs, provider_tools, image_input) -> int:
            estimate = int(ports.context.approx_tokens(msgs) or 0)
            # Transcript estimates exclude tool schemas and provider framing.
            # Include both in the fallback used by cloud and older local runtimes.
            try:
                schema_chars = len(json.dumps(
                    provider_tools or [],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ))
            except (TypeError, ValueError):
                schema_chars = len(str(provider_tools or []))
            estimate += schema_chars // 3
            estimate += 8 * len(msgs) + 64
            estimate += 2_048 * _image_count(image_input)
            return estimate

        async def _measure_provider_prompt(msgs, provider_tools, image_input):
            estimate = _approx_provider_prompt(msgs, provider_tools, image_input)
            counter = getattr(ports.context, "count_tokens", None)
            if counter is not None:
                exact = await counter(
                    msgs,
                    tools=provider_tools,
                    image_b64=image_input,
                )
                if isinstance(exact, int) and exact >= 0:
                    return exact, True
            return estimate, False

        async def _loop_compress(
            msgs,
            *,
            max_output_tokens: int = 0,
            image_input=None,
        ):
            if ports.loop.should_stop():
                return msgs
            threshold = ports.context.ctx_compress_threshold()
            provider_tools = _provider_tools_for_step()
            est, exact_used = await _measure_provider_prompt(
                msgs,
                provider_tools,
                image_input,
            )
            context_lineage.add_transform(
                lineage_receipt,
                kind="budget_evaluated",
                reason="token_threshold",
                token_estimate_before=est,
                limit=threshold,
                estimator=(
                    "provider_tokenizer" if exact_used else "chars_div_3"
                ),
                exact=exact_used,
            )
            transformed = False
            floor = compress_state["floor"]
            if est > threshold and (floor is None or est > floor + 512):
                before_n = len(msgs)
                new_messages = await ports.context.compress_messages(msgs)
                if ports.loop.should_stop():
                    return msgs
                if len(new_messages) < before_n:
                    print(
                        f"[compress] ~{est} tok > threshold; "
                        f"{before_n}->{len(new_messages)} messages",
                        flush=True,
                    )
                    if run:
                        await ports.loop.emit(
                            "note",
                            text="Compacted context to keep going.",
                        )
                    msgs = new_messages
                    transformed = True
                # A failed summary leaves this exact request envelope intact.
                # Comparing it to a message-only estimate loses tool/image and
                # framing costs and can retrigger on every following step.
                if not transformed:
                    compress_state["floor"] = est

            if transformed:
                # Measure the exact envelope after every structural transform;
                # this is the byte-equivalent request generation will receive.
                provider_tools = _provider_tools_for_step()
                est, exact_used = await _measure_provider_prompt(
                    msgs,
                    provider_tools,
                    image_input,
                )
                compress_state["floor"] = est if est > threshold else None

            context_limit_getter = getattr(ports.context, "context_limit", None)
            try:
                context_limit = int(
                    context_limit_getter() if callable(context_limit_getter) else 0
                )
            except (TypeError, ValueError, OverflowError):
                context_limit = 0
            output_reserve = max(0, int(max_output_tokens or 0))
            # Real model windows are never smaller than 2K. Treat tiny values
            # from incomplete adapters/test doubles as unavailable metadata,
            # not as a one-token hard limit.
            if context_limit >= 2_048:
                input_limit = prompt_input_limit(context_limit, output_reserve)
                context_lineage.add_transform(
                    lineage_receipt,
                    kind="budget_evaluated",
                    reason="provider_projection",
                    token_estimate_before=est,
                    limit=input_limit,
                    estimator=(
                        "provider_tokenizer" if exact_used else "chars_div_3"
                    ),
                    exact=exact_used,
                )
                print(
                    f"[context] preflight prompt={est} input_limit={input_limit} "
                    f"context={context_limit} output_reserve={output_reserve} "
                    f"exact={'yes' if exact_used else 'no'} "
                    f"tools={len(provider_tools)} "
                    f"images={_image_count(image_input)}",
                    flush=True,
                )
                enforce_prompt_admission(
                    prompt_tokens=est,
                    context_limit=context_limit,
                    output_reserve=output_reserve,
                    exact=exact_used,
                )
            return msgs

        def _loop_progressive_disclose(actions, groups, disc, msgs):
            if not selectable_tspec:
                return
            ctx = current_run_context()
            session = getattr(ctx, "chat_session", None) if ctx is not None else None
            active = getattr(session, "active", None) if session is not None else None
            visible = list(getattr(ctx, "disclosed_tool_specs", None) or ())
            if not visible:
                visible = list(getattr(active, "disclosed_tool_specs", None) or ())
            delta = [sp for sp in visible if sp.get("name") not in disc]
            if not delta:
                return
            disc |= {sp["name"] for sp in visible if sp.get("name")}
            context_lineage.add_selection(
                lineage_receipt,
                kind="tool_schema",
                source="tool_registry",
                trust="trusted_application",
                reason="tool_search",
                considered=len(selectable_tspec),
                kept=len(visible),
                dropped=max(0, len(selectable_tspec) - len(visible)),
                relevance="searched",
            )
            context_lineage.add_transform(
                lineage_receipt,
                kind="tool_schema_selection",
                reason="tool_search",
                input_count=len(selectable_tspec),
                output_count=len(visible),
                affected_count=len(delta),
            )

        loop_ports = loop_ports_from_task_turn(
            ports,
            compress=_loop_compress,
            progressive_disclose=_loop_progressive_disclose,
            checkpoint=_task_checkpoint,
        )

        loop = {
            "mood": "neutral",
            "reply": "",
            "reply_fragments": [],
            "step": 0,
            "interrupted": False,
            "route": "model_step",
            "actions": [],
            "thinking": "",
            "stop_reason": "",
            "terminal_reason": "",
            "consecutive_length_recoveries": 0,
            "length_recoveries": 0,
        }
        if resume_active:
            # A model-step checkpoint can already contain accepted partial
            # output and spent length recoveries, even without pending tools.
            loop.update(copy.deepcopy((resume_state.get("main") or {}).get("loop") or {}))
        if terminal_commit_only:
            loop["route"] = "finalize"
            loop["actions"] = []
            loop["terminal_commit_only"] = True
        elif pending_tool_loop is not None:
            loop.update(pending_tool_loop)
        # Single live bag owned by the runtime (not a second dict in metadata).
        live = runtime.live
        live.clear()
        requested_max_out = (
            MAIN_REASONING_MAX_OUTPUT_TOKENS
            if ports.setup.use_reasoning()
            else MAIN_PLAIN_MAX_OUTPUT_TOKENS
        )
        context_limit_getter = getattr(ports.context, "context_limit", None)
        try:
            selected_context_limit = int(
                context_limit_getter() if callable(context_limit_getter) else 0
            )
        except (TypeError, ValueError, OverflowError):
            selected_context_limit = 0
        live.assign(
            task=task,
            loop_ports=loop_ports,
            img_holder=img_holder,
            img_token=img_token,
            run=run,
            max_out=clamp_output_reserve(
                selected_context_limit, requested_max_out,
            ),
            engaged_groups=engaged_groups,
            disclosed=disclosed,
            checkpoint_ref=checkpoint_ref,
            context_receipt=lineage_receipt,
        )
        bind_main_live(live)
        # Loop routing is RunState-only; do not store on live.
        main = _main_state(loop, live)
        events = await _emit_runtime_event(
            state,
            ports,
            "agent_runtime:main_prepared",
            source=config.source,
        )
        projected = project_live_task_bundle(
            live,
            checkpoint_event="plan_ready",
            checkpoint_created_at=ckpt_created_at,
            model_name=ports.setup.current_model(),
        )
        resumed_extent_revision = resume_state.get(
            "host_context_extents_revision"
        )
        extent_revision = (
            resumed_extent_revision
            if resume_active
            and type(resumed_extent_revision) is int
            and resumed_extent_revision == HOST_CONTEXT_EXTENTS_REVISION
            else HOST_CONTEXT_EXTENTS_REVISION
            if not resume_active
            else None
        )
        return {
            "updated_at": time.time(),
            "messages": messages,
            "input": dict(
                (
                    resume_state.get("input")
                    if resume_active
                    else state.get("input")
                )
                or {"delivered": []}
            ),
            "main": main,
            **projected,
            "tools": {
                **dict(state.get("tools") or {}),
                "enabled_names": sorted(enabled_names),
                "disclosed_names": sorted(disclosed),
                "engaged_groups": sorted(g for g in engaged_groups if g),
            },
            "desktop": _desktop_snapshot(
                resume_state.get("desktop")
                if resume_active
                else state.get("desktop")
            ),
            "browser": _browser_snapshot(
                resume_state.get("browser")
                if resume_active
                else state.get("browser")
            ),
            "vision": {
                **dict(state.get("vision") or {}),
                # Metadata only. Image bytes remain on MainTaskRuntime so the
                # durable SQLite checkpointer never receives user pixels.
                "initial_image_count": len(runtime.images or []),
                "initial_image_media_types": [
                    str(item.get("media_type") or "")
                    for item in (runtime.images or [])
                    if isinstance(item, dict)
                ],
                "initial_image_variants": [
                    str(item.get("variant") or "full")
                    for item in (runtime.images or [])
                    if isinstance(item, dict)
                ],
                "degraded": False,
            },
            "observability": events,
            **(
                {"host_context_extents_revision": HOST_CONTEXT_EXTENTS_REVISION}
                if extent_revision == HOST_CONTEXT_EXTENTS_REVISION
                else {}
            ),
        }

    return _with_run_bindings(_node)


