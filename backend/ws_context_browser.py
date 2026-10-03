"""User control-plane browsing of an explicitly selected session context."""
from __future__ import annotations

import asyncio
import threading

from context_export import ContextExportCancelled, export_view
from tools import ToolError
from ws_protocol import request_id, session_chat_id

_OPERATIONS = {"capture", "children", "views", "status", "read", "search", "expand", "refresh", "export"}


def _text(message, key, *, default="", maximum=512):
    value = message.get(key, default)
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{key} must be text within its supported length.")
    return value


def _number(message, key, default, maximum):
    value = message.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{key} must be a nonnegative integer within its supported range.")
    return value


async def _export(service, chat_id, view_id, message):
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(export_view, service, chat_id, view_id,
        path=_text(message, "path", maximum=32768), format=_text(message, "format", default="jsonl", maximum=16),
        overwrite=message.get("overwrite", False), cancelled=cancelled.is_set))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        # The thread owns the staging file. Wait for its cleanup before the
        # handler releases ownership; cancellation never publishes a partial file.
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


async def operate(runtime, session, message):
    operation = _text(message, "operation", maximum=32)
    if operation not in _OPERATIONS:
        raise ValueError("Unsupported context operation.")
    chat_id = _text(message, "chat_id", default=session_chat_id(session))
    if not chat_id or not runtime.sessions.has_session(chat_id):
        raise ToolError("Choose an existing conversation.")
    service = runtime.session_context
    view_id = _text(message, "view_id")
    child_id = _text(message, "child_id")
    if operation == "children":
        manager = getattr(getattr(runtime, "catalog", None), "children", None)
        if manager is None:
            return {"items": [], "truncated": False}
        roster = await asyncio.to_thread(manager.inspection_snapshot, chat_id, limit=100)
        return {"items": [{key: row.get(key) for key in ("child_id", "name", "status", "deletion_state")}
                          for row in roster["children"] if row.get("deletion_state") not in {"deleted", "deleting"}],
                "truncated": bool(roster.get("truncated"))}
    if operation == "capture":
        if view_id:
            raise ValueError("Capture does not reopen a view. Use status to inspect a saved view.")
        view_id = await asyncio.to_thread(service.capture, chat_id, child_id=child_id)
        return {"view_id": view_id, "status": await asyncio.to_thread(service.status, chat_id, view_id)}
    if operation == "views":
        return await asyncio.to_thread(service.list_views, chat_id, child_id=child_id,
            after=_text(message, "after"), limit=_number(message, "limit", 20, 100))
    if not view_id:
        raise ValueError("Select or capture a context view first.")
    if operation == "refresh":
        view = await asyncio.to_thread(service._view, chat_id, view_id)
        refreshed = await asyncio.to_thread(service.capture, chat_id, child_id=view["child_id"])
        return {"view_id": refreshed, "status": await asyncio.to_thread(service.status, chat_id, refreshed)}
    if operation == "export":
        return await _export(service, chat_id, view_id, message)
    kwargs = {}
    if operation in {"read", "search"}:
        kwargs.update(after=_number(message, "after", 0, 2**63 - 1),
            limit=_number(message, "limit", 50 if operation == "read" else 20, 200 if operation == "read" else 100),
            kind=_text(message, "kind", maximum=32))
    if operation == "read":
        kwargs.update(around=_text(message, "around"), before=_number(message, "before", 2, 100))
    elif operation == "search":
        kwargs["query"] = _text(message, "query", maximum=1000)
    elif operation == "expand":
        kwargs.update(source_id=_text(message, "source_id"), offset=_number(message, "offset", 0, 2**63 - 1),
            max_chars=_number(message, "max_chars", 12000, 100000), part=_text(message, "part", default="result", maximum=16))
    return await asyncio.to_thread(getattr(service, operation), chat_id, view_id, **kwargs)


def register(on):
    @on("external-context:request")
    async def command(host, websocket, session, message):
        correlation = request_id(message)
        response = {"type": "external-context:result", "request_id": correlation,
                    "operation": str(message.get("operation") or "")[:32],
                    "chat_id": str(message.get("chat_id") or session_chat_id(session))[:512],
                    "ok": False}
        try:
            if not correlation:
                raise ValueError("A request ID is required.")
            result = await operate(host.require_runtime(), session, message)
            response.update(ok=True, result=result)
        except ContextExportCancelled:
            response["error"] = {"code": "context_export_cancelled", "message": "Context export was cancelled."}
        except (ValueError, ToolError) as exc:
            response["error"] = {"code": str(getattr(exc, "code", "") or "context_request_invalid"), "message": str(exc)}
        except FileExistsError:
            response["error"] = {"code": "context_export_exists", "message": "Destination already exists. Choose another file."}
        except Exception:
            response["error"] = {"code": "context_request_failed", "message": "Context request failed. Refresh and try again."}
        await websocket.send_json(response)
