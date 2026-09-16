"""Per-call diagnostics shared by streaming provider adapters.

The data here is operational metadata only: provider termination reasons and
the number of native tool-call fragments observed on the wire.  It never
changes model requests or agent-loop behavior.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StreamDiagnostics:
    finish_reason: str = ""
    tool_deltas: int = 0
    provider: str = ""
    model: str = ""

    def note_finish_reason(self, value) -> None:
        reason = str(value or "").strip()
        if reason:
            self.finish_reason = reason

    def note_model(self, provider, model) -> None:
        self.provider = str(provider or "").strip()
        self.model = str(model or "").strip()


class CountingToolCallSink:
    """Count provider-native tool fragments while preserving the real sink."""

    def __init__(self, sink, diagnostics: StreamDiagnostics):
        self._sink = sink
        self._diagnostics = diagnostics

    def __getattr__(self, name):
        return getattr(self._sink, name)

    def add_openai_delta(self, tool_calls) -> None:
        calls = [call for call in (tool_calls or []) if isinstance(call, dict)]
        self._diagnostics.tool_deltas += len(calls)
        self._sink.add_openai_delta(tool_calls)

    def anthropic_block_start(self, index: int, block: dict) -> None:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            self._diagnostics.tool_deltas += 1
        self._sink.anthropic_block_start(index, block)

    def anthropic_input_json_delta(self, index: int, partial_json: str) -> None:
        if partial_json:
            self._diagnostics.tool_deltas += 1
        self._sink.anthropic_input_json_delta(index, partial_json)

    def anthropic_block_stop(self, index: int) -> None:
        self._sink.anthropic_block_stop(index)

    def add_gemini_function_call(self, call_or_name, args=None, **kwargs) -> None:
        name = (
            call_or_name.get("name")
            if isinstance(call_or_name, dict)
            else call_or_name
        )
        if str(name or "").strip():
            self._diagnostics.tool_deltas += 1
        self._sink.add_gemini_function_call(call_or_name, args, **kwargs)


def counting_tool_sink(sink, diagnostics: StreamDiagnostics):
    if sink is None:
        return None
    return CountingToolCallSink(sink, diagnostics)


def log_finish_reason(diagnostics: StreamDiagnostics) -> str:
    """Return a compact single-field value safe for the structured log line."""
    reason = str(diagnostics.finish_reason or "unreported").strip()
    return "_".join(reason.split())[:80] or "unreported"
