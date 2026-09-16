"""Typed MCP v2 client preserving structured and binary content."""

from __future__ import annotations

import asyncio
import base64
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field, replace
import hashlib
import inspect
import json
import os
import re
import shlex
import sqlite3
from typing import Any, Awaitable, Callable, Mapping

import background_tasks
from core_invariants import canonical_json as _stable, request_fingerprint
from capability_broker import current_capability_invocation

class McpV2Error(RuntimeError): pass
class StaleMcpLease(McpV2Error): pass
class McpRequestConflict(McpV2Error): pass
class UnknownMcpEffect(McpV2Error): pass


_SECRET_ENV_MARKERS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL",
)


def child_environment(server_environment: Mapping[str, Any] | None) -> dict[str, str]:
    """Build the one inherited environment used by stdio MCP transports."""

    environment = {
        key: value for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
    }
    environment.update({
        str(key): str(value)
        for key, value in dict(server_environment or {}).items()
    })
    return environment


def command_transport_spec(
    raw: str, *, environment: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Parse the compact settings command into the canonical MCP v2 spec."""

    value = str(raw or "").strip()
    if re.match(r"^https?://", value, re.I):
        return transport_spec({"transport": "sse", "url": value})
    command = shlex.split(value) if value else []
    return transport_spec({
        "transport": "stdio",
        "command": command,
        "env": {str(key): str(item) for key, item in dict(environment or {}).items()},
    })


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping): return _protocol_json(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _protocol_json(dump(mode="json", by_alias=True, exclude_none=True))
    out = {}
    for name in (
        "type", "text", "data", "mimeType", "mime_type", "uri", "uriTemplate",
        "uri_template", "name", "title", "description", "blob", "resource",
        "inputSchema", "input_schema", "annotations", "arguments", "content",
        "structuredContent", "structured_content", "isError", "is_error",
        "nextCursor", "next_cursor", "resourceTemplates", "resources", "prompts",
        "tools", "messages", "icons", "contents",
    ):
        if hasattr(value, name): out[name] = getattr(value, name)
    return _protocol_json(out)


def _protocol_json(value: Any) -> Any:
    """Normalize SDK values before strict catalog hashing, without hiding errors."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {key: _protocol_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_protocol_json(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _protocol_json(dump(mode="json", by_alias=True, exclude_none=True))
    from pydantic import AnyUrl
    from pydantic_core import Url, MultiHostUrl
    if isinstance(value, (AnyUrl, Url, MultiHostUrl)):
        return str(value)
    # Unknown leaves deliberately reach canonical_json unchanged and fail its
    # strict validation. Never stringify a descriptor to make hashing succeed.
    return value


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)): return value
    if isinstance(value, bytes): return base64.b64encode(value).decode("ascii")
    if isinstance(value, Mapping): return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [_normalize(item) for item in value]
    mapped = _dict(value)
    return _normalize(mapped) if mapped else str(value)


@dataclass(frozen=True)
class McpLease:
    server_id: str; generation: int; capability_revision: int; kind: str; name: str; schema_digest: str
    def to_dict(self) -> dict[str, Any]: return {"server_id": self.server_id, "generation": self.generation, "capability_revision": self.capability_revision, "kind": self.kind, "name": self.name, "schema_digest": self.schema_digest}


@dataclass(frozen=True)
class McpResult:
    content: tuple[Mapping[str, Any], ...] = (); structured_content: Any = None
    is_error: bool = False; metadata: Mapping[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]:
        return {"schema": "variant1.mcp-result.v2", "content": [dict(item) for item in self.content],
                "structured_content": self.structured_content, "is_error": self.is_error,
                "metadata": dict(self.metadata)}


def preserve_content(result: Any) -> McpResult:
    raw = _dict(result)
    blocks = []
    values = list(raw.get("content") or getattr(result, "content", None) or [])
    # resources/read uses `contents`, unlike tools/call's `content` blocks.
    # Keep URI/MIME/text/blob together as resource content, not hidden metadata.
    values.extend({"type": "resource", "resource": item} for item in raw.get("contents") or [])
    for message in raw.get("messages") or []:
        normalized_message = _normalize(message)
        message_content = normalized_message.get("content") if isinstance(normalized_message, Mapping) else None
        for value in (message_content if isinstance(message_content, list) else [message_content]):
            if value is not None:
                mapped = _normalize(value)
                if isinstance(mapped, Mapping):
                    mapped = {"role": normalized_message.get("role"), **mapped}
                values.append(mapped)
    for value in values:
        item = _normalize(_dict(value)); kind = str(item.get("type") or "")
        if not kind:
            if item.get("text") is not None: kind = "text"
            elif item.get("data") is not None: kind = "image"
            elif item.get("resource") is not None: kind = "resource"
        normalized = {"type": kind, **item}
        if "mime_type" in normalized and "mimeType" not in normalized:
            normalized["mimeType"] = normalized.pop("mime_type")
        blocks.append(normalized)
    structured = _normalize(raw.get("structuredContent", raw.get("structured_content")))
    return McpResult(tuple(blocks), structured, bool(raw.get("isError", raw.get("is_error", False))),
                     _normalize({key: value for key, value in raw.items() if key not in {"content", "contents", "messages", "structuredContent", "structured_content", "isError", "is_error"}}))


def transport_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    spec = dict(value); transport = str(spec.get("transport") or "stdio").lower()
    aliases = {"http": "streamable_http", "streamable-http": "streamable_http", "legacy_sse": "sse"}
    transport = aliases.get(transport, transport)
    if transport not in {"stdio", "streamable_http", "sse"}: raise McpV2Error("unsupported MCP transport")
    if transport == "stdio":
        command = spec.get("command")
        if not isinstance(command, list) or not command: raise McpV2Error("stdio command must be a non-empty argument array")
    elif not str(spec.get("url") or "").startswith(("http://", "https://")):
        raise McpV2Error("HTTP MCP transport requires an HTTP(S) URL")
    return {**spec, "transport": transport}


class _Server:
    def __init__(self, server_id: str, spec: Mapping[str, Any], concurrency: int) -> None:
        self.id = server_id; self.spec = dict(spec); self.generation = 1; self.revision = 0
        self.session: Any = None; self.stack: Any = None
        self.catalog: dict[tuple[str, str], dict[str, Any]] = {}
        self.semaphore = asyncio.Semaphore(max(1, min(64, concurrency)))
        self.effect_locks: dict[str, asyncio.Lock] = {}; self.requests: dict[str, asyncio.Task] = {}
        self.request_fingerprints: dict[str, str] = {}
        self.request_owners: dict[str, tuple[Any, McpLease]] = {}
        self.subscriptions: set[str] = set(); self.status = "configured"


class McpV2Service:
    """Session-owning, bounded-concurrency MCP facade with stale-lease rejection."""
    def __init__(self, *, opener: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None,
                 max_concurrency: int = 16, deadline_s: float = 120.0,
                 database_path: str = "") -> None:
        self.opener = opener or self._open; self.max_concurrency = max_concurrency
        self.deadline_s = max(0.1, float(deadline_s)); self._servers: dict[str, _Server] = {}
        self._server_locks: dict[str, asyncio.Lock] = {}
        self.database_path = os.path.abspath(database_path) if database_path else ""
        if self.database_path:
            os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
            with sqlite3.connect(self.database_path) as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS mcp_server_v2(
                  server_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL, enabled INTEGER NOT NULL,
                  generation INTEGER NOT NULL, capability_revision INTEGER NOT NULL,
                  status TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS mcp_subscription_v2(
                  server_id TEXT NOT NULL, uri TEXT NOT NULL, active INTEGER NOT NULL,
                  updated_at REAL NOT NULL, PRIMARY KEY(server_id,uri));
                CREATE TABLE IF NOT EXISTS mcp_request_v2(
                  server_id TEXT NOT NULL, request_id TEXT NOT NULL,
                  generation INTEGER NOT NULL, capability_revision INTEGER NOT NULL,
                  kind TEXT NOT NULL, name TEXT NOT NULL, method TEXT NOT NULL,
                  request_fingerprint TEXT NOT NULL, effectful INTEGER NOT NULL,
                  state TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT 'null',
                  error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                  updated_at REAL NOT NULL, PRIMARY KEY(server_id,request_id));
                """)

    def _reserve_request(
        self,
        lease: McpLease,
        *,
        request_id: str,
        method: str,
        fingerprint: str,
        effectful: bool,
        reserve: bool = True,
    ) -> tuple[str, Any]:
        if not self.database_path or not request_id:
            return "execute", None
        import time
        now = time.time()
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM mcp_request_v2 WHERE server_id=? AND request_id=?",
                (lease.server_id, request_id),
            ).fetchone()
            if row is not None:
                same = (
                    str(row["kind"]) == lease.kind
                    and str(row["name"]) == lease.name
                    and str(row["method"]) == str(method)
                    and str(row["request_fingerprint"]) == fingerprint
                )
                if not same:
                    raise McpRequestConflict(
                        "MCP request_id was reused for a different request"
                    )
                state = str(row["state"])
                if state == "succeeded":
                    return "replay", json.loads(str(row["result_json"] or "null"))
                if state in {"failed", "unknown_effect"}:
                    message = str(row["error"] or state)
                    if state == "unknown_effect":
                        raise UnknownMcpEffect(message)
                    raise McpV2Error(message)
                if bool(row["effectful"]):
                    message = (
                        "A prior MCP request crossed dispatch without a terminal "
                        "receipt; the effect will not be called again automatically."
                    )
                    conn.execute(
                        "UPDATE mcp_request_v2 SET state='unknown_effect',error=?,"
                        "updated_at=? WHERE server_id=? AND request_id=? "
                        "AND state='dispatched'",
                        (message, now, lease.server_id, request_id),
                    )
                    raise UnknownMcpEffect(message)
                return "execute", None
            if not reserve:
                return "execute", None
            conn.execute(
                "INSERT INTO mcp_request_v2(server_id,request_id,generation,"
                "capability_revision,kind,name,method,request_fingerprint,"
                "effectful,state,result_json,error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'dispatched','null','',?,?)",
                (
                    lease.server_id, request_id, int(lease.generation),
                    int(lease.capability_revision), lease.kind, lease.name,
                    str(method), fingerprint, int(effectful), now, now,
                ),
            )
        return "execute", None

    def _finish_request(
        self,
        lease: McpLease,
        *,
        request_id: str,
        fingerprint: str,
        state: str,
        result: Any = None,
        error: str = "",
    ) -> None:
        if not self.database_path or not request_id:
            return
        import time
        with sqlite3.connect(self.database_path) as conn:
            changed = conn.execute(
                "UPDATE mcp_request_v2 SET state=?,result_json=?,error=?,"
                "updated_at=? WHERE server_id=? AND request_id=? "
                "AND request_fingerprint=? AND state='dispatched'",
                (
                    str(state), _stable(_normalize(result)), str(error or "")[:4000],
                    time.time(), lease.server_id, request_id, fingerprint,
                ),
            )
            if changed.rowcount != 1:
                row = conn.execute(
                    "SELECT state,request_fingerprint FROM mcp_request_v2 "
                    "WHERE server_id=? AND request_id=?",
                    (lease.server_id, request_id),
                ).fetchone()
                if row is None or str(row[1]) != fingerprint or str(row[0]) != state:
                    raise McpRequestConflict(
                        "MCP request completion lost its durable fence"
                    )

    def _persist_server(self, server: _Server, *, enabled: bool = True) -> None:
        if not self.database_path: return
        import time
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("INSERT INTO mcp_server_v2 VALUES (?,?,?,?,?,?,?) ON CONFLICT(server_id) "
                         "DO UPDATE SET spec_json=excluded.spec_json,enabled=excluded.enabled,generation=excluded.generation,"
                         "capability_revision=excluded.capability_revision,status=excluded.status,updated_at=excluded.updated_at",
                         (server.id, _stable(server.spec), int(enabled), server.generation,
                          server.revision, server.status, time.time()))

    @staticmethod
    async def _close_stack(stack: Any) -> None:
        closing = getattr(stack, "aclose", None)
        if callable(closing):
            await closing()

    async def _close_server_owner(self, server: _Server) -> None:
        for task in list(server.requests.values()):
            task.cancel()
        await asyncio.gather(*server.requests.values(), return_exceptions=True)
        if server.stack is not None:
            await self._close_stack(server.stack)
        elif callable(getattr(server.session, "aclose", None)):
            closing = asyncio.create_task(server.session.aclose())
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    continue
            closing.result()

    async def _open(self, raw_spec: Mapping[str, Any]) -> tuple[Any, Any]:
        spec = transport_spec(raw_spec)
        ready = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own_transport() -> None:
            stack = AsyncExitStack()
            try:
                from .mcp_session import CancellableClientSession
                from mcp.client.stdio import StdioServerParameters
                from .owned_stdio import owned_stdio_client
                if spec["transport"] == "stdio":
                    command = list(spec["command"]); params = StdioServerParameters(
                        command=command[0], args=command[1:],
                        env=child_environment(dict(spec.get("env") or {}))
                    )
                    read, write = await stack.enter_async_context(owned_stdio_client(params))
                elif spec["transport"] == "streamable_http":
                    from mcp.client.streamable_http import streamablehttp_client
                    streams = await stack.enter_async_context(streamablehttp_client(str(spec["url"]), headers=dict(spec.get("headers") or {})))
                    read, write = streams[0], streams[1]
                else:
                    from mcp.client.sse import sse_client
                    read, write = await stack.enter_async_context(sse_client(str(spec["url"]), headers=dict(spec.get("headers") or {})))
                session = await stack.enter_async_context(CancellableClientSession(read, write))
                await asyncio.wait_for(
                    session.initialize(), timeout=self.deadline_s
                )
                if not ready.done():
                    ready.set_result(session)
                await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    raise
            finally:
                await stack.aclose()

        task = asyncio.create_task(
            own_transport(),
            name=f"mcp-transport-{str(spec.get('transport') or 'client')}",
        )

        class TransportOwner:
            def __init__(self) -> None:
                self._closing = False
                self._disconnect_callback = None

            def set_disconnect_callback(self, callback) -> None:
                self._disconnect_callback = callback
                if task.done() and not self._closing:
                    self.transport_done(task)

            def transport_done(self, completed: asyncio.Task[Any]) -> None:
                if self._closing or self._disconnect_callback is None:
                    return
                error = None
                if not completed.cancelled():
                    try:
                        error = completed.exception()
                    except BaseException as exc:
                        error = exc
                result = self._disconnect_callback(error)
                if inspect.isawaitable(result):
                    background_tasks.spawn(
                        result, name="mcp-transport-disconnected"
                    )

            async def aclose(self) -> None:
                self._closing = True
                stop.set()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                # Unexpected transport failure is reported through the monitor;
                # cleanup remains idempotent and must not prevent reconnect.
                with suppress(BaseException):
                    task.result()

        owner = TransportOwner()
        task.add_done_callback(owner.transport_done)
        try:
            return await asyncio.wait_for(
                asyncio.shield(ready), timeout=self.deadline_s
            ), owner
        except BaseException:
            stop.set()
            with suppress(BaseException):
                await owner.aclose()
            raise

    async def _transport_lost(
        self, server: _Server, error: BaseException | None,
    ) -> None:
        """Revoke one catalog immediately when its owning transport exits."""

        lock = self._server_locks.setdefault(server.id, asyncio.Lock())
        async with lock:
            if self._servers.get(server.id) is not server:
                return
            if server.status != "connected":
                return
            for task in tuple(server.requests.values()):
                if not task.done():
                    task.cancel()
            if server.requests:
                await asyncio.gather(
                    *tuple(server.requests.values()), return_exceptions=True
                )
            server.requests.clear()
            server.request_fingerprints.clear()
            server.request_owners.clear()
            server.catalog.clear()
            server.revision += 1
            server.status = "disconnected"
            self._persist_server(server, enabled=True)

    async def connect(self, server_id: str, spec: Mapping[str, Any]) -> dict[str, Any]:
        identifier = str(server_id or "").strip()
        if not identifier: raise McpV2Error("server_id is required")
        lock = self._server_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            prior_owner = self._servers.get(identifier)
            if prior_owner is not None:
                await self._close_server_owner(prior_owner)
                prior_owner.status = "disconnected"
                self._persist_server(prior_owner)
                if self._servers.get(identifier) is prior_owner:
                    self._servers.pop(identifier, None)
            server = _Server(identifier, transport_spec(spec), self.max_concurrency)
            if self.database_path:
                with sqlite3.connect(self.database_path) as conn:
                    prior = conn.execute("SELECT generation FROM mcp_server_v2 WHERE server_id=?", (identifier,)).fetchone()
                if prior is not None: server.generation = int(prior[0]) + 1
            try:
                opened = await asyncio.wait_for(
                    self.opener(server.spec), timeout=self.deadline_s
                )
                if isinstance(opened, tuple): server.session, server.stack = opened
                else: server.session = opened
                server.status = "connected"
                await self._refresh_server(server)
                await self._restore_subscriptions(server)
            except BaseException as exc:
                try:
                    await self._close_server_owner(server)
                except BaseException as close_error:
                    server.status = "cleanup_pending"
                    self._servers[identifier] = server
                    self._persist_server(server)
                    raise McpV2Error(
                        f"MCP connect failed and transport cleanup is pending: {close_error}"
                    ) from exc
                server.status = "error"
                self._persist_server(server)
                raise
            self._servers[identifier] = server
            monitor = getattr(server.stack, "set_disconnect_callback", None)
            if callable(monitor):
                monitor(lambda error: self._transport_lost(server, error))
            self._persist_server(server)
            return self.status(identifier)

    async def _restore_subscriptions(self, server: _Server) -> None:
        if not self.database_path: return
        with sqlite3.connect(self.database_path) as conn:
            rows = conn.execute(
                "SELECT uri FROM mcp_subscription_v2 WHERE server_id=? AND active=1 ORDER BY uri",
                (server.id,),
            ).fetchall()
        subscribe = getattr(server.session, "subscribe_resource", None)
        if not callable(subscribe): return
        for row in rows:
            uri = str(row[0])
            if ("resource", uri) not in server.catalog: continue
            try:
                await asyncio.wait_for(subscribe(uri), self.deadline_s)
                server.subscriptions.add(uri)
            except Exception:
                continue

    async def disconnect(self, server_id: str, *, disable: bool = True) -> None:
        identifier = str(server_id)
        lock = self._server_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            server = self._servers.get(identifier)
            if server is None:
                if disable and self.database_path:
                    with sqlite3.connect(self.database_path) as conn:
                        conn.execute(
                            "UPDATE mcp_server_v2 SET enabled=0,status='disconnected',"
                            "updated_at=? WHERE server_id=?",
                            (__import__("time").time(), identifier),
                        )
                return
            await self._close_server_owner(server)
            if self._servers.get(identifier) is server:
                self._servers.pop(identifier, None)
            server.status = "disconnected"
            server.catalog.clear()
            server.revision += 1
            self._persist_server(server, enabled=not disable)

    async def disconnect_all(self) -> None:
        await asyncio.gather(*(
            self.disconnect(server_id, disable=False)
            for server_id in list(self._servers)
        ))

    async def reconnect(self, server_id: str) -> dict[str, Any]:
        identifier = str(server_id)
        if not self.database_path:
            raise LookupError("MCP persistence is unavailable")
        with sqlite3.connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT spec_json,enabled FROM mcp_server_v2 WHERE server_id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise LookupError("MCP server configuration is unavailable")
        return await self.connect(identifier, json.loads(str(row[0])))

    async def remove(self, server_id: str) -> bool:
        identifier = str(server_id)
        await self.disconnect(identifier)
        if not self.database_path:
            return False
        with sqlite3.connect(self.database_path) as conn:
            changed = conn.execute(
                "UPDATE mcp_server_v2 SET enabled=0,status='removed',updated_at=? "
                "WHERE server_id=?",
                (__import__("time").time(), identifier),
            )
            conn.execute(
                "UPDATE mcp_subscription_v2 SET active=0,updated_at=? WHERE server_id=?",
                (__import__("time").time(), identifier),
            )
        return int(changed.rowcount or 0) == 1

    @staticmethod
    def _items(result: Any, field: str) -> list[Any]:
        raw = _dict(result); return list(raw.get(field) or getattr(result, field, None) or [])

    async def _list_pages(self, fn: Callable[..., Awaitable[Any]], field: str) -> list[Any]:
        output: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            result = await asyncio.wait_for(
                fn(cursor=cursor) if cursor is not None else fn(), self.deadline_s
            )
            output.extend(self._items(result, field))
            raw = _dict(result)
            next_cursor = raw.get("nextCursor", raw.get("next_cursor"))
            if not next_cursor:
                break
            cursor = str(next_cursor)
            if cursor in seen:
                raise McpV2Error("MCP pagination returned a repeated cursor")
            seen.add(cursor)
        return output

    async def _refresh_server(self, server: _Server) -> None:
        catalog: dict[tuple[str, str], dict[str, Any]] = {}
        operations = (("tool", "list_tools", "tools"), ("resource", "list_resources", "resources"),
                      ("resource_template", "list_resource_templates", "resourceTemplates"),
                      ("prompt", "list_prompts", "prompts"))
        for kind, method, field in operations:
            fn = getattr(server.session, method, None)
            if not callable(fn): continue
            try: values = await self._list_pages(fn, field)
            except Exception: continue
            for value in values:
                item = _dict(value)
                name = str(
                    (item.get("uri") or item.get("uriTemplate") or item.get("uri_template"))
                    if kind in {"resource", "resource_template"}
                    else item.get("name") or ""
                )
                if not name: continue
                schema = item.get("inputSchema") or item.get("arguments") or item
                digest = hashlib.sha256(_stable(schema).encode()).hexdigest()
                catalog[(kind, name)] = {"kind": kind, "name": name, "descriptor": item, "schema_digest": digest}
        if catalog != server.catalog: server.revision += 1
        server.catalog = catalog
        self._persist_server(server)
    
    async def refresh(self, server_id: str) -> list[dict[str, Any]]:
        server = self._require(server_id)
        await self._refresh_server(server)
        return self.catalog(server_id)

    async def capabilities_changed(self, server_id: str) -> dict[str, Any]:
        await self.refresh(server_id)
        return self.status(server_id)

    def _require(self, server_id: str) -> _Server:
        server = self._servers.get(str(server_id))
        if server is None or server.status != "connected": raise McpV2Error(f"MCP server is not connected: {server_id}")
        return server

    def catalog(self, server_id: str = "") -> list[dict[str, Any]]:
        servers = (
            [self._require(server_id)]
            if server_id
            else [
                server for server in self._servers.values()
                if server.status == "connected"
            ]
        )
        rows = []
        for server in servers:
            for item in sorted(server.catalog.values(), key=lambda x: (x["kind"], x["name"])):
                lease = McpLease(server.id, server.generation, server.revision, item["kind"], item["name"], item["schema_digest"])
                rows.append({**item, "server_id": server.id, "lease": lease.to_dict()})
        return rows

    def lease(self, server_id: str, kind: str, name: str) -> McpLease:
        server = self._require(server_id); item = server.catalog.get((kind, name))
        if item is None: raise LookupError(f"unknown MCP {kind}: {name}")
        return McpLease(server.id, server.generation, server.revision, kind, name, item["schema_digest"])

    def search(self, query: str = "", *, kind: str = "", server_id: str = "") -> list[dict[str, Any]]:
        needle = str(query or "").casefold()
        rows = self.catalog(server_id)
        return [row for row in rows if (not kind or row["kind"] == kind) and
                (not needle or needle in (str(row["name"]) + " " +
                 str(row["descriptor"].get("description") or "")).casefold())]

    def list_tools(self, server_id: str = "") -> list[dict[str, Any]]:
        return self.search(kind="tool", server_id=server_id)

    def list_resources(self, server_id: str = "", *, include_templates: bool = True) -> list[dict[str, Any]]:
        kinds = {"resource", "resource_template"} if include_templates else {"resource"}
        return [row for row in self.catalog(server_id) if row["kind"] in kinds]

    def list_prompts(self, server_id: str = "") -> list[dict[str, Any]]:
        return self.search(kind="prompt", server_id=server_id)

    def describe(self, lease: McpLease) -> dict[str, Any]:
        server = self._validate(lease); item = server.catalog[(lease.kind, lease.name)]
        return {**item, "server_id": server.id, "lease": lease.to_dict()}

    def _validate(self, lease: McpLease) -> _Server:
        server = self._require(lease.server_id); current = server.catalog.get((lease.kind, lease.name))
        if server.generation != lease.generation or server.revision != lease.capability_revision or current is None or current["schema_digest"] != lease.schema_digest:
            raise StaleMcpLease("MCP lease is stale; refresh the connector catalog")
        return server

    async def _request(self, lease: McpLease, method: str, *args: Any, request_id: str = "",
                       effect_key: str = "", progress: Callable[[Any], Any] | None = None, **kwargs: Any) -> Any:
        server = self._validate(lease); connection = server.session
        fn = getattr(connection, method, None)
        if not callable(fn): raise McpV2Error(f"server does not support {method}")
        identifier = request_id or hashlib.sha256(f"{server.id}:{method}:{time_ns()}".encode()).hexdigest()[:24]
        fingerprint = request_fingerprint(
            f"mcp.{method}",
            {
                # A lease is validated before every dispatch, but its generation
                # is a connection incarnation.  Durable idempotency must survive
                # reconnects when the exact capability schema is unchanged.
                "server_id": lease.server_id,
                "kind": lease.kind,
                "name": lease.name,
                "schema_digest": lease.schema_digest,
                "args": _normalize(args),
            },
        )
        existing = server.requests.get(identifier)
        if existing is not None:
            if server.request_fingerprints.get(identifier) != fingerprint:
                raise McpRequestConflict(
                    "MCP request_id is already active for a different request"
                )
            return await asyncio.shield(existing)
        decision, replay = self._reserve_request(
            lease,
            request_id=str(request_id or ""),
            method=method,
            fingerprint=fingerprint,
            effectful=bool(effect_key),
            reserve=False,
        )
        if decision == "replay":
            return replay
        dispatched = False
        async def dispatch():
            nonlocal dispatched
            current = self._validate(lease)
            if current is not server or current.session is not connection:
                raise StaleMcpLease("MCP connection changed while awaiting dispatch")
            active_method = getattr(current.session, method, None)
            if not callable(active_method):
                raise McpV2Error(f"server does not support {method}")
            decision, replay = self._reserve_request(
                lease, request_id=str(request_id or ""), method=method,
                fingerprint=fingerprint, effectful=bool(effect_key),
            )
            if decision == "replay":
                return replay
            dispatched = True
            return await active_method(*args, **kwargs)
        async def invoke():
            async with server.semaphore:
                lock = server.effect_locks.setdefault(effect_key, asyncio.Lock()) if effect_key else None
                if lock:
                    async with lock: return await dispatch()
                return await dispatch()

        async def invoke_and_persist():
            try:
                result = await invoke()
                if not dispatched:
                    return result
                self._finish_request(
                    lease,
                    request_id=str(request_id or ""),
                    fingerprint=fingerprint,
                    state="succeeded",
                    result=result,
                )
                return result
            except BaseException as exc:
                if not dispatched:
                    raise
                self._finish_request(
                    lease,
                    request_id=str(request_id or ""),
                    fingerprint=fingerprint,
                    state="unknown_effect" if effect_key else "failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

        task = asyncio.create_task(invoke_and_persist()); server.requests[identifier] = task
        server.request_fingerprints[identifier] = fingerprint
        invocation = current_capability_invocation()
        server.request_owners[identifier] = (
            replace(invocation.cell_origin, nested_call_id="") if invocation is not None else None, lease,
        )
        try:
            return await asyncio.wait_for(task, self.deadline_s)
        finally:
            if server.requests.get(identifier) is task:
                server.requests.pop(identifier, None)
                server.request_fingerprints.pop(identifier, None)
                server.request_owners.pop(identifier, None)

    async def call_tool(self, lease: McpLease, arguments: Mapping[str, Any], *, request_id: str = "",
                        progress: Callable[[Any], Any] | None = None) -> McpResult:
        server = self._validate(lease)
        descriptor = server.catalog[(lease.kind, lease.name)]["descriptor"]
        annotations = dict(descriptor.get("annotations") or {})
        effect_key = "" if annotations.get("readOnlyHint") is True else lease.name
        kwargs: dict[str, Any] = {}
        if progress is not None:
            try: accepts_progress = "progress_callback" in inspect.signature(server.session.call_tool).parameters
            except Exception: accepts_progress = False
            if accepts_progress:
                async def forward(*values: Any) -> None:
                    emitted = progress(values[0] if len(values) == 1 else values)
                    if inspect.isawaitable(emitted): await emitted
                kwargs["progress_callback"] = forward
        result = await self._request(lease, "call_tool", lease.name, dict(arguments),
                                     request_id=request_id, effect_key=effect_key, **kwargs)
        return preserve_content(result)

    async def read_resource(self, lease: McpLease) -> McpResult:
        return preserve_content(await self._request(lease, "read_resource", lease.name))

    async def get_prompt(self, lease: McpLease, arguments: Mapping[str, Any] | None = None) -> McpResult:
        return preserve_content(await self._request(lease, "get_prompt", lease.name, dict(arguments or {})))

    async def subscribe(self, lease: McpLease) -> dict[str, Any]:
        server = self._validate(lease); await self._request(lease, "subscribe_resource", lease.name)
        server.subscriptions.add(lease.name); self._persist_subscription(server.id, lease.name, True)
        return {"server_id": server.id, "uri": lease.name, "subscribed": True}

    async def unsubscribe(self, lease: McpLease) -> dict[str, Any]:
        server = self._validate(lease); await self._request(lease, "unsubscribe_resource", lease.name)
        server.subscriptions.discard(lease.name); self._persist_subscription(server.id, lease.name, False)
        return {"server_id": server.id, "uri": lease.name, "subscribed": False}

    def _persist_subscription(self, server_id: str, uri: str, active: bool) -> None:
        if not self.database_path: return
        import time
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("INSERT INTO mcp_subscription_v2 VALUES (?,?,?,?) ON CONFLICT(server_id,uri) "
                         "DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at",
                         (server_id, uri, int(active), time.time()))

    def configured(self) -> list[dict[str, Any]]:
        if not self.database_path: return []
        with sqlite3.connect(self.database_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM mcp_server_v2 WHERE enabled=1 ORDER BY server_id").fetchall()
        return [{"server_id": row["server_id"], "spec": json.loads(row["spec_json"]),
                 "generation": row["generation"], "capability_revision": row["capability_revision"],
                 "status": row["status"]} for row in rows]

    async def reconnect_all(self) -> dict[str, bool]:
        async def one(row):
            try: return str(row["server_id"]), bool(await self.connect(str(row["server_id"]), row["spec"]))
            except Exception: return str(row["server_id"]), False
        pairs = await asyncio.gather(*(one(row) for row in self.configured()))
        return dict(pairs)

    async def complete(self, server_id: str, reference: Mapping[str, Any], argument: Mapping[str, Any]) -> Any:
        server = self._require(server_id); fn = getattr(server.session, "complete", None)
        if not callable(fn): raise McpV2Error("server does not support completion")
        return _dict(await asyncio.wait_for(fn(dict(reference), dict(argument)), self.deadline_s))

    async def ping(self, server_id: str) -> bool:
        server = self._require(server_id); fn = getattr(server.session, "send_ping", None) or getattr(server.session, "ping", None)
        if not callable(fn): raise McpV2Error("server does not support ping")
        await asyncio.wait_for(fn(), self.deadline_s); return True

    async def set_logging_level(self, server_id: str, level: str) -> None:
        server = self._require(server_id); fn = getattr(server.session, "set_logging_level", None)
        if not callable(fn): raise McpV2Error("server does not support logging level changes")
        await asyncio.wait_for(fn(str(level)), self.deadline_s)

    async def roots_changed(self, server_id: str) -> None:
        server = self._require(server_id); fn = getattr(server.session, "send_roots_list_changed", None)
        if not callable(fn): raise McpV2Error("server does not support roots change notification")
        await asyncio.wait_for(fn(), self.deadline_s)

    def validate_cancel(self, lease: McpLease, request_id: str, *, origin: Any) -> None:
        server = self._validate(lease)
        if origin is not None:
            origin = replace(origin, nested_call_id="")
        owned = server.request_owners.get(str(request_id))
        if owned is not None and (origin is None or owned != (origin, lease)):
            raise McpV2Error("MCP request is outside the current cell or connector lease")

    async def cancel(
        self, server_id: str, request_id: str, *, lease: McpLease | None = None,
        origin: Any = None,
    ) -> bool:
        if lease is not None:
            self.validate_cancel(lease, request_id, origin=origin)
        server = self._require(server_id); task = server.requests.get(str(request_id))
        if task is None: return False
        # The SDK session propagates cancellation using the actual wire ID.
        # The caller's durable request_id is never sent as a protocol ID.
        task.cancel()
        return True

    def status(self, server_id: str) -> dict[str, Any]:
        server = self._servers.get(str(server_id))
        if server is None: return {"server_id": str(server_id), "status": "disconnected"}
        return {"server_id": server.id, "status": server.status, "transport": server.spec["transport"],
                "generation": server.generation, "capability_revision": server.revision,
                "catalog_size": len(server.catalog), "in_flight": len(server.requests),
                "subscriptions": sorted(server.subscriptions), "oauth": "not_implemented"}


def time_ns() -> int:
    import time
    return time.time_ns()


def create_mcp_v2_service(*, opener=None, max_concurrency: int = 16,
                          deadline_s: float = 120.0, database_path: str = "") -> McpV2Service:
    return McpV2Service(opener=opener, max_concurrency=max_concurrency,
                        deadline_s=deadline_s, database_path=database_path)


__all__ = ["McpLease", "McpResult", "McpV2Error", "McpV2Service", "StaleMcpLease",
           "child_environment", "command_transport_spec", "create_mcp_v2_service",
           "preserve_content", "transport_spec"]
