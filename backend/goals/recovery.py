"""Conservative startup reconciliation for durable goal supervision."""

from __future__ import annotations

import time
from typing import Any

from core_invariants import exhaustive_keyset_pages
from work_fabric.models import WorkActor


def _all_goals(repository: Any):
    """Yield every goal through a stable keyset scan."""
    yield from exhaustive_keyset_pages(
        lambda cursor, size: repository.scan_goals(
            after_goal_id=str(cursor or ""), limit=size
        ),
        lambda goal: goal.goal_id,
    )


def reconcile_expired_step_leases(
    service: Any,
    *,
    now: float | None = None,
    goal_id: str = "",
) -> dict[str, Any]:
    """Fence interrupted running effects as blocked, never blindly replay them."""

    current = float(time.time() if now is None else now)
    recovered: list[dict[str, Any]] = []
    goals = (
        [service.repository.require_goal(goal_id)]
        if goal_id
        else _all_goals(service.repository)
    )
    for goal in goals:
        for step in service.repository.list_steps(goal.goal_id):
            if step.status not in {"leased", "running"}:
                continue
            if not step.lease_expires_at or step.lease_expires_at > current:
                continue
            latest = service.repository.require_goal(goal.goal_id)
            effects = [item for item in service.repository.list_effects(goal.goal_id)
                       if item.step_id == step.step_id and item.attempt == step.attempt_count]
            unknown = any(item.status in {"dispatched", "unknown_effect"} for item in effects)
            for effect in effects:
                if effect.status != "dispatched":
                    continue
                latest = service.repository.require_goal(goal.goal_id)
                service.repository.update_effect(
                    effect.effect_id,
                    expected_version=latest.version,
                    status="unknown_effect",
                    error="backend ownership ended before effect completion was reconciled",
                    actor=WorkActor("system", "goal-recovery"),
                )
            latest = service.repository.require_goal(goal.goal_id)
            reason = (
                "expired lease has an uncertain external effect; explicit retry required"
                if unknown else "expired step lease; explicit retry required"
            )
            latest, updated = service.repository.finish_step(
                latest.goal_id, step.step_id, expected_version=latest.version,
                status="blocked", error=reason,
                actor=WorkActor("system", "goal-recovery"),
            )
            recovered.append({
                "goal_id": latest.goal_id, "step_id": updated.step_id,
                "status": "unknown_effect" if unknown else "blocked",
            })
    return {"schema": "variant1.goal-recovery.v1", "recovered": recovered}


def recover_goal_supervisors(service: Any, *, now: float | None = None) -> dict[str, Any]:
    """Reconcile step leases and recreate any missing supervisor wake job.

    Work Fabric already recovers supervisor jobs that were durably queued before
    a crash.  This pass closes the narrower crash window between a goal mutation
    and creation of that job.  Version-based idempotency makes repeated startup
    calls harmless.  Event-only waits remain asleep until their matching event;
    timed waits and live step leases receive a future wake without holding a
    scheduler, model, process, or kernel slot.
    """

    current = float(time.time() if now is None else now)
    lease_report = reconcile_expired_step_leases(service, now=current)
    scheduled: list[dict[str, Any]] = []
    for stale in _all_goals(service.repository):
        goal = service.repository.require_goal(stale.goal_id)
        if goal.status not in {
            "queued", "running", "waiting_user", "waiting_external",
        }:
            continue
        pending_waits = service.repository.list_waits(
            goal.goal_id, status="pending",
        )
        wake_candidates = [
            wait.wake_at
            for wait in pending_waits
            if wait.wake_at
        ]
        if any(wait.source in service.wait_resolvers for wait in pending_waits):
            wake_candidates.append(current + 0.5)
        wake_candidates.extend(
            step.lease_expires_at
            for step in service.repository.list_steps(goal.goal_id)
            if step.status in {"leased", "running"} and step.lease_expires_at
        )
        if goal.status in {"queued", "running"}:
            # ``None`` is the stable semantic form of "ready immediately".
            # Passing the wall clock would turn two identical recovery scans
            # into different requests for the same idempotency key.
            available_at = None
        elif wake_candidates:
            wake_at = min(wake_candidates)
            available_at = wake_at if wake_at > current else None
        else:
            # An external/user event is the only legitimate wake source.
            continue
        job = service.supervisor.enqueue(
            goal.goal_id,
            reason="startup_recovery",
            available_at=available_at,
        )
        scheduled.append({
            "goal_id": goal.goal_id,
            "job_id": job.job_id,
            "available_at": job.available_at,
        })
    return {
        "schema": "variant1.goal-startup-recovery.v1",
        "recovered": lease_report["recovered"],
        "scheduled": scheduled,
    }


__all__ = ["reconcile_expired_step_leases", "recover_goal_supervisors"]
