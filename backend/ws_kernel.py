"""Correlated WebSocket inspection and control for persistent Python workers."""

from __future__ import annotations

import asyncio
from typing import Any

from kernel_runtime import control as kernel_control
from work_fabric.scope import WorkScope
from ws_protocol import CorrelatedResponder, request_id as _request_id


def _chat_id(srv: Any, session: Any) -> str:
    from ws_protocol import session_chat_id
    value = session_chat_id(session)
    if value:
        return value
    try:
        return str(
            srv.require_runtime().sessions.get_active() or ""
        ).strip()
    except Exception:
        return ""


def _target_chat_id(srv: Any, session: Any, msg: dict) -> str:
    """Explicit chat_id, or the displayed chat; detached windows stay scoped."""

    requested = str(msg.get("chat_id") or "").strip()
    own = _chat_id(srv, session)
    if not requested:
        return own
    if getattr(session, "view_role", "main") == "detached_chat" and requested != own:
        raise ValueError("invalid_chat_scope")
    return requested


def _history_tail(msg: dict) -> int | None:
    value = msg.get("tail")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 50:
        raise ValueError("tail must be an integer from 1 to 50")
    return value


def _children_capacity(srv: Any) -> dict[str, Any] | None:
    try:
        children = srv.require_runtime().catalog.children
        return children.capacity_status() if children is not None else None
    except Exception:
        return None


_mutation = CorrelatedResponder(
    family="kernel",
    schema="variant1.kernel-command.v1",
)


def register(on):
    @on("kernel:inventory:get")
    async def kernel_inventory(srv, websocket, session, msg):
        runtime = srv.require_runtime()
        rows = await asyncio.to_thread(runtime.kernel.live_inventory)
        if getattr(session, "view_role", "main") == "detached_chat":
            rows = [row for row in rows if row["chat_id"] == _chat_id(srv, session)]
        for row in rows:
            saved = runtime.sessions.get_session(row["chat_id"]) or {}
            row["title"] = str(saved.get("title") or row["chat_id"])
            row["busy"] = runtime.session_runtimes.is_busy(row["chat_id"])
        capacity = await asyncio.to_thread(_children_capacity, srv)
        await websocket.send_json({"type": "kernel:inventory:result",
                                   "request_id": _request_id(msg), "items": rows,
                                   "children_capacity": capacity})

    @on("kernel:release")
    async def kernel_release(srv, websocket, session, msg):
        chat_id = str(msg.get("chat_id") or "").strip()
        request_id = _request_id(msg)
        if not chat_id or (getattr(session, "view_role", "main") == "detached_chat"
                           and chat_id != _chat_id(srv, session)):
            await websocket.send_json({"type": "kernel:release:result", "request_id": request_id,
                                       "chat_id": chat_id, "ok": False, "error": "invalid_chat_scope"})
            return
        try:
            generation = msg.get("generation")
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                raise ValueError("generation is required")
            result = await srv.require_runtime().kernel.release_idle(chat_id, expected_generation=generation)
            await websocket.send_json({"type": "kernel:release:result", "request_id": request_id,
                                       "chat_id": chat_id, "ok": result["status"] in {"closed", "absent"},
                                       "error": "Stop the active run before closing this kernel." if result["status"] == "busy" else "Kernel generation changed; refresh before closing." if result["status"] == "stale" else "",
                                       "result": result})
        except Exception:
            await websocket.send_json({"type": "kernel:release:result", "request_id": request_id,
                                       "chat_id": chat_id, "ok": False, "error": "Kernel cleanup failed; refresh to inspect its state."})

    @on("kernel:get")
    async def kernel_get(srv, websocket, session, msg):
        chat_id = _chat_id(srv, session)
        if not chat_id:
            await websocket.send_json({
                "type": "kernel:snapshot",
                "schema": "variant1.kernel-snapshot.v1",
                "request_id": _request_id(msg),
                "runtime_chat_id": None,
                "status": {"state": "absent", "generation": 0, "pid": None},
                "history": [],
                "cursor": 0,
            })
            return
        after = max(0, int(msg.get("after_sequence") or 0))
        history = kernel_control.history(
            srv, chat_id,
            after_sequence=after,
            limit=max(1, min(int(msg.get("limit") or 100), 500)),
        )
        await websocket.send_json({
            "type": "kernel:snapshot",
            "schema": "variant1.kernel-snapshot.v1",
            "request_id": _request_id(msg),
            "runtime_chat_id": chat_id,
            "status": kernel_control.status(srv, chat_id),
            "history": history["items"],
            "cursor": history["next_sequence"],
        })

    @on("kernel:history")
    async def kernel_history(srv, websocket, session, msg):
        chat_id = _target_chat_id(srv, session, msg)
        if not chat_id:
            raise ValueError("no active runtime chat")
        result = await asyncio.to_thread(
            kernel_control.history,
            srv, chat_id,
            after_sequence=max(0, int(msg.get("after_sequence") or 0)),
            limit=max(1, min(int(msg.get("limit") or 100), 500)),
            tail=_history_tail(msg),
        )
        await websocket.send_json({
            "type": "kernel:history",
            "request_id": _request_id(msg),
            **result,
        })

    @on("kernel:namespace")
    async def kernel_namespace(srv, websocket, session, msg):
        chat_id = _chat_id(srv, session)
        if not chat_id:
            raise ValueError("no active runtime chat")
        result = await kernel_control.namespace(
            srv, chat_id,
            limit=max(1, min(int(msg.get("limit") or 100), 500)),
        )
        await websocket.send_json({
            "type": "kernel:namespace",
            "request_id": _request_id(msg),
            **result,
        })

    @on("kernel:notebook:export")
    async def kernel_notebook_export(srv, websocket, session, msg):
        async def action() -> dict[str, Any]:
            chat_id = _chat_id(srv, session)
            if not chat_id:
                raise ValueError("no active runtime chat")
            return kernel_control.export_notebook(
                srv, chat_id,
                after_sequence=max(0, int(msg.get("after_sequence") or 0)),
                limit=max(1, min(int(msg.get("limit") or 200), 500)),
            )

        await _mutation(websocket, msg, "notebook.export", action)

    @on("kernel:interrupt")
    async def kernel_interrupt(srv, websocket, session, msg):
        async def action() -> dict[str, Any]:
            chat_id = _target_chat_id(srv, session, msg)
            if not chat_id:
                raise ValueError("no active runtime chat")
            return await kernel_control.interrupt(
                srv, chat_id, intent="stop"
            )

        await _mutation(websocket, msg, "interrupt", action)

    @on("kernel:restart")
    async def kernel_restart(srv, websocket, session, msg):
        async def action() -> dict[str, Any]:
            chat_id = _target_chat_id(srv, session, msg)
            if not chat_id:
                raise ValueError("no active runtime chat")
            return await kernel_control.restart(
                srv, chat_id,
                reason=str(msg.get("reason") or "websocket_restart")[:512],
            )

        await _mutation(websocket, msg, "restart", action)

__all__ = ["register"]
