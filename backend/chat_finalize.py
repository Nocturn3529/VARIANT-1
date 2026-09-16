"""Chat turn finalizer: show, speak, persist, remember.

Extracted from chat_pipeline so the pipeline stays plan → routes → graph.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

import background_tasks
from speech import local_tts as tts
from observability.activity import clip as _clip
from observability.run_receipts import terminal_cause_class
from chat_attachments import strip_inlined_attachments
from llm_usage import observe_usage_category
from run_context import current_run_context

if TYPE_CHECKING:
    from chat_pipeline import ChatPorts

# Long reports are shown in full but spoken only up to here:
# reading a multi-thousand-char cited report aloud is minutes of audio, and the
# synthesis itself blocks the turn finalizer for that long too.
_SPOKEN_MAX_CHARS = 600
_LOG = logging.getLogger(__name__)


def _runtime_registry(owner: Any):
    injected = getattr(owner, "runtime_registry", None)
    if injected is not None:
        return injected
    require_runtime = getattr(owner, "require_runtime", None)
    if callable(require_runtime):
        return require_runtime().session_runtimes
    return None


def _chat_sessions(owner: Any):
    """Resolve the transcript store from an explicit I/O port or HostRuntime.

    Chat turns deliberately pass ``ChatIoPorts`` so this finalizer has no need
    to reopen the process host. Cancellation/disconnect paths pass AppHost and
    resolve the same store through its one installed runtime. There is no
    public ``AppHost.sessions`` compatibility path.
    """
    injected = getattr(owner, "sessions", None)
    if injected is not None:
        return injected
    require_runtime = getattr(owner, "require_runtime", None)
    if callable(require_runtime):
        return require_runtime().sessions
    raise TypeError(
        "chat finalization requires ChatIoPorts.sessions or HostRuntime"
    )


async def _publish_append(hub: Any, websocket: Any, message: dict) -> None:
    """Publish a durable append event without changing terminal semantics.

    The hub and individual socket are transport adapters and may raise
    implementation-specific exceptions. Once the transcript is durable, an
    observer failure is intentionally fail-open but always observable.
    """
    try:
        await hub.broadcast(message)
        return
    except Exception as exc:
        _LOG.warning(
            "chat append broadcast failed; trying the initiating socket",
            exc_info=True,
        )
    try:
        await websocket.send_json(message)
    except Exception:
        _LOG.debug(
            "chat append fallback could not reach the initiating socket",
            exc_info=True,
        )


async def _publish_transcript_failure(
    hub: Any,
    websocket: Any,
    *,
    session_id: str,
    delivered: list[dict],
    session: Any = None,
) -> None:
    from chat_stream import stream_meta

    await _publish_append(hub, websocket, {
        **stream_meta(session),
        "type": "chat:transcript_failed",
        "session_id": str(session_id or ""),
        "ticket_ids": [
            str(row.get("id") or "") for row in delivered
            if isinstance(row, dict) and str(row.get("id") or "")
        ],
        "error": "transcript_persistence_failed",
        "terminal_reply_visible": True,
        "retry_queued_input": False,
    })


async def publish_terminal_event(
    hub: Any,
    websocket: Any,
    message: dict,
    *,
    transports: list[Any] | None = None,
) -> None:
    """Broadcast a terminal event and cover transports outside the live hub."""
    if transports is not None:
        targets = list(transports)
        if not any(target is websocket for target in targets):
            targets.append(websocket)
        for target in targets:
            try:
                await target.send_json(message)
            except Exception:
                _LOG.debug("chat terminal targeted delivery failed", exc_info=True)
        return

    delivered_by_hub = False
    try:
        await hub.broadcast(message)
        delivered_by_hub = True
    except Exception as exc:
        _LOG.warning("chat terminal broadcast failed", exc_info=True)

    active = getattr(hub, "active", None)
    raw_socket = getattr(websocket, "_ws", websocket)
    socket_is_in_hub = (
        isinstance(active, (list, tuple, set, frozenset))
        and (websocket in active or raw_socket in active)
    )
    if delivered_by_hub and socket_is_in_hub:
        return
    try:
        await websocket.send_json(message)
    except Exception:
        _LOG.debug("chat terminal socket fallback failed", exc_info=True)


async def settle_undelivered_inputs(
    host: Any,
    websocket: Any,
    session: Any,
    chat_id: str,
    *,
    reason: str,
    runtime_registry: Any = None,
) -> tuple[int, list[str]]:
    """Park undelivered input tickets without discarding the user's queued work.

    This is also used by cancellation fallbacks that run before, or instead of,
    the ordinary ``chat_task`` finalizer. Returning the IDs keeps the cleanup
    observable without making a successful broadcast part of storage
    correctness.
    """
    settled = 0
    ticket_ids: list[str] = []
    if runtime_registry is not None and str(chat_id or "").strip():
        try:
            park_tickets = getattr(
                runtime_registry, "park_queued_input_tickets", None
            )
            if callable(park_tickets):
                durable_tickets = list(park_tickets(
                    chat_id,
                    reason=reason,
                ) or ())
                settled += len(durable_tickets)
                ticket_ids.extend(
                    str(getattr(ticket, "ticket_id", "") or "")
                    for ticket in durable_tickets
                )
        except Exception as exc:
            _LOG.exception("terminal input tickets could not be settled")

    ticket_ids = list(dict.fromkeys(
        ticket_id for ticket_id in ticket_ids if ticket_id
    ))
    if ticket_ids:
        event = runtime_registry.queue_snapshot(chat_id)
        await _publish_append(host.hub, websocket, event)
    return settled, ticket_ids


def _inputs_for_commit(session: Any) -> list[dict]:
    delivered = list(getattr(session.active, "delivered_inputs", None) or [])
    initial = str(getattr(session.active, "turn_ticket_id", "") or "")
    if initial and not any(row.get("id") == initial for row in delivered):
        delivered.insert(0, {"id": initial, "text": session.active.turn_display_user_text or "",
                             "delivery": "follow_up"})
    return delivered


def _peer_origin(source: Any, client_id: Any) -> dict[str, str] | None:
    raw_source = str(source or "")
    if not raw_source.startswith("peer:"):
        return None
    raw_client = str(client_id or "")
    return {
        "kind": "peer",
        "peer_id": raw_source.removeprefix("peer:")[:512],
        "message_id": raw_client.removeprefix("peer-message:")[:512],
    }


def _durable_turn_messages(
    session: Any,
    initial_user_text: str,
    final_assistant_text: str,
    *,
    mood: str,
    attachments: list | None = None,
    run_id: str = "",
) -> list[dict]:
    """Build the genuine ordered transcript for a run with active input."""
    initial: dict[str, Any] = {
        "role": "user",
        "text": str(initial_user_text or ""),
        "attachments": list(attachments or []),
    }
    transcript_id = str(
        getattr(session.active, "transcript_id", "") or ""
    ).strip()[:160]
    if transcript_id:
        initial["transcript_id"] = transcript_id
    ticket_id = str(getattr(session.active, "turn_ticket_id", "") or "").strip()
    if ticket_id:
        initial["ticket_id"] = ticket_id
    initial_origin = _peer_origin(
        getattr(session.active, "turn_source", ""),
        getattr(session.active, "turn_client_id", ""),
    )
    if initial_origin:
        initial["origin"] = initial_origin
    rows: list[dict] = [initial]
    for delivered in list(getattr(session.active, "delivered_inputs", None) or []):
        if not isinstance(delivered, dict):
            continue
        if "assistant_text" in delivered:
            assistant_text = str(delivered.get("assistant_text") or "")
            if assistant_text:
                rows.append({"role": "assistant", "text": assistant_text})
        queued_text = str(delivered.get("text") or "").strip()
        if queued_text:
            user_row = {"role": "user", "text": queued_text}
            ticket_id = str(delivered.get("id") or "").strip()
            if ticket_id:
                user_row["ticket_id"] = ticket_id
            delivery = str(delivered.get("delivery") or "").strip()
            if delivery in {"steer", "follow_up"}:
                user_row["delivery"] = delivery
            origin = _peer_origin(
                delivered.get("source"), delivered.get("client_id"),
            )
            if origin:
                user_row["origin"] = origin
            rows.append(user_row)
    rows.append({
        "role": "assistant",
        "text": str(final_assistant_text or ""),
        "mood": mood,
    })
    summaries = list(getattr(session.active, "provider_summaries", None) or [])
    if summaries:
        rows[-1]["provider_summaries"] = summaries
    owner_run = str(run_id or getattr(current_run_context(), "run_id", "") or "")
    if owner_run:
        for row in rows:
            if row["role"] == "assistant":
                row["run_id"] = owner_run
    return rows


def _display_transcript(sessions: Any, transcript: list[dict], chat_id: str) -> list[dict]:
    project = getattr(sessions, "project_messages_for_display", None)
    return project(transcript, chat_id=chat_id) if callable(project) else transcript


def spoken_lead(reply: str) -> str:
    """The part of `reply` worth voicing: the whole thing when short, otherwise
    the first paragraph(s) that fit, cut at a sentence boundary — never mid-word."""
    r = (reply or "").strip()
    if len(r) <= _SPOKEN_MAX_CHARS:
        return r
    head = r[:_SPOKEN_MAX_CHARS]
    for sep in ("\n\n", ". ", "! ", "? "):
        cut = head.rfind(sep)
        if cut > 120:
            return head[:cut + (0 if sep == "\n\n" else 1)].strip()
    return head.rsplit(" ", 1)[0].strip() + "…"


async def persist_unfinalized_turn(
    host: Any,
    websocket: Any,
    session: Any,
    reply: str,
    *,
    mood: str = "neutral",
    meta: dict | None = None,
    allow_assistant_only: bool = False,
) -> bool:
    """Persist a visible terminal exchange that bypassed the normal finalizer.

    Hard cancellation and uncaught turn errors can both send a terminal reply
    without reaching ``finish_chat_turn``. Persist that exact visible exchange
    so session rehydration cannot erase it.
    """
    if getattr(session.active, "turn_persisted", False):
        return True
    display_text = str(getattr(session.active, "turn_display_user_text", None) or "").strip()
    display_atts = getattr(session.active, "turn_display_attachments", None) or []
    if not display_text and not display_atts and not allow_assistant_only:
        return False
    sessions = _chat_sessions(host)
    from chat_session import turn_chat_id

    sid = turn_chat_id(session)
    if not sid:
        return False
    reply = (reply or "").strip() or "Task stopped."
    delivered = _inputs_for_commit(session)
    runtimes = _runtime_registry(host)
    if runtimes is not None:
        runtimes.begin_transcript_commit(delivered)
    try:
        if display_text or display_atts:
            transcript = _durable_turn_messages(
                session,
                display_text,
                reply,
                mood=mood,
                attachments=display_atts,
            )
        else:
            # Empty/invalid setup requests have no genuine user row, but their
            # visible handled response is still part of durable chat history.
            transcript = [{
                "role": "assistant",
                "text": reply,
                "mood": mood,
            }]
        store_meta = sessions.append_messages(sid, transcript)
        if store_meta is None:
            raise RuntimeError(
                f"chat transcript target is unavailable: {sid or '<none>'}"
            )
        store_meta.pop("_canonical_head", None)
    except Exception as exc:
        # The concrete chat store is injected and can fail with backend-specific
        # errors. The visible terminal reply remains valid, but is not claimed
        # durable when this boundary fails.
        _LOG.exception("terminal chat exchange could not be persisted")
        if runtimes is not None:
            runtimes.fail_transcript_commit(
                sid,
                delivered,
                error=f"{type(exc).__name__}: {exc}",
            )
        await _publish_transcript_failure(
            host.hub,
            websocket,
            session_id=sid,
            delivered=delivered,
            session=session,
        )
        return False
    session.active.turn_persisted = True
    if runtimes is not None:
        runtimes.complete_transcript_commit(sid, delivered)
    terminal_commit = getattr(
        session.active, "commit_transcript_terminal", None
    )
    if callable(terminal_commit):
        try:
            await terminal_commit()
        except Exception:
            _LOG.exception("fallback transcript could not terminalize native snapshot")
    if store_meta is None:
        return False
    append_msg = {
        "type": "chat:appended",
        "session_id": sid,
        "client_id": getattr(session.active, "turn_client_id", "") or "",
        "assistant": {"role": "assistant", "text": reply, "mood": mood},
        "messages": _display_transcript(sessions, transcript, sid),
        "meta": store_meta,
        **(meta or {}),
    }
    if display_text or display_atts:
        user_msg: dict = {"role": "user", "text": display_text}
        if display_atts:
            user_msg["attachments"] = list(display_atts)
        append_msg["user"] = user_msg
    await _publish_append(host.hub, websocket, append_msg)
    return True


async def _run_post_turn_effects(
    ports: "ChatPorts",
    session: Any,
    transcript: list[dict],
    reply: str,
    *,
    sid: str,
    interrupted: bool,
    completion_status: str,
    run: Any,
    extract_memory: bool,
    turn_seq: int,
) -> None:
    """Run optional continuity/speech work outside chat admission ownership."""

    if extract_memory:
        memory_user_text = "\n\n".join(
            str(row.get("text") or "")
            for row in transcript
            if row.get("role") == "user" and str(row.get("text") or "").strip()
        )
        try:
            memory_job = ports.memory.extract_and_store(
                memory_user_text,
                reply,
                evidence={
                    "session_id": sid or "",
                    "interrupted": bool(interrupted),
                    "completion_status": str(completion_status or ""),
                    "tools_used": list((run or {}).get("tools_used") or []),
                },
            )
            with observe_usage_category("memory"):
                await memory_job
        except Exception:
            _LOG.exception("post-turn memory extraction failed")

    if (
        reply
        and reply != "…"
        and ports.tts.tts_enabled()
        and (
            ports.tts.tts_available()
            if ports.tts.tts_available
            else tts.available()
        )
        and not session.interrupt
        and int(getattr(session, "latest_turn_seq", 0) or 0) == int(turn_seq)
    ):
        try:
            from speech.providers import audio_result
            synth = ports.tts.tts_synthesize or tts.synthesize
            fallback_mime = ports.tts.tts_mime_type() if ports.tts.tts_mime_type else "audio/wav"
            audio = audio_result(await synth(
                spoken_lead(reply),
                ports.tts.tts_speed(),
                voice=(ports.tts.tts_voice() if ports.tts.tts_voice else ""),
            ), fallback_mime=fallback_mime)
            if (
                not session.interrupt
                and int(getattr(session, "latest_turn_seq", 0) or 0)
                == int(turn_seq)
            ):
                await ports.io.hub.broadcast({
                    "type": "speak",
                    "audio": base64.b64encode(audio.data).decode("ascii"),
                    "mime_type": audio.mime_type,
                    "session_id": sid,
                })
        except Exception:
            _LOG.exception("optional chat speech synthesis or delivery failed")


async def finish_chat_turn(
    ports: "ChatPorts",
    websocket: Any,
    session: Any,
    text: Any,
    mood: Any,
    reply: Any,
    *,
    interrupted: bool = False,
    run: Any = None,
    completion_status: str = "ok",
    stop_reason: str = "",
    terminal_reason: str = "",
    length_recoveries: int = 0,
    transcript_id: str = "",
    extract_memory: bool = True,
    commit_transcript_terminal: Any = None,
) -> None:
    """Commit one terminal exchange, then release optional post-turn effects."""
    # Late import avoids circular import with chat_pipeline (re-exports this module).
    from chat_pipeline import stream_meta

    reply = str(reply or "…").strip() or "..."
    terminal_status = (
        "cancelled" if interrupted else str(completion_status or "ok").lower()
    )
    cause_class = terminal_cause_class(str(terminal_reason or ""))
    run_ctx = current_run_context()
    if run_ctx is not None:
        # The outer chat-task boundary owns undelivered input settlement.  Set
        # this directly so that contract does not depend on activity telemetry
        # succeeding or on a particular emitter implementation.
        run_ctx.metadata["_terminal_status"] = terminal_status
        run_ctx.metadata["_terminal_stop_reason"] = str(stop_reason or "")
        run_ctx.metadata["_terminal_reason"] = str(terminal_reason or "")
        run_ctx.metadata["_terminal_cause_class"] = cause_class
        run_ctx.metadata["_length_recoveries"] = max(
            0, int(length_recoveries or 0)
        )
    session.mood = mood
    session.active.terminal_reply = reply
    session.active.transcript_id = str(transcript_id or "").strip()
    session.active.commit_transcript_terminal = commit_transcript_terminal
    runtimes = _runtime_registry(ports.io)
    admission_id = str(
        getattr(session.active, "runtime_admission_id", "") or ""
    )
    if runtimes is not None and admission_id:
        # No steer/follow-up may enter after the graph has chosen its terminal
        # answer. A post-done message is admitted as a new turn once this
        # writer releases; it is never stranded in the completed loop.
        runtimes.begin_run_finalization(admission_id)
    if run:
        done_fields = {
            "run_id": run.get("id"),
            "source": run.get("source"),
            "status": ("cancelled" if interrupted else completion_status),
            "text": _clip(reply, 300),
            "stop_reason": str(stop_reason or ""),
            "terminal_reason": str(terminal_reason or ""),
            "length_recoveries": max(0, int(length_recoveries or 0)),
            "cause_class": cause_class,
        }
        if run.get("tools_used") is not None:
            done_fields["tools_used"] = run.get("tools_used")
        try:
            await ports.io.emit("task:done", **done_fields)
        except Exception as exc:
            # Activity telemetry must not prevent the actual chat result from
            # reaching the user and becoming durable.
            _LOG.exception("task completion activity event could not be emitted")
    # Durable transcript: short display line + attachment chips, never the
    # full inlined file dump that would explode the chat UI.
    display_text = getattr(session.active, "turn_display_user_text", None)
    display_atts = getattr(session.active, "turn_display_attachments", None)
    if display_text is None:
        display_text, recovered = strip_inlined_attachments(text)
        if display_atts is None:
            display_atts = recovered
    display_text = str(display_text or "").strip() or str(text or "").strip()
    if display_atts is None:
        display_atts = []
    transcript = _durable_turn_messages(
        session,
        display_text,
        reply,
        mood=mood,
        attachments=display_atts,
        run_id=str((run or {}).get("id") or getattr(run_ctx, "run_id", "") or ""),
    )
    delivered_inputs = _inputs_for_commit(session)
    last_user_text = str(text or "")
    if delivered_inputs:
        last_user_text = str(delivered_inputs[-1].get("text") or last_user_text)
    ports.session.set_last_user_text(last_user_text)
    from chat_session import turn_chat_id

    sid = turn_chat_id(session)
    transcript_durable = bool(getattr(session.active, "turn_persisted", False))
    transcript_failed = False
    # A terminal path may have already persisted via persist_unfinalized_turn.
    if getattr(session.active, "turn_persisted", False):
        meta = None
    else:
        if runtimes is not None:
            runtimes.begin_transcript_commit(delivered_inputs)
        try:
            if not sid:
                raise RuntimeError("chat transcript has no owning session")
            meta = ports.io.sessions.append_messages(sid, transcript)
            if meta is None:
                raise RuntimeError(
                    f"chat transcript target is unavailable: {sid or '<none>'}"
                )
            session.active.turn_persisted = True
            transcript_durable = True
            if runtimes is not None:
                runtimes.complete_transcript_commit(sid, delivered_inputs)
        except Exception as exc:
            # Persistence errors are terminally significant for durability but
            # cannot retract the reply already delivered to the user.
            _LOG.exception("completed chat exchange could not be persisted")
            if runtimes is not None:
                runtimes.fail_transcript_commit(
                    sid,
                    delivered_inputs,
                    error=f"{type(exc).__name__}: {exc}",
                )
            transcript_failed = True
            meta = None
    canonical_head = str((meta or {}).pop("_canonical_head", "") or "")
    if transcript_durable and callable(commit_transcript_terminal):
        try:
            reference = await commit_transcript_terminal()
            if isinstance(reference, dict) and canonical_head and terminal_status == "ok":
                from session_projection import promote_native_projection

                promote_native_projection(
                    ports.io.sessions, sid, reference, canonical_head=canonical_head,
                    router=ports.io.router, transcript_id=transcript_id,
                )
        except Exception:
            # The transcript remains authoritative. Keep the native head
            # resumable so startup can reconcile it instead of falsely
            # claiming a terminal snapshot that was never committed.
            _LOG.exception("terminal snapshot could not acknowledge transcript commit")

    done_msg = {
        "type": "done",
        "mood": mood,
        "text": reply,
        "run_id": str((run or {}).get("id") or getattr(run_ctx, "run_id", "") or ""),
        "status": terminal_status,
        "stop_reason": str(stop_reason or ""),
        "terminal_reason": str(terminal_reason or ""),
        "length_recoveries": max(0, int(length_recoveries or 0)),
        "cause_class": cause_class,
        "settled": False,
        "durable": transcript_durable,
        **stream_meta(session),
    }
    if interrupted:
        done_msg["cancelled"] = True
    session.active.terminal_sent = True
    await publish_terminal_event(
        ports.io.hub,
        websocket,
        done_msg,
        transports=(
            runtimes.attached_transports(sid)
            if runtimes is not None and sid
            else None
        ),
    )
    if transcript_failed:
        await _publish_transcript_failure(
            ports.io.hub,
            websocket,
            session_id=sid,
            delivered=delivered_inputs,
            session=session,
        )
    if meta is not None:
        assistant_msg = {"role": "assistant", "text": reply, "mood": mood}
        user_msg: dict = {"role": "user", "text": display_text}
        if display_atts:
            user_msg["attachments"] = list(display_atts)
        append_msg = {
            "type": "chat:appended",
            "session_id": sid,
            "client_id": getattr(session.active, "turn_client_id", "") or "",
            "user": user_msg,
            "assistant": assistant_msg,
            "messages": _display_transcript(ports.io.sessions, transcript, sid),
            "meta": meta,
            **stream_meta(session),
        }
        await _publish_append(ports.io.hub, websocket, append_msg)
    if transcript_durable:
        session.active.post_turn_task = background_tasks.spawn(
            _run_post_turn_effects(
                ports,
                session,
                transcript,
                reply,
                sid=str(sid or ""),
                interrupted=interrupted,
                completion_status=completion_status,
                run=run,
                extract_memory=extract_memory,
                turn_seq=int(getattr(session, "latest_turn_seq", 0) or 0),
            ),
            name=f"chat-post-turn:{str(sid or 'unknown')[:48]}",
        )
        if not admission_id:
            # Direct/unit callers have no chat writer to release. Preserve the
            # synchronous helper contract while production admissions remain
            # non-blocking after their durable terminal boundary.
            await session.active.post_turn_task
