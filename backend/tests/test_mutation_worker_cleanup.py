"""Mutation results must survive retirement of their disposable process tree."""

import asyncio
import os
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
