"""Whisper sidecar lifecycle regression tests."""

from __future__ import annotations

import asyncio

import pytest

from speech.local_stt import WhisperServer


def _server() -> WhisperServer:
    return WhisperServer({"binary": "missing.exe", "model": "missing.bin"}, ".")


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_spawn_another_whisper_startup():
    voice = _server()
    entered = asyncio.Event()
    release = asyncio.Event()
    starts = 0

    async def slow_start(_timeout=120.0):
        nonlocal starts
        starts += 1
        entered.set()
        await release.wait()
        voice.ready = True

    voice.start = slow_start
    first = asyncio.create_task(voice.ensure_started())
    await asyncio.wait_for(entered.wait(), timeout=1)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    second = asyncio.create_task(voice.ensure_started())
    await asyncio.sleep(0)
    assert starts == 1
    release.set()
    await asyncio.wait_for(second, timeout=1)
    assert voice.ready is True
    assert starts == 1


@pytest.mark.asyncio
async def test_simultaneous_waiters_share_one_whisper_startup():
    voice = _server()
    release = asyncio.Event()
    starts = 0

    async def slow_start(_timeout=120.0):
        nonlocal starts
        starts += 1
        await release.wait()
        voice.ready = True

    voice.start = slow_start
    waiters = [asyncio.create_task(voice.ensure_started()) for _ in range(5)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert starts == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(*waiters), timeout=1)
    assert starts == 1


@pytest.mark.asyncio
async def test_stop_cancels_shared_startup_task():
    voice = _server()
    entered = asyncio.Event()

    async def never_ready(_timeout=120.0):
        entered.set()
        await asyncio.Event().wait()

    voice.start = never_ready
    waiter = asyncio.create_task(voice.ensure_started())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await voice.stop()
    result = await asyncio.gather(waiter, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert voice._start_task is None


@pytest.mark.asyncio
async def test_stop_serializes_against_replacement_startup_generation():
    voice = _server()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_second = asyncio.Event()
    starts = 0

    async def generation_start(_timeout=120.0):
        nonlocal starts
        starts += 1
        if starts == 1:
            first_entered.set()
            await asyncio.Event().wait()
        second_entered.set()
        await release_second.wait()
        voice.ready = True

    voice.start = generation_start
    first = asyncio.create_task(voice.ensure_started())
    await first_entered.wait()
    stopping = asyncio.create_task(voice.stop())
    await asyncio.sleep(0)
    replacement = asyncio.create_task(voice.ensure_started())

    await asyncio.wait_for(stopping, timeout=1)
    await asyncio.wait_for(second_entered.wait(), timeout=1)
    assert starts == 2
    assert voice._start_task is not None
    release_second.set()
    await asyncio.wait_for(replacement, timeout=1)
    assert voice.ready is True
    assert isinstance((await asyncio.gather(first, return_exceptions=True))[0],
                      asyncio.CancelledError)


@pytest.mark.asyncio
async def test_stop_closes_job_and_clears_refs_when_job_termination_fails():
    class _Proc:
        pid = 4242
        returncode = 0

    class _Job:
        def __init__(self) -> None:
            self.closed = False

        def terminate(self) -> bool:
            raise OSError(5, "job termination failed")

        def close(self) -> bool:
            self.closed = True
            return True

    voice = _server()
    job = _Job()
    voice.ready = True
    voice.proc = _Proc()
    voice._proc_job = job

    with pytest.raises(OSError, match="job termination failed"):
        await voice.stop()

    assert job.closed is True
    assert voice.ready is False
    assert voice.proc is None
    assert voice._proc_job is None


@pytest.mark.asyncio
async def test_exited_owned_whisper_generation_is_reaped_and_restarted(monkeypatch):
    class Proc:
        returncode = 7

        async def wait(self):
            return self.returncode

    disposed = []
    voice = _server()
    voice.ready = True
    voice.proc = Proc()
    voice._proc_job = object()
    starts = 0

    async def restart(_timeout=120.0):
        nonlocal starts
        starts += 1
        voice.ready = True

    monkeypatch.setattr(
        "speech.local_stt.dispose_process_tree",
        lambda job, terminate=False: disposed.append((job, terminate)),
    )
    voice.start = restart

    await voice.ensure_started()

    assert starts == 1
    assert disposed and disposed[0][1] is False
    assert voice.ready is True
