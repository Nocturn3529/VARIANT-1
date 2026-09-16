"""Signed-in Ollama Desktop Cloud proxy policy.

Ollama Desktop owns authentication.  VARIANT-1 talks only to the loopback helper;
``*:cloud`` and ``*-cloud`` tags are executed by Ollama Cloud, not the local
GPU.  This module deliberately has no API-key or direct ollama.com path.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
from typing import Any

import httpx


OLLAMA_DESKTOP_ENDPOINT = "http://127.0.0.1:11434"
OLLAMA_CLOUD_OPENAI_BASE = OLLAMA_DESKTOP_ENDPOINT + "/v1"
DEFAULT_OLLAMA_CLOUD_MODEL = "gemma4:31b-cloud"


class OllamaCloudError(RuntimeError):
    """The signed-in Desktop Cloud proxy is unavailable or unsafe."""


def is_cloud_model_id(model: str) -> bool:
    value = str(model or "").strip().lower()
    return bool(value) and (value.endswith(":cloud") or value.endswith("-cloud"))


def require_cloud_model_id(model: str) -> str:
    value = str(model or "").strip()
    if not is_cloud_model_id(value):
        raise OllamaCloudError(
            f"Refusing non-cloud Ollama model {value!r}. Use a *:cloud or "
            "*-cloud tag so inference stays on Ollama Cloud."
        )
    return value


def desktop_app_path() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        raise OllamaCloudError("LOCALAPPDATA is unavailable; cannot locate Ollama Desktop")
    return Path(local_app_data) / "Programs" / "Ollama" / "ollama app.exe"


async def _request_json(path: str, *, timeout: float) -> dict[str, Any]:
    url = OLLAMA_DESKTOP_ENDPOINT + "/" + str(path or "").lstrip("/")
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise OllamaCloudError(f"Ollama Desktop returned invalid JSON from /{path}")
    return payload


async def _helper_online() -> bool:
    try:
        await _request_json("api/version", timeout=2.0)
        return True
    except Exception:
        return False


def _start_desktop_app() -> None:
    app = desktop_app_path()
    if not app.is_file():
        raise OllamaCloudError(f"Ollama Desktop app is missing: {app}")
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(
            [str(app)],
            cwd=str(app.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as exc:
        raise OllamaCloudError(f"Could not start Ollama Desktop: {exc}") from exc


async def available_cloud_models(*, start_if_needed: bool = True) -> list[str]:
    """Return only signed-in cloud tags advertised by the Desktop helper."""

    if not await _helper_online():
        if not start_if_needed:
            raise OllamaCloudError(
                f"Ollama Desktop helper is not running on {OLLAMA_DESKTOP_ENDPOINT}"
            )
        _start_desktop_app()
        deadline = asyncio.get_running_loop().time() + 30.0
        while asyncio.get_running_loop().time() < deadline:
            if await _helper_online():
                break
            await asyncio.sleep(0.4)
        else:
            raise OllamaCloudError(
                "Ollama Desktop did not open its loopback helper. Open the app "
                "and sign in, then try again."
            )
    try:
        payload = await _request_json("api/tags", timeout=8.0)
    except Exception as exc:
        raise OllamaCloudError(f"Could not read Ollama Desktop Cloud tags: {exc}") from exc
    names = []
    for row in payload.get("models") or ():
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("model") or "").strip()
        if is_cloud_model_id(name):
            names.append(name)
    models = sorted(set(names), key=str.casefold)
    if not models:
        raise OllamaCloudError(
            "No Ollama Cloud tags are available. Sign in through Ollama Desktop "
            "and pull a *:cloud model."
        )
    return models


async def ensure_cloud_model(model: str) -> dict[str, Any]:
    """Start/check Desktop and require the selected cloud tag to be pulled."""

    selected = require_cloud_model_id(model)
    models = await available_cloud_models(start_if_needed=True)
    by_key = {item.casefold(): item for item in models}
    if selected.casefold() not in by_key:
        raise OllamaCloudError(
            f"Ollama Cloud model {selected!r} is not pulled in the signed-in app. "
            f"Available Cloud tags: {', '.join(models)}"
        )
    return {
        "endpoint": OLLAMA_CLOUD_OPENAI_BASE,
        "model": by_key[selected.casefold()],
        "available_models": models,
    }


__all__ = [
    "DEFAULT_OLLAMA_CLOUD_MODEL",
    "OLLAMA_CLOUD_OPENAI_BASE",
    "OLLAMA_DESKTOP_ENDPOINT",
    "OllamaCloudError",
    "available_cloud_models",
    "ensure_cloud_model",
    "is_cloud_model_id",
    "require_cloud_model_id",
]
