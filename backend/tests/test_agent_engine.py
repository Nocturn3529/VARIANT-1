"""Behavioral tests for the typed main-chat model/tool loop."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import pytest

from agent_engine.runner import (
    NativeRunnerScopeError,
    _main_node_for_route,
    run_main_chat_task,
)
from agent_engine.snapshot_utils import is_incomplete_run_state
from agent_engine.state import new_run_state
from agent_engine.presets import chat_task_default
from agent_engine.task_ports import (
    TaskLoopCorePorts,
    TaskSetupPorts,
    TaskTurnPorts,
)
from agent_engine.shared_ports import AgentContextPorts
from agent_task import Task, TaskStatus
from agent_types import ToolBatchResult
from assistant_turn import AssistantTurn
from message_context_extents import HOST_CONTEXT_PREFIX_KEY


SPEC = {
    "name": "ipython",
    "description": "execute Python",
    "params": {"code": {"type": "string", "required": False}},
}


@dataclass
class FakePorts:
    turns: list[AssistantTurn]
    results: list[ToolBatchResult] = field(default_factory=list)
    stopped: bool = False
    action_batches: list[list] = field(default_factory=list)
    events: list[tuple] = field(default_factory=list)
    steering: list[dict] = field(default_factory=list)
    follow_up: list[dict] = field(default_factory=list)
    stream_messages: list[list[dict]] = field(default_factory=list)
    recorded_inputs: list[tuple[dict, str | None]] = field(default_factory=list)
    steering_after_action: list[dict] = field(default_factory=list)
    stop_after_stream: bool = False
    approx_prompt_tokens: int = 1
    compress_threshold: int = 100_000
    context_window: int = 0
    prompt_token_counts: list[int] = field(default_factory=list)
    compressed_messages: list[dict] | None = None
    compress_calls: int = 0
    capture_sessions: bool = False
    session_snapshots: list[tuple[dict, dict]] = field(default_factory=list)
    reasoning_enabled: bool = False
    stream_max_tokens: list[int] = field(default_factory=list)

    def build(self) -> TaskTurnPorts:
        async def stream(_messages, _max_tokens, _images):
            self.stream_max_tokens.append(int(_max_tokens))
            if self.capture_sessions:
                from browser_fabric import browser_binding_snapshot
                from desktop_fabric import desktop_binding_snapshot

                self.session_snapshots.append((
                    desktop_binding_snapshot(),
                    browser_binding_snapshot(),
                ))
            self.stream_messages.append([dict(message) for message in _messages])
            turn = self.turns.pop(0)
            if isinstance(turn, BaseException):
                raise turn
            if self.stop_after_stream:
                self.stopped = True
                self.stop_after_stream = False
            return turn

        async def run_actions(actions):
            self.action_batches.append(actions)
            if self.steering_after_action:
                self.steering.extend(self.steering_after_action)
                self.steering_after_action = []
            return self.results.pop(0)

        async def emit(event, **fields):
            self.events.append((event, fields))

        async def compress(messages):
            self.compress_calls += 1
            if self.compressed_messages is not None:
                return [dict(message) for message in self.compressed_messages]
            return messages

        async def count_prompt_tokens(_messages, *, tools, image_b64=None):
            if self.prompt_token_counts:
                return self.prompt_token_counts.pop(0)
            return None

        return TaskTurnPorts(
            setup=TaskSetupPorts(
                build_tools_block=lambda _names, _specs: "",
                tool_lines=lambda _specs: "",
                registry_get=lambda _name: object(),
                make_task=Task,
                new_run=lambda source, title: {"id": "run_test", "source": source, "title": title},
                install_image_sink=lambda _holder: object(),
                use_reasoning=lambda: self.reasoning_enabled,
                current_model=lambda: "test-model",
            ),
            loop=TaskLoopCorePorts(
                stream=stream,
                run_actions=run_actions,
                emit=emit,
                should_stop=lambda: self.stopped,
                clip=lambda text, limit: str(text)[:limit],
                state_block=lambda _task: "",
                drain_steering=lambda: self.steering.pop(0) if self.steering else None,
                drain_follow_up=lambda: self.follow_up.pop(0) if self.follow_up else None,
                record_active_input=lambda row, assistant: self.recorded_inputs.append(
                    (dict(row), assistant)
                ),
            ),
            context=AgentContextPorts(
                approx_tokens=lambda _messages: self.approx_prompt_tokens,
                ctx_compress_threshold=lambda: self.compress_threshold,
                compress_messages=compress,
                count_tokens=count_prompt_tokens,
                context_limit=lambda: self.context_window,
            ),
        )


async def run_main(fake: FakePorts, *, specs=None, resume_snap=None):
    return await run_main_chat_task(
        config=chat_task_default().with_overrides(checkpoints=False),
        text="do the task",
        base_system="system",
        full_tspec=list(specs if specs is not None else [SPEC]),
        convo_tail=[],
        images=[],
        is_resume=resume_snap is not None,
        resume_snap=resume_snap,
        ports=fake.build(),
    )


@pytest.mark.asyncio
async def test_chat_snapshot_terminal_waits_for_transcript_acknowledgement():
    boundaries: list[tuple[str, str, dict]] = []

    async def commit(completed: str, next_node: str, state: dict) -> None:
        boundaries.append((completed, next_node, copy.deepcopy(state)))

    result = await run_main_chat_task(
        config=chat_task_default().with_overrides(checkpoints=True),
        text="do the task",
        base_system="system",
        full_tspec=[SPEC],
        convo_tail=[],
        images=[],
        is_resume=False,
        resume_snap=None,
        ports=FakePorts([AssistantTurn(text="done")]).build(),
        commit=commit,
    )

    assert boundaries[-1][0:2] == ("finalize", "end")
    assert boundaries[-1][2]["status"] == "awaiting_transcript"
    terminal_model_step = next(
        state
        for completed, next_node, state in boundaries
        if completed == "model_step" and next_node == "main_finalize"
    )
    assert is_incomplete_run_state(terminal_model_step) is True
    resumed_ports = FakePorts([])
    resumed = await run_main(resumed_ports, resume_snap=terminal_model_step)
    assert resumed.loop_result.reply == "done"
    assert resumed.transcript_id == result.transcript_id
    assert resumed_ports.stream_messages == []
    assert resumed_ports.recorded_inputs == []
    assert result.commit_transcript_terminal is not None
    await result.commit_transcript_terminal()
    assert boundaries[-1][2]["status"] == "completed"
    assert boundaries[-1][2]["output"]["transcript_committed"] is True


@pytest.mark.asyncio
async def test_main_chat_rejects_any_provider_surface_beside_ipython():
    with pytest.raises(NativeRunnerScopeError, match="exactly one provider action"):
        await run_main(
            FakePorts([]),
            specs=[{
                "name": "apply_patch",
                "description": "retired direct provider path",
                "params": {},
            }],
        )


@pytest.mark.asyncio
async def test_plain_text_ends_the_turn_without_a_completion_judge():
    result = await run_main(FakePorts([AssistantTurn(text="done")]))

    assert result.loop_result.reply == "done"
    assert result.loop_result.completion_status == "ok"
    assert result.task.status is TaskStatus.COMPLETED
    assert result.messages[-1] == {"role": "assistant", "content": "done"}


@pytest.mark.asyncio
async def test_prepare_passes_current_request_to_host_continuation_context():
    fake = FakePorts([AssistantTurn(text='done')])
    ports = fake.build()
    seen = []
    def continuation(text):
        seen.append(text)
        return '[Host context for this request]'
    ports.setup.continuation_context = continuation
    result = await run_main_chat_task(
        config=chat_task_default().with_overrides(checkpoints=False, unified_conversation=True),
        text='Continue with pb10_state', base_system='system', full_tspec=[SPEC],
        convo_tail=[], images=[], is_resume=False, resume_snap=None, ports=ports,
    )
    assert seen == ['Continue with pb10_state']
    prompt = fake.stream_messages[0][-1]['content']
    assert '[Host context for this request]' in prompt
    assert 'Continue with pb10_state' in prompt
    message = fake.stream_messages[0][-1]
    assert message[HOST_CONTEXT_PREFIX_KEY] == len(
        '[Host context for this request]\n\n'
    )
    assert message['content'][message[HOST_CONTEXT_PREFIX_KEY]:] == (
        'Continue with pb10_state'
    )
    assert result.messages[-2][HOST_CONTEXT_PREFIX_KEY] == message[HOST_CONTEXT_PREFIX_KEY]
    from model_runtime.message_graph import (
        build_message_graph,
        render_anthropic_messages,
        render_openai_chat,
        render_openai_responses,
    )
    import json

    graph = build_message_graph(fake.stream_messages[0])
    for wire in (
        render_openai_chat(graph),
        render_openai_responses(graph),
        render_anthropic_messages(graph),
    ):
        encoded = json.dumps(wire)
        assert '[Host context for this request]' in encoded
        assert 'Continue with pb10_state' in encoded
        assert HOST_CONTEXT_PREFIX_KEY not in encoded
    assert fake.action_batches == []  # The context note never enforces a tool call.


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "  \n  "])
async def test_prepare_preserves_note_only_wire_and_tags_the_whole_prefix(text):
    fake = FakePorts([AssistantTurn(text="done")])
    ports = fake.build()
    note = "[Host context for an attachment-only request]"
    ports.setup.continuation_context = lambda _text: note

    await run_main_chat_task(
        config=chat_task_default().with_overrides(
            checkpoints=False,
            unified_conversation=True,
        ),
        text=text,
        base_system="system",
        full_tspec=[SPEC],
        convo_tail=[],
        images=[],
        is_resume=False,
        resume_snap=None,
        ports=ports,
    )

    current = fake.stream_messages[0][-1]
    assert current["content"] == note
    assert current[HOST_CONTEXT_PREFIX_KEY] == len(note)


@pytest.mark.asyncio
async def test_true_resume_does_not_default_legacy_host_extent_revision(tmp_path):
    legacy = new_run_state(
        source="chat",
        title="legacy resume",
        goal="original task",
        run_id="legacy-resume-run",
    )
    legacy.update({
        "status": "in_progress",
        "messages": [
            {"role": "system", "content": "old system"},
            {
                "role": "user",
                "content": "[Host kernel continuation]\nold facts\n\noriginal task",
            },
            {"role": "assistant", "content": "work so far"},
        ],
        "task": {
            "task_id": "legacy-resume-run",
            "goal": "original task",
            "status": "in_progress",
            "model_name": "test-model",
        },
        "main": {"loop": {"route": "model_step", "actions": []}},
        "tools": {"disclosed_names": ["ipython"]},
    })
    assert "host_context_extents_revision" not in legacy
    from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore

    snapshot_store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    snapshot_store.commit_boundary_sync(
        legacy,
        completed_node="loop_init",
        next_node="model_step",
        expected_head_sequence=None,
    )
    boundaries = []

    async def commit(completed, next_node, state):
        boundaries.append((completed, next_node, copy.deepcopy(state)))

    result = await run_main_chat_task(
        config=chat_task_default().with_overrides(checkpoints=True),
        text="original task",
        base_system="system",
        full_tspec=[SPEC],
        convo_tail=[],
        images=[],
        is_resume=True,
        resume_snap=legacy,
        ports=FakePorts([AssistantTurn(text="done")]).build(),
        snapshot_store=snapshot_store,
        commit=commit,
    )
    assert result.loop_result.reply == "done"
    assert boundaries
    assert all(
        "host_context_extents_revision" not in state
        for _completed, _next_node, state in boundaries
    )


@pytest.mark.asyncio
async def test_reasoning_only_terminal_turn_is_not_reclassified_as_failure():
    result = await run_main(FakePorts([
        AssistantTurn(thinking="private reasoning", stop_reason="stop"),
    ]))

    assert result.loop_result.reply == ""
    assert result.loop_result.completion_status == "ok"
    assert result.task.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_reasoning_enabled_main_turn_uses_16000_output_tokens():
    fake = FakePorts(
        [AssistantTurn(text="done")],
        reasoning_enabled=True,
    )

    result = await run_main(fake)

    assert result.loop_result.reply == "done"
    assert fake.stream_max_tokens == [16_000]


@pytest.mark.asyncio
async def test_reasoning_output_reserve_is_clamped_to_half_small_context():
    fake = FakePorts(
        [AssistantTurn(text="done")],
        reasoning_enabled=True,
        context_window=8_192,
    )

    result = await run_main(fake)

    assert result.loop_result.reply == "done"
    assert fake.stream_max_tokens == [4_096]


@pytest.mark.asyncio
async def test_length_continuation_preserves_every_visible_text_fragment():
    fake = FakePorts([
        AssistantTurn(text="first ", stop_reason="length"),
        AssistantTurn(text="second ", stop_reason="length"),
        AssistantTurn(text="third", stop_reason="stop"),
    ])

    result = await run_main(fake)

    assert result.loop_result.reply == "first second third"
    assert result.loop_result.length_recoveries == 2
    assert result.task.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_call_free_length_continues_then_executes_intended_tool_once():
    action = {
        "tool": "ipython",
        "args": {"code": "print('recovered')"},
        "id": "call_recovered",
    }
    batch = ToolBatchResult(
        text="recovered",
        outcomes=[{
            "tool": "ipython",
            "call_id": "call_recovered",
            "result": "recovered",
            "model_result": "recovered",
            "ok": True,
            "executed": True,
        }],
    )
    fake = FakePorts([
        AssistantTurn(thinking="unfinished", stop_reason="length"),
        AssistantTurn(tool_calls=(action,), stop_reason="tool_use"),
        AssistantTurn(text="done"),
    ], [batch])

    result = await run_main(fake)

    assert fake.action_batches == [[action]]
    assert fake.stream_messages[1][-1] == {
        "role": "user",
        "content": (
            "The previous response reached its output limit before the task "
            "was complete. Continue from the current state."
        ),
    }
    assert result.loop_result.reply == "done"
    assert result.loop_result.length_recoveries == 1
    assert result.loop_result.stop_reason == "stop"


@pytest.mark.asyncio
async def test_repeated_call_free_length_finishes_typed_truncated_not_ok():
    fake = FakePorts([
        AssistantTurn(stop_reason="length"),
        AssistantTurn(stop_reason="length"),
        AssistantTurn(stop_reason="length"),
    ])

    result = await run_main(fake)

    assert result.task.status is TaskStatus.FAILED
    assert result.loop_result.completion_status == "truncated"
    assert result.loop_result.stop_reason == "length"
    assert result.loop_result.terminal_reason == "model_output_limit"
    assert result.loop_result.length_recoveries == 2
    assert result.loop_result.reply != "…"
    assert fake.action_batches == []


@pytest.mark.asyncio
async def test_repeated_truncated_tool_calls_share_the_bounded_recovery_counter():
    turns = [
        AssistantTurn(
            text=f"attempt {index} ",
            tool_calls=({
                "tool": "ipython",
                "args": {"code": "print('partial')"},
                "id": f"call_{index}",
            },),
            stop_reason="length",
        )
        for index in range(3)
    ]

    result = await run_main(FakePorts(turns))

    assert result.task.status is TaskStatus.FAILED
    assert result.loop_result.completion_status == "truncated"
    assert result.loop_result.terminal_reason == "model_output_limit"
    assert result.loop_result.length_recoveries == 2
    assert result.loop_result.reply.startswith("attempt 0 attempt 1 attempt 2 ")


@pytest.mark.asyncio
async def test_native_tool_call_runs_then_result_returns_to_same_model():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    batch = ToolBatchResult(
        text="[ipython] applied",
        executed=True,
        outcomes=[{
            "tool": "ipython", "call_id": "call_1",
            "result": "applied", "model_result": "applied",
            "ok": True, "executed": True,
        }],
    )
    fake = FakePorts([
        AssistantTurn(tool_calls=(action,), stop_reason="tool_use"),
        AssistantTurn(text="finished"),
    ], [batch])

    result = await run_main(fake)

    assert fake.action_batches == [[action]]
    assert result.loop_result.reply == "finished"
    assert [message["role"] for message in result.messages[-3:]] == [
        "assistant", "tool", "assistant",
    ]
    assert result.messages[-2]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_main_resume_executes_checkpointed_tool_batch_before_model():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    batch = ToolBatchResult(
        text="applied",
        executed=True,
        outcomes=[{
            "tool": "ipython", "call_id": "call_1",
            "result": "applied", "model_result": "applied",
            "ok": True, "executed": True,
        }],
    )
    resume_snap = {
        "run_id": "resume-main",
        "thread_id": "resume-main",
        "goal": "finish the edit",
        "messages": [
            {"role": "system", "content": "old system"},
            {"role": "user", "content": "edit it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "ipython",
                        "arguments": "{\"code\": \"print('x')\"}",
                    },
                }],
            },
        ],
        "task": {
            "task_id": "resume-main",
            "goal": "finish the edit",
            "status": "in_progress",
            "model_name": "test-model",
        },
        "main": {"loop": {
            "route": "tools",
            "step": 1,
            "actions": [action],
            "turn_disposition": "tools",
            "stop_reason": "tool_use",
            "reply": "",
            "interrupted": False,
        }},
        "tools": {"disclosed_names": ["ipython"]},
    }
    fake = FakePorts([AssistantTurn(text="finished")], [batch])

    result = await run_main(fake, resume_snap=resume_snap)

    assert fake.action_batches == [[action]]
    assert len(fake.stream_messages) == 1
    assert [row["role"] for row in fake.stream_messages[0][-2:]] == [
        "assistant", "tool",
    ]
    assert fake.stream_messages[0][-1]["tool_call_id"] == "call_1"
    assert result.loop_result.reply == "finished"


@pytest.mark.asyncio
async def test_main_resume_rehydrates_desktop_and_browser_before_first_model_step():
    resume_snap = {
        "run_id": "resume-sessions",
        "thread_id": "resume-sessions",
        "goal": "continue in the same windows",
        "messages": [{"role": "user", "content": "continue"}],
        "task": {
            "task_id": "resume-sessions",
            "goal": "continue in the same windows",
            "status": "in_progress",
            "model_name": "test-model",
        },
        "main": {"loop": {"route": "model_step", "step": 1}},
        "tools": {"disclosed_names": []},
        "desktop": {
            "schema": "variant1.desktop-binding.v1",
            "binding_id": "desktop_binding_resume_exact",
            "active_window_id": "win_resume_pad",
            "focus_history": ["win_previous"],
            "owner_kind": "chat",
            "owner_id": "resume-sessions",
        },
        "browser": {
            "schema": "variant1.browser-binding.v1",
            "binding_id": "browser_binding_resume_exact",
            "fabric_session_id": "browser_fabric_resume_exact",
            "owner_kind": "chat",
            "owner_id": "resume-sessions",
            "resume_url": "https://example.test/resume",
        },
    }
    fake = FakePorts(
        [AssistantTurn(text="continued")],
        capture_sessions=True,
    )

    result = await run_main(fake, specs=[SPEC], resume_snap=resume_snap)

    assert result.loop_result.reply == "continued"
    assert fake.session_snapshots
    desktop, browser = fake.session_snapshots[0]
    assert desktop["binding_id"] == "desktop_binding_resume_exact"
    assert desktop["active_window_id"] == "win_resume_pad"
    assert desktop["focus_history"] == ["win_previous"]
    assert browser["binding_id"] == "browser_binding_resume_exact"
    assert browser["fabric_session_id"] == "browser_fabric_resume_exact"
    assert browser["resume_url"] == "https://example.test/resume"


@pytest.mark.asyncio
async def test_steering_is_delivered_after_the_complete_tool_batch():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    batch = ToolBatchResult(
        text="applied",
        executed=True,
        outcomes=[{
            "tool": "ipython", "call_id": "call_1",
            "result": "applied", "model_result": "applied",
            "ok": True, "executed": True,
        }],
    )
    fake = FakePorts(
        [
            AssistantTurn(text="I will edit it.", tool_calls=(action,)),
            AssistantTurn(text="redirected and done"),
        ],
        [batch],
        steering_after_action=[{
            "id": "input_1", "text": "Use the other file instead.",
            "delivery": "steer",
        }],
    )

    result = await run_main(fake)

    roles = [message["role"] for message in fake.stream_messages[1][-3:]]
    assert roles == ["assistant", "tool", "user"]
    assert fake.stream_messages[1][-1]["content"] == "Use the other file instead."
    assert result.loop_result.reply == "redirected and done"


@pytest.mark.asyncio
async def test_follow_up_waits_until_the_loop_would_otherwise_stop():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    batch = ToolBatchResult(
        text="applied",
        outcomes=[{
            "tool": "ipython", "call_id": "call_1",
            "result": "applied", "model_result": "applied",
            "ok": True, "executed": True,
        }],
    )
    fake = FakePorts(
        [
            AssistantTurn(tool_calls=(action,)),
            AssistantTurn(text="first answer"),
            AssistantTurn(text="follow-up answer"),
        ],
        [batch],
        follow_up=[{
            "id": "input_1", "text": "Now summarize it.",
            "delivery": "follow_up",
        }],
    )

    result = await run_main(fake)

    assert not any(
        message.get("content") == "Now summarize it."
        for message in fake.stream_messages[1]
    )
    assert fake.stream_messages[2][-2:] == [
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "Now summarize it."},
    ]
    assert result.loop_result.reply == "follow-up answer"


@pytest.mark.asyncio
async def test_active_input_is_fifo_with_steering_priority_and_one_at_a_time():
    fake = FakePorts(
        [
            AssistantTurn(text="one"),
            AssistantTurn(text="two"),
            AssistantTurn(text="three"),
        ],
        steering=[
            {"id": "s1", "text": "steer one", "delivery": "steer"},
            {"id": "s2", "text": "steer two", "delivery": "steer"},
        ],
        follow_up=[
            {"id": "f1", "text": "follow", "delivery": "follow_up"},
        ],
    )

    result = await run_main(fake)

    delivered = [messages[-1]["content"] for messages in fake.stream_messages]
    assert delivered == ["steer one", "steer two", "follow"]
    assert result.loop_result.reply == "three"


@pytest.mark.asyncio
async def test_main_loop_can_exceed_the_removed_turn_cap():
    cycles = 30
    turns = []
    results = []
    for index in range(cycles):
        call_id = f"call_{index}"
        action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": call_id}
        turns.append(AssistantTurn(tool_calls=(action,)))
        results.append(ToolBatchResult(
            text="ok",
            outcomes=[{
                "tool": "ipython", "call_id": call_id,
                "result": "ok", "model_result": "ok",
                "ok": True, "executed": True,
            }],
        ))
    turns.append(AssistantTurn(text="finished after many turns"))

    result = await run_main(FakePorts(turns, results))

    assert result.loop_result.reply == "finished after many turns"
    assert result.task.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_tool_error_is_observed_once_then_model_decides_next_step():
    action = {"tool": "ipython", "args": {"code": "raise ValueError('bad')"}, "id": "call_err"}
    batch = ToolBatchResult(
        text="[ipython] error: invalid cell",
        had_error=True,
        outcomes=[{
            "tool": "ipython", "call_id": "call_err",
            "result": "invalid patch", "model_result": "invalid patch",
            "ok": False, "executed": True, "error_class": "tool_error",
        }],
    )
    fake = FakePorts([
        AssistantTurn(tool_calls=(action,), stop_reason="tool_use"),
        AssistantTurn(text="I could not apply it."),
    ], [batch])

    result = await run_main(fake)

    assert len(fake.action_batches) == 1
    assert result.loop_result.reply == "I could not apply it."
    assert result.task.status is TaskStatus.COMPLETED
    error_result = next(row for row in result.messages if row.get("role") == "tool")
    assert error_result["is_error"] is True


@pytest.mark.asyncio
async def test_error_and_aborted_stop_reasons_never_execute_attached_calls():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}

    failed = await run_main(FakePorts([
        AssistantTurn(text="provider failed", tool_calls=(action,), stop_reason="error"),
    ]))
    aborted = await run_main(FakePorts([
        AssistantTurn(text="provider aborted", tool_calls=(action,), stop_reason="aborted"),
    ]))

    assert failed.loop_result.completion_status == "failed"
    assert failed.task.status is TaskStatus.FAILED
    assert aborted.loop_result.completion_status == "cancelled"
    assert aborted.loop_result.interrupted is True


@pytest.mark.asyncio
async def test_length_limited_calls_fail_once_per_call_without_execution():
    calls = (
        {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"},
        {"tool": "ipython", "args": {"code": "print('y')"}, "id": "call_2"},
    )
    fake = FakePorts([
        AssistantTurn(tool_calls=calls, stop_reason="length"),
        AssistantTurn(text="recovered"),
    ])

    result = await run_main(fake)

    assert fake.action_batches == []
    tool_messages = [row for row in result.messages if row.get("role") == "tool"]
    assert [row["tool_call_id"] for row in tool_messages[-2:]] == ["call_1", "call_2"]
    assert all("was not executed" in row["content"] for row in tool_messages[-2:])
    assert result.loop_result.reply == "recovered"


@pytest.mark.asyncio
async def test_provider_stream_exception_finishes_as_failed_graph_state():
    result = await run_main(FakePorts([RuntimeError("provider offline")]))

    assert result.task.status is TaskStatus.FAILED
    assert result.loop_result.completion_status == "failed"
    assert "provider offline" in result.loop_result.reply


@pytest.mark.asyncio
async def test_oversized_first_request_is_rejected_before_provider_dispatch():
    fake = FakePorts(
        [],
        approx_prompt_tokens=30_000,
        compress_threshold=11_468,
        context_window=16_384,
        prompt_token_counts=[25_864],
    )

    result = await run_main(fake)

    assert fake.compress_calls == 1
    assert fake.stream_messages == []
    assert result.task.status is TaskStatus.FAILED
    assert result.loop_result.completion_status == "failed"
    assert "25,864 input tokens" in result.loop_result.reply
    assert "readable local file path" in result.loop_result.reply


@pytest.mark.asyncio
async def test_preflight_recounts_successful_compaction_before_dispatch():
    fake = FakePorts(
        [AssistantTurn(text="read the compacted request")],
        approx_prompt_tokens=30_000,
        compress_threshold=11_468,
        context_window=16_384,
        prompt_token_counts=[20_000, 5_000],
        compressed_messages=[
            {"role": "user", "content": "condensed context and current task"},
        ],
    )

    result = await run_main(fake)

    assert fake.compress_calls == 1
    assert len(fake.stream_messages) == 1
    assert fake.stream_messages[0] == [
        {"role": "user", "content": "condensed context and current task"},
    ]
    assert result.loop_result.reply == "read the compacted request"


@pytest.mark.asyncio
async def test_failed_compaction_floor_includes_the_full_provider_envelope():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    fake = FakePorts(
        [AssistantTurn(tool_calls=(action,), stop_reason="tool_use"), AssistantTurn(text="done")],
        [ToolBatchResult(text="x")],
        approx_prompt_tokens=50,
        compress_threshold=96_000,
        prompt_token_counts=[97_842, 97_842],
    )
    result = await run_main(fake)
    assert fake.compress_calls == 1
    assert len(fake.stream_messages) == 2
    assert result.loop_result.reply == "done"


@pytest.mark.asyncio
async def test_client_output_cap_is_truncation_and_never_executes_partial_calls():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    fake = FakePorts([AssistantTurn(tool_calls=(action,), stop_reason="length:client_output_cap"),
                      AssistantTurn(text="recovered")])
    result = await run_main(fake)
    assert not fake.action_batches
    assert result.loop_result.reply == "recovered"


@pytest.mark.asyncio
async def test_cancellation_after_tool_request_keeps_one_result_per_call():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    cancelled = ToolBatchResult(
        text="[ipython] cancelled before execution",
        cancelled=True,
        outcomes=[{
            "tool": "ipython", "call_id": "call_1",
            "result": "cancelled before execution",
            "model_result": "cancelled before execution",
            "ok": False, "executed": False, "status": "cancelled",
        }],
    )
    fake = FakePorts(
        [AssistantTurn(tool_calls=(action,), stop_reason="tool_use")],
        [cancelled],
        stop_after_stream=True,
    )

    result = await run_main(fake)

    assert fake.action_batches == [[action]]
    assert result.loop_result.completion_status == "cancelled"
    assert result.task.status is TaskStatus.FAILED
    assert [row["role"] for row in result.messages[-2:]] == ["assistant", "tool"]
    assert result.messages[-1]["tool_call_id"] == "call_1"
    assert result.messages[-1]["is_error"] is True


@pytest.mark.asyncio
async def test_terminating_tool_batch_stops_unless_queued_input_continues():
    action = {"tool": "ipython", "args": {"code": "print('x')"}, "id": "call_1"}
    terminal_batch = ToolBatchResult(
        text="done",
        executed=True,
        terminate=True,
        outcomes=[{
            "tool": "ipython", "call_id": "call_1",
            "result": "done", "model_result": "done",
            "ok": True, "executed": True, "terminate": True,
        }],
    )
    stopped = FakePorts([
        AssistantTurn(text="Applied.", tool_calls=(action,)),
    ], [terminal_batch])

    stopped_result = await run_main(stopped)

    assert len(stopped.stream_messages) == 1
    assert stopped_result.task.status is TaskStatus.COMPLETED
    assert stopped_result.loop_result.reply == "Applied."

    result_only = FakePorts([
        AssistantTurn(tool_calls=(action,)),
    ], [terminal_batch])

    result_only_result = await run_main(result_only)

    assert result_only_result.task.status is TaskStatus.COMPLETED
    assert result_only_result.loop_result.reply == "done"

    continued = FakePorts(
        [
            AssistantTurn(text="Applied.", tool_calls=(action,)),
            AssistantTurn(text="Here is the requested follow-up."),
        ],
        [terminal_batch],
        follow_up=[{
            "id": "f1", "text": "Summarize that.", "delivery": "follow_up",
        }],
    )

    continued_result = await run_main(continued)

    assert continued.stream_messages[1][-1] == {
        "role": "user", "content": "Summarize that.",
    }
    assert continued_result.loop_result.reply == "Here is the requested follow-up."


def test_main_machine_routes_only_to_native_spine_nodes():
    assert _main_node_for_route("model_step") == "model_step"
    assert _main_node_for_route("tools") == "tool"
    assert _main_node_for_route("finalize") == "main_finalize"
    assert _main_node_for_route("finalize", after_finalize=True) == "finalize"
