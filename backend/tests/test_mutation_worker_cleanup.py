"""Mutation results must survive retirement of their disposable process tree."""

import asyncio
import os
import subprocess
import sys
import time

import psutil
import pytest

from session_catalog.mutation_worker_client import MutationWorkerClient
from session_catalog.mutation_contracts import MutationWorkerError


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows launcher and directory ownership")
@pytest.mark.parametrize("attempt", range(20))
async def test_result_retires_child_holding_worker_directory(tmp_path, attempt):
    worker = MutationWorkerClient(str(tmp_path / "workers"))

    async def unused_proxy(*args):
        raise AssertionError("This candidate does not use proxies")

    report = await worker.run(
        {
            "mode": "execute",
            "source": (
                "import subprocess\n"
                "def run(arguments):\n"
                "    child = subprocess.Popen([arguments['python'], '-c', "
                "'import time; time.sleep(60)'], "
                "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "    return {'pid': child.pid}\n"
            ),
            "arguments": {"python": sys.executable},
            "proxy_contracts": {},
        },
        proxy_call=unused_proxy,
    )
    assert not psutil.pid_exists(report["result"]["pid"])
    assert list((tmp_path / "workers").iterdir()) == []


@pytest.mark.asyncio
async def test_candidate_error_survives_repeated_retirement(tmp_path):
    worker = MutationWorkerClient(str(tmp_path / "workers"))
    for _ in range(20):
        with pytest.raises(MutationWorkerError) as caught:
            await worker.run(
                {"mode": "validate", "source": "async def run(arguments):\n    pass\n"},
                proxy_call=None,
            )
        assert caught.value.code == "candidate_contract_error"
        assert list((tmp_path / "workers").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows venv launcher ownership")
async def test_launcher_cannot_spawn_interpreter_before_job_assignment(tmp_path, monkeypatch):
    from kernel_runtime.job_object import KernelJobObject

    assign = KernelJobObject.assign_pid
    children_before_assignment = []
    owner = None

    def delayed_assign(job, pid):
        nonlocal owner
        owner = job
        time.sleep(0.2)
        children_before_assignment.extend(psutil.Process(pid).children(recursive=True))
        return assign(job, pid)

    monkeypatch.setattr(KernelJobObject, "assign_pid", delayed_assign)
    worker = MutationWorkerClient(str(tmp_path / "workers"))

    async def verify_owner(name, arguments, request_id):
        with owner._handle_lock:
            assert arguments["pid"] in owner._windows_process_ids()
        return {"ok": True, "result": "owned"}

    report = await worker.run(
        {"mode": "execute", "source": (
            "import os\ndef run(arguments):\n    return tools.owner(pid=os.getpid())\n"
         ), "arguments": {}, "proxy_contracts": {"tools.owner": {"parameters": ["pid"]}}},
        proxy_call=verify_owner,
    )
    assert report["result"] == "owned"
    assert children_before_assignment == [], "Launcher ran before process-tree ownership"


@pytest.mark.asyncio
async def test_retirement_settles_tree_before_repeated_cancel_returns():
    released = asyncio.Event()
    terminated = asyncio.Event()
    closed = False

    class Process:
        stdin = None
        returncode = 0

        async def wait(self):
            await released.wait()

    class Job:
        def terminate(self):
            terminated.set()

        def active_process_count(self):
            return 0 if released.is_set() else 1

        def close(self):
            nonlocal closed
            assert released.is_set()
            closed = True

    task = asyncio.create_task(MutationWorkerClient._retire(Process(), Job()))
    await terminated.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed


@pytest.mark.asyncio
async def test_retirement_failure_still_kills_and_reaps_and_is_not_transport_error():
    events = []

    class Process:
        stdin = None
        returncode = None

        def kill(self):
            events.append("kill")

        async def wait(self):
            events.append("reap")

    class Job:
        def terminate(self):
            raise OSError("injected termination failure")

        def active_process_count(self):
            events.append("tree_empty")
            return 0

        def close(self):
            events.append("close")

    with pytest.raises(MutationWorkerError) as caught:
        await MutationWorkerClient._retire(Process(), Job())
    assert caught.value.code == "worker_cleanup"
    assert events == ["kill", "tree_empty", "reap", "close"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object escape path")
async def test_named_pid_outside_the_job_fails_retirement(tmp_path):
    """A candidate-named pid outside the Job Object must not retire green.

    The attempt-17 CI failure was a sleeper spawned in the in-worker venv
    launcher's pre-assignment window: outside the job, invisible to
    active_process_count(), and green after escaping. Deterministic replay:
    the TEST process (outside the mutation job) owns the sleeper and the
    candidate merely names its pid.
    """
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        worker = MutationWorkerClient(str(tmp_path / "workers"))

        async def unused_proxy(*args):
            raise AssertionError("This candidate does not use proxies")

        with pytest.raises(MutationWorkerError) as report:
            await worker.run(
                {
                    "mode": "execute",
                    "source": (
                        "def run(arguments):\n"
                        "    return {'pid': arguments['outside_pid']}\n"
                    ),
                    "arguments": {"outside_pid": sleeper.pid},
                    "proxy_contracts": {},
                },
                proxy_call=unused_proxy,
            )
        assert report.value.code == "worker_cleanup"
        message = report.value.details.get(
            "message", str(report.value)
        ) + "".join(str(note) for note in getattr(report.value, "__notes__", []))
        assert "survived retirement" in str(report.value.__cause__ or message) or (
            "survived retirement" in message
            or any("survived retirement" in str(cause) for cause in [report.value.__cause__])
        )
        assert str(sleeper.pid) in (
            str(report.value.__cause__ or "") + message
        )
    finally:
        sleeper.kill()
        sleeper.wait()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object gate membership")
async def test_gate_refuses_worker_missing_from_job(
    tmp_path, monkeypatch
):
    """The gate must not open for a worker that is not in the job.

    If a runner's CREATE_SUSPENDED assignment did not hold, the worker
    could reach candidate code while outside ownership; children spawned
    then escape termination. The parent must verify membership (not
    liveness, not 'in any job') before creating parent-owned.gate.
    """
    from session_catalog import mutation_worker_client as mwc

    worker = MutationWorkerClient(str(tmp_path / "workers"))

    async def unused_proxy(*args):
        raise AssertionError("This candidate does not use proxies")

    real_contains = mwc.__dict__.get("contains_pid")

    class FakeJob:
        def __init__(self):
            self.closed = False

        def contains_pid(self, pid):
            return False

        def terminate(self):
            pass

        def active_process_count(self):
            return 0

        def close(self):
            self.closed = True

        def terminate_and_close(self):
            self.closed = True

    # Patch the gate check only: resume keeps real behavior, the
    # membership query reports the worker missing.
    original_run_once = worker._run_once

    async def run_once_missing_membership(request, *, proxy_call):
        import session_catalog.mutation_worker_client as module

        original_contains = module.KernelJobObject.contains_pid

        def missing(self, pid):
            return False

        monkeypatch.setattr(module.KernelJobObject, "contains_pid", missing)
        try:
            return await original_run_once(request, proxy_call=proxy_call)
        finally:
            monkeypatch.setattr(
                module.KernelJobObject, "contains_pid", original_contains
            )

    worker._run_once = run_once_missing_membership

    with pytest.raises(MutationWorkerError) as report:
        await worker.run(
            {
                "mode": "execute",
                "source": "def run(arguments):\n    return {'ok': True}\n",
                "arguments": {},
                "proxy_contracts": {},
            },
            proxy_call=unused_proxy,
        )
    assert report.value.code == "worker_cleanup"
    assert "not in its Job Object before the gate" in str(report.value.__cause__ or report.value)
