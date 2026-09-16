"""Named LLM call profiles for internal (non-user-facing) completions.

Every machinery call whose reply is parsed by code — not read by the user —
must go through ``complete()`` with a profile so per-model quirks (especially
reasoning budgets) live in one place.
"""

from __future__ import annotations

from contextlib import aclosing
from uuid import uuid4

from assistant_turn import AssistantTurn, normalized_stop_reason
from core_invariants import cancellation_is_requested
from llm_stream_diagnostics import StreamDiagnostics

# Machinery profiles use explicit budgets; worker turns inherit the bound
# model route's reasoning setting rather than silently lowering its effort.
PROFILES: dict[str, dict] = {
    # Internal JSON the engine parses (planning, replanning, structured extracts).
    "internal_json":  {"reasoning_budget": 0, "temperature": 0.2, "max_tokens": 500,
                       "json_mode": True},
    # Internal prose such as compression summaries.
    "internal_prose": {"reasoning_budget": 0, "temperature": 0.3, "max_tokens": 1024},
    # Headless worker loop (subagents / automations). The pinned worker surface
    # supplies one IPython action; the final reply is text.
    "agent_turn":     {"reasoning_budget": None, "max_tokens": 1024},
    # VLM calls that must answer in machine-readable form (element grounding).
    "vision":         {"reasoning_budget": 0, "temperature": 0.1, "max_tokens": 700,
                       "json_mode": True},
}


class IncompleteInternalResponseError(RuntimeError):
    """Host machinery must not commit an unfinished provider response."""


async def complete(router, messages: list, *, profile: str,
                   max_tokens: int = None, temperature: float = None,
                   image_b64: str = None, should_stop=None, route: str = "auto",
                   tools: list = None, require_complete: bool = False) -> str:
    """One-shot completion through ``router.stream`` with a named PROFILE.

    A free function (not a Router method) on purpose: call sites receive
    duck-typed/mocked routers via ports. Provider reasoning is discarded;
    visible text is preserved. Returns \"\" when ``should_stop`` fires.

    Agent loops use :func:`complete_turn`; this text-only function never folds
    provider tool calls into prose or private JSON.
    """
    if tools:
        raise TypeError("complete() is text-only; use complete_turn() with tools")
    cfg = PROFILES[profile]
    sampling = {"max_tokens": int(max_tokens or cfg["max_tokens"])}
    temp = temperature if temperature is not None else cfg.get("temperature")
    if temp is not None:
        sampling["temperature"] = temp
    import tool_calling

    diagnostics = StreamDiagnostics()
    accum = tool_calling.ToolCallAccumulator()

    stream_args = {
        "sampling": sampling,
        "json_mode": bool(cfg.get("json_mode")),
        "image_b64": image_b64,
        "reasoning_budget": cfg.get("reasoning_budget"),
        "reasoning_sink": lambda _chunk: None,
        "stream_diagnostics": diagnostics,
        "tool_call_sink": accum,
        "route": route,
        # This is host machinery, not the chat's executable agent turn.  The
        # host still qualifies the selected model route, but must not require
        # the pinned IPython action schema on text-only summaries/classifiers.
        "internal_projection": True,
    }
    if require_complete:
        # A one-off summary cannot extend/replace the durable chat's cache.
        # Transport retries within router.stream keep this identity; a later
        # summary attempt gets a new one. Automatic prefix caching may still
        # share matching bytes, which is outside the harness's control.
        stream_args["prompt_cache_key"] = f"internal:{profile}:{uuid4().hex}"
    async def _consume(args: dict) -> str:
        local_parts: list[str] = []
        async with aclosing(router.stream(messages, **args)) as owned_stream:
            async for tok in owned_stream:
                if cancellation_is_requested(should_stop):
                    return ""
                local_parts.append(tok)
        return "".join(local_parts)

    if cancellation_is_requested(should_stop):
        return ""
    text = await _consume(stream_args)
    if cancellation_is_requested(should_stop):
        return ""
    if require_complete:
        reason = str(diagnostics.finish_reason or "").strip().lower()
        if accum.actions() or reason not in {"stop", "end_turn", "completed", "stop_sequence", "eos_token"}:
            raise IncompleteInternalResponseError(
                "Internal response not committed: "
                + ("unexpected_tool_call" if accum.actions() else reason or "missing_terminal_reason")
            )

    return text.strip()


async def complete_turn(
    router,
    messages: list,
    *,
    profile: str = "agent_turn",
    max_tokens: int = None,
    should_stop=None,
    route: str = "auto",
    tools: list = None,
) -> AssistantTurn:
    """Return one typed assistant turn for the IPython agent loop."""
    import tool_calling

    cfg = PROFILES[profile]
    sampling = {"max_tokens": int(max_tokens or cfg["max_tokens"])}
    temp = cfg.get("temperature")
    if temp is not None:
        sampling["temperature"] = temp
    tool_specs = list(tools or [])
    accum = tool_calling.ToolCallAccumulator()
    thinking: list[str] = []
    diagnostics = StreamDiagnostics()
    stream_args = {
        "sampling": sampling,
        "json_mode": False,
        "reasoning_budget": cfg.get("reasoning_budget"),
        "reasoning_sink": thinking.append,
        "tools": tool_specs or None,
        "tool_call_sink": accum if tool_specs else None,
        "stream_diagnostics": diagnostics,
        "route": route,
    }

    text_parts: list[str] = []
    async with aclosing(router.stream(messages, **stream_args)) as owned_stream:
        async for token in owned_stream:
            if cancellation_is_requested(should_stop):
                return AssistantTurn(stop_reason="aborted")
            text_parts.append(token)

    actions = accum.actions()
    if actions:
        print(
            f"[tools] provider tool_calls profile={profile} n={len(actions)} "
            f"names={[a.get('tool') for a in actions]}",
            flush=True,
        )
    return AssistantTurn(
        text="".join(text_parts),
        thinking="".join(thinking),
        tool_calls=tuple(actions),
        stop_reason=normalized_stop_reason(
            diagnostics.finish_reason,
            has_tool_calls=bool(actions),
        ),
    )
