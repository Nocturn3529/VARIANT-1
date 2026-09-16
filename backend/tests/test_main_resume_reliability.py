"""Resume preserves accepted output and recovery limits at real boundaries."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_engine.presets import chat_task_default
from agent_engine.runner import run_main_chat_task
from assistant_turn import AssistantTurn
from chat_agent_stage import run_chat_agent_stage
from chat_stage_result import ChatStageFail
from llm_router import LocalEngineError
from test_agent_engine import FakePorts, SPEC, run_main


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_count", [1, 2])
async def test_resume_keeps_length_fragments_and_remaining_recovery_limit(recovery_count):
    boundaries = []

    async def commit(completed, next_node, state):
        boundaries.append((completed, next_node, copy.deepcopy(state)))

    await run_main_chat_task(
        config=chat_task_default().with_overrides(checkpoints=True),
        text="do the task", base_system="system", full_tspec=[SPEC],
        convo_tail=[], images=[], is_resume=False, resume_snap=None,
        ports=FakePorts([
            AssistantTurn(text="first ", stop_reason="length"),
            AssistantTurn(text="second ", stop_reason="length"),
            AssistantTurn(text="third", stop_reason="stop"),
        ]).build(), commit=commit,
    )
    saved = next(state for completed, _next, state in boundaries
                 if completed == "model_step"
                 and state["main"]["loop"]["route"] == "model_step"
                 and state["main"]["loop"]["length_recoveries"] == recovery_count)
    resumed_ports = FakePorts([
        AssistantTurn(text="resumed ", stop_reason="length" if recovery_count == 2 else "stop"),
        AssistantTurn(text="should not be requested", stop_reason="stop"),
    ])
    resumed = await run_main(resumed_ports, resume_snap=saved)
    assert len(resumed_ports.stream_messages) == 1
    assert resumed.loop_result.length_recoveries == recovery_count
    assert resumed.loop_result.reply.startswith(
        "first second resumed " if recovery_count == 2 else "first resumed "
    )
    assert resumed.loop_result.completion_status == ("truncated" if recovery_count == 2 else "ok")
    assert saved["main"]["loop"]["length_recoveries"] == recovery_count


@pytest.mark.asyncio
async def test_provider_failure_keeps_details_out_of_durable_user_message(monkeypatch):
    from agent_engine import executor
    failure = LocalEngineError("upstream payload with transport details and private text")
    monkeypatch.setattr(executor, "execute_main_chat", AsyncMock(side_effect=failure))
    request = SimpleNamespace(
        prepared=SimpleNamespace(is_resume=False, plan=SimpleNamespace(resume=SimpleNamespace(resume_state=None))),
        graph_revision="test", action_surface="ipython", provider_tool_schema_revision="test",
        agent_text="task", base_system="system", full_tool_specs=[SPEC], model_images=[],
        turn_ports=None, context_receipt={}, session_capabilities={},
    )
    session = SimpleNamespace(active=SimpleNamespace(task=None), convo=[], busy=True)
    result = await run_chat_agent_stage(SimpleNamespace(), session, request, reserved=False)
    assert isinstance(result, ChatStageFail)
    assert result.cause is failure
    assert str(failure) in result.error
    assert str(failure) not in result.user_message
    assert session.busy is False
