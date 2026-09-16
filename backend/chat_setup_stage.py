"""Attachment, readiness, resume, and session setup stage for chat turns."""

from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
import logging
from typing import TYPE_CHECKING

from agent_engine.snapshot_utils import is_resume_request, is_terminal_commit_only_state
from chat_attachments import display_user_message
from chat_commands import is_direct_command
from chat_memory import parse_remember_command
from chat_stage_result import ChatStageContinue, ChatStageDone, ChatStageResult
from chat_stream import bind_turn_identity, infer_turn_source, stream_meta
from chat_turn_plan import (
    AttachmentPlan,
    ChatTurnPlan,
    ResumePlan,
    apply_chat_turn_plan,
    build_attachment_plan,
    build_resume_plan,
    check_engine_ready,
    resolve_session_id,
)
from model_runtime.context import projection_budget_tokens
from session_projection import project_session_conversation, projection_model_route

if TYPE_CHECKING:
    from chat_pipeline import ChatPorts


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedChatTurn:
    plan: ChatTurnPlan
    attachment_text: str = ""

    @property
    def text(self) -> str:
        return self.plan.attachments.model_text

    @property
    def is_resume(self) -> bool:
        return self.plan.resume.is_resume


async def prepare_chat_turn_stage(
    ports: "ChatPorts",
    websocket,
    text: str,
    session,
    *,
    resume: bool,
    reserved: bool,
    client_id: str,
    source: str,
    images: list[dict] | None,
    attachment_text: str,
) -> ChatStageResult[PreparedChatTurn]:
    """Validate and atomically bind all turn setup decisions."""
    bind_turn_identity(session, client_id=client_id, source=source)

    def _done(**fields) -> dict:
        return {"type": "done", **stream_meta(session), **fields}

    sessions = getattr(getattr(ports, "io", None), "sessions", None)
    session_id = ""
    if sessions is not None:
        # Admission predates model warmup and can outlive navigation in this
        # window. Keep the writer, context, and transcript on its original chat.
        session_id = str(
            getattr(session.active, "runtime_chat_id", "") or ""
        ) if getattr(session.active, "runtime_admission_id", "") else ""
        if not session_id:
            session_id = resolve_session_id(
                sessions,
                getattr(session, "viewed_session_id", None),
            )
        viewed_id = getattr(session, "viewed_session_id", None)
        if not viewed_id or not sessions.has_session(viewed_id or ""):
            session.viewed_session_id = session_id

    attachment = build_attachment_plan(
        text=text,
        attachment_text=attachment_text,
        images=images,
        display_user_message=display_user_message,
    )
    if isinstance(attachment, tuple):
        if resume:
            attachment = AttachmentPlan(
                composer_text="",
                model_text="",
                display_text="",
                display_attachments=(),
                user_images=(),
                attach_suffix="",
            )
        else:
            mood, message = attachment
            empty_attachment = AttachmentPlan(
                composer_text=str(text or ""),
                model_text="",
                display_text=str(text or "").strip(),
                display_attachments=(),
                user_images=(),
                attach_suffix=str(attachment_text or ""),
            )
            plan = ChatTurnPlan(
                client_id=client_id or "",
                source=infer_turn_source(client_id, source),
                session_id=session_id,
                attachments=empty_attachment,
                resume=ResumePlan(requested=bool(resume)),
            )
            apply_chat_turn_plan(session, plan)
            return ChatStageDone(_done(mood=mood, text=message))

    plan = ChatTurnPlan(
        client_id=client_id or "",
        source=infer_turn_source(client_id, source),
        session_id=session_id,
        attachments=attachment,
        resume=ResumePlan(requested=(
            bool(resume) or bool(is_resume_request(attachment.model_text))
        )),
    )
    # Bind display/session identity before any user-visible setup gate can
    # short-circuit, so its terminal exchange has a durable target.
    apply_chat_turn_plan(session, plan)
    session.active.turn_persisted = False

    direct_command = (
        parse_remember_command(attachment.model_text) is not None
        or is_direct_command(attachment.model_text)
    )
    engine = check_engine_ready(ports.io.router)
    if not engine.ok and not direct_command:
        return ChatStageDone(_done(mood=engine.mood, text=engine.text))

    try:
        from agent_engine.snapshot_utils import log_snapshot_event
    except ImportError:
        _LOG.debug("checkpoint event helper is unavailable", exc_info=True)
        log_snapshot_event = None
    def _scoped_resume_state():
        callback = ports.session.snapshot_resume_state
        try:
            parameters = tuple(inspect.signature(callback).parameters.values())
            accepts_chat_id = any(
                parameter.kind in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                }
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_chat_id = True
        return callback(session_id) if accepts_chat_id else callback()

    def _scoped_follow_up_state():
        callback = getattr(
            ports.session, "snapshot_follow_up_state", None
        )
        if not callable(callback):
            return _scoped_resume_state()
        try:
            parameters = tuple(inspect.signature(callback).parameters.values())
            accepts_chat_id = any(
                parameter.kind in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                }
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_chat_id = True
        return callback(session_id) if accepts_chat_id else callback()

    resume_plan = build_resume_plan(
        text=attachment.model_text,
        resume_flag=resume,
        session_busy=bool(session.busy),
        reserved=reserved,
        snapshot_resume_state=_scoped_resume_state,
        snapshot_follow_up_state=_scoped_follow_up_state,
        is_resume_request=is_resume_request,
        log_snapshot_event=log_snapshot_event,
    )
    plan = replace(plan, resume=resume_plan)
    if resume_plan.is_resume and not str(text or "").strip():
        resume_state = dict(resume_plan.resume_state or {})
        resume_task = dict(resume_state.get("task") or {})
        original_text = str(
            resume_task.get("goal") or resume_state.get("goal") or ""
        ).strip()
        plan = replace(
            plan,
            attachments=replace(
                plan.attachments,
                model_text=original_text,
                display_text=original_text or "Resumed interrupted task",
            ),
        )
    apply_chat_turn_plan(session, plan)
    if resume_plan.blocked:
        return ChatStageDone(_done(
            mood=resume_plan.blocked_mood or "neutral",
            text=resume_plan.blocked_text,
        ))

    runtimes = getattr(ports.io, "runtime_registry", None)
    if resume_plan.is_resume and runtimes is not None:
        resume_state = dict(resume_plan.resume_state or {})
        runtimes.promote_recovered_inputs(
            session_id,
            delivered=(resume_state.get("input") or {}).get("delivered") or (),
            include_undelivered=not is_terminal_commit_only_state(resume_state),
        )

    if resume_plan.evidence_candidate is not None:
        accept = getattr(ports.session, "accept_follow_up_evidence", None)
        if callable(accept):
            reference = accept(session_id, resume_plan.evidence_candidate)
            if inspect.isawaitable(reference):
                reference = await reference
            resume_plan = replace(resume_plan, evidence_ref=reference)
            plan = replace(plan, resume=resume_plan)

    await websocket.send_json({"type": "start", **stream_meta(session)})
    session.clear_interrupt()
    session.busy = True
    session.active.turn_persisted = False

    session.convo = await project_session_conversation(
        ports.io.sessions,
        session_id,
        mode=str(getattr(ports.io.router, "mode", "local") or "local"),
        context_limit_tokens=projection_budget_tokens(ports.io.router),
        compress_messages=ports.session.compress_messages,
        snapshot_store=getattr(runtimes, "snapshot_store", None),
        model_route=projection_model_route(ports.io.router, ports.io.sessions, session_id),
    )
    return ChatStageContinue(PreparedChatTurn(
        plan,
        attachment_text=str(attachment_text or ""),
    ))
