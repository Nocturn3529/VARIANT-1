from unittest.mock import AsyncMock, patch
import asyncio
import time

import pytest

import server
from speech import local_stt as voice
from speech import local_tts as tts
from speech import xai as xai_speech


@pytest.mark.asyncio
async def test_cloud_stt_never_starts_local_whisper():
    cloud = AsyncMock(return_value="cloud transcript")
    local_start = AsyncMock()
    local_transcribe = AsyncMock(return_value="local transcript")
    with patch.object(server.APP.require_runtime().voice, "config", return_value={"stt_provider": "xai"}), \
            patch.object(xai_speech, "transcribe", cloud), \
            patch.object(server.APP.voice, "ensure_started", local_start), \
            patch.object(server.APP.voice, "transcribe", local_transcribe):
        result = await server.APP.require_runtime().voice.transcribe_audio(
            b"wav", language="en")

    assert result == "cloud transcript"
    cloud.assert_awaited_once_with(server.APP.router, b"wav", language="en")
    local_start.assert_not_awaited()
    local_transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_stt_uses_cloud_language_when_mic_omits_language():
    cloud = AsyncMock(return_value="cloud transcript")
    with patch.object(server.APP.require_runtime().voice, "config", return_value={
                "stt_provider": "xai", "stt": {"xai": {"language": "en"}}}), \
            patch.object(xai_speech, "transcribe", cloud):
        result = await server.APP.require_runtime().voice.transcribe_audio(
            b"wav", language=None)

    assert result == "cloud transcript"
    cloud.assert_awaited_once_with(server.APP.router, b"wav", language="en")


@pytest.mark.asyncio
async def test_cloud_stt_drops_auto_cloud_language_for_format_safety():
    cloud = AsyncMock(return_value="cloud transcript")
    with patch.object(server.APP.require_runtime().voice, "config", return_value={
                "stt_provider": "xai", "stt": {"xai": {"language": "auto"}}}), \
            patch.object(xai_speech, "transcribe", cloud):
        result = await server.APP.require_runtime().voice.transcribe_audio(
            b"wav", language=None)

    assert result == "cloud transcript"
    cloud.assert_awaited_once_with(server.APP.router, b"wav", language=None)


@pytest.mark.asyncio
async def test_local_stt_never_calls_cloud_adapter():
    cloud = AsyncMock(return_value="cloud transcript")
    local_start = AsyncMock()
    local_transcribe = AsyncMock(return_value="local transcript")
    with patch.object(server.APP.require_runtime().voice, "config", return_value={"stt_provider": "local"}), \
            patch.object(xai_speech, "transcribe", cloud), \
            patch.object(server.APP.voice, "ensure_started", local_start), \
            patch.object(server.APP.voice, "transcribe", local_transcribe):
        result = await server.APP.require_runtime().voice.transcribe_audio(
            b"wav", language="en")

    assert result == "local transcript"
    local_start.assert_awaited_once()
    local_transcribe.assert_awaited_once_with(b"wav", language="en")
    cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_route_is_an_exclusive_data_boundary():
    cloud = AsyncMock(return_value=b"cloud wav")
    local = AsyncMock(return_value=b"local wav")
    with patch.object(server.APP.require_runtime().voice, "config", return_value={
                "tts_provider": "xai", "tts": {"xai": {"voice": "eve", "language": "auto"}}}), \
            patch.object(xai_speech, "synthesize", cloud), \
            patch.object(tts, "synthesize", local):
        result = await server.APP.require_runtime().voice.synthesize(
            "hello", 1.0, voice="eve")
    assert result.data == b"cloud wav"
    assert result.mime_type == "audio/wav"
    cloud.assert_awaited_once()
    local.assert_not_awaited()

    cloud.reset_mock()
    local.reset_mock()
    with patch.object(server.APP.require_runtime().voice, "config", return_value={
                "tts_provider": "kokoro", "tts": {"kokoro": {"voice": "af_nova"}}}), \
            patch.object(xai_speech, "synthesize", cloud), \
            patch.object(tts, "synthesize", local):
        result = await server.APP.require_runtime().voice.synthesize(
            "hello", 1.0, voice="af_nova")
    assert result.data == b"local wav"
    assert result.mime_type == "audio/wav"
    local.assert_awaited_once()
    cloud.assert_not_awaited()


def test_local_kokoro_phonemizer_runtime_assets_are_usable():
    from kokoro_onnx.tokenizer import Tokenizer

    tokenizer = Tokenizer()
    phonemes = tokenizer.phonemize("VARIANT-1 local voice is ready.", "en-us")
    tokens = tokenizer.tokenize(phonemes)

    assert phonemes
    assert tokens


@pytest.mark.asyncio
async def test_local_kokoro_calls_are_serialized(monkeypatch):
    import numpy as np

    class Engine:
        def __init__(self):
            self.active = 0
            self.maximum = 0

        def get_voices(self):
            return ["af_nova"]

        def create(self, *_args, **_kwargs):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                time.sleep(0.03)
                return np.array([0.1, -0.1], dtype="float32"), 24000
            finally:
                self.active -= 1

    engine = Engine()
    monkeypatch.setattr(tts, "_engine", engine)
    monkeypatch.setattr(tts, "_loaded", True)
    monkeypatch.setattr(tts, "_engine_err", None)

    results = await asyncio.gather(
        tts.synthesize("first"),
        tts.synthesize("second"),
    )

    assert all(value.startswith(b"RIFF") for value in results)
    assert engine.maximum == 1


@pytest.mark.asyncio
async def test_tts_availability_does_not_wait_for_running_synthesis(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(tts, "_engine", object())
    def synthesis():
        with tts._engine_lock:
            entered.set()
            release.wait(2)
    worker = threading.Thread(target=synthesis, daemon=True)
    worker.start()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await asyncio.wait_for(asyncio.to_thread(tts.available), .25)
    finally:
        release.set()
        await asyncio.to_thread(worker.join, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [True, False])
async def test_tts_preview_keeps_produced_mime_when_route_changes(typed):
    import base64
    from types import SimpleNamespace
    from speech.providers import AudioResult
    from ws_config import _run_tts_preview
    entered, release = asyncio.Event(), asyncio.Event()
    mime = "audio/mpeg"
    async def synthesize(*args, **kwargs):
        entered.set()
        await release.wait()
        return AudioResult(b"mp3", "audio/mpeg") if typed else b"mp3"
    service = SimpleNamespace(synthesize=synthesize, mime_type=lambda: mime)
    socket = SimpleNamespace(send_json=AsyncMock())
    host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(voice=service))
    task = asyncio.create_task(_run_tts_preview(host, socket, SimpleNamespace(),
        request_id="preview-1", purpose="preview", session_id="", text="hello", voice_id=""))
    await entered.wait()
    mime = "audio/wav"
    release.set()
    await task
    result = socket.send_json.call_args.args[0]
    assert result["mime_type"] == "audio/mpeg"
    assert base64.b64decode(result["audio"]) == b"mp3"
