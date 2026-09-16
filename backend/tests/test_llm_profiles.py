"""llm_router.complete — the LLM call-profile layer (harness slice 4).

Every internal machinery call (reply parsed by code, not read by the user)
goes through a named profile so per-model quirks — reasoning suppression,
JSON grammar, and budgets — live in one place across classifiers, summarizers,
planning, and vision calls.
"""

from __future__ import annotations

import pytest

import llm_router
from llm_profiles import PROFILES


class FakeRouter:
    def __init__(self, tokens=("ok",), *, mode="cloud", tool_deltas=None):
        self.tokens = list(tokens)
        self.mode = mode
        self.tool_deltas = list(tool_deltas or [])
        self.calls = []

    async def stream(self, messages, sampling=None, json_mode=False,
                     image_b64=None, reasoning_budget=None, reasoning_sink=None,
                     tools=None, tool_call_sink=None, **kwargs):
        self.calls.append({"messages": messages, "sampling": sampling,
                           "json_mode": json_mode, "image_b64": image_b64,
                           "reasoning_budget": reasoning_budget,
                           "reasoning_sink": reasoning_sink,
                           "tools": tools, "tool_call_sink": tool_call_sink,
                           **kwargs})
        if reasoning_sink:
            reasoning_sink("private thoughts")
        if tool_call_sink is not None and self.tool_deltas:
            for delta in self.tool_deltas:
                tool_call_sink.add_openai_delta(delta)
        for t in self.tokens:
            yield t


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expect_json", [
    ("internal_json", True),
    ("internal_prose", False),
    ("agent_turn", False),
    ("vision", True),
])
async def test_only_machinery_profiles_suppress_reasoning(profile, expect_json):
    r = FakeRouter()
    out = await llm_router.complete(r, [{"role": "user", "content": "x"}], profile=profile)
    call = r.calls[0]
    assert call["reasoning_budget"] == (None if profile == "agent_turn" else 0)
    assert callable(call["reasoning_sink"])
    assert call["json_mode"] is expect_json
    assert call["sampling"]["max_tokens"] == PROFILES[profile]["max_tokens"]
    assert call["internal_projection"] is True
    assert out == "ok"
    assert "private thoughts" not in out            # sink content never leaks


@pytest.mark.asyncio
async def test_overrides_beat_profile_defaults_without_reply_rewriting():
    r = FakeRouter(tokens=("<think>hm</think>", "TASK"))
    out = await llm_router.complete(r, [{"role": "user", "content": "x"}],
                                    profile="internal_prose", max_tokens=8, temperature=0.5)
    assert out == "<think>hm</think>TASK"
    assert r.calls[0]["sampling"] == {"max_tokens": 8, "temperature": 0.5}


@pytest.mark.asyncio
async def test_should_stop_aborts_with_empty_string():
    r = FakeRouter(tokens=("a", "b", "c"))
    out = await llm_router.complete(r, [{"role": "user", "content": "x"}],
                                    profile="internal_prose", should_stop=lambda: True)
    assert out == ""


@pytest.mark.asyncio
async def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        await llm_router.complete(FakeRouter(), [], profile="nope")


@pytest.mark.asyncio
async def test_image_is_forwarded_for_vision_profile():
    r = FakeRouter()
    await llm_router.complete(r, [{"role": "user", "content": "x"}],
                              profile="vision", image_b64="PNGDATA")
    assert r.calls[0]["image_b64"] == "PNGDATA"


@pytest.mark.asyncio
async def test_agent_turn_with_tools_uses_native_calling():
    deltas = [[
        {"index": 0, "id": "call_x",
         "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
    ]]
    r = FakeRouter(tokens=("working",), tool_deltas=deltas)
    specs = [{"name": "read_file", "description": "read", "params": {
        "path": {"type": "string", "required": True}}}]
    out = await llm_router.complete_turn(
        r, [{"role": "user", "content": "x"}],
        profile="agent_turn", tools=specs)
    call = r.calls[0]
    assert call["json_mode"] is False
    assert call["tools"] == specs
    assert call["tool_call_sink"] is not None
    assert call["reasoning_budget"] is None
    assert "internal_projection" not in call
    assert out.text == "working"
    assert out.thinking == "private thoughts"
    assert out.actions == [{"tool": "read_file", "args": {"path": "a.txt"}, "id": "call_x"}]
    assert out.stop_reason == "tool_use"


@pytest.mark.asyncio
async def test_agent_turn_without_tools_uses_direct_answer_mode():
    r = FakeRouter()
    turn = await llm_router.complete_turn(
        r, [{"role": "user", "content": "x"}], profile="agent_turn")
    assert r.calls[0]["json_mode"] is False
    assert r.calls[0]["tools"] is None
    assert turn.text == "ok"


@pytest.mark.asyncio
async def test_text_completion_rejects_tool_schemas():
    with pytest.raises(TypeError, match="complete_turn"):
        await llm_router.complete(
            FakeRouter(), [], profile="agent_turn",
            tools=[{"name": "x", "params": {}}],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["", "length", "length:client_output_cap", "incomplete:max_output_tokens", "error", "SAFETY"])
async def test_compaction_rejects_unfinished_response_even_with_sentinel(finish):
    from llm_profiles import IncompleteInternalResponseError

    class Router(FakeRouter):
        async def stream(self, messages, **kwargs):
            kwargs["stream_diagnostics"].note_finish_reason(finish)
            yield "<variant1-recap-complete/>"

    with pytest.raises(IncompleteInternalResponseError):
        await llm_router.complete(Router(), [], profile="internal_prose", require_complete=True)


@pytest.mark.asyncio
async def test_compaction_cache_identity_is_separate_and_complete_usage_can_settle():
    class Router(FakeRouter):
        async def stream(self, messages, **kwargs):
            self.calls.append(kwargs)
            yield "complete recap"
            kwargs["stream_diagnostics"].note_finish_reason("completed")

    router = Router()
    for _ in range(2):
        assert await llm_router.complete(router, [], profile="internal_prose", require_complete=True) == "complete recap"
    assert router.calls[0]["prompt_cache_key"].startswith("internal:internal_prose:")
    assert router.calls[0]["prompt_cache_key"] != router.calls[1]["prompt_cache_key"]
    assert all(c["reasoning_budget"] == 0 for c in router.calls)


@pytest.mark.asyncio
async def test_compaction_rejects_unsolicited_tools_even_with_completed_status():
    from llm_profiles import IncompleteInternalResponseError

    class Router(FakeRouter):
        async def stream(self, messages, **kwargs):
            kwargs["tool_call_sink"].add_openai_delta([{
                "index": 0, "id": "call_1", "function": {"name": "ipython", "arguments": "{}"},
            }])
            kwargs["stream_diagnostics"].note_finish_reason("completed")
            yield "recap"

    with pytest.raises(IncompleteInternalResponseError, match="unexpected_tool_call"):
        await llm_router.complete(Router(), [], profile="internal_prose", require_complete=True)
