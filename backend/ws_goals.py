"""Correlated WebSocket commands for chat goals (one standing goal per chat)."""

from __future__ import annotations

import asyncio
from typing import Any

from ws_protocol import CorrelatedResponder, request_id as _request_id


def _goals(srv: Any):
    goals = getattr(srv.require_runtime(), "goals", None)
    if goals is None:
        raise RuntimeError("Goal service is unavailable")
    return goals


def _chat_id(msg) -> str:
    chat_id = str(msg.get("session_id") or "").strip()
    if not chat_id:
        raise ValueError("session_id is required")
    return chat_id


def _version(msg) -> int | None:
    value = msg.get("expected_version")
    if value is None or value == "":
        return None
    return int(value)


_goal_responder = CorrelatedResponder(family="goal", schema="variant1.goal-command.v2")


async def _respond(websocket, msg, operation, action, *, mutation=None):
    class ScopedReply:
        async def send_json(self, value):
            value = {**value, "session_id": str(msg.get("session_id") or "")}
            if operation == "current" and value["type"] == "goal:accepted":
                value["type"] = "goal:current"
            await websocket.send_json(value)

    await _goal_responder(ScopedReply(), msg, operation, action, mutation=mutation)


def register(on):
    @on("goal:submit")
    async def goal_submit(srv, websocket, session, msg):
        async def action():
            goal = await _goals(srv).submit(
                _chat_id(msg), _request_id(msg), msg.get("objective"),
                replace=msg.get("replace") is True)
            return {"goal": goal}

        await _respond(websocket, msg, "submit", action)

    @on("goal:current:get")
    async def goal_current(srv, websocket, session, msg):
        async def action():
            return {"goal": await asyncio.to_thread(_goals(srv).current, _chat_id(msg))}

        await _respond(websocket, msg, "current", action, mutation=False)

    @on("goal:control")
    async def goal_control(srv, websocket, session, msg):
        async def action():
            goal = await _goals(srv).control(
                _chat_id(msg), str(msg.get("goal_id") or ""), str(msg.get("action") or ""),
                expected_version=_version(msg), objective=str(msg.get("objective") or ""))
            return {"goal": goal}

        await _respond(websocket, msg, "control", action)
