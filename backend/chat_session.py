"""Per-connection session and per-turn state for interactive chat.

``ConnectionSession`` owns WS identity and long-lived connection fields.
``ActiveTurn`` owns one in-flight chat turn (cleared on done/cancel).

There is no flat facade — call sites use ``session.active.*`` for turn fields
and connection fields on ``ConnectionSession`` itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional
import uuid


def turn_chat_id(session: Any) -> str:
    """Resolve the owning turn without consulting the app-wide selected chat."""
    active = getattr(session, "active", None)
    for value in (
        getattr(active, "turn_session_id", None),
        getattr(active, "runtime_chat_id", None),
        getattr(session, "viewed_session_id", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def detached_message_error(message: dict, pinned_chat_id: str) -> str:
    """Validate pinned-view commands, including legacy chat IDs named ``id``."""
    kind = str(message.get("type") or "")
    if kind in {"browser:host:register", "browser:host:result"}:
        return "detached_chat_browser_host_forbidden"
    if kind == "chat:session:new":
        return "detached_chat_is_pinned"
    targets = [message.get("session_id"), message.get("chat_id")]
    if kind.startswith("chat:session:") or kind in {
        "session:settings:get", "chat:project:set", "model:options",
        "reasoning:effort:set", "chat:runtime:get", "chat:runtime:action",
        "chat:runtime:mutation:set",
    } or (kind == "mode:set" and message.get("scope") == "session"):
        targets.append(message.get("id"))
    if any(str(value or "").strip() not in {"", pinned_chat_id} for value in targets):
        return "detached_chat_owner_mismatch"
    return ""


@dataclass
class ActiveTurn:
    """One in-flight chat turn. Cleared or replaced at turn boundaries.

    The immutable ``ChatTurnPlan`` remains local to setup. Mirrored ``turn_*``
    fields stay here only for graph/host readers and are written via
    ``apply_chat_turn_plan``.
    """

    task: Any = None  # agent_task.Task for the graph path
    turn_task: Any = None  # owning asyncio.Task
    turn_session_id: Optional[str] = None
    turn_client_id: str = ""
    turn_source: str = ""
    turn_ticket_id: str = ""
    tool_catalog: Any = None
    available_tool_specs: Any = None
    disclosed_tool_specs: Any = None
    turn_display_user_text: Optional[str] = None
    turn_display_attachments: Optional[list] = None
    turn_persisted: bool = False
    delivered_inputs: list[dict] = field(default_factory=list)
    provider_summaries: list[dict] = field(default_factory=list)
    runtime_admission_id: str = ""
    runtime_chat_id: str = ""
    # Exact assistant text produced by the graph. This is populated before the
    # transcript commit begins so disconnect/cancellation in that narrow
    # window can persist the answer instead of replacing it with a stop stub.
    terminal_reply: str = ""
    transcript_id: str = ""
    commit_transcript_terminal: Any = None
    post_turn_task: Any = None
    # The visible terminal reply can precede final persistence/memory cleanup by
    # a few milliseconds or seconds.  The WebSocket dispatcher uses this bit to
    # wait for the owning task to release the durable writer instead of
    # stranding an immediately submitted next turn as late steering.
    terminal_sent: bool = False

    def clear(self) -> None:
        """Reset all turn-scoped runtime mirrors after done/cancel."""
        self.task = None
        self.turn_task = None
        self.turn_session_id = None
        self.turn_client_id = ""
        self.turn_source = ""
        self.turn_ticket_id = ""
        self.tool_catalog = None
        self.available_tool_specs = None
        self.disclosed_tool_specs = None
        self.turn_display_user_text = None
        self.turn_display_attachments = None
        self.turn_persisted = False
        self.delivered_inputs = []
        self.provider_summaries = []
        self.runtime_admission_id = ""
        self.runtime_chat_id = ""
        self.terminal_reply = ""
        self.transcript_id = ""
        self.commit_transcript_terminal = None
        self.post_turn_task = None
        self.terminal_sent = False


@dataclass
class ConnectionSession:
    """Per-WebSocket connection identity and durable conversation window."""

    convo: list = field(default_factory=list)
    mood: str = "neutral"
    interrupt: bool = False
    busy: bool = False
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_seq: int = 0
    latest_turn_seq: int = 0
    # Correlates a cancellation finalized inside ``chat_task`` with the
    # WebSocket stop handler that requested it. This stays connection-scoped so
    # clearing ActiveTurn cannot make the handler finalize (or wipe) a newer
    # turn that began after the writer fence was released.
    last_cancelled_turn_seq: Optional[int] = None
    # THIS connection's viewed/write chat session (per-client sessions).
    viewed_session_id: Optional[str] = None
    # Renderer role is fixed at WebSocket admission. Detached chat windows are
    # durable observers/controllers for one pinned chat, not lifecycle owners.
    view_role: str = "main"
    # One socket attachment identity. It can move between durable chats without
    # changing the ownership of another window attached to the same chat.
    attachment_id: str = field(
        default_factory=lambda: "attach_" + uuid.uuid4().hex
    )
    # In-flight STT transcription task (cancel on stop).
    transcribe_task: Any = None
    transcribe_request_id: str = ""
    transcribe_session_id: str = ""
    transcribe_terminal_ids: dict[str, None] = field(default_factory=dict)
    # One request-scoped TTS preview per socket. Reply auto-speak is owned by
    # the completed chat turn and does not use this preview slot.
    tts_preview_task: Any = None
    tts_preview_request_id: str = ""
    tts_preview_session_id: str = ""
    tts_preview_terminal_ids: set[str] = field(default_factory=set)
    # Active turn bag (one at a time).
    active: ActiveTurn = field(default_factory=ActiveTurn)
    def request_interrupt(self) -> None:
        self.interrupt = True

    def clear_interrupt(self) -> None:
        self.interrupt = False

    def reserve_turn(self) -> int:
        self.turn_seq += 1
        self.latest_turn_seq = self.turn_seq
        self.busy = True
        return self.turn_seq

    def record_active_input(
        self,
        row: dict,
        assistant_text: Optional[str],
    ) -> None:
        """Record the genuine transcript boundary for durable persistence."""
        item = dict(row)
        if assistant_text is not None:
            item["assistant_text"] = str(assistant_text)
        self.active.delivered_inputs.append(item)

    def clear_active_turn(self) -> None:
        """Clear turn-scoped state after completion or cancel."""
        self.active.clear()
