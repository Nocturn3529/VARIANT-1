"""Direct memory chat commands.

``/remember <text>`` is handled before model/tool selection. The command is a
user-authored durable write, so it never depends on the active model choosing a
memory tool or producing valid arguments.
"""

from __future__ import annotations

import re
from typing import Any

from chat_finalize import finish_chat_turn
from chat_routes import RouteDecision


_REMEMBER_COMMAND = re.compile(r"^\s*/remember(?:\s+(.*))?\s*$", re.I | re.S)


def parse_remember_command(text: str) -> str | None:
    """Return command content, ``""`` for a missing value, or ``None``."""
    match = _REMEMBER_COMMAND.match(str(text or ""))
    if not match:
        return None
    return str(match.group(1) or "").strip()


class MemoryChatRoute:
    """Deterministic user-facing memory commands; no model call is made."""

    name = "memory"

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
        note = parse_remember_command(text)
        if note is None:
            return RouteDecision()

        if not note:
            reply = "Add what you want saved after the command: `/remember <text>`."
            mood = "neutral"
        else:
            sid = str(
                getattr(session.active, "turn_session_id", None)
                or getattr(session, "viewed_session_id", None)
                or ""
            ).strip()
            try:
                writer = ports.memory.remember_explicit
                saved = bool(writer) and await writer(note, session_id=sid)
            except Exception as exc:
                print(f"[memory] /remember failed: {exc}", flush=True)
                saved = False
            if saved:
                reply = "Remembered."
                mood = "warm"
            else:
                reply = "I couldn't save that because durable memory is unavailable."
                mood = "concerned"

        await finish_chat_turn(
            ports, websocket, session, text, mood, reply, extract_memory=False)
        return RouteDecision(handled=True)
