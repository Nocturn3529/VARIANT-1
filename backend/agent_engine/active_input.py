"""Pi-style active-input delivery at safe main-loop boundaries."""

from __future__ import annotations

from typing import Any, Optional

from observability import context_lineage


async def wait_for_pause_boundary(ports: Any) -> None:
    wait = getattr(ports, "wait_if_paused", None)
    if wait is not None:
        await wait()


def drain_active_input(ports: Any, *, allow_follow_up: bool) -> Optional[dict]:
    """Drain one input, always giving steering priority over follow-ups."""
    row = ports.drain_steering()
    if row is None and allow_follow_up:
        row = ports.drain_follow_up()
    if not isinstance(row, dict):
        return None
    text = str(row.get("text") or "")
    if not text.strip():
        return None
    delivery = str(row.get("delivery") or "steer")
    if delivery not in {"steer", "follow_up"}:
        delivery = "steer"
    return {
        "id": str(row.get("id") or ""),
        "text": text,
        "delivery": delivery,
        "client_id": str(row.get("client_id") or ""),
        "source": str(row.get("source") or ""),
        "session_id": str(row.get("session_id") or ""),
    }


def inject_active_input(
    state: dict,
    messages: list,
    row: dict,
    *,
    receipt: dict | None = None,
) -> tuple[list, dict]:
    """Append one queued user message after the already-recorded turn."""
    out_messages = list(messages or [])
    out_messages.append({"role": "user", "content": row["text"]})

    input_state = dict(state.get("input") or {})
    delivered = list(input_state.get("delivered") or [])
    delivered.append(dict(row))
    input_state["delivered"] = delivered
    input_state["pending_model_ticket_id"] = str(row.get("id") or "pending")

    if isinstance(receipt, dict):
        context_lineage.add_item(
            receipt,
            kind="current_user",
            source="user",
            trust="user_supplied",
            decision="kept",
            reason="current_turn",
            relevance="required",
            producer=row.get("delivery"),
            chars_before=len(row["text"]),
            chars_after=len(row["text"]),
        )
        context_lineage.attach_to_messages(out_messages, receipt)
    return out_messages, input_state
