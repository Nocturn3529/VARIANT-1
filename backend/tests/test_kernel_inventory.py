"""Inventory observes retained state; explicit release fences active admission."""

from types import SimpleNamespace

import pytest

from test_kernel_runtime import kernel_stack  # noqa: F401
import ws_kernel


@pytest.mark.asyncio
async def test_inventory_measures_real_interpreter_and_release_preserves_other_chats(kernel_stack):
    manager, runtimes, _ = kernel_stack
    for chat in ("retained-a", "retained-b"):
        runtimes.ensure_runtime(chat, is_new=True)
        result = await manager.execute(chat_id=chat,
            code="import os\nretained = bytearray(32 * 1024 * 1024)\nprint(os.getpid())",
            run_id=chat, outer_tool_call_id=chat)
        assert result.ok
    rows = {row["chat_id"]: row for row in manager.live_inventory()}
    assert len(rows) == 2
    assert rows["retained-b"]["resources"]["process"]["pid"] == int(result.output.text().strip())
    assert rows["retained-b"]["resources"]["process"]["rss_bytes"] >= 32 * 1024 * 1024
    admission = runtimes.try_reserve_run("retained-a")
    try:
        generation = manager.status("retained-a")["generation"]
        assert (await manager.release_idle("retained-a", expected_generation=generation + 1))["status"] == "stale"
        assert (await manager.release_idle("retained-a", expected_generation=generation))["status"] == "busy"
        assert manager.status("retained-a")["state"] == "ready"
    finally:
        runtimes.finish_run(admission, status="complete")
    assert (await manager.release_idle("retained-a", expected_generation=generation))["status"] == "closed"
    assert manager.status("retained-a")["state"] == "absent"
    follow = await manager.execute(chat_id="retained-b", code="print(len(retained))",
                                  run_id="return", outer_tool_call_id="return")
    assert follow.generation == result.generation and follow.ok
    assert follow.output.text().strip() == str(32 * 1024 * 1024)


@pytest.mark.asyncio
async def test_inventory_reports_running_cell_queue_last_cell_and_last_exit(kernel_stack):
    import asyncio

    manager, runtimes, _ = kernel_stack
    chat = "observed"
    runtimes.ensure_runtime(chat, is_new=True)
    first = await manager.execute(chat_id=chat, code="\n\nvalue = 1  # first line\n", run_id="r0",
                                  outer_tool_call_id="c0")
    assert first.ok
    row = manager.live_inventory()[0]
    assert row["current_cell"] is None and row["queued_cells"] == 0
    assert row["last_cell"]["execution_id"] == first.execution_id
    assert row["last_cell"]["status"] == "ok" and row["last_exit"] is None

    lease = manager._leases[chat]
    running = asyncio.create_task(lease.execute(
        "import time\ntime.sleep(1.5)", manager_admission(manager, lease, "slow")))
    waiting = asyncio.create_task(lease.execute(
        "after = 2", manager_admission(manager, lease, "queued")))
    for _ in range(200):
        row = manager.live_inventory()[0]
        if row["current_cell"] and row["queued_cells"] == 1:
            break
        await asyncio.sleep(0.02)
    assert row["current_cell"]["label"] == "import time"
    assert row["current_cell"]["execution_id"] == "slow"
    assert row["current_cell"]["started_at"] > 0
    assert row["queued_cells"] == 1
    await asyncio.gather(running, waiting)
    assert manager.live_inventory()[0]["current_cell"] is None

    history = manager.execution_history(chat, tail=1)
    assert [item["label"] for item in history["items"]] == ["value = 1 # first line"]

    await manager.restart(chat, reason="operator_restart")
    await manager.execute(chat_id=chat, code="value = 3", run_id="r1", outer_tool_call_id="c1")
    exit_row = manager.live_inventory()[0]["last_exit"]
    assert exit_row["reason"] == "operator_restart"
    assert exit_row["generation"] == first.generation


def manager_admission(manager, lease, execution_id):
    from kernel_runtime.contracts import ExecutionAdmission

    return ExecutionAdmission(
        execution_id=execution_id, chat_id=lease.chat_id, run_id="direct",
        outer_tool_call_id=execution_id, generation=lease.generation,
        catalog_release_id="", mount_revision=0, selected_category_id="",
        overlay_revision=0, environment_digest="", workspace_root_ids=(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unhealthy", "close_failed"])
async def test_manual_release_can_retry_idle_unhealthy_cleanup(kernel_stack, state):
    manager, runtimes, _ = kernel_stack
    chat = "cleanup-retry"
    runtimes.ensure_runtime(chat, is_new=True)
    result = await manager.execute(chat_id=chat, code="value = 42", run_id=chat, outer_tool_call_id=chat)
    assert result.ok
    manager._leases[chat].state = state
    assert (await manager.release_idle(chat, expected_generation=result.generation))["status"] == "closed"
    assert manager.status(chat)["state"] == "absent"


@pytest.mark.asyncio
async def test_detached_inventory_and_release_cannot_access_another_chat():
    handlers = {}
    def on(*names):
        def register(fn):
            for name in names: handlers[name] = fn
            return fn
        return register
    ws_kernel.register(on)
    releases = []
    async def release(chat, **kwargs):
        releases.append(chat)
        return {"status": "closed"}
    runtime = SimpleNamespace(kernel=SimpleNamespace(live_inventory=lambda: [
        {"chat_id": "own", "generation": 1}, {"chat_id": "other", "generation": 2}], release_idle=release),
        sessions=SimpleNamespace(get_session=lambda chat: {"title": chat}),
        session_runtimes=SimpleNamespace(is_busy=lambda chat: False))
    messages = []
    async def send(value): messages.append(value)
    host = SimpleNamespace(require_runtime=lambda: runtime)
    session = SimpleNamespace(view_role="detached_chat", viewed_session_id="own", active=None)
    socket = SimpleNamespace(send_json=send)
    await handlers["kernel:inventory:get"](host, socket, session, {"request_id": "inventory"})
    assert [row["chat_id"] for row in messages[-1]["items"]] == ["own"]
    await handlers["kernel:release"](host, socket, session, {"request_id": "close", "chat_id": "other"})
    assert not messages[-1]["ok"] and not releases
