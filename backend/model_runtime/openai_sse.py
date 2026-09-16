"""One terminal-aware decoder for OpenAI-compatible chat SSE streams."""

from __future__ import annotations

from dataclasses import dataclass
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

    def __init__(self) -> None:
        self.saw_terminal = False
        self._pending = ""

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
        if data == "[DONE]":
            self.saw_terminal = True
            return OpenAIStreamEvent(done=True)
        try:
            payload = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        for choice in payload.get("choices") or ():
            if isinstance(choice, dict) and choice.get("finish_reason"):
                self.saw_terminal = True
                break
        return OpenAIStreamEvent(payload=payload)

    def feed_text(self, chunk: str | bytes) -> tuple[OpenAIStreamEvent, ...]:
        text = (
            chunk.decode("utf-8", errors="ignore")
            if isinstance(chunk, bytes)
            else str(chunk or "")
        )
        self._pending += text
        lines = self._pending.split("\n")
        self._pending = lines.pop()
        output = []
        for line in lines:
            event = self.decode_line(line.rstrip("\r"))
            if event is not None:
                output.append(event)
        return tuple(output)

    def finish(self) -> OpenAIStreamEvent | None:
        pending, self._pending = self._pending, ""
        return self.decode_line(pending.rstrip("\r")) if pending else None

    def require_terminal(
        self,
        label: str,
        *,
        error_factory: Callable[[str], BaseException] = RuntimeError,
    ) -> None:
        if not self.saw_terminal:
            raise error_factory(
                f"{str(label or 'OpenAI-compatible stream')} ended without "
                "a terminal event"
            )


__all__ = ["OpenAIChatSSEDecoder", "OpenAIStreamEvent"]
