"""Hermes Agent's signed-in Nous Portal proxy.

Hermes owns the OAuth session and refresh lifecycle. VARIANT-1 talks only to the
fixed loopback OpenAI-compatible proxy and never reads or copies Hermes bearer
tokens. The proxy is started on demand when the logged-in Hermes installation
is available.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import httpx


HERMES_PROXY_ENDPOINT = "http://127.0.0.1:8645"
HERMES_PROXY_OPENAI_BASE = HERMES_PROXY_ENDPOINT + "/v1"
DEFAULT_HERMES_MODEL = "upstage/solar-pro4:free"


class HermesProxyError(RuntimeError):
    """The signed-in Hermes Nous proxy is unavailable or misconfigured."""


def hermes_executable_path() -> Path:
    override = str(os.environ.get("VARIANT1_HERMES_EXECUTABLE") or "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "hermes"
            / "hermes-agent"
            / "venv"
            / "Scripts"
            / "hermes.exe"
        )
    discovered = shutil.which("hermes")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise HermesProxyError(
        "Hermes Agent is not installed. Install Hermes or set "
        "VARIANT1_HERMES_EXECUTABLE to hermes.exe."
    )


async def _request_json(path: str, *, timeout: float) -> dict[str, Any]:
    url = HERMES_PROXY_ENDPOINT + "/" + str(path or "").lstrip("/")
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise HermesProxyError(f"Hermes proxy returned invalid JSON from /{path}")
    return payload


async def _health() -> dict[str, Any] | None:
    try:
        return await _request_json("health", timeout=2.0)
    except Exception:
        return None


def _start_proxy() -> None:
    executable = hermes_executable_path()
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(
            [
                str(executable),
                "proxy",
                "start",
                "--provider",
                "nous",
                "--host",
                "127.0.0.1",
                "--port",
                "8645",
            ],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as exc:
        raise HermesProxyError(f"Could not start Hermes proxy: {exc}") from exc


def _validate_health(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict) or str(payload.get("status") or "").lower() != "ok":
        raise HermesProxyError("Hermes proxy health check failed")
    upstream = str(payload.get("upstream") or "").strip()
    if "nous" not in upstream.lower():
        raise HermesProxyError(
            f"Port 8645 is serving {upstream or 'an unknown upstream'}, not Nous Portal"
        )
    if payload.get("authenticated") is not True:
        raise HermesProxyError(
            "Hermes is not authenticated with Nous Portal. Log in through Hermes, "
            "then try again."
        )
    return payload


async def ensure_proxy(model: str = DEFAULT_HERMES_MODEL) -> dict[str, Any]:
    """Start/check the fixed Nous proxy without obtaining its OAuth bearer."""

    selected = str(model or "").strip()
    if not selected:
        raise HermesProxyError("A Hermes model ID is required")
    health = await _health()
    if health is None:
        _start_proxy()
        deadline = asyncio.get_running_loop().time() + 30.0
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.4)
            health = await _health()
            if health is not None:
                break
        else:
            raise HermesProxyError(
                "Hermes did not open its loopback proxy. Open Hermes, log in to "
                "Nous Portal, and try again."
            )
    _validate_health(health)
    return {
        "endpoint": HERMES_PROXY_OPENAI_BASE,
        "model": selected,
        "upstream": str(health.get("upstream") or "Nous Portal"),
    }


async def available_models(*, start_if_needed: bool = True) -> list[str]:
    """Return model IDs advertised by the authenticated Nous proxy."""

    health = await _health()
    if health is None:
        if not start_if_needed:
            raise HermesProxyError(
                f"Hermes proxy is not running on {HERMES_PROXY_ENDPOINT}"
            )
        await ensure_proxy()
    else:
        _validate_health(health)
    try:
        payload = await _request_json("v1/models", timeout=20.0)
    except Exception as exc:
        raise HermesProxyError(f"Could not list Hermes models: {exc}") from exc
    names = {
        str(row.get("id") or "").strip()
        for row in payload.get("data") or ()
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    if not names:
        raise HermesProxyError("Hermes returned no Nous Portal models")
    return sorted(names, key=str.casefold)


__all__ = [
    "DEFAULT_HERMES_MODEL",
    "HERMES_PROXY_ENDPOINT",
    "HERMES_PROXY_OPENAI_BASE",
    "HermesProxyError",
    "available_models",
    "ensure_proxy",
    "hermes_executable_path",
]
