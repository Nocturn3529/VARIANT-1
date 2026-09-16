"""Correlated WebSocket inspection and control for persistent Python workers."""

from __future__ import annotations

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


_mutation = CorrelatedResponder(
    family="kernel",
    schema="variant1.kernel-command.v1",
)


def register(on):
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
        chat_id = _chat_id(srv, session)
        if not chat_id:
            raise ValueError("no active runtime chat")
        result = kernel_control.history(
            srv, chat_id,
            after_sequence=max(0, int(msg.get("after_sequence") or 0)),
            limit=max(1, min(int(msg.get("limit") or 100), 500)),
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
            chat_id = _chat_id(srv, session)
            if not chat_id:
                raise ValueError("no active runtime chat")
            return await kernel_control.interrupt(
                srv, chat_id, intent="stop"
            )

        await _mutation(websocket, msg, "interrupt", action)

    @on("kernel:restart")
    async def kernel_restart(srv, websocket, session, msg):
        async def action() -> dict[str, Any]:
            chat_id = _chat_id(srv, session)
            if not chat_id:
                raise ValueError("no active runtime chat")
            return await kernel_control.restart(
                srv, chat_id,
                reason=str(msg.get("reason") or "websocket_restart")[:512],
            )

        await _mutation(websocket, msg, "restart", action)

__all__ = ["register"]
