"""Cross-cell continuity, output and durable settlement on a real worker."""

import asyncio
import json
import sqlite3
import threading
from uuid import uuid4

import pytest

from kernel_runtime.capsules import KernelCheckpointPolicy
from test_kernel_runtime import kernel_stack  # noqa: F401


@pytest.fixture
async def live(kernel_stack):
    manager, runtimes, store = kernel_stack

    async def cell(code, chat="test"):
        runtimes.ensure_runtime(chat, is_new=True)
        return await manager.execute(chat_id=chat, code=code, run_id=chat,
                                     outer_tool_call_id=uuid4().hex)

    try:
        yield manager, cell, store
    finally:
        await manager.shutdown()


async def test_checkpoint_failure_preserves_live_generation_until_corrected(live):
    manager, cell, _ = live
    manager.checkpoint_policy = KernelCheckpointPolicy(enabled=True, restore_on_boot=True)
    first = await cell("keep = 42\noversized = bytes(8*1024*1024+1)\nitems = []\nalias = items\nother = []")
    for result in (await manager.restart("test"),
                   await manager.release_idle("test", expected_generation=first.generation)):
        assert result["status"] == "checkpoint_failed"
        assert result["checkpoint"]["status"] == "failed"
        assert "oversized" in result["checkpoint"]["message"]
    still_live = await cell("keep")
    assert still_live.generation == first.generation and still_live.output.text().strip() == "42"
    assert (await cell("del oversized")).ok
    assert (await manager.restart("test"))["checkpoint"]["status"] == "captured"
    restored = await cell("alias.append(keep)\nassert items is alias and items == [42] and other == []")
    assert restored.ok and restored.generation > first.generation
    assert (await cell("oversized = bytes(8*1024*1024+1)")).ok
    await manager.shutdown()
    assert manager.status("test")["state"] == "absent"


async def test_idle_output_is_bounded_and_control_stays_responsive(live):
    manager, cell, _ = live
    origin = await cell(
        "import asyncio\n"
        "async def later():\n"
        "    await asyncio.sleep(0.05)\n"
        "    for i in range(120): print('🚀'*1400, i)\n"
        "    print('last idle update')\n"
        "background = asyncio.create_task(later())"
    )
    assert origin.ok
    async with asyncio.timeout(8):
        while True:
            output = manager.status("test")["background_output"]
            if any("last idle update" in row["text"] for row in output["tail"]):
                break
            await asyncio.sleep(0.02)
    assert 1 <= output["events"] <= 64 and output["retained_bytes"] <= 64 * 1024
    assert all(row["origin_id"] == origin.execution_id for row in output["tail"])
    assert manager._leases["test"].transport.queue.empty()
    assert (await cell("1+1")).output.text().strip() == "2"
    long = asyncio.create_task(cell("await asyncio.sleep(20)"))
    async with asyncio.timeout(5):
        while not manager._leases["test"]._admissions:
            await asyncio.sleep(0.01)
        await manager.interrupt("test", intent="stop")
        assert (await long).status == "cancelled"


async def test_causal_stream_boundaries_are_in_durable_output(live):
    _, cell, artifacts = live
    for statement, terminal_kind in [("3", "execute_result"), ("display('three')", "display_data"),
                                     ("raise ValueError('three')", "error")]:
        outcome = await cell("import sys\nprint('first')\nprint('second')\n"
                             "print('third', file=sys.stderr)\n" + statement)
        events = json.loads(artifacts.read_bytes_scoped(outcome.output_evidence_ref, "test"))["events"]
        assert events[-1]["type"] == terminal_kind
        assert "".join(e["body"]["data"] for e in events[:-1]) == "first\nsecond\nthird\n"


async def test_live_compiler_flags_and_source_follow_code_lifetime(live):
    manager, cell, _ = live
    assert (await cell("from __future__ import annotations")).ok
    assert (await cell("def kept(x: UnknownType):\n    raise ValueError('original source line')")).ok
    assert (await cell("import asyncio\nawait asyncio.sleep(0)\nassert kept.__annotations__['x'] == 'UnknownType'")).ok
    for i in range(18):
        assert (await cell(f"temp = {i}")).ok
    cached = await cell("import gc, linecache\ngc.collect()\n"
                        "len([k for k in linecache.cache if str(k).startswith('<variant1-cell:')])")
    assert int(cached.output.text().strip()) <= 4
    error = await cell("kept(0)")
    assert any("raise ValueError('original source line')" in line for line in error.output.traceback)
    await manager.restart("test")
    assert (await cell("def fresh(x: UnknownType): pass\nfresh.__annotations__")).output.error_name == "NameError"


async def test_ledger_writer_wait_yields_and_cancelled_admission_has_no_effect(live, tmp_path, monkeypatch):
    manager, cell, _ = live
    assert (await cell("marker = []")).ok
    assert (await cell("marker = []", "peer")).ok
    entered = threading.Event()
    original = manager.cell_ledger.admit

    def admit(instance, evidence):
        entered.set()
        return original(instance, evidence)

    monkeypatch.setattr(manager.cell_ledger, "admit", admit)
    # An independent connection owns the actual SQLite write lock. A timed
    # release also prevents a regression from hanging the test event loop.
    db = sqlite3.connect(manager.cell_ledger.path, check_same_thread=False, isolation_level=None)
    db.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(1.5, db.commit)
    timer.start()
    try:
        pending = asyncio.create_task(cell("marker.append('cancelled')"))
        assert await asyncio.to_thread(entered.wait, 1)
        peer = asyncio.create_task(cell("marker.append('peer')", "peer"))
        await asyncio.sleep(0.025)
        assert db.in_transaction and not pending.done()
        pending.cancel()
        db.commit()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await peer).ok
        assert (await cell("marker")).output.text().strip() == "[]"
        assert [row["status"] for row in manager.execution_history("test")["items"]] == ["ok", "cancelled", "ok"]
    finally:
        timer.cancel()
        timer.join()
        db.close()

    def fail(*args, **kwargs):
        raise OSError("fixture write error")

    with monkeypatch.context() as patch:
        patch.setattr(manager.cell_ledger, "admit", fail)
        result = await cell("marker.append('never dispatched')")
        assert result.error_code == "cell_admission_failed"
    with monkeypatch.context() as patch:
        patch.setattr(manager.cell_ledger, "append", fail)
        result = await cell("marker.append('once')")
        assert result.ok and "fixture write error" in result.ledger_error
    assert (await cell("marker")).output.text().strip() == "['once']"
