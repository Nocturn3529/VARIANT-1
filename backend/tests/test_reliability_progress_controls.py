"""Progress-enabling input must reach a managed resource waiting for that input."""
import asyncio
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_kernel_runtime import kernel_stack


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 4])
@pytest.mark.parametrize("kind", ["process", "terminal"])
async def test_process_input_reaches_its_pending_wait(kernel_stack, tmp_path, capacity, kind):
    from capability_broker import current_capability_invocation
    from execution_hosts import create_execution_runtime, ExecutionOwner
    from execution_hosts.capabilities import execution_handle_envelope, register_execution_tools
    from work_fabric.capabilities import register_work_fabric_tools

    manager, runtimes, _ = kernel_stack
    manager.fanout_limit = lambda _: capacity
    execution = create_execution_runtime(data_dir=str(tmp_path / "execution"))
    runtime = SimpleNamespace(execution=execution, registry=manager.broker.registry, broker=manager.broker)
    host = SimpleNamespace(require_runtime=lambda: runtime, remote_handle_routers={})
    register_work_fabric_tools(host)
    register_execution_tools(host)
    manager.catalog_service.reconcile_registry()
    chat = "progress-input"
    runtimes.ensure_runtime(chat, is_new=True)
    manager.catalog_service.select(chat, "build")

    async def create(args):
        context = current_capability_invocation()
        argv = [
            sys.executable, "-u", "-c",
            "import sys; print('READY',flush=True); print('ECHO:'+sys.stdin.readline().strip(),flush=True)",
        ]
        owner = ExecutionOwner("chat", chat, context.work_scope)
        if kind == "process":
            record = execution.processes.start(argv, owner=owner, cwd=str(tmp_path))
        else:
            record = execution.terminals.open(owner=owner, cwd=str(tmp_path), profile="custom", argv=argv)
        envelope = execution_handle_envelope(host, context, record)
        identity = envelope["$variant1_handle"]
        validate = host.remote_handle_routers["execution"].control_admission
        assert await validate(context, identity, "write", {"data": "not-sent-by-validation"}) is True
        assert await validate(context, {**identity, "revision": 0}, "write", {"data": "not-sent"}) is False
        assert await validate(context, identity, "write", {"data": "not-sent", "extra": True}) is False
        from tools import ToolError
        from work_fabric.scope import WorkScope
        outsider = replace(context, chat_id="other", work_scope=WorkScope(chat_id="other"))
        with pytest.raises(ToolError):
            await validate(outsider, identity, "write", {"data": "not-sent"})
        return envelope

    manager.broker.registry.get("read_file").handler = create
    try:
        code = """
import asyncio,time
process = tools.read_file(path='create')
waiting = asyncio.create_task(process.wait.async_(timeout=2))
await asyncio.sleep(.2)
assert not waiting.done()
start = time.monotonic()
RESIZE
process = await process.write.async_(data=INPUT_LINE)
elapsed = time.monotonic() - start
observed = await waiting
print('INPUT_SECONDS', elapsed, observed.metadata['state'])
assert elapsed < 1, 'input queued behind the wait it must unblock'
assert observed.metadata['state'] == 'exited', observed.metadata
print(process.read())
"""
        code = code.replace("RESIZE", "process = await process.resize.async_(cols=100, rows=30)" if kind == "terminal" else "")
        code = code.replace("INPUT_LINE", repr("input-unblocks-owned-wait" + ("\r" if kind == "terminal" and os.name == "nt" else "\n")))
        result = await manager.execute(chat_id=chat, run_id="progress-run", outer_tool_call_id="progress-call", code=code)
        assert result.ok, result.to_dict()
        assert "ECHO:input-unblocks-owned-wait" in result.output.text()
    finally:
        await manager.shutdown()
        execution.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 4])
async def test_child_message_reaches_pending_wait_through_real_kernel(kernel_stack, capacity):
    from capability_broker import current_capability_invocation
    from session_catalog.children import _child_handle, register_children_tool
    from work_fabric.capabilities import register_work_fabric_tools
    from tools import ToolError
    manager, runtimes, _ = kernel_stack
    manager.fanout_limit = lambda _: capacity
    runtime = SimpleNamespace(registry=manager.broker.registry, broker=manager.broker)
    host = SimpleNamespace(require_runtime=lambda: runtime, remote_handle_routers={})
    sent = asyncio.Event()
    inbox = []
    row = {"child_id": "owned-child", "run_generation": 1, "status": "running", "work_job_id": "test-job"}

    class ChildFixture:
        def __init__(self):
            self.host = host
        def inspect(self, parent, child):
            if parent != "child-progress" or child != row["child_id"]:
                raise ToolError("child outside parent ownership")
            return dict(row)
        def send(self, parent, child, text):
            self.inspect(parent, child)
            inbox.append(text)
            row["status"] = "completed"
            sent.set()
            return dict(row)
        def _require_work(self):
            return SimpleNamespace(jobs=SimpleNamespace(wait=self.wait))
        async def wait(self, job_id, timeout_s):
            assert job_id == "test-job"
            await asyncio.wait_for(sent.wait(), timeout_s)

    child = ChildFixture()
    runtime.registry.remove("children")
    register_children_tool(runtime.registry, child)
    register_work_fabric_tools(host)
    manager.catalog_service.reconcile_registry()
    runtimes.ensure_runtime("child-progress", is_new=True)
    manager.catalog_service.select("child-progress", "build")
    async def create(args):
        context = current_capability_invocation()
        envelope = _child_handle(child, context, row)
        identity = envelope["$variant1_handle"]
        validate = host.remote_handle_routers["children"].control_admission
        assert await validate(context, identity, "send", {"text": "probe"}) is True
        with pytest.raises(ToolError):
            await validate(replace(context, chat_id="other"), identity, "send", {"text": "probe"})
        with pytest.raises(ToolError):
            await validate(context, {**identity, "generation": 2}, "send", {"text": "probe"})
        return envelope
    runtime.registry.get("read_file").handler = create
    try:
        result = await manager.execute(chat_id="child-progress", run_id="child-run", outer_tool_call_id="child-call", code="""
import asyncio,time
child = tools.read_file(path='fixture')
waiting = asyncio.create_task(child.wait.async_(timeout_s=2))
await asyncio.sleep(.2)
assert not waiting.done()
start = time.monotonic()
child = await child.send.async_(text='revised requirement')
finished = await waiting
assert time.monotonic() - start < 1
assert finished.metadata['status'] == 'completed'
""")
        assert result.ok, result.to_dict()
        assert inbox == ["revised requirement"]
    finally:
        await manager.shutdown()
