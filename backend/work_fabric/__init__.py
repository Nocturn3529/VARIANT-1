"""Lightweight Work Fabric value exports.

Keep repository/service imports explicit (``work_fabric.service``) so early
run-context and native-snapshot imports do not initialize SQLite or create
host-runtime dependency cycles.
"""

from .models import (
    InvalidTransition,
    JobRecord,
    LeaseLost,
    OperationRecord,
    OutboxItem,
    ProjectionSnapshot,
    RepositoryCorrupt,
    WorkActor,
    WorkConflict,
    WorkEvent,
    WorkFabricError,
    WorkNotFound,
)
from .scope import (
    EMPTY_WORK_SCOPE,
    WorkScope,
    bind_work_scope,
    coerce_work_scope,
    current_work_scope,
    replace_current_work_scope,
)


__all__ = [
    "EMPTY_WORK_SCOPE",
    "InvalidTransition",
    "JobRecord",
    "LeaseLost",
    "OperationRecord",
    "OutboxItem",
    "ProjectionSnapshot",
    "RepositoryCorrupt",
    "WorkActor",
    "WorkConflict",
    "WorkEvent",
    "WorkFabricError",
    "WorkNotFound",
    "WorkScope",
    "bind_work_scope",
    "coerce_work_scope",
    "current_work_scope",
    "replace_current_work_scope",
]
