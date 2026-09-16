"""Provider-neutral model-step normalization shared by every graph policy.

Main chat and headless workers differ in image handling, active input, lineage,
and telemetry.  They must not differ in the semantic boundary after a model
call: validate ``AssistantTurn``, normalize its stop outcome, and append the
assistant message exactly once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, TypeAlias

import tool_calling
from assistant_turn import AssistantTurn, normalized_stop_reason, turn_disposition


LENGTH_RECOVERY_MESSAGE = (
    "The previous response reached its output limit before the task was "
    "complete. Continue from the current state."
)


def append_length_recovery(messages: list) -> list:
    """Append the one simple continuation boundary used by every agent graph."""

    messages.append({"role": "user", "content": LENGTH_RECOVERY_MESSAGE})
    return messages


@dataclass(frozen=True)
class ModelStep:
    turn: AssistantTurn
    actions: list[dict]
    disposition: str
    stop_reason: str
    clean_replays: int = 0


class ModelStepPolicy(Protocol):
    """Product policy around the one provider-neutral model-call engine."""

    contract: str

    def should_stop(self) -> bool: ...

    async def prepare_messages(self, messages: list) -> list: ...

    async def invoke(self, messages: list) -> AssistantTurn: ...


@dataclass(frozen=True)
class ModelStepCompleted:
    messages: list
    step: ModelStep


@dataclass(frozen=True)
class ModelStepInterrupted:
    messages: list


@dataclass(frozen=True)
class ModelStepFailed:
    messages: list
    exception: Exception
    clean_replays: int = 0

    @property
    def detail(self) -> str:
        return f"{type(self.exception).__name__}: {self.exception}"


ModelStepExecution: TypeAlias = (
    ModelStepCompleted | ModelStepInterrupted | ModelStepFailed
)


async def execute_model_step(
    messages: list,
    policy: ModelStepPolicy,
) -> ModelStepExecution:
    """Prepare, invoke, validate, and append one model turn for any product.

    Cancellation is checked before and after policy-specific preparation.  A
    cancellation that lands *after* the provider returns is intentionally left
    to product routing: main chat may stop a text turn immediately, while a
    headless worker must still bind tool-call results to their call ids.
    """
    working = messages
    if policy.should_stop():
        return ModelStepInterrupted(working)
    try:
        working = await policy.prepare_messages(working)
    except Exception as exc:
        return ModelStepFailed(working, exc)
    if policy.should_stop():
        return ModelStepInterrupted(working)
    clean_replays = 0
    max_clean_replays = max(
        0, min(int(getattr(policy, "max_clean_replays", 0) or 0), 2)
    )
    while True:
        try:
            turn = await policy.invoke(working)
            step = normalize_model_step(
                working,
                turn,
                contract=policy.contract,
                clean_replays=clean_replays,
            )
            return ModelStepCompleted(working, step)
        except Exception as exc:
            replay_safe = bool(
                getattr(exc, "clean_turn_replay_safe", False)
            )
            if (
                not replay_safe
                or clean_replays >= max_clean_replays
                or policy.should_stop()
            ):
                return ModelStepFailed(working, exc, clean_replays)
            clean_replays += 1
            requested = getattr(exc, "retry_after_seconds", None)
            try:
                delay = min(2.0, max(0.0, float(requested or 0.0)))
            except (TypeError, ValueError):
                delay = 0.0
            if delay:
                await asyncio.sleep(delay)


def normalize_model_step(
    messages: list,
    turn: AssistantTurn,
    *,
    contract: str,
    clean_replays: int = 0,
) -> ModelStep:
    """Validate and project one model result into the canonical transcript."""
    if not isinstance(turn, AssistantTurn):
        raise TypeError(f"{contract} stream must return AssistantTurn")
    actions = tool_calling.durable_provider_replay_actions(list(turn.actions))
    disposition = turn_disposition(turn)
    stop_reason = normalized_stop_reason(
        turn.stop_reason,
        has_tool_calls=bool(actions),
    )
    messages.append(tool_calling.format_assistant_turn_message(
        actions,
        assistant_text=turn.text,
    ))
    return ModelStep(
        turn=turn,
        actions=actions,
        disposition=disposition,
        stop_reason=stop_reason,
        clean_replays=max(0, int(clean_replays or 0)),
    )


def default_model_route(disposition: str) -> str:
    """Default route before caller-specific continuation policy is applied."""
    if disposition in {"tools", "truncated_tools"}:
        return "tools"
    if disposition == "truncated":
        return "model_step"
    return "finalize"
