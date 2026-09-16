"""UIA worker hang watchdog — restart pool on timeout."""

from __future__ import annotations

import asyncio
import threading

import pytest

import tools
from desktop import uia_worker


def test_watchdog_timeout_restarts_executor(monkeypatch):
    uia_worker._UIA_EXEC = None
    uia_worker._COM_STATUS = ""
    uia_worker._RESTARTS = 0
    uia_worker._UIA_GENERATION = 0
    uia_worker._ABANDONED_WORK.clear()
    release = threading.Event()

    def hang():
        release.wait(10)

    # Force a tiny timeout without real COM init path.
    monkeypatch.setattr(uia_worker, "com_begin", lambda: None)
    monkeypatch.setattr(uia_worker, "_COM_STATUS", "")
    async def _run():
        try:
            with pytest.raises(tools.ToolError, match="timed out"):
                await uia_worker.run(hang, timeout_s=0.2)
            assert uia_worker._RESTARTS >= 1
            assert uia_worker._UIA_GENERATION >= 1
            with pytest.raises(tools.ToolError, match="previous timed-out"):
                await uia_worker.run(lambda: "must not run")

            release.set()
            for _ in range(100):
                if not uia_worker._hung_work_pending():
                    break
                await asyncio.sleep(0.01)
            assert await uia_worker.run(lambda: "recovered") == "recovered"
        finally:
            release.set()

    asyncio.run(_run())


def test_abandoned_uia_owner_is_daemon_and_shutdown_never_joins_hung_call():
    release = threading.Event()
    entered = threading.Event()
    pool = uia_worker._DaemonSingleThreadExecutor(name="test-uia-daemon")

    def hang():
        entered.set()
        release.wait(5)

    pool.submit(hang)
    assert entered.wait(1)
    started = __import__("time").monotonic()
    pool.shutdown(wait=False, cancel_futures=True)
    elapsed = __import__("time").monotonic() - started
    try:
        assert pool.thread.daemon is True
        assert pool.thread.is_alive() is True
        assert elapsed < 0.2
    finally:
        release.set()
        pool.thread.join(timeout=1)


@pytest.mark.asyncio
async def test_cancellation_releases_lock_and_quarantines_unfinished_com(
    monkeypatch,
):
    uia_worker._UIA_EXEC = None
    uia_worker._COM_STATUS = ""
    uia_worker._RESTARTS = 0
    uia_worker._UIA_GENERATION = 0
    uia_worker._ABANDONED_WORK.clear()
    monkeypatch.setattr(uia_worker, "com_begin", lambda: None)
    release_com = threading.Event()
    com_started = threading.Event()
    physical_lock = asyncio.Lock()
    contender_entered = asyncio.Event()

    def blocking_com_call():
        com_started.set()
        release_com.wait(5)
        return "finished"

    async def first_owner():
        async with physical_lock:
            return await uia_worker.run(blocking_com_call, timeout_s=5)

    async def contender():
        async with physical_lock:
            contender_entered.set()
            with pytest.raises(tools.ToolError, match="blocked"):
                await uia_worker.run(lambda: pytest.fail("COM is still active"))

    first = asyncio.create_task(first_owner())
    try:
        for _ in range(200):
            if com_started.is_set():
                break
            await asyncio.sleep(0.005)
        assert com_started.is_set()
        first.cancel()
        second = asyncio.create_task(contender())
        await asyncio.sleep(0.05)
        assert first.done() is True
        assert contender_entered.is_set() is True
        assert physical_lock.locked() is False

        release_com.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(second, timeout=1)
        assert contender_entered.is_set() is True
        for _ in range(100):
            if not uia_worker._hung_work_pending():
                break
            await asyncio.sleep(.01)
        assert not uia_worker._hung_work_pending()
    finally:
        release_com.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        pool = uia_worker._UIA_EXEC
        uia_worker._UIA_EXEC = None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
