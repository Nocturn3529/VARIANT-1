"""Bounded remote-media admission for messaging adapters.

Transport adapters download provider-owned URLs before acknowledging the
message, then stage immutable files into VARIANT-1's ordinary chat-attachment
path.  The model therefore receives the same validated image/text projection
as a Deck attachment and retains a tool-readable path for other file types.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit

import httpx


MAX_REMOTE_ATTACHMENT_BYTES = 40 * 1024 * 1024
MAX_REMOTE_ATTACHMENTS = 8
_CHUNK_BYTES = 64 * 1024


def _safe_name(value: str, media_type: str = "") -> str:
    name = os.path.basename(str(value or "").replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")[:120]
    if not name:
        extension = mimetypes.guess_extension(str(media_type or "").split(";", 1)[0]) or ""
        name = "attachment" + extension
    return name


def _kind(media_type: str, name: str) -> str:
    mime = str(media_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        return "text"
    guessed, _encoding = mimetypes.guess_type(name)
    if str(guessed or "").startswith("image/"):
        return "image"
    if str(guessed or "").startswith("text/"):
        return "text"
    return "file"


def stage_attachment(
    root: str,
    *,
    adapter: str,
    conversation_id: str,
    message_id: str,
    name: str,
    media_type: str,
    payload: bytes,
    source_id: str = "",
) -> dict[str, Any]:
    raw = bytes(payload)
    if not raw:
        raise ValueError("remote attachment is empty")
    if len(raw) > MAX_REMOTE_ATTACHMENT_BYTES:
        raise ValueError(
            f"remote attachment exceeds {MAX_REMOTE_ATTACHMENT_BYTES} bytes"
        )
    digest = hashlib.sha256(raw).hexdigest()
    safe = _safe_name(name, media_type)
    identity = hashlib.sha256(
        f"{adapter}\0{conversation_id}\0{message_id}".encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()[:24]
    folder = os.path.join(os.path.abspath(root), str(adapter or "remote"), identity)
    os.makedirs(folder, exist_ok=True)
    destination = os.path.join(folder, f"{digest}-{safe}")
    if not os.path.isfile(destination):
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=folder, prefix=digest + ".", suffix=".tmp", delete=False
        )
        temporary = handle.name
        try:
            with handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
    return {
        "name": safe,
        "kind": _kind(media_type, safe),
        "mime": str(media_type or mimetypes.guess_type(safe)[0] or "application/octet-stream"),
        "path": destination,
        "size": len(raw),
        "sha256": digest,
        "source_id": str(source_id or ""),
    }


async def download_and_stage(
    client: httpx.AsyncClient,
    url: str,
    root: str,
    *,
    adapter: str,
    conversation_id: str,
    message_id: str,
    name: str,
    media_type: str = "",
    source_id: str = "",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    if adapter == "discord":
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            raise ValueError("Discord attachment URL must use its HTTPS CDN")
    async with client.stream("GET", str(url), headers=dict(headers or {}), follow_redirects=False) as response:
        response.raise_for_status()
        declared = response.headers.get("content-length")
        if declared:
            try:
                if int(declared) > MAX_REMOTE_ATTACHMENT_BYTES:
                    raise ValueError(
                        f"remote attachment exceeds {MAX_REMOTE_ATTACHMENT_BYTES} bytes"
                    )
            except ValueError as exc:
                if "exceeds" in str(exc):
                    raise
        resolved_media = (
            str(media_type or "").strip()
            or str(response.headers.get("content-type") or "").split(";", 1)[0].strip()
        )
        async for chunk in response.aiter_bytes(_CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_REMOTE_ATTACHMENT_BYTES:
                raise ValueError(
                    f"remote attachment exceeds {MAX_REMOTE_ATTACHMENT_BYTES} bytes"
                )
            chunks.append(bytes(chunk))
    return stage_attachment(
        root,
        adapter=adapter,
        conversation_id=conversation_id,
        message_id=message_id,
        name=name,
        media_type=resolved_media,
        payload=b"".join(chunks),
        source_id=source_id,
    )


__all__ = [
    "MAX_REMOTE_ATTACHMENT_BYTES",
    "MAX_REMOTE_ATTACHMENTS",
    "download_and_stage",
    "stage_attachment",
]
