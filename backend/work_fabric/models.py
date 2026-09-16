"""Value contracts shared by the Work Fabric repository and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .scope import WorkScope


WORK_EVENT_SCHEMA = "variant1.work-event.v1"
WORK_JOB_SCHEMA = "variant1.work-job.v1"
WORK_OPERATION_SCHEMA = "variant1.work-operation.v1"

JOB_STATES = frozenset({
    "queued",
    "leased",
    "running",
    "waiting",
    "paused",
    "succeeded",
    "failed",
    "cancelled",
    "unknown_effect",
})
JOB_TERMINAL_STATES = frozenset({
    "succeeded",
    "failed",
    "cancelled",
    "unknown_effect",
})
JOB_LEASED_STATES = frozenset({"leased", "running"})

OUTBOX_STATES = frozenset({"pending", "leased", "delivered", "dead"})
OPERATION_STATES = frozenset({
    "planned",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "unknown_effect",
})


class WorkFabricError(RuntimeError):
    """Base exception for typed Work Fabric failures."""


class WorkNotFound(WorkFabricError):
    """A requested durable aggregate does not exist."""


class WorkConflict(WorkFabricError):
    """A compare-and-set or idempotency constraint was lost."""


class InvalidTransition(WorkFabricError):
    """The requested aggregate state transition is illegal."""


class LeaseLost(WorkConflict):
    """A worker no longer owns the epoch required to mutate a leased job."""


class RepositoryCorrupt(WorkFabricError):
    """Persisted Work Fabric data failed structural validation."""


@dataclass(frozen=True)
class WorkActor:
    kind: str = "system"
    actor_id: str = "variant1"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.actor_id}


@dataclass(frozen=True)
class WorkEvent:
    event_id: str
    sequence: int
    aggregate_kind: str
    aggregate_id: str
    aggregate_version: int
    event_type: str
    scope: WorkScope = WorkScope()
    actor: WorkActor = WorkActor()
    correlation_id: str = ""
    causation_id: str = ""
    idempotency_key: str = ""
    request_fingerprint: str = ""
    payload_ref: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORK_EVENT_SCHEMA,
            "event_id": self.event_id,
            "sequence": int(self.sequence),
            "aggregate": {
                "kind": self.aggregate_kind,
                "id": self.aggregate_id,
                "version": int(self.aggregate_version),
            },
            "type": self.event_type,
            "scope": self.scope.to_dict(),
            "actor": self.actor.to_dict(),
            "correlation_id": self.correlation_id or None,
            "causation_id": self.causation_id or None,
            "idempotency_key": self.idempotency_key or None,
            "payload_ref": self.payload_ref or None,
            "payload": dict(self.payload),
            "created_at": float(self.created_at),
        }


@dataclass(frozen=True)
class OutboxItem:
    outbox_id: str
    event: WorkEvent
    status: str
    attempts: int = 0
    available_at: float = 0.0
    lease_owner: str = ""
    lease_epoch: int = 0
    lease_expires_at: float = 0.0
    last_error: str = ""
    created_at: float = 0.0
    delivered_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "event": self.event.to_dict(),
            "status": self.status,
            "attempts": int(self.attempts),
            "available_at": float(self.available_at),
            "lease_owner": self.lease_owner or None,
            "lease_epoch": int(self.lease_epoch),
            "lease_expires_at": float(self.lease_expires_at),
            "last_error": self.last_error or None,
            "created_at": float(self.created_at),
            "delivered_at": float(self.delivered_at),
        }


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    owner_kind: str
    owner_id: str
    kind: str
    status: str
    scope: WorkScope
    priority: int = 0
    revision: int = 1
    input_manifest: dict[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    lease_owner: str = ""
    lease_epoch: int = 0
    lease_expires_at: float = 0.0
    continuation: bool = False
    attempt: int = 0
    max_attempts: int = 1
    retry_policy: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)
    event_cursor: int = 0
    result_ref: str = ""
    diagnostics_ref: str = ""
    idempotency_key: str = ""
    request_fingerprint: str = ""
    cancel_requested: bool = False
    cancel_reason: str = ""
    available_at: float = 0.0
    created_at: float = 0.0
    started_at: float = 0.0
    heartbeat_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in JOB_TERMINAL_STATES

    @property
    def leased(self) -> bool:
        return self.status in JOB_LEASED_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORK_JOB_SCHEMA,
            "job_id": self.job_id,
            "owner": {"kind": self.owner_kind, "id": self.owner_id},
            "kind": self.kind,
            "status": self.status,
            "scope": self.scope.to_dict(),
            "priority": int(self.priority),
            "revision": int(self.revision),
            "input_manifest": dict(self.input_manifest),
            "artifact_refs": list(self.artifact_refs),
            "lease": {
                "owner": self.lease_owner or None,
                "epoch": int(self.lease_epoch),
                "expires_at": float(self.lease_expires_at),
                "continuation": bool(self.continuation),
            },
            "attempt": int(self.attempt),
            "max_attempts": int(self.max_attempts),
            "retry_policy": dict(self.retry_policy),
            "progress": dict(self.progress),
            "event_cursor": int(self.event_cursor),
            "result_ref": self.result_ref or None,
            "diagnostics_ref": self.diagnostics_ref or None,
            "idempotency_key": self.idempotency_key or None,
            "cancel_requested": bool(self.cancel_requested),
            "cancel_reason": self.cancel_reason or None,
            "available_at": float(self.available_at),
            "created_at": float(self.created_at),
            "started_at": float(self.started_at),
            "heartbeat_at": float(self.heartbeat_at),
            "updated_at": float(self.updated_at),
            "completed_at": float(self.completed_at),
            "error": self.error or None,
        }


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    kind: str
    status: str
    scope: WorkScope
    idempotency_key: str = ""
    request_fingerprint: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    effect_ref: str = ""
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in {
            "succeeded", "failed", "cancelled", "unknown_effect"
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WORK_OPERATION_SCHEMA,
            "operation_id": self.operation_id,
            "kind": self.kind,
            "status": self.status,
            "scope": self.scope.to_dict(),
            "idempotency_key": self.idempotency_key or None,
            "request": dict(self.request),
            "response": dict(self.response),
            "effect_ref": self.effect_ref or None,
            "revision": int(self.revision),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "completed_at": float(self.completed_at),
            "error": self.error or None,
        }


@dataclass(frozen=True)
class ProjectionSnapshot:
    name: str
    last_sequence: int
    state: dict[str, Any]
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "last_sequence": int(self.last_sequence),
            "state": dict(self.state),
            "updated_at": float(self.updated_at),
        }


__all__ = [
    "InvalidTransition",
    "JOB_LEASED_STATES",
    "JOB_STATES",
    "JOB_TERMINAL_STATES",
    "JobRecord",
    "LeaseLost",
    "OPERATION_STATES",
    "OUTBOX_STATES",
    "OperationRecord",
    "OutboxItem",
    "ProjectionSnapshot",
    "RepositoryCorrupt",
    "WorkActor",
    "WorkConflict",
    "WorkEvent",
    "WorkFabricError",
    "WorkNotFound",
]
