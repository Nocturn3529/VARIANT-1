"""Stable errors, limits, and authority records for session mutation."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from core_invariants import canonical_digest, canonical_json
from tool_core import json_safe


# The worker never exposes this proxy as a candidate namespace.  It is the
# authenticated host route used by typed remote handles returned from ordinary
# mutation proxies (connectors, jobs, browser sessions, and future services).
MUTATION_REMOTE_HANDLE_PROXY = "__variant1_internal.remote_handle_dispatch"
MUTATION_REMOTE_HANDLE_ROLE = "remote_handle_dispatch"


class MutationError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details)


class MutationWorkerError(MutationError):
    pass


@dataclass(frozen=True)
class WorkerLimits:
    timeout_s: float = 180.0
    max_processes: int = 8
    process_memory_bytes: int = 1_500_000_000
    job_memory_bytes: int = 2_000_000_000
    cpu_percent: int = 90
    max_frame_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class MutationAuthorityLease:
    """One durable per-chat mutation-write authority generation."""

    write_enabled: bool
    revision: int


def stable_json(value: Any) -> str:
    return canonical_json(json_safe(value))


def stable_digest(value: Any) -> str:
    return canonical_digest(json_safe(value))


def utc_timestamp() -> float:
    return time.time()


__all__ = [
    "MUTATION_REMOTE_HANDLE_PROXY",
    "MUTATION_REMOTE_HANDLE_ROLE",
    "MutationAuthorityLease",
    "MutationError",
    "MutationWorkerError",
    "WorkerLimits",
    "stable_digest",
    "stable_json",
    "utc_timestamp",
]
