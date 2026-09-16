"""Conservative startup and periodic Work Fabric recovery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time

from .repository import WorkRepository


@dataclass(frozen=True)
class RecoveryReport:
    recovered_jobs: int = 0
    unknown_effect_jobs: int = 0
    recovered_outbox_items: int = 0
    recovered_operations: int = 0
    ran_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "recovered_jobs": int(self.recovered_jobs),
            "unknown_effect_jobs": int(self.unknown_effect_jobs),
            "recovered_outbox_items": int(self.recovered_outbox_items),
            "recovered_operations": int(self.recovered_operations),
            "ran_at": float(self.ran_at),
        }


class WorkRecovery:
    """Reconcile expired leases without assuming an effect did not happen."""

    def __init__(
        self,
        repository: WorkRepository,
        *,
        stale_operation_after_s: float = 24 * 60 * 60,
    ) -> None:
        self.repository = repository
        self.stale_operation_after_s = max(60.0, float(stale_operation_after_s))
        self.last_report = RecoveryReport()

    def run_once(self, *, now: float | None = None) -> RecoveryReport:
        at = float(now if now is not None else time.time())
        jobs = self.repository.recover_expired_job_leases(now=at)
        outbox = self.repository.recover_expired_outbox(now=at)
        operations = self.repository.recover_stale_operations(
            updated_before=at - self.stale_operation_after_s
        )
        report = RecoveryReport(
            recovered_jobs=len(jobs),
            unknown_effect_jobs=sum(
                1 for job in jobs if job.status == "unknown_effect"
            ),
            recovered_outbox_items=outbox,
            recovered_operations=len(operations),
            ran_at=at,
        )
        self.last_report = report
        return report

    async def run_once_async(self, *, now: float | None = None) -> RecoveryReport:
        return await asyncio.to_thread(self.run_once, now=now)


__all__ = ["RecoveryReport", "WorkRecovery"]
