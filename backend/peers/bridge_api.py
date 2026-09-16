"""HTTP transport from portable MCP clients into the canonical peer service."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
import psutil

from .bridge_contract import call_peer_tool
from .bridge_discovery import PublishedBridge, process_matches


def publish_bridge(host, url, directory=None):
    previous = getattr(host, "peer_bridge_publication", None)
    if previous is not None:
        previous.close()
    host.peer_backend_url = url
    host.peer_bridge_token = secrets.token_hex(24)
    host.peer_bridge_publication = PublishedBridge(data_dir=host.data_dir, url=url,
        token=host.peer_bridge_token, directory=directory)
    return host.peer_bridge_publication


def _identity(payload):
    for key in ("connection_id", "harness", "native_session_id", "process_id", "runtime_id"):
        if not isinstance(payload.get(key), str) or not payload[key].strip() or len(payload[key]) > 512:
            raise ValueError(f"Invalid connection {key}")
    if payload["harness"] != "grok":
        raise ValueError("No native identity adapter is installed for this harness")
    if not process_matches(payload["process_id"], payload.get("process_started_at")):
        raise ValueError("The MCP connection process is no longer live")
    if not process_matches(payload.get("runtime_pid"), payload.get("runtime_started_at")):
        raise ValueError("The native harness process is no longer live")
    ancestors = psutil.Process(int(payload["process_id"])).parents()
    runtime_pid = int(payload["runtime_pid"])
    runtime = next((p for p in ancestors if p.pid == runtime_pid and p.name().lower() in {"grok", "grok.exe"}), None)
    if runtime is None:
        raise ValueError("The MCP process does not belong to the declared Grok runtime")
    expected = f"grok:{runtime_pid}:{float(payload['runtime_started_at']):.6f}"
    if payload["runtime_id"] != expected:
        raise ValueError("The runtime generation differs from its process evidence")
    # Derive a leader endpoint only from the proven runtime's own evidence.
    command = runtime.cmdline()
    leader_socket = ""
    if "agent" in command and "leader" in command:
        if "--leader-socket" in command:
            at = command.index("--leader-socket")
            leader_socket = command[at + 1] if at + 1 < len(command) else ""
        else:
            # Derive the native default from the proven runtime process, not
            # from the connecting MCP client's suggested endpoint.
            try:
                environment = runtime.environ()
            except psutil.Error:
                environment = None  # Inbox remains usable without ACP attachment.
            if environment is not None:
                home = environment.get("GROK_HOME")
                if not home:
                    home = str(Path(environment.get("USERPROFILE") or environment.get("HOME") or Path.home()) / ".grok")
                leader_socket = str(Path(home) / "leader.sock")
    return {"cwd": str(payload.get("cwd") or ""), "leader_socket": leader_socket,
        "delivery_mode": "inbox", "automatic_wake_available": bool(leader_socket)}


def register_bridge_routes(app, host):
    @app.post("/peers/bridge")
    async def peer_bridge(request: Request):
        token = str(getattr(host, "peer_bridge_token", ""))
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if str(getattr(request.client, "host", "")) not in {"127.0.0.1", "::1"} or not token or not secrets.compare_digest(token, supplied):
            return JSONResponse({"ok": False, "error": {"code": "unauthorized"}}, status_code=401)
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            return JSONResponse({"ok": False, "error": {"code": "request_too_large"}}, status_code=413)
        entered = False
        try:
            import json
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Object request required")
            service = host.require_runtime().peers
            from .grok import get_grok_integration
            grok = get_grok_integration(host)
            operation = payload.get("operation")
            if getattr(grok, "closed", False) and operation != "disconnect":
                raise ValueError("The peer service is shutting down")
            identity = str(payload.get("connection_id") or "")
            if operation == "register":
                metadata = _identity(payload)
                epoch = payload.get("epoch")
                if type(epoch) is not int or epoch < 1:
                    raise ValueError("Invalid connection epoch")
                service.expire_connections()
                previous = service.repository.get_connection(identity)
                if previous and previous.get("status") in {"closed", "expired"} and epoch <= previous["epoch"]:
                    return JSONResponse({"ok": False, "error": {"code": "peer_connection_epoch_expired",
                        "message": "The previous connection lease ended.", "next_epoch": previous["epoch"] + 1,
                        "commit_state": "not_committed"}})
                connection = service.register_connection(identity, payload["harness"], payload["native_session_id"],
                    display_name="Grok Build", process_id=payload["process_id"], process_started_at=payload["process_started_at"],
                    runtime_id=payload["runtime_id"], runtime_pid=str(payload["runtime_pid"]),
                    runtime_started_at=payload["runtime_started_at"], epoch=epoch, lease_seconds=15,
                    capabilities={"structured_replies": True, "live_ingress": False,
                        "automatic_wake_available": bool(metadata["leader_socket"]),
                        "busy_message_queueing": False, "native_agent_origin": False}, metadata=metadata)
                grok.note_connection(connection)
                value = connection
            else:
                connection = service.get_connection(identity)
                epoch = payload.get("epoch")
                if type(epoch) is not int or epoch != connection["epoch"]:
                    raise ValueError("The MCP request belongs to an earlier connection epoch")
                if operation == "disconnect":
                    value = service.close_connection(identity, connection["epoch"], reason="MCP process closed")
                    grok.note_connection(value)
                else:
                    if not process_matches(connection["process_id"], connection["process_started_at"]):
                        raise ValueError("The MCP process is no longer live")
                    connection = service.touch_connection(identity, connection["epoch"])
                    if operation == "heartbeat":
                        value = connection
                    elif operation == "call":
                        if connection["status"] == "conflicted" and payload.get("name") in {"peers_send", "peers_reply"}:
                            from .service import PeerError
                            raise PeerError("peer_connection_conflicted", "This native session has multiple live runtimes. Resolve the competing connections before sending; inbox inspection remains available.")
                        entered = True
                        value = await call_peer_tool(service, connection["peer_id"], payload.get("name"),
                            payload.get("arguments") or {}, connection=grok.connection_status(connection))
                    else:
                        raise ValueError("Unknown bridge operation")
            return JSONResponse({"ok": True, "result": value})
        except Exception as error:
            return JSONResponse({"ok": False, "error": {"code": str(getattr(error, "code", "bridge_request_failed")),
                "message": str(error), "commit_state": str(getattr(error, "commit_state", "unknown" if entered else "not_committed"))}})
