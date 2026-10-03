"""Deterministic utility commands that bypass model inference and tool schemas."""

from __future__ import annotations

import re
from typing import Any

from chat_finalize import finish_chat_turn
from chat_routes import RouteDecision


_STATUS_COMMAND = re.compile(r"^\s*/system-status(?:\s+(.*))?\s*$", re.I | re.S)
_RETIRED_REMEMBER_COMMAND = re.compile(r"^\s*/remember(?:\s+.*)?\s*$", re.I | re.S)


def parse_system_status_command(text: str) -> str | None:
    match = _STATUS_COMMAND.match(str(text or ""))
    return None if not match else str(match.group(1) or "").strip()


def is_direct_command(text: str) -> bool:
    return parse_system_status_command(text) is not None or bool(_RETIRED_REMEMBER_COMMAND.match(text))


class CommandChatRoute:
    """User-invoked host utilities; no model call is made."""

    name = "commands"

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
        if _RETIRED_REMEMBER_COMMAND.match(str(text or "")):
            await finish_chat_turn(
                ports, websocket, session, text, "neutral",
                "Cross-session fact memory has been retired. Session history is available "
                "to the agent through session.context(); /remember no longer saves facts.",
            )
            return RouteDecision(handled=True)
        status_arg = parse_system_status_command(text)
        if status_arg is not None:
            if status_arg:
                reply, mood = "Use `/system-status` without arguments.", "neutral"
            elif not ports.commands or not ports.commands.system_status:
                reply, mood = "System status is unavailable.", "concerned"
            else:
                try:
                    reply = await ports.commands.system_status()
                    mood = "neutral"
                except Exception as exc:
                    print(f"[commands] /system-status failed: {exc}", flush=True)
                    reply, mood = str(exc) or "System status is unavailable.", "concerned"
            await finish_chat_turn(
                ports, websocket, session, text, mood, reply)
            return RouteDecision(handled=True)

        return RouteDecision()
