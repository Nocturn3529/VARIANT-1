"""Correlated WebSocket commands for extensions and MCP v2."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from extensions.capabilities_v2 import EXTENSION_PACKAGE_JOB
from extensions.mcp_v2 import McpLease
from ws_protocol import (
    CorrelatedResponder,
    request_id as _request_id,
    session_work_scope as _scope,
)


def _runtime(srv: Any):
    runtime = getattr(srv.require_runtime(), "extensions", None)
    if runtime is None:
        raise RuntimeError("Extension runtime v2 is unavailable")
    return runtime


def _lease(value: Any) -> McpLease:
    if not isinstance(value, Mapping):
        raise ValueError("lease is required")
    return McpLease(
        str(value.get("server_id") or ""),
        int(value.get("generation") or 0),
        int(value.get("capability_revision") or 0),
        str(value.get("kind") or ""),
        str(value.get("name") or ""),
        str(value.get("schema_digest") or ""),
    )


_respond = CorrelatedResponder(
    family="extension-v2",
    schema="variant1.extension-command.v2",
)


def register(on):
    @on("extension-v2:list")
    async def extension_list(srv, websocket, session, msg):
        async def action():
            return await asyncio.to_thread(
                _runtime(srv).plugins,
                str(msg.get("query") or ""),
                limit=max(1, min(int(msg.get("limit") or 100), 500)),
            )
        await _respond(websocket, msg, "list", action, mutation=False)

    @on("extension-v2:rescan")
    async def extension_rescan(srv, websocket, session, msg):
        async def action():
            runtime = _runtime(srv)
            scan = await asyncio.to_thread(runtime.rescan)
            return {"scan": scan, "plugins": runtime.plugins(limit=500)}
        await _respond(websocket, msg, "rescan", action)

    @on("extension-v2:set-enabled")
    async def extension_set_enabled(srv, websocket, session, msg):
        async def action():
            package_id = str(msg.get("package_id") or "").strip()
            if not package_id or package_id.startswith("invalid:"):
                raise ValueError("a valid package_id is required")
            return await asyncio.to_thread(
                _runtime(srv).set_enabled,
                package_id,
                bool(msg.get("enabled", True)),
            )
        await _respond(websocket, msg, "set-enabled", action)

    @on("extension-v2:get")
    async def extension_get(srv, websocket, session, msg):
        async def action():
            package_id = str(msg.get("package_id") or "")
            result = await asyncio.to_thread(
                _runtime(srv).packages.inspect,
                package_id,
                version=str(msg.get("version") or ""),
                digest=str(msg.get("digest") or ""),
            )
            result["contributions"] = await asyncio.to_thread(
                _runtime(srv).packages.list_contributions,
                package_id,
                chat_id=_scope(session).chat_id,
            )
            return result
        await _respond(websocket, msg, "get", action, mutation=False)

    @on("extension-v2:resource")
    async def extension_resource(srv, websocket, session, msg):
        async def action():
            return await asyncio.to_thread(
                _runtime(srv).packages.read_resource,
                str(msg.get("package_id") or ""),
                str(msg.get("contribution_id") or ""),
                str(msg.get("resource") or ""),
                chat_id=_scope(session).chat_id,
            )
        await _respond(websocket, msg, "resource", action, mutation=False)

    @on("extension-v2:package")
    async def extension_package(srv, websocket, session, msg):
        async def action():
            selected = str(msg.get("action") or "")
            if selected not in {"install", "update", "rollback", "promote_dev_mount"}:
                raise ValueError("unsupported package action")
            scope = _scope(session)
            work = srv.require_runtime().work
            job = work.jobs.create(
                EXTENSION_PACKAGE_JOB,
                owner_kind="extension",
                owner_id=str(msg.get("package_id") or scope.chat_id or "variant1"),
                scope=scope,
                input_manifest={
                    "schema": "variant1.extension-package-request.v2",
                    "action": selected,
                    "source": str(msg.get("source") or ""),
                    "package_id": str(msg.get("package_id") or ""),
                    "version": str(msg.get("version") or ""),
                    "activate": bool(msg.get("activate", True)),
                    "chat_id": scope.chat_id,
                },
                max_attempts=1,
                idempotency_key=str(msg.get("idempotency_key") or _request_id(msg)),
            )
            return job.to_dict()
        await _respond(websocket, msg, "package", action)

    @on("extension-v2:pin", "extension-v2:dev-mount")
    async def extension_pin(srv, websocket, session, msg):
        operation = str(msg.get("type") or "").split(":", 1)[-1]

        async def action():
            chat_id = _scope(session).chat_id
            if not chat_id:
                raise ValueError("chat attachment is required")
            if operation == "pin":
                return await asyncio.to_thread(
                    _runtime(srv).packages.pin,
                    chat_id,
                    str(msg.get("package_id") or ""),
                    version=str(msg.get("version") or ""),
                )
            return await asyncio.to_thread(
                _runtime(srv).packages.dev_mount,
                str(msg.get("path") or ""),
                chat_id=chat_id,
            )
        await _respond(websocket, msg, operation, action)

    @on("extension-v2:invoke")
    async def extension_invoke(srv, websocket, session, msg):
        async def action():
            scope = _scope(session)
            request_id = _request_id(msg)
            package_id = str(msg.get("package_id") or "")
            contribution_id = str(msg.get("contribution_id") or "")
            arguments = dict(msg.get("arguments") or {})
            context = {
                "schema": "variant1.plugin-context.v1",
                "request_id": request_id,
                "surface": "websocket",
                "chat_id": scope.chat_id,
                "work_scope": scope.to_dict(),
            }
            idempotency_key = str(msg.get("idempotency_key") or request_id)
            deadline_s = max(
                0.1,
                min(float(msg.get("deadline_ms") or 30_000) / 1000.0, 300.0),
            )
            workers = _runtime(srv).workers
            return await workers.submit(
                package_id,
                contribution_id,
                arguments,
                context=context,
                chat_id=scope.chat_id,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline_s=deadline_s,
            )
        await _respond(websocket, msg, "invoke", action)

    @on("extension-v2:operation")
    async def extension_operation(srv, websocket, session, msg):
        async def action():
            return await asyncio.to_thread(
                _runtime(srv).workers.operation,
                str(msg.get("operation_id") or ""),
            )
        await _respond(websocket, msg, "operation", action, mutation=False)

    @on("extension-v2:cancel")
    async def extension_cancel(srv, websocket, session, msg):
        async def action():
            request_id = str(msg.get("target_request_id") or "").strip()
            operation_id = str(msg.get("operation_id") or "").strip()
            if not request_id and not operation_id:
                raise ValueError("target_request_id or operation_id is required")
            workers = _runtime(srv).workers
            target = request_id or operation_id
            cancelled = await workers.cancel(target)
            return {
                "request_id": request_id,
                "operation_id": operation_id,
                "cancelled": bool(cancelled),
            }
        await _respond(websocket, msg, "cancel", action)

    @on("mcp-v2:catalog")
    async def mcp_catalog(srv, websocket, session, msg):
        async def action():
            return _runtime(srv).mcp.search(
                str(msg.get("query") or ""),
                kind=str(msg.get("kind") or ""),
                server_id=str(msg.get("server_id") or ""),
            )[:max(1, min(int(msg.get("limit") or 100), 500))]
        await _respond(websocket, msg, "mcp.catalog", action, mutation=False)

    @on("mcp-v2:server")
    async def mcp_server(srv, websocket, session, msg):
        async def action():
            service = _runtime(srv).mcp
            selected = str(msg.get("action") or "")
            server_id = str(msg.get("server_id") or "")
            if selected == "list":
                return [
                    {**row, **service.status(str(row["server_id"]))}
                    for row in service.configured()
                ]
            if selected == "connect":
                return await service.connect(server_id, dict(msg.get("spec") or {}))
            if selected == "reconnect":
                return await service.reconnect(server_id)
            if selected == "disconnect":
                await service.disconnect(server_id)
                return {"server_id": server_id, "status": "disconnected"}
            if selected == "remove":
                removed = await service.remove(server_id)
                return {
                    "server_id": server_id,
                    "status": "removed",
                    "removed": bool(removed),
                }
            if selected == "refresh":
                return await service.refresh(server_id)
            if selected == "ping":
                return {"server_id": server_id, "ok": await service.ping(server_id)}
            if selected == "status":
                return service.status(server_id)
            raise ValueError("unsupported MCP server action")
        await _respond(websocket, msg, "mcp.server", action)

    @on("mcp-v2:invoke")
    async def mcp_invoke(srv, websocket, session, msg):
        async def action():
            service = _runtime(srv).mcp
            lease = _lease(msg.get("lease"))
            selected = str(msg.get("operation") or "call")
            if selected == "call":
                result = await service.call_tool(
                    lease, dict(msg.get("arguments") or {}),
                    request_id=str(msg.get("mcp_request_id") or _request_id(msg)),
                )
            elif selected == "read_resource":
                result = await service.read_resource(lease)
            elif selected == "get_prompt":
                result = await service.get_prompt(lease, dict(msg.get("arguments") or {}))
            elif selected == "subscribe":
                return await service.subscribe(lease)
            elif selected == "unsubscribe":
                return await service.unsubscribe(lease)
            else:
                raise ValueError("unsupported MCP invoke operation")
            return result.to_dict()
        await _respond(websocket, msg, "mcp.invoke", action)


__all__ = ["register"]
