"""Automation runner: scheduled/webhook work through the headless graph.

Owns the automation run lifecycle — run-context binding, interrupted-prior-run
detection and resume, prompt assembly, the headless worker graph call, history
recording, and proactive delivery — plus the scheduler loop that fires due
automations.

Process dependencies are injected per call through ``AutomationPorts``, which
the composed ``AppHost`` projects for ``HostWorkflowService``. Everything else
is imported directly from dependency-light modules; this domain never reaches
back into the ``server`` composition root.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import background_tasks
from automation import store as automations
from core_invariants import cancellation_is_requested
from observability.activity import clip as _clip
from run_context import bind_run_context, current_run_context
from agent_engine.shared_ports import HeadlessAgentPorts


_AUTOMATION_CLAIMS_INFLIGHT: set[str] = set()


def release_claim_tracking(claim_id: str) -> None:
    _AUTOMATION_CLAIMS_INFLIGHT.discard(str(claim_id or ""))


def prior_incomplete_automation_run(thread_id: str):
    """Most recent durable checkpoint for one automation's thread, only when
    that run never reached a terminal status.

    Scheduled and webhook workers use the run's cancellation predicate. An
    incomplete checkpoint can reflect interruption or a backend restart; it
    must not be mistaken for a completed run. The headless graph checkpoints
    after every worker step, retaining the conversation and step count that
    run_automation() restores via ``resume_snap`` on the next trigger.
    """
    from agent_engine.snapshot_utils import prior_incomplete_run_for_thread

    return prior_incomplete_run_for_thread(thread_id)


@dataclass
class AutomationPorts:
    """Live server dependencies for one automation run, captured at call time."""

    agent: HeadlessAgentPorts
    router: Any
    hub: Any
    history: Any
    emit: Callable[..., Awaitable[None]]
    new_run: Callable[[str, str], Optional[dict]]
    mem_query: Callable[..., Awaitable[list]]
    build_system_prompt: Callable[..., str]
    require_bound_run_context: Callable[..., Any]
    prior_incomplete_run: Callable[[str], Any]
    notify_proactive: Callable[..., Awaitable[None]]


async def execute_automation(
    ports: AutomationPorts,
    task: dict,
    payload: str = "",
    *,
    cancellation_requested=None,
    work_scope=None,
    deliver_result: bool = True,
    record_history: bool = True,
) -> dict[str, Any]:
    from model_runtime.context import normalize_model_route
    route = task.get("model_route") or normalize_model_route(ports.router, ports.router.bound_model_route())
    with ports.router.bind_model_route(route):
        return await _execute_automation(
            ports, {**task, "model_route": route}, payload,
            cancellation_requested=cancellation_requested, work_scope=work_scope,
            deliver_result=deliver_result, record_history=record_history,
        )


async def _execute_automation(
    ports: AutomationPorts, task: dict, payload: str = "", *,
    cancellation_requested=None, work_scope=None,
    deliver_result: bool = True, record_history: bool = True,
) -> dict[str, Any]:
    """Run one automation-family task through the canonical headless worker."""
    worker_source = "automation"
    ctx = current_run_context()
    if not (ctx and ctx.metadata.get("_server_bound_kind") == worker_source):
        name = task.get("name", "automation") if isinstance(task, dict) else "automation"
        auto_ctx = ports.agent.make_run_context(
            worker_source,
            name,
            metadata={"_server_bound_kind": worker_source, "payload": bool(payload),
                      "automation_id": task.get("id", "") if isinstance(task, dict) else "",
                      "automation_name": name,
                      "work_scope": work_scope or {}},
            inherit_parent=False,
            isolate_desktop=True,
        )
        with bind_run_context(auto_ctx):
            return await execute_automation(
                ports,
                task,
                payload=payload,
                cancellation_requested=cancellation_requested,
                work_scope=work_scope,
                deliver_result=deliver_result,
                record_history=record_history,
            )
    name = task.get("name", "automation")
    automation_id = task.get("id") or "unknown"
    started_at = time.time()

    def _record(
        status: str,
        summary: str = "",
        *,
        run_started_at: float | None = None,
        run_finished_at: float | None = None,
    ):
        if not record_history:
            return
        ports.history.add(
            automation_id,
            started_at if run_started_at is None else run_started_at,
            time.time() if run_finished_at is None else run_finished_at,
            status,
            _clip(summary, 300),
        )
        try:
            background_tasks.spawn(ports.hub.broadcast({
                "type": "automations:history",
                "items": ports.history.list(limit=100),
                "count": ports.history.count(),
            }), name="automation-history-broadcast")
        except Exception:
            pass

    if ports.require_bound_run_context(worker_source, operation="run") is None:
        _record("skipped", "automation run context unavailable")
        return {"status": "skipped", "reply": "", "mood": "neutral"}

    prompt = (task.get("prompt") or "").strip()
    if not prompt:
        _record("skipped", "empty prompt")
        return {"status": "skipped", "reply": "", "mood": "neutral"}
    router = ports.router
    from model_runtime.context import validate_worker_model_route
    try:
        validate_worker_model_route(router, task["model_route"])
    except ValueError as exc:
        _record("skipped", str(exc))
        return {"status": "skipped", "reply": str(exc), "mood": "neutral"}
    if not (router.engine_ready or (router.mode == "cloud" and router.cloud_route_ready())):
        ports.new_run("automation", name)
        await ports.emit("task:start", title=name,
                         text=("via webhook" if payload else "scheduled run"))
        await ports.emit("task:done", status="skipped", text="engine not ready")
        print(f"[automation] '{name}' skipped: engine not ready", flush=True)
        _record("skipped", "engine not ready")
        return {"status": "skipped", "reply": "", "mood": "neutral"}

    # Automations are durable by default. Set durable_checkpoints=false on an
    # individual saved automation to keep it ephemeral.
    durable_automation = (
        automations.durable_checkpoints_enabled(task)
        if worker_source == "automation" else False
    )
    thread_id = (
        f"{worker_source}:{task.get('id')}"
        if task.get("id")
        else f"{worker_source}:{ctx.run_id}"
    )
    interrupted_note = ""
    prior_run = ports.prior_incomplete_run(thread_id) if durable_automation else None
    if prior_run:
        prior_started = float(prior_run.get("created_at") or prior_run.get("updated_at") or started_at)
        prior_seen = float(prior_run.get("updated_at") or prior_started)
        _record(
            "interrupted",
            "Interrupted before finishing (app likely restarted mid-run) -- resuming.",
            run_started_at=prior_started,
            run_finished_at=prior_seen,
        )
        interrupted_note = (
            "The previous scheduled run of this automation was interrupted before "
            "finishing (the app appears to have restarted mid-run). You are "
            "continuing that run, not starting over -- the messages above are "
            "your own prior progress on this same task."
        )
        print(
            f"[automation] '{name}' resuming after an interrupted prior run "
            f"(step={prior_run.get('step', 0)})",
            flush=True,
        )
        try:
            from agent_engine.snapshot_utils import (
                log_snapshot_event,
                summarize_run_state_for_log,
            )
            summary = summarize_run_state_for_log(prior_run)
            summary["thread_id"] = thread_id or summary.get("thread_id") or ""
            log_snapshot_event("automation_resume", name=name, **summary)
        except Exception:
            pass

    text = prompt
    if payload:
        text += "\n\n[Incoming webhook payload]\n" + payload[:4000]

    from agent_engine.presets import automation_v1

    prepare_surface = ports.agent.prepare_worker_surface
    if not callable(prepare_surface):
        raise RuntimeError("headless worker surface projection is unavailable")
    worker_surface = prepare_surface(
        source=worker_source,
        key=automation_id,
        query=prompt,
        resume_state=prior_run,
    )
    run_config = automation_v1().with_overrides(
        checkpoints=durable_automation,
        action_surface=worker_surface["action_surface"],
        provider_tool_schema_revision=worker_surface[
            "provider_tool_schema_revision"
        ],
        graph_revision=worker_surface["graph_revision"],
    )
    ctx.run_config = run_config
    ctx.metadata.update({
        "chat_id": worker_surface["runtime_id"],
        "runtime_identity": dict(worker_surface.get("runtime_identity") or {}),
        "runtime_prompt": str(worker_surface.get("runtime_prompt") or ""),
        "worker_source": worker_source,
    })

    memories = await ports.mem_query(text, 5)
    system_prompt = ports.build_system_prompt(
        memories, "Scheduled run; no user is present."
    )
    system_prompt += "\n\n[Automated run: complete the task and give a short proactive result.]"
    tool_surface = ports.agent.tools
    context = ports.agent.context
    projected_specs = worker_surface.get("provider_specs")
    if not isinstance(projected_specs, list):
        raise RuntimeError("headless worker has no pinned provider projection")
    tspec = [dict(spec) for spec in projected_specs]
    enabled_now = {
        str(spec.get("name") or "") for spec in tspec if spec.get("name")
    }
    if len(tspec) != 1 or enabled_now != {"ipython"}:
        raise RuntimeError("headless worker projection must contain only ipython")
    runtime_prompt = str(worker_surface.get("runtime_prompt") or "").strip()
    if runtime_prompt:
        system_prompt += "\n\n" + runtime_prompt
    system_prompt += tool_surface.tools_prompt_block(enabled_now, tspec)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}]

    # When resuming, headless_worker_prepare_node uses resume_snap["messages"]
    # in place of the fresh messages above -- but with today's system prompt
    # (current tools/memories/mode) spliced in and the interrupted_note added,
    # not the stale one from the interrupted run, mirroring how main chat
    # resume refreshes its system prefix (snapshot_utils.restore_messages_from_run_state).
    resume_snap = None
    if prior_run:
        from agent_engine.snapshot_utils import restore_messages_from_run_state

        resume_snap = dict(prior_run)
        resume_snap["messages"] = restore_messages_from_run_state(
            prior_run, system_prompt, resume_context=interrupted_note,
        )

    # Activity run — surfaces this headless job live in the Activity Monitor, so a
    # scheduled/webhook task isn't invisible while it works.
    ports.new_run(worker_source, name)
    await ports.emit("task:start", title=name,
                     text=("via webhook" if payload else "scheduled run"))
    from agent_engine.executor import execute_headless_worker

    async def _stream_worker(msgs, max_tokens, bound_specs=None):
        # Every worker receives the same single IPython action.
        import llm_router
        return await llm_router.complete_turn(
            router, msgs, profile="agent_turn",
            max_tokens=max_tokens, tools=list(bound_specs or []) or None,
            should_stop=should_stop,
        )

    def should_stop() -> bool:
        return cancellation_is_requested(cancellation_requested)

    async def run_worker_actions(actions):
        return await tool_surface.run_actions_headless(
            actions,
            should_stop=should_stop,
        )

    admission_id = ""
    final_status = "error"
    try:
        begin_worker = ports.agent.begin_worker_run
        if callable(begin_worker):
            admission_id = await begin_worker(
                worker_surface,
                thread_id=thread_id,
                run_id=ctx.run_id,
            )
        run_state = await execute_headless_worker(
            snapshot_store=ports.agent.snapshot_store,
            config=run_config,
            title=name,
            goal=prompt,
            messages=messages,
            full_tspec=tspec,
            stream=_stream_worker,
            stream_tools=_stream_worker,
            run_actions=run_worker_actions,
            emit=ports.emit,
            compress=context.compress_messages,
            approx_tokens=context.approx_tokens,
            ctx_threshold=context.ctx_compress_threshold,
            should_stop=should_stop,
            clip=_clip,
            orchestration={
                "automation_id": task.get("id") or "",
                "automation_name": name,
                "trigger": dict(task.get("trigger") or {}),
                "durable_checkpoints": durable_automation,
            },
            thread_id=thread_id,
            is_resume=bool(prior_run),
            resume_snap=resume_snap,
        )
        final_status = str(run_state.get("status") or "completed")
    except asyncio.CancelledError:
        final_status = "cancelled"
        _record("cancelled", "Work job cancelled")
        await ports.emit("task:done", status="cancelled", text="Work job cancelled")
        raise
    except Exception as e:
        err = _clip(str(e), 200)
        _record("error", err)
        await ports.emit("task:done", status="error", text=err)
        raise
    finally:
        finish_worker = ports.agent.finish_worker_run
        if callable(finish_worker) and admission_id:
            finish_worker(admission_id, status=final_status)
    output = run_state.get("output") or {}
    mood = output.get("mood") or "neutral"
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
    result_fields = {
        "reply": reply,
        "mood": str(mood or "neutral"),
        "run_id": str(run_state.get("run_id") or ""),
        "thread_id": thread_id,
        "native_status": native_status,
        "completion_status": completion_status,
    }
    if native_cancelled:
        summary = _clip(reply, 300) if reply else "Work job cancelled"
        await ports.emit("task:done", status="cancelled", text=summary)
        _record("cancelled", summary)
        return {"status": "cancelled", "diagnostic": summary, **result_fields}

    native_truncated = (
        native_status == "truncated" or completion_status == "truncated"
    )
    if native_truncated:
        diagnostic = "Native output ended at its limit before completion."
        if reply:
            diagnostic += " Partial output: " + reply
        diagnostic = _clip(diagnostic, 300)
        print(f"[automation] '{name}' truncated", flush=True)
        await ports.emit("task:done", status="error", text=diagnostic)
        _record("error", diagnostic)
        return {
            "status": "truncated",
            "diagnostic": diagnostic,
            "partial": True,
            **result_fields,
        }

    native_failed = (
        native_status in {"error", "failed"}
        or completion_status in {"error", "failed"}
        or native_status not in {"completed", "ok"}
    )
    if native_failed:
        err = _clip(
            "; ".join(run_state.get("errors") or [])
            or reply
            or f"native worker ended with status {native_status or 'missing'}",
            200,
        )
        print(f"[automation] '{name}' error: {err}", flush=True)
        await ports.emit("task:done", status="error", text=err)
        _record("error", err)
        return {"status": "error", "diagnostic": err, **result_fields}

    await ports.emit("task:done", status="ok",
                     text=_clip(reply, 300) if reply else "Finished (no message).")
    _record("ok", reply or "Finished (no message).")
    if reply and deliver_result:
        await ports.notify_proactive(
            mood, reply, source=worker_source,
            title=name, meta={"automation_id": automation_id},
        )
        print(f"[automation] '{name}' delivered", flush=True)
    return {"status": native_status, **result_fields}


async def automation_loop(store: Any, run: Callable[..., Awaitable[str]]) -> None:
    """Wake periodically and run any scheduled task that's due."""
    print(f"[automation] scheduler started (croniter={automations.HAS_CRONITER})", flush=True)

    async def _dispatch(task: dict, *, payload: str = "",
                        trigger_source: str = "schedule",
                        tracking_id: str = ""):
        try:
            await run(
                task, payload=payload, trigger_source=trigger_source
            )
        except asyncio.CancelledError:
            if tracking_id:
                _AUTOMATION_CLAIMS_INFLIGHT.discard(tracking_id)
            raise
        except BaseException:
            if tracking_id:
                _AUTOMATION_CLAIMS_INFLIGHT.discard(tracking_id)
            raise

    async def _spawn_claims(claims) -> list[dict]:
        due: list[dict] = []
        for claim in list(claims or ()):
            task = dict(claim.get("task") or {})
            if not task:
                continue
            claim_id = str(claim.get("claim_id") or "")
            if claim_id and claim_id in _AUTOMATION_CLAIMS_INFLIGHT:
                continue
            if not store.claim_is_active(
                str(task.get("id") or ""), claim_id,
                claim_kind="scheduled",
            ):
                continue
            due.append(task)
            print(f"[automation] firing '{task.get('name')}' "
                  f"({automations.describe_trigger(task.get('trigger', {}))})", flush=True)
            if claim_id:
                task["_scheduled_claim_id"] = claim_id
                _AUTOMATION_CLAIMS_INFLIGHT.add(claim_id)
            try:
                await _dispatch(
                    task,
                    tracking_id=claim_id,
                )
            except asyncio.CancelledError:
                if claim_id:
                    _AUTOMATION_CLAIMS_INFLIGHT.discard(claim_id)
                raise
            except Exception as exc:
                if claim_id:
                    _AUTOMATION_CLAIMS_INFLIGHT.discard(claim_id)
                print(f"[automation] claim {claim_id} awaits retry: {exc}", flush=True)
        return due

    async def _spawn_durable_trigger_claims(claims, *, marker: str) -> None:
        for claim in list(claims or ()):
            claim_id = str(claim.get("claim_id") or "")
            task = dict(claim.get("task") or {})
            if (
                not claim_id or not task
                or claim_id in _AUTOMATION_CLAIMS_INFLIGHT
            ):
                continue
            if not store.claim_is_active(
                str(task.get("id") or ""), claim_id,
                claim_kind="trigger",
            ):
                continue
            task[marker] = claim_id
            _AUTOMATION_CLAIMS_INFLIGHT.add(claim_id)
            try:
                await _dispatch(
                    task,
                    payload=str(claim.get("payload") or ""),
                    trigger_source=str(claim.get("source") or "trigger"),
                    tracking_id=claim_id,
                )
            except asyncio.CancelledError:
                _AUTOMATION_CLAIMS_INFLIGHT.discard(claim_id)
                raise
            except Exception as exc:
                _AUTOMATION_CLAIMS_INFLIGHT.discard(claim_id)
                print(f"[automation] claim {claim_id} awaits retry: {exc}", flush=True)

    async def _recover_durable_claims() -> None:
        await _spawn_claims(store.recover_scheduled_claims() or ())
        await _spawn_durable_trigger_claims(
            store.recover_trigger_claims() or (), marker="_trigger_claim_id"
        )

    try:
        await _recover_durable_claims()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[automation] initial recovery awaits retry: {exc}", flush=True)

    while True:
        try:
            await asyncio.sleep(30)
            await _recover_durable_claims()
            claims = list(store.claim_due_tasks() or ())
            due = [dict(item.get("task") or {}) for item in claims]
            if due:
                print(f"[automation] {len(due)} task(s) due: "
                      f"{', '.join(t.get('name', '?') for t in due)}", flush=True)
            await _spawn_claims(claims)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[automation] loop error: {e}", flush=True)
