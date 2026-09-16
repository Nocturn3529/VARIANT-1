"""The WebSocket-facing chat turn pipeline.

One ordinary user turn flows: pure ``ChatTurnPlan`` setup (attachments, engine
gate, resume, session bind, project capability) → product ``ChatRoute``
hooks (direct commands and memory) → memory, attachment context,
and tool discovery → the native agent runner → shared turn finalizer
(show, speak, persist, remember).
The model answers directly or calls the provider-visible persistent-Python action; a direct first response
exits the state machine immediately.

Setup decisions live on an immutable ``ChatTurnPlan`` (``chat_turn_plan.py``)
applied once via ``apply_chat_turn_plan``; cancel clears ``session.active``
(plan + mirrors). Server dependencies are injected per call through
``ChatPorts``. The native executor is imported lazily inside the task path.
Product-specific branches live in ``chat_routes`` plugins.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from chat_turn_plan import resolve_session_id
from llm_router import observe_usage
from model_runtime.context import session_model_route
from run_context import bind_run_context, current_run_context
from observability.run_receipts import (
    build_run_receipt,
)
from observability.trace_events import (
    record_trace_event,
    trace_durability_barrier,
)
from chat_attachments import (
    attachment_labels_from_suffix,
    parse_chat_attachments,
    strip_inlined_attachments,  # re-export for tests / callers
)
from chat_stream import bind_turn_identity, infer_turn_source, stream_meta
from speech.providers import AudioResult


_LOG = logging.getLogger(__name__)


class ChatTurnStageError(RuntimeError):
    """A turn-stage contract failure with a safe user-facing explanation."""

    def __init__(self, message: str, *, user_message: str):
        super().__init__(message)
        self.user_message = user_message

# --- Structured ChatPorts (nested groups only) --------------------------------
# Production and tests construct nested groups (ports.io / ports.tts / …).


@dataclass
class ChatIoPorts:
    """Transport + activity surface for one chat turn."""

    router: Any
    hub: Any
    sessions: Any
    emit: Callable[..., Awaitable[None]]
    runtime_registry: Any = None
    children: Any = None


@dataclass
class ChatMemoryPorts:
    mem_query: Callable[..., Awaitable[list]]
    mem_add: Callable[..., Awaitable[None]]
    extract_and_store: Callable[..., Awaitable[list[str]]]
    remember_explicit: Optional[Callable[..., Awaitable[bool]]] = None
    silent_prefetch: Optional[Callable[..., Awaitable[list]]] = None


@dataclass
class ChatVisionPorts:
    vision_state: Callable[[], tuple]


@dataclass
class ChatToolsPorts:
    prompt_context: Callable[..., Any]
    build_task_turn_ports: Callable[[Any, Any], Any]
    provider_specs: Callable[[Any], list]
    runtime_prompt_block: Callable[..., str]
    graph_revision: Callable[[Any], str]
    runtime_identity: Callable[[Any], dict]
    runtime_prompt_projection: Optional[Callable[..., Any]] = None


@dataclass
class ChatSessionPorts:
    handle_chat: Callable[..., Awaitable[None]]
    make_run_context: Callable[..., Any]
    snapshot_resume_state: Callable[..., tuple]
    set_last_user_text: Callable[[str], None]
    # Canonical transcript -> disposable model projection compactor.
    compress_messages: Optional[Callable[..., Awaitable[list]]] = None
    snapshot_follow_up_state: Optional[Callable[..., tuple]] = None
    accept_follow_up_evidence: Optional[Callable[..., Any]] = None


@dataclass
class ChatTtsPorts:
    tts_enabled: Callable[[], bool]
    tts_speed: Callable[[], float]
    # Stable persisted voice id for TTS. Hosts without a selected voice may
    # leave it absent and use the speech service's engine default.
    tts_voice: Optional[Callable[[], str]] = None
    # Route-aware speech callbacks supplied by speech-enabled hosts.
    tts_available: Optional[Callable[[], bool]] = None
    tts_synthesize: Optional[Callable[..., Awaitable[AudioResult | bytes]]] = None
    tts_mime_type: Optional[Callable[[], str]] = None


@dataclass
class ChatCommandPorts:
    """Harness-owned implementations for direct slash commands."""

    system_status: Optional[Callable[[], Awaitable[str]]] = None


@dataclass
class ChatPorts:
    """Live server dependencies for one chat turn (nested groups only)."""

    io: ChatIoPorts
    memory: ChatMemoryPorts
    vision: ChatVisionPorts
    tools: ChatToolsPorts
    session: ChatSessionPorts
    tts: ChatTtsPorts
    commands: Optional[ChatCommandPorts] = None


from chat_context_stage import (  # noqa: E402 - ChatPorts TYPE_CHECKING cycle
    append_dynamic_memory_context,
    build_chat_context_stage,
)
from chat_agent_stage import run_chat_agent_stage  # noqa: E402
from chat_route_stage import run_chat_routes_stage  # noqa: E402
from chat_setup_stage import prepare_chat_turn_stage  # noqa: E402
from chat_stage_result import (  # noqa: E402
    ChatStageContinue,
    ChatStageDone,
    ChatStageFail,
)
from chat_finalize import (  # noqa: E402 — after ChatPorts for TYPE_CHECKING consumers
    _SPOKEN_MAX_CHARS,
    finish_chat_turn,
    persist_unfinalized_turn,
    publish_terminal_event,
    settle_undelivered_inputs,
    spoken_lead,
)


async def handle_chat(ports: ChatPorts, websocket, text: str, session,
                      *, resume: bool = False, reserved: bool = False,
                      client_id: str = "", source: str = "",
                      images: list[dict] | None = None,
                      attachment_text: str = "") -> None:
    # Production enters via chat_task (already binds Variant1RunContext with
    # chat_session). Direct callers (smoke tests, rare host paths) need the
    # same bind so prepare can publish disclosed_tool_specs onto session.active.
    # Bind once here — no recursive re-entry (M9).
    ctx = current_run_context()
    if ctx is None or getattr(ctx, "chat_session", None) is None:
        chat_ctx = None
        try:
            chat_ctx = ports.session.make_run_context(
                "chat",
                str(text or "")[:200],
                session=session,
                chat_transport=websocket,
                metadata={
                    "_server_bound_kind": "chat",
                    "resume": bool(resume),
                },
            )
        except Exception as exc:
            raise ChatTurnStageError(
                "chat run-context construction failed",
                user_message="I couldn't initialize this chat turn. Please try again.",
            ) from exc
        if chat_ctx is not None and getattr(chat_ctx, "chat_session", None) is not None:
            with bind_run_context(chat_ctx):
                return await _handle_chat_body(
                    ports, websocket, text, session,
                    resume=resume, reserved=reserved,
                    client_id=client_id, source=source,
                    images=images, attachment_text=attachment_text,
                )
        raise ChatTurnStageError(
            "chat run-context construction returned no bound chat session",
            user_message="I couldn't initialize this chat turn. Please try again.",
        )
    return await _handle_chat_body(
        ports, websocket, text, session,
        resume=resume, reserved=reserved,
        client_id=client_id, source=source,
        images=images, attachment_text=attachment_text,
    )


async def _handle_chat_body(ports: ChatPorts, websocket, text: str, session,
                            *, resume: bool = False, reserved: bool = False,
                            client_id: str = "", source: str = "",
                            images: list[dict] | None = None,
                            attachment_text: str = "") -> None:
    """Sequence executable chat stages and finalize one completed graph turn."""

    async def _stage(name: str, awaitable):
        started = time.perf_counter()
        record_trace_event(
            "chat_stage:start", stage=name, status="running",
        )
        try:
            result = await awaitable
        except BaseException as exc:
            record_trace_event(
                "chat_stage:complete",
                stage=name,
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error_type=type(exc).__name__,
            )
            raise
        stage_status = "error" if isinstance(result, ChatStageFail) else "ok"
        record_trace_event(
            "chat_stage:complete",
            stage=name,
            status=stage_status,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return result

    async def _terminal(outcome) -> bool:
        if isinstance(outcome, ChatStageDone):
            if outcome.payload is not None:
                payload = dict(outcome.payload)
                if payload.get("type") == "done":
                    terminal_text = str(payload.get("text") or "")
                    session.active.terminal_reply = terminal_text
                    durable = await persist_unfinalized_turn(
                        ports.io,
                        websocket,
                        session,
                        terminal_text,
                        mood=str(payload.get("mood") or "neutral"),
                        meta=stream_meta(
                            session, client_id=client_id, source=source
                        ),
                        allow_assistant_only=True,
                    )
                    payload["durable"] = bool(durable)
                    session.active.terminal_sent = True
                    sid = str(
                        getattr(session.active, "turn_session_id", "")
                        or getattr(session, "viewed_session_id", "")
                        or ""
                    )
                    runtimes = getattr(ports.io, "runtime_registry", None)
                    await publish_terminal_event(
                        ports.io.hub,
                        websocket,
                        payload,
                        transports=(
                            runtimes.attached_transports(sid)
                            if runtimes is not None and sid
                            else None
                        ),
                    )
                else:
                    await websocket.send_json(payload)
            if outcome.release_busy:
                session.busy = False
            return True
        if isinstance(outcome, ChatStageFail):
            failure = ChatTurnStageError(
                outcome.error,
                user_message=outcome.user_message,
            )
            if outcome.cause is not None:
                raise failure from outcome.cause
            raise failure
        if not isinstance(outcome, ChatStageContinue):
            raise ChatTurnStageError(
                f"unknown chat stage outcome: {type(outcome).__name__}",
                user_message="I couldn't complete that turn safely. Please try again.",
            )
        return False

    setup = await _stage("setup", prepare_chat_turn_stage(
        ports,
        websocket,
        text,
        session,
        resume=resume,
        reserved=reserved,
        client_id=client_id,
        source=source,
        images=images,
        attachment_text=attachment_text,
    ))
    if await _terminal(setup):
        return
    prepared = setup.value

    routed = await _stage("routes", run_chat_routes_stage(
        ports,
        websocket,
        session,
        prepared,
        reserved=reserved,
    ))
    if await _terminal(routed):
        return
    prepared = routed.value

    context = await _stage("context_projection", build_chat_context_stage(
        ports,
        websocket,
        session,
        prepared,
    ))
    if await _terminal(context):
        return

    agent_turn = await _stage("agent", run_chat_agent_stage(
        ports,
        session,
        context.value,
        reserved=reserved,
    ))
    if await _terminal(agent_turn):
        return
    result = agent_turn.value
    # Publish the exact terminal candidate before the next await. Cancellation
    # in the stage-to-writer handoff must preserve this answer, not manufacture
    # a stop stub.
    session.active.terminal_reply = str(result.reply or "").strip()
    session.active.transcript_id = str(result.transcript_id or "").strip()
    session.active.commit_transcript_terminal = result.commit_transcript_terminal

    await _stage("finalize", finish_chat_turn(
        ports,
        websocket,
        session,
        result.text,
        result.mood,
        result.reply,
        interrupted=result.interrupted,
        run=result.run,
        completion_status=result.completion_status,
        stop_reason=result.stop_reason,
        terminal_reason=result.terminal_reason,
        length_recoveries=result.length_recoveries,
        transcript_id=result.transcript_id,
        commit_transcript_terminal=result.commit_transcript_terminal,
    ))


async def chat_task(ports: ChatPorts, websocket, text, session, *, resume: bool = False,
                    turn_seq=None, client_id: str = "", source: str = "",
                    images: list[dict] | None = None, attachment_text: str = "",
                    runtime_admission_id: str = "", ticket_id: str = ""):
    """Release transferred or locally acquired admission if finalization fails."""
    owned_admission = str(runtime_admission_id or "")

    def take_admission(value: str) -> None:
        nonlocal owned_admission
        owned_admission = value

    try:
        return await _chat_task_owned(ports, websocket, text, session, resume=resume,
            turn_seq=turn_seq, client_id=client_id, source=source, images=images,
            attachment_text=attachment_text, runtime_admission_id=runtime_admission_id,
            ticket_id=ticket_id, _own_admission=take_admission)
    except BaseException:
        registry = getattr(ports.io, "runtime_registry", None)
        admission = owned_admission
        if registry is not None and admission:
            try:
                registry.finish_run(admission, status="admission_interrupted")
            finally:
                if (str(getattr(session.active, "runtime_admission_id", "") or "") == admission
                        and (turn_seq is None or turn_seq == session.latest_turn_seq)):
                    session.busy = False
                    session.clear_active_turn()
        raise


async def _chat_task_owned(ports: ChatPorts, websocket, text, session, *, resume: bool = False,
                    turn_seq=None, client_id: str = "", source: str = "",
                    images: list[dict] | None = None, attachment_text: str = "",
                    runtime_admission_id: str = "", ticket_id: str = "", _own_admission=None):
    """Run the chat turn as a background task with error reporting."""
    runtime_registry = getattr(ports.io, "runtime_registry", None)
    admission_id = str(runtime_admission_id or "")
    bound_sid = (
        runtime_registry.admission_chat_id(admission_id)
        if runtime_registry is not None and admission_id
        else resolve_session_id(
            ports.io.sessions, getattr(session, "viewed_session_id", None)
        )
    )
    ctx = current_run_context()
    if not (ctx and ctx.metadata.get("_server_bound_kind") == "chat"):
        chat_ctx = ports.session.make_run_context(
            "chat",
            text,
            session=session,
            chat_transport=websocket,
            metadata={"_server_bound_kind": "chat", "resume": bool(resume),
                      "chat_id": bound_sid,
                      "turn_seq": turn_seq},
        )
        with bind_run_context(chat_ctx):
            return await chat_task(
                ports, websocket, text, session, resume=resume,
                turn_seq=turn_seq, client_id=client_id, source=source,
                images=images, attachment_text=attachment_text,
                runtime_admission_id=runtime_admission_id,
                ticket_id=ticket_id,
            )
    # Only an explicitly transferred reservation belongs to this task. An
    # ActiveTurn may still expose the finishing predecessor while this task is
    # queued on the durable writer lock; inheriting it would couple two runs to
    # one admission identity.
    if runtime_registry is not None:
        runtime_registry.ensure_runtime(bound_sid)
        writer_lock = runtime_registry.writer_lock(bound_sid)
    else:
        writer_lock = session.turn_lock
    async with writer_lock:
        if runtime_registry is not None:
            if not admission_id:
                admission_id = await runtime_registry.reserve_run(
                    bound_sid,
                    attachment_id=getattr(session, "attachment_id", ""),
                ) or ""
                if not admission_id:
                    if turn_seq is None or turn_seq == session.latest_turn_seq:
                        session.busy = False
                    raise ChatTurnStageError(
                        "durable chat already has an admitted writer",
                        user_message="This chat already has a turn in progress.",
                    )
                if _own_admission is not None:
                    _own_admission(admission_id)
                runtime_registry.bind_admission_task(
                    admission_id, asyncio.current_task()
                )
            session.active.runtime_admission_id = admission_id
            session.active.runtime_chat_id = bound_sid
        if turn_seq is not None and turn_seq != session.latest_turn_seq and not resume:
            if runtime_registry is not None and admission_id:
                runtime_registry.finish_run(admission_id, status="stale_turn")
            return
        ctx_at_admission = current_run_context()
        if runtime_registry is not None and admission_id:
            try:
                runtime_registry.begin_run(
                    admission_id,
                    run_id=str(getattr(ctx_at_admission, "run_id", "") or ""),
                    thread_id=str(getattr(ctx_at_admission, "thread_id", "") or ""),
                    source=source or "chat",
                )
            except BaseException:
                runtime_registry.finish_run(
                    admission_id, status="admission_start_failed"
                )
                if (
                    (turn_seq is None or turn_seq == session.latest_turn_seq)
                    and str(
                        getattr(session.active, "runtime_admission_id", "") or ""
                    ) == admission_id
                ):
                    session.busy = False
                    session.clear_active_turn()
                raise
            if resume:
                try:
                    resume_state, _resume_error = (
                        ports.session.snapshot_resume_state(bound_sid)
                    )
                    resume_task = dict((resume_state or {}).get("task") or {})
                    resume_thread_id = str(
                        (resume_state or {}).get("thread_id")
                        or resume_task.get("task_id")
                        or (resume_state or {}).get("run_id")
                        or ""
                    ).strip()
                    if resume_thread_id:
                        runtime_registry.repository.link_thread(
                            bound_sid, resume_thread_id, source="chat"
                        )
                except Exception:
                    _LOG.exception("resumed checkpoint thread could not be indexed")
        turn_router = getattr(ports.io, "router", None)
        route_token = None
        started = time.perf_counter()
        started_at_wall = time.time()
        usage_events = []
        receipt_wall_time_s = 0.0
        receipt_session_id = ""
        receipt_tool_names: list[str] = []
        receipt: dict[str, Any] = {}
        turn_status = "ok"
        hard_cancelled = False
        cancel_terminal_finalized = False
        run_id = ""
        ctx_now = current_run_context()
        run_id = str(getattr(ctx_now, "run_id", "") or "")
        text_len = len(str(text or ""))
        print(
            f"[turn] start run_id={run_id or '-'} source={source or 'chat'} "
            f"resume={'yes' if resume else 'no'} text_len={text_len} "
            f"client={client_id or '-'}",
            flush=True,
        )

        def _capture_usage(event):
            if isinstance(event, dict):
                usage_events.append(dict(event))

        try:
            model_route = (
                session_model_route(ports.io.sessions, bound_sid, turn_router)
                if turn_router is not None else {}
            )
            if turn_router is not None:
                try:
                    if not ports.io.sessions.get_model_route(bound_sid):
                        ports.io.sessions.set_model_route(bound_sid, model_route)
                except Exception:
                    _LOG.exception("session model route could not be persisted")
            push_route = getattr(turn_router, "push_model_route", None)
            if callable(push_route):
                route_token = push_route(model_route)
            if (turn_router is not None and model_route.get("mode") == "local"
                    and not turn_router.engine_ready):
                try:
                    from model_runtime import engine_manager
                    await engine_manager.ensure_local_engine(turn_router)
                except Exception:
                    # The ordinary engine gate below produces the user-facing error.
                    _LOG.exception("local engine warm-up failed before readiness gate")
            session.active.turn_ticket_id = str(ticket_id or "").strip()[:96]
            with observe_usage(_capture_usage):
                await ports.session.handle_chat(
                    websocket, text, session, resume=resume,
                    reserved=turn_seq is not None,
                    client_id=client_id, source=source,
                    images=images, attachment_text=attachment_text,
                )
        except asyncio.CancelledError:
            session.request_interrupt()
            cancel_context = current_run_context()
            if cancel_context is not None:
                cancel_context.metadata["_terminal_stop_reason"] = "aborted"
                cancel_context.metadata["_terminal_reason"] = "user_cancelled"
            if runtime_registry is not None and admission_id:
                runtime_registry.begin_run_finalization(admission_id)
            cancelled_seq = (
                turn_seq
                if turn_seq is not None
                else int(getattr(session, "latest_turn_seq", 0) or 0)
            )
            terminal_reply = str(
                getattr(session.active, "terminal_reply", "") or ""
            ).strip()
            if (
                bool(getattr(session.active, "turn_persisted", False))
                or terminal_reply
            ):
                # Once the graph has produced an exact terminal answer,
                # cancellation of its persistence/notification boundary must
                # preserve that answer rather than manufacture a stop stub.
                terminal_ctx = current_run_context()
                turn_status = str(
                    ((terminal_ctx.metadata or {}).get("_terminal_status") or "ok")
                    if terminal_ctx is not None
                    else "ok"
                )
                durable = bool(getattr(session.active, "turn_persisted", False))
                if (
                    terminal_reply
                    and not bool(getattr(session.active, "turn_persisted", False))
                ):
                    durable = await persist_unfinalized_turn(
                        ports.io,
                        websocket,
                        session,
                        terminal_reply,
                        mood=str(getattr(session, "mood", "neutral") or "neutral"),
                        meta=stream_meta(
                            session, client_id=client_id, source=source
                        ),
                    )
                if terminal_reply and not bool(
                    getattr(session.active, "terminal_sent", False)
                ):
                    session.active.terminal_sent = True
                    await publish_terminal_event(
                        ports.io.hub,
                        websocket,
                        {
                            "type": "done",
                            "mood": str(
                                getattr(session, "mood", "neutral") or "neutral"
                            ),
                            "text": terminal_reply,
                            "status": turn_status,
                            "durable": bool(durable),
                            **stream_meta(
                                session, client_id=client_id, source=source
                            ),
                        },
                        transports=(
                            runtime_registry.attached_transports(bound_sid)
                            if runtime_registry is not None and bound_sid
                            else None
                        ),
                    )
                cancel_terminal_finalized = True
                session.last_cancelled_turn_seq = cancelled_seq
                print("[cancel] terminal reply preserved", flush=True)
                raise
            turn_status = "cancelled"
            hard_cancelled = True
            print("[cancel] chat turn cancelled", flush=True)
            try:
                done_fields = {"status": "cancelled", "text": "Stopped by user"}
                await ports.io.emit("task:done", **done_fields)
            except Exception:
                _LOG.exception("cancelled task activity event could not be emitted")
            stopped_text = "Task stopped."
            cancel_meta = stream_meta(
                session, client_id=client_id, source=source
            )
            session.active.terminal_reply = stopped_text
            durable = await persist_unfinalized_turn(
                ports.io,
                websocket,
                session,
                stopped_text,
                meta=cancel_meta,
            )
            session.active.terminal_sent = True
            done_event = {
                "type": "done",
                "mood": "neutral",
                "text": stopped_text,
                "cancelled": True,
                "durable": bool(durable),
                **cancel_meta,
            }
            # Cancellation is chat-scoped, so every attached Deck must see the
            # same terminal boundary, not only the writer socket.
            await publish_terminal_event(
                ports.io.hub,
                websocket,
                done_event,
                transports=(
                    runtime_registry.attached_transports(bound_sid)
                    if runtime_registry is not None and bound_sid
                    else None
                ),
            )
            cancel_terminal_finalized = True
            session.last_cancelled_turn_seq = cancelled_seq
            raise
        except Exception as e:
            turn_status = "error"
            error_context = current_run_context()
            if error_context is not None:
                error_context.metadata["_terminal_reason"] = "harness_error"
            _LOG.exception("chat task failed")
            error_reply = str(
                getattr(e, "user_message", "")
                or "The task could not finish because of an internal error. Details are recorded in the backend log.")
            try:
                session.active.terminal_reply = error_reply
                durable = await persist_unfinalized_turn(
                    ports.io,
                    websocket,
                    session,
                    error_reply,
                    mood="concerned",
                    meta=stream_meta(
                        session, client_id=client_id, source=source
                    ),
                )
                session.active.terminal_sent = True
                error_event = {
                    "type": "done",
                    "mood": "concerned",
                    "text": error_reply,
                    "durable": bool(durable),
                    **stream_meta(session, client_id=client_id, source=source),
                }
                await publish_terminal_event(
                    ports.io.hub,
                    websocket,
                    error_event,
                    transports=(
                        runtime_registry.attached_transports(bound_sid)
                        if runtime_registry is not None and bound_sid
                        else None
                    ),
                )
            except Exception:
                _LOG.debug("chat error response could not be sent", exc_info=True)
        finally:
            if runtime_registry is not None and admission_id:
                runtime_registry.begin_run_finalization(admission_id)
            if route_token is not None:
                try:
                    turn_router.reset_model_route(route_token)
                except Exception:
                    _LOG.exception("invocation model route reset failed")
            receipt_wall_time_s = max(0.0, time.perf_counter() - started)
            wall_ms = int(receipt_wall_time_s * 1000)
            ctx_end = current_run_context()
            receipt_tool_names = list(
                ((ctx_end.metadata or {}).get("_tool_call_names") or [])
                if ctx_end else []
            )
            receipt_tool_result_observations = None
            receipt_tool_result_observations_truncated = 0
            if (ctx_end is not None
                    and "_tool_result_observations" in (ctx_end.metadata or {})):
                receipt_tool_result_observations = list(
                    (ctx_end.metadata or {}).get("_tool_result_observations") or []
                )
                receipt_tool_result_observations_truncated = int(
                    (ctx_end.metadata or {}).get(
                        "_tool_result_observations_truncated"
                    ) or 0
                )
            if turn_status == "ok" and ctx_end is not None:
                turn_status = str(
                    (ctx_end.metadata or {}).get("_terminal_status") or turn_status
                )
            terminal_stop_reason = str(
                ((ctx_end.metadata or {}).get("_terminal_stop_reason") or "")
                if ctx_end is not None else ""
            )
            terminal_reason = str(
                ((ctx_end.metadata or {}).get("_terminal_reason") or "")
                if ctx_end is not None else ""
            )
            length_recoveries = int(
                ((ctx_end.metadata or {}).get("_length_recoveries") or 0)
                if ctx_end is not None else 0
            )
            provider_attempts = int(
                ((ctx_end.metadata or {}).get("_provider_attempts") or 0)
                if ctx_end is not None else 0
            )
            clean_replays = int(
                ((ctx_end.metadata or {}).get("_clean_replays") or 0)
                if ctx_end is not None else 0
            )
            from chat_session import turn_chat_id

            receipt_session_id = turn_chat_id(session)
            receipt = build_run_receipt(
                run_id=run_id,
                status=turn_status,
                tool_names=receipt_tool_names,
                usage_events=usage_events,
                wall_time_s=receipt_wall_time_s,
                stop_reason=terminal_stop_reason,
                terminal_reason=terminal_reason,
                length_recoveries=length_recoveries,
                provider_attempts=max(provider_attempts, len(usage_events)),
                clean_replays=clean_replays,
                tool_result_observations=receipt_tool_result_observations,
                tool_result_observations_truncated=(
                    receipt_tool_result_observations_truncated
                ),
            )
            if receipt_session_id:
                if runtime_registry is not None:
                    try:
                        runtime_registry.record_run_usage(
                            receipt_session_id, run_id, receipt
                        )
                    except Exception:
                        _LOG.exception("runtime budget receipt could not be persisted")
                    try:
                        total_delta = runtime_registry.run_usage_delta(admission_id)
                        direct_budget = {
                            "provider_calls": float(receipt.get("llm_calls") or 0),
                            "tokens": float(receipt.get("total_tokens") or 0),
                            "cost_usd": float(receipt.get("cost_usd") or 0),
                            "wall_time_s": float(receipt.get("wall_time_s") or 0),
                        }
                        descendant = {
                            key: round(max(
                                0.0,
                                float(total_delta.get(key) or 0.0)
                                - float(direct_budget.get(key) or 0.0),
                            ), 8)
                            for key in direct_budget
                        }
                        receipt["descendant_usage"] = {
                            "llm_calls": int(descendant["provider_calls"]),
                            "total_tokens": int(descendant["tokens"]),
                            "cost_usd": descendant["cost_usd"],
                            "wall_time_s": round(descendant["wall_time_s"], 3),
                        }
                    except Exception:
                        _LOG.exception("descendant usage delta could not be projected")
                children = getattr(ports.io, "children", None)
                if children is not None:
                    try:
                        tree = children.tree(receipt_session_id, limit=100)
                        items = [
                            dict(item)
                            for item in tree.get("items") or ()
                            if float(item.get("created_at") or 0.0)
                            >= started_at_wall
                        ]
                        terminal_states = {"completed", "failed", "cancelled"}
                        receipt["descendants"] = {
                            "total": len(items),
                            "active": sum(
                                1 for item in items
                                if str(item.get("status") or "")
                                not in terminal_states
                            ),
                            "terminal": sum(
                                1 for item in items
                                if str(item.get("status") or "")
                                in terminal_states
                            ),
                            "ids": [
                                str(item.get("child_id") or "")
                                for item in items[:32]
                                if str(item.get("child_id") or "")
                            ],
                            "truncated": len(items) > 32,
                        }
                    except Exception:
                        _LOG.exception("descendant tree could not be projected")
                try:
                    ports.io.sessions.set_last_run_receipt(receipt_session_id, receipt)
                except Exception:
                    _LOG.exception("run receipt could not be persisted")
            sequence_text = ",".join(receipt_tool_names) or "-"
            print(
                f"[turn] end run_id={run_id or '-'} status={turn_status} "
                f"ms={wall_ms} llm_calls={len(usage_events)} "
                f"tool_calls={len(receipt_tool_names)} tools={sequence_text}",
                flush=True,
            )
            if usage_events:
                by_category = receipt.get("llm_calls_by_category") or {}
                category_text = ", ".join(
                    f"{name} {count}" for name, count in by_category.items()
                )
                category_text = f" ({category_text})" if category_text else ""
                cost_text = (f" · ${receipt['cost_usd']:.6f}" if receipt["cost_known"] else "")
                try:
                    await ports.io.emit(
                        "task:resources", status=turn_status, receipt=receipt,
                        text=(f"Resource receipt: {receipt['llm_calls']} LLM call(s)"
                              f"{category_text} · {receipt['tool_calls']} tool call(s) · "
                              f"{receipt['total_tokens']:,} tokens · "
                              f"{receipt['inference_time_s']:.1f}s inference · "
                              f"{receipt['wall_time_s']:.1f}s wall{cost_text}"),
                    )
                except Exception:
                    # Operational observation is fail-open; it cannot retain a
                    # completed ActiveTurn or change terminal chat semantics.
                    _LOG.exception("resource receipt activity event could not be emitted")
            # No loop remains after this boundary.  Settle every input that
            # was queued too late to be delivered so a failed/aborted (or
            # already completed) turn cannot leak work into the next chat.
            input_settlement_reason = (
                "turn_"
                + str(turn_status or "terminal").strip().lower()
                + "_before_input_delivery"
            )
            settled_inputs, _settled_ticket_ids = await settle_undelivered_inputs(
                ports.io,
                websocket,
                session,
                bound_sid,
                reason=input_settlement_reason,
                runtime_registry=runtime_registry,
            )
            if settled_inputs:
                print(
                    f"[turn] settled_inputs={settled_inputs} "
                    f"reason={input_settlement_reason}",
                    flush=True,
                )
            turn_can_release = not hard_cancelled or cancel_terminal_finalized
            if ((turn_seq is None or turn_seq == session.latest_turn_seq)
                    and turn_can_release):
                session.busy = False
                session.clear_active_turn()
            if runtime_registry is not None and admission_id and turn_can_release:
                runtime_registry.finish_run(admission_id, status=turn_status)
            if turn_can_release:
                receipt["settled"] = True
                receipt["settled_at"] = round(time.time(), 6)
                if receipt_session_id:
                    try:
                        ports.io.sessions.set_last_run_receipt(
                            receipt_session_id, receipt
                        )
                        durable_receipt = ports.io.sessions.get_last_run_receipt(
                            receipt_session_id
                        )
                        if isinstance(durable_receipt, dict) and durable_receipt:
                            receipt = durable_receipt
                    except Exception:
                        _LOG.exception("settled run receipt could not be persisted")
                settled_event = {
                    "type": "run:settled",
                    "schema": "variant1.run-settled.v1",
                    "run_id": run_id,
                    "session_id": receipt_session_id or bound_sid,
                    "status": turn_status,
                    "stop_reason": terminal_stop_reason,
                    "terminal_reason": terminal_reason,
                    "cause_class": receipt.get("cause_class") or "unknown",
                    "length_recoveries": length_recoveries,
                    "settled": True,
                    "receipt": receipt,
                    "client_id": client_id,
                    "source": source or "chat",
                }
                record_trace_event(
                    "run:settled",
                    run_id=run_id,
                    session_id=receipt_session_id or bound_sid,
                    status=turn_status,
                    terminal_reason=terminal_reason,
                    cause_class=receipt.get("cause_class") or "unknown",
                )
                # A single bounded barrier at the lifecycle boundary preserves
                # terminal evidence without making each error/tool event
                # synchronously wait for disk.
                await asyncio.to_thread(trace_durability_barrier, 0.5)
                try:
                    hub = getattr(ports.io, "hub", None)
                    if hub is None:
                        await websocket.send_json(settled_event)
                    else:
                        await publish_terminal_event(
                            hub,
                            websocket,
                            settled_event,
                            transports=(
                                runtime_registry.attached_transports(bound_sid)
                                if runtime_registry is not None and bound_sid
                                else None
                            ),
                        )
                except Exception:
                    _LOG.exception("settled run event could not be emitted")
