"""Reconnectable terminal and structured-process services."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import socket
import threading
import time
from urllib.parse import urlsplit

import httpx
import uuid
from typing import Any, Callable, Mapping, Sequence

from core_invariants import cancellation_is_requested
from .local import StructuredChildProcess, inherited_environment, spawn_terminal
from .models import (
    ACTIVE_PROCESS_STATES,
    ACTIVE_TERMINAL_STATES,
    BoundedProcessResult,
    ExecutionNotFound,
    ExecutionOwner,
    ExecutionUnavailable,
    ExecutionValidationError,
    OutputPage,
    ProcessRecipe,
    ProcessRecord,
    TerminalRecord,
    coerce_argv,
)
from .profiles import ExecutionProfileRegistry
from .input_queue import InputQueue


def _signal_receipt(runtime, name: str, *, transport: str) -> dict[str, Any]:
    normalized = str(name or "").strip().lower()
    if normalized not in {"interrupt", "ctrl_c", "sigint", "terminate", "kill"}:
        raise ExecutionValidationError(f"unsupported signal: {name}")
    unsupported = (os.name == "nt" and transport in {"pipe", "pipe_fallback"}
                   and normalized in {"interrupt", "ctrl_c", "sigint"})
    receipt = {
        "signal": normalized, "transport": transport,
        "supported": not unsupported, "accepted": False,
        "status": "unsupported" if unsupported else "unavailable",
        "effect": "not_sent",
    }
    if transport == "conpty" and normalized in {"interrupt", "ctrl_c", "sigint"}:
        receipt["delivery"] = "terminal_input"
    if unsupported:
        receipt["reason"] = "Windows pipe children have no console. Use a terminal for console interrupts or stop/close for tree termination."
        return receipt
    try:
        receipt["accepted"] = bool(runtime.signal(normalized))
    except OSError as exc:
        receipt["reason"] = str(exc)[:300]
    if receipt["accepted"]:
        receipt.update(status="accepted", effect="unverified")
    return receipt


def _input_write(service, kind, identity, raw):
    with service._lock:
        runtime = service._live.get(identity)
        if runtime is None or service._closing:
            raise ExecutionUnavailable(f"{kind} {identity} has no writable live runtime")
        record = getattr(service.repository, f"get_{kind}")(identity)
        if record.state not in {"running", "healthy"}:
            raise ExecutionUnavailable(f"{kind} {identity} is not accepting input (state={record.state})")
        queue = service._inputs.get(identity)
        if queue is None or queue.runtime is not runtime:
            if queue is not None:
                queue.close()
            queue = InputQueue(runtime, lambda receipt: service.repository.record_action(
                kind, identity, f"{kind}.input_{receipt['state']}", receipt,
            ))
            service._inputs[identity] = queue
        receipt = queue.submit(raw)
    return {f"{kind}_id": identity, "bytes": 0, **receipt}


def _close_input(service, identity, runtime=None, *, retire=False):
    with service._lock:
        queue = service._inputs.get(identity)
        if queue is not None and (runtime is None or queue.runtime is runtime):
            queue.close()
            if retire:
                service._inputs.pop(identity, None)
from .repository import ExecutionRepository, new_execution_id


class _StartCancelled(ExecutionUnavailable):
    """Admission was withdrawn; any child already spawned is reaped."""


@dataclass
class _PendingSpawn:
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


def _check_start_cancelled(cancellation_requested=None, pending=None):
    if (cancellation_is_requested(cancellation_requested)
            or (pending is not None and pending.cancelled.is_set())):
        raise _StartCancelled("execution start was cancelled")


@contextmanager
def _admission_lock(lock, cancellation_requested=None):
    while not lock.acquire(timeout=0.05):
        _check_start_cancelled(cancellation_requested)
    try:
        _check_start_cancelled(cancellation_requested)
        yield
    finally:
        lock.release()


def _drain_output(repository, kind, identity, runtime):
    join = getattr(runtime, "join_output", None)
    if not callable(join):
        return {}
    try:
        settled = join(timeout=2.0)
        if not settled:
            stop = getattr(runtime, "stop_output", None)
            if callable(stop):
                settled = stop(timeout=1.0)
        status = getattr(runtime, "output_status", lambda: {})()
        incomplete = not settled or any(not item.get("complete") for item in status.values())
        if not incomplete:
            return {}
        evidence = {"output_drain_incomplete": True, "output_readers": status,
                    "readers_settled": bool(settled)}
    except Exception as exc:
        evidence = {"output_drain_incomplete": True, "error": str(exc)[:500]}
    try:
        repository.record_action(kind, identity, f"{kind}.output_drain_incomplete", evidence)
    except Exception as exc:
        evidence["evidence_write_error"] = str(exc)[:500]
    return evidence


def _cwd(value: str) -> str:
    raw = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
    if not raw:
        raise ExecutionValidationError("cwd is required; execution never rebinds implicitly")
    resolved = os.path.realpath(os.path.abspath(raw))
    if not os.path.isdir(resolved):
        raise ExecutionValidationError(f"cwd is not a directory: {resolved}")
    return resolved


def _owner(value: ExecutionOwner | Mapping[str, Any]) -> ExecutionOwner:
    if isinstance(value, ExecutionOwner):
        return value
    return ExecutionOwner.from_mapping(value)


class TerminalService:
    """Multiple interactive sessions over true PTY or disclosed fallback."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        backend_instance_id: str,
        profiles: ExecutionProfileRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.backend_instance_id = str(backend_instance_id)
        self.profiles = profiles or ExecutionProfileRegistry()
        self._live: dict[str, Any] = {}
        self._watchers: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._closing = False
        self._inputs: dict[str, InputQueue] = {}
        self._pending: dict[str, _PendingSpawn] = {}

    def open(
        self,
        *,
        owner: ExecutionOwner | Mapping[str, Any],
        cwd: str,
        profile: str = "",
        cols: int = 120,
        rows: int = 30,
        environment: Mapping[str, str] | None = None,
        argv: Sequence[str] | None = None,
        force_pipe_fallback: bool = False,
        terminal_id: str = "",
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> TerminalRecord:
        width = int(cols)
        height = int(rows)
        if not 2 <= width <= 1000 or not 1 <= height <= 500:
            raise ExecutionValidationError("terminal dimensions are outside supported bounds")
        binding = _owner(owner)
        workdir = _cwd(cwd)
        resolved = self.profiles.resolve(
            profile, argv=argv, environment=environment)
        environment_delta = dict(resolved.environment)
        env = inherited_environment(environment_delta)
        identity = str(terminal_id or new_execution_id("term"))
        registered = threading.Event()
        pending = _PendingSpawn()

        def on_output(stream: str, payload: bytes) -> None:
            if not registered.wait(timeout=5):
                return
            try:
                self.repository.append_output("terminal", identity, stream, payload)
            except Exception as exc:
                try:
                    self.repository.record_action(
                        "terminal", identity, "terminal.output_spool_failed",
                        {"error": f"{type(exc).__name__}: {exc}"[:1000]},
                    )
                except Exception:
                    pass

        with _admission_lock(self._lock, cancellation_requested):
            if self._closing:
                raise ExecutionUnavailable("terminal service is shutting down")
            try:
                existing = self.repository.get_terminal(identity)
            except ExecutionNotFound:
                existing = None
            if existing is not None:
                if (
                    existing.owner != binding
                    or existing.cwd != workdir
                    or existing.profile != resolved.name
                    or existing.cols != width
                    or existing.rows != height
                ):
                    raise ExecutionValidationError(
                        "terminal_id is reserved for a different terminal recipe or owner"
                    )
                return existing
            if identity in self._pending:
                raise ExecutionUnavailable(f"terminal {identity} is already starting")
            self._pending[identity] = pending
        runtime = None
        record = None
        try:
            _check_start_cancelled(cancellation_requested, pending)
            runtime = spawn_terminal(
                resolved.argv, cwd=workdir, env=env, cols=width, rows=height,
                on_output=on_output, force_pipe_fallback=bool(force_pipe_fallback),
            )
            capabilities = dict(runtime.capabilities)
            capabilities.update({
                "profile_host_kind": resolved.host_kind,
                "reconnect_same_backend": True,
                "durable_output_replay": True,
            })
            with self._lock:
                _check_start_cancelled(cancellation_requested, pending)
                if self._closing:
                    raise _StartCancelled("terminal service is shutting down")
                record = self.repository.create_terminal(
                    terminal_id=identity, owner=binding, profile=resolved.name,
                    cwd=workdir, cols=width, rows=height,
                    transport=str(runtime.transport), capabilities=capabilities,
                    pid=int(runtime.pid), pid_started_at=float(runtime.pid_started_at),
                    backend_instance_id=self.backend_instance_id,
                )
                self._live[identity] = runtime
            registered.set()
            watcher = threading.Thread(
                target=self._watch_terminal, args=(identity, runtime),
                name=f"variant1-terminal-watch-{identity}", daemon=True,
            )
            with self._lock:
                self._watchers[identity] = watcher
            watcher.start()
            return record
        except BaseException:
            registered.set()
            if runtime is not None:
                runtime.terminate()
                runtime.close()
            with self._lock:
                self._live.pop(identity, None)
                self._watchers.pop(identity, None)
                if record is not None:
                    self.repository.transition_terminal(
                        identity, "failed", event_type="terminal.spawn_failed",
                    )
            raise
        finally:
            with self._lock:
                if self._pending.get(identity) is pending:
                    self._pending.pop(identity, None)
            pending.done.set()

    def _watch_terminal(self, terminal_id: str, runtime: Any) -> None:
        try:
            code = runtime.wait(timeout=None)
        except Exception:
            code = None
        drain = _drain_output(self.repository, "terminal", terminal_id, runtime)
        if not callable(getattr(runtime, "join_output", None)):
            # ConPTY closes and joins its reader atomically in close().
            time.sleep(0.03)
        with self._lock:
            if self._live.get(terminal_id) is not runtime:
                return
            try:
                record = self.repository.get_terminal(terminal_id)
                if record.state in ACTIVE_TERMINAL_STATES:
                    target = "terminated" if record.state == "terminating" else "exited"
                    self.repository.transition_terminal(
                        terminal_id, target, exit_code=code,
                        recovery={**record.recovery, **drain},
                        event_type=f"terminal.{target}",
                    )
            except Exception:
                pass
            self._live.pop(terminal_id, None)
            self._watchers.pop(terminal_id, None)
        _close_input(self, terminal_id, runtime, retire=True)
        runtime.close()

    def get(self, terminal_id: str, *, scope=None) -> TerminalRecord:
        return self.repository.get_terminal(terminal_id, scope=scope)

    def list(
        self, *, owner_kind: str = "", owner_id: str = "", scope=None,
        limit: int = 200,
    ) -> list[TerminalRecord]:
        return self.repository.list_terminals(
            owner_kind=owner_kind, owner_id=owner_id, scope=scope, limit=limit)

    def attach(self, terminal_id: str) -> TerminalRecord:
        with self._lock:
            if terminal_id not in self._live:
                record = self.repository.get_terminal(terminal_id)
                raise ExecutionUnavailable(
                    f"terminal {terminal_id} is not live in this backend "
                    f"(state={record.state})"
                )
            return self.repository.adjust_terminal_attachments(terminal_id, 1)

    def detach(self, terminal_id: str) -> TerminalRecord:
        return self.repository.adjust_terminal_attachments(terminal_id, -1)

    def write(self, terminal_id: str, data: str | bytes) -> dict[str, Any]:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if len(raw) > 1024 * 1024:
            raise ExecutionValidationError("one terminal write cannot exceed 1 MiB")
        return _input_write(self, "terminal", terminal_id, raw)

    def input_status(self, terminal_id: str) -> list[dict]:
        queue = self._inputs.get(terminal_id)
        return queue.status() if queue is not None else self.repository.input_receipts("terminal", terminal_id)

    def read(
        self,
        terminal_id: str,
        *,
        after_cursor: int = 0,
        max_bytes: int = 64 * 1024,
        max_frames: int = 200,
        prefer_artifact_refs: bool = False,
    ) -> OutputPage:
        return self.repository.read_output(
            "terminal", terminal_id, after_cursor=after_cursor,
            max_bytes=max_bytes, max_frames=max_frames,
            prefer_artifact_refs=prefer_artifact_refs,
        )

    def resize(self, terminal_id: str, *, cols: int, rows: int) -> dict[str, Any]:
        width, height = int(cols), int(rows)
        if not 2 <= width <= 1000 or not 1 <= height <= 500:
            raise ExecutionValidationError("terminal dimensions are outside supported bounds")
        with self._lock:
            runtime = self._live.get(terminal_id)
            record = self.repository.get_terminal(terminal_id)
            if runtime is None:
                raise ExecutionUnavailable(
                    f"terminal {terminal_id} is not controllable (state={record.state})")
            # Fitting a newly attached view often repeats the current geometry.
            # ConPTY would redraw the TUI even though nothing changed.
            if (record.capabilities.get("resize")
                    and (record.cols, record.rows) == (width, height)):
                return {"terminal_id": terminal_id, "supported": True,
                        "cols": width, "rows": height}
            supported = bool(runtime.resize(width, height))
            if supported:
                self.repository.resize_terminal(terminal_id, cols=width, rows=height)
            else:
                self.repository.record_action(
                    "terminal", terminal_id, "terminal.resize_unsupported",
                    {"cols": width, "rows": height, "transport": runtime.transport},
                )
        return {
            "terminal_id": terminal_id, "supported": supported,
            "cols": width, "rows": height,
        }

    def signal(self, terminal_id: str, name: str = "interrupt") -> dict[str, Any]:
        if str(name).lower() in {"terminate", "kill"}:
            _close_input(self, terminal_id)
        with self._lock:
            runtime = self._live.get(terminal_id)
            if runtime is None:
                record = self.repository.get_terminal(terminal_id)
                raise ExecutionUnavailable(
                    f"terminal {terminal_id} is not controllable (state={record.state})")
        receipt = _signal_receipt(runtime, name, transport=runtime.transport)
        self.repository.record_action(
            "terminal", terminal_id,
            "terminal.signal_" + receipt["status"], receipt,
        )
        return {"terminal_id": terminal_id, **receipt}

    def wait(self, terminal_id: str, *, timeout: float = 30.0) -> TerminalRecord:
        deadline = time.monotonic() + max(0.0, min(float(timeout), 86400.0))
        while True:
            record = self.repository.get_terminal(terminal_id)
            if not record.live:
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def close(self, terminal_id: str, *, force: bool = True) -> TerminalRecord:
        _close_input(self, terminal_id)
        with self._lock:
            pending = self._pending.get(terminal_id)
            if pending is not None:
                pending.cancelled.set()
        if pending is not None and not pending.done.wait(timeout=5.0):
            raise ExecutionUnavailable(
                f"terminal {terminal_id} start cancellation is pending; its owned dispatcher will reap any late child"
            )
        with self._lock:
            runtime = self._live.get(terminal_id)
            record = self.repository.get_terminal(terminal_id)
            if not record.live:
                return record
            if runtime is None:
                self.repository.transition_terminal(
                    terminal_id,
                    "unknown_effect",
                    recovery={
                        "status": "live_runtime_missing",
                        "controllable": False,
                        "pid": int(record.pid or 0),
                    },
                    event_type="terminal.live_runtime_missing",
                )
                raise ExecutionUnavailable(
                    f"terminal {terminal_id} remained live without a controllable runtime"
                )
            watcher = self._watchers.get(terminal_id)
            record = self.repository.transition_terminal(
                terminal_id, "terminating", event_type="terminal.termination_requested",
                payload={"force": bool(force)},
            )
            _close_input(self, terminal_id, runtime)
        runtime.terminate(force=force)
        # ``terminating`` is no longer a live durable state, so polling only
        # the record can return before the watcher removes the exact process
        # generation from ``_live``.  Join the owner that performs both the
        # terminal transition and live-map cleanup before reporting closure.
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=5.0)
            if watcher.is_alive():
                raise ExecutionUnavailable(
                    f"terminal {terminal_id} close is still pending; its owned watcher has not released the process and PTY"
                )
        with self._lock:
            if self._live.get(terminal_id) is runtime:
                raise ExecutionUnavailable(
                    f"terminal {terminal_id} close is still pending; its exact runtime remains live"
                )
        if getattr(runtime, "_closed", None) is False:
            raise ExecutionUnavailable(
                f"terminal {terminal_id} watcher exited without releasing its runtime handles"
            )
        return self.repository.get_terminal(terminal_id)

    terminate = close

    def shutdown(self, *, terminate_live: bool = True) -> None:
        with self._lock:
            self._closing = True
            if terminate_live:
                identities = list(dict.fromkeys([*self._live, *self._pending]))
                for pending in self._pending.values():
                    pending.cancelled.set()
            else:
                identities = []
        if terminate_live:
            for terminal_id in identities:
                try:
                    self.close(terminal_id, force=True)
                except Exception:
                    pass


class ProcessService:
    """Structured process recipes with output, exit, health, and restart state."""

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        backend_instance_id: str,
        terminals: TerminalService,
    ) -> None:
        self.repository = repository
        self.backend_instance_id = str(backend_instance_id)
        self.terminals = terminals
        self._live: dict[str, StructuredChildProcess] = {}
        self._watchers: dict[str, threading.Thread] = {}
        self._registered: dict[str, threading.Event] = {}
        self._stop_requested: set[str] = set()
        self._lock = threading.RLock()
        self._closing = False
        self._inputs: dict[str, InputQueue] = {}
        self._pending: dict[str, _PendingSpawn] = {}

    def start(
        self,
        command: str | Sequence[str],
        *,
        owner: ExecutionOwner | Mapping[str, Any],
        cwd: str,
        environment: Mapping[str, str] | None = None,
        environment_profile_id: str = "",
        shell: bool = False,
        restart: str = "never",
        max_attempts: int = 1,
        restart_delay_s: float = 0.25,
        health_check: Mapping[str, Any] | None = None,
        tty: bool = False,
        process_id: str = "",
        force_pipe_fallback: bool = False,
        cancellation_requested: Callable[[], bool] | None = None,
    ) -> ProcessRecord | TerminalRecord:
        argv = coerce_argv(command, shell=bool(shell))
        binding = _owner(owner)
        workdir = _cwd(cwd)
        if tty:
            if shell:
                if os.name == "nt":
                    argv = ("powershell.exe", "-NoLogo", "-Command", argv[0])
                else:
                    argv = ("/bin/sh", "-lc", argv[0])
            return self.terminals.open(
                owner=binding, cwd=workdir, profile="process-tty", argv=argv,
                environment=environment, force_pipe_fallback=force_pipe_fallback,
                cancellation_requested=cancellation_requested,
            )
        recipe = ProcessRecipe(
            argv=argv, cwd=workdir,
            environment={str(key): str(value)
                         for key, value in dict(environment or {}).items()},
            environment_profile_id=str(environment_profile_id or ""),
            shell=bool(shell), restart=str(restart or "never"),
            max_attempts=int(max_attempts), restart_delay_s=float(restart_delay_s),
            health_check=dict(health_check or {}),
        )
        identity = str(process_id or new_execution_id("proc"))
        pending = _PendingSpawn()
        with _admission_lock(self._lock, cancellation_requested):
            if self._closing:
                raise ExecutionUnavailable("process service is shutting down")
            try:
                existing = self.repository.get_process(identity)
            except ExecutionNotFound:
                existing = None
            if existing is not None:
                # Validate a deterministic replay against the durable recipe,
                # then return its exact state without spawning another child.
                self.repository.reserve_process(
                    process_id=identity,
                    owner=binding,
                    recipe=recipe,
                    backend_instance_id=self.backend_instance_id,
                )
                return existing
            self.repository.reserve_process(
                process_id=identity,
                owner=binding,
                recipe=recipe,
                backend_instance_id=self.backend_instance_id,
            )
            registered = threading.Event()
            self._registered[identity] = registered
            self._pending[identity] = pending
        # The durable reservation already accepts output. Neither OS spawn nor
        # cleanup may hold the lock shared by all process identities.
        registered.set()
        runtime = None
        try:
            _check_start_cancelled(cancellation_requested, pending)
            runtime = self._spawn_runtime(identity, recipe, registered)
            with self._lock:
                _check_start_cancelled(cancellation_requested, pending)
                if self._closing:
                    raise _StartCancelled("process service is shutting down")
                record = self.repository.activate_reserved_process(
                    identity, pid=runtime.pid, pid_started_at=runtime.pid_started_at,
                )
                self._live[identity] = runtime
            self._start_watcher(identity, runtime)
            return record
        except BaseException as exc:
            cleanup_error = None
            if runtime is not None:
                try:
                    runtime.terminate()
                    runtime.close()
                except BaseException as error:
                    cleanup_error = error
            with self._lock:
                self._live.pop(identity, None)
                self._watchers.pop(identity, None)
                state = "terminated" if isinstance(exc, _StartCancelled) else "failed"
                if cleanup_error is not None:
                    state = "unknown_effect"
                self.repository.transition_process(
                    identity, state,
                    pid=runtime.pid if runtime is not None else 0,
                    pid_started_at=runtime.pid_started_at if runtime is not None else 0,
                    recovery={
                        "dispatch_reserved": True,
                        "spawn_cancelled" if isinstance(exc, _StartCancelled) else "spawn_failed":
                            f"{type(exc).__name__}: {exc}"[:1000],
                        **({"cleanup_error": str(cleanup_error)[:1000]} if cleanup_error else {}),
                    },
                    event_type="process.spawn_cancelled" if isinstance(exc, _StartCancelled) else "process.spawn_failed",
                )
                self._registered.pop(identity, None)
            raise
        finally:
            with self._lock:
                if self._pending.get(identity) is pending:
                    self._pending.pop(identity, None)
            pending.done.set()

    def _spawn_runtime(
        self, process_id: str, recipe: ProcessRecipe, registered: threading.Event,
    ) -> StructuredChildProcess:
        def on_output(stream: str, payload: bytes) -> None:
            if not registered.wait(timeout=5):
                return
            try:
                self.repository.append_output("process", process_id, stream, payload)
            except Exception as exc:
                try:
                    self.repository.record_action(
                        "process", process_id, "process.output_spool_failed",
                        {"error": f"{type(exc).__name__}: {exc}"[:1000]},
                    )
                except Exception:
                    pass

        return StructuredChildProcess(
            recipe.argv, cwd=recipe.cwd,
            env=inherited_environment(recipe.environment), shell=recipe.shell,
            on_output=on_output,
        )

    def _start_watcher(self, process_id: str, runtime: StructuredChildProcess) -> None:
        watcher = threading.Thread(
            target=self._watch_process, args=(process_id, runtime),
            name=f"variant1-process-watch-{process_id}", daemon=True,
        )
        with self._lock:
            self._watchers[process_id] = watcher
        watcher.start()

    def _watch_process(self, process_id: str, runtime: StructuredChildProcess) -> None:
        try:
            code = runtime.wait(timeout=None)
        except Exception:
            code = None
        _close_input(self, process_id, runtime, retire=True)
        self._wait_owned_descendants(process_id, runtime, code)
        drain = _drain_output(self.repository, "process", process_id, runtime)
        should_restart = False
        pending = None
        with self._lock:
            current_generation = self._live.get(process_id) is runtime
            record = None
            if current_generation:
                try:
                    record = self.repository.get_process(process_id)
                except ExecutionNotFound:
                    pass
            if record is not None:
                stopped = process_id in self._stop_requested or self._closing
                should_restart = (
                    not stopped
                    and record.attempt < record.recipe.max_attempts
                    and (
                        record.recipe.restart == "always"
                        or (record.recipe.restart == "on_failure" and code not in (0, None))
                    )
                )
                if should_restart:
                    pending = _PendingSpawn()
                    self._pending[process_id] = pending
                    self.repository.transition_process(
                        process_id, "restarting", exit_code=code,
                        recovery={**record.recovery, **drain},
                        event_type="process.restart_scheduled",
                        payload={"delay_s": record.recipe.restart_delay_s},
                    )
                else:
                    target = "terminated" if stopped or record.state == "terminating" else "exited"
                    health = dict(record.health)
                    health.update({"status": "stopped", "exit_code": code})
                    if health.get('leader_alive') is False:
                        health['owned_descendant_count'] = 0
                        health.pop('tree_liveness_error', None)
                    self.repository.transition_process(
                        process_id, target, exit_code=code, health=health,
                        recovery={**record.recovery, **drain},
                        event_type=f"process.{target}",
                    )
            if current_generation and not should_restart:
                self._live.pop(process_id, None)
                self._watchers.pop(process_id, None)
                self._registered.pop(process_id, None)
        # This includes output stream closure and Job disposal. In particular,
        # never hold the admission lock while another thread owns pipe I/O.
        try:
            runtime.close(terminate_tree=not current_generation)
        except Exception as exc:
            self.repository.record_action("process", process_id, "process.cleanup_failed",
                                          {"error": str(exc)[:1000]})
            if pending is not None:
                pending.cancelled.set()
        if not should_restart:
            return
        replacement = None
        try:
            pending.cancelled.wait(record.recipe.restart_delay_s)
            _check_start_cancelled(pending=pending)
            with self._lock:
                if self._closing or self._live.get(process_id) is not runtime:
                    raise _StartCancelled("process restart was withdrawn")
                registered = self._registered[process_id]
            replacement = self._spawn_runtime(process_id, record.recipe, registered)
            with self._lock:
                _check_start_cancelled(pending=pending)
                if self._closing or self._live.get(process_id) is not runtime:
                    raise _StartCancelled("process restart was withdrawn")
                self.repository.transition_process(
                    process_id, "running", exit_code=None,
                    health={"status": "unchecked"}, pid=replacement.pid,
                    pid_started_at=replacement.pid_started_at, attempt=record.attempt + 1,
                    event_type="process.restarted",
                )
                self._live[process_id] = replacement
            self._start_watcher(process_id, replacement)
        except Exception as exc:
            cleanup_error = None
            if replacement is not None:
                try:
                    replacement.terminate()
                    replacement.close()
                except Exception as error:
                    cleanup_error = error
            with self._lock:
                if self._live.get(process_id) in (runtime, replacement):
                    cancelled = isinstance(exc, _StartCancelled)
                    self.repository.transition_process(
                        process_id, "unknown_effect" if cleanup_error else "terminated" if cancelled else "failed",
                        exit_code=code,
                        health={"status": "restart_cancelled" if cancelled else "restart_failed",
                                "error": str(exc)[:1000]},
                        recovery={**record.recovery,
                                  **({"cleanup_error": str(cleanup_error)[:1000]} if cleanup_error else {})},
                        event_type="process.restart_cancelled" if cancelled else "process.restart_failed",
                    )
                    self._live.pop(process_id, None)
                    self._watchers.pop(process_id, None)
                    self._registered.pop(process_id, None)
        finally:
            with self._lock:
                if self._pending.get(process_id) is pending:
                    self._pending.pop(process_id, None)
            pending.done.set()

    def _wait_owned_descendants(
        self, process_id: str, runtime: StructuredChildProcess, code: int | None,
    ) -> None:
        """Keep launcher handoffs owned and stoppable until their group is empty."""
        previous = None
        observed_nonempty = False
        while True:
            error = ''
            try:
                count = runtime.active_process_count()
            except OSError as exc:
                # A failed query cannot authorize destroying potentially live
                # work. Keep ownership and Stop available while retrying.
                count = None
                error = f'{type(exc).__name__}: {exc}'[:500]
            if count == 0:
                return
            if count is not None and not observed_nonempty:
                # Short-lived launchers can report their exit just before a
                # child finishes OS teardown. Confirm at the next normal
                # liveness poll before publishing an asynchronous handoff.
                observed_nonempty = True
                time.sleep(0.05)
                continue
            evidence = {
                'leader_alive': False, 'launcher_exit_code': code,
                'owned_descendant_count': count,
            }
            if error:
                evidence['tree_liveness_error'] = error
            if evidence != previous:
                with self._lock:
                    if self._live.get(process_id) is not runtime:
                        return
                    current = self.repository.get_process(process_id)
                    health = {**current.health, **evidence}
                    if not error:
                        health.pop('tree_liveness_error', None)
                    self.repository.transition_process(
                        process_id, current.state, exit_code=code, health=health,
                        event_type='process.owned_descendants_active', payload=evidence,
                    )
                previous = evidence
            time.sleep(0.2 if error else 0.05)

    def get(self, process_id: str, *, scope=None) -> ProcessRecord:
        return self.repository.get_process(process_id, scope=scope)

    def list(
        self, *, owner_kind: str = "", owner_id: str = "", scope=None,
        limit: int = 200,
    ) -> list[ProcessRecord]:
        return self.repository.list_processes(
            owner_kind=owner_kind, owner_id=owner_id, scope=scope, limit=limit)

    def logs(
        self,
        process_id: str,
        *,
        after_cursor: int = 0,
        max_bytes: int = 64 * 1024,
        max_frames: int = 200,
        prefer_artifact_refs: bool = False,
    ) -> OutputPage:
        return self.repository.read_output(
            "process", process_id, after_cursor=after_cursor,
            max_bytes=max_bytes, max_frames=max_frames,
            prefer_artifact_refs=prefer_artifact_refs,
        )

    def _collect_bounded_output(
        self,
        process_id: str,
        *,
        max_output_bytes: int,
    ) -> tuple[bytes, bytes, tuple[str, ...], int, bool]:
        """Project durable process output without creating another spool."""

        bound = max(1, min(int(max_output_bytes), 512 * 1024 * 1024))
        remaining = bound
        cursor = 0
        stdout: list[bytes] = []
        stderr: list[bytes] = []
        artifact_refs: list[str] = []
        end_cursor = 0
        for _page_number in range(10_000):
            if remaining <= 0:
                break
            page = self.logs(
                process_id,
                after_cursor=cursor,
                max_bytes=min(remaining, 1024 * 1024),
                max_frames=1000,
                prefer_artifact_refs=False,
            )
            end_cursor = max(end_cursor, int(page.end_cursor))
            consumed = 0
            for frame in page.frames:
                if frame.artifact_ref:
                    artifact_refs.append(frame.artifact_ref)
                    continue
                payload = bytes(frame.data)
                consumed += len(payload)
                (stderr if frame.stream == "stderr" else stdout).append(payload)
            remaining -= consumed
            next_cursor = int(page.next_cursor)
            if not page.more or next_cursor <= cursor:
                cursor = max(cursor, next_cursor)
                break
            cursor = next_cursor
        if not end_cursor:
            end_cursor = self.get(process_id).output_cursor
        return (
            b"".join(stdout),
            b"".join(stderr),
            tuple(dict.fromkeys(artifact_refs)),
            end_cursor,
            cursor < end_cursor,
        )

    def run_bounded(
        self,
        command: str | Sequence[str],
        *,
        owner: ExecutionOwner | Mapping[str, Any],
        cwd: str,
        environment: Mapping[str, str] | None = None,
        environment_profile_id: str = "",
        timeout: float = 30.0,
        max_output_bytes: int = 16 * 1024 * 1024,
        cancellation_requested: Callable[[], bool] | None = None,
        process_id: str = "",
    ) -> BoundedProcessResult:
        """Wait for the command leader, retaining owned background children.

        This is the sole one-shot execution path.  ``run_command`` and Review
        checks both call it. A command that launches background work returns
        its leader exit status and the still-live durable process handle. Its
        children remain owned until natural exit, explicit Stop or shutdown.
        """

        started = time.perf_counter()
        record = self.start(
            command,
            owner=owner,
            cwd=cwd,
            environment=environment,
            environment_profile_id=environment_profile_id,
            shell=False,
            restart="never",
            max_attempts=1,
            process_id=process_id,
            cancellation_requested=cancellation_requested,
        )
        if isinstance(record, TerminalRecord):  # Defensive: tty is never requested.
            raise ExecutionValidationError("bounded execution cannot create a terminal")
        timed_out = False
        cancelled = False
        deadline = time.monotonic() + max(0.1, min(float(timeout), 24 * 60 * 60))

        try:
            while record.live:
                if cancellation_is_requested(cancellation_requested):
                    cancelled = True
                    record = self.stop(record.process_id, force=True)
                    break
                if record.health.get('leader_alive') is False:
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    record = self.stop(record.process_id, force=True)
                    break
                time.sleep(0.05)
                record = self.get(record.process_id, scope=record.owner.scope)
        except BaseException:
            try:
                current = self.get(record.process_id)
                if current.live:
                    self.stop(record.process_id, force=True)
            except BaseException:
                pass
            raise

        record = self.get(record.process_id, scope=record.owner.scope)
        stdout, stderr, refs, output_cursor, truncated = self._collect_bounded_output(
            record.process_id,
            max_output_bytes=max_output_bytes,
        )
        return BoundedProcessResult(
            process=record,
            stdout=stdout,
            stderr=stderr,
            artifact_refs=refs,
            output_cursor=output_cursor,
            timed_out=timed_out,
            cancelled=cancelled,
            truncated=truncated,
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )

    def write(self, process_id: str, data: str | bytes) -> dict[str, Any]:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if len(raw) > 1024 * 1024:
            raise ExecutionValidationError("one process write cannot exceed 1 MiB")
        return _input_write(self, "process", process_id, raw)

    def input_status(self, process_id: str) -> list[dict]:
        queue = self._inputs.get(process_id)
        return queue.status() if queue is not None else self.repository.input_receipts("process", process_id)

    def signal(self, process_id: str, name: str = "interrupt") -> dict[str, Any]:
        if str(name).lower() in {"terminate", "kill"}:
            _close_input(self, process_id)
        with self._lock:
            runtime = self._live.get(process_id)
            if runtime is None:
                record = self.repository.get_process(process_id)
                raise ExecutionUnavailable(
                    f"process {process_id} is not controllable (state={record.state})")
            if str(name).lower() in {"terminate", "kill"}:
                self._stop_requested.add(process_id)
                pending = self._pending.get(process_id)
                if pending is not None:
                    pending.cancelled.set()
        receipt = _signal_receipt(runtime, name, transport="pipe")
        self.repository.record_action(
            "process", process_id,
            "process.signal_" + receipt["status"], receipt,
        )
        return {"process_id": process_id, **receipt}

    @staticmethod
    def _check_health(record: ProcessRecord) -> tuple[bool, dict[str, Any]]:
        check = dict(record.recipe.health_check)
        kind = str(check.get("kind") or "process").strip().lower()
        started = time.perf_counter()
        try:
            if kind == "process":
                ok = record.state in ACTIVE_PROCESS_STATES
                detail: dict[str, Any] = {"kind": kind}
            elif kind == "http":
                url = str(check.get("url") or "").strip()
                if not url:
                    raise ExecutionValidationError("HTTP health check requires url")
                parsed = urlsplit(url)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                    raise ExecutionValidationError("HTTP health check requires an http(s) URL")
                # Local development servers are a principal health-check use.
                # Keep localhost/private hosts, but never open files or follow
                # a redirect and accidentally check an unrelated destination.
                with httpx.Client(trust_env=False, follow_redirects=False, timeout=min(
                    5.0, max(0.1, float(check.get("request_timeout_s") or 1.0)))) as client:
                    with client.stream("GET", url) as response:
                        status = int(response.status_code)
                expected = {int(item) for item in (check.get("statuses") or range(200, 400))}
                ok = status in expected
                detail = {"kind": kind, "url": url, "http_status": status}
            elif kind == "tcp":
                host = str(check.get("host") or "127.0.0.1")
                port = int(check.get("port") or 0)
                if not 1 <= port <= 65535:
                    raise ExecutionValidationError("TCP health check requires a valid port")
                with socket.create_connection(
                    (host, port), timeout=min(5.0, max(
                        0.1, float(check.get("request_timeout_s") or 1.0)))):
                    pass
                ok = True
                detail = {"kind": kind, "host": host, "port": port}
            else:
                raise ExecutionValidationError(f"unsupported health check kind: {kind}")
        except Exception as exc:
            ok = False
            detail = {"kind": kind, "error": f"{type(exc).__name__}: {exc}"[:1000]}
        detail.update({
            "status": "healthy" if ok else "unhealthy",
            "checked_at": time.time(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        return ok, detail

    def wait(
        self,
        process_id: str,
        *,
        condition: str = "exit",
        timeout: float = 30.0,
        poll_interval_s: float = 0.1,
    ) -> ProcessRecord:
        wanted = str(condition or "exit").strip().lower()
        if wanted not in {"exit", "running", "healthy"}:
            raise ExecutionValidationError(
                "process wait condition must be exit, running, or healthy")
        deadline = time.monotonic() + max(0.0, min(float(timeout), 86400.0))
        interval = max(0.02, min(float(poll_interval_s), 5.0))
        while True:
            record = self.repository.get_process(process_id)
            if wanted == "exit" and not record.live:
                return record
            if wanted == "running" and record.live:
                return record
            if wanted == "healthy":
                if record.state == "healthy":
                    return record
                if not record.live:
                    return record
                ok, health = self._check_health(record)
                if ok:
                    with self._lock:
                        current = self.repository.get_process(process_id)
                        if current.live and current.state != "healthy":
                            record = self.repository.transition_process(
                                process_id, "healthy", exit_code=current.exit_code,
                                health={**current.health, **health},
                                event_type="process.healthy",
                            )
                        else:
                            record = current
                    return record
                # Persist bounded latest health evidence without treating every
                # poll as a process attempt.
                with self._lock:
                    current = self.repository.get_process(process_id)
                    if current.live:
                        record = self.repository.transition_process(
                            process_id, current.state, exit_code=current.exit_code,
                            health={**current.health, **health},
                            event_type="process.health_checked",
                        )
            if time.monotonic() >= deadline:
                return record
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def stop(self, process_id: str, *, force: bool = True) -> ProcessRecord:
        _close_input(self, process_id)
        with self._lock:
            pending = self._pending.get(process_id)
            if pending is not None:
                self._stop_requested.add(process_id)
                pending.cancelled.set()
        if pending is not None and not pending.done.wait(timeout=5.0):
            raise ExecutionUnavailable(
                f"process {process_id} start cancellation is pending; its owned dispatcher will reap any late child"
            )
        with self._lock:
            runtime = self._live.get(process_id)
            record = self.repository.get_process(process_id)
            if not record.live:
                return record
            if runtime is None:
                self.repository.transition_process(
                    process_id,
                    "unknown_effect",
                    recovery={
                        "status": "live_runtime_missing",
                        "controllable": False,
                        "pid": int(record.pid or 0),
                    },
                    event_type="process.live_runtime_missing",
                )
                raise ExecutionUnavailable(
                    f"process {process_id} remained live without a controllable runtime"
                )
            self._stop_requested.add(process_id)
            record = self.repository.transition_process(
                process_id, "terminating", event_type="process.termination_requested",
                payload={"force": bool(force)},
            )
            _close_input(self, process_id, runtime)
        runtime.terminate(force=force)
        settled = self.wait(process_id, condition="exit", timeout=5.0)
        if settled.live:
            raise ExecutionUnavailable(
                f"process {process_id} remained live after forced termination"
            )
        return settled

    def shutdown(self, *, terminate_live: bool = True) -> None:
        with self._lock:
            self._closing = True
            if terminate_live:
                identities = list(dict.fromkeys([*self._live, *self._pending]))
                for pending in self._pending.values():
                    pending.cancelled.set()
            else:
                identities = []
        if terminate_live:
            for process_id in identities:
                try:
                    self.stop(process_id, force=True)
                except Exception:
                    pass


@dataclass(slots=True)
class ExecutionRuntime:
    repository: ExecutionRepository
    terminals: TerminalService
    processes: ProcessService
    backend_instance_id: str
    recovery_report: dict[str, list[str]]
    owns_artifact_store: bool = False

    @staticmethod
    async def _settle_thread_call(name: str, function, /, *args, _cancel_event=None, **kwargs):
        task = asyncio.create_task(
            asyncio.to_thread(function, *args, **kwargs), name=name,
        )
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if _cancel_event is not None:
                    _cancel_event.set()
                cancellation = cancellation or exc
                continue
            except BaseException:
                # Retrieve the worker exception below so a requested Stop
                # keeps its cancellation identity after cleanup settles.
                if task.done():
                    break
                raise
        try:
            return task.result(), cancellation
        except BaseException as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise

    async def open_terminal(self, **kwargs) -> TerminalRecord:
        cancel_event = threading.Event()
        supplied = kwargs.pop("cancellation_requested", None)
        record, cancellation = await self._settle_thread_call(
            "execution-terminal-open", self.terminals.open, **kwargs,
            cancellation_requested=lambda: cancel_event.is_set() or cancellation_is_requested(supplied),
            _cancel_event=cancel_event,
        )
        if cancellation is not None:
            try:
                await self.close_terminal(record.terminal_id, force=True)
            except BaseException as cleanup_error:
                try:
                    cancellation.add_note(
                        f"terminal cleanup failed: {cleanup_error}"
                    )
                except Exception:
                    pass
            raise cancellation
        return record

    async def close_terminal(
        self, terminal_id: str, *, force: bool = True,
    ) -> TerminalRecord:
        record, cancellation = await self._settle_thread_call(
            f"execution-terminal-close:{terminal_id}",
            self.terminals.close,
            terminal_id,
            force=force,
        )
        if cancellation is not None:
            raise cancellation
        return record

    async def start_process(
        self, command: str | Sequence[str], **kwargs,
    ) -> ProcessRecord | TerminalRecord:
        cancel_event = threading.Event()
        supplied = kwargs.pop("cancellation_requested", None)
        record, cancellation = await self._settle_thread_call(
            "execution-process-start",
            self.processes.start,
            command,
            cancellation_requested=lambda: cancel_event.is_set() or cancellation_is_requested(supplied),
            _cancel_event=cancel_event,
            **kwargs,
        )
        if cancellation is not None:
            try:
                if isinstance(record, TerminalRecord):
                    await self.close_terminal(record.terminal_id, force=True)
                else:
                    await self.stop_process(record.process_id, force=True)
            except BaseException as cleanup_error:
                try:
                    cancellation.add_note(
                        f"process cleanup failed: {cleanup_error}"
                    )
                except Exception:
                    pass
            raise cancellation
        return record

    async def run_bounded_process(
        self,
        command: str | Sequence[str],
        **kwargs,
    ) -> BoundedProcessResult:
        """Cancellation-safe async adapter for ``ProcessService.run_bounded``."""

        cancellation_event = threading.Event()
        supplied = kwargs.pop("cancellation_requested", None)

        def cancellation_requested() -> bool:
            return cancellation_event.is_set() or cancellation_is_requested(supplied)

        result, cancellation = await self._settle_thread_call(
            "execution-process-bounded",
            self.processes.run_bounded,
            command,
            cancellation_requested=cancellation_requested,
            _cancel_event=cancellation_event,
            **kwargs,
        )
        if cancellation is not None:
            raise cancellation
        return result

    async def stop_process(
        self, process_id: str, *, force: bool = True,
    ) -> ProcessRecord:
        record, cancellation = await self._settle_thread_call(
            f"execution-process-stop:{process_id}",
            self.processes.stop,
            process_id,
            force=force,
        )
        if cancellation is not None:
            raise cancellation
        return record

    def capability_report(self) -> dict[str, Any]:
        from .windows_conpty import conpty_available
        return {
            "schema": "variant1.execution-host-capabilities.v1",
            "platform": os.name,
            "windows_conpty_api_available": bool(conpty_available()),
            "true_pty_preferred": True,
            "pipe_fallback_disclosed": True,
            "same_backend_reconnect": True,
            "durable_cursor_replay": True,
            "backend_restart_survival": False,
            "backend_restart_contract": (
                "stale Python-owned handles are fenced as unknown_effect; "
                "an Electron-owned native host is required for live survival"
            ),
            "profiles": list(self.terminals.profiles.names()),
        }

    def events(
        self, *, after_sequence: int = 0, limit: int = 200,
        entity_kind: str = "", entity_id: str = "", scope=None,
    ):
        return self.repository.list_events(
            after_sequence=after_sequence, limit=limit,
            entity_kind=entity_kind, entity_id=entity_id, scope=scope,
        )

    def worktree_has_live_owner(self, worktree_id: str) -> bool:
        return self.repository.has_live_worktree_owner(worktree_id)

    async def delete_chat(self, chat_id: str) -> int:
        terminal_ids, process_ids = self.repository.live_ids_for_chat(chat_id)
        outcomes = await asyncio.gather(
            *(self.close_terminal(identity, force=True) for identity in terminal_ids),
            *(self.stop_process(identity, force=True) for identity in process_ids),
            return_exceptions=True,
        )
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        if failures:
            raise ExecutionUnavailable(
                f"execution chat cleanup left {len(failures)} owner(s) retryable"
            ) from failures[0]
        return len(terminal_ids) + len(process_ids)

    def shutdown(self) -> None:
        # Structured processes stop first; a tty=true structured request may be
        # represented by TerminalService and is then settled in the second step.
        self.processes.shutdown(terminate_live=True)
        self.terminals.shutdown(terminate_live=True)


def create_execution_runtime(
    *,
    path: str | None = None,
    data_dir: str | None = None,
    artifact_store: Any | None = None,
    live_spool_bytes: int = 4 * 1024 * 1024,
    profiles: ExecutionProfileRegistry | None = None,
    backend_instance_id: str = "",
    reconcile: bool = True,
) -> ExecutionRuntime:
    """Build the backend-owned execution runtime without host side effects.

    Until an Electron-owned native host is composed, the returned capability
    truthfully advertises ``backend_restart_survival=False``.  Reconciliation
    fences stale handles as ``unknown_effect`` instead of relaunching recipes.
    """
    owns_artifacts = artifact_store is None
    if artifact_store is None:
        from artifacts import ContentAddressedArtifactStore
        if data_dir:
            artifact_root = os.path.join(os.path.abspath(data_dir), "artifacts")
        elif path:
            artifact_root = os.path.join(os.path.dirname(os.path.abspath(path)), "artifacts")
        else:
            artifact_root = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), os.pardir,
                "data", "execution", "artifacts",
            )
        artifact_store = ContentAddressedArtifactStore(os.path.abspath(artifact_root))
    repository = ExecutionRepository(
        path=path, data_dir=data_dir, artifact_store=artifact_store,
        live_spool_bytes=live_spool_bytes,
    )
    instance_id = str(backend_instance_id or f"backend_{uuid.uuid4().hex}")
    recovery = (
        repository.reconcile_stale_backends(instance_id)
        if reconcile else {"terminals": [], "processes": []}
    )
    terminals = TerminalService(
        repository, backend_instance_id=instance_id, profiles=profiles)
    processes = ProcessService(
        repository, backend_instance_id=instance_id, terminals=terminals)
    return ExecutionRuntime(
        repository=repository, terminals=terminals, processes=processes,
        backend_instance_id=instance_id, recovery_report=recovery,
        owns_artifact_store=owns_artifacts,
    )


__all__ = [
    "ExecutionRuntime", "ProcessService", "TerminalService",
    "create_execution_runtime",
]
