"""``session.get_goal`` / ``create_goal`` / ``update_goal`` for the model."""
from __future__ import annotations

import asyncio
from typing import Any

from tools import ToolError

from .store import GoalError

GOAL_METHODS = (
    {
        "name": "get_goal",
        "description": ("Return this chat's goal: objective, status, tokens used, token budget "
                        "and remaining tokens. Returns goal=None when the chat has no goal."),
        "effect_class": "read",
        "params": {},
    },
    {
        "name": "create_goal",
        "description": ("Create a standing goal for this chat. Only when the user explicitly asks "
                        "for a goal; never infer one from an ordinary task. Set token_budget only "
                        "when the user asks for a budget. Fails while an unfinished goal exists."),
        "effect_class": "external_side_effect",
        "params": {
            "objective": {"type": "string", "required": True, "maxLength": 32000},
            "token_budget": {"type": "integer", "required": False},
        },
    },
    {
        "name": "update_goal",
        "description": (
            "Set this chat's goal status. complete: only after checking every requirement "
            "against current evidence. blocked: only when the same blocker has stopped "
            "you for three goal turns in a row. paused: only when the user explicitly asks. "
            "You can't resume a goal or lift its budget."),
        "effect_class": "external_side_effect",
        "params": {
            "status": {"type": "string", "required": True,
                       "enum": ["complete", "blocked", "paused"]},
        },
    },
)


def goal_handlers(host_getter) -> dict[str, Any]:
    """Handlers that read the host at call time; the catalog may get it later."""
    from capability_broker import current_capability_invocation

    def service():
        host = host_getter()
        goals = getattr(host.require_runtime(), "goals", None) if host is not None else None
        if goals is None:
            raise ToolError("Goals are unavailable")
        return goals

    def invocation():
        context = current_capability_invocation()
        if context is None or not context.chat_id:
            raise ToolError("Goal tools need an admitted chat cell")
        return context

    async def call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except GoalError as exc:
            raise ToolError(str(exc)) from exc

    async def get_goal(_args: dict[str, Any]):
        return await call(service().tool_get, invocation().chat_id)

    async def create_goal(args: dict[str, Any]):
        return await call(service().tool_create, invocation().chat_id,
                          args.get("objective"), args.get("token_budget"))

    async def update_goal(args: dict[str, Any]):
        context = invocation()
        return await call(service().tool_update, context.chat_id, args.get("status"),
                          context=context)

    return {"get_goal": get_goal, "create_goal": create_goal, "update_goal": update_goal}
