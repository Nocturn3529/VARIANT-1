"""Shared WorkScheduler admission must serialize every run_once caller."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from work_fabric.jobs import JobResult
from work_fabric.service import WorkService


def _service(tmp_path) -> WorkService:
    return WorkService.open(str(tmp_path / "work.sqlite3"), worker_id="test-worker")


@pytest.mark.asyncio
async def test_concurrent_run_once_does_not_oversubscribe_global_limit(tmp_path):
    service = _service(tmp_path)
    service.scheduler.max_concurrency = 1
    original = service.jobs.repository.lease_next_job
    inflight = 0
    max_inflight = 0
    guard = threading.Lock()
    leased_ids: list[str] = []

    def gated_lease(*args, **kwargs):
        nonlocal inflight, max_inflight
        with guard:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        time.sleep(0.05)
        try:
            leased = original(*args, **kwargs)
        finally:
            with guard:
                inflight -= 1
        if leased is not None:
            leased_ids.append(leased.job_id)
        return leased

    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(context):
        entered.set()
        await release.wait()
        return JobResult()

    service.register_job_handler("test.limited", handler)
    first = service.jobs.create("test.limited")
    service.jobs.create("test.limited")
    service.jobs.repository.lease_next_job = gated_lease
    try:
        started = await asyncio.wait_for(
            asyncio.gather(service.scheduler.run_once(), service.scheduler.run_once()),
            timeout=5,
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert started == [1, 0] or started == [0, 1]
        assert max_inflight == 1
        assert leased_ids == [first.job_id]
        assert service.scheduler.active_count == 1
    finally:
        release.set()
        await service.scheduler.shutdown()


@pytest.mark.asyncio
async def test_followup_admits_queued_work_after_slot_frees(tmp_path):
    service = _service(tmp_path)
    service.scheduler.max_concurrency = 1
    started: list[str] = []
    first_entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(context):
        started.append(context.job.job_id)
        if len(started) == 1:
            first_entered.set()
            await release.wait()
        return JobResult()

    service.register_job_handler("test.follow", handler)
    first = service.jobs.create("test.follow")
    second = service.jobs.create("test.follow")
    try:
        assert await service.scheduler.run_once() == 1
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        assert await service.scheduler.run_once() == 0
        assert started == [first.job_id]
        release.set()
        for _ in range(400):
            if second.job_id in started:
                break
            await asyncio.sleep(0.01)
        assert started == [first.job_id, second.job_id]
        assert (await service.jobs.wait(second.job_id, timeout_s=2)).status == "succeeded"
    finally:
        release.set()
        await service.scheduler.shutdown()


@pytest.mark.asyncio
async def test_per_kind_limit_is_respected_by_concurrent_unstarted_pumps(tmp_path):
    service = _service(tmp_path)
    service.scheduler.max_concurrency = 4
    limited_started: list[str] = []
    limited_entered = asyncio.Event()
    unrelated_entered = asyncio.Event()
    release = asyncio.Event()

    async def limited(context):
        limited_started.append(context.job.job_id)
        limited_entered.set()
        await release.wait()
        return JobResult()

    async def unrelated(_context):
        unrelated_entered.set()
        return JobResult()

    service.register_job_handler("test.limited", limited, max_concurrency=1)
    service.register_job_handler("test.unrelated", unrelated)
    first = service.jobs.create("test.limited")
    second = service.jobs.create("test.limited")
    other = service.jobs.create("test.unrelated")
    try:
        await asyncio.wait_for(
            asyncio.gather(
                service.scheduler.run_once(),
                service.scheduler.run_once(),
                service.scheduler.run_once(),
            ),
            timeout=5,
        )
        await asyncio.wait_for(limited_entered.wait(), timeout=2)
        await asyncio.wait_for(unrelated_entered.wait(), timeout=2)
        assert limited_started == [first.job_id]
        assert service.jobs.require(second.job_id).status == "queued"
        assert (await service.jobs.wait(other.job_id, timeout_s=2)).status == "succeeded"
        release.set()
        assert (await service.jobs.wait(first.job_id, timeout_s=2)).status == "succeeded"
        assert (await service.jobs.wait(second.job_id, timeout_s=2)).status == "succeeded"
        assert limited_started == [first.job_id, second.job_id]
    finally:
        release.set()
        await service.scheduler.shutdown()


@pytest.mark.asyncio
async def test_started_scheduler_does_not_need_followup_task(tmp_path):
    service = _service(tmp_path)
    entered = asyncio.Event()

    async def handler(_context):
        entered.set()
        return JobResult()

    service.register_job_handler("test.live", handler)
    job = service.jobs.create("test.live")
    await service.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert (await service.jobs.wait(job.job_id, timeout_s=2)).status == "succeeded"
        assert service.scheduler.running is True
        followup = service.scheduler._followup_task
        assert followup is None or followup.done()
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_followup_and_active_jobs(tmp_path):
    service = _service(tmp_path)
    service.scheduler.max_concurrency = 1
    entered = asyncio.Event()

    async def handler(_context):
        entered.set()
        await asyncio.sleep(30)
        return JobResult()

    service.register_job_handler("test.block", handler)
    service.jobs.create("test.block")
    service.jobs.create("test.block")
    try:
        assert await service.scheduler.run_once() == 1
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.wait_for(service.scheduler.shutdown(), timeout=5)
        assert service.scheduler.active_count == 0
        assert service.scheduler._followup_task is None
        assert service.scheduler.running is False
    finally:
        await service.scheduler.shutdown()
