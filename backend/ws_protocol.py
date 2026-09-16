"""Shared, domain-neutral WebSocket response primitives.

Domain adapters keep ownership of service lookup, scope validation, commands,
and transactions.  This module owns only the repeated transport envelope used
to correlate a Main Deck request with one accepted or rejected response.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from work_fabric.scope import WorkScope


def request_id(message: Mapping[str, Any]) -> str:
    """Return the bounded correlation ID used by Deck command protocols."""

    return str(message.get("request_id") or "").strip()[:512]


def session_chat_id(session: Any) -> str:
    """Return the one chat identity used by every Deck domain adapter."""

    active = getattr(session, "active", None)
    return str(
        getattr(session, "viewed_session_id", "")
        or getattr(active, "runtime_chat_id", "")
        or getattr(active, "turn_session_id", "")
        or ""
    ).strip()


def session_work_scope(session: Any) -> WorkScope:
    """Project the current durable chat into the canonical Work scope."""

    return WorkScope(chat_id=session_chat_id(session))


def chat_is_busy(runtime: Any, session: Any, chat_id: str) -> bool:
    registry = getattr(runtime, "session_runtimes", None)
    if registry is not None:
        return bool(registry.is_busy(chat_id))
    active = getattr(session, "active", None)
    owner = str(getattr(active, "runtime_chat_id", "") or
                getattr(active, "turn_session_id", "") or session_chat_id(session))
    return bool(getattr(session, "busy", False) and owner == chat_id)


@dataclass(frozen=True, slots=True)
class CorrelatedResponder:
    """Emit one stable accepted/rejected envelope around a domain action."""

    family: str
    schema: str
    mutation_default: bool = True

    async def __call__(
        self,
        websocket: Any,
        message: Mapping[str, Any],
        operation: str,
        action: Callable[[], Awaitable[Any]],
        *,
        mutation: bool | None = None,
    ) -> None:
        correlation_id = request_id(message)
        owner_chat_id = str(message.get("chat_id") or "").strip()
        require_request = (
            self.mutation_default if mutation is None else bool(mutation)
        )
        try:
            if require_request and not correlation_id:
                raise ValueError("request_id is required")
            result = await action()
            response = {
                "type": f"{self.family}:accepted",
                "schema": self.schema,
                "request_id": correlation_id,
                "operation": operation,
                "result": result,
            }
            if owner_chat_id:
                response["chat_id"] = owner_chat_id
            await websocket.send_json(response)
        except Exception as exc:
            response = {
                "type": f"{self.family}:rejected",
                "schema": self.schema,
                "request_id": correlation_id,
                "operation": operation,
                "reason_code": str(
                    getattr(exc, "code", type(exc).__name__)
                ),
                "error": str(exc),
            }
            if owner_chat_id:
                response["chat_id"] = owner_chat_id
            await websocket.send_json(response)


__all__ = [
    "CorrelatedResponder", "request_id", "session_chat_id", "session_work_scope", "chat_is_busy",
]
