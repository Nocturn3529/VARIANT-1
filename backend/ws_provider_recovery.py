"""Correlated Settings reads/saves for explicit provider recovery routes."""
from __future__ import annotations

import provider_recovery_settings as settings
from ws_protocol import request_id


def register(on):
    @on("provider-recovery:get", "provider-recovery:set")
    async def command(host, websocket, session, message):
        operation = message["type"].split(":")[1]
        correlation = request_id(message)
        response = {"type": "provider-recovery:result", "operation": operation,
                    "request_id": correlation, "ok": False}
        try:
            if not correlation:
                raise settings.SettingsValidation("A request ID is required.")
            result = settings.snapshot(host.router) if operation == "get" else settings.save(host.router, message)
            response.update(ok=True, result=result)
        except settings.SettingsConflict as exc:
            response["error"] = {"code": "revision_conflict", "message": str(exc)}
        except settings.SettingsValidation as exc:
            response["error"] = {"code": "invalid_routing_setting", "message": str(exc)}
        except Exception:
            response["error"] = {"code": "routing_setting_failed",
                                 "message": "Routing settings could not be saved. Refresh and try again."}
        await websocket.send_json(response)
