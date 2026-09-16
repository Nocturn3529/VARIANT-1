"""Focused contracts for VARIANT-1's framework-free headless runner."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agent_engine.runner as runner_module
from artifacts import ContentAddressedArtifactStore
from browser_fabric import BrowserBinding, bind_browser_fabric, create_browser_fabric
from agent_engine.presets import automation_v1
from agent_engine.runner import (
    _apply_node,
    run_headless_worker,
)
from agent_types import ToolBatchResult
from assistant_turn import AssistantTurn
from test_browser_fabric_phase4 import FakeBrowserAdapter
from work_fabric.scope import WorkScope


SPEC = {"name": "ipython", "description": "execute Python", "params": {}}


@pytest.mark.asyncio
async def test_headless_runner_owns_and_closes_the_fabric_session(monkeypatch, tmp_path):
    adapters = []

    def factory(kind, profile, session):
        adapter = FakeBrowserAdapter()
        adapters.append(adapter)
        return adapter

    fabric = create_browser_fabric(
        data_dir=str(tmp_path / "data"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
        adapter_factory=factory,
    )
    scope = WorkScope(chat_id="headless-chat")
    session = await fabric.open_session(scope=scope)
    binding = BrowserBinding(
        fabric_session_id=session.session_id,
        owner_kind="run",
        owner_id="headless-run",
        scope=scope,
    )
    context = SimpleNamespace(browser_binding=binding)
    monkeypatch.setattr(
        runner_module, "run_context_from_state", lambda *_args, **_kwargs: context,
    )

    async def fail_body(**_kwargs):
        assert fabric.session(session.session_id, scope=scope).state == "active"
        raise RuntimeError("headless failure")

    monkeypatch.setattr(
        runner_module, "_run_headless_state_machine_body", fail_body,
    )
    with bind_browser_fabric(fabric):
        with pytest.raises(RuntimeError, match="headless failure"):
            await runner_module.run_headless_state_machine(
                config=SimpleNamespace(),
                initial_state={},
                runtime=SimpleNamespace(),
            )
    assert fabric.session(session.session_id, scope=scope).state == "closed"
    assert adapters[0].closed is True
    assert binding.fabric_session_id == ""


async def _run_worker(
    turns,
    run_actions,
    *,
    resume_snap=None,
    call_order=None,
    drain_inbound=None,
    ack_inbound=None,
):
    queued_turns = list(turns)

    async def stream(messages, _max_tokens, _specs=None):
        if call_order is not None:
            call_order.append(("provider", [dict(row) for row in messages]))
        return queued_turns.pop(0)

    async def emit(_event, **_fields):
        return None

    async def compress(messages):
        return messages

    return await run_headless_worker(
        config=automation_v1(),
        title="automation",
        goal="complete scheduled work",
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ],
        full_tspec=[SPEC],
        stream=stream,
        stream_tools=stream,
        run_actions=run_actions,
        emit=emit,
        compress=compress,
        approx_tokens=lambda _messages: 1,
        ctx_threshold=lambda: 100_000,
        should_stop=lambda: False,
        clip=lambda text, limit: str(text)[:limit],
        is_resume=resume_snap is not None,
        resume_snap=resume_snap,
        drain_inbound=drain_inbound,
        ack_inbound=ack_inbound,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_ack", [False, True])
async def test_worker_acknowledges_inbound_message_and_finishes(async_ack):
    acknowledged = []
    calls = []

    async def acknowledge_async(message_ids):
        await asyncio.sleep(0)
        acknowledged.extend(message_ids)

    async def no_actions(_actions):
        raise AssertionError("a text reply must not execute tools")

    state = await _run_worker(
        [AssistantTurn(text="applied the parent instruction")],
        no_actions,
        call_order=calls,
        drain_inbound=lambda: [{
            "message_id": "parent-message-1", "text": "Use the updated constraint",
        }],
        ack_inbound=acknowledge_async if async_ack else acknowledged.extend,
    )

    assert acknowledged == ["parent-message-1"]
    assert state["status"] == "completed"
    assert state["output"]["reply"] == "applied the parent instruction"
    assert calls[0][1][-1]["content"] == (
        "[PARENT MESSAGE]\nUse the updated constraint"
    )


def _event_names(state):
    return [row["event"] for row in state.get("observability") or []]


@pytest.mark.asyncio
async def test_node_patch_uses_top_level_last_value_replacement():
    state = {
        "messages": [{"role": "user", "content": "keep"}],
        "worker": {"route": "model_step", "reply": "replace me"},
    }

    async def patch(_state):
        return {"worker": {"route": "finalize"}}

    updated = await _apply_node(state, patch)

    assert updated["messages"] is state["messages"]
    assert updated["worker"] == {"route": "finalize"}


@pytest.mark.asyncio
async def test_plain_text_route_has_the_explicit_native_phase_order():
    async def no_actions(_actions):
        raise AssertionError("plain text must not route through tools")

    state = await _run_worker([AssistantTurn(text="done")], no_actions)

    assert state["status"] == "completed"
    assert state["step"] == 1
    assert state["output"]["reply"] == "done"
    assert _event_names(state) == [
        "agent_runtime:init",
        "agent_runtime:worker_prepare",
        "agent_runtime:worker_step",
        "agent_runtime:worker_complete",
        "agent_runtime:finalize",
    ]


@pytest.mark.asyncio
async def test_headless_length_continuation_preserves_all_fragments():
    async def no_actions(_actions):
        raise AssertionError("text continuation must not route through tools")

    state = await _run_worker([
        AssistantTurn(text="one ", stop_reason="length"),
        AssistantTurn(text="two ", stop_reason="length"),
        AssistantTurn(text="three"),
    ], no_actions)

    assert state["status"] == "completed"
    assert state["output"]["reply"] == "one two three"
    assert state["output"]["length_recoveries"] == 2


@pytest.mark.asyncio
async def test_headless_truncated_tool_recovery_preserves_assistant_text():
    async def no_actions(_actions):
        raise AssertionError("a truncated tool call must never be executed")

    state = await _run_worker([
        AssistantTurn(
            text="preamble ",
            tool_calls=({"tool": "ipython", "args": {}, "id": "cut-1"},),
            stop_reason="length",
        ),
        AssistantTurn(text="done"),
    ], no_actions)

    assert state["status"] == "completed"
    assert state["output"]["reply"] == "preamble done"
    assert state["output"]["length_recoveries"] == 1


@pytest.mark.asyncio
async def test_headless_terminal_output_limit_keeps_partial_text_and_diagnostic():
    async def no_actions(_actions):
        raise AssertionError("text continuation must not route through tools")

    state = await _run_worker([
        AssistantTurn(text="one ", stop_reason="length"),
        AssistantTurn(text="two ", stop_reason="length"),
        AssistantTurn(text="three ", stop_reason="length"),
    ], no_actions)

    assert state["status"] == "truncated"
    assert state["output"]["reply"].startswith("one two three ")
    assert "reached its output limit repeatedly" in state["output"]["reply"]
    assert state["output"]["length_recoveries"] == 2


@pytest.mark.asyncio
async def test_tool_route_returns_to_model_in_the_explicit_native_phase_order():
    action = {"tool": "ipython", "args": {}, "id": "r1"}
    calls = []

    async def run_actions(actions):
        calls.append(actions)
        return ToolBatchResult(
            text="data",
            executed=True,
            outcomes=[{
                "tool": "ipython",
                "call_id": "r1",
                "result": "data",
                "model_result": "data",
                "ok": True,
                "executed": True,
            }],
        )

    state = await _run_worker([
        AssistantTurn(tool_calls=(action,), stop_reason="tool_use"),
        AssistantTurn(text="summary"),
    ], run_actions)

    assert calls == [[action]]
    assert state["status"] == "completed"
    assert state["step"] == 2
    assert state["output"]["reply"] == "summary"
    assert [row["role"] for row in state["messages"][-3:]] == [
        "assistant", "tool", "assistant",
    ]
    assert _event_names(state) == [
        "agent_runtime:init",
        "agent_runtime:worker_prepare",
        "agent_runtime:worker_step",
        "agent_runtime:worker_tool_result",
        "agent_runtime:worker_step",
        "agent_runtime:worker_complete",
        "agent_runtime:finalize",
    ]


@pytest.mark.asyncio
async def test_dangling_tool_call_resume_executes_tool_before_provider():
    action = {"tool": "ipython", "args": {}, "id": "r1"}
    call_order = []

    async def run_actions(actions):
        call_order.append(("tool", list(actions)))
        return ToolBatchResult(
            text="data",
            executed=True,
            outcomes=[{
                "tool": "ipython",
                "call_id": "r1",
                "result": "data",
                "model_result": "data",
                "ok": True,
                "executed": True,
            }],
        )

    resume_snap = {
        "step": 1,
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "r1",
                    "type": "function",
                    "function": {"name": "ipython", "arguments": "{}"},
                }],
            },
        ],
        "worker": {
            "route": "tools",
            "actions": [action],
            "turn_disposition": "tools",
            "stop_reason": "tool_use",
            "reply": "",
            "interrupted": False,
        },
        "tools": {"disclosed_names": ["ipython"]},
    }

    state = await _run_worker(
        [AssistantTurn(text="summary")],
        run_actions,
        resume_snap=resume_snap,
        call_order=call_order,
    )

    assert [kind for kind, _payload in call_order] == ["tool", "provider"]
    provider_messages = call_order[1][1]
    assert [row["role"] for row in provider_messages[-2:]] == [
        "assistant", "tool",
    ]
    assert provider_messages[-1]["tool_call_id"] == "r1"
    assert state["step"] == 2
    assert state["output"]["reply"] == "summary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [automation_v1()],
)
async def test_native_headless_runner_accepts_automation(config):
    model_calls = 0
    tool_calls = 0

    async def stream(_messages, _max_tokens, _specs=None):
        nonlocal model_calls
        model_calls += 1
        return AssistantTurn(text="must not run")

    async def run_actions(_actions):
        nonlocal tool_calls
        tool_calls += 1
        return ToolBatchResult()

    async def emit(_event, **_fields):
        return None

    async def compress(messages):
        return messages

    state = await run_headless_worker(
        config=config,
        title="native source",
        goal="run natively",
        messages=[{"role": "user", "content": "run"}],
        full_tspec=[SPEC],
        stream=stream,
        stream_tools=stream,
        run_actions=run_actions,
        emit=emit,
        compress=compress,
        approx_tokens=lambda _messages: 1,
        ctx_threshold=lambda: 100_000,
        should_stop=lambda: False,
        clip=lambda text, limit: str(text)[:limit],
    )

    assert state["status"] == "completed"
    assert model_calls == 1
    assert tool_calls == 0
