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
