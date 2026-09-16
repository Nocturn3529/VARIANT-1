"""WebSocket handlers for automations."""

from __future__ import annotations

from automation.store import AutomationPersistenceError


def _automation_items(srv):
    """Return saved automations and any already-assigned worker identity.

    A read-only UI refresh must not create or pin runtime state.  The first
    actual run remains the assignment point for legacy definitions.
    """
    from session_catalog.profiles import is_action_surface
    from session_catalog.worker_surface import worker_runtime_id

    items = [dict(row) for row in (srv.automations.list() or ())]
    runtime = srv.require_runtime()
    runtimes = runtime.session_runtimes
    kernels = runtime.kernel
    for item in items:
        automation_id = str(item.get("id") or "")
        if not automation_id:
            continue
        runtime_id = worker_runtime_id("automation", automation_id)
        assignment_error = ""
        snapshot = {}
        try:
            snapshot = runtimes.snapshot(runtime_id)
        except Exception as exc:
            assignment_error = str(exc)[:300]
        kernel = (
            kernels.status(runtime_id)
            if snapshot
            else {"state": "absent", "generation": 0, "pid": None}
        )
        surface = str(snapshot.get("action_surface") or "")
        item["runtime"] = {
            "schema": "variant1.automation-runtime.v1",
            "runtime_id": runtime_id,
            "assignment_state": "pinned" if snapshot else "unassigned",
            "lifecycle_state": str(snapshot.get("lifecycle_state") or ""),
            "action_surface": surface,
            "trust_profile": str(snapshot.get("trust_profile") or ""),
            "graph_revision": str(snapshot.get("graph_revision") or ""),
            "catalog_release_id": str(snapshot.get("catalog_release_id") or ""),
            "busy": bool(snapshot.get("busy")),
            "mutation_enabled": False,
            "kernel": kernel,
            "warning": (
                "Trusted local Python — same-user runtime, not sandboxed."
                if is_action_surface(surface) else ""
            ),
            "error": assignment_error,
        }
    return items


def register(on):
    # ---- Automations ------------------------------------------------------

    async def persistence_error(websocket, msg, action: str) -> None:
        await websocket.send_json({
            "type": "automation:error",
            "request_id": str(msg.get("request_id") or ""),
            "action": str(action),
            "error": "automation_persistence_failed",
        })

    @on("automation:list")
    async def _automation_list(srv, websocket, session, msg):
        await websocket.send_json({
            "type": "automations",
            "items": _automation_items(srv),
        })

    @on("automations:history")
    async def _automation_history(srv, websocket, session, msg):
        await websocket.send_json({
            "type": "automations:history",
            "items": srv.automation_history.list(
                automation_id=str(msg.get("id") or msg.get("automation_id") or ""),
                limit=int(msg.get("limit") or 100),
            ),
            "count": srv.automation_history.count(),
        })

    @on("automation:add")
    async def _automation_add(srv, websocket, session, msg):
        from model_runtime.context import session_model_route
        from ws_protocol import session_chat_id
        kwargs = {}
        kwargs["model_route"] = session_model_route(
            srv.require_runtime().sessions, session_chat_id(session), srv.router,
        )
        if "misfire_policy" in msg:
            kwargs["misfire_policy"] = msg.get("misfire_policy")
        try:
            srv.automations.add(
                msg.get("name", ""),
                msg.get("prompt", ""),
                msg.get("trigger", {}) or {},
                bool(msg.get("enabled", True)),
                **kwargs,
            )
        except AutomationPersistenceError:
            await persistence_error(websocket, msg, "add")
            return
        await websocket.send_json({
            "type": "automation:accepted",
            "request_id": str(msg.get("request_id") or ""),
            "action": "add",
        })
        await srv.hub.broadcast({
            "type": "automations",
            "items": _automation_items(srv),
        })

    @on("automation:update")
    async def _automation_update(srv, websocket, session, msg):
        keys = (
            "name", "prompt", "enabled", "trigger", "misfire_policy", "model_route",
        )
        if "model_route" in msg:
            from model_runtime.context import normalize_model_route
            msg = {**msg, "model_route": normalize_model_route(srv.router, msg["model_route"])}
        try:
            updated = srv.automations.update(
                msg.get("id", ""),
                {key: msg[key] for key in keys if key in msg},
            )
        except AutomationPersistenceError:
            await persistence_error(websocket, msg, "update")
            return
        if updated and "enabled" in msg and not bool(msg.get("enabled")):
            srv.require_runtime().workflows.cancel_automation(
                str(msg.get("id") or ""), reason="automation_disabled",
            )
        await websocket.send_json({
            "type": "automation:accepted",
            "request_id": str(msg.get("request_id") or ""),
            "action": "update",
        })
        await srv.hub.broadcast({
            "type": "automations",
            "items": _automation_items(srv),
        })

    @on("automation:remove")
    async def _automation_remove(srv, websocket, session, msg):
        automation_id = str(msg.get("id") or "")
        try:
            removed = srv.automations.remove(automation_id)
        except AutomationPersistenceError:
            await persistence_error(websocket, msg, "remove")
            return
        if removed:
            srv.require_runtime().workflows.cancel_automation(
                automation_id, reason="automation_deleted",
            )
            delete_worker = srv.automation_ports().agent.delete_worker_runtime
            if callable(delete_worker):
                await delete_worker(source="automation", key=automation_id)
        await websocket.send_json({
            "type": "automation:accepted",
            "request_id": str(msg.get("request_id") or ""),
            "action": "remove",
        })
        await srv.hub.broadcast({
            "type": "automations",
            "items": _automation_items(srv),
        })

    @on("automation:run")
    async def _automation_run(srv, websocket, session, msg):
        task = srv.automations.get(msg.get("id", ""))
        if task:
            await srv.require_runtime().workflows.run_automation(
                task, trigger_source="manual",
            )
        await websocket.send_json({
            "type": "automations",
            "items": _automation_items(srv),
        })
