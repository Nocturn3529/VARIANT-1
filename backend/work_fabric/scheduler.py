"""Lease-based async scheduler for Work Fabric jobs."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from .jobs import (
    JobExecutionContext,
    JobResult,
    JobService,
    RetryJob,
    UnknownJobEffect,
)
from .models import JOB_TERMINAL_STATES, LeaseLost


_LOG = logging.getLogger(__name__)
JobHandler = Callable[[JobExecutionContext], Any]
ConcurrencyLimit = int | Callable[[], int]


def default_worker_id() -> str:
    return f"work:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


class WorkScheduler:
    """Poll, lease, and execute registered durable job kinds."""

    def __init__(
        self,
        jobs: JobService,
        *,
        worker_id: str = "",
        handlers: Mapping[str, JobHandler] | None = None,
        max_concurrency: int = 16,
        poll_interval_s: float = 0.25,
        lease_ttl_s: float = 60.0,
    ) -> None:
        self.jobs = jobs
        self.worker_id = worker_id or default_worker_id()
        self.max_concurrency = max(1, int(max_concurrency))
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.lease_ttl_s = max(6.0, float(lease_ttl_s))
        self._handlers: dict[str, JobHandler] = {}
        self._handler_limits: dict[str, ConcurrencyLimit] = {}
        for kind, handler in dict(handlers or {}).items():
            self.register(kind, handler)
        self._loop_task: asyncio.Task | None = None
        self._active: dict[str, asyncio.Task] = {}
        self._active_kinds: dict[str, str] = {}
        self._stopping = asyncio.Event()
        self._admission = asyncio.Lock()
        self._followup_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def active_count(self) -> int:
        return len(self._active)

    def register(
        self,
        kind: str,
        handler: JobHandler,
        *,
        max_concurrency: ConcurrencyLimit | None = None,
    ) -> None:
        clean_kind = str(kind or "").strip()
        if not clean_kind:
            raise ValueError("job handler kind is required")
        if not callable(handler):
            raise TypeError("job handler must be callable")
        self._handlers[clean_kind] = handler
        if max_concurrency is None:
            self._handler_limits.pop(clean_kind, None)
        else:
            self._handler_limits[clean_kind] = max_concurrency

    def unregister(self, kind: str) -> None:
        clean_kind = str(kind)
        self._handlers.pop(clean_kind, None)
        self._handler_limits.pop(clean_kind, None)

    def _eligible_kinds(self) -> tuple[str, ...]:
        active = Counter(self._active_kinds.values())
        eligible = []
        for kind in self._handlers:
            raw_limit = self._handler_limits.get(kind)
            if raw_limit is None:
                eligible.append(kind)
                continue
            try:
                value = raw_limit() if callable(raw_limit) else raw_limit
                limit = max(1, min(self.max_concurrency, int(value)))
            except Exception:
                # A broken host capacity resolver must reduce admission rather
                # than silently widening it.
                limit = 1
            if int(active.get(kind) or 0) < limit:
                eligible.append(kind)
        return tuple(eligible)

    async def _invoke_handler(
        self,
        handler: JobHandler,
        context: JobExecutionContext,
    ) -> Any:
        if inspect.iscoroutinefunction(handler):
            return await handler(context)
        worker = asyncio.create_task(asyncio.to_thread(handler, context))
        cancellation = None
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError as exc:
                context.request_local_cancellation()
                cancellation = cancellation or exc
                continue
        try:
            result = worker.result()
        except BaseException as handler_error:
            if cancellation is not None:
                raise cancellation from handler_error
            raise
        if cancellation is not None:
            raise cancellation
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _result(value: Any) -> JobResult:
        if value is None:
            return JobResult()
        if isinstance(value, JobResult):
            return value
        if isinstance(value, str):
            return JobResult(result_ref=value)
        if isinstance(value, Mapping):
            return JobResult(
                result_ref=str(value.get("result_ref") or ""),
                diagnostics_ref=str(value.get("diagnostics_ref") or ""),
                progress=dict(value.get("progress") or {}),
            )
        raise TypeError(
            "job handler must return None, str, mapping, or JobResult"
        )

    async def _heartbeat_loop(
        self,
        context: JobExecutionContext,
        stopped: asyncio.Event,
        handler_task: asyncio.Task | None = None,
    ) -> None:
        interval = max(2.0, self.lease_ttl_s / 3.0)
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(context.heartbeat)
            except LeaseLost:
                context.request_local_cancellation()
                if handler_task is not None and not handler_task.done():
                    handler_task.cancel()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("job heartbeat failed: %s", context.job.job_id)

    def _retry_delay(self, context: JobExecutionContext) -> float:
        policy = context.job.retry_policy
        base = max(0.0, float(policy.get("base_delay_s") or 0.5))
        cap = max(base, float(policy.get("max_delay_s") or 60.0))
        return min(cap, base * (2 ** max(0, context.job.attempt - 1)))

    async def _execute(self, leased) -> None:
        context = JobExecutionContext(
            self.jobs, leased, lease_ttl_s=self.lease_ttl_s
        )
        heartbeat_stop = asyncio.Event()
        heartbeat: asyncio.Task | None = None
        handler_task: asyncio.Task | None = None
        try:
            handler = self._handlers.get(leased.kind)
            started = await asyncio.to_thread(
                self.jobs.repository.start_job,
                leased.job_id,
                lease_owner=leased.lease_owner,
                lease_epoch=leased.lease_epoch,
                sync_handler=(handler is not None and not inspect.iscoroutinefunction(handler)),
            )
            context.job = started
            if handler is None:
                raise RetryJob(f"no handler registered for {started.kind}", delay_s=5.0)
            handler_task = asyncio.create_task(
                self._invoke_handler(handler, context), name=f"work-handler:{leased.job_id}",
            )
            heartbeat = asyncio.create_task(
                self._heartbeat_loop(context, heartbeat_stop, handler_task),
                name=f"work-heartbeat:{leased.job_id}",
            )
            raw_result = await handler_task
            current = await asyncio.to_thread(self.jobs.require, started.job_id)
            context.job = current
            if current.status == "waiting" or current.status in JOB_TERMINAL_STATES:
                return
            if current.cancel_requested:
                await asyncio.to_thread(
                    self.jobs.repository.acknowledge_job_cancel,
                    current.job_id,
                    lease_owner=context.lease_owner,
                    lease_epoch=context.lease_epoch,
                )
                return
            result = self._result(raw_result)
            await asyncio.to_thread(
                self.jobs.repository.finish_job,
                current.job_id,
                status="succeeded",
                lease_owner=context.lease_owner,
                lease_epoch=context.lease_epoch,
                result_ref=result.result_ref,
                diagnostics_ref=result.diagnostics_ref,
                progress=result.progress or None,
            )
        except RetryJob as exc:
            try:
                await asyncio.to_thread(
                    self.jobs.repository.release_job,
                    leased.job_id,
                    lease_owner=context.lease_owner,
                    lease_epoch=context.lease_epoch,
                    error=str(exc),
                    retry_delay_s=exc.delay_s or self._retry_delay(context),
                )
            except LeaseLost:
                pass
        except UnknownJobEffect as exc:
            try:
                await asyncio.to_thread(
                    self.jobs.repository.release_job,
                    leased.job_id,
                    lease_owner=context.lease_owner,
                    lease_epoch=context.lease_epoch,
                    error=str(exc),
                    unknown_effect=True,
                )
            except LeaseLost:
                pass
        except asyncio.CancelledError:
            # Explicit job cancellation is distinct from scheduler shutdown.
            # The former has a durable request and can be acknowledged after
            # the handler's cancellation cleanup returns; the latter leaves
            # the lease for conservative startup recovery.
            try:
                current = await asyncio.to_thread(self.jobs.require, leased.job_id)
            except Exception:
                current = None
            if current is not None and current.cancel_requested:
                try:
                    await asyncio.to_thread(
                        self.jobs.repository.acknowledge_job_cancel,
                        current.job_id,
                        lease_owner=context.lease_owner,
                        lease_epoch=context.lease_epoch,
                    )
                except LeaseLost:
                    pass
                return
            raise
        except Exception as exc:
            _LOG.exception("Work Fabric job handler failed: %s", leased.job_id)
            try:
                await asyncio.to_thread(
                    self.jobs.repository.release_job,
                    leased.job_id,
                    lease_owner=context.lease_owner,
                    lease_epoch=context.lease_epoch,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_delay_s=self._retry_delay(context),
                )
            except LeaseLost:
                pass
        finally:
            if handler_task is not None and not handler_task.done():
                context.request_local_cancellation()
                handler_task.cancel()
                await asyncio.gather(handler_task, return_exceptions=True)
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    def _track(self, job_id: str, kind: str, task: asyncio.Task) -> None:
        self._active[job_id] = task
        self._active_kinds[job_id] = str(kind)

        def done(finished: asyncio.Task) -> None:
            if self._active.get(job_id) is finished:
                self._active.pop(job_id, None)
                self._active_kinds.pop(job_id, None)
            if finished.cancelled():
                self._schedule_followup()
                return
            try:
                finished.result()
            except Exception:
                _LOG.exception("uncaught Work Fabric scheduler task failure")
            self._schedule_followup()

        task.add_done_callback(done)

    def _schedule_followup(self) -> None:
        if self._stopping.is_set() or self.running:
            return
        existing = self._followup_task
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._followup_once(), name="work-scheduler-followup"
        )
        self._followup_task = task

        def done(finished: asyncio.Task, *, expected=task) -> None:
            if self._followup_task is expected:
                self._followup_task = None
            if finished.cancelled():
                return
            try:
                finished.result()
            except Exception:
                _LOG.exception("uncaught Work Fabric scheduler follow-up failure")

        task.add_done_callback(done)

    async def _followup_once(self) -> None:
        try:
            while not self.running and not self._stopping.is_set():
                count = await self.run_once()
                if count <= 0:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Work Fabric scheduler follow-up failed")

    def cancel_active(self, job_id: str) -> bool:
        """Interrupt the locally owned handler after durable cancel admission."""

        task = self._active.get(str(job_id or ""))
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def run_once(self) -> int:
        async with self._admission:
            return await self._admit_locked()

    async def _admit_locked(self) -> int:
        available = self.max_concurrency - len(self._active)
        if available <= 0 or not self._handlers:
            return 0
        started = 0
        for _ in range(available):
            eligible_kinds = self._eligible_kinds()
            if not eligible_kinds:
                break
            leased = await asyncio.to_thread(
                self.jobs.repository.lease_next_job,
                self.worker_id,
                kinds=eligible_kinds,
                # Lease recovery may requeue a job while its prior local
                # handler is still unwinding. Keep it tracked until cleanup
                # settles; unrelated jobs can still claim the available slots.
                exclude_job_ids=tuple(self._active),
                lease_ttl_s=self.lease_ttl_s,
            )
            if leased is None:
                break
            task = asyncio.create_task(
                self._execute(leased), name=f"work-job:{leased.job_id}"
            )
            self._track(leased.job_id, leased.kind, task)
            started += 1
        return started

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                count = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("Work Fabric scheduler poll failed")
                count = 0
            if count:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.poll_interval_s
                )
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if self.running:
            return
        self._stopping = asyncio.Event()
        self._loop_task = asyncio.create_task(
            self._run(), name="variant1-work-scheduler"
        )

    async def shutdown(self) -> None:
        loop_task = self._loop_task
        self._loop_task = None
        self._stopping.set()
        followup = self._followup_task
        self._followup_task = None
        if loop_task is not None:
            loop_task.cancel()
        if followup is not None:
            followup.cancel()
        active = tuple(self._active.values())
        for task in active:
            task.cancel()
        if loop_task is not None:
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        if followup is not None:
            try:
                await followup
            except asyncio.CancelledError:
                pass
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self._active.clear()
        self._active_kinds.clear()


__all__ = [
    "ConcurrencyLimit",
    "JobHandler",
    "WorkScheduler",
    "default_worker_id",
]
