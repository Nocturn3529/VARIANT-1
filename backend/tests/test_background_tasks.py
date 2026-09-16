import asyncio

import pytest

import background_tasks


@pytest.mark.asyncio
async def test_spawn_retains_task_until_completion():
    gate = asyncio.Event()

    async def wait_for_gate():
        await gate.wait()
        return 42

    before = background_tasks.active_count()
    task = background_tasks.spawn(wait_for_gate(), name="retention-test")
    await asyncio.sleep(0)
    assert background_tasks.active_count() == before + 1

    gate.set()
    assert await task == 42
    await asyncio.sleep(0)
    assert background_tasks.active_count() == before


@pytest.mark.asyncio
async def test_spawn_reports_unhandled_exception():
    reports = []

    async def fail():
        raise RuntimeError("boom")

    task = background_tasks.spawn(
        fail(), name="failure-test",
        reporter=lambda label, exc: reports.append((label, str(exc))),
    )
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert reports == [("failure-test", "boom")]


@pytest.mark.asyncio
async def test_spawn_does_not_report_cancellation():
    reports = []

    async def wait_forever():
        await asyncio.Event().wait()

    task = background_tasks.spawn(
        wait_forever(), name="cancel-test",
        reporter=lambda label, exc: reports.append((label, str(exc))),
    )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert reports == []


@pytest.mark.asyncio
async def test_cancel_all_settles_registered_tasks():
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    task = background_tasks.spawn(wait_forever(), name="shutdown-test")
    await started.wait()
    await background_tasks.cancel_all()
    await asyncio.sleep(0)

    assert task.cancelled()
    assert background_tasks.active_count() == 0
