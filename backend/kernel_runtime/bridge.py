"""Host-owned authenticated RPC endpoint for one kernel generation."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core_invariants import request_fingerprint
from .bridge_protocol import (
    BRIDGE_HANDSHAKE,
    BRIDGE_SCHEMA,
    DEFAULT_MAX_FRAME_BYTES,
    BridgeProtocolError,
    UserWaitSignal,
    read_async_frame,
    sign_envelope,
    verify_envelope,
    write_async_response,
    write_async_frame,
)
from .wire_values import pack_value


InvokeHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class _ActiveRequest:
    cancellation: asyncio.Event
    execution_id: str
    outer_tool_call_id: str
    fingerprint: str
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    user_wait: UserWaitSignal = field(default_factory=UserWaitSignal)


class KernelBridgeServer:
    """Loopback transport with handshake, live correlation, and bounded ACKs."""

    def __init__(
        self,
        *,
        secret: bytes,
        nonce: str,
        generation: int,
        invoke_handler: InvokeHandler,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        cancellation_capacity: int = 2048,
        max_in_flight: int = 8,
        invoke_handler_limits_concurrency: bool = False,
        wait_heartbeat_s: float = 20.0,
    ) -> None:
        self.secret = bytes(secret)
        self.nonce = str(nonce)
        self.generation = int(generation)
        self.invoke_handler = invoke_handler
        self.max_frame_bytes = max(1024, int(max_frame_bytes))
        self.cancellation_capacity = max(64, int(cancellation_capacity))
        self.max_in_flight = max(1, min(int(max_in_flight), 64))
        self.invoke_handler_limits_concurrency = bool(invoke_handler_limits_concurrency)
        self.wait_heartbeat_s = max(0.01, min(float(wait_heartbeat_s), 20.0))
        self._server: asyncio.AbstractServer | None = None
        self._handshaken = False
        self._invoke_slots = asyncio.Semaphore(self.max_in_flight)
        self._active: dict[str, _ActiveRequest] = {}
        self._pending_cancellations: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._active_lock = asyncio.Lock()
        self._connections: set[asyncio.StreamWriter] = set()
        self._closing = False

    @property
    def endpoint(self) -> tuple[str, int]:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("kernel bridge has not started")
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    @property
    def handshaken(self) -> bool:
        return self._handshaken

    async def start(self) -> tuple[str, int]:
        if self._server is None:
            self._closing = False
            self._server = await asyncio.start_server(
                self._handle,
                host="127.0.0.1",
                port=0,
                limit=self.max_frame_bytes + 4,
            )
        return self.endpoint

    async def close(self) -> None:
        self._closing = True
        async with self._active_lock:
            for active in self._active.values():
                active.cancellation.set()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        connections = tuple(self._connections)
        for writer in connections:
            writer.close()
        if connections:
            await asyncio.gather(
                *(writer.wait_closed() for writer in connections),
                return_exceptions=True,
            )

    async def cancel_execution(self, execution_id: str, *, timeout_s: float) -> bool:
        """Cancel and settle this cell's admitted calls before kernel reuse."""
        async with self._active_lock:
            active = tuple(
                request for request in self._active.values()
                if request.execution_id == str(execution_id)
            )
            for request in active:
                request.cancellation.set()
        if not active:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*(request.settled.wait() for request in active)),
                timeout=max(0.001, float(timeout_s)),
            )
        except asyncio.TimeoutError:
            return False
        return True

    async def _cancel_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str(request.get("request_id") or "")
        target_id = str(request.get("target_request_id") or "")
        execution_id = str(request.get("execution_id") or "")
        outer_call_id = str(request.get("outer_tool_call_id") or "")
        if not target_id or len(target_id) > 160:
            raise BridgeProtocolError("bridge cancellation target is invalid")
        if not execution_id or not outer_call_id:
            raise BridgeProtocolError("bridge cancellation origin is missing")
        active_now = False
        async with self._active_lock:
            active = self._active.get(target_id)
            if active is not None:
                if (
                    active.execution_id != execution_id
                    or active.outer_tool_call_id != outer_call_id
                ):
                    return self._response(
                        request_id,
                        ok=False,
                        error={
                            "code": "cancel_origin_mismatch",
                            "message": "Cancellation origin does not own the target request.",
                        },
                    )
                active.cancellation.set()
                active_now = True
            else:
                # The cancellation connection can win the loopback race against
                # the original invoke connection. Retain a bounded tombstone so
                # the target is cancelled before broker dispatch when it arrives.
                prior = self._pending_cancellations.get(target_id)
                if prior is not None and prior != (execution_id, outer_call_id):
                    return self._response(request_id, ok=False, error={
                        "code": "cancel_origin_mismatch",
                        "message": "Cancellation origin differs from the pending request.",
                    })
                if prior is None and len(self._pending_cancellations) >= self.cancellation_capacity:
                    return self._response(request_id, ok=False, error={
                        "code": "cancel_capacity_exhausted",
                        "message": "Pending cancellation capacity is full; cancellation was not accepted.",
                    })
                self._pending_cancellations[target_id] = (
                    execution_id,
                    outer_call_id,
                )
                self._pending_cancellations.move_to_end(target_id)
        return self._response(
            request_id,
            ok=True,
            target_request_id=target_id,
            active=active_now,
        )

    async def forget_execution(self, execution_id: str) -> None:
        """Retire pre-admission cancels once the kernel rejects this execution."""
        async with self._active_lock:
            for key, origin in list(self._pending_cancellations.items()):
                if origin[0] == execution_id:
                    self._pending_cancellations.pop(key, None)

    def _response(self, request_id: str, **fields: Any) -> dict[str, Any]:
        return sign_envelope(self.secret, {
            "schema": BRIDGE_SCHEMA,
            "nonce": self.nonce,
            "generation": self.generation,
            "request_id": str(request_id or ""),
            **fields,
        })

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str(request.get("request_id") or "")
        if not request_id or len(request_id) > 160:
            raise BridgeProtocolError("bridge request ID is invalid")
        if int(request.get("generation") or -1) != self.generation:
            raise BridgeProtocolError("bridge kernel generation is stale")
        operation = str(request.get("op") or "")
        if operation == "handshake":
            if str(request.get("handshake") or "") != BRIDGE_HANDSHAKE:
                raise BridgeProtocolError("kernel bridge handshake is unsupported")
            self._handshaken = True
            return self._response(
                request_id,
                ok=True,
                handshake=BRIDGE_HANDSHAKE,
            )
        if operation not in {"invoke", "invoke_many", "cancel"}:
            raise BridgeProtocolError("kernel bridge operation is unsupported")
        if not self._handshaken:
            raise BridgeProtocolError("kernel bridge handshake is incomplete")
        if operation == "cancel":
            return await self._cancel_request(request)

        fingerprint = request_fingerprint(operation, {
            key: value for key, value in request.items()
            if key not in {"mac", "request_id", "op"}
        })
        execution_id = str(request.get("execution_id") or "")
        outer_call_id = str(request.get("outer_tool_call_id") or "")
        cancellation = asyncio.Event()
        async with self._active_lock:
            active = self._active.get(request_id)
            if active is not None:
                return self._response(
                    request_id,
                    ok=False,
                    error={
                        "code": (
                            "duplicate_request_in_flight"
                            if active.fingerprint == fingerprint
                            else "duplicate_request_conflict"
                        ),
                        "message": (
                            "Bridge request ID is already in flight."
                            if active.fingerprint == fingerprint
                            else "Bridge request ID conflicts with the active payload."
                        ),
                    },
                )
            pending = self._pending_cancellations.get(request_id)
            if pending is not None and pending != (execution_id, outer_call_id):
                return self._response(request_id, ok=False, error={
                    "code": "cancel_origin_mismatch", "message": "Request ID has a cancellation owned by another execution.",
                })
            self._pending_cancellations.pop(request_id, None)
            if pending == (execution_id, outer_call_id) or self._closing:
                cancellation.set()
            active = _ActiveRequest(
                cancellation=cancellation,
                execution_id=execution_id,
                outer_tool_call_id=outer_call_id,
                fingerprint=fingerprint,
            )
            self._active[request_id] = active
        admitted_request = dict(request)
        admitted_request["_bridge_cancel_event"] = cancellation
        admitted_request["_bridge_user_wait"] = active.user_wait
        try:
            # A kernel lease classifies owned controls before its bounded gate.
            # Applying a second limit here can trap cancel behind its target.
            async with (nullcontext() if self.invoke_handler_limits_concurrency else self._invoke_slots):
                response_fields = await self.invoke_handler(admitted_request)
        finally:
            active.settled.set()
            async with self._active_lock:
                self._active.pop(request_id, None)
        fields = dict(response_fields or {})
        for key in ('result', 'results'):
            if key in fields:
                fields[key] = pack_value(fields[key])
        return self._response(request_id, **fields)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request_id = ""
        dispatch = None
        self._connections.add(writer)
        try:
            raw = await asyncio.wait_for(
                read_async_frame(reader, max_bytes=self.max_frame_bytes),
                timeout=5.0,
            )
            request = verify_envelope(
                self.secret,
                raw,
                expected_nonce=self.nonce,
            )
            request_id = str(request.get("request_id") or "")
            dispatch = asyncio.create_task(self._dispatch(request))
            last_wait_notice = 0.0
            while not dispatch.done():
                await asyncio.wait({dispatch}, timeout=min(0.1, self.wait_heartbeat_s))
                if reader.at_eof() and not dispatch.done():
                    # A cancelled awaitable closes its response socket before
                    # its separate cancel frame can arrive. Preserve nested-
                    # call ownership by using the same cooperative signal;
                    # cancelling dispatch here would misclassify it as a
                    # parent/run cancellation.
                    abandoned = self._active.get(request_id)
                    if abandoned is not None:
                        abandoned.cancellation.set()
                active = self._active.get(request_id)
                now = asyncio.get_running_loop().time()
                # Liveness is independent of why a capability is still pending.
                # Healthy commands and queued calls must outlive the transport's
                # idle window, just like a call awaiting a user answer.
                if active is not None and now - last_wait_notice >= self.wait_heartbeat_s:
                    await asyncio.wait_for(write_async_frame(
                        writer, self._response(
                            request_id, in_flight=True,
                            waiting_for_user=active.user_wait.is_set(),
                        ),
                        max_bytes=self.max_frame_bytes,
                    ), timeout=5.0)
                    last_wait_notice = now
            response = await dispatch
        except asyncio.CancelledError:
            if dispatch is not None and not dispatch.done():
                dispatch.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await dispatch
            self._connections.discard(writer)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            raise
        except Exception as exc:
            response = self._response(
                request_id,
                ok=False,
                error={
                    "code": (
                        "bridge_protocol_error"
                        if isinstance(exc, BridgeProtocolError)
                        else "bridge_host_error"
                    ),
                    "message": str(exc) or type(exc).__name__,
                },
            )
        try:
            with suppress(
                OSError,
                ConnectionError,
                TimeoutError,
                asyncio.CancelledError,
            ):
                await write_async_response(
                    writer, response, secret=self.secret,
                    max_bytes=self.max_frame_bytes,
                )
        finally:
            if dispatch is not None and not dispatch.done():
                dispatch.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await dispatch
            self._connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
