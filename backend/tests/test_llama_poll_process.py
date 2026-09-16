"""LlamaServer.poll_process clears stale ready after process exit."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from model_runtime.llama_server import LlamaServer


def test_poll_process_clears_ready_when_proc_exited():
    eng = LlamaServer({"autostart": False}, app_root=".")
    eng.ready = True
    eng.proc = SimpleNamespace(returncode=1, pid=1234)
    assert eng.poll_process() is False
    assert eng.ready is False
    assert eng.proc is None


def test_poll_process_keeps_ready_when_proc_alive():
    eng = LlamaServer({"autostart": False}, app_root=".")
    eng.ready = True
    eng.proc = SimpleNamespace(returncode=None, pid=1234)
    assert eng.poll_process() is True
    assert eng.ready is True
    assert eng.proc is not None


@pytest.mark.asyncio
async def test_stop_closes_job_and_clears_refs_when_job_termination_fails():
    class _Job:
        def __init__(self) -> None:
            self.closed = False

        def terminate(self) -> bool:
            raise OSError(5, "job termination failed")

        def close(self) -> bool:
            self.closed = True
            return True

    eng = LlamaServer({"autostart": False}, app_root=".")
    job = _Job()
    eng.ready = True
    eng.proc = SimpleNamespace(returncode=0, pid=1234)
    eng._proc_job = job

    with pytest.raises(OSError, match="job termination failed"):
        await eng.stop()

    assert job.closed is True
    assert eng.ready is False
    assert eng.proc is None
    assert eng._proc_job is None


def test_poll_process_closes_dead_process_job_without_terminating_it():
    class _Job:
        def __init__(self) -> None:
            self.terminate_calls = 0
            self.close_calls = 0

        def terminate(self) -> bool:
            self.terminate_calls += 1
            return True

        def close(self) -> bool:
            self.close_calls += 1
            return True

    eng = LlamaServer({"autostart": False}, app_root=".")
    job = _Job()
    eng.ready = True
    eng.proc = SimpleNamespace(returncode=1, pid=1234)
    eng._proc_job = job

    assert eng.poll_process() is False
    assert job.terminate_calls == 0
    assert job.close_calls == 1
    assert eng.proc is None
    assert eng._proc_job is None
