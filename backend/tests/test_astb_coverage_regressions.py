"""Regressions from actual ASTB coverage, through persistent worker callbacks."""

import asyncio
import os
import sys
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from test_kernel_runtime import kernel_stack
from run_context import Variant1RunContext, bind_run_context, current_run_context
from work_fabric.scope import WorkScope, current_work_scope


@pytest.mark.asyncio
async def test_control_gate_overlaps_effect_but_preserves_effect_serialization():
    from kernel_runtime.lease import _CapabilityConcurrencyGate
    gate = _CapabilityConcurrencyGate()
    await gate.acquire(False)
    blocked = asyncio.create_task(gate.acquire(False))
    await asyncio.sleep(0)
    await asyncio.wait_for(gate.acquire(False, control=True), .2)
    assert not blocked.done()
    await gate.release(False, control=True)
    assert not blocked.done()
    await gate.release(False)
    await asyncio.wait_for(blocked, .2)
    await gate.release(False)


@pytest.mark.asyncio
async def test_job_cancel_reaches_wait_after_revision_advances(kernel_stack, tmp_path):
    from capability_broker import current_capability_invocation
    from work_fabric.service import WorkService
    from work_fabric.capabilities import register_work_fabric_tools
    from work_fabric.handles import job_handle_envelope

    manager, runtimes, _ = kernel_stack
    manager.fanout_limit = lambda _: 1
    work = WorkService.open(str(tmp_path / "cancel-work.sqlite3"))
    runtime = SimpleNamespace(work=work, registry=manager.broker.registry, broker=manager.broker)
    host = SimpleNamespace(require_runtime=lambda: runtime, remote_handle_routers={})
    register_work_fabric_tools(host)
    manager.catalog_service.reconcile_registry()
    chat = "job-control"
    runtimes.ensure_runtime(chat, is_new=True)
    manager.catalog_service.select(chat, "build")
    created = []

    async def create(args):
        context = current_capability_invocation()
        job = work.jobs.create("test.pending", owner_kind="chat", owner_id=chat, scope=context.work_scope)
        created.append(job)
        return job_handle_envelope(job, broker=manager.broker, context=context)

    manager.broker.registry.get("read_file").handler = create
    original_wait = work.jobs.wait

    async def advance_and_wait(job_id, **kwargs):
        current = work.jobs.require(job_id)
        work.jobs.pause(job_id, expected_revision=current.revision)
        return await original_wait(job_id, **kwargs)

    work.jobs.wait = advance_and_wait
    try:
        result = await asyncio.wait_for(manager.execute(chat_id=chat, run_id="job-run", outer_tool_call_id="job-call", code="""
import asyncio, time
job = tools.read_file(path='create')
pending = asyncio.create_task(job.wait.async_(timeout_s=20))
await asyncio.sleep(.3)
assert not pending.done()
start = time.monotonic()
cancelled = await job.cancel.async_()
await pending
assert time.monotonic() - start < 2
print(cancelled.metadata['status'])
"""), 15)
        assert result.ok, result.to_dict()
        assert "cancelled" in result.output.text()
        latest = work.jobs.require(created[0].job_id)
        assert latest.terminal and latest.revision > created[0].revision
    finally:
        await manager.shutdown()
@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows PIPE signal contract")
async def test_process_signal_receipt_and_stop_during_wait_reach_kernel(kernel_stack, tmp_path):
    from capability_broker import current_capability_invocation
    from execution_hosts import create_execution_runtime, ExecutionOwner
    from execution_hosts.capabilities import execution_handle_envelope, register_execution_tools
    from work_fabric.capabilities import register_work_fabric_tools

    manager, runtimes, _ = kernel_stack
    manager.fanout_limit = lambda _: 1
    execution = create_execution_runtime(data_dir=str(tmp_path / "execution"))
    runtime = SimpleNamespace(execution=execution, registry=manager.broker.registry, broker=manager.broker)
    host = SimpleNamespace(require_runtime=lambda: runtime, remote_handle_routers={})
    register_work_fabric_tools(host)
    register_execution_tools(host)
    manager.catalog_service.reconcile_registry()
    chat = "process-control"
    runtimes.ensure_runtime(chat, is_new=True)
    manager.catalog_service.select(chat, "build")

    async def create(args):
        context = current_capability_invocation()
        record = execution.processes.start([sys.executable, "-c", "import time; time.sleep(20)"],
            owner=ExecutionOwner("chat", chat, context.work_scope), cwd=str(tmp_path))
        return execution_handle_envelope(host, context, record)

    manager.broker.registry.get("read_file").handler = create
    try:
        result = await asyncio.wait_for(manager.execute(chat_id=chat, run_id="process-run", outer_tool_call_id="process-call", code="""
import asyncio,time
process = tools.read_file(path='create')
process = await process.signal.async_()
receipt = process.metadata['signal_receipt']
assert receipt['accepted'] is False and receipt['supported'] is False
assert receipt['status'] == 'unsupported' and receipt['transport'] == 'pipe'
assert 'signal_receipt=' in repr(process)
pending = asyncio.create_task(process.wait.async_(timeout=20))
await asyncio.sleep(.3)
assert not pending.done()
start = time.monotonic()
stopped = await process.stop.async_()
await pending
assert time.monotonic() - start < 3
print(stopped.metadata['state'])
"""), 15)
        assert result.ok, result.to_dict()
        assert "terminated" in result.output.text() or "exited" in result.output.text()
    finally:
        await manager.shutdown()
        execution.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [1, 4])
async def test_mcp_cancel_reaches_pending_request_through_real_worker(kernel_stack, tmp_path, capacity):
    from capability_broker import InvocationContext
    from extensions.mcp_v2 import McpV2Service, McpV2Error
    from extensions.capabilities_v2 import register_extension_v2_tools
    from test_extensions_v2 import _FakeSession, _service
    from work_fabric.capabilities import register_work_fabric_tools
    from dataclasses import replace

    manager, runtimes, _ = kernel_stack
    manager.fanout_limit = lambda _: capacity
    audit = []
    entered = asyncio.Event()
    active = 0
    peak = 0

    class Session(_FakeSession):
        async def call_tool(self, name, arguments, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            ordinary = arguments["text"] == "ordinary"
            audit.append("ordinary_started" if ordinary else "started")
            entered.set()
            try:
                await asyncio.sleep(.05 if ordinary else 20)
                audit.append("ordinary_completed" if ordinary else "completed")
                return {"content": [{"type": "text", "text": "done"}]}
            except asyncio.CancelledError:
                audit.append("cancelled")
                raise
            finally:
                active -= 1

    mcp = McpV2Service(opener=lambda _: asyncio.sleep(0, result=Session()), deadline_s=30)
    await mcp.connect("demo", {"transport": "streamable_http", "url": "https://example.test/mcp"})
    runtime = SimpleNamespace(broker=manager.broker, registry=manager.broker.registry,
        extensions=SimpleNamespace(mcp=mcp, packages=_service(tmp_path / "packages"), workers=SimpleNamespace()))
    host = SimpleNamespace(require_runtime=lambda: runtime, remote_handle_routers={})
    register_work_fabric_tools(host)
    register_extension_v2_tools(host)
    manager.broker.enabled_resolver = lambda: {tool.name for tool in runtime.registry.all()}
    manager.catalog_service.enabled_resolver = manager.broker.enabled_resolver
    manager.catalog_service.reconcile_registry()
    chat = "real-control"
    runtimes.ensure_runtime(chat, is_new=True)
    manager.catalog_service.select(chat, "operate")
    task = asyncio.create_task(manager.execute(chat_id=chat, run_id="control-run", outer_tool_call_id="control-call", code=f"capacity = {capacity}\n" + """
import asyncio, time
match = connectors.search(query='echo').top_match
pending = asyncio.create_task(match.handle.invoke.async_(arguments={'text': 'pending'}, request_id='owned-wait'))
await asyncio.sleep(.1)
queued = [asyncio.create_task(match.handle.invoke.async_(arguments={'text': 'ordinary'}, request_id=f'queued-{i}')) for i in range(capacity - 1)]
await asyncio.sleep(.5)
assert not pending.done()
start = time.monotonic()
cancelled = await match.cancel.async_(request_id='owned-wait')
assert cancelled is True
assert time.monotonic() - start < 2
outcome = await asyncio.gather(pending, return_exceptions=True)
assert isinstance(outcome[0], Variant1CapabilityError), outcome
assert outcome[0].code == 'broker_cancelled_after_dispatch', outcome
assert outcome[0].receipt, outcome
await asyncio.gather(*queued)
assert await match.cancel.async_(request_id='owned-wait') is False
print('cancel-control-passed')
"""))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        server = mcp._require("demo")
        origin, lease = server.request_owners["owned-wait"]
        with pytest.raises(McpV2Error, match="outside"):
            mcp.validate_cancel(lease, "owned-wait", origin=replace(origin, chat_id="another-chat"))
        result = await asyncio.wait_for(task, 8)
        assert result.ok, result.to_dict()
        assert "cancel-control-passed" in result.output.text()
        assert audit[:2] == ["started", "cancelled"]
        assert audit.count("ordinary_completed") == capacity - 1
        assert peak == 1  # Reserved control capacity never runs extra ordinary work.
        assert not server.requests and not server.request_owners
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()
        await mcp.disconnect("demo")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_reused_worker_question_uses_current_run_and_transport(kernel_stack, tmp_path, cancel):
    from clarification import tool_ask_user, resolve_response
    from capability_broker import current_capability_invocation
    from work_fabric.service import WorkService

    manager, runtimes, _ = kernel_stack
    work = WorkService.open(str(tmp_path / "questions.sqlite3"))
    chat = "reused-bridge-context"
    runtimes.ensure_runtime(chat, is_new=True)
    manager.catalog_service.select(chat, "build")
    asked = asyncio.Event()
    seen = []
    ambient = ContextVar("test_callback_ambient", default="absent")

    class Transport:
        def __init__(self):
            self.messages = []

        async def send_json(self, value):
            self.messages.append(value)
            if value["type"] == "clarification:request":
                asked.set()

    old, current = Transport(), Transport()

    async def callback(args):
        ctx = current_run_context()
        invocation = current_capability_invocation()
        seen.append((ctx.run_id, ctx.work_scope, current_work_scope(), ambient.get(), ctx.chat_transport))
        assert ctx.run_id == invocation.run_id
        assert ctx.work_scope.kernel_generation == int(invocation.kernel_generation)
        if args["path"] == "ask":
            return await tool_ask_user(work.interactions, {"questions": [{
                "question": "Choose a label", "header": "Label",
                "options": [{"label": "Cedar", "description": "One label"},
                            {"label": "Maple", "description": "Another label"}],
            }]})
        return "ready"

    manager.broker.registry.get("read_file").handler = callback
    task = None
    try:
        for label, transport in (("A", old), ("B", current)):
            ctx = Variant1RunContext.create(
                source="chat", run_id=label, work_scope=WorkScope(chat_id=chat),
                chat_session=SimpleNamespace(), chat_transport=transport,
            )
            with bind_run_context(ctx):
                token = ambient.set(label)
                try:
                    task = asyncio.create_task(manager.execute(
                        chat_id=chat, code="print(tools.read_file(path=" + repr("ask" if label == "B" else "seed") + "))",
                        run_id=label, outer_tool_call_id="call-" + label,
                    ))
                finally:
                    ambient.reset(token)
            if label == "A":
                first = await task
                assert first.ok, first.to_dict()
        await asyncio.wait_for(asked.wait(), 15)
        assert old.messages == []
        request = current.messages[0]
        assert request["run_id"] == "B"
        record = work.interactions.get(request["id"])
        assert record.owner_id == record.metadata["run_id"] == "B"
        assert record.scope.kernel_generation == first.generation
        assert record.scope.catalog_release_id
        assert seen[-1][0] == seen[-1][3] == "B"
        assert seen[-1][1] == seen[-1][2] == record.scope
        assert seen[-1][4] is current
        if cancel:
            await manager.interrupt(chat)
        else:
            assert resolve_response(work.interactions, request["id"], {"q1": "Cedar"}, skipped=False)
        result = await asyncio.wait_for(task, 6)
        assert result.status == ("cancelled" if cancel else "ok"), result.to_dict()
        assert result.generation == first.generation
        assert work.interactions.get(request["id"]).status == ("cancelled" if cancel else "answered")
        assert current.messages[-1]["type"] == "clarification:closed"
        assert current.messages[-1]["run_id"] == "B"
        assert old.messages == []
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "AT21 authorized final lifecycle test"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_public_restart_checkpoint_boundary_is_independent_of_reason(kernel_stack, tmp_path, reason, enabled):
    from kernel_runtime.capabilities import KERNEL_RESTART_JOB, register_kernel_control_job_handlers
    from work_fabric.capabilities import register_work_fabric_tools
    from work_fabric.service import WorkService

    manager, runtimes, artifacts = kernel_stack
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    runtime = SimpleNamespace(work=work, kernel=manager, session_artifacts=artifacts,
                              broker=manager.broker, registry=manager.broker.registry)
    host = SimpleNamespace(require_runtime=lambda: runtime, remote_handle_routers={})
    manager.catalog_service.host = host
    register_work_fabric_tools(host)
    manager.broker.enabled_resolver = lambda: {tool.name for tool in runtime.registry.all()}
    manager.catalog_service.enabled_resolver = manager.broker.enabled_resolver
    manager.catalog_service.reconcile_registry()
    handlers = {}
    work.register_job_handler = lambda kind, handler: handlers.__setitem__(kind, handler)
    register_kernel_control_job_handlers(host)
    chat = "public-restart"
    runtimes.ensure_runtime(chat, is_new=True)
    try:
        code = (
            f"session.configure_continuity(checkpoint_enabled={enabled}, restore_on_boot={enabled})\n"
            "checkpoint_marker = {'value': 73, 'nonce': 'current'}\n"
            + ("session.restart()" if reason is None else f"session.restart(reason={reason!r})")
        )
        first = await manager.execute(chat_id=chat, code=code, run_id="restart-A", outer_tool_call_id="restart-call")
        assert first.ok, first.to_dict()
        jobs = work.jobs.list(owner_kind="chat", owner_id=chat)
        job = next(row for row in jobs if row.kind == KERNEL_RESTART_JOB)
        result = await handlers[KERNEL_RESTART_JOB](SimpleNamespace(job=job))
        assert result.progress["checkpoint"]["boundary"] == "operator_restart"
        assert result.progress["checkpoint"]["status"] == ("captured" if enabled else "skipped")
        assert result.progress["requested_reason"] == (reason or "ipython_restart")
        second = await manager.execute(chat_id=chat, code="print(globals().get('checkpoint_marker'))",
                                       run_id="restart-B", outer_tool_call_id="restart-verify")
        assert second.ok, second.to_dict()
        assert second.generation > first.generation
        assert ("'value': 73" in second.output.text()) == enabled
    finally:
        await manager.shutdown()
