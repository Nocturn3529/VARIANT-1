"""Embedded-browser driver RPC for the Browser core.

The Electron renderer registers the one browser host WebSocket. Normal chat
Browser core adapters send commands to that socket and wait for a correlated result while the
WebSocket receive loop remains free. Background workers never use this bridge;
they receive an isolated Playwright backend instead.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any


class BrowserHostUnavailable(RuntimeError):
    """Raised when no live Main Deck browser host is registered."""


class BrowserHostBroker:
    def __init__(self) -> None:
        self._host: Any = None
        self._pending: dict[str, tuple[Any, asyncio.Future]] = {}

    @property
    def available(self) -> bool:
        return self._host is not None

    async def register(self, websocket: Any) -> None:
        prior = self._host
        self._host = websocket
        if prior is not None and prior is not websocket:
            self._fail_for_host(prior, BrowserHostUnavailable(
                "Main Deck browser host was replaced"))

    def unregister(self, websocket: Any) -> None:
        if self._host is websocket:
            self._host = None
        self._fail_for_host(websocket, BrowserHostUnavailable(
            "Main Deck browser host disconnected"))

    def _fail_for_host(self, websocket: Any, error: Exception) -> None:
        for request_id, (owner, future) in list(self._pending.items()):
            if owner is not websocket:
                continue
            self._pending.pop(request_id, None)
            if not future.done():
                future.set_exception(error)

    async def request(self, command: dict, *, timeout: float = 35.0) -> dict:
        host = self._host
        if host is None:
            raise BrowserHostUnavailable(
                "The visible VARIANT-1 browser is unavailable. Open the Main Deck and retry."
            )
        request_id = secrets.token_hex(12)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (host, future)
        try:
            await host.send_json({
                "type": "browser:host:command",
                "id": request_id,
                "command": dict(command or {}),
            })
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("visible browser command timed out") from exc
        finally:
            self._pending.pop(request_id, None)
        if not isinstance(result, dict):
            raise RuntimeError("visible browser returned an invalid result")
        if not result.get("ok", False):
            raise RuntimeError(str(result.get("error") or "visible browser command failed"))
        return result

    def resolve(self, websocket: Any, request_id: str, result: Any) -> bool:
        row = self._pending.get(str(request_id or ""))
        if row is None:
            return False
        owner, future = row
        if owner is not websocket or future.done():
            return False
        future.set_result(result if isinstance(result, dict) else {
            "ok": False,
            "error": "invalid browser host result",
        })
        return True


BROKER = BrowserHostBroker()


async def register_host(websocket: Any) -> None:
    await BROKER.register(websocket)


def unregister_host(websocket: Any) -> None:
    BROKER.unregister(websocket)


def resolve_host_result(websocket: Any, request_id: str, result: Any) -> bool:
    return BROKER.resolve(websocket, request_id, result)


async def request_host(command: dict, *, timeout: float = 35.0) -> dict:
    return await BROKER.request(command, timeout=timeout)
