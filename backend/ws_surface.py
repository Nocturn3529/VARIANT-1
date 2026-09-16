"""WebSocket handlers for the messaging gateway."""

from __future__ import annotations

def register(on):
    # ---- Messaging gateway ------------------------------------------------------------

    @on("messaging:get")
    async def _messaging_get(srv, websocket, session, msg):
        await websocket.send_json(srv.gateway.public_state())

    @on("messaging:set")
    async def _messaging_set(srv, websocket, session, msg):
        adapter = str(msg.get("adapter") or "")
        if "enabled" in msg and not adapter:
            srv.gateway.update(enabled=bool(msg.get("enabled")))
        elif adapter:
            cfg = msg.get("config") if isinstance(msg.get("config"), dict) else {
                k: msg[k] for k in (
                    "enabled", "allowed_users", "allowed_conversations",
                    "prefix", "mention_only", "allow_all", "fields",
                ) if k in msg
            }
            srv.gateway.update(adapter=adapter, values=cfg or {})
        else:
            if "enabled" in msg:
                srv.gateway.update(enabled=bool(msg.get("enabled")))
        try:
            await srv.gateway.reconcile()
        except Exception as exc:
            payload = srv.gateway.public_state()
            payload["lifecycle_error"] = str(exc)
            await websocket.send_json(payload)
            return
        await srv.hub.broadcast(srv.gateway.public_state())

    @on("messaging:credential:set")
    async def _messaging_credential_set(srv, websocket, session, msg):
        adapter = str(msg.get("adapter") or "").strip().lower()
        try:
            field = str(msg.get("field") or "").strip()
            value = str(msg.get("value") or msg.get("token") or "")
            if field:
                srv.messaging_credentials.set(adapter, field, value)
            else:
                srv.messaging_credentials.set_token(adapter, value)
            await srv.gateway.restart()
        except Exception as exc:
            payload = srv.gateway.public_state()
            payload["credential_error"] = str(exc)
            await websocket.send_json(payload)
            return
        await srv.hub.broadcast(srv.gateway.public_state())


    @on("messaging:credential:clear")
    async def _messaging_credential_clear(srv, websocket, session, msg):
        adapter = str(msg.get("adapter") or "").strip().lower()
        try:
            srv.messaging_credentials.clear(adapter, str(msg.get("field") or ""))
            await srv.gateway.restart()
        except Exception as exc:
            payload = srv.gateway.public_state()
            payload["credential_error"] = str(exc)
            await websocket.send_json(payload)
            return
        await srv.hub.broadcast(srv.gateway.public_state())

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

