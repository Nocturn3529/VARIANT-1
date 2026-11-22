"""Voluntary checkpointed restart and cell admission share a generation fence."""

import asyncio
import threading
from uuid import uuid4

import pytest

from kernel_runtime.capsules import KernelCheckpointPolicy
from test_kernel_runtime import kernel_stack  # noqa: F401


@pytest.mark.asyncio
async def test_restart_rechecks_cells_that_already_acquired_the_lease(kernel_stack, tmp_path, monkeypatch):
    manager, runtimes, _ = kernel_stack
    manager.checkpoint_policy = KernelCheckpointPolicy(enabled=True, restore_on_boot=True)
    chat = "restart-race"
    runtimes.ensure_runtime(chat, is_new=True)

    async def cell(code):
        return await manager.execute(chat_id=chat, code=code, run_id=chat, outer_tool_call_id=uuid4().hex)

    admitted, release = threading.Event(), threading.Event()
    original = manager.cell_ledger.admit

    def hold_admission(*args, **kwargs):
        admitted.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    active = None
    try:
        seed = await cell("saved = 42")
        monkeypatch.setattr(manager.cell_ledger, "admit", hold_admission)
        active = asyncio.create_task(cell(
            "from pathlib import Path\nimport asyncio\nPath('running').touch()\n"
            "while not Path('finish').exists(): await asyncio.sleep(.01)\nsaved += 1"
        ))
        assert await asyncio.to_thread(admitted.wait, 3)
        assert manager.status(chat)["state"] == "ready"
        assert (await manager.restart(chat))["status"] == "busy"
        # Hold the real retirement lock while an already admitted cell starts.
        async with manager._boot_locks[chat]:
            restarting = asyncio.create_task(manager.restart(chat))
            await asyncio.sleep(0)
            release.set()
            async with asyncio.timeout(3):
                while not (tmp_path / "running").exists():
                    await asyncio.sleep(.01)
        restarted = await asyncio.wait_for(restarting, 2)
        assert restarted["status"] == "busy"
        assert manager.status(chat)["generation"] == seed.generation
        assert not active.done()
        (tmp_path / "finish").touch()
        assert (await active).ok
        assert (await cell("print(saved)")).output.text().strip() == "43"
        assert (await manager.restart(chat))["checkpoint"]["status"] == "captured"
        restored = await cell("print(saved)")
        assert restored.generation > seed.generation and restored.output.text().strip() == "43"
    finally:
        release.set()
        (tmp_path / "finish").touch()
        if active is not None:
            await asyncio.gather(active, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_new_cell_after_checkpoint_cannot_enter_until_retirement(kernel_stack, tmp_path, monkeypatch):
    manager, runtimes, _ = kernel_stack
    manager.checkpoint_policy = KernelCheckpointPolicy(enabled=True, restore_on_boot=True)
    chat = "checkpoint-to-close"
    runtimes.ensure_runtime(chat, is_new=True)
    captured, release = asyncio.Event(), asyncio.Event()
    original = manager.continuity._checkpoint_lease

    async def boundary(lease, *, reason, **kwargs):
        outcome = await original(lease, reason=reason, **kwargs)
        if reason == "operator_restart":
            assert outcome["status"] == "captured"
            captured.set()
            await release.wait()
        return outcome

    async def cell(code):
        return await manager.execute(chat_id=chat, code=code, run_id=chat, outer_tool_call_id=uuid4().hex)

    tasks = []
    try:
        first = await cell("marker = 42")
        monkeypatch.setattr(manager.continuity, "_checkpoint_lease", boundary)
        restart = asyncio.create_task(manager.restart(chat))
        tasks.append(restart)
        await asyncio.wait_for(captured.wait(), 3)
        later = asyncio.create_task(cell("marker += 1\nwith open('effect', 'a') as f: f.write(str(marker))"))
        tasks.append(later)
        await asyncio.sleep(0)
        assert not later.done() and not (tmp_path / "effect").exists()
        release.set()
        assert (await restart)["status"] == "closed"
        result = await later
        assert result.ok and result.generation > first.generation
        assert (tmp_path / "effect").read_text() == "43"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.shutdown()
