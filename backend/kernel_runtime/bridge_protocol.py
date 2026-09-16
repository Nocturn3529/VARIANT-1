"""Authenticated framing for worker-to-host capability bridge calls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import struct
import time
from typing import Any, Callable, Iterator, Mapping

from core_invariants import canonical_json_bytes as canonical_json


BRIDGE_SCHEMA = "variant1.kernel-bridge.v3"
BRIDGE_HANDSHAKE = "variant1.kernel-handshake.v3"
DEFAULT_MAX_FRAME_BYTES = 1_048_576
_LENGTH = struct.Struct("!I")


class BridgeProtocolError(RuntimeError):
    """An authenticated bridge frame violated its pinned contract."""


class UserWaitSignal:
    """Host-loop-owned pause clock, shared with one admitted capability call."""
    def __init__(self) -> None:
        self._started: float | None = None
        self._elapsed = 0.0

    def set(self) -> None:
        if self._started is None:
            self._started = time.monotonic()

    def clear(self) -> None:
        if self._started is not None:
            self._elapsed += time.monotonic() - self._started
            self._started = None

    def is_set(self) -> bool:
        return self._started is not None

    def elapsed(self) -> float:
        return self._elapsed + (time.monotonic() - self._started if self._started is not None else 0.0)


def _unsigned(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in envelope.items() if key != "mac"}


def sign_envelope(secret: bytes, envelope: Mapping[str, Any]) -> dict[str, Any]:
    if not secret:
        raise ValueError("bridge secret is required")
    out = _unsigned(envelope)
    out["mac"] = hmac.new(secret, canonical_json(out), hashlib.sha256).hexdigest()
    return out


def verify_envelope(
    secret: bytes,
    envelope: Mapping[str, Any],
    *,
    expected_nonce: str,
    expected_schema: str = BRIDGE_SCHEMA,
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise BridgeProtocolError("bridge frame must be a JSON object")
    supplied = str(envelope.get("mac") or "")
    expected = hmac.new(
        secret, canonical_json(_unsigned(envelope)), hashlib.sha256
    ).hexdigest()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise BridgeProtocolError("bridge frame authentication failed")
    if str(envelope.get("schema") or "") != expected_schema:
        raise BridgeProtocolError("bridge schema revision is unsupported")
    if str(envelope.get("nonce") or "") != str(expected_nonce or ""):
        raise BridgeProtocolError("bridge generation nonce is stale")
    return _unsigned(envelope)


def encode_frame(value: Mapping[str, Any], *, max_bytes: int) -> bytes:
    raw = canonical_json(dict(value))
    limit = max(1024, int(max_bytes))
    if len(raw) > limit:
        raise BridgeProtocolError(
            f"bridge frame exceeds {limit} bytes ({len(raw)})"
        )
    return _LENGTH.pack(len(raw)) + raw


def decode_frame(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except Exception as exc:
        raise BridgeProtocolError("bridge frame is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise BridgeProtocolError("bridge frame must decode to an object")
    return value


async def read_async_frame(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> dict[str, Any]:
    try:
        prefix = await reader.readexactly(_LENGTH.size)
    except asyncio.IncompleteReadError as exc:
        raise BridgeProtocolError("bridge frame ended before its length") from exc
    (size,) = _LENGTH.unpack(prefix)
    limit = max(1024, int(max_bytes))
    if size <= 0 or size > limit:
        raise BridgeProtocolError(f"bridge frame length {size} is not admitted")
    try:
        raw = await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise BridgeProtocolError("bridge frame body was truncated") from exc
    return decode_frame(raw)


async def write_async_frame(
    writer: asyncio.StreamWriter,
    value: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> None:
    writer.write(encode_frame(value, max_bytes=max_bytes))
    await writer.drain()


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise BridgeProtocolError("bridge socket closed during a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_sync_frame(
    sock: socket.socket,
    *,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> dict[str, Any]:
    (size,) = _LENGTH.unpack(_recv_exact(sock, _LENGTH.size))
    limit = max(1024, int(max_bytes))
    if size <= 0 or size > limit:
        raise BridgeProtocolError(f"bridge frame length {size} is not admitted")
    return decode_frame(_recv_exact(sock, size))


def write_sync_frame(
    sock: socket.socket,
    value: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> None:
    sock.sendall(encode_frame(value, max_bytes=max_bytes))


def response_frames(
    value: Mapping[str, Any], *, secret: bytes, max_bytes: int,
) -> Iterator[dict[str, Any]]:
    """Keep the complete signed response while bounding individual wire frames."""
    response = dict(value)
    raw = canonical_json(response)
    limit = max(1024, int(max_bytes))
    if len(raw) <= limit:
        yield response
        return
    identity = {key: response[key] for key in ('schema', 'nonce', 'generation', 'request_id')}
    # Base64 introduces no JSON escaping. Reserve the largest sequence field
    # needed by this response before choosing a fixed chunk size.
    overhead = len(canonical_json(sign_envelope(secret, {
        **identity, 'response_part': len(raw), 'data': '',
    })))
    chunk_bytes = 3 * ((limit - overhead) // 4)
    if chunk_bytes < 1:
        raise BridgeProtocolError('bridge response identity exceeds frame limit')
    count = (len(raw) + chunk_bytes - 1) // chunk_bytes
    yield sign_envelope(secret, {**identity, 'response_stream': {
        'bytes': len(raw), 'parts': count, 'sha256': hashlib.sha256(raw).hexdigest(),
    }})
    for index, offset in enumerate(range(0, len(raw), chunk_bytes)):
        yield sign_envelope(secret, {
            **identity, 'response_part': index,
            'data': base64.b64encode(raw[offset:offset + chunk_bytes]).decode('ascii'),
        })


class _ResponseAssembly:
    def __init__(self, first: dict[str, Any], verify: Callable[[dict[str, Any]], dict[str, Any]]):
        header = verify(first)
        self.verify = verify
        self.result = first
        self.parts = 0
        self.size = 0
        self.digest = ''
        self.body = bytearray()
        stream = header.get('response_stream')
        if stream is None:
            return
        if not isinstance(stream, dict):
            raise BridgeProtocolError('invalid bridge response stream')
        self.parts = stream.get('parts', 0)
        self.size = stream.get('bytes', 0)
        self.digest = str(stream.get('sha256') or '')
        if (type(self.parts) is not int or type(self.size) is not int
                or not 1 <= self.parts <= self.size or len(self.digest) != 64):
            raise BridgeProtocolError('invalid bridge response stream bounds')

    def append(self, raw: dict[str, Any], index: int) -> None:
        part = self.verify(raw)
        if type(part.get('response_part')) is not int or part['response_part'] != index:
            raise BridgeProtocolError('bridge response part sequence mismatch')
        try:
            data = base64.b64decode(part.get('data', ''), validate=True)
        except (ValueError, TypeError) as exc:
            raise BridgeProtocolError('invalid bridge response part encoding') from exc
        if not data or len(self.body) + len(data) > self.size:
            raise BridgeProtocolError('bridge response part exceeds declared length')
        self.body.extend(data)

    def finish(self) -> dict[str, Any]:
        if self.parts:
            if len(self.body) != self.size or hashlib.sha256(self.body).hexdigest() != self.digest:
                raise BridgeProtocolError('bridge response stream is incomplete or changed')
            self.result = decode_frame(bytes(self.body))
            # The reconstructed response has its own authentication/correlation,
            # in addition to each transport frame's authentication.
            self.verify(self.result)
        return self.result


def read_sync_response(
    sock: socket.socket, *, verify: Callable[[dict[str, Any]], dict[str, Any]],
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> dict[str, Any]:
    while True:
        first = read_sync_frame(sock, max_bytes=max_bytes)
        progress = verify(first)
        if progress.get('in_flight') is not True and progress.get('waiting_for_user') is not True:
            break
    assembly = _ResponseAssembly(first, verify)
    for index in range(assembly.parts):
        assembly.append(read_sync_frame(sock, max_bytes=max_bytes), index)
    return assembly.finish()


async def read_async_response(
    reader: asyncio.StreamReader, *, verify: Callable[[dict[str, Any]], dict[str, Any]],
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    idle_timeout_s: float | None = None,
) -> dict[str, Any]:
    async def read():
        return await asyncio.wait_for(read_async_frame(reader, max_bytes=max_bytes), timeout=idle_timeout_s)
    while True:
        first = await read()
        progress = verify(first)
        if progress.get('in_flight') is not True and progress.get('waiting_for_user') is not True:
            break
    assembly = _ResponseAssembly(first, verify)
    for index in range(assembly.parts):
        assembly.append(await read(), index)
    return assembly.finish()


async def write_async_response(
    writer: asyncio.StreamWriter, value: Mapping[str, Any], *, secret: bytes,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES, frame_timeout_s: float = 5.0,
) -> None:
    for frame in response_frames(value, secret=secret, max_bytes=max_bytes):
        await asyncio.wait_for(
            write_async_frame(writer, frame, max_bytes=max_bytes), timeout=frame_timeout_s,
        )
