"""Route-aware speech service for transcription and synthesis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from copy import deepcopy
from typing import TYPE_CHECKING

import host_voice
from speech import local_stt
from speech import providers

if TYPE_CHECKING:
    from app_host import AppHost


@dataclass
class SpeechService:
    """Own the live STT/TTS policy used by WebSocket and chat surfaces."""

    host: "AppHost"

    def config(self) -> dict:
        return host_voice.voice_cfg(self.host.router)

    def stt_route(self) -> str:
        return host_voice.stt_route(self.host.router)

    def stt_provider(self) -> str:
        return str(self.config().get("stt_provider") or "local").strip().lower()

    def route(self) -> str:
        return host_voice.tts_route(self.host.router)

    def provider(self) -> str:
        return str(self.config().get("tts_provider") or "kokoro").strip().lower()

    def voice(self) -> str:
        return host_voice.tts_voice(
            self.host.router,
            default_cloud="eve",
            default_local="af_nova",
        )

    def available(self) -> bool:
        snapshot = providers.catalog(
            self.host.router, self.config(),
            local_stt_available=self.host.voice.installed(),
        )
        row = next((item for item in snapshot["tts_providers"]
                    if item["id"] == self.provider()), None)
        return bool(row and row.get("available"))

    def mime_type(self) -> str:
        row = next((item for item in providers.TTS_PROVIDERS
                    if item["id"] == self.provider()), None)
        return str((row or {}).get("mime_type") or "audio/wav")

    def catalog(self) -> dict:
        return providers.catalog(
            self.host.router, self.config(),
            local_stt_available=self.host.voice.installed(),
        )

    async def list_voices(self) -> list[dict]:
        return await providers.list_voices(
            self.provider(), self.host.router, self.config())

    async def synthesize(
        self,
        text: str,
        speed: float | None = None,
        voice: str = "",
    ) -> providers.AudioResult:
        speed = self.host.tts_speed() if speed is None else speed
        config = deepcopy(self.config())
        provider = str(config.get("tts_provider") or "kokoro").strip().lower()
        return await providers.synthesize(
            provider, self.host.router, config, text,
            voice=voice or self.voice(), speed=speed,
        )

    async def transcribe_audio(
        self,
        wav: bytes,
        language: str | None = None,
    ) -> str:
        if self.stt_provider() == "local":
            await self.host.voice.ensure_started()
            return await self.host.voice.transcribe(wav, language=language)
        root = self.config().get("stt")
        root = root if isinstance(root, dict) else {}
        options = root.get(self.stt_provider())
        options = options if isinstance(options, dict) else {}
        lang = str(language or options.get("language") or "").strip() or None
        if lang and lang.lower() in {"auto", "detect", "none"}:
            lang = None
        return await providers.transcribe(
            self.stt_provider(), self.host.router, self.config(), wav,
            language=lang,
        )

    def set_config(self, key, value) -> None:
        host_voice.set_tts(
            self.host.router,
            key,
            value,
            save=self.host.router.save_config,
        )

    async def transcribe_task(
        self,
        websocket,
        session,
        wav: bytes,
        language: str | None,
        *,
        request_id: str = "",
        session_id: str = "",
    ) -> None:
        """Run cancellable STT without coupling it to chat-turn interruption."""
        try:
            text = await self.transcribe_audio(wav, language=language)
            await self.host.hub.broadcast(self.host.engine_status_message())
        except asyncio.CancelledError:
            await self._send_transcript_terminal(
                websocket,
                session,
                request_id=request_id,
                session_id=session_id,
                text="",
                cancelled=True,
            )
            return
        except (local_stt.VoiceUnavailable, providers.SpeechProviderError) as exc:
            await self._send_transcript_terminal(
                websocket, session, request_id=request_id,
                session_id=session_id, text="", error=str(exc),
            )
            return
        except Exception as exc:
            await self._send_transcript_terminal(
                websocket, session, request_id=request_id,
                session_id=session_id, text="",
                error=f"transcription failed: {exc}",
            )
            return
        finally:
            if session.transcribe_task is asyncio.current_task():
                session.transcribe_task = None
                session.transcribe_request_id = ""
                session.transcribe_session_id = ""
        await self._send_transcript_terminal(
            websocket, session, request_id=request_id,
            session_id=session_id, text=text,
        )

    async def _send_transcript_terminal(
        self,
        websocket,
        session,
        *,
        request_id: str,
        session_id: str,
        text: str,
        error: str = "",
        cancelled: bool = False,
    ) -> None:
        identity = str(request_id or "").strip()
        delivered = getattr(session, "transcribe_terminal_ids", None)
        if not isinstance(delivered, dict):
            delivered = dict.fromkeys(delivered or ())
            session.transcribe_terminal_ids = delivered
        if identity and identity in delivered:
            return
        if identity:
            delivered[identity] = None
            if len(delivered) > 32:
                # Keep the most recent terminal identity through a racing
                # cancellation. set.pop() could discard the just-added ID.
                delivered.pop(next(iter(delivered)))
        payload = {
            "type": "transcript",
            "text": str(text or ""),
        }
        if identity:
            payload["request_id"] = identity
        if session_id:
            payload["session_id"] = str(session_id)
        if error:
            payload["error"] = str(error)
        if cancelled:
            payload["cancelled"] = True
        try:
            await websocket.send_json(payload)
        except Exception:
            pass

    async def cancel_transcription(
        self,
        websocket,
        session,
        *,
        request_id: str = "",
        session_id: str = "",
    ) -> bool:
        task = getattr(session, "transcribe_task", None)
        active_id = str(getattr(session, "transcribe_request_id", "") or "")
        if request_id and str(request_id) != active_id:
            return False
        origin = str(getattr(session, "transcribe_session_id", "") or "")
        if session_id and origin and str(session_id) != origin:
            return False
        cancelled = bool(task is not None and not task.done() and task.cancel())
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        if active_id:
            await self._send_transcript_terminal(
                websocket, session, request_id=active_id,
                session_id=origin, text="", cancelled=True,
            )
        if getattr(session, "transcribe_task", None) is task:
            session.transcribe_task = None
            session.transcribe_request_id = ""
            session.transcribe_session_id = ""
        return cancelled
