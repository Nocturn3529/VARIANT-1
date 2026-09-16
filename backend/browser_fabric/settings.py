"""Validated browser preferences consumed by the existing Fabric adapters."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

from .models import BrowserValidationError


DEFAULTS = {
    "headed": True,
    "command_timeout_s": 10,
    "click_timeout_s": 4,
    "navigation_timeout_s": 30,
    "dialog_policy": "auto_dismiss",
    "record_sessions": False,
    "allow_private_urls": True,
    "evaluate_enabled": True,
}
MODES = ("embedded", "managed", "personal", "cdp", "cloud")
CLOUD_PROVIDERS = ("browserbase", "browser-use", "firecrawl")


def browser_surface(session) -> dict:
    settings = session.metadata.get('browser_settings') or {}
    mode = settings.get('mode') or session.kind
    surface = 'VARIANT-1 in-app browser' if session.kind == 'embedded' else 'managed external browser'
    if mode == 'cdp':
        surface = 'external browser attached through CDP'
    elif mode == 'cloud':
        surface = str(settings.get('cloud_provider') or 'remote') + ' cloud browser'
    return {'browser_kind': session.kind, 'browser_mode': mode, 'surface': surface}


def connection_url(value: object) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        if (parsed.scheme in {"http", "https", "ws", "wss"} and parsed.hostname
                and not parsed.username and not parsed.password and not parsed.fragment
                and not any(ord(c) < 32 for c in text)):
            _ = parsed.port
            return text
    except ValueError:
        pass
    raise BrowserValidationError("CDP endpoint must be an HTTP(S) or WebSocket URL without embedded credentials.")


def connection_error_detail(error: Exception) -> str:
    """Keep the actionable error line without signed URLs or bearer values."""
    line = str(error).splitlines()[0] if str(error) else type(error).__name__
    def origin(match):
        try:
            url = urlsplit(match.group(0))
            return f'{url.scheme}://{url.hostname or "remote"}'
        except ValueError:
            return '[browser endpoint]'
    line = re.sub(r'(?:https?|wss?)://[^\s<>]+', origin, line)
    line = re.sub(r'(?i)bearer\s+\S+', 'Bearer [redacted]', line)
    return line[:400]


def normalize_options(selection: dict) -> dict:
    result = {}
    for key in ("headed", "record_sessions", "allow_private_urls", "evaluate_enabled"):
        if key in selection:
            if type(selection[key]) is not bool:
                raise BrowserValidationError(f"{key} must be true or false.")
            result[key] = selection[key]
    for key in ("command_timeout_s", "navigation_timeout_s", "click_timeout_s"):
        if key in selection:
            value = selection[key]
            if type(value) is not int or not 1 <= value <= 300:
                raise BrowserValidationError(f"{key} must be from 1 to 300 seconds.")
            result[key] = value
    if "dialog_policy" in selection:
        if selection["dialog_policy"] not in {"auto_dismiss", "auto_accept"}:
            raise BrowserValidationError("Choose auto_dismiss or auto_accept dialog policy.")
        result["dialog_policy"] = selection["dialog_policy"]
    if selection["mode"] == "cdp":
        result["cdp_url"] = connection_url(selection.get("cdp_url"))
    if selection["mode"] == "cloud":
        provider = selection.get("cloud_provider")
        if provider not in CLOUD_PROVIDERS:
            raise BrowserValidationError("Choose Browserbase, Browser Use, or Firecrawl.")
        result["cloud_provider"] = provider
        if selection.get("project_id"):
            result["project_id"] = str(selection["project_id"]).strip()[:200]
        if provider == "browserbase" and not result.get("project_id"):
            raise BrowserValidationError("Browserbase requires its project ID.")
    if selection.get("executable_path"):
        if selection["mode"] != "managed":
            raise BrowserValidationError("A custom Chromium executable requires managed mode.")
        import os
        path = os.path.abspath(str(selection["executable_path"]))
        if not os.path.isfile(path):
            raise BrowserValidationError("The selected Chromium executable does not exist.")
        result["executable_path"] = path
    mode = selection["mode"]
    if mode in {"embedded", "cdp", "cloud"} and result.get("record_sessions"):
        raise BrowserValidationError("Local WebM recording requires a managed or personal browser.")
    if mode == "embedded":
        if result.get("headed") is False or result.get("allow_private_urls") is False:
            raise BrowserValidationError("The built-in browser is visible and permits local websites.")
        if "dialog_policy" in result:
            raise BrowserValidationError("Built-in browser dialogs are managed by the desktop.")
    for field in settings_catalog()['fields']:
        if field['key'] in selection and mode not in field['modes']:
            raise BrowserValidationError(f"{field['key']} is not supported by {mode} mode.")
    return result


def settings_catalog() -> dict:
    return {
        "modes": list(MODES), "defaults": dict(DEFAULTS),
        "cloud_providers": [{"id": name, "credential_service": "browser"} for name in CLOUD_PROVIDERS],
        "fields": [
            {"key": "cdp_url", "type": "string", "modes": ["cdp"]},
            {"key": "cloud_provider", "type": "select", "options": list(CLOUD_PROVIDERS), "modes": ["cloud"]},
            {"key": "project_id", "type": "string", "modes": ["cloud"]},
            {"key": "headed", "type": "boolean", "modes": ["managed", "personal"]},
            {"key": "executable_path", "type": "string", "modes": ["managed"]},
            {"key": "command_timeout_s", "type": "number", "min": 1, "max": 300, "modes": ["managed", "personal", "cdp", "cloud"]},
            {"key": "click_timeout_s", "type": "number", "min": 1, "max": 300, "modes": ["managed", "personal", "cdp", "cloud"]},
            {"key": "navigation_timeout_s", "type": "number", "min": 1, "max": 300, "modes": ["managed", "personal", "cdp", "cloud"]},
            {"key": "dialog_policy", "type": "select", "options": ["auto_dismiss", "auto_accept"], "modes": ["managed", "personal", "cdp", "cloud"]},
            {"key": "record_sessions", "type": "boolean", "modes": ["managed", "personal"]},
            {"key": "allow_private_urls", "type": "boolean", "modes": ["managed", "personal", "cdp", "cloud"]},
            {"key": "evaluate_enabled", "type": "boolean", "modes": list(MODES)},
        ],
    }


async def is_private_url(url: str) -> bool:
    """Optional browser policy; ordinary Python/network access is unchanged."""
    import asyncio
    host = urlsplit(url).hostname
    if not host:
        return False
    if host.casefold() == "localhost" or host.casefold().endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        rows = await asyncio.to_thread(socket.getaddrinfo, host, None)
        return any(not ipaddress.ip_address(row[4][0]).is_global for row in rows)
