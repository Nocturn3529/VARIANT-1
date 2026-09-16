"""Canonical model-step normalization shared by main and worker policies."""

import pytest

from agent_engine.model_step import (
    ModelStepCompleted,
    ModelStepFailed,
    ModelStepInterrupted,
    default_model_route,
    execute_model_step,
    normalize_model_step,
)
from assistant_turn import AssistantTurn
from model_providers import ProviderRequestError


def test_model_step_appends_assistant_turn_once_and_normalizes_route():
    messages = [{"role": "user", "content": "go"}]
    turn = AssistantTurn(
        text="working",
        tool_calls=({"id": "c1", "tool": "read_file", "args": {"path": "x"}},),
        stop_reason="stop",
    )

    step = normalize_model_step(messages, turn, contract="worker")

    assert step.disposition == "tools"
    assert step.stop_reason == "tool_use"
    assert default_model_route(step.disposition) == "tools"
    assert len(messages) == 2
    assert messages[-1]["role"] == "assistant"


def test_model_step_rejects_untyped_provider_results_without_mutating_history():
    messages = [{"role": "user", "content": "go"}]

    with pytest.raises(TypeError, match="task stream must return AssistantTurn"):
        normalize_model_step(messages, {"text": "not typed"}, contract="task")

    assert messages == [{"role": "user", "content": "go"}]


@pytest.mark.parametrize(
    ("disposition", "route"),
    [("tools", "tools"), ("truncated_tools", "tools"),
     ("truncated", "model_step"), ("stop", "finalize"),
     ("error", "finalize"), ("aborted", "finalize")],
)
def test_default_model_route(disposition, route):
    assert default_model_route(disposition) == route


class _Policy:
    contract = "test"

    def __init__(self, turn=None, *, stop_after_prepare=False, error=None):
        self.turn = turn
        self.stop_after_prepare = stop_after_prepare
        self.error = error
        self.prepared = False

    def should_stop(self):
        return self.prepared and self.stop_after_prepare

    async def prepare_messages(self, messages):
        self.prepared = True
        return list(messages)

    async def invoke(self, _messages):
        if self.error:
            raise self.error
        return self.turn


@pytest.mark.asyncio
async def test_policy_engine_owns_prepare_invoke_validate_and_append():
    messages = [{"role": "user", "content": "go"}]

    outcome = await execute_model_step(
        messages,
        _Policy(AssistantTurn(text="done")),
    )

    assert isinstance(outcome, ModelStepCompleted)
    assert outcome.step.turn.text == "done"
    assert outcome.messages[-1] == {"role": "assistant", "content": "done"}


@pytest.mark.asyncio
async def test_policy_engine_distinguishes_interruption_from_failure():
    messages = [{"role": "user", "content": "go"}]

    interrupted = await execute_model_step(
        messages, _Policy(stop_after_prepare=True))
    failed = await execute_model_step(
        messages, _Policy(error=ValueError("bad provider")))

    assert isinstance(interrupted, ModelStepInterrupted)
    assert isinstance(failed, ModelStepFailed)
    assert failed.detail == "ValueError: bad provider"


@pytest.mark.asyncio
async def test_policy_engine_replays_one_clean_retryable_provider_turn(monkeypatch):
    failure = ProviderRequestError(
        "hermes",
        "gateway timeout",
        status_code=524,
        retry_after_seconds=120,
    )
    failure.clean_turn_replay_safe = True

    class ReplayPolicy(_Policy):
        max_clean_replays = 1

        def __init__(self):
            super().__init__()
            self.calls = 0

        async def invoke(self, _messages):
            self.calls += 1
            if self.calls == 1:
                raise failure
            return AssistantTurn(text="recovered")

    delays = []

    async def no_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("agent_engine.model_step.asyncio.sleep", no_sleep)
    messages = [{"role": "user", "content": "go"}]
    policy = ReplayPolicy()

    outcome = await execute_model_step(messages, policy)

    assert isinstance(outcome, ModelStepCompleted)
    assert policy.calls == 2
    assert outcome.step.clean_replays == 1
    assert outcome.messages == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "recovered"},
    ]
    assert delays == [2.0]
