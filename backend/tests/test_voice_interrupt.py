"""Voice interrupt / barge-in: TTS synthesis must be dropped (not spoken) when
the session was interrupted before or during synthesis, and STT transcription
must run cancellably so a stop/interrupt message (or a newer recording) can
cancel it without waiting for whisper.cpp to finish -- see server.py's
_finish_chat_turn (TTS) and _transcribe_task (STT).

Also covers the WebSocket-level STT cancellation contract through the real
/ws dispatch loop: explicit stop/cancel, newer mic input, and typed chat turns
must all supersede any in-flight transcription without leaking stale transcript
messages.
"""

from __future__ import annotations

import asyncio
import base64
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from host_runtime_testkit import patch_host_runtime

pytest.importorskip("httpx")  # TestClient needs httpx; skip cleanly if absent.

from fastapi.testclient import TestClient

import chat_pipeline
import server
from speech import local_stt as voice
from speech import local_tts as tts
import ws_dispatch
from chat_session import ConnectionSession


async def _transcribe_task(*args, **kwargs):
    return await server.APP.require_runtime().voice.transcribe_task(*args, **kwargs)


async def _finish_chat_turn(websocket, session, text, mood, reply, **kwargs):
    return await chat_pipeline.finish_chat_turn(
        server.APP.chat_ports(), websocket, session, text, mood, reply, **kwargs)


class FakeWebSocket:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_json(self, data):
        self.messages.append(data)


@pytest.fixture
def session():
    value = ConnectionSession()
    value.active.turn_session_id = server.APP.require_runtime().sessions.get_active()
    return value


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.fixture(autouse=True)
def stub_whisper_startup():
    """Interrupt tests exercise task/WS behavior, never a real model sidecar.

    Patching only ``transcribe`` still lets ``_transcribe_task`` call the real
    lazy startup first.  That can load the model during ``npm test`` and, when a
    development VARIANT-1 instance already owns 8081, create an untracked fallback-
    port whisper-server.  Keep the suite hermetic.

    Also pin STT/TTS to local: if the developer's live config selects cloud
    providers, these tests would otherwise hit
    the cloud adapters (and fail closed without keys) instead of the patched
    local VOICE/tts seams.
    """
    with (
        patch.object(server.APP.voice, "ensure_started", new=AsyncMock()),
        patch.object(
            server.APP.require_runtime().voice,
            "stt_provider",
            return_value="local",
        ),
        patch.object(
            server.APP.require_runtime().voice,
            "provider",
            return_value="kokoro",
        ),
    ):
        yield


def _audio_payload() -> str:
    return base64.b64encode(b"wav").decode("ascii")


def _drain_until(ws, mtype, limit=40, *, forbidden=()):
    for _ in range(limit):
        m = ws.receive_json()
        assert m.get("type") not in forbidden, f"unexpected {m.get('type')!r} message: {m!r}"
        if m.get("type") == mtype:
            return m
    raise AssertionError(f"did not receive a {mtype!r} message")


def _speak_broadcasts(bc) -> list:
    """speak messages captured by a patched HUB.broadcast AsyncMock."""
    return [c.args[0] for c in bc.await_args_list
            if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == "speak"]


# ---------------------------------------------------------------------------
# TTS interrupt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tts_skipped_entirely_when_already_interrupted(fake_ws, session):
    """If the session was interrupted before synthesis would even start (a
    newer message already arrived), never call tts.synthesize at all."""
    session.request_interrupt()
    synth = AsyncMock(return_value=b"audio")
    with (
        patch_host_runtime(server.APP, chat={"extract_and_store": AsyncMock()}),
        patch.object(server.APP, "tts_enabled", return_value=True),
        patch.object(tts, "available", return_value=True),
        patch.object(tts, "synthesize", new=synth),
        patch.object(server.APP.hub, "broadcast", new=AsyncMock()) as bc,
    ):
        await _finish_chat_turn(fake_ws, session, "hello", "neutral", "hi there")

    synth.assert_not_awaited()
    assert not _speak_broadcasts(bc)
    done = [m for m in fake_ws.messages if m.get("type") == "done"]
    assert done, "done must still be sent even when speech is skipped"


@pytest.mark.asyncio
async def test_tts_dropped_when_superseded_mid_synthesis(fake_ws, session):
    """The common real race: synthesis was already running when a newer
    message arrived (session.interrupt flips True during the await). The
    finished audio must be discarded, not sent."""
    sid = server.APP.require_runtime().sessions.get_active()

    async def slow_synthesize(text, speed, voice=""):
        # Completion is durable before optional synthesis begins.
        messages = server.APP.require_runtime().sessions.get_session(
            sid
        )["messages"]
        assert messages[-2]["text"] == "hello"
        assert messages[-1]["text"] == "hi there"
        session.request_interrupt()  # simulates a new message arriving mid-synthesis
        return b"stale-audio"

    with (
        patch_host_runtime(server.APP, chat={"extract_and_store": AsyncMock()}),
        patch.object(server.APP, "tts_enabled", return_value=True),
        patch.object(tts, "available", return_value=True),
        patch.object(tts, "synthesize", new=slow_synthesize),
        patch.object(server.APP.hub, "broadcast", new=AsyncMock()) as bc,
    ):
        await _finish_chat_turn(fake_ws, session, "hello", "neutral", "hi there")

    assert not _speak_broadcasts(bc),         "stale audio synthesized after being superseded must not be sent"


@pytest.mark.asyncio
async def test_tts_sent_normally_when_not_interrupted(fake_ws, session):
    """Sanity check: the happy path is unaffected -- speech still plays when
    nothing interrupted the turn."""
    with (
        patch_host_runtime(server.APP, chat={"extract_and_store": AsyncMock()}),
        patch.object(server.APP, "tts_enabled", return_value=True),
        patch.object(
            server.APP.require_runtime().voice,
            "available",
            return_value=True,
        ),
        patch.object(server.APP, "tts_speed", return_value=1.0),
        patch.object(
            server.APP.require_runtime().voice,
            "voice",
            return_value="",
        ),
        patch.object(
            server.APP.require_runtime().voice,
            "synthesize",
            new=AsyncMock(return_value=b"audio"),
        ),
        patch.object(tts, "available", return_value=True),
        patch.object(tts, "synthesize", new=AsyncMock(return_value=b"audio")),
        patch.object(server.APP.hub, "broadcast", new=AsyncMock()) as bc,
    ):
        await _finish_chat_turn(fake_ws, session, "hello", "neutral", "hi there")

    # Speech is BROADCAST (the avatar overlay voices Deck-initiated turns too);
    # the initiating socket no longer gets a direct copy.
    assert len(_speak_broadcasts(bc)) == 1
    assert not [m for m in fake_ws.messages if m.get("type") == "speak"]


# ---------------------------------------------------------------------------
# STT interrupt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcription_cancel_matches_chat_and_request_before_start(fake_ws, session):
    from speech.service import SpeechService
    voice = SpeechService(SimpleNamespace())
    task = asyncio.create_task(asyncio.Event().wait())
    session.transcribe_task = task
    session.transcribe_request_id = "speech-one"
    session.transcribe_session_id = "chat-A"
    try:
        assert not await voice.cancel_transcription(fake_ws, session, request_id="speech-one", session_id="chat-B")
        assert not await voice.cancel_transcription(fake_ws, session, request_id="speech-other", session_id="chat-A")
        assert not task.cancelling() and not fake_ws.messages
        assert await voice.cancel_transcription(fake_ws, session, request_id="speech-one", session_id="chat-A")
        assert not await voice.cancel_transcription(fake_ws, session, request_id="speech-one", session_id="chat-A")
        assert fake_ws.messages == [{"type":"transcript", "request_id":"speech-one", "session_id":"chat-A", "text":"", "cancelled":True}]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_transcription_terminal_retains_the_newest_ids_in_order(fake_ws, session):
    from speech.service import SpeechService
    voice = SpeechService(SimpleNamespace())
    for index in range(40):
        await voice._send_transcript_terminal(fake_ws, session, request_id=f"speech-{index}", session_id="chat-A", text="done")
    assert set(session.transcribe_terminal_ids) == {f"speech-{index}" for index in range(8, 40)}
    await voice._send_transcript_terminal(fake_ws, session, request_id="speech-39", session_id="chat-A", text="", cancelled=True)
    assert len(fake_ws.messages) == 40


@pytest.mark.asyncio
async def test_transcribe_task_cancellation_sends_terminal_transcript(fake_ws, session):
    """Cancellation must release the renderer's transcribing phase."""
    started = asyncio.Event()

    async def slow_transcribe(wav, language=None):
        started.set()
        await asyncio.sleep(10)
        return "should never get here"

    with patch.object(server.APP.voice, "transcribe", new=slow_transcribe):
        task = asyncio.create_task(_transcribe_task(fake_ws, session, b"wav", None))
        session.transcribe_task = task
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert fake_ws.messages == [{
        "type": "transcript", "text": "", "cancelled": True,
    }]
    assert session.transcribe_task is None, "task must clear itself off the session when done"


@pytest.mark.asyncio
async def test_transcribe_task_delivers_transcript_when_not_cancelled(fake_ws, session):
    with patch.object(server.APP.voice, "transcribe", new=AsyncMock(return_value="hello there")):
        task = asyncio.create_task(_transcribe_task(fake_ws, session, b"wav", None))
        session.transcribe_task = task
        await task

    transcripts = [m for m in fake_ws.messages if m.get("type") == "transcript"]
    assert transcripts == [{"type": "transcript", "text": "hello there"}]
    assert session.transcribe_task is None


@pytest.mark.asyncio
async def test_transcribe_task_voice_unavailable_reports_error_not_crash(fake_ws, session):
    async def unavailable(wav, language=None):
        raise voice.VoiceUnavailable("no whisper binary")

    with patch.object(server.APP.voice, "transcribe", new=unavailable):
        task = asyncio.create_task(_transcribe_task(fake_ws, session, b"wav", None))
        session.transcribe_task = task
        await task

    transcripts = [m for m in fake_ws.messages if m.get("type") == "transcript"]
    assert len(transcripts) == 1
    assert transcripts[0]["error"] == "no whisper binary"
    assert session.transcribe_task is None


@pytest.mark.asyncio
async def test_ws_cancel_hard_cancels_active_chat_and_completes_ui(fake_ws, session):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_provider_call():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    turn = asyncio.create_task(blocked_provider_call())
    session.active.turn_task = turn
    session.busy = True
    await started.wait()

    await ws_dispatch.HANDLERS["cancel"](server, fake_ws, session, {"type": "cancel"})

    assert cancelled.is_set()
    assert turn.cancelled()
    assert fake_ws.messages[0]["type"] == "cancelling"
    assert fake_ws.messages[0]["accepted"] is True
    assert fake_ws.messages[0]["cleared_inputs"] == 0
    assert fake_ws.messages[-1]["type"] == "done"
    assert fake_ws.messages[-1]["cancelled"] is True


# ---------------------------------------------------------------------------
# WS-level STT interrupt
# ---------------------------------------------------------------------------

def test_ws_cancel_drops_in_flight_transcription():
    started = threading.Event()
    cancelled = threading.Event()

    async def slow_transcribe(wav, language=None):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "stale transcript"

    client = TestClient(server.app)
    with patch.object(server.APP.voice, "transcribe", new=slow_transcribe):
        with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
            ws.send_json({"type": "chat:sessions"})
            _drain_until(ws, "chat:sessions")
            ws.send_json({"type": "voice:transcribe", "audio": _audio_payload()})
            assert started.wait(2), "transcription task did not start"

            ws.send_json({"type": "cancel"})
            terminal = _drain_until(ws, "transcript")
            assert terminal["cancelled"] is True
            _drain_until(ws, "cancelling")
            assert cancelled.wait(2), "transcription task was not cancelled"

            ws.send_json({"type": "ping"})
            _drain_until(ws, "pong", forbidden=("transcript",))


def test_ws_new_transcription_supersedes_in_flight_transcription():
    first_started = threading.Event()
    first_cancelled = threading.Event()

    async def transcribe(wav, language=None):
        if wav == b"first":
            first_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
            return "first transcript"
        return "second transcript"

    client = TestClient(server.app)
    with patch.object(server.APP.voice, "transcribe", new=transcribe):
        with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
            ws.send_json({"type": "chat:sessions"})
            _drain_until(ws, "chat:sessions")
            first_audio = base64.b64encode(b"first").decode("ascii")
            second_audio = base64.b64encode(b"second").decode("ascii")

            ws.send_json({"type": "voice:transcribe", "audio": first_audio})
            assert first_started.wait(2), "first transcription task did not start"
            ws.send_json({"type": "voice:transcribe", "audio": second_audio})

            superseded = _drain_until(ws, "transcript")
            assert superseded["cancelled"] is True
            transcript = _drain_until(ws, "transcript")
            assert transcript["type"] == "transcript"
            assert transcript["text"] == "second transcript"
            assert transcript.get("cancelled") is not True
            assert first_cancelled.wait(2), "first transcription task was not cancelled"

            ws.send_json({"type": "ping"})
            _drain_until(ws, "pong", forbidden=("transcript",))


def test_ws_chat_drops_in_flight_transcription():
    started = threading.Event()
    cancelled = threading.Event()

    async def slow_transcribe(wav, language=None):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "stale transcript"

    async def chat_done(websocket, text, session, **kwargs):
        await websocket.send_json({"type": "done", "mood": "neutral", "text": "chat stub"})

    client = TestClient(server.app)
    with (
        patch_host_runtime(server.APP, chat={"run_task": chat_done}),
        patch.object(server.APP.voice, "transcribe", new=slow_transcribe),
    ):
        with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
            ws.send_json({"type": "chat:sessions"})
            _drain_until(ws, "chat:sessions")
            ws.send_json({"type": "voice:transcribe", "audio": _audio_payload()})
            assert started.wait(2), "transcription task did not start"

            ws.send_json({"type": "chat", "text": "typed instead"})
            assert cancelled.wait(2), "transcription task was not cancelled"
            terminal = _drain_until(ws, "transcript")
            assert terminal["cancelled"] is True
            _drain_until(ws, "done")

            ws.send_json({"type": "ping"})
            _drain_until(ws, "pong", forbidden=("transcript",))


def test_tts_preview_does_not_block_socket_and_can_be_cancelled():
    started = threading.Event()

    async def slow_synthesis(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(10)
        return b"late"

    client = TestClient(server.app)
    with patch.object(
        server.APP.require_runtime().voice,
        "synthesize",
        new=slow_synthesis,
    ):
        with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
            ws.send_json({
                "type": "tts:preview",
                "purpose": "preview",
                "request_id": "preview-slow",
                "text": "hello",
            })
            assert started.wait(2), "preview synthesis did not start"
            ws.send_json({"type": "ping"})
            _drain_until(ws, "pong", forbidden=("tts:preview",))
            ws.send_json({
                "type": "tts:preview:cancel",
                "request_id": "preview-slow",
            })
            cancelled = _drain_until(ws, "tts:preview")
            assert cancelled["request_id"] == "preview-slow"
            assert cancelled["cancelled"] is True


def test_stt_request_for_another_chat_is_rejected_without_inference():
    client = TestClient(server.app)
    transcribe = AsyncMock(return_value="must not run")
    with patch.object(server.APP.voice, "transcribe", new=transcribe):
        with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}") as ws:
            ws.send_json({
                "type": "voice:transcribe",
                "request_id": "wrong-chat-stt",
                "session_id": "chat:another",
                "audio": _audio_payload(),
            })
            terminal = _drain_until(ws, "transcript")
            assert terminal["request_id"] == "wrong-chat-stt"
            assert terminal["session_id"] == "chat:another"
            assert "chat changed" in terminal["error"]
    transcribe.assert_not_awaited()
