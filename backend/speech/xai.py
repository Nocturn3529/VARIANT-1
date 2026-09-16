"""xAI adapters for VARIANT-1's explicit cloud STT/TTS routes.

The adapters deliberately reuse ``LLMRouter`` credential leases.  That gives
speech the same DPAPI storage, subscription-token refresh, credential pooling,
cooldowns, and xAI OAuth host restriction as model inference.  Audio and reply
text are uploaded only when the user selected the cloud route.
"""

from __future__ import annotations

from typing import Any

import httpx


DEFAULT_VOICE_ID = "eve"
DEFAULT_LANGUAGE = "auto"
_TIMEOUT_STT = 120.0
_TIMEOUT_TTS = 60.0


class SpeechUnavailable(RuntimeError):
    pass


def available(router) -> bool:
    """Cheap, network-free availability check for the xAI speech route."""
    return bool(router.has_cloud_key("xai"))


def _v1_url(base: str, path: str) -> str:
    base = str(base or "").rstrip("/")
    clean = path.lstrip("/")
    return f"{base}/{clean}" if base.endswith("/v1") else f"{base}/v1/{clean}"


async def _candidates(router):
    await router.ensure_oauth_fresh("xai")
    # This module is part of the router implementation boundary: using leases
    # Using credential leases preserves pool rotation and lets
    # provider_base_url enforce the OAuth x.ai-only host rule.
    leases = router._credential_leases("xai")
    if not leases:
        raise SpeechUnavailable(
            "xAI cloud speech needs an xAI API key or connected subscription."
        )
    out = []
    for lease in leases:
        base = router.provider_base_url("xai", lease)
        if base:
            out.append((lease, base))
    if not out:
        raise SpeechUnavailable("xAI cloud speech has no usable API endpoint.")
    return out


def _mark_success(router, lease) -> None:
    try:
        router.credential_pools.mark_success(lease)
    except Exception:
        pass


def _mark_failure(router, lease, status: int, detail: str) -> None:
    try:
        router.credential_pools.mark_failure(
            lease, status_code=int(status or 0), detail=str(detail or "")[:240]
        )
    except Exception:
        pass


def _error_detail(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("detail") or err)[:240]
            return str(body.get("message") or body.get("detail") or body)[:240]
    except Exception:
        pass
    return response.text[:240]


def _stt_form_data(language: str | None) -> dict[str, str]:
    """Optional STT form fields that must precede the audio ``file``.

    xAI rejects ``format=true`` without a concrete language code (HTTP 400).
    Mic capture usually omits language, so only enable inverse-text formatting
    when the caller (or config) supplies a real code such as ``en``.
    """
    lang = str(language or "").strip()
    if not lang or lang.lower() in ("auto", "detect", "none"):
        return {}
    # format=true requires language; together they normalize numbers/currency.
    return {"format": "true", "language": lang}


async def transcribe(router, wav_bytes: bytes, *, language: str | None = None) -> str:
    """Upload one in-memory WAV recording to xAI ``POST /v1/stt``."""
    if not wav_bytes:
        raise SpeechUnavailable("no audio captured")
    form_data = _stt_form_data(language)
    failures = []
    for lease, base in await _candidates(router):
        try:
            # httpx emits data fields before files, satisfying xAI's "file last" rule.
            async with httpx.AsyncClient(timeout=_TIMEOUT_STT, trust_env=False) as client:
                response = await client.post(
                    _v1_url(base, "stt"),
                    headers={"Authorization": f"Bearer {lease.secret}"},
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data=form_data or None,
                )
            if response.status_code == 200:
                result = response.json()
                text = str((result or {}).get("text") or "").strip()
                if not text:
                    raise SpeechUnavailable("xAI STT returned an empty transcript")
                _mark_success(router, lease)
                return text
            detail = _error_detail(response)
            _mark_failure(router, lease, response.status_code, detail)
            failures.append(f"HTTP {response.status_code}: {detail}")
        except SpeechUnavailable:
            raise
        except Exception as exc:
            _mark_failure(router, lease, 0, str(exc))
            failures.append(str(exc))
    raise SpeechUnavailable("xAI STT failed: " + "; ".join(failures[-3:]))


async def synthesize(
    router,
    text: str,
    *,
    voice: str = DEFAULT_VOICE_ID,
    language: str = DEFAULT_LANGUAGE,
    speed: float = 1.0,
) -> bytes:
    """Return WAV bytes from xAI ``POST /v1/tts``."""
    text = str(text or "").strip()
    if not text:
        raise SpeechUnavailable("no text to speak")
    payload = {
        "text": text[:15000],
        "voice_id": str(voice or DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID,
        "language": str(language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE,
        "output_format": {"codec": "wav", "sample_rate": 24000},
    }
    speed = max(0.7, min(1.5, float(speed or 1.0)))
    if speed != 1.0:
        payload["speed"] = speed

    failures = []
    for lease, base in await _candidates(router):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_TTS, trust_env=False) as client:
                response = await client.post(
                    _v1_url(base, "tts"),
                    headers={
                        "Authorization": f"Bearer {lease.secret}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if response.status_code == 200 and response.content:
                _mark_success(router, lease)
                return bytes(response.content)
            detail = _error_detail(response)
            _mark_failure(router, lease, response.status_code, detail)
            failures.append(f"HTTP {response.status_code}: {detail}")
        except Exception as exc:
            _mark_failure(router, lease, 0, str(exc))
            failures.append(str(exc))
    raise SpeechUnavailable("xAI TTS failed: " + "; ".join(failures[-3:]))


async def list_voices(router) -> list[dict]:
    """Fetch the current built-in xAI voice roster."""
    failures = []
    for lease, base in await _candidates(router):
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                response = await client.get(
                    _v1_url(base, "tts/voices"),
                    headers={"Authorization": f"Bearer {lease.secret}"},
                )
            if response.status_code == 200:
                rows = (response.json() or {}).get("voices") or []
                items = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    voice_id = str(row.get("voice_id") or row.get("id") or "").strip()
                    if voice_id:
                        items.append({
                            "id": voice_id,
                            "name": str(row.get("name") or voice_id).strip(),
                            "language": str(row.get("language") or "multilingual").strip(),
                        })
                _mark_success(router, lease)
                return items
            detail = _error_detail(response)
            _mark_failure(router, lease, response.status_code, detail)
            failures.append(f"HTTP {response.status_code}: {detail}")
        except Exception as exc:
            _mark_failure(router, lease, 0, str(exc))
            failures.append(str(exc))
    raise SpeechUnavailable("Could not load xAI voices: " + "; ".join(failures[-3:]))
