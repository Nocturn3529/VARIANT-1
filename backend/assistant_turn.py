"""Typed result of one assistant inference turn.

The agent loop consumes this object directly.  Provider-native tool calls never
pass through assistant prose or VARIANT-1-authored JSON, and private thinking is
kept separate from user-visible text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class AssistantTurn:
    """Provider-neutral assistant text, thinking, and native tool calls."""

    text: str = ""
    thinking: str = ""
    tool_calls: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    stop_reason: str = "stop"

    @property
    def actions(self) -> list[dict[str, Any]]:
        """Mutable copies suitable for the tool execution boundary."""
        return [dict(call) for call in self.tool_calls]


def normalized_stop_reason(reason: str, *, has_tool_calls: bool) -> str:
    """Normalize provider finish reasons to the small loop-level vocabulary."""
    raw = str(reason or "").strip().lower().replace("-", "_")
    if raw in {"length", "max_tokens", "max_output_tokens"} or raw.startswith("length:"):
        return "length"
    if raw in {"cancelled", "canceled", "aborted"}:
        return "aborted"
    if raw in {"error", "failed"}:
        return "error"
    if has_tool_calls or raw in {"tool_calls", "tool_call", "tool_use"}:
        return "tool_use"
    return "stop"


TurnDisposition = Literal[
    "stop", "tools", "truncated", "truncated_tools", "error", "aborted",
]


def turn_disposition(turn: AssistantTurn) -> TurnDisposition:
    """Return Pi's model-turn stop table for a provider-neutral turn.

    Error and abort always win over tool calls. Length-limited tool calls are
    never executed because their arguments may be truncated. Any other turn
    carrying calls executes them. A call-free length turn is recoverably
    truncated; only a call-free normal stop completes.
    """
    has_calls = bool(turn.tool_calls)
    reason = normalized_stop_reason(
        turn.stop_reason,
        has_tool_calls=has_calls,
    )
    if reason == "error":
        return "error"
    if reason == "aborted":
        return "aborted"
    if reason == "length":
        return "truncated_tools" if has_calls else "truncated"
    if has_calls:
        return "tools"
    return "stop"


def truncated_tool_outcomes(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create exactly one call-bound failure for each truncated tool call."""
    outcomes = []
    for index, action in enumerate(actions or []):
        tool = str(action.get("tool") or "")
        call_id = str(action.get("id") or f"call_{index}")
        error = (
            f'Tool call "{tool}" was not executed: the response hit the '
            "output token limit, so its arguments may be truncated. Re-issue "
            "the tool call with complete arguments."
        )
        outcomes.append({
            "tool": tool,
            "call_id": call_id,
            "result": error,
            "model_result": error,
            "ok": False,
            "executed": False,
            "status": "error",
            "error_class": "truncated_tool_call",
        })
    return outcomes
