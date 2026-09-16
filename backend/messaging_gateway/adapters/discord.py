"""Discord Gateway v10 adapter with REST replies."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from ..base import GatewayAdapter, MessageEnvelope
from ..media import MAX_REMOTE_ATTACHMENTS, download_and_stage


def gateway_url(value: str) -> str:
    """A resume endpoint must remain on Discord's encrypted gateway domain."""
    parsed = urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "wss" or not (host == "gateway.discord.gg" or host.endswith(".discord.gg"))
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Discord resume gateway must be a Discord wss endpoint")
    return f"wss://{host}"


class DiscordAdapter(GatewayAdapter):
    name = "discord"
    display_name = "Discord"
    DEFAULT_INTENTS = 512 | 4096 | 32768  # guild messages, DMs, message content

    def __init__(self, gateway):
        super().__init__(gateway)
        self.client = None
        self.bot_user_id = ""
        self.session_id = str(
            gateway.ingress.adapter_state(self.name, "session_id", "") or ""
        )
        saved_sequence = gateway.ingress.adapter_state(self.name, "sequence", None)
        self.sequence = int(saved_sequence) if saved_sequence is not None else None
        self._received_sequence = self.sequence
        self.resume_gateway_url = str(
            gateway.ingress.adapter_state(
                self.name, "resume_gateway_url", "wss://gateway.discord.gg"
            ) or "wss://gateway.discord.gg"
        )
        self.bot_user_id = str(
            gateway.ingress.adapter_state(self.name, "bot_user_id", "") or ""
        )

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        token = await self.gateway.token(self.name)
        if not token:
            self.last_error = "Discord bot token is not configured"
            return
        self.last_error = ""
        self.task = asyncio.create_task(self._run(token), name="messaging-discord")

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.client:
            await self.client.aclose()
            self.client = None
        self.connected = False

    async def _heartbeat(self, socket, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await socket.send(json.dumps({"op": 1, "d": self._received_sequence}))

    def _persist_resume_state(self) -> None:
        self.gateway.ingress.set_adapter_state(self.name, "session_id", self.session_id)
        self.gateway.ingress.set_adapter_state(self.name, "sequence", self.sequence)
        self.gateway.ingress.set_adapter_state(
            self.name, "resume_gateway_url", self.resume_gateway_url
        )
        self.gateway.ingress.set_adapter_state(
            self.name, "bot_user_id", self.bot_user_id
        )

    def _clear_resume_state(self) -> None:
        self.session_id = ""
        self.sequence = None
        self._received_sequence = None
        self.resume_gateway_url = "wss://gateway.discord.gg"
        self._persist_resume_state()

    async def _run(self, token: str) -> None:
        import websockets
        self.client = httpx.AsyncClient(trust_env=False, timeout=30)
        while True:
            try:
                self._received_sequence = self.sequence
                try:
                    endpoint = gateway_url(self.resume_gateway_url)
                except ValueError:
                    self._clear_resume_state()
                    endpoint = gateway_url(self.resume_gateway_url)
                async with websockets.connect(
                        f"{endpoint}/?v=10&encoding=json",
                        open_timeout=20, close_timeout=5) as socket:
                    hello = json.loads(await socket.recv())
                    interval = float((hello.get("d") or {}).get("heartbeat_interval") or 45000) / 1000
                    heartbeat = asyncio.create_task(
                        self._heartbeat(socket, interval))
                    cfg = self.gateway.adapter_config(self.name)
                    intents = int(cfg.get("intents") or self.DEFAULT_INTENTS)
                    if self.session_id and self.sequence is not None:
                        await socket.send(json.dumps({"op": 6, "d": {
                            "token": token,
                            "session_id": self.session_id,
                            "seq": self.sequence,
                        }}))
                    else:
                        await socket.send(json.dumps({"op": 2, "d": {
                            "token": token, "intents": intents,
                            "properties": {
                                "os": "windows", "browser": "variant1", "device": "variant1"
                            },
                        }}))
                    self.connected = True
                    self.last_error = ""
                    try:
                        async for raw in socket:
                            event = json.loads(raw)
                            if event.get("s") is not None:
                                self._received_sequence = int(event["s"])
                            if event.get("op") == 7:
                                break
                            if event.get("op") == 9:
                                if not bool(event.get("d")):
                                    self._clear_resume_state()
                                await asyncio.sleep(1)
                                break
                            if event.get("t") == "READY":
                                ready = event.get("d") or {}
                                endpoint = gateway_url(str(ready.get("resume_gateway_url") or self.resume_gateway_url))
                                self.bot_user_id = str((ready.get("user") or {}).get("id") or "")
                                self.session_id = str(ready.get("session_id") or "")
                                self.resume_gateway_url = endpoint
                                self._persist_resume_state()
                            if event.get("t") == "MESSAGE_CREATE":
                                await self._message(event.get("d") or {})
                            if event.get("s") is not None:
                                # Resume only after durable inbox admission (or
                                # explicit rejection), never after receipt alone.
                                accepted_sequence = int(event["s"])
                                self.gateway.ingress.set_adapter_state(
                                    self.name, "sequence", accepted_sequence,
                                )
                                self.sequence = accepted_sequence
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
                        self.connected = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                await asyncio.sleep(5)

    async def _message(self, message: dict) -> None:
        author = message.get("author") or {}
        if author.get("bot") or str(author.get("id") or "") == self.bot_user_id:
            return
        text = str(message.get("content") or "").strip()
        attachments, notices = await self._message_attachments(message)
        if notices:
            text = (text + "\n\n" if text else "") + "\n".join(notices)
        if not text and not attachments:
            return
        cfg = self.gateway.adapter_config(self.name)
        mentions = {str(item.get("id") or "") for item in (message.get("mentions") or [])}
        guild_id = str(message.get("guild_id") or "")
        referenced = message.get("referenced_message") or {}
        reply_author = str((referenced.get("author") or {}).get("id") or "")
        reply_to_bot = bool(self.bot_user_id and reply_author == self.bot_user_id)
        if (
            cfg.get("mention_only")
            and guild_id
            and self.bot_user_id not in mentions
            and not reply_to_bot
        ):
            return
        if self.bot_user_id:
            text = text.replace(f"<@{self.bot_user_id}>", "").replace(
                f"<@!{self.bot_user_id}>", "").strip()
        envelope = MessageEnvelope(
            adapter=self.name, message_id=str(message.get("id") or ""),
            conversation_id=str(message.get("channel_id") or ""),
            user_id=str(author.get("id") or ""), text=text,
            user_name=str(author.get("global_name") or author.get("username") or ""),
            reply_to=str((message.get("message_reference") or {}).get("message_id") or ""),
            metadata={"guild_id": guild_id, "attachment_count": len(attachments)},
            attachments=tuple(attachments),
        )
        if not await self.gateway.admit(envelope):
            raise RuntimeError("Discord message was not durably admitted")

    async def _message_attachments(self, message: dict) -> tuple[list[dict], list[str]]:
        rows = [
            item for item in (message.get("attachments") or [])
            if isinstance(item, dict)
        ][:MAX_REMOTE_ATTACHMENTS]
        if not rows:
            return [], []
        if self.client is None:
            raise RuntimeError("Discord HTTP client is unavailable")
        attachments: list[dict] = []
        notices: list[str] = []
        for index, item in enumerate(rows):
            name = str(item.get("filename") or f"attachment-{index + 1}")
            url = str(item.get("url") or item.get("proxy_url") or "")
            if not url:
                notices.append(f"[Discord attachment {name} had no download URL.]")
                continue
            try:
                attachments.append(await download_and_stage(
                    self.client,
                    url,
                    self.gateway.attachment_root,
                    adapter=self.name,
                    conversation_id=str(message.get("channel_id") or ""),
                    message_id=str(message.get("id") or ""),
                    name=name,
                    media_type=str(item.get("content_type") or ""),
                    source_id=str(item.get("id") or ""),
                ))
            except httpx.HTTPStatusError as exc:
                status = int(exc.response.status_code)
                if 400 <= status < 500 and status != 429:
                    notices.append(
                        f"[Discord attachment {name} was rejected by Discord (HTTP {status}).]"
                    )
                    continue
                raise
            except ValueError as exc:
                notices.append(f"[Discord attachment {name} was rejected: {exc}.]")
        return attachments, notices

    async def send_text(self, envelope: MessageEnvelope, text: str) -> None:
        token = await self.gateway.token(self.name)
        client = self.client or httpx.AsyncClient(trust_env=False, timeout=30)
        owned = client is not self.client
        try:
            # Discord caps ordinary bot messages at 2000 characters.
            for start in range(0, len(text), 1900):
                response = await client.post(
                    f"https://discord.com/api/v10/channels/{envelope.conversation_id}/messages",
                    headers={"Authorization": f"Bot {token}"},
                    json={"content": text[start:start + 1900],
                          "message_reference": {
                              "message_id": envelope.message_id,
                              "fail_if_not_exists": False,
                          }})
                response.raise_for_status()
        finally:
            if owned:
                await client.aclose()

    async def send_typing(self, envelope: MessageEnvelope) -> None:
        token = await self.gateway.token(self.name)
        client = self.client or httpx.AsyncClient(trust_env=False, timeout=10)
        owned = client is not self.client
        try:
            await client.post(
                f"https://discord.com/api/v10/channels/{envelope.conversation_id}/typing",
                headers={"Authorization": f"Bot {token}"})
        finally:
            if owned:
                await client.aclose()
