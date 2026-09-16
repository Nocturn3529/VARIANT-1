"""Explicit product-command stage for an interactive chat turn."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chat_routes import default_routes
from chat_setup_stage import PreparedChatTurn
from chat_stage_result import ChatStageContinue, ChatStageDone, ChatStageResult

if TYPE_CHECKING:
    from chat_pipeline import ChatPorts


async def run_chat_routes_stage(
    ports: "ChatPorts",
    websocket,
    session,
    prepared: PreparedChatTurn,
    *,
    reserved: bool,
) -> ChatStageResult[PreparedChatTurn]:
    """Give slash/product routes first refusal, then continue ordinary chat."""
    for route in default_routes():
        decision = await route.before_intent(
            ports,
            websocket,
            session,
            prepared.text,
            is_resume=prepared.is_resume,
            resume_state=prepared.plan.resume.resume_state,
            reserved=reserved,
        )
        if decision.handled:
            return ChatStageDone(release_busy=not reserved)
    return ChatStageContinue(prepared)
