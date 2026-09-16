"""Shared process-tree ownership for local engine subprocesses."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from process_tree import (
    OwnedProcessTree,
    PROCESS_SET_QUOTA,
    PROCESS_TERMINATE,
    attach_process,
    attach_process_and_reap,
    dispose_process_tree,
    find_free_tcp_port,
    process_started_at,
    tcp_port_is_free,
)


class _FakeCtypes:
    def __init__(self, error: int = 5):
        self.error = error

    def get_last_error(self) -> int:
        return self.error

    @staticmethod
    def FormatError(error: int) -> str:
        return f"win32 error {error}"


class _FakeKernel32:
    def __init__(
        self,
        *,
        process_handle: int = 202,
        assign_result: bool = True,
        close_result: bool = True,
    ) -> None:
        self.process_handle = process_handle
        self.assign_result = assign_result
        self.close_result = close_result
        self.open_calls: list[tuple[int, bool, int]] = []
        self.assign_calls: list[tuple[int, int]] = []
        self.close_calls: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        self.open_calls.append((access, inherit, pid))
        return self.process_handle

    def AssignProcessToJobObject(self, job_handle: int, process_handle: int) -> bool:
        self.assign_calls.append((job_handle, process_handle))
        return self.assign_result

    def CloseHandle(self, handle: int) -> bool:
        self.close_calls.append(handle)
        return self.close_result


def _fake_job(kernel32: _FakeKernel32) -> OwnedProcessTree:
    job = OwnedProcessTree.__new__(OwnedProcessTree)
    job._is_windows = True
    job._ctypes = _FakeCtypes()
    job._kernel32 = kernel32
    job._handle = 101
    job._handle_lock = threading.RLock()
    job._closed = False
    return job


@pytest.mark.parametrize('reused_pid', [False, True])
def test_job_liveness_rechecks_children_born_during_parent_exit(monkeypatch, reused_pid):
    """An empty-looking first snapshot must not authorize killing a handoff."""
    class ExitingParent(_FakeKernel32):
        def __init__(self):
            super().__init__()
            self.first = True

        def OpenProcess(self, access, inherit, pid):
            if reused_pid and self.first:
                self.first = False
                return 0  # Gone before opening; this PID may already be reused.
            return pid

        def WaitForSingleObject(self, handle, _timeout):
            return 258 if reused_pid or handle == 22 else 0

    kernel32 = ExitingParent()
    job = _fake_job(kernel32)
    job._ctypes = _FakeCtypes(error=87)
    member_sets = iter([[11], [11 if reused_pid else 22]])
    monkeypatch.setattr(job, '_windows_process_ids', lambda: next(member_sets))
    assert job.active_process_count() == 1
    assert kernel32.close_calls == ([11] if reused_pid else [11, 22])


def test_posix_owned_group_liveness_ignores_zombies_and_other_groups(monkeypatch):
    import psutil

    job = OwnedProcessTree.__new__(OwnedProcessTree)
    job._is_windows = False
    job._closed = False
    job._handle_lock = threading.RLock()
    job._pgids = {42}
    processes = [
        SimpleNamespace(pid=11, info={'pid':11, 'status':psutil.STATUS_RUNNING}),
        SimpleNamespace(pid=12, info={'pid':12, 'status':psutil.STATUS_ZOMBIE}),
        SimpleNamespace(pid=13, info={'pid':13, 'status':psutil.STATUS_RUNNING}),
        SimpleNamespace(pid=14, info={'pid':14, 'status':psutil.STATUS_RUNNING}),
    ]
    monkeypatch.setattr(psutil, 'process_iter', lambda _attrs: iter(processes))
    def group(pid):
        if pid == 14:
            raise ProcessLookupError(pid)
        return 42 if pid in {11,12} else 43
    monkeypatch.setattr(os, 'getpgid', group, raising=False)
    try:
        assert job.active_process_count() == 1
    finally:
        job._closed = True
    assert job.active_process_count() == 0


def test_stale_job_pid_is_bounded_without_falsely_declaring_empty(monkeypatch):
    kernel32 = _FakeKernel32(process_handle=0)
    job = _fake_job(kernel32)
    job._ctypes = _FakeCtypes(error=87)
    monkeypatch.setattr(job, '_windows_process_ids', lambda: [11])
    with pytest.raises(OSError, match='did not stabilize'):
        job.active_process_count()
    assert len(kernel32.open_calls) == 8
    assert not job._closed


def test_job_query_error_does_not_leak_the_ownership_lock(monkeypatch):
    kernel32 = _FakeKernel32(process_handle=0)
    job = _fake_job(kernel32)
    monkeypatch.setattr(job, '_windows_process_ids', lambda: [11])
    with pytest.raises(OSError, match='tree liveness'):
        job.active_process_count()
    acquired = []

    def acquire():
        ok = job._handle_lock.acquire(timeout=1)
        acquired.append(ok)
        if ok:
            job._handle_lock.release()

    thread = threading.Thread(target=acquire)
    thread.start()
    thread.join(2)
    assert acquired == [True]


def test_shared_tcp_port_probe_and_ephemeral_selection_are_exact():
    host = "127.0.0.1"
    port = find_free_tcp_port(host)
    assert tcp_port_is_free(host, port) is True

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((host, port))
        assert tcp_port_is_free(host, port) is False
    finally:
        listener.close()


def test_shared_process_start_identity_resolves_the_current_process():
    assert process_started_at(os.getpid()) > 0


def test_assign_opens_asyncio_child_by_pid_and_closes_temporary_handle():
    kernel32 = _FakeKernel32()
    job = _fake_job(kernel32)
    proc = SimpleNamespace(pid=4242)

    assert job.assign(proc) is True
    assert kernel32.open_calls == [
        (
            PROCESS_SET_QUOTA | PROCESS_TERMINATE,
            False,
            4242,
        )
    ]
    assert kernel32.assign_calls == [(101, 202)]
    assert kernel32.close_calls == [202]


def test_assign_closes_temporary_handle_when_job_assignment_fails():
    kernel32 = _FakeKernel32(assign_result=False)
    job = _fake_job(kernel32)

    with pytest.raises(OSError, match="AssignProcessToJobObject.*win32 error 5"):
        job.assign(SimpleNamespace(pid=4242))

    assert kernel32.close_calls == [202]


def test_assign_reports_open_process_failure_without_attempting_assignment():
    kernel32 = _FakeKernel32(process_handle=0)
    job = _fake_job(kernel32)

    with pytest.raises(OSError, match=r"OpenProcess\(pid=4242\).*win32 error 5"):
        job.assign(SimpleNamespace(pid=4242))

    assert kernel32.assign_calls == []
    assert kernel32.close_calls == []


def test_assign_reports_failure_to_close_temporary_process_handle():
    kernel32 = _FakeKernel32(close_result=False)
    job = _fake_job(kernel32)

    with pytest.raises(OSError, match=r"CloseHandle\(process pid=4242\).*win32 error 5"):
        job.assign(SimpleNamespace(pid=4242))

    assert kernel32.assign_calls == [(101, 202)]
    assert kernel32.close_calls == [202]


def test_attach_process_kills_child_and_closes_new_owner_when_ownership_fails():
    class _BrokenJob:
        def __init__(self) -> None:
            self.closed = False

        def assign(self, _proc) -> bool:
            raise OSError(5, "assignment failed")

        def close(self) -> bool:
            self.closed = True
            return True

    class _Proc:
        pid = 4242
        returncode = None

        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    job = _BrokenJob()
    proc = _Proc()
    with pytest.raises(OSError, match="assignment failed"):
        attach_process(None, proc, factory=lambda: job)
    assert proc.killed is True
    assert job.closed is True


@pytest.mark.asyncio
async def test_attach_process_failure_is_reaped_before_it_propagates():
    class _BrokenJob:
        def assign(self, _proc) -> bool:
            raise OSError(5, "assignment failed")

    class _Proc:
        pid = 4242
        returncode = None

        def __init__(self) -> None:
            self.killed = False
            self.waited = False

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            self.returncode = 1
            return self.returncode

    proc = _Proc()
    with pytest.raises(OSError, match="assignment failed"):
        await attach_process_and_reap(_BrokenJob(), proc)
    assert proc.killed is True
    assert proc.waited is True
    assert proc.returncode == 1


def test_dispose_process_tree_always_closes_when_termination_fails():
    class _Job:
        def __init__(self) -> None:
            self.terminate_calls = 0
            self.close_calls = 0

        def terminate(self) -> bool:
            self.terminate_calls += 1
            raise OSError(5, "termination failed")

        def close(self) -> bool:
            self.close_calls += 1
            return True

    job = _Job()
    with pytest.raises(OSError, match="termination failed"):
        dispose_process_tree(job)
    assert job.terminate_calls == 1
    assert job.close_calls == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration")
@pytest.mark.asyncio
async def test_real_asyncio_process_is_owned_by_pid_and_dies_when_job_closes():
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    )
    job = OwnedProcessTree()
    try:
        # asyncio.subprocess.Process exposes a PID, not the private Popen handle
        # that engine_process previously tried to read.
        assert not hasattr(proc, "_handle")
        assert job.assign(proc) is True
        assert proc.returncode is None

        assert job.close() is True
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode is not None
    finally:
        job.close()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
