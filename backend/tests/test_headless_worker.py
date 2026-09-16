"""Native typed-loop tests for subagents and other headless workers."""

from __future__ import annotations

import pytest

from agent_engine.runner import run_headless_worker
from agent_engine.runner import NativeRunnerScopeError, _require_single_ipython_catalog
from tool_discovery import ToolCatalogSnapshot
from agent_engine.presets import automation_v1, subagent_v1
from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
from agent_types import ToolBatchResult
from assistant_turn import AssistantTurn


SPEC = {"name": "ipython", "description": "execute Python", "params": {}}


def test_headless_runner_rejects_every_non_ipython_provider_catalog():
    with pytest.raises(NativeRunnerScopeError, match="exactly one provider action"):
        _require_single_ipython_catalog(ToolCatalogSnapshot.from_specs([
            {"name": "run_command", "description": "legacy", "params": {}},
        ]))


async def _run(
    turns,
    run_actions,
    *,
    stop_after_stream=False,
    resume_snap=None,
    stream_messages=None,
    config=None,
    snapshot_store=None,
    thread_id=None,
):
    stopped = False

    async def stream(_messages, _max_tokens, _specs=None):
        nonlocal stopped
        if stream_messages is not None:
            stream_messages.append([dict(message) for message in _messages])
        turn = turns.pop(0)
        if stop_after_stream:
            stopped = True
        return turn

    async def emit(_event, **_fields):
        return None

    async def compress(messages):
        return messages

    return await run_headless_worker(
        config=config or automation_v1(),
        title="worker",
        goal="read",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        full_tspec=[SPEC],
        stream=stream,
        stream_tools=stream,
        run_actions=run_actions,
        emit=emit,
        compress=compress,
        approx_tokens=lambda _messages: 1,
        ctx_threshold=lambda: 100_000,
        should_stop=lambda: stopped,
        clip=lambda text, limit: str(text)[:limit],
        is_resume=resume_snap is not None,
        resume_snap=resume_snap,
        snapshot_store=snapshot_store,
        thread_id=thread_id,
    )


@pytest.mark.asyncio
async def test_worker_plain_text_finishes_directly():
    async def no_actions(_actions):
        raise AssertionError("tools should not run")

    state = await _run([AssistantTurn(text="done")], no_actions)
    assert state["status"] == "completed"
    assert state["output"]["reply"] == "done"
    assert state["messages"][-1] == {"role": "assistant", "content": "done"}


@pytest.mark.asyncio
async def test_worker_tool_result_continues_to_next_typed_turn():
    action = {"tool": "ipython", "args": {}, "id": "r1"}
    calls = []

    async def run_actions(actions):
        calls.append(actions)
        return ToolBatchResult(
            text="[read_file] data",
            executed=True,
            outcomes=[{
                "tool": "ipython", "call_id": "r1",
                "result": "data", "model_result": "data",
                "ok": True, "executed": True,
            }],
        )

    state = await _run([
        AssistantTurn(tool_calls=(action,), stop_reason="tool_use"),
        AssistantTurn(text="summary"),
    ], run_actions)

    assert calls == [[action]]
    assert state["output"]["reply"] == "summary"
    assert [m["role"] for m in state["messages"][-3:]] == [
        "assistant", "tool", "assistant",
    ]


@pytest.mark.asyncio
async def test_worker_resume_executes_checkpointed_tool_batch_before_model():
    action = {"tool": "ipython", "args": {}, "id": "r1"}
    calls = []
    streamed = []

    async def run_actions(actions):
        calls.append(actions)
        return ToolBatchResult(
            text="data",
            executed=True,
            outcomes=[{
                "tool": "ipython", "call_id": "r1",
                "result": "data", "model_result": "data",
                "ok": True, "executed": True,
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

    state = await _run(
        [AssistantTurn(text="summary")],
        run_actions,
        resume_snap=resume_snap,
        stream_messages=streamed,
    )

    assert calls == [[action]]
    assert len(streamed) == 1
    assert [row["role"] for row in streamed[0][-2:]] == ["assistant", "tool"]
    assert streamed[0][-1]["tool_call_id"] == "r1"
    assert state["step"] == 2
    assert state["output"]["reply"] == "summary"


@pytest.mark.asyncio
async def test_worker_resume_preserves_saved_finalize_route_without_reinvocation():
    async def no_actions(_actions):
        raise AssertionError("a finalized checkpoint must not execute tools")

    resume_snap = {
        "run_id": "finalize-run",
        "step": 3,
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "finish"},
            {"role": "assistant", "content": "saved answer"},
        ],
        "worker": {
            "route": "finalize",
            "reply": "saved answer",
            "reply_fragments": ["saved ", "answer"],
            "interrupted": False,
            "terminal_reason": "completed",
            "consecutive_length_recoveries": 2,
            "length_recoveries": 2,
        },
    }

    state = await _run([], no_actions, resume_snap=resume_snap)

    assert state["status"] == "completed"
    assert state["step"] == 3
    assert state["output"]["reply"] == "saved answer"
    assert state["output"]["length_recoveries"] == 2
    assert state["worker"]["reply_fragments"] == ["saved ", "answer"]


@pytest.mark.asyncio
async def test_worker_tool_runner_exception_leaves_pending_boundary_resumable(tmp_path):
    action = {"tool": "ipython", "args": {"code": "effect()"}, "id": "effect-1"}
    store = SQLiteRunSnapshotStore(str(tmp_path / "worker-snapshots.sqlite3"))
    config = subagent_v1()
    thread_id = "durable-worker"

    async def explode(_actions):
        raise RuntimeError("tool runner exploded")

    with pytest.raises(RuntimeError, match="tool runner exploded"):
        await _run(
            [AssistantTurn(tool_calls=(action,), stop_reason="tool_use")],
            explode,
            config=config,
            snapshot_store=store,
            thread_id=thread_id,
        )

    pending = store.load_head_sync(thread_id)
    assert pending is not None
    assert pending.completed_node == "worker_step"
    assert pending.next_node == "worker_tool"
    assert pending.state["worker"]["route"] == "tools"
    assert pending.state["messages"][-1]["tool_calls"][0]["id"] == "effect-1"

    replayed = []

    async def recover(actions):
        replayed.append(actions)
        return ToolBatchResult(
            text="durable outcome",
            executed=True,
            outcomes=[{
                "tool": "ipython",
                "call_id": "effect-1",
                "result": "durable outcome",
                "model_result": "durable outcome",
                "ok": True,
                "executed": True,
                "status": "ok",
            }],
        )

    final = await _run(
        [AssistantTurn(text="finished after reconciliation")],
        recover,
        resume_snap=pending.state,
        config=config,
        snapshot_store=store,
        thread_id=thread_id,
    )

    assert replayed == [[action]]
    assert final["status"] == "completed"
    assert final["output"]["reply"] == "finished after reconciliation"
    assert store.load_head_sync(thread_id).status == "completed"


@pytest.mark.asyncio
async def test_worker_rejects_untyped_stream_results():
    async def no_actions(_actions):
        return ToolBatchResult()

    state = await _run(["legacy string"], no_actions)
    assert state["status"] == "error"
    assert "AssistantTurn" in state["output"]["reply"]


@pytest.mark.asyncio
async def test_worker_error_and_aborted_turns_do_not_execute_attached_calls():
    action = {"tool": "ipython", "args": {}, "id": "r1"}
    calls = []

    async def run_actions(actions):
        calls.append(actions)
        return ToolBatchResult()

    failed = await _run([
        AssistantTurn(text="provider failed", tool_calls=(action,), stop_reason="error"),
    ], run_actions)
    aborted = await _run([
        AssistantTurn(text="provider aborted", tool_calls=(action,), stop_reason="aborted"),
    ], run_actions)

    assert calls == []
    assert failed["status"] == "error"
    assert aborted["status"] == "cancelled"


@pytest.mark.asyncio
async def test_worker_length_limited_calls_return_bound_errors_without_execution():
    actions = (
        {"tool": "ipython", "args": {}, "id": "r1"},
        {"tool": "ipython", "args": {}, "id": "r2"},
    )
    calls = []

    async def run_actions(batch):
        calls.append(batch)
        return ToolBatchResult()

    state = await _run([
        AssistantTurn(tool_calls=actions, stop_reason="length"),
        AssistantTurn(text="recovered"),
    ], run_actions)

    assert calls == []
    tool_messages = [row for row in state["messages"] if row.get("role") == "tool"]
    assert [row["tool_call_id"] for row in tool_messages] == ["r1", "r2"]
    assert all("was not executed" in row["content"] for row in tool_messages)
    assert state["output"]["reply"] == "recovered"


@pytest.mark.asyncio
async def test_worker_call_free_length_recovers_then_finishes():
    streams = []

    async def no_actions(_actions):
        raise AssertionError("no tool call was requested")

    state = await _run([
        AssistantTurn(stop_reason="length"),
        AssistantTurn(text="recovered"),
    ], no_actions, stream_messages=streams)

    assert state["status"] == "completed"
    assert state["output"]["reply"] == "recovered"
    assert state["output"]["length_recoveries"] == 1
    assert streams[1][-1]["content"].startswith(
        "The previous response reached its output limit"
    )


@pytest.mark.asyncio
async def test_worker_repeated_call_free_length_is_typed_truncated():
    async def no_actions(_actions):
        raise AssertionError("no tool call was requested")

    state = await _run([
        AssistantTurn(stop_reason="length"),
        AssistantTurn(stop_reason="length"),
        AssistantTurn(stop_reason="length"),
    ], no_actions)

    assert state["status"] == "truncated"
    assert state["output"]["completion_status"] == "truncated"
    assert state["output"]["stop_reason"] == "length"
    assert state["output"]["terminal_reason"] == "model_output_limit"
    assert state["output"]["length_recoveries"] == 2


@pytest.mark.asyncio
async def test_worker_can_exceed_the_removed_turn_cap():
    cycles = 30
    turns = []
    for index in range(cycles):
        turns.append(AssistantTurn(tool_calls=({
            "tool": "ipython", "args": {}, "id": f"r{index}",
        },), stop_reason="tool_use"))
    turns.append(AssistantTurn(text="done after many turns"))

    async def run_actions(actions):
        call_id = actions[0]["id"]
        return ToolBatchResult(
            text="x",
            outcomes=[{
                "tool": "ipython", "call_id": call_id,
                "result": "x", "model_result": "x",
                "ok": True, "executed": True,
            }],
        )

    state = await _run(turns, run_actions)
    assert state["step"] == cycles + 1
    assert state["status"] == "completed"
    assert state["output"]["reply"] == "done after many turns"


@pytest.mark.asyncio
async def test_worker_cancellation_after_request_skips_dispatch_and_keeps_call_bound_result():
    action = {"tool": "ipython", "args": {}, "id": "r1"}
    calls = []

    async def run_actions(actions):
        calls.append(actions)
        raise AssertionError("a stop before dispatch must not call the tool runner")

    state = await _run(
        [AssistantTurn(tool_calls=(action,), stop_reason="tool_use")],
        run_actions,
        stop_after_stream=True,
    )

    assert calls == []
    assert state["status"] == "cancelled"
    assert state["output"]["terminal_reason"] == "user_cancelled"
    assert [row["role"] for row in state["messages"][-2:]] == ["assistant", "tool"]
    assert state["messages"][-1]["tool_call_id"] == "r1"
    assert state["messages"][-1]["is_error"] is True
    assert state["messages"][-1]["content"] == "cancelled before execution"


@pytest.mark.asyncio
async def test_worker_terminating_batch_stops_without_another_model_call():
    action = {"tool": "ipython", "args": {}, "id": "r1"}
    calls = []

    async def run_actions(actions):
        calls.append(actions)
        return ToolBatchResult(
            text="done",
            executed=True,
            terminate=True,
            outcomes=[{
                "tool": "ipython", "call_id": "r1",
                "result": "done", "model_result": "done",
                "ok": True, "executed": True, "terminate": True,
            }],
        )

    state = await _run([
        AssistantTurn(text="Finished.", tool_calls=(action,)),
    ], run_actions)

    assert calls == [[action]]
    assert state["status"] == "completed"
    assert state["output"]["reply"] == "Finished."
