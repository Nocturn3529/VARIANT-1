"""Shared backend invariants used across mounted-core domains."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import asyncio
import hashlib
import json
import math
import os
import sqlite3
import threading
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar


class StrictJSONError(ValueError):
    pass


def strict_json_value(
    value: Any,
    *,
    path: str = "$",
    _seen: set[int] | None = None,
) -> Any:
    """Return a detached strict-JSON value or reject the exact bad path."""

    seen = _seen if _seen is not None else set()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StrictJSONError(f"non-finite number at {path}")
        return value
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise StrictJSONError(f"cyclic JSON value at {path}")
        seen.add(identity)
        try:
            return [
                strict_json_value(
                    item, path=f"{path}[{index}]", _seen=seen
                )
                for index, item in enumerate(value)
            ]
        finally:
            seen.remove(identity)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise StrictJSONError(f"cyclic JSON value at {path}")
        seen.add(identity)
        try:
            output: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise StrictJSONError(
                        f"non-string JSON key at {path}: {key!r}"
                    )
                output[key] = strict_json_value(
                    item, path=f"{path}.{key}", _seen=seen
                )
            return output
        finally:
            seen.remove(identity)
    to_dict = getattr(type(value), "to_dict", None)
    if callable(to_dict):
        identity = id(value)
        if identity in seen:
            raise StrictJSONError(f"cyclic JSON value at {path}")
        seen.add(identity)
        try:
            return strict_json_value(to_dict(value), path=path, _seen=seen)
        finally:
            seen.remove(identity)
    raise StrictJSONError(
        f"unsupported JSON value at {path}: {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        strict_json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8", errors="strict")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def request_fingerprint(operation: str, arguments: Any) -> str:
    """Bind an idempotency identity to an operation and its exact payload."""

    return canonical_digest({
        "operation": str(operation or ""),
        "arguments": strict_json_value(arguments),
    })


@dataclass(frozen=True)
class CancellationProbeResult:
    requested: bool
    authority_failed: bool = False
    error: str = ""


def probe_cancellation(authority: Any) -> CancellationProbeResult:
    """Poll one cancellation authority and fence work if the probe breaks."""

    if authority is None:
        return CancellationProbeResult(False)
    try:
        if hasattr(authority, "is_cancelled"):
            requested = bool(authority.is_cancelled())
        elif hasattr(authority, "is_set"):
            requested = bool(authority.is_set())
        elif callable(authority):
            requested = bool(authority())
        else:
            requested = bool(authority)
        return CancellationProbeResult(requested)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return CancellationProbeResult(
            True,
            authority_failed=True,
            error=f"{type(exc).__name__}: {exc}"[:1000],
        )


def cancellation_is_requested(authority: Any) -> bool:
    return probe_cancellation(authority).requested


_SQLITE_LOCKS_GUARD = threading.Lock()
_SQLITE_WRITER_LOCKS: dict[str, threading.RLock] = {}


def sqlite_writer_lock(path: str) -> threading.RLock:
    """Return the one in-process writer lock for an exact SQLite authority."""

    key = os.path.normcase(os.path.abspath(str(path)))
    with _SQLITE_LOCKS_GUARD:
        return _SQLITE_WRITER_LOCKS.setdefault(key, threading.RLock())


def sqlite_wal_connection(
    path: str,
    *,
    foreign_keys: bool = True,
) -> sqlite3.Connection:
    """Open the shared short-lived durable SQLite connection profile."""

    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def sqlite_session_connection(
    path: str,
    *,
    autocommit: bool = True,
) -> sqlite3.Connection:
    """Open the shared 10-second ASTB/session repository profile."""

    if autocommit:
        connection = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    else:
        connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


@contextmanager
def sqlite_read_connection(connect: Callable[[], T]) -> Iterator[T]:
    """Own and close one short-lived read connection."""

    connection = connect()
    try:
        yield connection
    finally:
        connection.close()  # type: ignore[attr-defined]


@contextmanager
def sqlite_unit_of_work(
    connect: Callable[[], T],
    writer_lock: threading.RLock,
    *,
    fault_name: str = "",
) -> Iterator[T]:
    """Run one atomic SQLite write with rollback and injectable pre-commit fault."""

    with writer_lock:
        connection = connect()
        try:
            with sqlite_transaction(
                connection, immediate=True, fault_name=fault_name
            ):
                yield connection
        finally:
            connection.close()  # type: ignore[attr-defined]


@contextmanager
def sqlite_transaction(
    connection: T,
    *,
    immediate: bool = True,
    fault_name: str = "",
) -> Iterator[T]:
    """Apply the shared commit/rollback boundary to an existing connection."""

    begun = False
    try:
        connection.execute(  # type: ignore[attr-defined]
            "BEGIN IMMEDIATE" if immediate else "BEGIN"
        )
        begun = True
        yield connection
        if fault_name:
            fault_point(fault_name)
        connection.execute("COMMIT")  # type: ignore[attr-defined]
        begun = False
    except BaseException as error:
        if begun:
            try:
                connection.execute("ROLLBACK")  # type: ignore[attr-defined]
            except BaseException as rollback_error:
                add_note = getattr(error, "add_note", None)
                if callable(add_note):
                    add_note(f"SQLite rollback failed: {rollback_error}")
        raise


T = TypeVar("T")
C = TypeVar("C")


def exhaustive_keyset_pages(
    fetch: Callable[[C | None, int], Iterable[T]],
    cursor_of: Callable[[T], C],
    *,
    page_size: int = 500,
) -> Iterator[T]:
    """Yield a recovery scan to exhaustion and reject a non-advancing cursor."""

    size = max(1, int(page_size))
    cursor: C | None = None
    seen: set[Any] = set()
    while True:
        page = list(fetch(cursor, size))
        if not page:
            return
        for item in page:
            yield item
        next_cursor = cursor_of(page[-1])
        try:
            marker = canonical_json(next_cursor)
        except StrictJSONError:
            marker = repr(next_cursor)
        if marker in seen:
            raise RuntimeError("keyset pagination cursor did not advance")
        seen.add(marker)
        cursor = next_cursor
        if len(page) < size:
            return


@dataclass(frozen=True, slots=True)
class CellOrigin:
    chat_id: str
    run_id: str
    outer_tool_call_id: str
    cell_execution_id: str
    nested_call_id: str
    kernel_generation: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "chat_id": self.chat_id,
            "run_id": self.run_id,
            "outer_tool_call_id": self.outer_tool_call_id,
            "cell_execution_id": self.cell_execution_id,
            "nested_call_id": self.nested_call_id,
            "kernel_generation": self.kernel_generation,
        }


class InjectedFault(RuntimeError):
    pass


_FAULTS: ContextVar[frozenset[str]] = ContextVar(
    "variant1_fault_injection", default=frozenset()
)


@contextmanager
def inject_faults(*names: str) -> Iterator[None]:
    selected = frozenset(str(name) for name in names if str(name))
    token = _FAULTS.set(selected)
    try:
        yield
    finally:
        _FAULTS.reset(token)


def fault_point(name: str) -> None:
    if str(name) in _FAULTS.get():
        raise InjectedFault(str(name))


__all__ = [
    "CellOrigin",
    "CancellationProbeResult",
    "InjectedFault",
    "StrictJSONError",
    "cancellation_is_requested",
    "probe_cancellation",
    "canonical_digest",
    "canonical_json",
    "canonical_json_bytes",
    "exhaustive_keyset_pages",
    "fault_point",
    "inject_faults",
    "request_fingerprint",
    "sqlite_read_connection",
    "sqlite_session_connection",
    "sqlite_wal_connection",
    "sqlite_transaction",
    "sqlite_unit_of_work",
    "sqlite_writer_lock",
    "strict_json_value",
]
