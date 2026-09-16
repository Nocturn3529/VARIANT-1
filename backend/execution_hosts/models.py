"""Typed contracts for VARIANT-1 terminal and structured-process execution.

The records in this module are durable descriptions.  Live process handles,
threads, pipe objects and PTY handles deliberately never enter SQLite or an
CPython result envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
from typing import Any, Mapping, Sequence

from core_invariants import StrictJSONError, canonical_json as _canonical_json, strict_json_value
from work_fabric.scope import WorkScope, coerce_work_scope


EXECUTION_TERMINAL_SCHEMA = "variant1.execution-terminal.v1"
EXECUTION_PROCESS_SCHEMA = "variant1.execution-process.v1"
EXECUTION_EVENT_SCHEMA = "variant1.execution-event.v1"
EXECUTION_OUTPUT_SCHEMA = "variant1.execution-output-page.v1"
EXECUTION_BOUNDED_RESULT_SCHEMA = "variant1.execution-bounded-result.v1"

TERMINAL_STATES = frozenset({
    "starting", "running", "exited", "terminating", "terminated",
    "failed", "unknown_effect",
})
PROCESS_STATES = frozenset({
    "starting", "running", "healthy", "exited", "restarting",
    "terminating", "terminated", "failed", "unknown_effect",
})
ACTIVE_TERMINAL_STATES = frozenset({"starting", "running", "terminating"})
ACTIVE_PROCESS_STATES = frozenset({
    "starting", "running", "healthy", "restarting", "terminating",
})
RESTART_POLICIES = frozenset({"never", "on_failure", "always"})


class ExecutionError(RuntimeError):
    """Base error for the execution-host domain."""


class ExecutionNotFound(ExecutionError):
    """A terminal or process identity does not exist."""


class ExecutionUnavailable(ExecutionError):
    """A durable record has no controllable live runtime in this backend."""


class ExecutionConflict(ExecutionError):
    """A requested transition lost a state/revision race."""


class ExecutionScopeMismatch(ExecutionError):
    """An execution record is outside the supplied WorkScope."""


class ExecutionValidationError(ExecutionError, ValueError):
    """An execution request or persisted payload is invalid."""


def _text(value: Any, field_name: str, *, required: bool = False,
          limit: int = 4096) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ExecutionValidationError(f"{field_name} is required")
    if "\x00" in result or len(result) > limit:
        raise ExecutionValidationError(f"{field_name} is invalid or too long")
    return result


def json_value(value: Any, *, path: str = "$") -> Any:
    try:
        return strict_json_value(value, path=path)
    except StrictJSONError as exc:
        raise ExecutionValidationError(str(exc)) from exc


def canonical_json(value: Any) -> str:
    return _canonical_json(json_value(value))


@dataclass(frozen=True, slots=True)
class ExecutionOwner:
    """Exact durable owner and workspace/worktree correlation."""

    kind: str
    owner_id: str
    scope: WorkScope = WorkScope()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _text(
            self.kind, "owner.kind", required=True, limit=128))
        object.__setattr__(self, "owner_id", _text(
            self.owner_id, "owner.id", required=True, limit=512))
        object.__setattr__(self, "scope", coerce_work_scope(self.scope))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionOwner":
        raw = dict(value or {})
        return cls(
            kind=str(raw.get("kind") or raw.get("owner_kind") or ""),
            owner_id=str(raw.get("id") or raw.get("owner_id") or ""),
            scope=coerce_work_scope(raw.get("scope")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.owner_id,
            "scope": self.scope.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TerminalRecord:
    terminal_id: str
    owner: ExecutionOwner
    profile: str
    cwd: str
    cols: int
    rows: int
    state: str
    transport: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    pid: int = 0
    pid_started_at: float = 0.0
    backend_instance_id: str = ""
    output_cursor: int = 0
    live_start_cursor: int = 0
    attachments: int = 0
    exit_code: int | None = None
    recovery: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    exited_at: float = 0.0

    def __post_init__(self) -> None:
        if self.state not in TERMINAL_STATES:
            raise ExecutionValidationError(f"invalid terminal state: {self.state}")
        if self.cols < 2 or self.rows < 1:
            raise ExecutionValidationError("terminal dimensions are invalid")
        canonical_json(self.capabilities)
        canonical_json(self.recovery)

    @property
    def live(self) -> bool:
        return self.state in ACTIVE_TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_TERMINAL_SCHEMA,
            "id": self.terminal_id,
            "owner": self.owner.to_dict(),
            "profile": self.profile,
            "cwd": self.cwd,
            "dimensions": {"cols": self.cols, "rows": self.rows},
            "state": self.state,
            "transport": self.transport,
            "capabilities": json_value(self.capabilities),
            "pid": self.pid or None,
            "pid_started_at": self.pid_started_at or None,
            "backend_instance_id": self.backend_instance_id or None,
            "output_cursor": self.output_cursor,
            "live_start_cursor": self.live_start_cursor,
            "attachments": self.attachments,
            "exit_code": self.exit_code,
            "recovery": json_value(self.recovery),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "exited_at": self.exited_at or None,
        }


@dataclass(frozen=True, slots=True)
class ProcessRecipe:
    argv: tuple[str, ...]
    cwd: str
    environment: dict[str, str] = field(default_factory=dict)
    environment_profile_id: str = ""
    shell: bool = False
    restart: str = "never"
    max_attempts: int = 1
    restart_delay_s: float = 0.25
    health_check: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        argv = tuple(str(item) for item in self.argv)
        if not argv or any(not item or "\x00" in item for item in argv):
            raise ExecutionValidationError("process argv must contain valid arguments")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", _text(
            self.cwd, "process.cwd", required=True, limit=32768))
        if self.restart not in RESTART_POLICIES:
            raise ExecutionValidationError(
                f"unsupported restart policy: {self.restart}")
        if self.max_attempts < 1 or self.max_attempts > 100:
            raise ExecutionValidationError("max_attempts must be between 1 and 100")
        if not 0 <= float(self.restart_delay_s) <= 60:
            raise ExecutionValidationError("restart_delay_s must be between 0 and 60")
        clean_env: dict[str, str] = {}
        for key, value in self.environment.items():
            name = _text(key, "environment key", required=True, limit=32767)
            text = str(value)
            if "\x00" in text:
                raise ExecutionValidationError("environment value contains NUL")
            clean_env[name] = text
        object.__setattr__(self, "environment", clean_env)
        canonical_json(self.health_check)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProcessRecipe":
        raw = dict(value or {})
        argv_value = raw.get("argv") or ()
        if isinstance(argv_value, str):
            argv_value = (argv_value,)
        return cls(
            argv=tuple(str(item) for item in argv_value),
            cwd=str(raw.get("cwd") or ""),
            environment={str(key): str(item)
                         for key, item in dict(raw.get("environment") or {}).items()},
            environment_profile_id=str(raw.get("environment_profile_id") or ""),
            shell=bool(raw.get("shell")),
            restart=str(raw.get("restart") or "never"),
            max_attempts=int(raw.get("max_attempts") or 1),
            restart_delay_s=float(
                0.25 if raw.get("restart_delay_s") is None
                else raw.get("restart_delay_s")
            ),
            health_check=dict(raw.get("health_check") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "environment": dict(self.environment),
            "environment_profile_id": self.environment_profile_id or None,
            "shell": self.shell,
            "restart": self.restart,
            "max_attempts": self.max_attempts,
            "restart_delay_s": self.restart_delay_s,
            "health_check": json_value(self.health_check),
        }


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    process_id: str
    owner: ExecutionOwner
    recipe: ProcessRecipe
    state: str
    pid: int = 0
    pid_started_at: float = 0.0
    backend_instance_id: str = ""
    attempt: int = 1
    output_cursor: int = 0
    live_start_cursor: int = 0
    exit_code: int | None = None
    health: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    exited_at: float = 0.0

    def __post_init__(self) -> None:
        if self.state not in PROCESS_STATES:
            raise ExecutionValidationError(f"invalid process state: {self.state}")
        canonical_json(self.health)
        canonical_json(self.recovery)

    @property
    def live(self) -> bool:
        return self.state in ACTIVE_PROCESS_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_PROCESS_SCHEMA,
            "id": self.process_id,
            "owner": self.owner.to_dict(),
            "recipe": self.recipe.to_dict(),
            "state": self.state,
            "pid": self.pid or None,
            "pid_started_at": self.pid_started_at or None,
            "backend_instance_id": self.backend_instance_id or None,
            "attempt": self.attempt,
            "output_cursor": self.output_cursor,
            "live_start_cursor": self.live_start_cursor,
            "exit_code": self.exit_code,
            "health": json_value(self.health),
            "recovery": json_value(self.recovery),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "exited_at": self.exited_at or None,
        }


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """Result of the one bounded-process operation shared by all callers.

    The durable :class:`ProcessRecord` remains the authority for lifecycle and
    replay.  This value is only the bounded, caller-friendly projection of its
    captured output and termination reason.
    """

    process: ProcessRecord
    stdout: bytes = b""
    stderr: bytes = b""
    artifact_refs: tuple[str, ...] = ()
    output_cursor: int = 0
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_BOUNDED_RESULT_SCHEMA,
            "process": self.process.to_dict(),
            "stdout": self.stdout.decode("utf-8", errors="replace"),
            "stderr": self.stderr.decode("utf-8", errors="replace"),
            "artifact_refs": list(self.artifact_refs),
            "output_cursor": self.output_cursor,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class OutputFrame:
    start_cursor: int
    end_cursor: int
    stream: str
    data: bytes = b""
    artifact_ref: str = ""
    artifact_offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "start_cursor": self.start_cursor,
            "end_cursor": self.end_cursor,
            "stream": self.stream,
            "bytes": self.end_cursor - self.start_cursor,
        }
        if self.artifact_ref:
            result.update({
                "artifact_ref": self.artifact_ref,
                "artifact_offset": self.artifact_offset,
            })
        else:
            result.update({
                "text": self.data.decode("utf-8", errors="replace"),
                "data_base64": base64.b64encode(self.data).decode("ascii"),
            })
        return result


@dataclass(frozen=True, slots=True)
class OutputPage:
    entity_kind: str
    entity_id: str
    after_cursor: int
    next_cursor: int
    end_cursor: int
    frames: tuple[OutputFrame, ...]
    more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_OUTPUT_SCHEMA,
            "entity": {"kind": self.entity_kind, "id": self.entity_id},
            "after_cursor": self.after_cursor,
            "next_cursor": self.next_cursor,
            "end_cursor": self.end_cursor,
            "more": self.more,
            "frames": [frame.to_dict() for frame in self.frames],
        }


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    sequence: int
    event_id: str
    entity_kind: str
    entity_id: str
    event_type: str
    revision: int
    payload: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_EVENT_SCHEMA,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "entity": {"kind": self.entity_kind, "id": self.entity_id},
            "type": self.event_type,
            "revision": self.revision,
            "payload": json_value(self.payload),
            "created_at": self.created_at,
        }


def coerce_argv(command: str | Sequence[str], *, shell: bool) -> tuple[str, ...]:
    if isinstance(command, str):
        if not shell:
            raise ExecutionValidationError(
                "string commands require shell=True; pass argv for structured execution"
            )
        return (command,)
    return tuple(str(item) for item in command)


__all__ = [
    "ACTIVE_PROCESS_STATES", "ACTIVE_TERMINAL_STATES",
    "EXECUTION_EVENT_SCHEMA", "EXECUTION_OUTPUT_SCHEMA",
    "EXECUTION_PROCESS_SCHEMA", "EXECUTION_TERMINAL_SCHEMA",
    "ExecutionConflict", "ExecutionError", "ExecutionEvent",
    "ExecutionNotFound", "ExecutionOwner", "ExecutionScopeMismatch",
    "ExecutionUnavailable",
    "ExecutionValidationError", "OutputFrame", "OutputPage",
    "PROCESS_STATES", "ProcessRecipe", "ProcessRecord", "RESTART_POLICIES",
    "TERMINAL_STATES", "TerminalRecord", "canonical_json", "coerce_argv",
    "json_value",
]
