"""A recovered lease must not overlap its still-running local handler."""
import asyncio

import pytest

from work_fabric.service import WorkService


@pytest.mark.asyncio
async def test_recovered_job_waits_for_local_cleanup_without_blocking_other_jobs(tmp_path):
    service = WorkService.open(str(tmp_path / "work.sqlite3"))
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    retried = asyncio.Event()
    other_finished = asyncio.Event()
    invocations = []

    async def handler(context):
        invocations.append(context.lease_epoch)
        if len(invocations) > 1:
            retried.set()
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()

    async def other_handler(_context):
        other_finished.set()

    service.register_job_handler("test.recovered", handler)
    service.register_job_handler("test.other", other_handler)
    job = service.jobs.create(
        "test.recovered", owner_kind="system", owner_id="test", max_attempts=3,
        retry_policy={"on_lease_expiry": "retry", "base_delay_s": 0},
    )
    original = None
    try:
        await service.scheduler.run_once()
        await asyncio.wait_for(entered.wait(), 3)
        original = service.scheduler._active[job.job_id]
        # Expired durable ownership does not prove local execution has stopped.
        with service.repository._write() as conn:
            conn.execute("UPDATE work_job SET lease_expires_at=0 WHERE job_id=?", (job.job_id,))
        service.recovery.run_once()
        assert service.jobs.require(job.job_id).status == "queued"
        service.jobs.create("test.other", owner_kind="system", owner_id="test")

        assert await service.scheduler.run_once() == 1
        await asyncio.wait_for(other_finished.wait(), 3)
        assert invocations == [1]
        assert service.scheduler._active[job.job_id] is original

        # Model the heartbeat's cancellation once it observes the lost lease.
        service.scheduler.cancel_active(job.job_id)
        await asyncio.wait_for(cleaning.wait(), 3)
        await service.scheduler.run_once()
        assert invocations == [1]
        assert not original.done()

        release_cleanup.set()
        await asyncio.gather(original, return_exceptions=True)
        await asyncio.sleep(0)  # Deliver the scheduler's completion callback.
        await service.scheduler.run_once()
        await asyncio.wait_for(retried.wait(), 3)
        assert invocations == [1, 2]
    finally:
        release_cleanup.set()
        if original is not None and not original.done():
            original.cancel()
        await service.scheduler.shutdown()
        if original is not None:
            await asyncio.gather(original, return_exceptions=True)
