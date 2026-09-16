"""WebSocket surface for multi-terminal and structured-process execution."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from execution_hosts.models import ExecutionOwner
from project_context import chat_project_context
from work_fabric.scope import WorkScope
from ws_protocol import CorrelatedResponder, request_id as _request_id


def _runtime(srv: Any) -> Any:
    runtime = getattr(srv.require_runtime(), "execution", None)
    if runtime is None:
        raise RuntimeError("execution runtime is unavailable")
    return runtime


class ExecutionChatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


def _chat_id(srv: Any, session: Any, message: Any) -> str:
    del session
    requested = str((message or {}).get("chat_id") or "").strip()
    if not requested:
        raise ExecutionChatError(
            "execution_chat_id_required", "chat_id is required",
        )
    sessions = getattr(srv.require_runtime(), "sessions", None)
    has_session = getattr(sessions, "has_session", None)
    if not callable(has_session):
        raise ExecutionChatError(
            "execution_chat_registry_unavailable",
            "durable chat registry is unavailable",
        )
    if not has_session(requested):
        raise ExecutionChatError(
            "execution_chat_unknown", "execution chat does not exist",
        )
    return requested


def _scope(srv: Any, session: Any, message: Any) -> WorkScope:
    return WorkScope(
        chat_id=_chat_id(srv, session, message),
    )


def _owner(srv: Any, session: Any, message: Any) -> ExecutionOwner:
    scope = _scope(srv, session, message)
    if scope.chat_id:
        return ExecutionOwner("chat", scope.chat_id, scope)
    raise ValueError("no active chat owner")


def _cwd(srv: Any, chat_id: str, value: Any) -> str:
    supplied = str(value or "").strip()
    if supplied:
        path = os.path.realpath(os.path.abspath(os.path.expanduser(supplied)))
        if not os.path.isdir(path):
            raise ValueError(f"cwd is not a directory: {path}")
        return path
    fallback = chat_project_context(srv, chat_id).cwd
    if not os.path.isdir(fallback):
        raise ValueError("cwd is ambiguous")
    return fallback


_terminal_response = CorrelatedResponder(
    family="terminal",
    schema="variant1.execution-command.v1",
)
_process_response = CorrelatedResponder(
    family="process",
    schema="variant1.execution-command.v1",
)


def _execution_snapshot(runtime, chat_id, msg):
    """Read the durable registry off the WebSocket event loop."""
    terminals = [row.to_dict() for row in
                 runtime.repository.list_terminals_for_chat(chat_id, limit=200)]
    processes = [row.to_dict() for row in
                 runtime.repository.list_processes_for_chat(chat_id, limit=200)]
    events = runtime.repository.list_events_for_chat(
        chat_id, after_sequence=max(0, int(msg.get("after_sequence") or 0)),
        limit=max(1, min(int(msg.get("limit") or 200), 500)),
    )
    return terminals, processes, events


def _execution_status(service, identity, scope):
    return {**service.get(identity, scope=scope).to_dict(),
            "input_writes": service.input_status(identity)}


def register(on):
    @on("execution:get")
    async def execution_get(srv, websocket, session, msg):
        try:
            chat_id = await asyncio.to_thread(_chat_id, srv, session, msg)
        except Exception as exc:
            await websocket.send_json({
                "type": "execution:rejected",
                "schema": "variant1.execution-snapshot.v1",
                "request_id": _request_id(msg),
                "chat_id": str(msg.get("chat_id") or "").strip(),
                "reason_code": str(getattr(exc, "code", type(exc).__name__)),
                "error": str(exc),
            })
            return
        runtime = _runtime(srv)
        scope = WorkScope(chat_id=chat_id)
        terminals, processes, events = await asyncio.to_thread(
            _execution_snapshot, runtime, chat_id, msg,
        )
        await websocket.send_json({
            "type": "execution:snapshot",
            "schema": "variant1.execution-snapshot.v1",
            "request_id": _request_id(msg),
            "chat_id": chat_id,
            "scope": scope.to_dict(),
            "terminals": terminals,
            "processes": processes,
            "events": [row.to_dict() for row in events],
            "cursor": int(events[-1].sequence) if events else max(
                0, int(msg.get("after_sequence") or 0)
            ),
            "recovery": {
                "terminals": [row['id'] for row in terminals
                              if row['id'] in runtime.recovery_report.get('terminals', ())],
                "processes": [row['id'] for row in processes
                              if row['id'] in runtime.recovery_report.get('processes', ())],
            },
        })

    @on("terminal:open")
    async def terminal_open(srv, websocket, session, msg):
        async def action():
            chat_id = _chat_id(srv, session, msg)
            record = await _runtime(srv).open_terminal(
                owner=_owner(srv, session, msg),
                cwd=_cwd(srv, chat_id, msg.get("cwd")),
                profile=str(msg.get("profile") or ""),
                cols=int(msg.get("cols") or 120),
                rows=int(msg.get("rows") or 30),
                environment=dict(msg.get("environment") or {}),
                argv=(
                    tuple(str(item) for item in msg["argv"])
                    if isinstance(msg.get("argv"), list) else None
                ),
                force_pipe_fallback=bool(msg.get("force_pipe_fallback")),
            )
            return {"chat_id": chat_id, **record.to_dict()}
        await _terminal_response(websocket, msg, "open", action)

    @on("terminal:get", "terminal:read", "terminal:write", "terminal:resize",
        "terminal:signal", "terminal:attach", "terminal:detach", "terminal:close")
    async def terminal_command(srv, websocket, session, msg):
        operation = str(msg.get("type") or "").split(":", 1)[-1]

        async def action():
            runtime = _runtime(srv)
            chat_id = await asyncio.to_thread(_chat_id, srv, session, msg)
            terminal_id = str(msg.get("terminal_id") or "").strip()
            record = await asyncio.to_thread(
                runtime.repository.get_terminal_for_chat, terminal_id, chat_id,
            )
            scope = record.owner.scope
            signal_receipt = None
            if operation == "get":
                return {"chat_id": chat_id, **record.to_dict(), "input_writes":
                        await asyncio.to_thread(runtime.terminals.input_status, terminal_id)}
            if operation == "read":
                result = await asyncio.to_thread(
                    runtime.terminals.read, terminal_id,
                    after_cursor=max(0, int(msg.get("after_cursor") or 0)),
                    max_bytes=max(1, min(int(msg.get("max_bytes") or 262144), 1048576)),
                    max_frames=max(1, min(int(msg.get("max_frames") or 500), 1000)),
                    prefer_artifact_refs=bool(msg.get("prefer_artifact_refs")),
                )
                return {"chat_id": chat_id, **result.to_dict()}
            if operation == "write":
                await asyncio.to_thread(runtime.terminals.write, terminal_id, str(msg.get("data") or ""))
            elif operation == "resize":
                await asyncio.to_thread(
                    runtime.terminals.resize, terminal_id,
                    cols=int(msg.get("cols") or record.cols),
                    rows=int(msg.get("rows") or record.rows),
                )
            elif operation == "signal":
                signal_receipt = await asyncio.to_thread(
                    runtime.terminals.signal, terminal_id, str(msg.get("name") or "interrupt")
                )
            elif operation == "attach":
                await asyncio.to_thread(runtime.terminals.attach, terminal_id)
            elif operation == "detach":
                await asyncio.to_thread(runtime.terminals.detach, terminal_id)
            elif operation == "close":
                await runtime.close_terminal(
                    terminal_id, force=bool(msg.get("force", True)),
                )
            return {"chat_id": chat_id,
                    **await asyncio.to_thread(_execution_status, runtime.terminals, terminal_id, scope),
                    **({"signal_receipt": signal_receipt} if signal_receipt is not None else {})}

        await _terminal_response(
            websocket, msg, operation, action,
            mutation=operation not in {"get", "read"},
        )

    @on("process:start")
    async def process_start(srv, websocket, session, msg):
        async def action():
            chat_id = _chat_id(srv, session, msg)
            command = msg.get("command")
            if not isinstance(command, (str, list)):
                raise ValueError("command must be a string or argument array")
            record = await _runtime(srv).start_process(
                command,
                owner=_owner(srv, session, msg),
                cwd=_cwd(srv, chat_id, msg.get("cwd")),
                environment=dict(msg.get("environment") or {}),
                environment_profile_id=str(msg.get("environment_profile_id") or ""),
                shell=bool(msg.get("shell")),
                restart=str(msg.get("restart") or "never"),
                max_attempts=int(msg.get("max_attempts") or 1),
                restart_delay_s=float(msg.get("restart_delay_s") or 0.25),
                health_check=dict(msg.get("health_check") or {}),
                tty=bool(msg.get("tty")),
                force_pipe_fallback=bool(msg.get("force_pipe_fallback")),
            )
            return {"chat_id": chat_id, **record.to_dict()}
        await _process_response(websocket, msg, "start", action)

    @on("process:get", "process:logs", "process:write", "process:signal",
        "process:wait", "process:stop")
    async def process_command(srv, websocket, session, msg):
        operation = str(msg.get("type") or "").split(":", 1)[-1]

        async def action():
            runtime = _runtime(srv)
            chat_id = await asyncio.to_thread(_chat_id, srv, session, msg)
            process_id = str(msg.get("process_id") or "").strip()
            record = await asyncio.to_thread(
                runtime.repository.get_process_for_chat, process_id, chat_id,
            )
            scope = record.owner.scope
            signal_receipt = None
            if operation == "get":
                return {"chat_id": chat_id, **record.to_dict(), "input_writes":
                        await asyncio.to_thread(runtime.processes.input_status, process_id)}
            if operation == "logs":
                result = await asyncio.to_thread(
                    runtime.processes.logs, process_id,
                    after_cursor=max(0, int(msg.get("after_cursor") or 0)),
                    max_bytes=max(1, min(int(msg.get("max_bytes") or 262144), 1048576)),
                    max_frames=max(1, min(int(msg.get("max_frames") or 500), 1000)),
                    prefer_artifact_refs=bool(msg.get("prefer_artifact_refs")),
                )
                return {"chat_id": chat_id, **result.to_dict()}
            if operation == "write":
                await asyncio.to_thread(runtime.processes.write, process_id, str(msg.get("data") or ""))
            elif operation == "signal":
                signal_receipt = await asyncio.to_thread(
                    runtime.processes.signal, process_id, str(msg.get("name") or "interrupt")
                )
            elif operation == "wait":
                record = await asyncio.to_thread(
                    runtime.processes.wait,
                    process_id,
                    condition=str(msg.get("condition") or "exit"),
                    timeout=max(0.0, min(float(msg.get("timeout") or 0), 30.0)),
                )
            elif operation == "stop":
                record = await runtime.stop_process(
                    process_id, force=bool(msg.get("force", True)),
                )
            return {"chat_id": chat_id,
                    **await asyncio.to_thread(_execution_status, runtime.processes, process_id, scope),
                    **({"signal_receipt": signal_receipt} if signal_receipt is not None else {})}

        await _process_response(
            websocket, msg, operation, action,
            mutation=operation not in {"get", "logs"},
        )


__all__ = ["register"]
