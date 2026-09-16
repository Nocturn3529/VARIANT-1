"""Messaging-gateway chat bridge extracted from the server composition root."""

from __future__ import annotations

from chat_session import ConnectionSession
from messaging_gateway import MessagingIngressOutcomeUnknown


class GatewayChatSocket:
    """Minimal chat-pipeline sink that captures the user-facing final reply."""

    def __init__(self):
        self.reply = ""

    async def send_json(self, message: dict):
        if message.get("type") == "done" and message.get("text") is not None:
            self.reply = str(message.get("text") or "")


async def gateway_route(h, envelope, text: str) -> str:
    """Route one authorized remote message through the ordinary chat pipeline.

    Route bindings and transcript/kernel continuity live in the canonical
    services. The per-turn connection bag is not a second session registry.
    """
    route_key = envelope.route_key
    chat_sessions = h.require_runtime().sessions
    sid = h.gateway.session_id(route_key)
    if not sid or not chat_sessions.has_session(sid):
        title = (
            f"{envelope.adapter.title()}: "
            f"{envelope.conversation_name or envelope.user_name or envelope.conversation_id}"
        )
        sid = chat_sessions.create_session(
            title=title[:120], make_active=False
        )
        h.gateway.bind_session(route_key, sid)
    session = ConnectionSession(viewed_session_id=sid)
    ticket_id = str(envelope.ticket_id)
    if chat_sessions.has_message_ticket(sid, ticket_id):
        return chat_sessions.reply_for_message_ticket(sid, ticket_id)
    if bool((envelope.metadata or {}).get("_variant1_ingress_reconcile_only")):
        raise MessagingIngressOutcomeUnknown(
            "the prior route has no durable transcript proof; refusing to rerun effects"
        )
    from chat_attachments import parse_chat_attachments

    images, attachment_text = parse_chat_attachments(
        list(envelope.attachments or ()),
        staging_root=str(
            getattr(h, "data_dir", "") or getattr(h.gateway, "attachment_root", "")
        ),
    )
    sink = GatewayChatSocket()
    await h.require_runtime().chat.run_task(
        sink, text, session,
        client_id=(
            f"gateway:{envelope.adapter}:{envelope.conversation_id}:"
            f"{envelope.message_id}"
        ),
        source="messaging",
        ticket_id=ticket_id,
        images=images,
        attachment_text=attachment_text,
    )
    return sink.reply
