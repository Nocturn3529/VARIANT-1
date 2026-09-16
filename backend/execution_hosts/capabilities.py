"""Descriptor-routed terminal/process handles for the grown shell seed.

``run_command`` constructs or reconnects handles through its small ``mode``
selector. Continued interaction dispatches through this router; no separate
terminal or process broker tools are registered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from capability_broker import InvocationContext
from tools import ToolError
from work_fabric.handles import remote_handle_envelope
from work_fabric.scope import effective_work_scope as _scope

from .models import ProcessRecord, TerminalRecord


def _method(name: str, description: str, params=()) -> dict[str, Any]:
    return {
        "name": name,
        "control": name in {"signal", "stop", "close", "write", "resize"},
        "description": description,
        "params": list(params),
        "returns": "object",
    }


_READ_PARAMS = (
    {"name": "after_cursor", "type": "int", "required": False, "default": 0},
    {"name": "max_bytes", "type": "int", "required": False, "default": 65536},
    {"name": "max_frames", "type": "int", "required": False, "default": 200},
    {"name": "prefer_artifact_refs", "type": "bool", "required": False, "default": False},
)
_TERMINAL_METHODS = [
    _method("refresh", "Return a new terminal handle: handle = handle.refresh(). Use the returned handle for later effects; the old handle stays unchanged."),
    _method("inspect", "Return the durable terminal descriptor."),
    _method("read", "Read bounded terminal output after a cursor.", _READ_PARAMS),
    _method("write", "Queue raw terminal input; use carriage return for Enter on Windows. Inspect input_writes for delivery or backpressure.", ({"name": "data", "type": "str", "required": True},)),
    _method("resize", "Resize the terminal; retain the returned replacement handle for later effects.", (
        {"name": "cols", "type": "int", "required": True},
        {"name": "rows", "type": "int", "required": True},
    )),
    _method("signal", "Request a signal. Retain the returned handle; its metadata.signal_receipt reports acceptance, not observed effect. Read or wait to verify.", ({"name": "name", "type": "str", "required": False, "default": "interrupt"},)),
    _method("wait", "Wait for terminal completion.", ({"name": "timeout", "type": "float", "required": False, "default": 0.0},)),
    _method("detach", "Detach without stopping the terminal."),
    _method("close", "Close the terminal.", ({"name": "force", "type": "bool", "required": False, "default": True},)),
]
_PROCESS_METHODS = [
    _method("refresh", "Return a new process handle: handle = handle.refresh(). Use the returned handle for later effects; the old handle stays unchanged."),
    _method("inspect", "Return the durable process descriptor."),
    _method("read", "Read bounded process logs after a cursor.", _READ_PARAMS),
    _method("write", "Queue stdin; inspect input_writes for delivery or backpressure.", ({"name": "data", "type": "str", "required": True},)),
    _method("signal", "Request a signal. Retain the returned handle; metadata.signal_receipt reports acceptance, not observed effect. Windows pipe processes cannot receive console interrupts; use stop() for tree termination.", ({"name": "name", "type": "str", "required": False, "default": "interrupt"},)),
    _method("wait", "Wait for a process condition.", (
        {"name": "condition", "type": "str", "required": False, "default": "exit"},
        {"name": "timeout", "type": "float", "required": False, "default": 0.0},
    )),
    _method("stop", "Stop the process tree.", ({"name": "force", "type": "bool", "required": False, "default": True},)),
]


def _runtime(host: Any) -> Any:
    graph = getattr(host, "require_runtime", lambda: None)()
    runtime = getattr(graph, "execution", None)
    if runtime is None:
        raise ToolError("execution host is unavailable")
    return runtime


def _terminal(runtime: Any, context: InvocationContext, terminal_id: Any) -> TerminalRecord:
    try:
        return runtime.terminals.get(
            str(terminal_id or ""), scope=_scope(context),
        )
    except Exception as exc:
        raise ToolError(str(exc)) from exc


def _process(runtime: Any, context: InvocationContext, process_id: Any) -> ProcessRecord:
    try:
        return runtime.processes.get(
            str(process_id or ""), scope=_scope(context),
        )
    except Exception as exc:
        raise ToolError(str(exc)) from exc


def execution_handle_envelope(
    host: Any,
    context: InvocationContext,
    record: TerminalRecord | ProcessRecord,
    *, signal_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the reconstructable IPython handle returned by ``run_command``."""
    if isinstance(record, TerminalRecord):
        metadata = {
            "state": record.state,
            "profile": record.profile,
            "cwd": record.cwd,
            "transport": record.transport,
            "true_pty": bool(record.capabilities.get("true_pty")),
            "output_cursor": int(record.output_cursor),
            "owner": record.owner.to_dict(),
            "backend_restart_survival": bool(
                record.capabilities.get("backend_restart_survival")
            ),
        }
        kind, identity = "terminal", record.terminal_id
    elif isinstance(record, ProcessRecord):
        metadata = {
            "state": record.state,
            "cwd": record.recipe.cwd,
            "attempt": int(record.attempt),
            "output_cursor": int(record.output_cursor),
            "owner": record.owner.to_dict(),
            "backend_restart_survival": False,
        }
        if record.health.get('leader_alive') is False:
            metadata['health'] = {
                key: record.health[key] for key in (
                    'leader_alive', 'launcher_exit_code', 'owned_descendant_count',
                    'tree_liveness_error',
                ) if key in record.health
            }
        kind, identity = "process", record.process_id
    else:
        raise TypeError("unsupported execution record")
    service = getattr(host.require_runtime().execution, "terminals" if kind == "terminal" else "processes")
    status = getattr(service, "input_status", None)
    if callable(status):
        metadata["input_writes"] = status(identity)
    if signal_receipt is not None:
        metadata["signal_receipt"] = dict(signal_receipt)
    return remote_handle_envelope(
        service="execution",
        kind=kind,
        handle_id=identity,
        generation=1,
        revision=int(record.revision),
        metadata=metadata,
        methods=(
            _TERMINAL_METHODS if kind == "terminal" else _PROCESS_METHODS
        ),
        broker=host.require_runtime().broker,
        context=context,
    )


async def _handle_router(
    host: Any,
    context: InvocationContext,
    identity: Mapping[str, Any],
    method: str,
    arguments: dict[str, Any],
    *, control_only: bool = False,
) -> Any:
    if int(identity.get("generation") or -1) != 1:
        raise ToolError("execution handle generation is unsupported")
    runtime = _runtime(host)
    kind = str(identity.get("kind") or "")
    supplied_revision = int(identity.get("revision") or -1)
    if kind == "terminal":
        record = _terminal(runtime, context, identity.get("id"))
        close_revision = method == "close" and 1 <= supplied_revision <= record.revision
        if control_only:
            return ((supplied_revision == record.revision or close_revision) and (
                (method == "signal" and not (set(arguments) - {"name"}))
                or (method == "close" and not (set(arguments) - {"force"}))
                or (method == "write" and supplied_revision == record.revision
                    and set(arguments) == {"data"} and isinstance(arguments["data"], str))
                or (method == "resize" and supplied_revision == record.revision
                    and set(arguments) == {"cols", "rows"}
                    and all(type(arguments[key]) is int for key in ("cols", "rows")))
            ))
        if method == "refresh":
            return execution_handle_envelope(host, context, record)
        if method == "inspect":
            return {**record.to_dict(), "input_writes": runtime.terminals.input_status(record.terminal_id)}
        if method == "read":
            return runtime.terminals.read(
                record.terminal_id,
                after_cursor=max(0, int(arguments.get("after_cursor") or 0)),
                max_bytes=max(1, min(int(arguments.get("max_bytes") or 65536), 1024 * 1024)),
                max_frames=max(1, min(int(arguments.get("max_frames") or 200), 1000)),
                prefer_artifact_refs=bool(arguments.get("prefer_artifact_refs")),
            ).to_dict()
        if method == "wait":
            # Waiting is an observation of the current lifecycle state. A
            # terminal may naturally advance its revision by exiting between
            # model cells; requiring the pre-exit revision would make the
            # operation intended to observe that exit unusable.
            updated = await asyncio.to_thread(
                runtime.terminals.wait,
                record.terminal_id,
                timeout=max(0.0, min(float(arguments.get("timeout") or 0), 30.0)),
            )
            return execution_handle_envelope(host, context, updated)
        if supplied_revision != record.revision and not close_revision:
            raise ToolError(
                f"stale execution.terminal handle: expected revision {record.revision}; "
                "assign replacement = handle.refresh(); retry using replacement. "
                "The old handle is unchanged.",
                code="stale_execution_handle", cause_class="model",
            )
        if method == "write":
            runtime.terminals.write(record.terminal_id, str(arguments.get("data") or ""))
            return execution_handle_envelope(
                host, context, runtime.terminals.get(record.terminal_id)
            )
        if method == "resize":
            runtime.terminals.resize(
                record.terminal_id,
                cols=int(arguments.get("cols") or record.cols),
                rows=int(arguments.get("rows") or record.rows),
            )
            return execution_handle_envelope(
                host, context, runtime.terminals.get(record.terminal_id)
            )
        if method == "signal":
            receipt = runtime.terminals.signal(
                record.terminal_id, str(arguments.get("name") or "interrupt")
            )
            return execution_handle_envelope(
                host, context, runtime.terminals.get(record.terminal_id), signal_receipt=receipt,
            )
        if method == "detach":
            return execution_handle_envelope(
                host, context, runtime.terminals.detach(record.terminal_id)
            )
        if method == "close":
            updated = await runtime.close_terminal(
                record.terminal_id, force=bool(arguments.get("force", True)),
            )
            return execution_handle_envelope(host, context, updated)
        raise ToolError(f"unsupported execution.terminal method: {method}")

    if kind == "process":
        record = _process(runtime, context, identity.get("id"))
        stop_revision = method == "stop" and 1 <= supplied_revision <= record.revision
        if control_only:
            return ((supplied_revision == record.revision or stop_revision) and (
                (method == "signal" and not (set(arguments) - {"name"}))
                or (method == "stop" and not (set(arguments) - {"force"}))
                or (method == "write" and supplied_revision == record.revision
                    and set(arguments) == {"data"} and isinstance(arguments["data"], str))
            ))
        if method == "refresh":
            return execution_handle_envelope(host, context, record)
        if method == "inspect":
            return {**record.to_dict(), "input_writes": runtime.processes.input_status(record.process_id)}
        if method == "read":
            return runtime.processes.logs(
                record.process_id,
                after_cursor=max(0, int(arguments.get("after_cursor") or 0)),
                max_bytes=max(1, min(int(arguments.get("max_bytes") or 65536), 1024 * 1024)),
                max_frames=max(1, min(int(arguments.get("max_frames") or 200), 1000)),
                prefer_artifact_refs=bool(arguments.get("prefer_artifact_refs")),
            ).to_dict()
        if method == "wait":
            # Process exit is itself a revision change. Keep wait safe across
            # that expected race while retaining stale fencing for effects.
            updated = await asyncio.to_thread(
                runtime.processes.wait,
                record.process_id,
                condition=str(arguments.get("condition") or "exit"),
                timeout=max(0.0, min(float(arguments.get("timeout") or 0), 30.0)),
            )
            return execution_handle_envelope(host, context, updated)
        if supplied_revision != record.revision and not stop_revision:
            raise ToolError(
                f"stale execution.process handle: expected revision {record.revision}; "
                "assign replacement = handle.refresh(); retry using replacement. "
                "The old handle is unchanged.",
                code="stale_execution_handle", cause_class="model",
            )
        if method == "write":
            runtime.processes.write(record.process_id, str(arguments.get("data") or ""))
            return execution_handle_envelope(
                host, context, runtime.processes.get(record.process_id)
            )
        if method == "signal":
            receipt = runtime.processes.signal(
                record.process_id, str(arguments.get("name") or "interrupt")
            )
            return execution_handle_envelope(
                host, context, runtime.processes.get(record.process_id), signal_receipt=receipt,
            )
        if method == "stop":
            updated = await runtime.stop_process(
                record.process_id, force=bool(arguments.get("force", True)),
            )
            return execution_handle_envelope(host, context, updated)
        raise ToolError(f"unsupported execution.process method: {method}")
    raise ToolError("execution handle kind is unsupported")


def register_execution_tools(host: Any) -> None:
    """Install only the execution remote-handle router."""

    routers = getattr(host, "remote_handle_routers", None)
    if routers is None:
        routers = {}
        host.remote_handle_routers = routers
    routers["execution"] = (
        lambda context, identity, method, arguments:
        _handle_router(host, context, identity, method, arguments)
    )
    routers["execution"].control_admission = (
        lambda context, identity, method, arguments:
        _handle_router(host, context, identity, method, arguments, control_only=True)
    )

__all__ = [
    "execution_handle_envelope",
    "register_execution_tools",
]
