"""Durable job API and worker execution context."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
import time
from collections.abc import Iterable, Mapping
from typing import Any

from .models import JOB_TERMINAL_STATES, JobRecord, WorkActor, WorkNotFound
from .repository import WorkRepository
from .scope import WorkScope, coerce_work_scope, current_work_scope


class RetryJob(RuntimeError):
    """A handler explicitly requests a durable retry."""

    def __init__(self, message: str = "retry requested", *, delay_s: float = 0.0) -> None:
        super().__init__(message)
        self.delay_s = max(0.0, float(delay_s))


class UnknownJobEffect(RuntimeError):
    """A handler cannot prove whether its external effect completed."""


@dataclass(frozen=True)
class JobResult:
    result_ref: str = ""
    diagnostics_ref: str = ""
    progress: dict[str, Any] = field(default_factory=dict)


class JobService:
    """Stable service surface used by the host, kernel bridge, and scheduler."""

    def __init__(self, repository: WorkRepository) -> None:
        self.repository = repository

    def create(
        self,
        kind: str,
        *,
        owner_kind: str = "chat",
        owner_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        priority: int = 0,
        input_manifest: Mapping[str, Any] | None = None,
        artifact_refs: Iterable[str] = (),
        retry_policy: Mapping[str, Any] | None = None,
        max_attempts: int = 1,
        idempotency_key: str = "",
        available_at: float | None = None,
        job_id: str = "",
        actor: WorkActor | None = None,
        correlation_id: str = "",
    ) -> JobRecord:
        resolved = coerce_work_scope(scope) if scope is not None else current_work_scope()
        effective_owner_id = str(owner_id or resolved.chat_id or "variant1")
        return self.repository.create_job(
            kind=kind,
            owner_kind=owner_kind,
            owner_id=effective_owner_id,
            scope=resolved,
            priority=priority,
            input_manifest=input_manifest,
            artifact_refs=artifact_refs,
            retry_policy=retry_policy,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            available_at=available_at,
            job_id=job_id,
            actor=actor,
            correlation_id=correlation_id,
        )

    def get(self, job_id: str) -> JobRecord | None:
        return self.repository.get_job(job_id)

    def get_by_idempotency(self, **identity: str) -> JobRecord | None:
        return self.repository.get_job_by_idempotency(**identity)

    def require(self, job_id: str) -> JobRecord:
        return self.repository.require_job(job_id)

    def list(self, **filters: Any) -> list[JobRecord]:
        return self.repository.list_jobs(**filters)

    def cancel(
        self,
        job_id: str,
        *,
        reason: str = "",
        expected_revision: int | None = None,
        actor: WorkActor | None = None,
    ) -> JobRecord:
        return self.repository.request_job_cancel(
            job_id,
            reason=reason,
            expected_revision=expected_revision,
            actor=actor,
        )

    def pause(
        self,
        job_id: str,
        *,
        reason: str = "",
        expected_revision: int | None = None,
        actor: WorkActor | None = None,
    ) -> JobRecord:
        return self.repository.pause_job(
            job_id,
            reason=reason,
            expected_revision=expected_revision,
            actor=actor,
        )

    def resume(
        self,
        job_id: str,
        *,
        expected_revision: int | None = None,
        available_at: float | None = None,
        actor: WorkActor | None = None,
    ) -> JobRecord:
        return self.repository.wake_job(
            job_id,
            expected_revision=expected_revision,
            available_at=available_at,
            actor=actor,
        )

    def update_progress(
        self,
        job_id: str,
        progress: Mapping[str, Any],
        **kwargs: Any,
    ) -> JobRecord:
        return self.repository.update_job_progress(job_id, progress, **kwargs)

    async def wait(
        self,
        job_id: str,
        *,
        timeout_s: float | None = None,
        poll_interval_s: float = 0.25,
        terminal_states: Iterable[str] = JOB_TERMINAL_STATES,
    ) -> JobRecord:
        """Wait without holding a database transaction or scheduler slot."""

        wanted = frozenset(str(item) for item in terminal_states)
        started = time.monotonic()
        while True:
            job = await asyncio.to_thread(self.repository.get_job, job_id)
            if job is None:
                raise WorkNotFound(f"unknown work job: {job_id}")
            if job.status in wanted:
                return job
            if timeout_s is not None:
                remaining = float(timeout_s) - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for work job {job_id}")
                delay = min(max(0.02, float(poll_interval_s)), remaining)
            else:
                delay = max(0.02, float(poll_interval_s))
            await asyncio.sleep(delay)


class JobExecutionContext:
    """Lease-fenced mutations available to one scheduler handler."""

    def __init__(
        self,
        service: JobService,
        job: JobRecord,
        *,
        lease_ttl_s: float,
    ) -> None:
        if not job.lease_owner or not job.lease_epoch:
            raise ValueError("job execution context requires a lease")
        self.service = service
        self.job = job
        self.lease_owner = job.lease_owner
        self.lease_epoch = job.lease_epoch
        self.lease_ttl_s = max(2.0, float(lease_ttl_s))
        self._local_cancellation = threading.Event()

    def refresh(self) -> JobRecord:
        self.job = self.service.require(self.job.job_id)
        return self.job

    def heartbeat(self, progress: Mapping[str, Any] | None = None) -> JobRecord:
        self.job = self.service.repository.heartbeat_job(
            self.job.job_id,
            lease_owner=self.lease_owner,
            lease_epoch=self.lease_epoch,
            lease_ttl_s=self.lease_ttl_s,
            progress=progress,
        )
        return self.job

    def progress(self, value: Mapping[str, Any], *, merge: bool = True) -> JobRecord:
        self.job = self.service.repository.update_job_progress(
            self.job.job_id,
            value,
            lease_owner=self.lease_owner,
            lease_epoch=self.lease_epoch,
            merge=merge,
            actor=WorkActor("worker", self.lease_owner),
        )
        return self.job

    def cancellation_requested(self) -> bool:
        if self._local_cancellation.is_set():
            return True
        current = self.refresh()
        expires = float(current.lease_expires_at or 0)
        valid = (current.lease_owner == self.lease_owner
                 and current.lease_epoch == self.lease_epoch
                 and current.status in {"leased", "running"}
                 and expires > time.time())
        if not valid:
            self.request_local_cancellation()
        return not valid or current.cancel_requested

    def request_local_cancellation(self) -> None:
        self._local_cancellation.set()

    def commit_result(self, publish) -> JobResult:
        """Commit same-database effects and success against cancel/lease admission.

        The database writer owns the race: a cancellation committed first aborts
        publication; a successful commit first is already terminal when cancel arrives.
        ``publish(connection)`` must not perform network or long-running rendering.
        """
        repository = self.service.repository
        with repository._write() as connection:
            row = repository._job_row_tx(connection, self.job.job_id)
            repository._expect_lease(row, lease_owner=self.lease_owner,
                                     lease_epoch=self.lease_epoch, now=time.time())
            if self._local_cancellation.is_set() or row["cancel_requested"]:
                raise asyncio.CancelledError("job cancelled before publication commit")
            result = publish(connection)
            if self._local_cancellation.is_set():
                raise asyncio.CancelledError("job cancelled during publication commit")
            self.job = repository.finish_job(self.job.job_id, status="succeeded",
                lease_owner=self.lease_owner, lease_epoch=self.lease_epoch,
                result_ref=result.result_ref, diagnostics_ref=result.diagnostics_ref,
                progress=result.progress, _connection=connection)
        return result

    def mark_waiting(self, progress: Mapping[str, Any] | None = None) -> JobRecord:
        self.job = self.service.repository.mark_job_waiting(
            self.job.job_id,
            lease_owner=self.lease_owner,
            lease_epoch=self.lease_epoch,
            progress=progress,
        )
        return self.job


__all__ = [
    "JobExecutionContext",
    "JobResult",
    "JobService",
    "RetryJob",
    "UnknownJobEffect",
]
