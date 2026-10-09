"""Desktop-control status and the macOS permission request for Settings."""
from __future__ import annotations


def register(on):
    @on("desktop:status", "desktop:permissions:request")
    async def desktop(srv, websocket, session, msg):
        from desktop_fabric.status import desktop_status, request_desktop_permissions

        reply = {"type": "desktop:status", "request_id": str(msg.get("request_id") or "")[:200]}
        try:
            fabric = srv.require_runtime().desktop
            if msg.get("type") == "desktop:permissions:request":
                reply["permission_request"] = {"started": request_desktop_permissions(fabric)}
            reply.update(ok=True, **desktop_status(fabric))
        except Exception as exc:
            reply.update(ok=False, error={"code": "desktop_status_failed", "message": str(exc)})
        await websocket.send_json(reply)
