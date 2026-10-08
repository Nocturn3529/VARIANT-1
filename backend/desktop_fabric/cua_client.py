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
import sys
import threading
from typing import Any, Mapping


class CuaDriverError(RuntimeError):
    """The cua-driver process failed or returned an MCP error."""


# The driver is a third-party binary: it gets only what it needs to find the
# desktop session and its own files, never VARIANT-1's provider keys or other
# secrets from the backend environment.
_DRIVER_ENV_KEYS = frozenset({
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "USER", "USERNAME", "LOGNAME", "LANG", "LANGUAGE", "TZ",
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "CUA_DRIVER_RS_ENABLE_WAYLAND",
})
_DRIVER_ENV_PREFIXES = ("LC_",)


def cua_driver_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allowlisted child environment with upstream telemetry turned off.

    cua-driver sends content-free usage telemetry by default. VARIANT-1 turns
    it off unless the user sets ``VARIANT1_CUA_TELEMETRY=1``.
    """

    source = dict(os.environ if base is None else base)
    env = {
        key: value for key, value in source.items()
        if key.upper() in _DRIVER_ENV_KEYS
        or key.upper().startswith(_DRIVER_ENV_PREFIXES)
    }
    if str(source.get("VARIANT1_CUA_TELEMETRY") or "").strip() != "1":
        env["DO_NOT_TRACK"] = "1"
        env["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
    return env


def bundled_cua_driver_path() -> str | None:
    """The copy setup installs under bin/cua-driver, not a Hermes or Codex install."""

    from paths import APP_ROOT

    root = os.path.join(APP_ROOT, "bin", "cua-driver")
    if sys.platform == "darwin":
        # macOS ships trycua's signed app bundle, never a bare binary.
        candidate = os.path.join(root, "CuaDriver.app", "Contents", "MacOS", "cua-driver")
    else:
        candidate = os.path.join(root, "cua-driver.exe" if os.name == "nt" else "cua-driver")
    if os.path.isfile(candidate):
        return candidate
    return None


def pinned_cua_driver_version(binary: str) -> str:
    """The VERSION stamp setup writes next to the bundled driver, if any."""

    path = os.path.abspath(binary)
    bundle, marker, _ = path.partition(os.path.join(".app", "Contents", "MacOS", ""))
    # Inside CuaDriver.app the stamp sits next to the bundle.
    directory = os.path.dirname(bundle + ".app") if marker else os.path.dirname(path)
    stamp = os.path.join(directory, "VERSION")
    try:
        with open(stamp, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


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
    """MCP ``2025-06-18`` over newline-delimited JSON-RPC on stdio.

    Requests are serialized: one request owns the pipe until its response
    arrives, so concurrent callers can never consume each other's replies.
    """

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
        self.env = cua_driver_env() if env is None else dict(env)
        self.timeout_s = float(timeout_s)
        self.server_info: dict[str, Any] = {}
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._request_lock = threading.RLock()
        self._stderr_lock = threading.Lock()
        self._stderr_text = ""
        self._id = 0
        self._closed = False

    def open(self) -> None:
        if self._process is not None:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                creationflags=flags,
            )
        except OSError as exc:
            raise CuaDriverError(f"could not start cua-driver: {exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name="cua-driver-mcp", daemon=True,
        )
        self._reader.start()
        threading.Thread(
            target=self._read_stderr, name="cua-driver-mcp-err", daemon=True,
        ).start()
        result = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "variant1", "version": "0.1"},
        })
        info = result.get("serverInfo") if isinstance(result, dict) else None
        self.server_info = dict(info) if isinstance(info, dict) else {}
        self._notify("notifications/initialized", {})

    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

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

    def list_tools(self) -> list[str]:
        result = self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return [str(tool.get("name")) for tool in tools or () if isinstance(tool, dict)]

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
        with self._request_lock:
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
        with self._request_lock:
            self._write({
                "jsonrpc": "2.0",
                "method": method,
                "params": dict(params),
            })

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CuaDriverError("cua-driver is not running")
        # MCP stdio is one JSON-RPC message per line. Content-Length framing
        # is not a message, so cua-driver 0.28 never answers it.
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            process.stdin.write(body + b"\n")
            process.stdin.flush()
        except OSError as exc:
            raise CuaDriverError(f"cua-driver stopped accepting requests{self._stderr_suffix()}") from exc

    def _stderr_suffix(self) -> str:
        with self._stderr_lock:
            detail = self._stderr_text.strip()
        return f": {detail}" if detail else ""

    def _next(self, timeout_s: float) -> dict[str, Any]:
        try:
            message = self._messages.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise CuaDriverError(f"cua-driver timed out{self._stderr_suffix()}") from exc
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
                line = stream.readline()
                if not line:
                    self._messages.put(None)
                    return
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                payload = json.loads(text)
                if isinstance(payload, dict):
                    self._messages.put(payload)
        except Exception:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        process = self._process
        stream = process.stderr if process is not None else None
        if stream is None:
            return
        kept = bytearray()
        try:
            while not self._closed:
                chunk = stream.read(1024)
                if not chunk:
                    break
                kept.extend(chunk)
                if len(kept) > 4000:
                    del kept[:-4000]
                with self._stderr_lock:
                    self._stderr_text = kept.decode("utf-8", "replace")
        except Exception:
            return


def unwrap_tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Structured tool output, plus the first image the driver returned.

    Screenshots arrive as a separate MCP ``image`` content item, so they are
    attached as ``_image_base64`` / ``_image_mime_type``.
    """

    unwrapped: dict[str, Any] | None = None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        unwrapped = dict(structured)
    else:
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
                unwrapped = parsed
                break
    if unwrapped is None:
        unwrapped = {
            key: value for key, value in result.items() if key != "content"
        }
    for item in result.get("content") or ():
        if isinstance(item, dict) and item.get("type") == "image" and item.get("data"):
            unwrapped.setdefault("_image_base64", str(item.get("data")))
            unwrapped.setdefault("_image_mime_type", str(item.get("mimeType") or "image/png"))
            break
    return unwrapped


def _error_text(result: Mapping[str, Any]) -> str:
    for item in result.get("content") or ():
        if isinstance(item, dict) and item.get("text"):
            return str(item.get("text"))[:500]
    return ""


__all__ = [
    "CuaDriverClient",
    "CuaDriverError",
    "cua_driver_env",
    "pinned_cua_driver_version",
    "resolve_cua_driver_command",
    "unwrap_tool_result",
]
