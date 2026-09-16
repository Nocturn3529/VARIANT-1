"""Telegram Bot API long-polling adapter."""

from __future__ import annotations

import asyncio

import httpx

from ..base import GatewayAdapter, MessageEnvelope
from ..media import MAX_REMOTE_ATTACHMENTS, download_and_stage


class TelegramAdapter(GatewayAdapter):
    name = "telegram"
    display_name = "Telegram"

    def __init__(self, gateway):
        super().__init__(gateway)
        self.offset = gateway.ingress.adapter_cursor(self.name)
        self.client = None

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        token = await self.gateway.token(self.name)
        if not token:
            self.last_error = "Telegram bot token is not configured"
            return
        self.last_error = ""
        self.task = asyncio.create_task(self._poll(token), name="messaging-telegram")

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.client:
            await self.client.aclose()
            self.client = None
        self.connected = False

    async def _poll(self, token: str) -> None:
        base = f"https://api.telegram.org/bot{token}"
        timeout = max(5, min(50, int(self.gateway.adapter_config(self.name).get("poll_timeout") or 25)))
        self.client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(timeout + 10, connect=15))
        try:
            while True:
                try:
                    response = await self.client.get(
                        f"{base}/getUpdates", params={"offset": self.offset, "timeout": timeout,
                                                       "allowed_updates": '["message"]'})
                    response.raise_for_status()
                    self.connected = True
                    self.last_error = ""
                    updates = sorted(
                        response.json().get("result") or [],
                        key=lambda item: int(item.get("update_id") or 0),
                    )
                    for update in updates:
                        await self._accept_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connected = False
                    self.last_error = str(exc)
                    await asyncio.sleep(3)
        finally:
            self.connected = False

    async def _accept_update(self, update: dict) -> None:
        """Advance Telegram's acknowledgement offset only after routing wins."""

        update_id = int(update.get("update_id") or 0)
        next_offset = max(self.offset, update_id + 1)
        message = update.get("message") or {}
        text = str(message.get("text") or message.get("caption") or "").strip()
        attachments, notices = await self._message_attachments(message)
        if notices:
            text = (text + "\n\n" if text else "") + "\n".join(notices)
        if not text and not attachments:
            # This update contains no message payload VARIANT-1 supports. It is an
            # explicit rejection, so advancing the cursor is correct.
            self.offset = self.gateway.ingress.set_adapter_cursor(
                self.name, next_offset
            )
            return
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        envelope = MessageEnvelope(
            adapter=self.name,
            message_id=str(message.get("message_id") or update_id),
            conversation_id=str(chat.get("id") or ""),
            user_id=str(user.get("id") or ""),
            text=text,
            user_name=str(user.get("username") or user.get("first_name") or ""),
            conversation_name=str(chat.get("title") or chat.get("username") or ""),
            metadata={
                "chat_type": chat.get("type") or "",
                "attachment_count": len(attachments),
            },
            attachments=tuple(attachments),
        )
        # Adapter shutdown stops polling, but the gateway owns every already
        # submitted route and drains it before core teardown.
        delivered = await asyncio.shield(self.gateway.submit(envelope))
        if delivered is False:
            raise RuntimeError(
                f"Telegram update {update_id} was not accepted by the gateway"
            )
        self.offset = self.gateway.ingress.set_adapter_cursor(
            self.name, next_offset
        )

    async def _message_attachments(self, message: dict) -> tuple[list[dict], list[str]]:
        candidates: list[tuple[str, dict]] = []
        photos = message.get("photo") or []
        if isinstance(photos, list) and photos:
            largest = photos[-1] if isinstance(photos[-1], dict) else {}
            candidates.append(("photo", largest))
        for kind in ("document", "voice", "audio", "video", "animation"):
            value = message.get(kind)
            if isinstance(value, dict):
                candidates.append((kind, value))
        if not candidates:
            return [], []
        token = await self.gateway.token(self.name)
        if not token:
            raise RuntimeError("Telegram bot token is not configured")
        client = self.client or httpx.AsyncClient(trust_env=False, timeout=30)
        owned = client is not self.client
        attachments: list[dict] = []
        notices: list[str] = []
        try:
            for index, (kind, item) in enumerate(candidates[:MAX_REMOTE_ATTACHMENTS]):
                file_id = str(item.get("file_id") or "")
                default_names = {
                    "photo": f"photo-{index + 1}.jpg",
                    "voice": f"voice-{index + 1}.ogg",
                    "audio": f"audio-{index + 1}.mp3",
                    "video": f"video-{index + 1}.mp4",
                    "animation": f"animation-{index + 1}.mp4",
                }
                name = str(
                    item.get("file_name")
                    or default_names.get(kind)
                    or f"{kind}-{index + 1}"
                )
                media_type = str(
                    item.get("mime_type")
                    or ("image/jpeg" if kind == "photo" else "")
                )
                if not file_id:
                    notices.append(f"[Telegram {kind} attachment had no file identifier.]")
                    continue
                info = await client.get(
                    f"https://api.telegram.org/bot{token}/getFile",
                    params={"file_id": file_id},
                )
                try:
                    info.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    status = int(exc.response.status_code)
                    if 400 <= status < 500 and status != 429:
                        notices.append(
                            f"[Telegram attachment {name} was rejected by Telegram "
                            f"before download (HTTP {status}).]"
                        )
                        continue
                    raise
                payload = info.json()
                if isinstance(payload, dict) and payload.get("ok") is False:
                    notices.append(
                        f"[Telegram attachment {name} was rejected before download: "
                        f"{str(payload.get('description') or 'unknown Telegram error')}.]"
                    )
                    continue
                file_path = str(((payload.get("result") or {}).get("file_path") or ""))
                if not file_path:
                    raise RuntimeError("Telegram getFile did not return file_path")
                try:
                    attachments.append(await download_and_stage(
                        client,
                        f"https://api.telegram.org/file/bot{token}/{file_path}",
                        self.gateway.attachment_root,
                        adapter=self.name,
                        conversation_id=str((message.get("chat") or {}).get("id") or ""),
                        message_id=str(message.get("message_id") or ""),
                        name=name,
                        media_type=media_type,
                        source_id=file_id,
                    ))
                except httpx.HTTPStatusError as exc:
                    status = int(exc.response.status_code)
                    if 400 <= status < 500 and status != 429:
                        notices.append(
                            f"[Telegram attachment {name} was rejected by Telegram (HTTP {status}).]"
                        )
                        continue
                    raise
                except ValueError as exc:
                    notices.append(f"[Telegram attachment {name} was rejected: {exc}.]")
        finally:
            if owned:
                await client.aclose()
        return attachments, notices

    async def send_text(self, envelope: MessageEnvelope, text: str) -> None:
        token = await self.gateway.token(self.name)
        client = self.client or httpx.AsyncClient(trust_env=False, timeout=30)
        owned = client is not self.client
        try:
            # Telegram caps one message at 4096 characters.
            for start in range(0, len(text), 4000):
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": envelope.conversation_id, "text": text[start:start + 4000],
                          "reply_to_message_id": envelope.message_id})
                response.raise_for_status()
        finally:
            if owned:
                await client.aclose()

    async def send_typing(self, envelope: MessageEnvelope) -> None:
        token = await self.gateway.token(self.name)
        if not token:
            return
        client = self.client or httpx.AsyncClient(trust_env=False, timeout=10)
        owned = client is not self.client
        try:
            await client.post(f"https://api.telegram.org/bot{token}/sendChatAction",
                              json={"chat_id": envelope.conversation_id, "action": "typing"})
        finally:
            if owned:
                await client.aclose()
