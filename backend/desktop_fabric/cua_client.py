"""Stdio MCP client for a pinned cua-driver process.

The driver is an external program. This module does not vendor its
accessibility or input implementation.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from typing import Any, Mapping


class CuaDriverError(RuntimeError):
    """The cua-driver process failed or returned an MCP error."""


def bundled_cua_driver_path() -> str | None:
    """The copy setup installs under bin/cua-driver, not a Hermes or Codex install."""

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    name = "cua-driver.exe" if os.name == "nt" else "cua-driver"
    candidate = os.path.join(root, "bin", "cua-driver", name)
    if os.path.isfile(candidate):
        return candidate
    return None


def resolve_cua_driver_command() -> list[str] | None:
    """Return ``[binary, "mcp"]`` or None when the driver is not enabled."""

    override = os.environ.get("VARIANT1_CUA_DRIVER", "").strip()
    if override.lower() in {"0", "off", "false", "disabled"}:
        return None
    if override:
        return [override, "mcp"]
    bundled = bundled_cua_driver_path()
    if bundled:
        return [bundled, "mcp"]
    found = shutil.which("cua-driver") or shutil.which("cua-driver.exe")
    if not found:
        return None
    return [found, "mcp"]


class CuaDriverClient:
    """Legacy MCP ``2025-06-18`` over Content-Length framed stdio."""

    def __init__(
        self,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        if not command or not command[0]:
            raise CuaDriverError("cua-driver command is empty")
        self.command = list(command)
        self.env = dict(os.environ if env is None else env)
        self.timeout_s = float(timeout_s)
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._id = 0
        self._closed = False

    def open(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
            )
        except OSError as exc:
            raise CuaDriverError(f"could not start cua-driver: {exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name="cua-driver-mcp", daemon=True,
        )
        self._reader.start()
        self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "variant1", "version": "0.1"},
        })
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        self._closed = True
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = self._request("tools/call", {
            "name": name,
            "arguments": dict(arguments),
        })
        if not isinstance(result, dict):
            raise CuaDriverError(f"cua-driver tool {name} returned {type(result).__name__}")
        if result.get("isError"):
            raise CuaDriverError(_error_text(result) or f"cua-driver tool {name} failed")
        return unwrap_tool_result(result)

    def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        with self._write_lock:
            self._id += 1
            message_id = self._id
            self._write({
                "jsonrpc": "2.0",
                "id": message_id,
                "method": method,
                "params": dict(params),
            })
        while True:
            message = self._next(self.timeout_s)
            if message.get("id") != message_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                detail = error.get("message") if isinstance(error, dict) else error
                raise CuaDriverError(f"cua-driver {method} failed: {detail}")
            return message.get("result")

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        with self._write_lock:
            self._write({
                "jsonrpc": "2.0",
                "method": method,
                "params": dict(params),
            })

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CuaDriverError("cua-driver is not running")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        process.stdin.write(header + body)
        process.stdin.flush()

    def _next(self, timeout_s: float) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise CuaDriverError("cua-driver timed out") from exc
        if message is None:
            raise CuaDriverError("cua-driver closed the connection")
        return message

    def _read_loop(self) -> None:
        process = self._process
        stream = process.stdout if process is not None else None
        if stream is None:
            self._messages.put(None)
            return
        try:
            while not self._closed:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        self._messages.put(None)
                        return
                    if line in {b"\r\n", b"\n"}:
                        break
                    decoded = line.decode("ascii", "replace")
                    if ":" not in decoded:
                        continue
                    key, value = decoded.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
                length = int(headers.get("content-length") or "0")
                if length <= 0:
                    continue
                body = stream.read(length)
                if len(body) < length:
                    self._messages.put(None)
                    return
                payload = json.loads(body.decode("utf-8"))
                if isinstance(payload, dict):
                    self._messages.put(payload)
        except Exception:
            self._messages.put(None)


def unwrap_tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return dict(structured)
    for item in result.get("content") or ():
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return dict(result)


def _error_text(result: Mapping[str, Any]) -> str:
    for item in result.get("content") or ():
        if isinstance(item, dict) and item.get("text"):
            return str(item.get("text"))[:500]
    return ""


__all__ = [
    "CuaDriverClient",
    "CuaDriverError",
    "resolve_cua_driver_command",
    "unwrap_tool_result",
]
