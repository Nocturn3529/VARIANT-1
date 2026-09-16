"""Chat stream identity metadata shared by pipeline stages and surfaces."""

from __future__ import annotations


def infer_turn_source(client_id: str = "", source: str = "") -> str:
    src = (source or "").strip().lower()
    if src:
        return src
    client = (client_id or "").strip()
    if client.startswith("voice-") or client.startswith("mic-"):
        return "voice"
    if client:
        return "chat"
    return ""


def stream_meta(session=None, *, client_id: str = "", source: str = "") -> dict:
    active = getattr(session, "active", None) if session is not None else None
    resolved_client = (
        client_id
        or (getattr(active, "turn_client_id", "") if active else "")
        or ""
    )
    resolved_source = (
        source
        or (getattr(active, "turn_source", "") if active else "")
        or ""
    )
    resolved_source = infer_turn_source(resolved_client, resolved_source)
    resolved_session = (
        (getattr(active, "runtime_chat_id", "") if active else "")
        or (getattr(active, "turn_session_id", "") if active else "")
        or (getattr(session, "viewed_session_id", "") if session is not None else "")
        or ""
    )
    meta = {}
    if resolved_client:
        meta["client_id"] = resolved_client
    if resolved_source:
        meta["source"] = resolved_source
    if resolved_session:
        meta["session_id"] = str(resolved_session)
    admission_id = str(getattr(active, "runtime_admission_id", "") or "")
    if admission_id:
        meta["admission_id"] = admission_id
    ticket_id = str(getattr(active, "turn_ticket_id", "") or "")
    if ticket_id:
        meta["ticket_id"] = ticket_id
        if resolved_source == "queue_continue" and resolved_client:
            meta["request_id"] = resolved_client
    return meta


def bind_turn_identity(
    session,
    *,
    client_id: str = "",
    source: str = "",
) -> None:
    active = getattr(session, "active", None)
    if active is None:
        return
    active.turn_client_id = client_id or ""
    active.turn_source = infer_turn_source(client_id, source)
