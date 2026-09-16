"""Speech provider registry and adapters.

The registry keeps provider choice out of the model prompt.  Desktop voice
controls select one STT and one TTS backend; every adapter returns the same
small host contract.  Cloud credentials use VARIANT-1's encrypted shared
credential pool, while heavyweight local engines remain optional/user-owned.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import os
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from service_credentials import configured as credential_configured
from service_credentials import secret as credential_secret
from speech import local_tts, xai


class SpeechProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioResult:
    data: bytes
    mime_type: str


def audio_result(value, *, fallback_mime: str = "audio/wav") -> AudioResult:
    """Keep produced metadata; bytes-only local ports use an admission-time MIME."""
    if isinstance(value, AudioResult):
        return value
    if isinstance(value, (bytes, bytearray)):
        return AudioResult(bytes(value), fallback_mime)
    raise TypeError("speech synthesis must return AudioResult or audio bytes")


TTS_PROVIDERS: tuple[dict[str, Any], ...] = (
    {"id": "kokoro", "name": "Kokoro", "kind": "local", "auth": "none",
     "description": "Separately installed Kokoro speech server. Configure its API base URL; no engine or weights are bundled.", "default_voice": "af_nova",
     "default_model": "kokoro", "mime_type": "audio/wav"},
    {"id": "edge", "name": "Microsoft Edge", "kind": "cloud", "auth": "none",
     "description": "Microsoft neural voices through Edge TTS.",
     "default_voice": "en-US-AriaNeural", "default_model": "edge-tts",
     "mime_type": "audio/mpeg"},
    {"id": "elevenlabs", "name": "ElevenLabs", "kind": "cloud", "auth": "api_key",
     "description": "ElevenLabs multilingual speech and cloned voices.",
     "env_vars": ("ELEVENLABS_API_KEY",), "signup_url": "https://elevenlabs.io",
     "default_voice": "pNInz6obpgDQGcFmaJgB", "default_model": "eleven_multilingual_v2",
     "mime_type": "audio/mpeg"},
    {"id": "openai", "name": "OpenAI", "kind": "cloud", "auth": "api_key",
     "description": "OpenAI speech generation.", "env_vars": ("OPENAI_API_KEY",),
     "shared_provider": "openai", "signup_url": "https://platform.openai.com/api-keys",
     "default_voice": "alloy", "default_model": "gpt-4o-mini-tts",
     "mime_type": "audio/wav"},
    {"id": "minimax", "name": "MiniMax", "kind": "cloud", "auth": "api_key",
     "description": "MiniMax speech synthesis.", "env_vars": ("MINIMAX_API_KEY",),
     "shared_provider": "minimax", "signup_url": "https://platform.minimaxi.com",
     "default_voice": "English_expressive_narrator", "default_model": "speech-2.8-hd",
     "mime_type": "audio/mpeg"},
    {"id": "xai", "name": "xAI", "kind": "cloud", "auth": "shared",
     "description": "xAI Grok speech generation.", "env_vars": ("XAI_API_KEY",),
     "shared_provider": "xai", "signup_url": "https://console.x.ai",
     "default_voice": "eve", "default_model": "grok-tts", "mime_type": "audio/wav"},
    {"id": "mistral", "name": "Mistral", "kind": "cloud", "auth": "api_key",
     "description": "Mistral Voxtral text-to-speech.", "env_vars": ("MISTRAL_API_KEY",),
     "shared_provider": "mistral", "signup_url": "https://console.mistral.ai",
     "default_voice": "c69964a6-ab8b-4f8a-9465-ec0925096ec8",
     "default_model": "voxtral-mini-tts-2603", "mime_type": "audio/wav"},
    {"id": "gemini", "name": "Google Gemini", "kind": "cloud", "auth": "api_key",
     "description": "Gemini controllable speech generation.",
     "env_vars": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "shared_provider": "gemini",
     "signup_url": "https://aistudio.google.com/app/apikey", "default_voice": "Kore",
     "default_model": "gemini-3.1-flash-tts-preview", "mime_type": "audio/wav"},
    {"id": "neutts", "name": "NeuTTS", "kind": "local", "auth": "none",
     "description": "Local voice-cloning engine; user supplies package and reference audio.",
     "default_voice": "reference", "default_model": "neuphonic/neutts-air-q4-gguf",
     "mime_type": "audio/wav"},
    {"id": "kittentts", "name": "KittenTTS", "kind": "local", "auth": "none",
     "description": "Small local CPU text-to-speech model.", "default_voice": "Jasper",
     "default_model": "KittenML/kitten-tts-nano-0.8-int8", "mime_type": "audio/wav"},
    {"id": "piper", "name": "Piper", "kind": "local", "auth": "none",
     "description": "Local VITS speech with user-supplied Piper voice files.",
     "default_voice": "en_US-lessac-medium", "default_model": "",
     "mime_type": "audio/wav"},
    {"id": "deepinfra", "name": "DeepInfra", "kind": "cloud", "auth": "api_key",
     "description": "OpenAI-compatible speech generation through DeepInfra.",
     "env_vars": ("DEEPINFRA_API_KEY",), "shared_provider": "deepinfra",
     "signup_url": "https://deepinfra.com/dash/api_keys", "default_voice": "default",
     "default_model": "hexgrad/Kokoro-82M", "mime_type": "audio/wav"},
)

STT_PROVIDERS: tuple[dict[str, Any], ...] = (
    {"id": "local", "name": "Local Whisper", "kind": "local", "auth": "none",
     "description": "User-supplied whisper.cpp server and model.",
     "default_model": "whisper", "signup_url": ""},
    {"id": "groq", "name": "Groq", "kind": "cloud", "auth": "api_key",
     "description": "Fast hosted Whisper transcription.", "env_vars": ("GROQ_API_KEY",),
     "shared_provider": "groq", "signup_url": "https://console.groq.com/keys",
     "default_model": "whisper-large-v3-turbo"},
    {"id": "openai", "name": "OpenAI", "kind": "cloud", "auth": "api_key",
     "description": "OpenAI audio transcription.", "env_vars": ("OPENAI_API_KEY",),
     "shared_provider": "openai", "signup_url": "https://platform.openai.com/api-keys",
     "default_model": "gpt-4o-mini-transcribe"},
    {"id": "mistral", "name": "Mistral", "kind": "cloud", "auth": "api_key",
     "description": "Mistral Voxtral transcription.", "env_vars": ("MISTRAL_API_KEY",),
     "shared_provider": "mistral", "signup_url": "https://console.mistral.ai",
     "default_model": "voxtral-mini-latest"},
    {"id": "xai", "name": "xAI", "kind": "cloud", "auth": "shared",
     "description": "xAI Grok speech-to-text.", "env_vars": ("XAI_API_KEY",),
     "shared_provider": "xai", "signup_url": "https://console.x.ai",
     "default_model": "grok-stt"},
    {"id": "elevenlabs", "name": "ElevenLabs", "kind": "cloud", "auth": "api_key",
     "description": "ElevenLabs Scribe transcription.",
     "env_vars": ("ELEVENLABS_API_KEY",), "signup_url": "https://elevenlabs.io",
     "default_model": "scribe_v2"},
    {"id": "deepinfra", "name": "DeepInfra", "kind": "cloud", "auth": "api_key",
     "description": "Hosted Whisper through DeepInfra's OpenAI-compatible API.",
     "env_vars": ("DEEPINFRA_API_KEY",), "shared_provider": "deepinfra",
     "signup_url": "https://deepinfra.com/dash/api_keys",
     "default_model": "openai/whisper-large-v3-turbo"},
)

_TTS = {item["id"]: item for item in TTS_PROVIDERS}
_STT = {item["id"]: item for item in STT_PROVIDERS}


def tts_definitions() -> tuple[dict[str, Any], ...]:
    if not getattr(sys, "frozen", False):
        return TTS_PROVIDERS
    return tuple(row for row in TTS_PROVIDERS if row['id'] not in {'neutts', 'kittentts', 'piper'})


def accepts_credential(capability: str, provider: str) -> bool:
    rows = tts_definitions() if capability == 'tts' else STT_PROVIDERS if capability == 'stt' else ()
    return any(row['id'] == provider and row.get('auth') in {'api_key', 'optional', 'shared'} for row in rows)

VOICE_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "edge": ("en-US-AriaNeural", "en-US-JennyNeural", "en-US-AndrewNeural",
             "en-US-BrianNeural", "en-US-GuyNeural", "en-GB-SoniaNeural"),
    "openai": ("alloy", "ash", "ballad", "cedar", "coral", "echo", "fable",
               "marin", "nova", "onyx", "sage", "shimmer", "verse"),
    "xai": ("eve",),
    "gemini": ("Kore", "Zephyr", "Puck", "Charon", "Fenrir", "Leda", "Orus",
                "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus"),
    "kittentts": ("Jasper",),
    "piper": ("en_US-lessac-medium", "en_US-amy-medium", "en_US-ryan-high",
              "en_GB-alan-medium"),
}


def _provider_config(config: dict, capability: str, provider: str) -> dict:
    root = config.get(capability)
    root = root if isinstance(root, dict) else {}
    value = root.get(provider)
    return dict(value) if isinstance(value, dict) else {}


async def _key(router, capability: str, definition: dict) -> str:
    return await credential_secret(
        router, capability, str(definition["id"]),
        env_vars=tuple(definition.get("env_vars") or ()),
        shared_provider=str(definition.get("shared_provider") or ""),
    )


def _configured(router, capability: str, definition: dict) -> bool:
    if definition.get("auth") == "none":
        return True
    return credential_configured(
        router, capability, str(definition["id"]),
        env_vars=tuple(definition.get("env_vars") or ()),
        shared_provider=str(definition.get("shared_provider") or ""),
    )


def _module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def catalog(router, config: dict, *, local_stt_available: bool) -> dict:
    tts_rows = []
    for definition in tts_definitions():
        provider = str(definition["id"])
        available = _configured(router, "tts", definition)
        if provider == "kokoro":
            options = _provider_config(config, "tts", provider)
            if str(options.get("base_url") or "").strip():
                try:
                    _kokoro_base(options)
                    available = True
                except (ValueError, SpeechProviderError):
                    available = False
            else:
                available = local_tts.available()
        elif provider == "edge":
            available = _module("edge_tts")
        elif provider == "neutts":
            available = _module("neutts")
        elif provider == "kittentts":
            available = _module("kittentts")
        elif provider == "piper":
            available = _module("piper")
        tts_rows.append({**definition, "env_vars": list(definition.get("env_vars") or ()),
                         "configured": _configured(router, "tts", definition),
                         "available": bool(available),
                         "config": _provider_config(config, "tts", provider),
                         "voices": list(VOICE_SUGGESTIONS.get(provider, ()))})
    stt_rows = []
    for definition in STT_PROVIDERS:
        provider = str(definition["id"])
        available = local_stt_available if provider == "local" else _configured(
            router, "stt", definition)
        stt_rows.append({**definition, "env_vars": list(definition.get("env_vars") or ()),
                         "configured": _configured(router, "stt", definition),
                         "available": bool(available),
                         "config": _provider_config(config, "stt", provider)})
    return {"tts_providers": tts_rows, "stt_providers": stt_rows}


async def _response_bytes(response: httpx.Response, label: str) -> bytes:
    if response.status_code >= 400:
        detail = " ".join((response.text or "").split())[:400]
        raise SpeechProviderError(f"{label} returned HTTP {response.status_code}" +
                                  (f": {detail}" if detail else ""))
    data = bytes(response.content or b"")
    if not data:
        raise SpeechProviderError(f"{label} returned empty audio")
    return data


async def _post_audio(url: str, payload: dict, headers: dict, label: str,
                      *, timeout: float = 90) -> bytes:
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout, follow_redirects=True) as client:
            response = await client.post(url, json=payload, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        raise SpeechProviderError(f"{label} request failed ({type(exc).__name__})") from exc
    return await _response_bytes(response, label)


def _wav_from_pcm(pcm: bytes, rate: int = 24000, channels: int = 1,
                  sample_width: int = 2) -> bytes:
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(sample_width)
        stream.setframerate(rate)
        stream.writeframes(pcm)
    return target.getvalue()


async def list_voices(provider: str, router, config: dict) -> list[dict]:
    provider = str(provider or "kokoro").lower()
    if provider == "kokoro":
        options = _provider_config(config, "tts", provider)
        if options.get("base_url"):
            base = _kokoro_base(options)
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
                    response = await client.get(f"{base}/audio/voices")
                    response.raise_for_status()
                    values = response.json().get("voices", [])
                return [{"id": str(value), "name": str(value), "language": ""}
                        for value in values if isinstance(value, str)]
            except (httpx.HTTPError, OSError, ValueError, AttributeError) as exc:
                raise SpeechProviderError("Could not load voices from the configured Kokoro server. Check that it is running and its API base URL is correct.") from exc
        values = await asyncio.to_thread(local_tts.list_voices)
        return [{"id": value, "name": value, "language": ""} for value in values]
    if provider == "xai":
        return await xai.list_voices(router)
    if provider == "elevenlabs":
        key = await _key(router, "tts", _TTS[provider])
        if not key:
            raise SpeechProviderError("ElevenLabs needs an API key")
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
                response = await client.get("https://api.elevenlabs.io/v2/voices",
                                            headers={"xi-api-key": key})
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("voices") or []
            return [{"id": str(row.get("voice_id") or ""),
                     "name": str(row.get("name") or row.get("voice_id") or ""),
                     "language": ""} for row in rows if isinstance(row, dict)]
        except Exception as exc:
            raise SpeechProviderError(f"Could not load ElevenLabs voices: {exc}") from exc
    values = VOICE_SUGGESTIONS.get(provider, ())
    if not values:
        default = str((_TTS.get(provider) or {}).get("default_voice") or "")
        values = (default,) if default else ()
    return [{"id": value, "name": value, "language": ""} for value in values]


def _kokoro_base(options: dict) -> str:
    from urllib.parse import urlsplit
    base = str(options.get("base_url") or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SpeechProviderError("Kokoro needs an HTTP(S) API base URL, for example http://127.0.0.1:8880/v1.")
    return base


async def synthesize(provider: str, router, config: dict, text: str, *,
                     voice: str = "", speed: float = 1.0) -> AudioResult:
    provider = str(provider or "kokoro").lower()
    if provider not in _TTS:
        raise SpeechProviderError(f"Unknown TTS provider: {provider}")
    definition = _TTS[provider]
    options = _provider_config(config, "tts", provider)
    voice = str(voice or options.get("voice") or options.get("voice_id")
                or definition.get("default_voice") or "")
    model = str(options.get("model") or definition.get("default_model") or "")
    speed = max(0.25, min(4.0, float(speed or 1.0)))
    if provider == "kokoro":
        if options.get("base_url"):
            base = _kokoro_base(options)
            data = await _post_audio(f"{base}/audio/speech",
                {"model": model, "voice": voice, "input": text, "response_format": "wav", "speed": speed},
                {"Content-Type": "application/json"}, "Kokoro speech server")
            if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise SpeechProviderError("Kokoro server returned non-WAV audio; configure an OpenAI-compatible speech endpoint with WAV support.")
            return AudioResult(data, "audio/wav")
        data = await local_tts.synthesize(text, max(0.7, min(1.4, speed)), voice=voice)
        return AudioResult(data, "audio/wav")
    if provider == "xai":
        data = await xai.synthesize(router, text, voice=voice,
                                    language=str(options.get("language") or "en"),
                                    speed=max(0.7, min(1.5, speed)))
        return AudioResult(data, "audio/wav")
    if provider == "edge":
        return await _edge_tts(text, voice, speed)
    if provider == "elevenlabs":
        key = await _key(router, "tts", definition)
        if not key:
            raise SpeechProviderError("ElevenLabs needs an API key")
        data = await _post_audio(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            {"text": text, "model_id": model},
            {"xi-api-key": key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
            "ElevenLabs TTS")
        return AudioResult(data, "audio/mpeg")
    if provider in {"openai", "deepinfra"}:
        key = await _key(router, "tts", definition)
        if not key:
            raise SpeechProviderError(f"{definition['name']} needs an API key")
        base = str(options.get("base_url") or (
            "https://api.openai.com/v1" if provider == "openai"
            else "https://api.deepinfra.com/v1/openai")).rstrip("/")
        data = await _post_audio(f"{base}/audio/speech",
            {"model": model, "voice": voice, "input": text,
             "response_format": "wav", "speed": speed},
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            f"{definition['name']} TTS")
        return AudioResult(data, "audio/wav")
    if provider == "minimax":
        return await _minimax_tts(router, definition, options, text, voice, model, speed)
    if provider == "mistral":
        key = await _key(router, "tts", definition)
        if not key:
            raise SpeechProviderError("Mistral needs an API key")
        base = str(options.get("base_url") or "https://api.mistral.ai/v1").rstrip("/")
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
                response = await client.post(f"{base}/audio/speech",
                    json={"model": model, "input": text, "voice_id": voice,
                          "response_format": "wav"},
                    headers={"Authorization": f"Bearer {key}"})
            if response.status_code >= 400:
                await _response_bytes(response, "Mistral TTS")
            payload = response.json()
            encoded = str(payload.get("audio_data") or payload.get("audio") or "")
            if not encoded:
                raise SpeechProviderError("Mistral TTS returned no audio_data")
            return AudioResult(base64.b64decode(encoded), "audio/wav")
        except SpeechProviderError:
            raise
        except Exception as exc:
            raise SpeechProviderError(f"Mistral TTS failed: {exc}") from exc
    if provider == "gemini":
        return await _gemini_tts(router, definition, options, text, voice, model)
    if provider == "piper":
        data = await asyncio.to_thread(_piper_tts, text, options, voice, model, speed)
        return AudioResult(data, "audio/wav")
    if provider == "kittentts":
        data = await asyncio.to_thread(_kitten_tts, text, options, voice, model, speed)
        return AudioResult(data, "audio/wav")
    if provider == "neutts":
        data = await asyncio.to_thread(_neutts, text, options, model)
        return AudioResult(data, "audio/wav")
    raise SpeechProviderError(f"No TTS adapter for {provider}")


async def _edge_tts(text: str, voice: str, speed: float) -> AudioResult:
    try:
        import edge_tts
    except ImportError as exc:
        raise SpeechProviderError("Edge TTS runtime is not installed") from exc
    rate = f"{round((speed - 1.0) * 100):+d}%"
    communicator = edge_tts.Communicate(text, voice=voice, rate=rate)
    parts = bytearray()
    async for chunk in communicator.stream():
        if chunk.get("type") == "audio":
            parts.extend(chunk.get("data") or b"")
    if not parts:
        raise SpeechProviderError("Edge TTS returned empty audio")
    return AudioResult(bytes(parts), "audio/mpeg")


async def _minimax_tts(router, definition: dict, options: dict, text: str,
                       voice: str, model: str, speed: float) -> AudioResult:
    key = await _key(router, "tts", definition)
    if not key:
        raise SpeechProviderError("MiniMax needs an API key")
    base = str(options.get("base_url") or "https://api.minimax.io/v1/t2a_v2")
    group_id = str(options.get("group_id") or os.environ.get("MINIMAX_GROUP_ID") or "").strip()
    if group_id and "GroupId=" not in base:
        base += ("&" if "?" in base else "?") + f"GroupId={group_id}"
    payload = {"model": model, "text": text, "stream": False,
               "voice_setting": {"voice_id": voice, "speed": speed,
                                  "vol": float(options.get("volume") or 1.0),
                                  "pitch": int(options.get("pitch") or 0),
                                  "emotion": str(options.get("emotion") or "neutral")},
               "audio_setting": {"sample_rate": 32000, "bitrate": 128000,
                                  "format": "mp3", "channel": 1}}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
            response = await client.post(base, json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        response.raise_for_status()
        data = response.json()
        status = (data.get("base_resp") or {}).get("status_code", 0)
        if status not in {0, "0", None}:
            raise SpeechProviderError(str((data.get("base_resp") or {}).get("status_msg") or status))
        encoded = str((data.get("data") or {}).get("audio") or "")
        if not encoded:
            raise SpeechProviderError("MiniMax returned empty audio")
        return AudioResult(bytes.fromhex(encoded), "audio/mpeg")
    except SpeechProviderError:
        raise
    except Exception as exc:
        raise SpeechProviderError(f"MiniMax TTS failed: {exc}") from exc


async def _gemini_tts(router, definition: dict, options: dict, text: str,
                      voice: str, model: str) -> AudioResult:
    key = await _key(router, "tts", definition)
    if not key:
        raise SpeechProviderError("Gemini needs an API key")
    base = str(options.get("base_url") or
               "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    payload = {"contents": [{"parts": [{"text": text}]}],
               "generationConfig": {"responseModalities": ["AUDIO"],
                   "speechConfig": {"voiceConfig": {
                       "prebuiltVoiceConfig": {"voiceName": voice}}}}}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
            response = await client.post(f"{base}/models/{model}:generateContent",
                                         params={"key": key}, json=payload)
        response.raise_for_status()
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        inline = next((part.get("inlineData") or part.get("inline_data")
                       for part in parts if isinstance(part, dict)
                       and (part.get("inlineData") or part.get("inline_data"))), None)
        encoded = str((inline or {}).get("data") or "")
        if not encoded:
            raise SpeechProviderError("Gemini returned no audio data")
        return AudioResult(_wav_from_pcm(base64.b64decode(encoded)), "audio/wav")
    except SpeechProviderError:
        raise
    except Exception as exc:
        raise SpeechProviderError(f"Gemini TTS failed: {exc}") from exc


def _numpy_wav(samples: Any, rate: int = 24000) -> bytes:
    import numpy as np
    values = np.asarray(samples, dtype=np.float32).reshape(-1)
    pcm = (np.clip(values, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    return _wav_from_pcm(pcm, rate=rate)


def _kitten_tts(text: str, options: dict, voice: str, model: str, speed: float) -> bytes:
    try:
        from kittentts import KittenTTS
    except ImportError as exc:
        raise SpeechProviderError("KittenTTS is not installed") from exc
    engine = KittenTTS(model)
    return _numpy_wav(engine.generate(text, voice=voice, speed=speed,
                                      clean_text=options.get("clean_text", True)))


def _piper_tts(text: str, options: dict, _voice: str, model: str, speed: float) -> bytes:
    model_path = str(options.get("model_path") or model or "").strip()
    if not model_path or not Path(model_path).is_file():
        raise SpeechProviderError("Piper needs a user-supplied model_path")
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise SpeechProviderError("piper-tts is not installed") from exc
    engine = PiperVoice.load(model_path)
    target = io.BytesIO()
    with wave.open(target, "wb") as stream:
        try:
            from piper import SynthesisConfig
            engine.synthesize_wav(text, stream,
                syn_config=SynthesisConfig(length_scale=1.0 / max(0.25, speed)))
        except ImportError:
            engine.synthesize_wav(text, stream)
    return target.getvalue()


def _neutts(text: str, options: dict, model: str) -> bytes:
    from speech.local_neutts import synthesize as run_neutts
    try:
        return run_neutts(text, options, model)
    except Exception as exc:
        raise SpeechProviderError(str(exc)) from exc


async def transcribe(provider: str, router, config: dict, wav: bytes,
                     *, language: str | None = None) -> str:
    provider = str(provider or "local").lower()
    if provider == "local":
        raise SpeechProviderError("local transcription is owned by whisper.cpp")
    if provider not in _STT:
        raise SpeechProviderError(f"Unknown STT provider: {provider}")
    definition = _STT[provider]
    if provider == "xai":
        return await xai.transcribe(router, wav, language=language)
    key = await _key(router, "stt", definition)
    if not key:
        raise SpeechProviderError(f"{definition['name']} needs an API key")
    options = _provider_config(config, "stt", provider)
    model = str(options.get("model") or definition.get("default_model") or "")
    if provider == "elevenlabs":
        return await _multipart_transcribe("https://api.elevenlabs.io/v1/speech-to-text",
            key, wav, model, language, provider, auth_header="xi-api-key", model_field="model_id")
    bases = {"groq": "https://api.groq.com/openai/v1",
             "openai": "https://api.openai.com/v1",
             "mistral": "https://api.mistral.ai/v1",
             "deepinfra": "https://api.deepinfra.com/v1/openai"}
    base = str(options.get("base_url") or bases[provider]).rstrip("/")
    return await _multipart_transcribe(f"{base}/audio/transcriptions", key, wav,
                                       model, language, provider)


async def _multipart_transcribe(url: str, key: str, wav: bytes, model: str,
                                language: str | None, provider: str, *,
                                auth_header: str = "Authorization",
                                model_field: str = "model") -> str:
    headers = {auth_header: key if auth_header != "Authorization" else f"Bearer {key}"}
    fields: dict[str, str] = {model_field: model}
    if language and str(language).lower() not in {"auto", "detect", "none"}:
        fields["language"] = str(language)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=90, follow_redirects=True) as client:
            response = await client.post(url, data=fields,
                files={"file": ("audio.wav", wav, "audio/wav")}, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        raise SpeechProviderError(f"{provider} transcription failed ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        detail = " ".join((response.text or "").split())[:400]
        raise SpeechProviderError(f"{provider} transcription returned HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except Exception:
        text = response.text.strip()
        if text:
            return text
        raise SpeechProviderError(f"{provider} transcription returned no text")
    text = str(payload.get("text") or payload.get("transcript") or "").strip()
    if not text:
        raise SpeechProviderError(f"{provider} transcription returned no text")
    return text
