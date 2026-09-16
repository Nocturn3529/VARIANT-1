"""Strict bounded JSONL protocol for VARIANT-1's persistent CPython worker.

The protocol carries execution and namespace-control frames only. Nested host
capability calls continue to use the separately authenticated bridge channel.
"""

from __future__ import annotations

import json
import math
import re
import threading
from typing import Any, BinaryIO, Mapping


REPL_PROTOCOL_SCHEMA = "variant1.repl-protocol.v1"
DEFAULT_MAX_REPL_FRAME_BYTES = 64 * 1024 * 1024
MAX_REQUEST_ID_CHARS = 160

REQUEST_TYPES = frozenset({
    "execute",
    "interrupt",
    "mount",
    "capsule_capture",
    "capsule_inspect",
    "capsule_restore",
    "resource_snapshot",
    "list_names",
    "shutdown",
})

EVENT_TYPES = frozenset({
    "ready",
    "stdout",
    "stderr",
    "result",
    "display",
    "update_display",
    "clear_output",
    "error",
    "namespace_delta",
    "execution_control",
    "resource_snapshot",
    "diagnostic",
    "done",
})

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class ReplProtocolError(RuntimeError):
    """One control frame violated the pinned REPL transport contract."""


def _finite_json(value: Any, *, depth: int = 0) -> bool:
    if depth > 128:
        return False
    if value is None or type(value) in {bool, int, str}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _finite_json(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


def _validate_common(frame: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(frame, Mapping):
        raise ReplProtocolError("REPL frame must be a JSON object")
    if str(frame.get("schema") or "") != REPL_PROTOCOL_SCHEMA:
        raise ReplProtocolError("REPL protocol revision is unsupported")
    frame_type = str(frame.get("type") or "")
    request_id = str(frame.get("id") or "")
    if request_id and (
        len(request_id) > MAX_REQUEST_ID_CHARS
        or _REQUEST_ID.fullmatch(request_id) is None
    ):
        raise ReplProtocolError("REPL request id is invalid")
    if not _finite_json(dict(frame)):
        raise ReplProtocolError("REPL frame contains a non-finite or non-JSON value")
    return frame_type, request_id


def validate_request(frame: Mapping[str, Any]) -> dict[str, Any]:
    frame_type, request_id = _validate_common(frame)
    if frame_type not in REQUEST_TYPES:
        raise ReplProtocolError(f"unknown REPL request type: {frame_type!r}")
    if not request_id:
        raise ReplProtocolError("REPL request id is required")
    return dict(frame)


def validate_event(frame: Mapping[str, Any]) -> dict[str, Any]:
    frame_type, request_id = _validate_common(frame)
    if frame_type not in EVENT_TYPES:
        raise ReplProtocolError(f"unknown REPL event type: {frame_type!r}")
    if frame_type == "ready":
        if request_id:
            raise ReplProtocolError("ready event must not have a request id")
    elif frame_type not in {"stdout", "stderr", "diagnostic"} and not request_id:
        raise ReplProtocolError(f"{frame_type} event requires a request id")
    return dict(frame)


def encode_line(
    frame: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_REPL_FRAME_BYTES,
) -> bytes:
    if not _finite_json(dict(frame)):
        raise ReplProtocolError("REPL frame contains a non-finite or non-JSON value")
    try:
        raw = json.dumps(
            dict(frame),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except Exception as exc:
        raise ReplProtocolError("REPL frame is not strict JSON") from exc
    limit = max(1024, int(max_bytes))
    if len(raw) > limit:
        raise ReplProtocolError(
            f"REPL frame exceeds {limit} bytes ({len(raw)})"
        )
    return raw + b"\n"


def decode_line(
    raw: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_REPL_FRAME_BYTES,
) -> dict[str, Any]:
    limit = max(1024, int(max_bytes))
    if len(raw) > limit + 1:
        raise ReplProtocolError(
            f"REPL frame exceeds {limit} bytes ({len(raw)})"
        )
    if not raw.endswith(b"\n"):
        raise ReplProtocolError("REPL frame ended before newline")
    body = raw[:-1]
    if not body:
        raise ReplProtocolError("REPL frame is empty")
    try:
        value = json.loads(body.decode("utf-8", errors="strict"))
    except Exception as exc:
        raise ReplProtocolError("REPL frame is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReplProtocolError("REPL frame must decode to an object")
    if not _finite_json(value):
        raise ReplProtocolError("REPL frame contains a non-finite value")
    return value


def read_line(
    stream: BinaryIO,
    *,
    max_bytes: int = DEFAULT_MAX_REPL_FRAME_BYTES,
) -> dict[str, Any] | None:
    """Read one bounded line without allowing an unbounded partial buffer."""

    limit = max(1024, int(max_bytes))
    raw = stream.readline(limit + 2)
    if raw == b"":
        return None
    if len(raw) > limit + 1 or not raw.endswith(b"\n"):
        raise ReplProtocolError("REPL frame is oversized or truncated")
    return decode_line(raw, max_bytes=limit)


class JsonLineWriter:
    """Thread-safe writer used by Python streams, fd drains, and the loop."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        max_bytes: int = DEFAULT_MAX_REPL_FRAME_BYTES,
    ) -> None:
        self.stream = stream
        self.max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()

    def write(self, frame: Mapping[str, Any]) -> None:
        raw = encode_line(frame, max_bytes=self.max_bytes)
        with self._lock:
            self.stream.write(raw)
            self.stream.flush()


def request_frame(request_id: str, frame_type: str, **fields: Any) -> dict[str, Any]:
    frame = {
        "schema": REPL_PROTOCOL_SCHEMA,
        "type": str(frame_type),
        "id": str(request_id),
        **fields,
    }
    return validate_request(frame)


def event_frame(request_id: str | None, frame_type: str, **fields: Any) -> dict[str, Any]:
    frame = {
        "schema": REPL_PROTOCOL_SCHEMA,
        "type": str(frame_type),
        "id": str(request_id or ""),
        **fields,
    }
    return validate_event(frame)


__all__ = [
    "DEFAULT_MAX_REPL_FRAME_BYTES",
    "EVENT_TYPES",
    "JsonLineWriter",
    "REPL_PROTOCOL_SCHEMA",
    "REQUEST_TYPES",
    "ReplProtocolError",
    "decode_line",
    "encode_line",
    "event_frame",
    "read_line",
    "request_frame",
    "validate_event",
    "validate_request",
]
