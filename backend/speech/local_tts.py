"""Optional in-process Kokoro support for source development only.

Packaged VARIANT-1 does not include this engine or its dependencies. Its canonical
speech provider connects to a separately installed Kokoro HTTP service instead.
Source users can install requirements-speech-optional.txt and supply the matching
ONNX/voices pair. Speech work never changes the model-facing Python tool loop.
"""

import asyncio
import struct
import threading
import sys

from speech.assets import kokoro_drop_dir, resolve_kokoro_assets

DEFAULT_VOICE_ID = "af_nova"   # spec 6.3's original fixed voice, now the default
SAMPLE_RATE = 24000

_engine = None        # kokoro_onnx.Kokoro instance
_engine_err = None
_loaded = False
_engine_lock = threading.RLock()
_synthesis_lock = threading.Lock()
_engine_voices: tuple[str, ...] = ()


class TtsUnavailable(RuntimeError):
    pass


def _onnx_files_present() -> bool:
    model, voices = resolve_kokoro_assets()
    return model.is_file() and voices.is_file()


def asset_status() -> dict:
    model, voices = resolve_kokoro_assets()
    return {
        "available": available(),
        "drop_path": str(kokoro_drop_dir()),
        "model_path": str(model),
        "voices_path": str(voices),
        "required_files": ["kokoro-v1.0.onnx", "voices-v1.0.bin"],
    }


def _load_engine():
    """Build the kokoro-onnx engine once (lazy). Re-attempts if the voice files
    appeared since a prior failed attempt (e.g. setup finished downloading them),
    so no restart is needed."""
    global _engine, _engine_err, _loaded, _engine_voices
    with _engine_lock:
        if getattr(sys, "frozen", False):
            _engine_err = "Kokoro is separately installed. Start a compatible speech server and set its API base URL in Settings > Voice > Kokoro. Model files alone do not install the engine."
            return
        if _loaded:
            if _engine is not None or not _onnx_files_present():
                return
        _loaded = True

        if not _onnx_files_present():
            _engine_err = f"voice files missing ({kokoro_drop_dir()})"
            print(f"[tts] {_engine_err}", flush=True)
            return
        try:
            from kokoro_onnx import Kokoro
            model, voices = resolve_kokoro_assets()
            _engine = Kokoro(str(model), str(voices))
            try:
                _engine_voices = tuple(sorted(str(v) for v in (_engine.get_voices() or [])))
            except Exception:
                _engine_voices = ()
            print("[tts] engine: kokoro-onnx", flush=True)
        except Exception as e:
            _engine_err = str(e)
            print(f"[tts] kokoro-onnx unavailable: {e}", flush=True)


def available() -> bool:
    """Cheap check: could TTS work? (Doesn't build the engine or load the model —
    that happens lazily on the first synth.)"""
    # The engine reference is published only after construction. Reading this
    # metadata must never wait for the lock held across synthesis.
    if getattr(sys, "frozen", False):
        return False
    if _engine is not None:
        return True
    try:
        import importlib.util
        return _onnx_files_present() and importlib.util.find_spec("kokoro_onnx") is not None
    except Exception:
        return False


def _wav_bytes(samples, rate=SAMPLE_RATE) -> bytes:
    import numpy as np
    s = np.clip(np.asarray(samples, dtype="float32").reshape(-1), -1.0, 1.0)
    pcm = (s * 32767.0).astype("<i2").tobytes()
    n = len(pcm)
    header = (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
              + b"data" + struct.pack("<I", n))
    return header + pcm


def _voice_names() -> list:
    """Voice ids the loaded pack offers (engine must be loaded). Empty on error."""
    with _engine_lock:
        if _engine is None:
            return []
        if _engine_voices:
            return list(_engine_voices)
        try:
            return sorted(str(v) for v in (_engine.get_voices() or []))
        except Exception:
            return []


def list_voices() -> list:
    """Stable voice identifiers available for selection/preview. Loads the
    engine lazily (first call may take a moment); [] when TTS is unavailable."""
    with _engine_lock:
        _load_engine()
        return _voice_names()


def _resolve_voice(voice: str) -> str:
    """Validate a requested voice id against the pack; fall back to default."""
    v = (voice or "").strip() or DEFAULT_VOICE_ID
    names = _voice_names()
    if names and v not in names:
        return DEFAULT_VOICE_ID if DEFAULT_VOICE_ID in names else names[0]
    return v


def _synth(
    text: str, speed: float, voice: str = "",
    cancelled: threading.Event | None = None,
) -> bytes:
    with _engine_lock:
        _load_engine()
        if _engine is None:
            raise TtsUnavailable(f"no TTS engine: {_engine_err or 'kokoro-onnx files/package missing'}")
        speed = max(0.7, min(1.4, float(speed or 1.0)))
        engine = _engine
        selected_voice = _resolve_voice(voice)
    # Kokoro synthesis itself is serialized, but its potentially long ONNX
    # call must not hold the metadata/load lock used by voice enumeration.
    # Cancelling an in-process worker cannot preempt ONNX; the cancelled
    # caller discards its result while metadata and future load checks remain
    # responsive.
    with _synthesis_lock:
        if cancelled is not None and cancelled.is_set():
            raise TtsUnavailable("TTS request was cancelled before synthesis")
        samples, rate = engine.create(
            text, voice=selected_voice, speed=speed, lang="en-us"
        )
    import numpy as np
    if not np.asarray(samples).size:
        raise TtsUnavailable("no audio produced")
    return _wav_bytes(samples, int(rate or SAMPLE_RATE))


async def synthesize(text: str, speed: float = 1.0, voice: str = "") -> bytes:
    """Return mono 16-bit WAV bytes for `text`. Runs the engine off the event loop.
    ``voice`` is a stable Kokoro voice id; empty/unknown falls back to the default."""
    text = (text or "").strip()
    if not text:
        raise TtsUnavailable("no text to speak")
    loop = asyncio.get_event_loop()
    cancelled = threading.Event()
    worker = loop.run_in_executor(None, lambda: _synth(text, speed, voice, cancelled))
    try:
        return await worker
    except asyncio.CancelledError:
        # ONNX cannot be preempted once create() has started, but a cancelled
        # request waiting behind another synthesis must not create stale audio.
        cancelled.set()
        raise
