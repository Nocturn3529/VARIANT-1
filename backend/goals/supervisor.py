"""Deterministic, resource-releasing goal supervisor."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import time
import threading
import weakref
from typing import Any

from work_fabric.jobs import JobResult
from work_fabric.models import WorkActor, WorkConflict

from .conditions import budget_allows, dependency_readiness, wait_is_ready
from .executor import StepExecutionContext, StepExecutionResult, StepExecutor
from .models import GoalRecord, StepRecord, GoalConflict
from .verification import verify_goal


GOAL_SUPERVISOR_JOB = "goal.supervise.v1"


class GoalSupervisor:
    """One-tick-at-a-time supervisor; it owns no durable live resources."""

    def __init__(self, service: Any, *, executor: StepExecutor | None = None,
                 worker_id: str = "goal-supervisor", max_dispatch: int = 4,
                 step_lease_ttl_s: float = 60.0) -> None:
        self.service = service
        self.repository = service.repository
        self.work = service.work
        self.executor = executor or StepExecutor()
        self.worker_id = str(worker_id or "goal-supervisor")
        self.max_dispatch = max(1, min(32, int(max_dispatch)))
        self.step_lease_ttl_s = max(5.0, float(step_lease_ttl_s))
        self._tick_locks = weakref.WeakValueDictionary()

    def register_work_handler(self) -> None:
        self.work.register_job_handler(GOAL_SUPERVISOR_JOB, self._work_handler)

    def enqueue(
        self, goal_id: str, *, reason: str = "state_changed",
        available_at: float | None = None,
        dedupe_key: str = "",
    ):
        goal = self.repository.require_goal(goal_id)
        scope = self.repository._scope(goal)
        key = (
            f"goal-supervise:{goal.goal_id}:{str(dedupe_key)}"
            if str(dedupe_key or "").strip()
            else f"goal-supervise:{goal.goal_id}:v{goal.version}"
        )
        existing = self.work.jobs.get_by_idempotency(
            owner_kind="goal", owner_id=goal.goal_id,
            kind=GOAL_SUPERVISOR_JOB, idempotency_key=key,
        )
        if existing is not None:
            if (existing.scope != scope
                    or existing.input_manifest.get("goal_id") != goal.goal_id
                    or (not dedupe_key and existing.input_manifest.get("goal_version") != goal.version)):
                raise WorkConflict("goal supervisor occurrence identity conflict")
            return existing
        return self.work.jobs.create(
            GOAL_SUPERVISOR_JOB, owner_kind="goal", owner_id=goal.goal_id,
            scope=scope, priority=goal.priority,
            input_manifest={"goal_id": goal.goal_id, "reason": str(reason),
                            "goal_version": goal.version},
            retry_policy={"on_lease_expiry": "retry", "base_delay_s": 0.25,
                          "max_delay_s": 5.0}, max_attempts=5,
            idempotency_key=key,
            available_at=available_at,
        )

    async def _work_handler(self, execution) -> JobResult:
        goal_id = str((execution.job.input_manifest or {}).get("goal_id") or "")
        result = await self.tick(goal_id, cancellation_requested=getattr(execution, "cancellation_requested", None))
        goal = self.repository.require_goal(goal_id)
        wake_at_values: list[float] = []
        if goal.status in {"queued", "running", "waiting_user", "waiting_external"}:
            now = time.time()
            steps = self.repository.list_steps(goal_id)
            by_id = {step.step_id: step for step in steps}
            dependencies = self.repository.dependencies(goal_id)
            for step in steps:
                if step.status not in {"pending", "ready", "retry_scheduled"}:
                    continue
                decision = dependency_readiness(
                    step, dependencies.get(step.step_id, ()), by_id, now=now,
                )
                if decision.state in {"ready", "blocked"}:
                    wake_at_values.append(max(0.0, step.available_at))
                elif step.status == "retry_scheduled" and step.available_at > now:
                    wake_at_values.append(step.available_at)
            pending_waits = self.repository.list_waits(goal_id, status="pending")
            wake_at_values.extend(
                wait.wake_at
                for wait in pending_waits
                if wait.wake_at
            )
            if any(wait.source in self.service.wait_resolvers for wait in pending_waits):
                wake_at_values.append(time.time() + 0.5)
            wake_at_values.extend(
                step.lease_expires_at
                for step in steps
                if step.status in {"leased", "running"} and step.lease_expires_at
            )
        if wake_at_values:
            wake_at = min(wake_at_values)
            # The current job remains running until this handler returns. A
            # version-only idempotency key would therefore resolve back to the
            # current job whenever polling did not mutate the goal. Fence one
            # deterministic successor to this exact execution instead.
            self.enqueue(
                goal_id,
                reason="time_wait",
                available_at=wake_at if wake_at > time.time() else None,
                dedupe_key=f"poll-after:{execution.job.job_id}",
            )
            result = {**result, "next_wake_at": wake_at,
                      "waiting_job_holds_execution_slot": False}
        return JobResult(progress=result)

    async def _prepare_ready(self, goal: GoalRecord, now: float) -> GoalRecord:
        steps = self.repository.list_steps(goal.goal_id)
        dependencies = self.repository.dependencies(goal.goal_id)
        by_id = {step.step_id: step for step in steps}
        for original in steps:
            current = self.repository.get_step(goal.goal_id, original.step_id)
            if current is None or current.status not in {"pending", "retry_scheduled", "ready"}:
                continue
            decision = dependency_readiness(
                current, dependencies.get(current.step_id, ()), by_id, now=now
            )
            if decision.state in {"ready", "blocked"} and current.status != decision.state:
                goal, updated = self.repository.set_step_status(
                    goal.goal_id, current.step_id, decision.state,
                    expected_version=goal.version, expected_step_version=current.version,
                    reason=decision.reason, actor=WorkActor("system", self.worker_id),
                )
                by_id[updated.step_id] = updated
        return goal

    async def _wake_due(self, goal: GoalRecord, now: float) -> GoalRecord:
        for wait in self.repository.list_waits(goal.goal_id, status="pending"):
            resolver = self.service.wait_resolvers.get(wait.source)
            resolved = None
            if resolver is not None:
                raw = resolver(wait)
                resolved = await raw if inspect.isawaitable(raw) else raw
                if resolved is None or resolved is False:
                    continue
                if not isinstance(resolved, dict):
                    resolved = {"value": resolved}
            elif wait_is_ready(wait, now=now):
                resolved = {
                    "source": wait.source,
                    "wake_at": wait.wake_at,
                    "satisfied_by": "supervisor_clock",
                }
            if resolved is not None:
                # A resolver may reconcile its external effect ledger before
                # returning. Reload the aggregate version before satisfying
                # the wait so that evidence projection and wake remain CAS-safe.
                goal = self.repository.require_goal(goal.goal_id)
                goal, _wait, _step = self.repository.satisfy_wait(
                    wait.wait_id, expected_version=goal.version,
                    result=resolved,
                    actor=WorkActor("system", self.worker_id),
                )
        return goal

    async def _verification_result(
        self, goal: GoalRecord, step: StepRecord
    ) -> StepExecutionResult:
        criteria = step.verification_spec.get("criteria")
        target = goal
        if isinstance(criteria, list):
            target = replace(goal, success_criteria=tuple(
                dict(item) for item in criteria if isinstance(item, dict)
            ))
        # A verification step checks its own persisted criteria.  Requiring
        # later plan steps here would deadlock an otherwise valid DAG; the
        # aggregate-level verifier below still requires every required step
        # before the goal can succeed.
        steps = [replace(step, status="succeeded")]
        evidence = self.repository.state_get(goal.goal_id, "evidence") or {}
        report = verify_goal(
            target, steps, evidence=evidence if isinstance(evidence, dict) else {},
            artifacts=self.repository.list_artifacts(goal.goal_id),
        )
        return StepExecutionResult(
            status="succeeded" if report.passed else "failed",
            error="" if report.passed else "deterministic verification failed",
            metadata={"verification": report.to_dict()},
        )

    async def _apply_result(
        self, goal: GoalRecord, step: StepRecord, attempt: Any,
        result: StepExecutionResult,
    ) -> GoalRecord:
        current = self.repository.get_step(goal.goal_id, step.step_id)
        if current is None:
            return goal
        origin_attempt = int(getattr(attempt, "attempt", 0) or 0)
        if (
            current.attempt_count != origin_attempt
            or current.lease_epoch != step.lease_epoch
            or current.lease_owner != step.lease_owner
            or current.status not in {"leased", "running"}
            or (current.lease_expires_at and current.lease_expires_at <= time.time())
        ):
            # The originating handler outlived its lease. Its result has no
            # authority over a later retry and is deliberately ignored.
            return self.repository.require_goal(goal.goal_id)
        goal = self.repository.require_goal(goal.goal_id)
        projection = dict(result.snapshot_projection)
        if projection:
            cursor = projection.get("snapshot_cursor")
            if isinstance(cursor, dict):
                self.repository.update_attempt_projection(
                    goal.goal_id, current.step_id, expected_version=goal.version,
                    attempt=origin_attempt, snapshot_cursor=cursor,
                    machine_revision=str(projection.get("machine_revision") or ""),
                    native_run_id=str(projection.get("native_run_id") or ""),
                    last_receipt_id=str(projection.get("last_receipt_id") or ""),
                    actor=WorkActor("system", self.worker_id),
                )
                goal = self.repository.require_goal(goal.goal_id)
        if result.status == "waiting":
            self.repository.create_wait(
                goal.goal_id, current.step_id, expected_version=goal.version,
                source=result.wait_source or "external",
                matcher=result.wait_matcher, wake_at=result.wake_at,
                actor=WorkActor("system", self.worker_id),
            )
            return self.repository.require_goal(goal.goal_id)
        if result.status == "retry_scheduled":
            goal, _ = self.repository.finish_step(
                goal.goal_id, current.step_id, expected_version=goal.version,
                status="failed", error_ref=result.diagnostics_ref, error=result.error,
                expected_attempt=origin_attempt,
                lease_owner=step.lease_owner,
                lease_epoch=step.lease_epoch,
                actor=WorkActor("system", self.worker_id),
            )
            if current.attempt_count >= current.max_attempts:
                return goal
            delay = float(current.retry_policy.get("base_delay_s") or 0.5)
            goal, _ = self.repository.retry_step(
                goal.goal_id, current.step_id, expected_version=goal.version,
                delay_s=delay, actor=WorkActor("system", self.worker_id),
            )
            return goal
        goal, _ = self.repository.finish_step(
            goal.goal_id, current.step_id, expected_version=goal.version,
            status=result.status, result_ref=result.result_ref,
            error_ref=result.diagnostics_ref, error=result.error,
            expected_attempt=origin_attempt,
            lease_owner=step.lease_owner,
            lease_epoch=step.lease_epoch,
            actor=WorkActor("system", self.worker_id),
        )
        return goal

    async def _execute_step_owned(self, context: StepExecutionContext):
        local_stop = threading.Event()
        heartbeat_stop = asyncio.Event()
        original_stop = context.cancellation_requested
        def should_stop():
            if local_stop.is_set() or original_stop():
                return True
            current = self.repository.get_step(context.goal.goal_id, context.step.step_id)
            return (current is None or current.status not in {"leased", "running"}
                    or current.lease_owner != context.step.lease_owner
                    or current.lease_epoch != context.step.lease_epoch
                    or current.lease_expires_at <= time.time())
        context = replace(context, cancellation_requested=should_stop)
        def renew():
            if should_stop():
                raise GoalConflict("step execution ownership revoked")
            self.repository.heartbeat_step(
                context.goal.goal_id, context.step.step_id,
                lease_owner=context.step.lease_owner, lease_epoch=context.step.lease_epoch,
                lease_ttl_s=self.step_lease_ttl_s,
            )
        renew()
        handler = asyncio.create_task(self.executor.execute(context))
        async def heartbeat():
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), self.step_lease_ttl_s / 3)
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await asyncio.to_thread(renew)
                except Exception:
                    local_stop.set()
                    handler.cancel()
                    return
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            return await asyncio.shield(handler)
        finally:
            local_stop.set()
            heartbeat_stop.set()
            heartbeat_task.cancel()
            if not handler.done():
                handler.cancel()
            await asyncio.gather(heartbeat_task, handler, return_exceptions=True)

    async def _dispatch(self, goal: GoalRecord, cancellation_requested=None) -> GoalRecord:
        ready = [step for step in self.repository.list_steps(goal.goal_id)
                 if step.status == "ready"][: self.max_dispatch]
        external: list[tuple[StepRecord, Any, StepExecutionContext]] = []
        for step in ready:
            goal, leased, attempt = self.repository.lease_step(
                goal.goal_id, step.step_id, expected_version=goal.version,
                lease_owner=self.worker_id, actor=WorkActor("system", self.worker_id),
                lease_ttl_s=self.step_lease_ttl_s,
            )
            goal, running, attempt = self.repository.start_step(
                goal.goal_id, step.step_id, expected_version=goal.version,
                lease_owner=leased.lease_owner, lease_epoch=leased.lease_epoch,
                actor=WorkActor("system", self.worker_id),
            )
            if running.kind == "wait":
                spec = dict(running.wait_spec or running.config)
                source = str(spec.get("source") or "time")
                try:
                    wake_at = float(spec.get("wake_at") or 0)
                    delay_s = float(spec.get("delay_s") or 0)
                except (TypeError, ValueError, OverflowError):
                    wake_at = 0
                    delay_s = 0
                if source == "time" and wake_at <= 0 and delay_s > 0:
                    wake_at = time.time() + delay_s
                if source == "time" and wake_at <= 0:
                    goal = await self._apply_result(
                        goal, running, attempt,
                        StepExecutionResult(
                            status="failed",
                            error="time wait requires a positive wake_at or delay_s",
                        ),
                    )
                    continue
                self.repository.create_wait(
                    goal.goal_id, running.step_id, expected_version=goal.version,
                    source=source,
                    matcher=dict(spec.get("matcher") or {}),
                    wake_at=wake_at,
                    actor=WorkActor("system", self.worker_id),
                )
                goal = self.repository.require_goal(goal.goal_id)
            elif running.kind == "input":
                spec = dict(running.config)
                self.repository.request_input(
                    goal.goal_id, running.step_id, expected_version=goal.version,
                    prompt=str(spec.get("prompt") or running.instructions or "Input required"),
                    schema=dict(spec.get("schema") or {}),
                    actor=WorkActor("system", self.worker_id),
                )
                goal = self.repository.require_goal(goal.goal_id)
            elif running.kind == "verification":
                result = await self._verification_result(goal, running)
                goal = await self._apply_result(goal, running, attempt, result)
            else:
                context = StepExecutionContext(
                    goal=goal, step=running, attempt=attempt,
                    scope=self.repository._scope(
                        goal, step_id=running.step_id, attempt=attempt.attempt
                    ).to_dict(),
                    cancellation_requested=(
                        lambda goal_id=goal.goal_id: (
                            self.repository.require_goal(goal_id).status == "cancelled"
                            or (callable(cancellation_requested) and cancellation_requested())
                        )
                    ),
                )
                external.append((running, attempt, context))
        if external:
            results = await asyncio.gather(
                *(self._execute_step_owned(item[2]) for item in external),
                return_exceptions=True,
            )
            for (step, attempt, _context), raw in zip(external, results):
                result = (
                    StepExecutionResult(status="cancelled", error="step execution cancelled")
                    if isinstance(raw, asyncio.CancelledError) else
                    StepExecutionResult(status="failed", error=f"{type(raw).__name__}: {raw}")
                    if isinstance(raw, BaseException) else raw
                )
                goal = self.repository.require_goal(goal.goal_id)
                goal = await self._apply_result(goal, step, attempt, result)
        return goal

    def _aggregate_status(self, goal: GoalRecord) -> str:
        steps = self.repository.list_steps(goal.goal_id)
        if not steps:
            return "blocked"
        if all((not step.required) or step.status in {"succeeded", "skipped"} for step in steps):
            return "verify"
        if any(step.status == "waiting" and step.kind == "input" for step in steps):
            return "waiting_user"
        if any(step.status == "waiting" for step in steps):
            return "waiting_external"
        if any(step.required and step.status in {"failed", "blocked", "cancelled"} for step in steps):
            return "blocked"
        return "running"

    async def tick(self, goal_id: str, *, now: float | None = None, cancellation_requested=None) -> dict[str, Any]:
        lock = self._tick_locks.setdefault(str(goal_id), asyncio.Lock())
        async with lock:
            return await self._tick_owned(goal_id, now=now, cancellation_requested=cancellation_requested)

    async def _tick_owned(self, goal_id: str, *, now: float | None = None, cancellation_requested=None) -> dict[str, Any]:
        current_time = float(time.time() if now is None else now)
        # A retried Work job is also the recovery boundary for an interrupted
        # step attempt.  Unknown external effects are fenced before readiness
        # is recomputed; they are never replayed implicitly.
        from .recovery import reconcile_expired_step_leases
        reconcile_expired_step_leases(
            self.service, now=current_time, goal_id=goal_id
        )
        goal = self.repository.require_goal(goal_id)
        if goal.status in {"draft", "paused", "succeeded", "failed", "cancelled", "archived"}:
            return {"goal_id": goal.goal_id, "status": goal.status,
                    "dispatched": 0, "idle": True}
        if goal.status == "queued":
            goal = self.repository.transition_goal(
                goal.goal_id, "running", expected_version=goal.version,
                actor=WorkActor("system", self.worker_id),
            )
        budget = budget_allows(goal, now=current_time)
        if not budget.allowed:
            goal = self.repository.transition_goal(
                goal.goal_id, "paused", expected_version=goal.version,
                reason=budget.reason, actor=WorkActor("system", self.worker_id),
                event_type="goal.budget_exhausted",
            )
            return {"goal_id": goal.goal_id, "status": goal.status,
                    "paused_reason": budget.reason, "dispatched": 0}
        goal = await self._wake_due(goal, current_time)
        goal = self.repository.require_goal(goal_id)
        if goal.status in {"paused", "succeeded", "failed", "cancelled", "archived"}:
            return {"goal_id": goal.goal_id, "status": goal.status,
                    "dispatched": 0, "idle": True}
        goal = await self._prepare_ready(goal, current_time)
        before_attempts = sum(step.attempt_count for step in self.repository.list_steps(goal.goal_id))
        goal = await self._dispatch(goal, cancellation_requested=cancellation_requested)
        after_attempts = sum(step.attempt_count for step in self.repository.list_steps(goal.goal_id))
        goal = self.repository.require_goal(goal.goal_id)
        if goal.status in {"paused", "succeeded", "failed", "cancelled", "archived"}:
            return {
                "schema": "variant1.goal-supervisor-tick.v1",
                "goal_id": goal.goal_id,
                "status": goal.status,
                "goal_version": goal.version,
                "dispatched": max(0, after_attempts - before_attempts),
                "supported_handlers": list(self.executor.supported_kinds),
                "resources_held_while_waiting": False,
                "idle": True,
            }
        target = self._aggregate_status(goal)
        outcome=self.repository.state_get(goal.goal_id,'objective_outcome') or {}
        if target=='blocked' and goal.completion_policy.get('auto_continue') and outcome.get('status')=='continuing':
            step=self.repository.get_step(goal.goal_id,str(outcome.get('step_id') or ''))
            if step is not None and step.status=='blocked':
                goal,_=self.repository.retry_step(goal.goal_id,step.step_id,
                    expected_version=goal.version,delay_s=0,continuation=True,actor=WorkActor('system',self.worker_id))
                target='running'
        if target == "verify":
            report = self.service.verification_report(goal.goal_id)
            target = "succeeded" if report.passed else "blocked"
            reason = "" if report.passed else "required deterministic criteria are not satisfied"
        else:
            reason = "required step failed or is blocked" if target == "blocked" else ""
        goal = self.repository.require_goal(goal.goal_id)
        if target != goal.status:
            goal = self.repository.transition_goal(
                goal.goal_id, target, expected_version=goal.version, reason=reason,
                actor=WorkActor("system", self.worker_id),
            )
        return {
            "schema": "variant1.goal-supervisor-tick.v1", "goal_id": goal.goal_id,
            "status": goal.status, "goal_version": goal.version,
            "dispatched": max(0, after_attempts - before_attempts),
            "supported_handlers": list(self.executor.supported_kinds),
            "resources_held_while_waiting": False,
        }


__all__ = ["GOAL_SUPERVISOR_JOB", "GoalSupervisor"]
