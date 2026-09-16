"""Out-of-process extension contribution adapter for MessagingGateway."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any, Mapping

from .base import GatewayAdapter, MessageEnvelope


class ExtensionMessagingAdapter(GatewayAdapter):
    """Poll and send through one immutable extension-worker dispatcher."""

    def __init__(self, gateway, runtime: Any, contribution: Mapping[str, Any]):
        self.runtime = runtime
        self.contribution = dict(contribution)
        self.descriptor = dict(contribution.get("descriptor") or {})
        self.package_id = str(contribution.get("package_id") or "")
        self.contribution_id = str(contribution.get("id") or "")
        self.descriptor_digest = str(contribution.get("descriptor_digest") or "")
        self.name = str(
            self.descriptor.get("adapter_name") or self.contribution_id
        ).strip()
        self.display_name = str(
            self.descriptor.get("display_name") or self.name
        ).strip()
        if not self.name or self.name in {"telegram", "discord"}:
            raise ValueError("extension messaging adapter name is invalid or reserved")
        self.poll_interval_s = max(
            0.1, min(float(self.descriptor.get("poll_interval_s") or 2.0), 60.0)
        )
        self.deadline_s = max(
            1.0, min(float(self.descriptor.get("deadline_s") or 55.0), 300.0)
        )
        super().__init__(gateway)

    async def _invoke(
        self,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> Any:
        receipt = await self.runtime.workers.invoke(
            self.package_id,
            self.contribution_id,
            {"operation": str(operation), **dict(arguments)},
            context={
                "surface": "messaging",
                "adapter": self.name,
                "package_digest": str(self.contribution.get("package_digest") or ""),
            },
            idempotency_key=idempotency_key,
            request_id="messaging-" + uuid.uuid4().hex,
            deadline_s=self.deadline_s,
            contribution_kind="messaging_adapters",
        )
        return receipt.get("result")

    async def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.last_error = ""
        self.task = asyncio.create_task(
            self._poll(), name=f"messaging-extension:{self.name}"
        )

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.connected = False

    async def _poll(self) -> None:
        try:
            while True:
                try:
                    cursor = self.gateway.ingress.adapter_cursor(self.name)
                    result = await self._invoke(
                        "poll",
                        {
                            "cursor": cursor,
                            "config": self.gateway.adapter_config(self.name),
                            "token": await self.gateway.token(self.name),
                            "credentials": await self.gateway.credentials(self.name),
                        },
                    )
                    value = dict(result or {}) if isinstance(result, Mapping) else {}
                    messages = value.get("messages") or []
                    if not isinstance(messages, list):
                        raise ValueError("extension adapter poll messages must be a list")
                    self.connected = True
                    self.last_error = ""
                    for raw in messages[:500]:
                        if not isinstance(raw, Mapping):
                            raise ValueError("extension adapter message must be an object")
                        envelope = MessageEnvelope(
                            adapter=self.name,
                            message_id=str(raw.get("message_id") or ""),
                            conversation_id=str(raw.get("conversation_id") or ""),
                            user_id=str(raw.get("user_id") or ""),
                            text=str(raw.get("text") or ""),
                            user_name=str(raw.get("user_name") or ""),
                            conversation_name=str(raw.get("conversation_name") or ""),
                            reply_to=str(raw.get("reply_to") or ""),
                            metadata=dict(raw.get("metadata") or {}),
                        )
                        if not (
                            envelope.message_id
                            and envelope.conversation_id
                            and envelope.user_id
                        ):
                            raise ValueError("extension adapter message identity is incomplete")
                        accepted = await asyncio.shield(
                            self.gateway.submit(envelope)
                        )
                        if accepted is False:
                            raise RuntimeError("extension adapter message was not accepted")
                    next_cursor = int(value.get("cursor") or cursor)
                    if next_cursor < cursor:
                        raise ValueError("extension adapter cursor moved backwards")
                    self.gateway.ingress.set_adapter_cursor(self.name, next_cursor)
                    await asyncio.sleep(self.poll_interval_s if not messages else 0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connected = False
                    self.last_error = str(exc)
                    await asyncio.sleep(max(1.0, self.poll_interval_s))
        finally:
            self.connected = False

    async def send_text(self, envelope: MessageEnvelope, text: str) -> None:
        digest = hashlib.sha256(
            (
                f"{self.name}\0{envelope.conversation_id}\0"
                f"{envelope.message_id}\0{str(text)}"
            ).encode("utf-8", errors="replace")
        ).hexdigest()
        await self._invoke(
            "send_text",
            {
                "conversation_id": envelope.conversation_id,
                "message_id": envelope.message_id,
                "reply_to": envelope.reply_to,
                "text": str(text),
                "config": self.gateway.adapter_config(self.name),
                "token": await self.gateway.token(self.name),
                "credentials": await self.gateway.credentials(self.name),
            },
            idempotency_key=f"messaging:{self.name}:send:{digest}",
        )

    async def send_typing(self, envelope: MessageEnvelope) -> None:
        await self._invoke(
            "send_typing",
            {
                "conversation_id": envelope.conversation_id,
                "message_id": envelope.message_id,
                "config": self.gateway.adapter_config(self.name),
                "token": await self.gateway.token(self.name),
                "credentials": await self.gateway.credentials(self.name),
            },
        )

    def status(self) -> dict:
        return {
            **super().status(),
            "extension": True,
            "package_id": self.package_id,
            "package_digest": str(self.contribution.get("package_digest") or ""),
            "descriptor_digest": self.descriptor_digest,
        }


__all__ = ["ExtensionMessagingAdapter"]
