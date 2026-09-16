"""VARIANT-1's durable backend execution-host foundation.

Importing this package does not open SQLite, spawn a process, or probe WSL/SSH.
Use :func:`create_execution_runtime` for explicit composition.
"""

from __future__ import annotations

from .models import (
    BoundedProcessResult,
    ExecutionConflict,
    ExecutionError,
    ExecutionNotFound,
    ExecutionOwner,
    ExecutionScopeMismatch,
    ExecutionUnavailable,
    ExecutionValidationError,
    OutputFrame,
    OutputPage,
    ProcessRecipe,
    ProcessRecord,
    TerminalRecord,
)
from .profiles import ExecutionProfileRegistry, ResolvedProfile
from .repository import ExecutionRepository
from .service import ExecutionRuntime, ProcessService, TerminalService


def create_execution_runtime(**kwargs):
    from .service import create_execution_runtime as create
    return create(**kwargs)


__all__ = [
    "BoundedProcessResult", "ExecutionConflict", "ExecutionError", "ExecutionNotFound",
    "ExecutionOwner", "ExecutionProfileRegistry",
    "ExecutionScopeMismatch",
    "ExecutionRepository", "ExecutionRuntime", "ExecutionUnavailable",
    "ExecutionValidationError",
    "OutputFrame", "OutputPage", "ProcessRecipe", "ProcessRecord",
    "ProcessService", "ResolvedProfile", "TerminalRecord", "TerminalService",
    "create_execution_runtime",
]
