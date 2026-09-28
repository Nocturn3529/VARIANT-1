"""Caller cancellation during admission must never become a boot retry."""

import asyncio
from contextlib import suppress

import psutil
import pytest

from kernel_runtime import lease as lease_module
from test_kernel_runtime import kernel_stack  # noqa: F401 -- shared isolated stack


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_spawn", "awaiting_ready"])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancel_boot_retires_lease_without_retry_or_cell(
    kernel_stack, monkeypatch, tmp_path, stage, repeat_cancel,
):
    manager, runtimes, _ = kernel_stack
    chat_id = "cancel-boot"
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.catalog_service.select(chat_id, "build")
    marker = tmp_path / "cell-ran.txt"
    reached = asyncio.Event()
    release_boot = asyncio.Event()
    closing = asyncio.Event()
    release_close = asyncio.Event()
    leases = []
    resources = {}
    original_start = lease_module.KernelLease.start
    original_bridge_start = lease_module.KernelBridgeServer.start
    original_receive = lease_module._ReplTransport.receive
    original_close = lease_module.KernelLease._close_resources

    async def remember_start(self):
        leases.append(self)
        return await original_start(self)

    async def pause_bridge(self):
        result = await original_bridge_start(self)
        if stage == "before_spawn" and not reached.is_set():
            reached.set()
            await release_boot.wait()
        return result

    async def pause_ready(self, **kwargs):
        if stage == "awaiting_ready" and not reached.is_set():
            reached.set()
            await release_boot.wait()
        return await original_receive(self, **kwargs)

    async def pause_cleanup(self, **kwargs):
        closing.set()
        if repeat_cancel:
            await release_close.wait()
        return await original_close(self, **kwargs)

    monkeypatch.setattr(lease_module.KernelLease, "start", remember_start)
    monkeypatch.setattr(lease_module.KernelBridgeServer, "start", pause_bridge)
    monkeypatch.setattr(lease_module._ReplTransport, "receive", pause_ready)
    monkeypatch.setattr(lease_module.KernelLease, "_close_resources", pause_cleanup)

    task = asyncio.create_task(manager.execute(
        chat_id=chat_id,
        code=f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')",
        run_id="cancelled-run", outer_tool_call_id="cancelled-cell",
    ))
    try:
        await asyncio.wait_for(reached.wait(), timeout=15)
        first = leases[0]
        resources = {name: getattr(first, name) for name in ("process", "job", "bridge", "transport")}
        children = (
            psutil.Process(first.process.pid).children(recursive=True)
            if first.process is not None else []
        )
        task.cancel("user stop")
        await asyncio.wait_for(closing.wait(), timeout=10)
        if repeat_cancel:
            task.cancel("second stop")
            await asyncio.sleep(0)
            assert not task.done(), "Cancellation returned before resource cleanup"
        release_close.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, timeout=15)
        assert caught.value.args == ("user stop",)
        assert len(leases) == 1, "Cancelled boot started a replacement generation"
        assert not marker.exists(), "Cancelled caller's cell was executed"
        assert first._closed and first.state == "absent"
        assert chat_id not in manager._leases
        assert runtimes.kernel_lease(chat_id) is None
        assert not manager._boot_tasks
        assert resources["bridge"]._server is None
        if resources["process"] is not None:
            assert resources["process"].poll() is not None
            assert not resources["job"].active
            assert resources["transport"]._closed
            _, alive = psutil.wait_procs(children, timeout=5)
            assert not alive

        # Cancellation is not a permanent ban: only a new explicit request runs.
        release_boot.set()
        result = await manager.execute(
            chat_id=chat_id, code="print('new request')",
            run_id="later-run", outer_tool_call_id="later-cell",
        )
        assert result.ok, result.to_dict()
        assert "new request" in result.output.text()
        assert len(leases) == 2
        assert not marker.exists()
    finally:
        release_boot.set()
        release_close.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=15)
        await asyncio.wait_for(manager.shutdown(), timeout=15)


@pytest.mark.asyncio
async def test_real_boot_failure_still_retries(kernel_stack, monkeypatch):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("retry-boot", is_new=True)
    manager.catalog_service.select("retry-boot", "build")
    write = lease_module._write_private_json
    attempts = []

    def fail_first(path, value):
        attempts.append(path)
        if len(attempts) == 1:
            raise OSError("transient descriptor write failure")
        return write(path, value)

    monkeypatch.setattr(lease_module, "_write_private_json", fail_first)
    try:
        result = await manager.execute(
            chat_id="retry-boot", code="print('retried')",
            run_id="retry-run", outer_tool_call_id="retry-cell",
        )
        assert result.ok, result.to_dict()
        assert "retried" in result.output.text()
        assert len(attempts) == 2
        assert result.generation == 2
    finally:
        await asyncio.wait_for(manager.shutdown(), timeout=15)


@pytest.mark.asyncio
async def test_cleanup_error_does_not_replace_boot_cancellation(kernel_stack, monkeypatch):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("cancel-cleanup-error", is_new=True)
    original_close = lease_module.KernelLease._close_resources
    cancellation = asyncio.CancelledError("original stop")

    async def cancelled_start(self):
        raise cancellation

    async def failing_close(self, **kwargs):
        await original_close(self, **kwargs)
        raise OSError("injected teardown failure")

    monkeypatch.setattr(lease_module.KernelBridgeServer, "start", cancelled_start)
    monkeypatch.setattr(lease_module.KernelLease, "_close_resources", failing_close)
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await manager.execute(
                chat_id="cancel-cleanup-error", code="raise AssertionError('must not run')",
                run_id="cancel-cleanup-error", outer_tool_call_id="cancel-cleanup-error",
            )
        assert caught.value is cancellation
        assert any("injected teardown failure" in note for note in cancellation.__notes__)
        assert not manager._leases
        assert not manager._boot_tasks
    finally:
        await asyncio.wait_for(manager.shutdown(), timeout=15)
