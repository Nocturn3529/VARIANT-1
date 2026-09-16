"""Strict framing and validation for the persistent CPython transport."""

from __future__ import annotations

import io

import pytest

from kernel_runtime.repl_protocol import (
    REPL_PROTOCOL_SCHEMA,
    ReplProtocolError,
    decode_line,
    encode_line,
    event_frame,
    read_line,
    request_frame,
    validate_event,
    validate_request,
)


def test_repl_protocol_round_trips_strict_utf8_json_lines():
    request = request_frame(
        "cell-a", "execute", code="value = 'Ω'", admission={"generation": 1}
    )
    encoded = encode_line(request, max_bytes=4096)
    assert encoded.endswith(b"\n")
    assert decode_line(encoded, max_bytes=4096) == request
    assert read_line(io.BytesIO(encoded), max_bytes=4096) == request


def test_repl_protocol_rejects_unknown_nonfinite_and_partial_frames():
    with pytest.raises(ReplProtocolError, match="unknown REPL request"):
        validate_request({
            "schema": REPL_PROTOCOL_SCHEMA,
            "type": "complete",
            "id": "request-a",
        })
    with pytest.raises(ReplProtocolError, match="non-finite"):
        encode_line({
            "schema": REPL_PROTOCOL_SCHEMA,
            "type": "execute",
            "id": "request-a",
            "value": float("nan"),
        })
    with pytest.raises(ReplProtocolError, match="before newline"):
        decode_line(b'{"schema":"variant1.repl-protocol.v1"}')
    with pytest.raises(ReplProtocolError, match="oversized or truncated"):
        read_line(io.BytesIO(b"x" * 1026), max_bytes=1024)


def test_repl_event_contract_distinguishes_ready_background_and_terminal():
    ready = event_frame(
        None,
        "ready",
        protocol=REPL_PROTOCOL_SCHEMA,
        generation=1,
        nonce="nonce",
    )
    background = event_frame(None, "stdout", text="background")
    terminal = event_frame("cell-a", "done", status="ok")
    assert validate_event(ready)["id"] == ""
    assert validate_event(background)["id"] == ""
    assert validate_event(terminal)["id"] == "cell-a"
    with pytest.raises(ReplProtocolError, match="requires a request id"):
        validate_event({
            "schema": REPL_PROTOCOL_SCHEMA,
            "type": "done",
            "id": "",
            "status": "ok",
        })
