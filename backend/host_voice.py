"""Canonical speech configuration derived from the live router."""

from __future__ import annotations

from typing import Callable, Optional
from copy import deepcopy
import hashlib
import json


def voice_cfg(router) -> dict:
    value = router.cfg.setdefault("voice", {})
    if not isinstance(value, dict):
        value = {}
        router.cfg["voice"] = value
    value.setdefault("stt_provider", "local")
    value.setdefault("tts_provider", "kokoro")
    value.setdefault("auto_tts", False)
    value.setdefault("speed", 1.0)
    value.setdefault("stt", {})
    value.setdefault("tts", {})
    return value


def stt_provider(router) -> str:
    return str(voice_cfg(router).get("stt_provider") or "local").strip().lower()


def tts_provider(router) -> str:
    return str(voice_cfg(router).get("tts_provider") or "kokoro").strip().lower()


def stt_route(router) -> str:
    return "local" if stt_provider(router) == "local" else "cloud"


def tts_route(router) -> str:
    return "local" if tts_provider(router) in {"kokoro", "neutts", "kittentts", "piper"} else "cloud"


def tts_enabled(router) -> bool:
    return bool(voice_cfg(router).get("auto_tts", False))


def tts_speed(router) -> float:
    try:
        return max(0.25, min(4.0, float(voice_cfg(router).get("speed", 1.0))))
    except Exception:
        return 1.0


def provider_options(router, capability: str, provider: str) -> dict:
    root = voice_cfg(router).get(capability)
    root = root if isinstance(root, dict) else {}
    value = root.get(provider)
    return dict(value) if isinstance(value, dict) else {}


def tts_voice(router, *, default_cloud: str = "eve", default_local: str = "af_nova") -> str:
    provider = tts_provider(router)
    options = provider_options(router, "tts", provider)
    if options.get("voice") or options.get("voice_id"):
        return str(options.get("voice") or options.get("voice_id"))
    try:
        from speech.providers import TTS_PROVIDERS
        definition = next(item for item in TTS_PROVIDERS if item["id"] == provider)
        return str(definition.get("default_voice") or "")
    except Exception:
        return default_local if tts_route(router) == "local" else default_cloud


def set_tts(router, key, value, *, save: Optional[Callable[[], None]] = None) -> None:
    previous = voice_cfg(router)
    config = deepcopy(previous)
    name = str(key or "")
    if name in {"tts_enabled", "auto_tts"}:
        config["auto_tts"] = bool(value)
    elif name == "speed":
        try:
            config["speed"] = max(0.25, min(4.0, float(value)))
        except (TypeError, ValueError):
            return
    elif name in {"tts_provider", "stt_provider"}:
        provider = str(value or "").strip().lower()
        if not provider:
            return
        config[name] = provider
    elif name == "voice":
        provider = tts_provider(router)
        voice = str(value or "").strip()
        target = config.setdefault("tts", {}).setdefault(provider, {})
        if voice:
            target["voice"] = voice
            target.pop("voice_id", None)
        else:
            target.pop("voice", None)
            target.pop("voice_id", None)
    elif name in {"tts_options", "stt_options"}:
        capability = name.split("_", 1)[0]
        if not isinstance(value, dict):
            return
        provider = str(value.get("provider") or (
            tts_provider(router) if capability == "tts" else stt_provider(router)
        )).strip().lower()
        fields = value.get("fields")
        if not provider or not isinstance(fields, dict):
            return
        target = config.setdefault(capability, {}).setdefault(provider, {})
        for field, field_value in fields.items():
            if str(field) not in {"api_key", "token", "secret"}:
                target[str(field)] = field_value
        if capability == "tts" and provider == "kokoro" and "base_url" in fields:
            from speech.providers import _kokoro_base
            target["base_url"] = str(target.get("base_url") or "").strip()
            if target["base_url"]:
                target["base_url"] = _kokoro_base(target)
    else:
        return
    router.cfg["voice"] = config
    try:
        persist = save or getattr(router, "save_config", None)
        if persist is not None and persist() is False:
            raise OSError("Speech settings could not be saved.")
    except BaseException:
        router.cfg["voice"] = previous
        raise


def tts_config_key(config: dict) -> str:
    provider = str(config.get("tts_provider") or "kokoro")
    value = {"provider": provider, "options": (config.get("tts") or {}).get(provider) or {}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
