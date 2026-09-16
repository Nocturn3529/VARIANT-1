"""Immutable, apply-once setup plan for an interactive chat turn.

Mid-turn mutations create a replacement plan and re-apply it atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class AttachmentPlan:
    """Composer text / vision / transcript display for one turn."""

    composer_text: str
    model_text: str
    display_text: str
    display_attachments: tuple[dict, ...]
    # Transient typed observations. This immutable plan lives only for the
    # active connection turn and is never copied into durable run state.
    user_images: tuple[dict, ...]
    attach_suffix: str


@dataclass(frozen=True)
class ResumePlan:
    """Whether this turn restores a durable checkpoint."""

    requested: bool
    is_resume: bool = False
    resume_state: Any = None
    resume_source: str = ""
    # An ordinary follow-up after Stop starts a new task but inherits the
    # interrupted run's internal message graph as working context.
    carry_context: bool = False
    evidence_candidate: Any = None
    evidence_ref: dict | None = None
    # Early-exit done payload when resume cannot proceed.
    blocked_mood: str = ""
    blocked_text: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_text)


@dataclass(frozen=True)
class EngineGate:
    """Local/cloud readiness gate before start is sent."""

    ok: bool
    mood: str = "neutral"
    text: str = ""


@dataclass(frozen=True)
class ChatTurnPlan:
    """Immutable snapshot of decisions for one interactive chat turn.

    Identity + attachment + resume + capabilities are pure/setup-time.
    Tool catalog binding may produce a new plan via ``replace`` before re-apply.
    """

    client_id: str = ""
    source: str = ""
    session_id: str = ""
    attachments: AttachmentPlan = field(
        default_factory=lambda: AttachmentPlan(
            composer_text="",
            model_text="",
            display_text="",
            display_attachments=(),
            user_images=(),
            attach_suffix="",
        )
    )
    resume: ResumePlan = field(default_factory=lambda: ResumePlan(requested=False))
    # Filled after tool catalog binding (still an immutable plan instance).
    tool_catalog: Any = None

    @property
    def model_text(self) -> str:
        return self.attachments.model_text

    @property
    def display_text(self) -> str:
        return self.attachments.display_text

    @property
    def is_resume(self) -> bool:
        return self.resume.is_resume


# ── Pure builders ────────────────────────────────────────────────────────────


def build_attachment_plan(
    *,
    text: str,
    attachment_text: str = "",
    images: list[dict] | tuple[dict, ...] | None = None,
    display_user_message,
) -> AttachmentPlan | tuple[str, str]:
    """Return AttachmentPlan, or ``(mood, text)`` done payload for empty turns."""
    user_images = tuple(dict(item) for item in (images or ()) if isinstance(item, dict))
    attach_suffix = str(attachment_text or "")
    composer_text = str(text or "")
    display_text, display_atts = display_user_message(
        composer_text,
        attach_suffix=attach_suffix,
        has_image=bool(user_images),
    )
    model_text = (
        (composer_text.rstrip() + attach_suffix).strip()
        if attach_suffix
        else composer_text.strip()
    )
    if not model_text.strip():
        if user_images:
            model_text = "Please look at the attached image or images."
            if not display_text:
                display_text = "📷 Attached image"
        else:
            return (
                "neutral",
                "Send a message or attach an image/text file.",
            )
    atts = tuple(display_atts or ())
    return AttachmentPlan(
        composer_text=composer_text,
        model_text=model_text,
        display_text=display_text or model_text,
        display_attachments=atts,
        user_images=user_images,
        attach_suffix=attach_suffix,
    )


def check_engine_ready(router) -> EngineGate:
    """Pure readiness check against the live router (no side effects)."""
    has_cloud = router.cloud_route_ready()
    if router.mode == "cloud":
        if not has_cloud:
            return EngineGate(
                ok=False,
                mood="concerned",
                text="Cloud mode is on but no API key is set. Add one in the AI Brain panel.",
            )
        return EngineGate(ok=True)
    # local
    if not router.engine_ready:
        return EngineGate(
            ok=False,
            mood="neutral",
            text="My local model isn't loaded yet.",
        )
    return EngineGate(ok=True)


def build_resume_plan(
    *,
    text: str,
    resume_flag: bool,
    session_busy: bool,
    reserved: bool,
    snapshot_resume_state,
    is_resume_request,
    log_snapshot_event=None,
    snapshot_follow_up_state=None,
) -> ResumePlan:
    """Decide resume vs ordinary turn. May log miss events via optional hook."""
    resume_requested = bool(resume_flag) or bool(is_resume_request(text))
    if resume_requested and session_busy and not reserved:
        if log_snapshot_event:
            try:
                log_snapshot_event("resume_blocked", reason="already_running")
            except Exception:
                pass
        return ResumePlan(
            requested=True,
            blocked_mood="neutral",
            blocked_text="A task is already running. Stop it first, then resume.",
        )
    if not resume_requested:
        context_loader = (
            snapshot_follow_up_state
            if callable(snapshot_follow_up_state)
            else lambda: (None, "no evidence loader")
        )
        prior_state, _prior_error = context_loader()
        if prior_state:
            return ResumePlan(
                requested=False,
                is_resume=False,
                evidence_candidate=prior_state,
                resume_source="stopped_evidence",
                carry_context=True,
            )
        return ResumePlan(requested=False)
    resume_state, resume_err = snapshot_resume_state()
    if resume_state:
        return ResumePlan(
            requested=True,
            is_resume=True,
            resume_state=resume_state,
            resume_source="native_snapshot",
        )
    if log_snapshot_event:
        try:
            log_snapshot_event("resume_miss", reason=resume_err or "no snapshot")
        except Exception:
            pass
    resume_reason = str(resume_err or "").strip()
    msg = (
        "No interrupted task to resume."
        if not resume_reason or resume_reason.casefold() in {
            "no checkpoint", "not found", "no snapshot", "no interrupted task",
        }
        else f"Cannot resume: {resume_reason}"
    )
    return ResumePlan(
        requested=True,
        blocked_mood="neutral",
        blocked_text=msg,
    )


def resolve_session_id(sessions, viewed_session_id: str | None) -> str:
    """Pick the durable chat session id for this connection's write path."""
    sid = viewed_session_id
    if not (sid and sessions.has_session(sid)):
        sid = sessions.get_active()
    return sid or ""


def apply_chat_turn_plan(session, plan: ChatTurnPlan) -> None:
    """Atomic write of plan fields onto ``session.active`` (one place)."""
    active = getattr(session, "active", None)
    if active is None:
        return
    active.turn_client_id = plan.client_id or ""
    active.turn_source = plan.source or ""
    active.turn_session_id = plan.session_id or None
    active.turn_display_user_text = plan.attachments.display_text
    active.turn_display_attachments = list(plan.attachments.display_attachments)
    if plan.tool_catalog is not None:
        active.tool_catalog = plan.tool_catalog


def with_tool_catalog(
    plan: ChatTurnPlan,
    *,
    snapshot,
) -> ChatTurnPlan:
    return replace(
        plan,
        tool_catalog=snapshot,
    )
