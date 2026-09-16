"""Durable value contracts for VARIANT-1 goals.

Goals are product aggregates coordinated by Work Fabric.  They are deliberately
independent from the native agent runner: only an injected step handler may
start an agent/child attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


GOAL_SCHEMA = "variant1.goal.v1"
STEP_SCHEMA = "variant1.goal-step.v1"
ATTEMPT_SCHEMA = "variant1.goal-step-attempt.v1"

GOAL_STATES = frozenset({
    "draft", "queued", "running", "waiting_user", "waiting_external",
    "blocked", "paused", "succeeded", "failed", "cancelled", "archived",
})
GOAL_TERMINAL_STATES = frozenset({
    "succeeded", "failed", "cancelled", "archived",
})
STEP_KINDS = frozenset({
    "agent", "python", "process", "child", "verification", "input", "wait",
    "integration",
})
STEP_STATES = frozenset({
    "pending", "ready", "leased", "running", "waiting", "retry_scheduled",
    "succeeded", "failed", "blocked", "skipped", "cancelled",
})
STEP_TERMINAL_STATES = frozenset({
    "succeeded", "failed", "blocked", "skipped", "cancelled",
})
ATTENTION_STATES = frozenset({"open", "answered", "dismissed"})
WAIT_STATES = frozenset({"pending", "satisfied", "cancelled"})
EFFECT_STATES = frozenset({
    "planned", "dispatched", "succeeded", "failed", "cancelled",
    "unknown_effect",
})


class GoalError(RuntimeError):
    """Base class for typed goal failures."""


class GoalNotFound(GoalError):
    """A requested goal or child record does not exist."""


class GoalConflict(GoalError):
    """An optimistic version, lease, or immutable identity was lost."""


class GoalTransitionError(GoalError):
    """A requested deterministic state transition is invalid."""


class GoalValidationError(GoalError):
    """Input does not satisfy the bounded goal schema."""


@dataclass(frozen=True)
class GoalRecord:
    goal_id: str
    owner_chat_id: str
    workspace_id: str
    title: str
    objective: str
    constraints: tuple[Any, ...] = ()
    success_criteria: tuple[Mapping[str, Any], ...] = ()
    completion_policy: Mapping[str, Any] = field(default_factory=dict)
    status: str = "draft"
    priority: int = 0
    version: int = 1
    budget_limits: Mapping[str, Any] = field(default_factory=dict)
    budget_usage: Mapping[str, Any] = field(default_factory=dict)
    deadline: float = 0.0
    pause_reason: str = ""
    active_worktree_id: str = ""
    terminal_summary_ref: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    archived_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in GOAL_TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": GOAL_SCHEMA,
            "goal_id": self.goal_id,
            "owner_chat_id": self.owner_chat_id or None,
            "workspace_id": self.workspace_id or None,
            "title": self.title,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "success_criteria": [dict(item) for item in self.success_criteria],
            "completion_policy": dict(self.completion_policy),
            "status": self.status,
            "priority": int(self.priority),
            "version": int(self.version),
            "budget": {
                "limits": dict(self.budget_limits),
                "usage": dict(self.budget_usage),
            },
            "deadline": float(self.deadline) or None,
            "pause_reason": self.pause_reason or None,
            "active_worktree_id": self.active_worktree_id or None,
            "terminal_summary_ref": self.terminal_summary_ref or None,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "completed_at": float(self.completed_at) or None,
            "archived_at": float(self.archived_at) or None,
        }


@dataclass(frozen=True)
class StepRecord:
    step_id: str
    goal_id: str
    parent_step_id: str
    ordinal: int
    kind: str
    instructions: str
    config: Mapping[str, Any] = field(default_factory=dict)
    status: str = "pending"
    required: bool = True
    version: int = 1
    attempt_count: int = 0
    max_attempts: int = 1
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    native_thread_id: str = ""
    snapshot_cursor: Mapping[str, Any] = field(default_factory=dict)
    child_id: str = ""
    process_id: str = ""
    worktree_id: str = ""
    wait_spec: Mapping[str, Any] = field(default_factory=dict)
    verification_spec: Mapping[str, Any] = field(default_factory=dict)
    result_ref: str = ""
    error_ref: str = ""
    error: str = ""
    lease_owner: str = ""
    lease_epoch: int = 0
    lease_expires_at: float = 0.0
    available_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in STEP_TERMINAL_STATES

    def to_dict(self, *, dependencies: tuple[str, ...] = ()) -> dict[str, Any]:
        return {
            "schema": STEP_SCHEMA,
            "step_id": self.step_id,
            "goal_id": self.goal_id,
            "parent_step_id": self.parent_step_id or None,
            "ordinal": int(self.ordinal),
            "dependencies": list(dependencies),
            "kind": self.kind,
            "instructions": self.instructions,
            "config": dict(self.config),
            "status": self.status,
            "required": bool(self.required),
            "version": int(self.version),
            "attempt_count": int(self.attempt_count),
            "max_attempts": int(self.max_attempts),
            "retry_policy": dict(self.retry_policy),
            "native_thread_id": self.native_thread_id or None,
            "snapshot_cursor": dict(self.snapshot_cursor),
            "child_id": self.child_id or None,
            "process_id": self.process_id or None,
            "worktree_id": self.worktree_id or None,
            "wait_spec": dict(self.wait_spec),
            "verification_spec": dict(self.verification_spec),
            "result_ref": self.result_ref or None,
            "error_ref": self.error_ref or None,
            "error": self.error or None,
            "lease": {
                "owner": self.lease_owner or None,
                "epoch": int(self.lease_epoch),
                "expires_at": float(self.lease_expires_at) or None,
            },
            "available_at": float(self.available_at),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "completed_at": float(self.completed_at) or None,
        }


@dataclass(frozen=True)
class StepAttemptRecord:
    attempt_id: str
    goal_id: str
    step_id: str
    attempt: int
    status: str
    snapshot_thread_id: str = ""
    snapshot_cursor: Mapping[str, Any] = field(default_factory=dict)
    machine_revision: str = ""
    native_run_id: str = ""
    last_receipt_id: str = ""
    result_ref: str = ""
    error_ref: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ATTEMPT_SCHEMA,
            "attempt_id": self.attempt_id,
            "goal_id": self.goal_id,
            "step_id": self.step_id,
            "attempt": int(self.attempt),
            "status": self.status,
            "snapshot_thread_id": self.snapshot_thread_id or None,
            "snapshot_cursor": dict(self.snapshot_cursor),
            "machine_revision": self.machine_revision or None,
            "native_run_id": self.native_run_id or None,
            "last_receipt_id": self.last_receipt_id or None,
            "result_ref": self.result_ref or None,
            "error_ref": self.error_ref or None,
            "started_at": float(self.started_at),
            "updated_at": float(self.updated_at),
            "completed_at": float(self.completed_at) or None,
        }


@dataclass(frozen=True)
class AttentionRecord:
    attention_id: str
    goal_id: str
    step_id: str
    kind: str
    status: str
    prompt: str
    schema: Mapping[str, Any] = field(default_factory=dict)
    response: Any = None
    version: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    resolved_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attention_id": self.attention_id, "goal_id": self.goal_id,
            "step_id": self.step_id or None, "kind": self.kind,
            "status": self.status, "prompt": self.prompt,
            "schema": dict(self.schema), "response": self.response,
            "version": int(self.version), "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at or None,
        }


@dataclass(frozen=True)
class WaitRecord:
    wait_id: str
    goal_id: str
    step_id: str
    source: str
    matcher: Mapping[str, Any]
    status: str = "pending"
    wake_at: float = 0.0
    result: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = ""
    created_at: float = 0.0
    satisfied_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "wait_id": self.wait_id, "goal_id": self.goal_id,
            "step_id": self.step_id, "source": self.source,
            "matcher": dict(self.matcher), "status": self.status,
            "wake_at": self.wake_at or None, "result": dict(self.result),
            "event_id": self.event_id or None, "created_at": self.created_at,
            "satisfied_at": self.satisfied_at or None,
        }


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    goal_id: str
    step_id: str
    attempt: int
    sequence: int
    kind: str
    status: str
    idempotency_key: str
    request: Mapping[str, Any] = field(default_factory=dict)
    response: Mapping[str, Any] = field(default_factory=dict)
    receipt_id: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id, "goal_id": self.goal_id,
            "step_id": self.step_id, "attempt": self.attempt,
            "sequence": self.sequence, "kind": self.kind,
            "status": self.status, "idempotency_key": self.idempotency_key,
            "request": dict(self.request), "response": dict(self.response),
            "receipt_id": self.receipt_id or None, "error": self.error or None,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class GoalArtifactRecord:
    link_id: str
    goal_id: str
    step_id: str
    artifact_ref: str
    role: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id, "goal_id": self.goal_id,
            "step_id": self.step_id or None, "artifact_ref": self.artifact_ref,
            "role": self.role, "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


__all__ = [
    "ATTENTION_STATES", "EFFECT_STATES", "GOAL_STATES",
    "GOAL_TERMINAL_STATES", "STEP_KINDS", "STEP_STATES",
    "STEP_TERMINAL_STATES", "WAIT_STATES", "AttentionRecord", "EffectRecord",
    "GoalArtifactRecord", "GoalConflict", "GoalError", "GoalNotFound",
    "GoalRecord", "GoalTransitionError", "GoalValidationError", "StepAttemptRecord",
    "StepRecord", "WaitRecord",
]
