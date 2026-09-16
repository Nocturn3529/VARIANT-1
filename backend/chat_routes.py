"""Chat turn interception plugins.

The WebSocket chat pipeline owns readiness, resume, context/tool discovery,
and the unified native conversation path. Product-specific interception
registers as ``ChatRoute`` plugins so ``handle_chat`` stays a
linear shell:

  readiness → resume → *before_intent hooks* → context/tool discovery
  → native conversation state machine

A route either fully handles the turn (``handled=True``) or returns control.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class RouteDecision:
    """Outcome of one route plugin hook."""

    # Turn is finished (reply sent / graph started). Pipeline returns.
    handled: bool = False


@runtime_checkable
class ChatRoute(Protocol):
    """One product route that can intercept a chat turn before inference."""

    name: str

    async def before_intent(
        self,
        ports: Any,
        websocket: Any,
        session: Any,
        text: str,
        *,
        is_resume: bool,
        resume_state: Any,
        reserved: bool,
    ) -> RouteDecision:
        """Run after session bind, before memory and tool discovery."""
        ...

def default_routes() -> list[ChatRoute]:
    """Installed product routes (order = priority)."""
    from chat_commands import CommandChatRoute
    from chat_memory import MemoryChatRoute
    return [CommandChatRoute(), MemoryChatRoute()]
