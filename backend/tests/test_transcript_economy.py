"""Context economy for native role:tool observations."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from observability import context_lineage
from run_context import Variant1RunContext, bind_run_context
import server
import transcript_economy as te
from transcript_service import TranscriptService
from work_fabric.scope import WorkScope


def _complete_recap(
    *,
    goal: str = "Continue the active task.",
    confirmed: str = "Evidence retained.",
    pending: str = "Continue from the retained state.",
    failed: str = "None observed.",
    exact: str = "Use the original request and current host contracts.",
) -> str:
    return (
        f"## Goal\n{goal}\n"
        f"## Confirmed Results\n{confirmed}\n"
        f"## Pending Work\n{pending}\n"
        f"## Failed Attempts (not blockers)\n{failed}\n"
        f"## Exact Context\n{exact}\n"
        f"{te.COMPACT_SUMMARY_SENTINEL}"
    )


def _compressible_messages() -> list[dict]:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Complete the active task."},
    ]
    for index in range(6):
        messages.extend((
            {"role": "assistant", "content": f"attempt {index}"},
            {"role": "user", "content": f"observation {index}"},
        ))
    return messages


@pytest.mark.asyncio
async def test_failed_compaction_cools_down_across_growing_suffix_and_recovers(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(te.time, "monotonic", lambda: now[0])
    state = te.CompactionRetryState()
    messages = _compressible_messages()
    original = copy.deepcopy(messages)
    calls = []

    async def complete(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "truncated" if len(calls) == 1 else _complete_recap()

    assert await te.compress_messages(messages, complete=complete, retry_state=state) is messages
    assert len(calls) == 1 and state.failures == 1 and state.retry_after == 160
    assert len(state.failed_fingerprint) == 64
    assert calls[0][1]["require_complete"] is True
    assert await te.compress_messages(messages, complete=complete, retry_state=state) is messages
    growing = messages + [{"role": "assistant", "content": "another observation"}]
    now[0] = 159
    assert await te.compress_messages(growing, complete=complete, retry_state=state) is growing
    assert len(calls) == 1
    now[0] = 160
    result = await te.compress_messages(growing, complete=complete, retry_state=state)
    assert len(result) < len(growing) and len(calls) == 2
    assert state.failures == 0 and state.failed_fingerprint == ""
    assert messages == original


@pytest.mark.asyncio
async def test_compaction_abort_preserves_canonical_state_without_failure_backoff():
    state = te.CompactionRetryState()
    messages = _compressible_messages()

    async def complete(*_args, **_kwargs):
        return ""

    assert await te.compress_messages(messages, complete=complete, retry_state=state,
                                      should_stop=lambda: True) is messages
    assert state.failures == 0


def test_compaction_backoff_increases_and_is_bounded(monkeypatch):
    monkeypatch.setattr(te.time, "monotonic", lambda: 100)
    state = te.CompactionRetryState()
    delays = []
    for _ in range(7):
        state.failed("same-fingerprint")
        delays.append(state.retry_after - 100)
    assert delays == [60, 120, 240, 480, 900, 900, 900]


@pytest.mark.asyncio
async def test_compaction_service_isolates_failure_backoff_by_chat_and_model(monkeypatch):
    calls = []
    router = SimpleNamespace(mode="cloud", cloud_provider="test", model="model-a")
    router.active_model_name = lambda _mode: router.model
    service = TranscriptService(SimpleNamespace(router=router))

    async def failing(*_args, **_kwargs):
        calls.append(router.model)
        return "incomplete recap"

    monkeypatch.setattr("llm_router.complete", failing)
    messages = _compressible_messages()
    for chat in ["a", "a", "b"]:
        context = Variant1RunContext.create(source="chat", work_scope=WorkScope(chat_id=chat))
        with bind_run_context(context):
            assert await service.compress_messages(messages) is messages
    assert calls == ["model-a", "model-a"]
    router.model = "model-b"
    context = Variant1RunContext.create(source="chat", work_scope=WorkScope(chat_id="a"))
    with bind_run_context(context):
        assert await service.compress_messages(messages) is messages
    assert calls == ["model-a", "model-a", "model-b"]


@pytest.mark.asyncio
async def test_identical_failed_projection_has_one_delayed_retry_then_stays_suppressed(monkeypatch):
    from llm_usage import current_usage_category

    now = [1.0]
    monkeypatch.setattr(te.time, "monotonic", lambda: now[0])
    state = te.CompactionRetryState()
    messages = _compressible_messages()
    calls = []

    async def failed(*_args, **_kwargs):
        calls.append(current_usage_category())
        return "bad summary"

    assert await te.compress_messages(messages, complete=failed, retry_state=state) is messages
    now[0] = state.retry_after
    assert await te.compress_messages(messages, complete=failed, retry_state=state) is messages
    now[0] = state.retry_after + 3600
    assert await te.compress_messages(messages, complete=failed, retry_state=state) is messages
    assert calls == ["compaction", "compaction"]
    assert current_usage_category() == "agent"
    # Changed assistant evidence can try again after cooldown; the frozen failed
    # prefix does not impose a permanent block on later progress.
    changed = copy.deepcopy(messages)
    changed[2]["content"] = "new evidence"
    await te.compress_messages(changed, complete=failed, retry_state=state)
    assert len(calls) == 3


def test_output_reserve_never_consumes_more_than_half_a_known_window():
    assert te.clamp_output_reserve(8_192, 16_000) == 4_096
    assert te.clamp_output_reserve(16_384, 16_000) == 8_192
    assert te.clamp_output_reserve(32_768, 16_000) == 16_000
    assert te.clamp_output_reserve(0, 16_000) == 16_000


@pytest.mark.asyncio
async def test_count_prompt_tokens_prefers_rendered_request_with_tools():
    captured = {}

    async def rendered(messages, *, tools):
        captured["messages"] = messages
        captured["tools"] = tools
        return 321

    async def unexpected(_text):
        raise AssertionError("flat fallback should not run")

    tools = [{"name": "read_file", "params": {}}]
    with patch.object(server.APP.router, "count_prompt_tokens", new=rendered), \
            patch.object(server.APP.router, "count_tokens", new=unexpected):
        count = await server.APP.require_runtime().chat.count_prompt_tokens(
            [{"role": "user", "content": "hello"}], tools=tools)

    assert count == 321
    assert captured["tools"] == tools


@pytest.mark.asyncio
async def test_count_prompt_tokens_falls_back_with_message_framing():
    async def unavailable(_messages, *, tools):
        return None

    async def count(text):
        assert "hello" in text
        return 100

    with patch.object(server.APP.router, "count_prompt_tokens", new=unavailable), \
            patch.object(server.APP.router, "count_tokens", new=count):
        result = await server.APP.require_runtime().chat.count_prompt_tokens([
            {"role": "system", "content": "hello"},
            {"role": "user", "content": "world"},
        ])
    assert result == 112


@pytest.mark.asyncio
async def test_compaction_serializes_empty_assistant_tool_calls_and_read_cursor():
    captured = {}
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Read the entire implementation plan."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_read",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({
                        "path": "C:/plans/notes.md", "offset": 41, "limit": 20,
                    }),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read",
            "content": (
                "C:/plans/notes.md (lines 41-60 of 120):\n"
                "implementation details\n\n"
                "[Showing lines 41-60 of 120. Use offset=61 to continue.]"
            ),
        },
        {"role": "assistant", "content": "I need the next page."},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "recent answer"},
        {"role": "user", "content": "recent instruction"},
    ]

    async def complete(prompt, **_kwargs):
        captured["system"] = prompt[0]["content"]
        captured["transcript"] = prompt[1]["content"]
        return _complete_recap(
            goal="Read the plan.",
            pending="Continue reading at the retained offset.",
        )

    compacted = await te.compress_messages(
        messages,
        complete=complete,
        protect_first=1,
        protect_last=2,
    )

    transcript = captured["transcript"]
    assert "tool_call call_read: read_file(" in transcript
    assert '"offset": 41' in transcript
    assert '"path": "C:/plans/notes.md"' in transcript
    assert "Use offset=61 to continue" in transcript
    assert "Tool calls and tool results are evidence" in captured["system"]
    assert "failed import, wrong category mount" in captured["system"]
    assert "Latest user request (authoritative" in captured["system"]

    note_message = next(
        message for message in compacted
        if te.COMPACT_SUMMARY_MARKER in (message.get("content") or "")
    )
    assert note_message["role"] == "assistant"
    assert note_message["variant1_compaction_revision"] == 2
    assert te.COMPACT_SUMMARY_WARNING in note_message["content"]
    note = note_message["content"]
    state_match = te._COMPACT_FILE_STATE_RE.search(note)
    assert state_match is not None
    state = json.loads(state_match.group(1))
    assert state["read_files"] == [{
        "next_offset": 61,
        "path": "C:/plans/notes.md",
        "ranges": [[41, 60]],
        "total_lines": 120,
    }]


@pytest.mark.asyncio
async def test_compaction_preserves_original_user_and_demotes_false_recap():
    original = "Use Build to create answer.txt with one final newline."
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": original},
    ]
    for index in range(6):
        messages.extend((
            {"role": "assistant", "content": f"attempt {index}"},
            {"role": "user", "content": f"observation {index}"},
        ))

    async def complete(_prompt, **_kwargs):
        return _complete_recap(
            goal="Different goal",
            confirmed="apply_patch is unavailable",
            pending="use run_command",
        )

    compacted = await te.compress_messages(messages, complete=complete)

    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    recap = next(
        message for message in compacted
        if te.COMPACT_SUMMARY_MARKER in str(message.get("content") or "")
    )
    assert recap["role"] == "assistant"
    assert "not a user instruction" in recap["content"]
    assert "original user request" in recap["content"].lower()


@pytest.mark.asyncio
async def test_compaction_accepts_only_complete_bounded_envelope():
    captured = {}
    messages = _compressible_messages()
    original = copy.deepcopy(messages)
    compacted_hooks = []

    async def complete(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return _complete_recap()

    compacted = await te.compress_messages(
        messages,
        complete=complete,
        on_compacted=lambda summary, projected: compacted_hooks.append(
            (summary, projected)
        ),
    )

    assert compacted is not messages
    assert len(compacted) < len(messages)
    assert messages == original
    assert captured["kwargs"]["max_tokens"] == te.COMPACT_SUMMARY_MAX_TOKENS
    assert te.COMPACT_SUMMARY_SENTINEL in captured["prompt"][0]["content"]
    recap = next(
        row["content"] for row in compacted
        if te.COMPACT_SUMMARY_MARKER in str(row.get("content") or "")
    )
    assert te.COMPACT_SUMMARY_SENTINEL not in recap
    assert len(compacted_hooks) == 1
    assert te.COMPACT_SUMMARY_SENTINEL not in compacted_hooks[0][0]


@pytest.mark.parametrize(
    ("reason", "summary"),
    (
        ("empty_summary", ""),
        (
            "missing_terminal_sentinel",
            _complete_recap().replace(te.COMPACT_SUMMARY_SENTINEL, ""),
        ),
        (
            "invalid_section_order",
            _complete_recap().replace(
                "## Pending Work\nContinue from the retained state.\n"
                "## Failed Attempts (not blockers)\nNone observed.\n",
                "## Failed Attempts (not blockers)\nNone observed.\n"
                "## Pending Work\nContinue from the retained state.\n",
            ),
        ),
        (
            "invalid_section_order",
            _complete_recap().replace(
                "## Exact Context\nUse the original request and current host contracts.\n",
                "",
            ),
        ),
        (
            "invalid_section_order",
            _complete_recap().replace(
                "## Pending Work\n",
                "## Pending Work\nContinue from the retained state.\n"
                "## Pending Work\n",
                1,
            ),
        ),
        (
            "empty_section",
            _complete_recap(confirmed=""),
        ),
        (
            "summary_preamble_present",
            "Here is the recap.\n" + _complete_recap(),
        ),
        (
            "duplicate_terminal_sentinel",
            _complete_recap() + "\n" + te.COMPACT_SUMMARY_SENTINEL,
        ),
        (
            "summary_too_large",
            _complete_recap(exact="x" * te.COMPACT_SUMMARY_MAX_CHARS),
        ),
    ),
)
@pytest.mark.asyncio
async def test_compaction_rejects_unusable_replacement_without_mutating_source(
    reason,
    summary,
    capsys,
):
    messages = _compressible_messages()
    receipt = context_lineage.new_receipt("main_chat_step")
    context_lineage.attach_to_messages(messages, receipt)
    original = copy.deepcopy(messages)
    compacted_hooks = []

    async def complete(_prompt, **_kwargs):
        return summary

    result = await te.compress_messages(
        messages,
        complete=complete,
        on_compacted=lambda recap, projected: compacted_hooks.append(
            (recap, projected)
        ),
    )

    assert result is messages
    assert messages == original
    assert compacted_hooks == []
    assert reason in capsys.readouterr().out


@pytest.mark.asyncio
async def test_mid_turn_compaction_pins_latest_user_beyond_fixed_tail():
    current = "Create the requested artifact and verify its checksum."
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": current},
    ]
    for index in range(8):
        messages.extend((
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call_{index}", "type": "function",
                "function": {"name": "ipython", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": f"call_{index}",
             "content": f"result {index}"},
        ))

    captured = {}

    async def complete(prompt, **_kwargs):
        captured["system"] = prompt[0]["content"]
        captured["transcript"] = prompt[1]["content"]
        return _complete_recap()

    compacted = await te.compress_messages(
        messages, complete=complete, protect_first=2, protect_last=6,
    )

    assert {"role": "user", "content": current} in compacted
    current_index = compacted.index({"role": "user", "content": current})
    assert current_index == 2
    assert compacted[current_index + 1]["role"] == "assistant"
    assert te.COMPACT_SUMMARY_MARKER in compacted[current_index + 1]["content"]
    assert compacted[-6:] == messages[-6:]
    assert len(compacted) < len(messages)
    assert current in captured["system"]
    assert "result 0" in captured["transcript"]


@pytest.mark.asyncio
async def test_long_single_user_turn_compacts_old_actions_without_losing_request():
    request = "Review the repository and report every concrete finding."
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": request},
    ]
    for index in range(12):
        messages.extend((
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call_{index}", "type": "function",
                "function": {
                    "name": "ipython",
                    "arguments": json.dumps({"code": f"print({index})"}),
                },
            }]},
            {"role": "tool", "tool_call_id": f"call_{index}",
             "content": f"finding evidence {index}"},
        ))

    async def complete(_prompt, **_kwargs):
        return _complete_recap(goal="Review the repository.")

    compacted = await te.compress_messages(
        messages, complete=complete, protect_first=2, protect_last=6,
    )

    assert compacted[:2] == messages[:2]
    assert compacted[-6:] == messages[-6:]
    assert len(compacted) < len(messages)
    assert any(
        te.COMPACT_SUMMARY_MARKER in str(message.get("content") or "")
        for message in compacted
    )


@pytest.mark.asyncio
async def test_recursive_compaction_preserves_all_user_intent_and_tool_protocol():
    long_constraint = " Keep this exact constraint." * 240
    requests = [
        (
            "Write every deliverable to C:/requested/work, never the application "
            "directory." + long_constraint + " <setup-end>"
        ),
        "Change the output name to final.json and include only the east region.",
        "Finalize the retained result and verify the delivered files.",
    ]
    messages = [{"role": "system", "content": "host contract"}]
    captured_prompts = []

    async def lossy_recap(prompt, **_kwargs):
        captured_prompts.append(copy.deepcopy(prompt))
        return _complete_recap(
            goal="Finish work.",
            confirmed="Python state is live.",
            pending="Write output.",
            exact="Current working directory is C:/app.",
        )

    def user_messages(rows):
        return [row for row in rows if row.get("role") == "user"]

    def assert_protocol_pairs(rows):
        calls = {}
        results = {}
        for index, row in enumerate(rows):
            for call in row.get("tool_calls") or []:
                calls[call["id"]] = index
            if row.get("role") == "tool":
                results[row["tool_call_id"]] = index
        assert calls.keys() == results.keys()
        assert all(calls[call_id] < results[call_id] for call_id in calls)

    for turn, request in enumerate(requests):
        messages.append({
            "role": "user",
            "content": request,
            "request_revision": turn + 1,
        })
        for step in range(8):
            call_id = f"call_{turn}_{step}"
            messages.extend((
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "ipython",
                            "arguments": '{"code":"print(state)"}',
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "state retained; cwd=C:/app",
                },
            ))
        source = copy.deepcopy(messages)
        compacted = await te.compress_messages(messages, complete=lossy_recap)

        assert messages == source
        assert compacted != source
        assert user_messages(compacted) == [
            {
                "role": "user",
                "content": text,
                "request_revision": index + 1,
            }
            for index, text in enumerate(requests[:turn + 1])
        ]
        assert_protocol_pairs(compacted)
        assert request in captured_prompts[-1][0]["content"]
        assert request not in captured_prompts[-1][1]["content"]
        assert all(
            row.get("role") == "assistant"
            and row.get("variant1_compaction_revision") == 2
            for row in compacted
            if te.COMPACT_SUMMARY_MARKER in str(row.get("content") or "")
        )
        messages = compacted

    assert requests[0] in [row["content"] for row in user_messages(messages)]
    assert requests[1] in [row["content"] for row in user_messages(messages)]
    assert requests[2] in [row["content"] for row in user_messages(messages)]


@pytest.mark.asyncio
async def test_transcript_service_refreshes_current_mount_without_progress_poison(
    monkeypatch,
):
    observed = {}

    class Catalog:
        def runtime_prompt(self, chat_id, query, *, allow_auto_mount=True):
            observed.update(
                chat_id=chat_id, query=query,
                allow_auto_mount=allow_auto_mount,
            )
            return "## Working environment\nCURRENT OPERATE MOUNT"

    catalog = Catalog()
    host = SimpleNamespace(
        router=object(),
        require_runtime=lambda: SimpleNamespace(
            catalog=catalog,
            goals=SimpleNamespace(get=lambda _goal_id: None),
        ),
    )
    service = TranscriptService(host)

    async def complete(_router, _prompt, **_kwargs):
        return _complete_recap(goal="keep task", pending="continue")

    monkeypatch.setattr("llm_router.complete", complete)
    original = "Use Operate, then Build, and create answer.txt."
    messages = [
        {
            "role": "system",
            "content": "IDENTITY\n\n## Working environment\nSTALE BASE MOUNT",
        },
        {"role": "user", "content": original},
    ]
    for index in range(4):
        messages.extend((
            {"role": "assistant", "content": f"attempt {index}"},
            {"role": "user", "content": f"result {index}"},
        ))
    context = Variant1RunContext.create(
        source="chat",
        work_scope=WorkScope(chat_id="chat-refresh"),
    )

    with bind_run_context(context):
        compacted = await service.compress_messages(
            messages, protect_last=2,
        )

    assert observed == {
        "chat_id": "chat-refresh", "query": "",
        "allow_auto_mount": False,
    }
    assert compacted[0]["content"] == (
        "IDENTITY\n\n## Working environment\nCURRENT OPERATE MOUNT"
    )
    assert compacted[1] == {"role": "user", "content": original}
    assert any(
        message.get("role") == "assistant"
        and te.COMPACT_SUMMARY_MARKER in str(message.get("content") or "")
        for message in compacted
    )


def test_compaction_file_state_merges_ranges_across_compactions():
    prior = {
        "read_files": [{
            "path": "C:/plans/notes.md",
            "ranges": [[1, 20]],
            "total_lines": 60,
            "next_offset": 21,
        }],
        "modified_files": [],
    }
    messages = [
        {
            "role": "user",
            "content": (
                f"{te.COMPACT_SUMMARY_MARKER}\nold summary\n\n"
                f"{te.COMPACT_FILE_STATE_OPEN}\n"
                f"{json.dumps(prior)}\n"
                f"{te.COMPACT_FILE_STATE_CLOSE}"
            ),
        },
        {
            "role": "tool",
            "tool_call_id": "next",
            "content": (
                "C:/plans/notes.md (lines 21-40 of 60):\nnext page\n\n"
                "[Showing lines 21-40 of 60. Use offset=41 to continue.]"
            ),
        },
        {
            "role": "tool",
            "tool_call_id": "duplicate",
            "content": (
                "C:/plans/notes.md (lines 1-10 of 60):\nrepeated page\n\n"
                "[Showing lines 1-10 of 60. Use offset=11 to continue.]"
            ),
        },
    ]

    state = te._collect_file_state(messages)

    assert state["read_files"] == [{
        "path": "C:/plans/notes.md",
        "ranges": [[1, 40]],
        "total_lines": 60,
        "next_offset": 41,
    }]
