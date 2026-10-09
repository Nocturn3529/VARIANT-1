"""One terminal-aware decoder for OpenAI-compatible chat SSE streams."""

from __future__ import annotations

from dataclasses import dataclass
import codecs
import json
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class OpenAIStreamEvent:
    payload: dict[str, Any] | None = None
    done: bool = False


class OpenAIChatSSEDecoder:
    """Decode SSE data lines and retain the provider terminal boundary.

    Callers still own token/tool/usage projection. This object owns the part
    that must not drift between chat, loopback gateway, and benchmarks:
    framing, JSON admission, ``[DONE]``, and ``finish_reason`` detection.
    """

    def __init__(self, *, max_frame_chars: int = 4 * 1024 * 1024) -> None:
        self.saw_terminal = False
        self._pending = ""
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.max_frame_chars = max(1, int(max_frame_chars))
        self.data_lines = self.json_events = self.malformed_events = 0
        self.terminal_kind = ""
        self.end_reason = "reading"
        self.http_status = 0

    def diagnostics(self, end_reason: str = "") -> dict:
        return {"schema": "variant1.openai-stream.v1", "end_reason": end_reason or self.end_reason,
                "saw_terminal": self.saw_terminal, "terminal_kind": self.terminal_kind,
                "data_lines": self.data_lines, "json_events": self.json_events,
                "malformed_events": self.malformed_events, "http_status": self.http_status}

    def decode_line(self, line: str | bytes) -> OpenAIStreamEvent | None:
        text = (
            line.decode("utf-8", errors="ignore")
            if isinstance(line, bytes)
            else str(line or "")
        ).strip()
        if not text or not text.startswith("data:"):
            return None
        data = text[5:].strip()
        if not data:
            return None
        self.data_lines += 1
        if len(data) > self.max_frame_chars:
            self.end_reason = "oversized_event"
            raise ValueError("OpenAI stream event exceeds the bounded frame size")
        if data == "[DONE]":
            self.saw_terminal = True
            self.terminal_kind = "done"
            self.end_reason = "terminal"
            return OpenAIStreamEvent(done=True)
        try:
            payload = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.malformed_events += 1
            return None
        if not isinstance(payload, dict):
            self.malformed_events += 1
            return None
        self.json_events += 1
        for choice in payload.get("choices") or ():
            if isinstance(choice, dict) and choice.get("finish_reason"):
                self.saw_terminal = True
                self.terminal_kind = "finish_reason"
                self.end_reason = "terminal"
                break
        return OpenAIStreamEvent(payload=payload)

    def feed_text(self, chunk: str | bytes) -> tuple[OpenAIStreamEvent, ...]:
        text = (
            self._utf8.decode(chunk)
            if isinstance(chunk, bytes)
            else str(chunk or "")
        )
        self._pending += text
        lines = self._pending.split("\n")
        self._pending = lines.pop()
        if len(self._pending) > self.max_frame_chars:
            self.end_reason = "oversized_event"
            self._pending = ""
            raise ValueError("OpenAI stream event exceeds the bounded frame size")
        output = []
        for line in lines:
            event = self.decode_line(line.rstrip("\r"))
            if event is not None:
                output.append(event)
        return tuple(output)

    def finish(self) -> OpenAIStreamEvent | None:
        pending, self._pending = self._pending + self._utf8.decode(b"", final=True), ""
        return self.decode_line(pending.rstrip("\r")) if pending else None

    def require_terminal(
        self,
        label: str,
        *,
        error_factory: Callable[[str], BaseException] = RuntimeError,
    ) -> None:
        if not self.saw_terminal:
            self.end_reason = "incomplete_eof"
            raise error_factory(
                f"{str(label or 'OpenAI-compatible stream')} ended without "
                "a terminal event"
            )


__all__ = ["OpenAIChatSSEDecoder", "OpenAIStreamEvent"]
