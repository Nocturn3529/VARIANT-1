"""WebSocket message dispatch: registry + core chat lifecycle only.

Domain handlers live in ``ws_*.py`` modules that call ``register(on)``.
This file keeps liveness and chat turn start/cancel/queue routing because those
operations touch session task ownership tightly.

Register a handler with ``@on("mtype")``; one function may own several canonical types.
``dispatch()`` returns False for unknown types so the endpoint can reply with
its standard error.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from typing import Any, Awaitable, Callable, Dict

import clarification
import ws_tools
import ws_browser
import ws_memory
import ws_config
import ws_surface
import ws_automations
import ws_inference_platform
import ws_local_models
import ws_service_settings
import ws_work
import ws_chat_sessions
import ws_kernel
import ws_execution
import ws_peers
import ws_goals
import ws_children
import ws_extensions_v2
from ws_protocol import session_chat_id

HANDLERS: Dict[str, Callable[..., Awaitable[None]]] = {}


def on(*mtypes: str):
    """Register a handler for one or more canonical message types."""
    def deco(fn):
        for t in mtypes:
            HANDLERS[t] = fn
        return fn
    return deco


async def dispatch(srv: Any, websocket, session, msg: Any) -> bool:
    """Route one decoded WS message; returns False when the type is unknown.

    Handler exceptions are caught here so one broken adapter cannot tear down
    the entire WebSocket (which previously caused a reconnect loop when
    ``model:list`` raised NameError for a missing ``os`` import).
    """
    if not isinstance(msg, dict):
        try:
            await websocket.send_json({
                "type": "error",
                "error": "invalid_envelope:expected_object",
            })
        except Exception:
            pass
        return True
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype.strip():
        try:
            await websocket.send_json({
                "type": "error",
                "error": "invalid_envelope:type_must_be_nonempty_string",
            })
        except Exception:
            pass
        return True
    fn = HANDLERS.get(mtype)
    if fn is None:
        return False
    if getattr(session, "view_role", "main") == "detached_chat":
        from chat_session import detached_message_error

        error = detached_message_error(msg, str(session.viewed_session_id or ""))
        if error:
            await websocket.send_json({"type": "error", "error": error})
            return True
    try:
        await fn(srv, websocket, session, msg)
    except Exception as exc:
        # Keep implementation details in server-side diagnostics only. The
        # client contract is stable and must not expose paths, credentials, or
        # other exception text from a failed adapter.
        try:
            detail = f"{type(exc).__name__}: {exc}"
        except Exception:
            detail = type(exc).__name__
        try:
            # Always stdout so Electron dual-writes into main0/main1.
            print(f"[ws] handler type={mtype!r} failed: {detail}", flush=True)
        except Exception:
            pass
        try:
            log = getattr(srv, "log", None) or getattr(srv, "_log", None)
        except Exception:
            log = None
        if callable(log):
            try:
                log(f"[ws] handler {mtype!r} failed: {detail}")
            except Exception:
                pass
        try:
            await websocket.send_json({
                "type": "error",
                "error": f"handler_failed:{mtype}",
            })
        except Exception:
            pass
    return True


# ---- Liveness / status ------------------------------------------------------

@on("ping")
async def _ping(srv, websocket, session, msg):
    await websocket.send_json({"type": "pong", "t": time.time()})


@on("status")
async def _status(srv, websocket, session, msg):
    await websocket.send_json(srv.engine_status_message())


# ---- Chat turn lifecycle ----------------------------------------------------

def _session_runtime_registry(srv):
    candidate = srv.require_runtime().session_runtimes
    try:
        from session_runtime import SessionRuntimeRegistry

        return candidate if isinstance(candidate, SessionRuntimeRegistry) else None
    except Exception:
        return None


def _input_reply_fields(msg, session_id=""):
    return {"session_id": str(session_id or msg.get("session_id") or ""),
            "client_id": str(msg.get("client_id") or ""),
            **({"request_id": str(msg["request_id"])} if msg.get("request_id") else {})}


async def _validated_active_input(websocket, msg) -> tuple[str, str] | None:
    """Validate a chat payload before converting it to active-turn input."""
    text = str(msg.get("text") or "")
    if not text.strip():
        await websocket.send_json({
            "type": "chat:queue_rejected",
            "error": "active_input_empty",
            **_input_reply_fields(msg),
        })
        return None
    if msg.get("attachments"):
        await websocket.send_json({
            "type": "chat:queue_rejected",
            "error": "active_input_attachments_not_supported",
            **_input_reply_fields(msg),
        })
        return None
    requested = str(msg.get("delivery") or "").strip().lower()
    delivery = "follow_up" if requested == "follow_up" else "steer"
    return text, delivery

async def _check_run_fence(websocket, registry, chat_id, msg, *, cancelling=False):
    if not msg.get("run_id") and not msg.get("admission_id"):
        return True
    snapshot = registry.snapshot(chat_id) if registry is not None else {}
    if ((msg.get("run_id") and str(msg["run_id"]) != str(snapshot.get("active_run_id") or ""))
            or (msg.get("admission_id") and str(msg["admission_id"]) != str(snapshot.get("active_admission_id") or ""))):
        await websocket.send_json({
            "type": "cancelling" if cancelling else "chat:queue_rejected",
            "accepted": False, "error": "stale_run", "session_id": chat_id,
            "client_id": str(msg.get("client_id") or ""),
            **({"request_id": str(msg["request_id"])} if msg.get("request_id") else {}),
        })
        return False
    return True


@on("chat")
async def _chat(srv, websocket, session, msg, *, queued_ticket=None):
    from chat_turn_plan import resolve_session_id

    connection_session = session
    runtime_registry = _session_runtime_registry(srv)
    if runtime_registry is None:
        raise RuntimeError("chat admission requires SessionRuntimeRegistry")
    sessions = srv.require_runtime().sessions
    requested_sid = msg.get("session_id")
    if requested_sid is not None and (not isinstance(requested_sid, str) or not requested_sid or not sessions.has_session(requested_sid)):
        await websocket.send_json({"type": "chat:rejected", "error": "unknown_session",
                                   "session_id": requested_sid if isinstance(requested_sid, str) else "",
                                   "client_id": str(msg.get("client_id") or "")})
        return
    viewed_sid = requested_sid or resolve_session_id(sessions, getattr(session, "viewed_session_id", None))
    msg = {**msg, "session_id": viewed_sid}
    if not await _check_run_fence(websocket, runtime_registry, viewed_sid, msg):
        return
    if (str(getattr(session, "viewed_session_id", "") or "") != viewed_sid
            or (session.busy and str(getattr(session.active, "runtime_chat_id", "") or viewed_sid) != viewed_sid)):
        # A socket may view B while its original turn bag still belongs to A.
        # Give B a separate turn bag; durable writer/kernel ownership stays in
        # the existing per-chat registry. Both replies retain their chat IDs.
        from chat_session import ConnectionSession
        session = ConnectionSession(viewed_session_id=viewed_sid,
                                    attachment_id=session.attachment_id, mood=session.mood)
    bound_sid = viewed_sid
    if session.busy:
        bound_sid = str(
            getattr(session.active, "runtime_chat_id", "") or bound_sid
        )
    runtime_busy = bool(runtime_registry.is_busy(bound_sid))
    if queued_ticket is not None and (session.busy or runtime_busy):
        raise RuntimeError("chat_busy")
    # ``done`` is a visible response boundary, but the owning turn may still be
    # persisting the transcript or finishing post-turn bookkeeping.  A message
    # submitted in that narrow window is a new turn, not steering for a graph
    # that has already reached its terminal node.  Wait for the known owning
    # task to release its writer, then continue through normal new-turn
    # admission.  This avoids a durable queued ticket that no loop remains to
    # consume.
    terminal_session = session
    owner = (
        runtime_registry.active_run_owner(bound_sid)
        if runtime_busy
        else None
    )
    if owner is not None:
        terminal_session = owner[0]
    terminal_task = getattr(terminal_session.active, "turn_task", None)
    if (
        (session.busy or runtime_busy)
        and (
            bool(getattr(terminal_session.active, "terminal_sent", False))
            or (
                runtime_busy
                and not runtime_registry.accepts_inputs(bound_sid)
            )
        )
        and terminal_task is not None
        and terminal_task is not asyncio.current_task()
    ):
        await asyncio.gather(asyncio.shield(terminal_task), return_exceptions=True)
        # Keep the target captured at message admission across this await.
        # A concurrently displayed chat must not change an already sent input.
        if not await _check_run_fence(websocket, runtime_registry, viewed_sid, msg):
            return
        bound_sid = viewed_sid
        runtime_busy = bool(runtime_registry.is_busy(bound_sid))
    if session.busy or runtime_busy:
        if runtime_busy and not runtime_registry.accepts_inputs(bound_sid):
            await websocket.send_json({
                "type": "chat:queue_rejected",
                "error": "active_turn_finalizing",
                **_input_reply_fields(msg, bound_sid),
            })
            return
        active_input = await _validated_active_input(websocket, msg)
        if active_input is None:
            return
        runtime_registry.attach(
            bound_sid,
            getattr(session, "attachment_id", ""),
            session,
            websocket,
        )
        text, delivery = active_input
        try:
            item = runtime_registry.enqueue_input(
                bound_sid,
                delivery=delivery,
                text=text,
                client_id=str(msg.get("client_id") or ""),
                source=str(msg.get("source") or ""),
                attachment_id=getattr(session, "attachment_id", ""),
                ticket_id=str(msg.get("ticket_id") or ""),
                expected_admission_id=str(msg.get("admission_id") or ""),
                expected_run_id=str(msg.get("run_id") or ""),
            )
        except RuntimeError as exc:
            if str(exc) == "session_configuration_pending":
                await websocket.send_json({"type":"chat:queue_rejected", "error":str(exc),
                                           **_input_reply_fields(msg, bound_sid)})
                return
            if str(exc) == "stale_run":
                await _check_run_fence(websocket, runtime_registry, bound_sid, msg)
                return
            if not runtime_registry.accepts_inputs(bound_sid):
                await websocket.send_json({
                    "type": "chat:queue_rejected",
                    "error": "active_turn_finalizing",
                    **_input_reply_fields(msg, bound_sid),
                })
                return
            raise
        queue_size = runtime_registry.queued_input_count(bound_sid)
        item_id = item.ticket_id
        queue_meta = {**_input_reply_fields(msg, bound_sid), **{
            key.removeprefix("active_"): value
            for key, value in runtime_registry.snapshot(bound_sid).items()
            if key in {"active_admission_id", "active_run_id"} and value}}
        if connection_session.transcribe_task is not None:
            await srv.require_runtime().voice.cancel_transcription(
                websocket, connection_session, session_id=bound_sid)
        # Steering joins the next model/tool boundary. Explicit Stop and
        # kernel interruption remain separate controls; admission never
        # interrupts an in-flight Python cell merely to deliver new text.
        await websocket.send_json({
            "type": "chat:queued",
            "id": item_id,
            "delivery": delivery,
            "queue_size": queue_size,
            "queue": runtime_registry.queue_snapshot(bound_sid),
            **queue_meta,
        })
        return
    from agent_engine.snapshot_utils import is_terminal_commit_only_state

    chat_runtime = getattr(srv.require_runtime(), "chat", None)
    snapshot_resume_state = getattr(chat_runtime, "snapshot_resume_state", None)

    def _resume_snapshot():
        if not callable(snapshot_resume_state):
            return None, "resume snapshot service unavailable"
        return snapshot_resume_state(bound_sid)

    resume_requested = bool(msg.get("resume"))
    if not resume_requested:
        pending_state, _pending_error = _resume_snapshot()
        if is_terminal_commit_only_state(pending_state):
            await websocket.send_json({
                "type": "chat:rejected",
                "error": "transcript_commit_required",
                "text": "Recover the prior completed turn before starting another one.",
                **_input_reply_fields(msg, bound_sid),
            })
            return
    runtime_registry.attach(
        bound_sid,
        getattr(session, "attachment_id", ""),
        session,
        websocket,
    )
    # A new user turn (typed, or a resume request -- both arrive as
    # "chat") supersedes any pending STT transcription too, so a
    # stale transcript can't auto-submit after the user already
    # moved on. See _transcribe_task's docstring for why this is
    # deliberately decoupled from session.interrupt.
    # Validate transient attachments before reserving the durable writer so a
    # malformed payload cannot strand an admission with no owning task.
    from chat_pipeline import parse_chat_attachments

    from chat_attachments import validate_chat_attachments
    try:
        validate_chat_attachments(msg.get("attachments"))
        images, attachment_text = parse_chat_attachments(
            msg.get("attachments"),
            staging_root=os.path.join(str(getattr(srv, "data_dir", "") or "."), "attachments", "chat"),
        )
    except (ValueError, OSError) as exc:
        await websocket.send_json({"type":"chat:rejected", "error":"attachment_preparation_failed",
                                   "text":str(exc), **_input_reply_fields(msg, bound_sid)})
        return
    if not resume_requested and not (str(msg.get("text") or "").strip() or images or attachment_text or msg.get("image")):
        await websocket.send_json({"type":"chat:rejected", "error":"empty_input",
                                   **_input_reply_fields(msg, bound_sid)})
        return
    if connection_session.transcribe_task is not None:
        await srv.require_runtime().voice.cancel_transcription(
            websocket, connection_session, session_id=bound_sid)
    runtime_admission_id = ""
    try:
        runtime_admission_id = await runtime_registry.reserve_run(
            bound_sid,
            attachment_id=getattr(session, "attachment_id", ""),
        ) or ""
    except Exception as exc:
        from session_runtime import BudgetExhausted

        if isinstance(exc, BudgetExhausted):
            await websocket.send_json({
                "type": "chat:rejected",
                "error": "paused_budget_exhausted",
                "text": str(exc),
                **_input_reply_fields(msg, bound_sid),
            })
            return
        raise
    if not runtime_admission_id:
        if queued_ticket is not None:
            raise RuntimeError("chat_busy")
        if runtime_registry.configuration_pending(bound_sid):
            await websocket.send_json({"type":"chat:rejected", "error":"session_configuration_pending",
                                       **_input_reply_fields(msg, bound_sid)})
            return
        # Another window won the admission race after the earlier busy
        # snapshot. Apply the exact busy-path contract before converting
        # the payload to durable active input; attachments must never be
        # silently stripped by this fallback.
        active_input = await _validated_active_input(websocket, msg)
        if active_input is None:
            return
        active_text, active_delivery = active_input
        try:
            item = runtime_registry.enqueue_input(
                bound_sid,
                active_text,
                delivery=active_delivery,
                client_id=str(msg.get("client_id") or ""),
                source=str(msg.get("source") or ""),
                attachment_id=getattr(session, "attachment_id", ""),
                ticket_id=str(msg.get("ticket_id") or ""),
                expected_admission_id=str(msg.get("admission_id") or ""),
                expected_run_id=str(msg.get("run_id") or ""),
            )
        except RuntimeError as exc:
            if str(exc) == "session_configuration_pending":
                await websocket.send_json({"type":"chat:queue_rejected", "error":str(exc),
                                           **_input_reply_fields(msg, bound_sid)})
                return
            if str(exc) == "stale_run":
                await _check_run_fence(websocket, runtime_registry, bound_sid, msg)
                return
            if not runtime_registry.accepts_inputs(bound_sid):
                await websocket.send_json({
                    "type": "chat:queue_rejected",
                    "error": "active_turn_finalizing",
                    **_input_reply_fields(msg, bound_sid),
                })
                return
            raise
        queue_meta = {**_input_reply_fields(msg, bound_sid), **{
            key.removeprefix("active_"): value
            for key, value in runtime_registry.snapshot(bound_sid).items()
            if key in {"active_admission_id", "active_run_id"} and value}}
        await websocket.send_json({
            "type": "chat:queued",
            "id": item.ticket_id,
            "delivery": item.delivery,
            "queue_size": runtime_registry.queued_input_count(bound_sid),
            "queue": runtime_registry.queue_snapshot(bound_sid),
            **queue_meta,
        })
        return
    initial_ticket_id = ""
    if queued_ticket is not None:
        try:
            claimed = runtime_registry.continue_parked_input(
                bound_sid, queued_ticket[0], expected_revision=queued_ticket[1],
                admission_id=runtime_admission_id,
            )
            initial_ticket_id = claimed.ticket_id
        except Exception:
            runtime_registry.finish_run(runtime_admission_id, status="queue_admission_failed")
            raise
    try:
        chat_service = srv.require_runtime().chat
        launcher = getattr(chat_service, "launch_reserved_turn", None)
        arguments = dict(
            runtime_admission_id=runtime_admission_id,
            resume=bool(msg.get("resume")),
            client_id=str(msg.get("client_id") or ""),
            source=("queue_continue" if initial_ticket_id else str(msg.get("source") or "")),
            images=images, attachment_text=attachment_text,
            ticket_id=initial_ticket_id,
        )
        if callable(launcher):
            turn_task = launcher(
                websocket, msg.get("text", ""), session, **arguments,
            )
        else:
            from host_chat_service import launch_reserved_chat_turn

            turn_task = launch_reserved_chat_turn(
                srv, chat_service.run_task,
                websocket, msg.get("text", ""), session, **arguments,
            )
    except Exception:
        raise
    return True


@on("chat:queue:get", "chat:queue:continue", "chat:queue:remove")
async def _queue_command(srv, websocket, session, msg):
    from ws_protocol import session_chat_id
    registry = _session_runtime_registry(srv)
    operation = str(msg.get("type") or "").rsplit(":", 1)[-1]
    chat_id = str(msg.get("session_id") or session_chat_id(session))
    request_id = str(msg.get("request_id") or "")
    accepted, error = False, ""
    try:
        if not registry or not srv.require_runtime().sessions.has_session(chat_id):
            raise ValueError("unknown_session")
        if operation == "get":
            await websocket.send_json({**registry.queue_snapshot(chat_id), "request_id": request_id})
            return
        if not request_id:
            raise ValueError("request_id_required")
        if chat_id != session_chat_id(session):
            raise RuntimeError("stale_queue_selection")
        ticket_id = str(msg.get("ticket_id") or "")
        expected_revision = msg.get("expected_revision")
        if operation == "remove":
            registry.repository.queued_ticket_command(chat_id, ticket_id,
                expected_revision=expected_revision, operation="remove")
            accepted = True
        else:
            snapshot = registry.queue_snapshot(chat_id)
            if type(expected_revision) is not int or expected_revision != snapshot["revision"]:
                raise RuntimeError("stale_queue_revision")
            item = next((row for row in snapshot["items"] if row["ticket_id"] == ticket_id), None)
            if item is None or item["state"] != "parked":
                raise RuntimeError("ticket_not_parked")
            accepted = bool(await _chat(srv, websocket, session, {
                "type": "chat", "session_id": chat_id, "text": item["text"],
                "client_id": request_id, "source": "queue_continue",
            }, queued_ticket=(ticket_id, expected_revision)))
            if not accepted:
                error = "queue_turn_not_started"
    except Exception as exc:
        error = str(exc)
    try:
        snapshot = registry.queue_snapshot(chat_id) if registry and chat_id else None
    except Exception:
        snapshot = None  # Preserve the client's last observation when storage is unavailable.
    response = {"type": "chat:queue_result", "operation": operation,
                "session_id": chat_id, "request_id": request_id,
                "accepted": accepted, "queue": snapshot}
    if error:
        response["error"] = error
    await websocket.send_json(response)
    if accepted and snapshot is not None:
        from chat_finalize import _publish_append
        await _publish_append(srv.hub, websocket, snapshot)


@on("chat:pause")
@on("chat:resume")
async def _pause_or_resume(srv, websocket, session, msg):
    registry = _session_runtime_registry(srv)
    chat_id = str(msg.get("session_id") or getattr(session, "viewed_session_id", "") or "")
    correlation = {"request_id": str(msg["request_id"])} if msg.get("request_id") else {}
    try:
        if registry is None or not chat_id:
            raise RuntimeError("no_active_run")
        payload = registry.set_run_paused(
            chat_id, msg.get("type") == "chat:pause",
            expected_admission_id=str(msg.get("admission_id") or ""),
            expected_run_id=str(msg.get("run_id") or ""),
        )
    except (RuntimeError, ValueError) as exc:
        state = registry.pause_snapshot(chat_id) if registry is not None and chat_id else {
            "type": "chat:pause_state", "session_id": chat_id, "admission_id": "",
            "run_id": "", "state": "idle", "pause_revision": 0,
        }
        await websocket.send_json({**state, **correlation, "accepted": False, "error": str(exc)})
        return
    payload = {**payload, **correlation, "accepted": True}
    transports = list(registry.attached_transports(chat_id))
    if websocket not in transports:
        transports.append(websocket)
    await asyncio.gather(*(transport.send_json(payload) for transport in transports),
                         return_exceptions=True)


@on("cancel")
async def _cancel(srv, websocket, session, msg):
    # Explicit stop button / command. The interrupt flag remains the cooperative
    # boundary used by tools, while cancelling the owning asyncio task makes a
    # no-first-token provider stream or other awaited operation stop promptly.
    host = srv
    if getattr(srv, "hub", None) is None:
        host = getattr(srv, "APP", None) or srv
    runtime_registry = _session_runtime_registry(host)
    runtime_task = None
    runtime_chat_id = str(
        msg.get("session_id") or getattr(session, "viewed_session_id", "")
        or getattr(session.active, "runtime_chat_id", "")
        or ""
    )
    if not await _check_run_fence(websocket, runtime_registry, runtime_chat_id, msg, cancelling=True):
        return
    # Cancellation is chat-scoped, but terminal state belongs to the window
    # that admitted the run. Resolve it before cancelling while the admission
    # still points at its owner attachment.
    owner_session = session
    owner_websocket = websocket
    if runtime_registry is not None and runtime_chat_id:
        owner = runtime_registry.active_run_owner(runtime_chat_id)
        if owner is not None:
            owner_session, owner_transport = owner
            if owner_transport is not None:
                owner_websocket = owner_transport
        elif (str(getattr(session.active, "runtime_chat_id", "") or
                  getattr(session.active, "turn_session_id", "") or runtime_chat_id) != runtime_chat_id):
            # Stopping idle B cannot cancel the A turn bag retained by this
            # socket after navigation. B has no active turn owner to cancel.
            from chat_session import ConnectionSession
            owner_session = ConnectionSession(viewed_session_id=runtime_chat_id)
    if owner_session is not session or not session.busy or str(
        getattr(session.active, "runtime_chat_id", "") or runtime_chat_id
    ) == runtime_chat_id:
        owner_session.request_interrupt()
    owner_turn_seq = int(getattr(owner_session, "latest_turn_seq", 0) or 0)
    admission_id = str(
        getattr(owner_session.active, "runtime_admission_id", "")
        or (
            runtime_registry.active_admission(runtime_chat_id)
            if runtime_registry is not None and runtime_chat_id
            else ""
        )
        or ""
    )
    if runtime_registry is not None and admission_id:
        runtime_registry.begin_run_finalization(admission_id)
    turn_task = getattr(owner_session.active, "turn_task", None)
    if runtime_registry is not None and runtime_chat_id:
        runtime_task = runtime_registry.cancel_active_run(runtime_chat_id)
    chat_cancelled = bool(turn_task is not None and not turn_task.done()
                          and turn_task.cancel())
    chat_cancelled = bool(chat_cancelled or runtime_task is not None)
    transcription_cancelled = False
    preview_cancelled = False
    runtime = host.require_runtime()
    voice = getattr(runtime, "voice", None)
    owners = []
    seen_owners: set[int] = set()
    for candidate_session, candidate_socket in (
        (session, websocket),
        (owner_session, owner_websocket),
    ):
        marker = id(candidate_session)
        if marker in seen_owners:
            continue
        seen_owners.add(marker)
        owners.append((candidate_session, candidate_socket))
    for candidate_session, candidate_socket in owners:
        cancel_transcription = getattr(voice, "cancel_transcription", None)
        if callable(cancel_transcription) and inspect.iscoroutinefunction(
            cancel_transcription
        ):
            transcription_cancelled = bool(
                await cancel_transcription(candidate_socket, candidate_session, session_id=runtime_chat_id)
                or transcription_cancelled
            )
        else:
            # Reduced/headless hosts may omit SpeechService. Preserve generic
            # stop semantics for any connection-owned task without pretending
            # the product's correlated transcript transport exists there.
            task = getattr(candidate_session, "transcribe_task", None)
            if task is not None and not task.done():
                transcription_cancelled = bool(
                    task.cancel() or transcription_cancelled
                )
                await asyncio.gather(task, return_exceptions=True)
            if getattr(candidate_session, "transcribe_task", None) is task:
                candidate_session.transcribe_task = None
        preview_origin = str(getattr(candidate_session, "tts_preview_session_id", "") or "")
        if not preview_origin or preview_origin == runtime_chat_id:
            preview_cancelled = bool(
                await ws_config._cancel_tts_preview(candidate_socket, candidate_session)
                or preview_cancelled)
    cleared_inputs = 0
    print(f"[cancel] received active_turn={'yes' if chat_cancelled else 'no'} "
          f"transcription={'yes' if transcription_cancelled else 'no'} "
          f"speech={'yes' if preview_cancelled else 'no'}", flush=True)
    from chat_pipeline import stream_meta
    meta = stream_meta(owner_session)
    await websocket.send_json({
        "type": "cancelling",
        "accepted": chat_cancelled or transcription_cancelled or preview_cancelled,
        "cleared_inputs": cleared_inputs,
        **meta,
        "session_id": runtime_chat_id,
        **({"request_id": str(msg["request_id"])} if msg.get("request_id") else {}),
    })

    settling = [task for task in (turn_task, runtime_task)
                if task is not None and not task.done()]
    settling = list(dict.fromkeys(settling))
    if settling:
        await asyncio.gather(*settling, return_exceptions=True)

    # A normal chat_task owns cancellation persistence and terminal broadcast.
    # This fallback covers tasks cancelled before their coroutine body entered,
    # plus lightweight/test-owned tasks that do not implement that boundary.
    finalized_by_turn = (
        getattr(owner_session, "last_cancelled_turn_seq", None)
        == owner_turn_seq
    )
    from chat_finalize import (
        persist_unfinalized_turn,
        publish_terminal_event,
        settle_undelivered_inputs,
    )
    await settle_undelivered_inputs(
        host,
        owner_websocket,
        owner_session,
        runtime_chat_id,
        reason="explicit_user_cancel",
        runtime_registry=runtime_registry,
    )
    if chat_cancelled and not finalized_by_turn:
        terminal_reply = str(
            getattr(owner_session.active, "terminal_reply", "") or ""
        ).strip()
        stopped_text = terminal_reply or "Task stopped."
        preserved_terminal = bool(terminal_reply)
        try:
            durable = await persist_unfinalized_turn(
                host, owner_websocket, owner_session, stopped_text, meta=meta,
            )
        except Exception as exc:
            durable = False
            print(f"[cancel] persist interrupted turn failed: {exc}", flush=True)
        owner_session.active.terminal_sent = True
        terminal_event = {
            "type": "done",
            "mood": str(getattr(owner_session, "mood", "neutral") or "neutral"),
            "text": stopped_text,
            "durable": bool(durable),
            **meta,
        }
        if not preserved_terminal:
            terminal_event["cancelled"] = True
        await publish_terminal_event(host.hub, owner_websocket, terminal_event, transports=(
            runtime_registry.attached_transports(runtime_chat_id)
            if runtime_registry is not None and runtime_chat_id
            else None
        ))
        if owner_session.active.turn_task is turn_task:
            owner_session.active.turn_task = None
        owner_session.busy = False
        owner_session.clear_active_turn()
        if runtime_registry is not None and admission_id:
            runtime_registry.finish_run(admission_id, status="cancelled")
    elif chat_cancelled and finalized_by_turn:
        # Defensive completion if a post-persistence observer failed inside the
        # task's finalizer. Correlation prevents this cleanup from touching a
        # newer turn that started after the original admission was released.
        if (
            int(getattr(owner_session, "latest_turn_seq", 0) or 0)
            == owner_turn_seq
            and str(
                getattr(owner_session.active, "runtime_admission_id", "") or ""
            ) == admission_id
        ):
            owner_session.busy = False
            owner_session.clear_active_turn()
        if (
            runtime_registry is not None
            and admission_id
            and runtime_registry.active_admission(runtime_chat_id) == admission_id
        ):
            runtime_registry.finish_run(admission_id, status="cancelled")
    print(f"[cancel] completed active_turn={'yes' if chat_cancelled else 'no'} "
          f"transcription={'yes' if transcription_cancelled else 'no'} "
          f"speech={'yes' if preview_cancelled else 'no'}", flush=True)


@on("clarification:list")
async def _clarification_list(srv, websocket, session, msg):
    runtime = srv.require_runtime()
    if "chat_id" in msg or "request_id" in msg:
        chat_id = str(msg.get("chat_id") or getattr(session, "viewed_session_id", "") or session_chat_id(session) or "")
        await websocket.send_json({
            "type": "clarification:snapshot", "chat_id": chat_id,
            "request_id": str(msg.get("request_id") or "")[:200],
            "pending": clarification.pending_interactions(runtime.work.interactions, chat_id),
        })
        return
    chat_id = session_chat_id(session)
    pending = clarification.pending_goal_input(
        runtime.work.interactions,
        chat_id,
    )
    projected = clarification.interaction_request(pending)
    if projected is None:
        await websocket.send_json({
            "type": "clarification:closed",
            "id": "",
            "kind": "goal_input",
            "chat_id": chat_id,
            "status": "empty",
        })
    else:
        await websocket.send_json(projected)


@on("clarification:response")
async def _clarification_response(srv, websocket, session, msg):
    runtime = srv.require_runtime()
    chat_id = str(msg.get("chat_id") or getattr(session, "viewed_session_id", "") or session_chat_id(session) or "")
    resolved = clarification.resolve_response(
        runtime.work.interactions,
        str(msg.get("id") or ""),
        msg.get("answers"),
        skipped=bool(msg.get("skipped")),
        goals=runtime.goals,
        chat_id=chat_id,
    )
    response = {"id": str(msg.get("id") or ""), "chat_id": chat_id,
                "request_id": str(msg.get("request_id") or "")[:200],
                "status": "resolved" if resolved else "stale"}
    if "chat_id" in msg or "request_id" in msg:
        await websocket.send_json({"type": "clarification:response:ack", **response})
    await websocket.send_json({
        "type": "clarification:closed",
        **response,
    })

ws_tools.register(on)
ws_browser.register(on)
ws_memory.register(on)
ws_config.register(on)
ws_surface.register(on)
ws_automations.register(on)
ws_inference_platform.register(on)
ws_local_models.register(on)
ws_service_settings.register(on)
ws_work.register(on)
ws_chat_sessions.register(on)
ws_kernel.register(on)
ws_execution.register(on)
ws_peers.register(on)
ws_goals.register(on)
ws_children.register(on)
ws_extensions_v2.register(on)
