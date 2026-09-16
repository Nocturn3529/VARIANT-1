"""Portable MCP stdio proxy. No AppHost, model loop or terminal control lives here."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

import psutil

from .bridge_contract import MCP_TOOLS, content_result, initialize_result
from .bridge_discovery import BridgeUnavailable, resolve_bridge


def native_identity(harness):
    if harness != "grok":
        raise BridgeUnavailable("harness_unsupported", "This harness needs a native session identity adapter.")
    session_id = str(os.environ.get("GROK_SESSION_ID") or "").strip()
    if not session_id:
        raise BridgeUnavailable("native_session_unavailable", "Grok did not provide this MCP process's native session ID.")
    current = psutil.Process()
    grok = next((p for p in current.parents() if p.name().lower() in {"grok", "grok.exe"}), None)
    if grok is None:
        raise BridgeUnavailable("native_runtime_unavailable", "The native Grok runtime is not available.")
    command = grok.cmdline()
    leader_socket = ""
    if "agent" in command and "leader" in command:
        if "--leader-socket" in command:
            index = command.index("--leader-socket")
            if index + 1 < len(command):
                leader_socket = command[index + 1]
        else:
            leader_socket = str(Path(os.environ.get("GROK_HOME") or Path.home() / ".grok") / "leader.sock")
    return {"harness": harness, "native_session_id": session_id, "cwd": os.getcwd(),
        "process_id": str(current.pid), "process_started_at": current.create_time(),
        "runtime_id": f"grok:{grok.pid}:{grok.create_time():.6f}",
        "runtime_pid": grok.pid, "runtime_started_at": grok.create_time(), "leader_socket": leader_socket}


class BridgeClient:
    def __init__(self, *, profile_id, harness, directory=None, identity=None):
        self.profile_id, self.harness, self.directory = profile_id, harness, directory
        self.identity = identity
        self.connection_id = "mcp_" + uuid.uuid4().hex
        self.instance_id = ""
        self.connection_epoch = 0
        self.registration_instance = ""
        self.registration_epoch = 0
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.thread = None
        self.last_error = ""

    def _post(self, descriptor, payload, timeout=15):
        request = Request(descriptor["url"], data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + descriptor["token"]})
        with urlopen(request, timeout=timeout) as response:
            # Fifty maximum-sized messages can exceed 2 MiB after JSON escaping.
            # Keep the wire cap consistent with the public inbox limit.
            raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError("Peer response exceeds the transport limit")
            return json.loads(raw)

    def _registered(self):
        descriptor = resolve_bridge(self.profile_id, self.directory)
        if descriptor["instance_id"] != self.instance_id:
            identity = self.identity or native_identity(self.harness)
            if descriptor["instance_id"] != self.registration_instance:
                self.registration_epoch = max(self.registration_epoch, self.connection_epoch) + 1
                self.registration_instance = descriptor["instance_id"]
            for attempt in range(2):
                result = self._post(descriptor, {"operation": "register", "connection_id": self.connection_id,
                    "epoch": self.registration_epoch, **identity})
                if result.get("ok"):
                    break
                error = result.get("error") or {}
                if attempt == 0 and error.get("code") == "peer_connection_epoch_expired" and type(error.get("next_epoch")) is int:
                    self.registration_epoch = max(self.registration_epoch + 1, error["next_epoch"])
                    continue
                raise BridgeUnavailable(error.get("code", "connection_failed"), error.get("message", "Peer registration failed."))
            self.instance_id = descriptor["instance_id"]
            self.connection_epoch = result["result"]["epoch"]
        return descriptor

    def call(self, name, arguments):
        # Only registration is retried automatically. A send whose response was
        # lost must be inspected by its original request ID, never blindly replayed.
        entered = False
        try:
            with self.lock:
                descriptor = self._registered()
                epoch = self.connection_epoch
            entered = True
            result = self._post(descriptor, {"operation": "call", "connection_id": self.connection_id,
                "epoch": epoch, "name": name, "arguments": arguments})
            if result.get("ok"):
                return content_result(result.get("result"))
            return content_result(result.get("error") or {"code": "request_failed"}, error=True)
        except BridgeUnavailable as error:
            return content_result({"code": error.code, "message": str(error), "commit_state": "unknown" if entered else "not_committed"}, error=True)
        except (OSError, ValueError, HTTPError, URLError) as error:
            self.instance_id = ""
            return content_result({"code": "bridge_confirmation_unavailable", "message": str(error),
                "commit_state": "unknown" if entered else "not_committed"}, error=True)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._heartbeat, name="peer-bridge-heartbeat", daemon=True)
            self.thread.start()

    def _heartbeat(self):
        while not self.closed.is_set():
            try:
                with self.lock:
                    descriptor = self._registered()
                    epoch = self.connection_epoch
                result = self._post(descriptor, {"operation": "heartbeat", "connection_id": self.connection_id, "epoch": epoch}, timeout=3)
                if not result.get("ok"):
                    self.instance_id = ""
                    raise BridgeUnavailable((result.get("error") or {}).get("code", "heartbeat_failed"),
                        (result.get("error") or {}).get("message", "Peer heartbeat failed"))
                self.last_error = ""
            except (OSError, ValueError, BridgeUnavailable, psutil.Error) as error:
                self.instance_id = ""
                label = str(getattr(error, "code", type(error).__name__)) + ": " + str(error)
                if label != self.last_error:
                    print("VARIANT-1 peer bridge: " + label[:500], file=sys.stderr, flush=True)
                    self.last_error = label
            self.closed.wait(3)

    def close(self):
        self.closed.set()
        try:
            descriptor = resolve_bridge(self.profile_id, self.directory)
            if descriptor["instance_id"] == self.instance_id:
                self._post(descriptor, {"operation": "disconnect", "connection_id": self.connection_id, "epoch": self.connection_epoch}, timeout=2)
        except (OSError, ValueError, BridgeUnavailable):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="VARIANT-1 shared peer MCP bridge")
    parser.add_argument("--harness", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--discovery-dir")
    args = parser.parse_args(argv)
    client = BridgeClient(profile_id=args.profile_id, harness=args.harness, directory=args.discovery_dir)
    write_lock = threading.Lock()

    def write(message):
        raw = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with write_lock:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    def handle(message):
        identity = message.get("id")
        try:
            method, params = message.get("method"), message.get("params") or {}
            if method == "initialize":
                result = initialize_result(params.get("protocolVersion"))
                client.start()
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "ping":
                result = {}
            elif method == "tools/call":
                result = client.call(params.get("name"), params.get("arguments") or {})
            else:
                write({"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": "Unknown method"}})
                return
            write({"jsonrpc": "2.0", "id": identity, "result": result})
        except Exception as error:
            write({"jsonrpc": "2.0", "id": identity, "error": {"code": -32603, "message": str(error)}})

    try:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="peer-mcp") as pool:
            while True:
                line = sys.stdin.buffer.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    raise ValueError("MCP frame exceeded 1 MiB")
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("MCP requires object messages")
                except ValueError:
                    write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
                    continue
                if message.get("id") is not None:
                    pool.submit(handle, message)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
