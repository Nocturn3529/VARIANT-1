"""Transport-neutral messaging gateway contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import hashlib


@dataclass(frozen=True)
class MessageEnvelope:
    adapter: str
    message_id: str
    conversation_id: str
    user_id: str
    text: str
    user_name: str = ""
    conversation_name: str = ""
    reply_to: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def route_key(self) -> str:
        return f"{self.adapter}:{self.conversation_id}"

    @property
    def ticket_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.adapter}\0{self.conversation_id}\0{self.message_id}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()
        return "message_" + digest[:80]


class MessagingIngressOutcomeUnknown(RuntimeError):
    """Recovery cannot prove whether a previously routed turn took effect."""


class GatewayAdapter:
    """Small lifecycle/send interface implemented by messaging transports."""

    name = "adapter"
    display_name = "Adapter"

    def __init__(self, gateway):
        self.gateway = gateway
        self.task = None
        self.last_error = ""
        self.connected = False

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def send_text(self, envelope: MessageEnvelope, text: str) -> None:
        raise NotImplementedError

    async def send_typing(self, envelope: MessageEnvelope) -> None:
        return None

    def status(self) -> dict:
        return {
            "name": self.name, "display_name": self.display_name,
            "running": bool(self.task and not self.task.done()),
            "connected": bool(self.connected), "last_error": self.last_error,
        }
