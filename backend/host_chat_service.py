"""Concrete interactive-chat runtime service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import background_tasks
import chat_pipeline
import host_memory_ops
import host_orphan
import host_prompt
from transcript_service import TranscriptService


class NativeChatEventTransport:
    """Host event sink for chat turns admitted without a renderer socket."""

    def __init__(self, host: "AppHost", chat_id: str) -> None:
        self.host = host
        self.chat_id = str(chat_id)

    async def send_json(self, message: dict) -> None:
        payload = dict(message or {})
        payload.setdefault("session_id", self.chat_id)
        await self.host.hub.broadcast(payload)


def launch_reserved_chat_turn(
    host,
    run_task,
    transport,
    text: str,
    session,
    *,
    runtime_admission_id: str,
    client_id: str = "",
    source: str = "",
    resume: bool = False,
    images: list[dict] | None = None,
    attachment_text: str = "",
    ticket_id: str = "",
    task_name: str = "chat-turn",
) -> asyncio.Task:
    """Canonical post-reservation launch used by UI and host-owned ingress."""

    registry = host.require_runtime().session_runtimes
    chat_id = registry.admission_chat_id(runtime_admission_id)
    turn_seq = session.reserve_turn()
    session.active.runtime_admission_id = runtime_admission_id
    session.active.runtime_chat_id = chat_id
    session.active.turn_ticket_id = str(ticket_id or "")
    if ticket_id:
        session.active.turn_client_id = str(client_id or "")
        session.active.turn_source = str(source or "queue_continue")
    try:
        task = background_tasks.spawn(
            run_task(
                transport,
                text,
                session,
                resume=resume,
                turn_seq=turn_seq,
                client_id=client_id,
                source=source,
                images=images,
                attachment_text=attachment_text,
                runtime_admission_id=runtime_admission_id,
                ticket_id=str(ticket_id or ""),
            ),
            name=task_name,
        )
    except Exception:
        if ticket_id:
            registry.repository.transition_ticket(
                str(ticket_id), "parked",
                expected=("selected", "preparing"),
                error="queue_launch_failed",
            )
        registry.finish_run(runtime_admission_id, status="admission_failed")
        session.busy = False
        session.clear_active_turn()
        raise
    session.active.turn_task = task
    registry.bind_admission_task(runtime_admission_id, task)

    def clear(done: asyncio.Task) -> None:
        if session.active.turn_task is done:
            session.active.turn_task = None
        if not done.cancelled():
            registry.finish_run(runtime_admission_id, status="terminal")

    task.add_done_callback(clear)
    return task

if TYPE_CHECKING:
    from app_host import AppHost
    from host_model_service import ModelService


@dataclass
class ChatService:
    """Chat entry points and chat-owned projections exposed by ``HostRuntime``."""

    host: "AppHost"
    transcript: TranscriptService
    models: "ModelService"

    async def handle_chat(
        self,
        websocket,
        text: str,
        session,
        *,
        resume: bool = False,
        reserved: bool = False,
        client_id: str = "",
        source: str = "",
        images: list[dict] | None = None,
        attachment_text: str = "",
    ) -> None:
        await chat_pipeline.handle_chat(
            self.host.chat_ports(),
            websocket,
            text,
            session,
            resume=resume,
            reserved=reserved,
            client_id=client_id,
            source=source,
            images=images,
            attachment_text=attachment_text,
        )

    def build_task_turn_ports(self, websocket, session):
        return self.host.task_turn_ports(websocket, session)

    async def extract_and_store(
        self,
        user_text: str,
        reply: str,
        *,
        evidence: dict | None = None,
    ) -> list[str]:
        return await host_memory_ops.extract_and_store(
            self.host, user_text, reply, evidence=evidence)

    async def remember_explicit(
        self,
        text: str,
        *,
        session_id: str = "",
    ) -> bool:
        return await host_memory_ops.remember_explicit(
            self.host, text, session_id=session_id)

    async def compress_messages(
        self,
        messages: list,
        protect_first: int = 2,
        protect_last: int = 6,
        should_stop: Callable[[], bool] | None = None,
    ) -> list:
        return await self.transcript.compress_messages(
            messages,
            protect_first=protect_first,
            protect_last=protect_last,
            should_stop=should_stop,
        )

    def approx_tokens(self, messages: list) -> int:
        return self.transcript.approx_tokens(messages)

    def ctx_compress_threshold(self) -> int:
        return self.transcript.compress_threshold()

    async def count_prompt_tokens(
        self,
        messages: list,
        *,
        tools: list | None = None,
        image_b64=None,
    ) -> int | None:
        return await self.transcript.count_prompt_tokens(
            messages,
            tools=tools,
            image_b64=image_b64,
        )

    def prompt_context(self, memories: list, attachment_context: str = ""):
        return host_prompt.prompt_context(
            self.host, memories, attachment_context=attachment_context)

    def vision_state(self) -> tuple[bool, str]:
        return self.models.vision_state()

    def build_system_prompt(
        self,
        memories: list,
        attachment_context: str = "",
    ) -> str:
        return host_prompt.build_system_prompt(
            self.host, memories, attachment_context=attachment_context)

    def build_internal_system_prompt(
        self,
        memories: list,
        attachment_context: str = "",
    ) -> str:
        return host_prompt.build_internal_system_prompt(
            self.host, memories, attachment_context=attachment_context)

    def snapshot_resume_state(self, chat_id: str = ""):
        return host_orphan.snapshot_resume_state(self.host, chat_id)

    def snapshot_follow_up_state(self, chat_id: str = ""):
        return host_orphan.snapshot_follow_up_state(self.host, chat_id)

    async def accept_follow_up_evidence(self, chat_id: str, candidate):
        import asyncio
        from stopped_evidence import accept_stopped_evidence
        return await asyncio.to_thread(accept_stopped_evidence, self.host.require_runtime(), chat_id, candidate)

    def orphaned_task_payload(self, chat_id: str = '') -> dict | None:
        return host_orphan.orphaned_task_payload(self.host,chat_id)

    def sessions_message(self) -> dict:
        return host_orphan.chat_sessions_msg(self.host)

    def viewed_session_id(self, session) -> str:
        return host_orphan.viewed_chat_sid(self.host, session)

    async def run_task(
        self,
        websocket,
        text: str,
        session,
        *,
        resume: bool = False,
        turn_seq=None,
        client_id: str = "",
        source: str = "",
        images: list[dict] | None = None,
        attachment_text: str = "",
        runtime_admission_id: str = "",
        ticket_id: str = "",
    ) -> None:
        await chat_pipeline.chat_task(
            self.host.chat_ports(),
            websocket,
            text,
            session,
            resume=resume,
            turn_seq=turn_seq,
            client_id=client_id,
            source=source,
            images=images,
            attachment_text=attachment_text,
            runtime_admission_id=runtime_admission_id,
            ticket_id=ticket_id,
        )

    def launch_reserved_turn(
        self,
        transport,
        text: str,
        session,
        *,
        runtime_admission_id: str,
        client_id: str = "",
        source: str = "",
        resume: bool = False,
        images: list[dict] | None = None,
        attachment_text: str = "",
        ticket_id: str = "",
        task_name: str = "chat-turn",
    ) -> asyncio.Task:
        """Launch the one canonical chat task after durable writer admission."""
        return launch_reserved_chat_turn(
            self.host, self.run_task, transport, text, session,
            runtime_admission_id=runtime_admission_id,
            client_id=client_id, source=source, resume=resume,
            images=images, attachment_text=attachment_text,
            ticket_id=ticket_id, task_name=task_name,
        )

    async def start_next_queued_input(self, chat_id: str) -> dict:
        """Wake an idle durable chat from its oldest queued input ticket."""

        runtime = self.host.require_runtime()
        sessions = runtime.sessions
        target = sessions.get_session(chat_id)
        if target is None:
            return {"status": "unavailable", "reason": "unknown_chat"}
        if target.get("archived"):
            return {"status": "parked", "reason": "chat_archived"}
        registry = runtime.session_runtimes
        if registry.is_busy(chat_id):
            return {"status": "queued", "reason": "chat_busy"}
        admission_id = await registry.reserve_run(
            chat_id, attachment_id="",
        ) or ""
        if not admission_id:
            return {"status": "queued", "reason": "admission_busy"}
        claimed = registry.claim_input(
            chat_id, "follow_up", run_id=admission_id,
        )
        if claimed is None:
            claimed = registry.claim_input(
                chat_id, "steer", run_id=admission_id,
            )
        if claimed is None:
            registry.finish_run(admission_id, status="queue_empty")
            return {"status": "idle", "reason": "queue_empty"}
        from chat_session import ConnectionSession

        session = ConnectionSession(viewed_session_id=chat_id)
        transport = NativeChatEventTransport(self.host, chat_id)
        task = self.launch_reserved_turn(
            transport,
            str(claimed.get("text") or ""),
            session,
            runtime_admission_id=admission_id,
            client_id=str(claimed.get("client_id") or ""),
            source=str(claimed.get("source") or "peer"),
            ticket_id=str(claimed.get("id") or ""),
            task_name=f"peer-chat-turn:{chat_id[:48]}",
        )
        return {
            "status": "started",
            "chat_id": chat_id,
            "ticket_id": str(claimed.get("id") or ""),
            "admission_id": admission_id,
            # Internal chaining receipt. This is never sent over a provider or
            # renderer wire; the peer service awaits the canonical turn before
            # deciding whether another durable peer ticket can run.
            "_task": task,
        }
