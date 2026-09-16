"""Composition root and integration-facing API for the Work Fabric."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import os
from typing import Any

from .events import WorkEventService
from .jobs import JobService
from .interactions import InteractionService
from .models import JobRecord, OperationRecord, WorkActor
from .recovery import WorkRecovery
from .repository import WorkRepository
from .scheduler import (
    ConcurrencyLimit,
    JobHandler,
    WorkScheduler,
    default_worker_id,
)
from .scope import WorkScope, current_work_scope


_LOG = logging.getLogger(__name__)


class WorkService:
    """Host-owned Work Fabric facade.

    The facade performs no tool registration itself.  Host composition may
    mount the JSON-safe convenience methods into the kernel catalog and may
    register ``record_capability_receipt`` as a broker receipt sink.
    """

    def __init__(
        self,
        repository: WorkRepository,
        *,
        worker_id: str = "",
        handlers: Mapping[str, JobHandler] | None = None,
        recovery_interval_s: float = 5.0,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id or default_worker_id()
        self.events = WorkEventService(
            repository, consumer_id=f"{self.worker_id}:events"
        )
        self.jobs = JobService(repository)
        self.interactions = InteractionService(repository)
        self.recovery = WorkRecovery(repository)
        self.scheduler = WorkScheduler(
            self.jobs,
            worker_id=self.worker_id,
            handlers=handlers,
        )
        self._started = False
        self._lifecycle_lock = asyncio.Lock()
        self._recovery_interval_s = max(0.05, float(recovery_interval_s))
        self._recovery_stop = asyncio.Event()
        self._recovery_task: asyncio.Task | None = None

    @classmethod
    def open(
        cls,
        path: str | None = None,
        *,
        data_dir: str | None = None,
        worker_id: str | None = None,
        handlers: Mapping[str, JobHandler] | None = None,
        recovery_interval_s: float = 5.0,
    ) -> "WorkService":
        """Open a service.

        ``data_dir`` means the existing VARIANT-1 ``data`` directory; the
        repository appends ``work/work.sqlite3``.  Host composition should use
        :func:`build_work_runtime`, which converts ``AppHost.data_dir`` (the
        application root) to ``<host.data_dir>/data``.
        """

        repository = WorkRepository(path, data_dir=data_dir)
        return cls(
            repository,
            worker_id=str(worker_id or ""),
            handlers=handlers,
            recovery_interval_s=recovery_interval_s,
        )

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Recover durable state and start pumps; safe to call repeatedly."""

        async with self._lifecycle_lock:
            if self._started:
                return
            await self.recovery.run_once_async()
            await self.events.start()
            await self.scheduler.start()
            self._recovery_stop = asyncio.Event()
            self._recovery_task = asyncio.create_task(
                self._run_periodic_recovery(), name="variant1-work-recovery"
            )
            self._started = True

    async def _run_periodic_recovery(self) -> None:
        """Recover leases that expire after the one-time startup scan."""

        while not self._recovery_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._recovery_stop.wait(), timeout=self._recovery_interval_s
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.recovery.run_once_async()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("periodic Work Fabric recovery failed")

    async def _stop_periodic_recovery(self) -> None:
        task, self._recovery_task = self._recovery_task, None
        self._recovery_stop.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        """Stop dispatch before the database owner exits; idempotent."""

        async with self._lifecycle_lock:
            if not self._started:
                # Components are independently idempotent and may have been
                # started directly by a host integration.
                await self._stop_periodic_recovery()
                await self.scheduler.shutdown()
                await self.events.shutdown()
                return
            self._started = False
            await self._stop_periodic_recovery()
            await self.scheduler.shutdown()
            await self.events.shutdown()

    def register_job_handler(
        self,
        kind: str,
        handler: JobHandler,
        *,
        max_concurrency: ConcurrencyLimit | None = None,
    ) -> None:
        self.scheduler.register(
            kind,
            handler,
            max_concurrency=max_concurrency,
        )

    def current_scope(self) -> WorkScope:
        return current_work_scope()

    def scope_current(self) -> dict[str, Any]:
        return self.current_scope().to_dict()

    def state(self) -> dict[str, Any]:
        return {
            "schema": "variant1.work-runtime.v1",
            "database": self.repository.path,
            "started": bool(self._started),
            "worker_id": self.worker_id,
            "scheduler": {
                "running": self.scheduler.running,
                "active_jobs": self.scheduler.active_count,
                "registered_kinds": sorted(self.scheduler._handlers),
            },
            "periodic_recovery_running": bool(
                self._recovery_task is not None
                and not self._recovery_task.done()
            ),
            "last_recovery": self.recovery.last_report.to_dict(),
            "scope": self.scope_current(),
        }

    def cancel_job(self, job_id: str, **kwargs: Any) -> JobRecord:
        """Durably request cancellation and interrupt the local handler."""

        updated = self.jobs.cancel(job_id, **kwargs)
        if updated.cancel_requested and not updated.terminal:
            self.scheduler.cancel_active(updated.job_id)
        return updated

    async def delete_chat(self, chat_id: str) -> int:
        """Cancel every nonterminal Work owner scoped to a deleted chat."""

        owner_chat = str(chat_id or "").strip()
        if not owner_chat:
            return 0
        cancelled = 0
        while True:
            # Running jobs remain nonterminal until their handler acknowledges
            # cancellation. Filtering out already-requested jobs makes each
            # batch advance instead of repeatedly fetching the same 1000 rows.
            jobs = self.jobs.list(
                chat_id=owner_chat, statuses=("queued", "leased", "running", "waiting", "paused"),
                cancel_requested=False, limit=1000,
            )
            if not jobs:
                break
            for job in jobs:
                self.cancel_job(job.job_id, reason="chat_deleted")
                cancelled += 1
        return cancelled

    def record_capability_receipt(self, receipt: Any) -> None:
        """Broker sink for one terminal receipt (return value intentionally void)."""

        self.ingest_capability_receipt(receipt)

    def ingest_capability_receipt(self, receipt: Any) -> OperationRecord:
        """Persist and return one idempotent operation receipt."""

        if isinstance(receipt, Mapping):
            raw = dict(receipt)
        else:
            to_dict = getattr(receipt, "to_dict", None)
            if not callable(to_dict):
                raise TypeError("capability receipt must be a mapping or expose to_dict()")
            value = to_dict()
            if not isinstance(value, Mapping):
                raise TypeError("capability receipt to_dict() must return a mapping")
            raw = dict(value)
        receipt_id = str(raw.get("receipt_id") or "").strip()
        if not receipt_id:
            raise ValueError("capability receipt has no receipt_id")
        capability = dict(raw.get("capability") or {})
        attribution = dict(raw.get("attribution") or {})
        error_value = raw.get("error")
        error = dict(error_value) if isinstance(error_value, Mapping) else {}
        effect_value = raw.get("effect")
        effect = dict(effect_value) if isinstance(effect_value, Mapping) else {}
        result_metadata = dict(raw.get("result_metadata") or {})
        nested_scope = (
            raw.get("work_scope")
            or attribution.get("work_scope")
            or result_metadata.get("work_scope")
        )
        scope = WorkScope.from_mapping(
            nested_scope if isinstance(nested_scope, Mapping) else attribution
        )
        receipt_status = str(raw.get("status") or "error")
        may_have_applied = bool(error.get("may_have_applied"))
        if receipt_status == "ok":
            operation_status = "succeeded"
        elif receipt_status in {"cancelled", "cancelled_before_start"}:
            operation_status = "cancelled"
        elif receipt_status == "needs_reconciliation" or may_have_applied:
            operation_status = "unknown_effect"
        else:
            operation_status = "failed"
        capability_id = str(
            capability.get("capability_id")
            or capability.get("ref_id")
            or "capability"
        )
        effect_ref = str(
            result_metadata.get("effect_ref")
            or result_metadata.get("operation_ref")
            or ""
        )
        actor_id = str(attribution.get("principal_actor_id") or "model")
        correlation_id = str(
            attribution.get("nested_call_id")
            or attribution.get("outer_tool_call_id")
            or ""
        )
        request = {
            "capability": capability,
            "arguments_sha256": str(raw.get("arguments_sha256") or ""),
            "attribution": attribution,
        }
        return self.repository.record_operation_receipt(
            operation_id=receipt_id,
            kind=capability_id,
            status=operation_status,
            scope=scope,
            request=request,
            response=raw,
            effect_ref=effect_ref,
            error=str(error.get("message") or ""),
            idempotency_key=str(effect.get("idempotency_key") or receipt_id),
            actor=WorkActor("agent", actor_id),
            correlation_id=correlation_id,
        )


def build_work_runtime(
    host: Any = None,
    *,
    path: str | None = None,
    data_dir: str | None = None,
    worker_id: str | None = None,
    handlers: Mapping[str, JobHandler] | None = None,
    recovery_interval_s: float = 5.0,
) -> WorkService:
    """Build, but do not start, the host Work Fabric runtime.

    ``AppHost.data_dir`` is the writable application root, so the default host
    location is ``<host.data_dir>/data/work/work.sqlite3``.
    """

    if path and data_dir:
        raise ValueError("pass either path or data_dir, not both")
    resolved_data_dir = data_dir
    if path is None and resolved_data_dir is None and host is not None:
        app_root = str(getattr(host, "data_dir", "") or "").strip()
        if app_root:
            resolved_data_dir = os.path.join(os.path.abspath(app_root), "data")
    return WorkService.open(
        path,
        data_dir=resolved_data_dir,
        worker_id=worker_id,
        handlers=handlers,
        recovery_interval_s=recovery_interval_s,
    )


__all__ = ["WorkService", "build_work_runtime"]
