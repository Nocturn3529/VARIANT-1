"""WebSocket handlers for the messaging gateway."""

from __future__ import annotations


async def _messaging_write(
    srv, websocket, msg, *, operation: str, adapter: str, field: str = "",
    write, refresh, error_key: str,
) -> None:
    """Acknowledge a persisted write separately from gateway availability."""
    accepted = False
    error = ""
    try:
        write()
        accepted = True
        await refresh()
    except Exception:
        if accepted:
            error = "The change was saved. The messaging gateway could not refresh."
        elif error_key == "credential_error":
            error = "Could not save the messaging credential change."
        else:
            error = "Could not save the messaging settings."

    state = srv.gateway.public_state()
    if accepted:
        await srv.hub.broadcast(state)
    request_id = msg.get("request_id")
    if isinstance(request_id, str) and request_id:
        await websocket.send_json({
            "type": "messaging:write_result",
            "request_id": request_id,
            "operation": operation,
            "adapter": adapter,
            "field": field,
            "accepted": accepted,
            "error": error,
        })
    if error:
        await websocket.send_json({**state, error_key: error})


def register(on):
    # ---- Messaging gateway ------------------------------------------------------------

    @on("messaging:get")
    async def _messaging_get(srv, websocket, session, msg):
        await websocket.send_json(srv.gateway.public_state())

    @on("messaging:set")
    async def _messaging_set(srv, websocket, session, msg):
        adapter = str(msg.get("adapter") or "")

        def save():
            if adapter:
                cfg = msg.get("config") if isinstance(msg.get("config"), dict) else {
                    k: msg[k] for k in (
                        "enabled", "allowed_users", "allowed_conversations",
                        "prefix", "mention_only", "allow_all", "fields",
                    ) if k in msg
                }
                srv.gateway.update(adapter=adapter, values=cfg)
            elif "enabled" in msg:
                srv.gateway.update(enabled=bool(msg.get("enabled")))

        await _messaging_write(
            srv, websocket, msg, operation="messaging:set", adapter=adapter,
            write=save, refresh=srv.gateway.reconcile, error_key="lifecycle_error",
        )

    @on("messaging:credential:set")
    async def _messaging_credential_set(srv, websocket, session, msg):
        adapter = str(msg.get("adapter") or "").strip().lower()
        field = str(msg.get("field") or "").strip()

        def save():
            value = str(msg.get("value") or msg.get("token") or "")
            if field:
                srv.messaging_credentials.set(adapter, field, value)
            else:
                srv.messaging_credentials.set_token(adapter, value)

        await _messaging_write(
            srv, websocket, msg, operation="messaging:credential:set", adapter=adapter, field=field,
            write=save, refresh=srv.gateway.restart, error_key="credential_error",
        )

    @on("messaging:credential:clear")
    async def _messaging_credential_clear(srv, websocket, session, msg):
        adapter = str(msg.get("adapter") or "").strip().lower()
        field = str(msg.get("field") or "").strip()
        await _messaging_write(
            srv, websocket, msg, operation="messaging:credential:clear", adapter=adapter, field=field,
            write=lambda: srv.messaging_credentials.clear(adapter, field),
            refresh=srv.gateway.restart, error_key="credential_error",
        )

    @on("messaging:pairing:approve")
    async def _messaging_pairing_approve(srv, websocket, session, msg):
        changed = srv.gateway.approve_pairing(
            str(msg.get("platform") or ""), str(msg.get("request_id") or ""))
        if not changed:
            await websocket.send_json({"type": "messaging:error",
                "error": "pairing request is no longer pending"})
            return
        await srv.hub.broadcast(srv.gateway.public_state())

    @on("messaging:pairing:revoke")
    async def _messaging_pairing_revoke(srv, websocket, session, msg):
        changed = srv.gateway.revoke_pairing(
            str(msg.get("platform") or ""), str(msg.get("user_id") or ""))
        if not changed:
            await websocket.send_json({"type": "messaging:error",
                "error": "approved user was not found"})
            return
        await srv.hub.broadcast(srv.gateway.public_state())

