"""Correlated composer settings over the canonical chat runtime and route store."""

from contextlib import nullcontext
from functools import wraps
import logging

from session_runtime import SessionRuntimeRegistry


class _SettingsReply:
    def __init__(self, websocket, *, request_id, session_id):
        self.websocket = websocket
        self.request_id = request_id
        self.session_id = session_id
        self.applied = False
        self.error = {}

    def __getattr__(self, name):
        return getattr(self.websocket, name)

    async def send_json(self, payload):
        kind = payload.get("type")
        if kind == "error":
            self.error = dict(payload)
        if self.request_id:
            payload = {**payload, "request_id": self.request_id, "session_id": self.session_id}
        await self.websocket.send_json(payload)


def session_settings_command(operation):
    """Preserve legacy replies and settle supplied request IDs after application."""
    def decorate(handler):
        @wraps(handler)
        async def command(srv, websocket, session, msg):
            if operation == "mode:set" and str(msg.get("scope") or "").strip().lower() != "session":
                return await handler(srv, websocket, session, msg)
            runtime = srv.require_runtime()
            sid = str(msg.get("id") or "").strip() or runtime.chat.viewed_session_id(session)
            request_id = str(msg.get("request_id") or "").strip()[:512]
            reply = _SettingsReply(websocket, request_id=request_id, session_id=sid)
            registry = getattr(runtime, "session_runtimes", None)
            guard = (registry.change_settings(sid)
                     if isinstance(registry, SessionRuntimeRegistry) and runtime.sessions.has_session(sid)
                     else nullcontext())
            try:
                with guard:
                    await handler(srv, reply, session, {**msg, "id": sid})
            except Exception as exc:
                code = str(exc)
                if code == "session_has_active_run":
                    code = "session_busy_model_switch" if operation == "mode:set" else "session_busy_reasoning_change"
                elif code != "session_configuration_pending":
                    code = "settings_update_failed"
                if not reply.applied:
                    await reply.send_json({"type":"error", "code":code, "error":str(exc)})
                else:
                    logging.getLogger(__name__).warning(
                        "settings post-apply response failed: operation=%s chat=%s request=%s",
                        operation, sid, request_id, exc_info=True)
            if request_id:
                status = "applied" if reply.applied else "rejected"
                payload = {"type":"session:settings:ack", "operation":operation,
                           "request_id":request_id, "session_id":sid, "status":status}
                if status == "applied":
                    from model_runtime.context import session_model_route
                    payload["route"] = session_model_route(runtime.sessions, sid, srv.router)
                else:
                    payload.update(code=reply.error.get("code", "settings_update_failed"),
                                   error=reply.error.get("error", "Settings change did not complete"))
                await websocket.send_json(payload)
        return command
    return decorate


def mark_settings_applied(websocket):
    """Record the durable route write before fallible post-apply work/replies."""
    if isinstance(websocket, _SettingsReply):
        websocket.applied = True
