"""Concrete automation and notification service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
import uuid

from automation import runner as automation_runner
from automation.store import cancellation_epoch, execution_task_snapshot
from work_fabric.jobs import JobExecutionContext, JobResult, UnknownJobEffect
from work_fabric.models import JOB_STATES, JOB_TERMINAL_STATES, WorkConflict
from work_fabric.scope import WorkScope

if TYPE_CHECKING:
    from app_host import AppHost


AUTOMATION_EXECUTION_JOB = "automation.execute.v1"
AUTOMATION_CANCEL_BATCH_SIZE = 5000


@dataclass
class WorkflowService:
    host: "AppHost"
    _work: object | None = field(default=None, init=False, repr=False)
    _automation_locks: dict[str, asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False,
    )

    def bind_work(self, work) -> None:
        if self._work is work:
            return
        if self._work is not None:
            raise RuntimeError("WorkflowService is already bound to Work Fabric")
        self._work = work
        work.register_job_handler(AUTOMATION_EXECUTION_JOB, self._automation_job)
        work.events.subscribe(self._automation_work_event)

    def _automation_work_event(self, event) -> None:
        """Finalize durable trigger claims only after the Work job is terminal."""

        if (
            self._work is None
            or str(getattr(event, "aggregate_kind", "")) != "job"
            or str(getattr(event, "event_type", "")) not in {
                "job.succeeded", "job.failed", "job.cancelled", "job.unknown_effect",
            }
        ):
            return
        job = self._work.jobs.get(str(getattr(event, "aggregate_id", "") or ""))
        if job is None or job.kind != AUTOMATION_EXECUTION_JOB:
            return
        self._finalize_automation_claim(job)

    def _finalize_automation_claim(self, job) -> None:
        manifest = dict(job.input_manifest or {})
        automation_id = str(job.owner_id or "")
        claim_id = str(manifest.get("claim_id") or "")
        claim_kind = str(manifest.get("claim_kind") or "")
        if not claim_id:
            return
        status = str((job.progress or {}).get("status") or job.status)
        if claim_kind == "trigger":
            self.host.automations.complete_trigger_claim(
                automation_id, claim_id, status=status,
            )
        elif claim_kind == "scheduled":
            if status == "skipped":
                self.host.automations.release_skipped_scheduled_claim(
                    automation_id, claim_id,
                )
            else:
                self.host.automations.complete_scheduled_claim(
                    automation_id, claim_id, status=status,
                )
        automation_runner.release_claim_tracking(claim_id)

    async def _automation_job(
        self, execution: JobExecutionContext,
    ) -> JobResult:
        manifest = dict(execution.job.input_manifest or {})
        task = self._task_with_model_route(dict(manifest.get("task") or {}))
        automation_id = str(task.get("id") or execution.job.owner_id)
        claim_id = str(manifest.get("claim_id") or "")
        claim_kind = str(manifest.get("claim_kind") or "")
        trigger_source = str(manifest.get("trigger_source") or "trigger")
        accepted_cancellation_epoch = max(
            0, int(manifest.get("cancellation_epoch") or 0),
        )
        lock = self._automation_locks.setdefault(automation_id, asyncio.Lock())
        async with lock:
            if execution.cancellation_requested():
                raise asyncio.CancelledError
            live_task = self.host.automations.get(automation_id)
            explicit_manual = trigger_source == "manual" and not claim_id
            live_cancellation_epoch = cancellation_epoch(live_task)
            cancellation_epoch_matches = (
                accepted_cancellation_epoch == live_cancellation_epoch
            )
            claim_active = (
                self.host.automations.claim_is_active(
                    automation_id, claim_id, claim_kind=claim_kind,
                )
                if claim_id else False
            )
            if (
                live_task is None
                or not cancellation_epoch_matches
                or (claim_id and not claim_active)
                or (not claim_id and not explicit_manual and not live_task.get("enabled"))
            ):
                if live_task is None:
                    reason = "automation_deleted"
                elif not cancellation_epoch_matches:
                    reason = "automation_cancellation_epoch_advanced"
                elif not live_task.get("enabled") and not explicit_manual:
                    reason = "automation_disabled"
                elif claim_id and not claim_active:
                    reason = "automation_claim_retired"
                else:
                    reason = "automation_disabled"
                execution.service.cancel(
                    execution.job.job_id,
                    reason=reason,
                )
                raise asyncio.CancelledError
            raw_result = await automation_runner.execute_automation(
                self.host.automation_ports(),
                task,
                payload=str(manifest.get("payload") or ""),
                cancellation_requested=execution.cancellation_requested,
            )
        result = dict(raw_result or {})
        status = str(result.get("status") or "").strip().lower()
        detail = str(
            result.get("diagnostic")
            or result.get("reply")
            or "automation execution failed"
        )
        if status in {"cancelled", "canceled"}:
            if not execution.cancellation_requested():
                execution.service.cancel(
                    execution.job.job_id,
                    reason="native automation worker cancelled",
                )
            raise asyncio.CancelledError
        if status == "truncated":
            # Tool effects may already have committed before the model ran out
            # of output. Do not replay the whole automation as an ordinary
            # transient failure; preserve the partial result as unknown-effect
            # evidence for explicit reconciliation.
            raise UnknownJobEffect(detail)
        if status not in {"completed", "ok", "skipped"}:
            raise RuntimeError(detail)
        return JobResult(progress={
            "phase": "complete",
            "status": status,
            "automation_id": automation_id,
            "trigger_source": str(manifest.get("trigger_source") or "trigger"),
        })

    async def notify_proactive(
        self,
        mood: str,
        text: str,
        *,
        source: str = "",
        title: str = "",
        meta: dict | None = None,
    ) -> None:
        await self.host.hub.broadcast({
            "type": "proactive",
            "mood": mood or "neutral",
            "text": str(text or ""),
            "source": str(source or ""),
            "title": str(title or ""),
            "meta": dict(meta or {}),
        })

    async def run_automation(
        self,
        task: dict,
        payload: str = "",
        trigger_source: str = "trigger",
    ) -> str:
        if self._work is None:
            raise RuntimeError("automation execution requires Work Fabric")
        automation_id = str((task or {}).get("id") or "anonymous")
        trigger_claim = str((task or {}).get("_trigger_claim_id") or "")
        scheduled_claim = str((task or {}).get("_scheduled_claim_id") or "")
        claim_id = trigger_claim or scheduled_claim
        claim_kind = "trigger" if trigger_claim else "scheduled" if scheduled_claim else ""
        if claim_id and not self.host.automations.claim_is_active(
            automation_id, claim_id, claim_kind=claim_kind,
        ):
            automation_runner.release_claim_tracking(claim_id)
            return ""
        request_key = claim_id or f"manual:{uuid.uuid4().hex}"
        idempotency_key = f"automation:{automation_id}:{request_key}"
        live_task = self.host.automations.get(automation_id)
        accepted_cancellation_epoch = cancellation_epoch(live_task)
        if claim_id:
            existing = self._work.jobs.get_by_idempotency(
                owner_kind="automation", owner_id=automation_id,
                kind=AUTOMATION_EXECUTION_JOB, idempotency_key=idempotency_key,
            )
            if existing is not None:
                manifest = dict(existing.input_manifest or {})
                expected = {
                    "payload": str(payload or ""),
                    "trigger_source": str(trigger_source or "trigger"),
                    "claim_id": claim_id, "claim_kind": claim_kind,
                }
                if existing.scope != WorkScope() or any(
                    manifest.get(key) != value for key, value in expected.items()
                ):
                    raise WorkConflict("automation occurrence identity conflict")
                if existing.terminal:
                    self._finalize_automation_claim(existing)
                elif not self._work.started:
                    await self._work.scheduler.run_once()
                return existing.job_id
        accepted_task = (
            self.host.automations.task_for_claim(automation_id, claim_id)
            if claim_id else None
        )
        accepted_task = self._task_with_model_route(accepted_task or execution_task_snapshot(task or {}))
        job = self._work.jobs.create(
            AUTOMATION_EXECUTION_JOB,
            owner_kind="automation",
            owner_id=automation_id,
            scope=WorkScope(),
            input_manifest={
                "task": accepted_task,
                "payload": str(payload or ""),
                "trigger_source": str(trigger_source or "trigger"),
                "claim_id": claim_id,
                "claim_kind": claim_kind,
                "cancellation_epoch": accepted_cancellation_epoch,
            },
            max_attempts=3,
            retry_policy={
                "on_lease_expiry": "retry",
                "base_delay_s": 0.5,
                "max_delay_s": 5.0,
            },
            idempotency_key=idempotency_key,
        )
        if not self._work.started:
            await self._work.scheduler.run_once()
        return job.job_id

    def cancel_automation(self, automation_id: str, *, reason: str) -> int:
        """Cancel all nonterminal Work owned by one automation definition."""

        if self._work is None:
            return 0
        cancelled = 0
        while True:
            jobs = self._work.jobs.list(
                statuses=tuple(sorted(JOB_STATES.difference(JOB_TERMINAL_STATES))),
                cancel_requested=False,
                kind=AUTOMATION_EXECUTION_JOB,
                owner_kind="automation",
                owner_id=str(automation_id or ""),
                limit=AUTOMATION_CANCEL_BATCH_SIZE,
            )
            if not jobs:
                break
            for job in jobs:
                self._work.cancel_job(
                    job.job_id,
                    reason=str(reason or "automation_stopped"),
                )
                cancelled += 1
        return cancelled

    def _task_with_model_route(self, task: dict) -> dict:
        if task.get("model_route"):
            return task
        from model_runtime.context import normalize_model_route
        router = self.host.router
        route = normalize_model_route(router, router.bound_model_route())
        return {**task, "model_route": self.host.automations.pin_model_route(str(task.get("id") or ""), route)}

    @staticmethod
    def prior_incomplete_automation_run(run_id: str):
        return automation_runner.prior_incomplete_automation_run(run_id)

    async def automation_loop(self) -> None:
        await automation_runner.automation_loop(
            self.host.automations, self.run_automation)
