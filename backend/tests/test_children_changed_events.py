import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from artifacts.store import ContentAddressedArtifactStore
from server_lifespan import start_critical_services
from tests.test_child_sessions import _Host, _manager, _runtimes
from tools import ToolError


async def _wait_for(events, count):
    for _ in range(200):
        if len(events) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} child events, got {len(events)}")


def _bind(manager):
    events = []

    async def publish(event):
        events.append(event)

    manager.bind_change_publisher(
        publish, loop=asyncio.get_running_loop()
    )
    return events


@pytest.mark.asyncio
async def test_committed_handle_message_outcome_and_usage_changes_publish(
    tmp_path, monkeypatch,
):
    host = _Host(tmp_path / "children.sqlite3")
    manager = _manager(
        str(tmp_path / "children.sqlite3"),
        host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    monkeypatch.setattr(manager, "_enqueue", AsyncMock(return_value="held"))
    events = _bind(manager)

    child = await manager.spawn("parent", task="inspect")
    await _wait_for(events, 1)
    assert events[-1] == {
        "type": "children:changed",
        "schema": "variant1.children-changed.v1",
        "session_id": "parent",
        "revision": manager.inspection_snapshot("parent")["revision"],
        "child_id": child["child_id"],
    }

    manager.send("parent", child["child_id"], "use this evidence")
    await _wait_for(events, 2)
    pending = manager._drain_messages(child["child_id"])
    assert len(pending) == 1
    assert manager._ack_messages(
        child["child_id"], [pending[0]["message_id"]]
    ) == 1
    await _wait_for(events, 3)
    assert events[0]["revision"] < events[1]["revision"] < events[2]["revision"]

    # Establish an active generation as test setup, then prove a sync outcome
    # write invoked from a worker thread reaches the server-owned loop.
    with manager._lock, manager._connect() as conn:
        conn.execute(
            "UPDATE astb_child_handle SET status='running',outcome_run_id='run-1' "
            "WHERE child_id=?",
            (child["child_id"],),
        )
    before_thread_event = len(events)
    outcome = await asyncio.to_thread(
        manager.report_outcome,
        child["child_chat_id"],
        "run-1",
        status="blocked",
        summary="Need evidence",
    )
    assert outcome["status"] == "blocked"
    await _wait_for(events, before_thread_event + 1)
    roster = manager.inspection_snapshot("parent")
    assert roster["children"][0]["outcome"]["status"] == "blocked"
    assert events[-1]["revision"] == roster["revision"]

    assert manager._finish_child(
        child["child_id"],
        status="completed",
        usage={
            "llm_calls": 1,
            "total_tokens": 9,
            "cost_usd": 0.0,
            "wall_time_s": 0.5,
        },
        completed=10.0,
        result_text="done",
        parent_message=True,
    )
    await _wait_for(events, before_thread_event + 3)
    terminal = manager.inspection_snapshot("parent", child_id=child["child_id"])
    projected = terminal["children"][0]
    assert projected["status"] == "completed"
    assert projected["usage"]["total_tokens"] == 9
    assert projected["usage_rollup_state"] == "complete"
    assert terminal["revision"] == events[-1]["revision"]
    assert all(event["session_id"] == "parent" for event in events)


@pytest.mark.asyncio
async def test_restart_rollback_is_silent_and_delete_retains_old_parent(
    tmp_path, monkeypatch,
):
    host = _Host(tmp_path / "children.sqlite3")
    manager = _manager(
        str(tmp_path / "children.sqlite3"),
        host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    monkeypatch.setattr(manager, "_enqueue", AsyncMock(return_value="held"))
    events = _bind(manager)

    target = await manager.spawn("rollback-parent", task="target")
    with manager._lock, manager._connect() as conn:
        conn.execute(
            "UPDATE astb_child_handle SET status='completed' WHERE child_id=?",
            (target["child_id"],),
        )
    blocker = await manager.spawn("rollback-parent", task="blocker")
    await _wait_for(events, 2)
    events.clear()
    host.router.cfg = {"subagents": {"max_admitted": 1}}
    revision_before = manager.inspection_snapshot("rollback-parent")["revision"]

    with pytest.raises(RuntimeError, match="admission-capacity"):
        await manager.restart(
            "rollback-parent",
            target["child_id"],
            expected_generation=1,
            request_id="rolled-back",
            message="must not persist",
        )
    await asyncio.sleep(0)
    unchanged = manager.inspect("rollback-parent", target["child_id"])
    assert unchanged["run_generation"] == 1
    assert unchanged["restart_request_id"] == ""
    assert all(
        row["text"] != "must not persist" for row in unchanged["messages"]
    )
    assert manager.inspection_snapshot("rollback-parent")["revision"] == revision_before
    assert events == []

    doomed = await manager.spawn("delete-parent", task="delete me")
    with manager._lock, manager._connect() as conn:
        conn.execute(
            "UPDATE astb_child_handle SET status='completed' WHERE child_id=?",
            (doomed["child_id"],),
        )
    await _wait_for(events, 1)
    events.clear()
    original_delete = _runtimes(host).delete_child_runtime

    async def fail_delete(_runtime_id, *, parent_chat_id):
        del parent_chat_id
        raise OSError("cleanup failed")

    monkeypatch.setattr(_runtimes(host), "delete_child_runtime", fail_delete)
    with pytest.raises(RuntimeError, match="cleanup remains incomplete"):
        await manager.delete_chat("delete-parent")
    await _wait_for(events, 2)
    failed = manager.inspection_snapshot(
        "delete-parent", child_id=doomed["child_id"]
    )["children"][0]
    assert failed["cleanup"] == {
        "status": "failed",
        "complete": False,
        "error": "OSError: cleanup failed",
    }
    assert {event["session_id"] for event in events} == {"delete-parent"}
    assert {event["child_id"] for event in events} == {doomed["child_id"]}

    monkeypatch.setattr(
        _runtimes(host), "delete_child_runtime", original_delete
    )
    events.clear()
    await manager.delete_chat("delete-parent")
    await _wait_for(events, 2)
    assert manager.inspection_snapshot("delete-parent")["children"] == []
    assert events[-1]["session_id"] == "delete-parent"
    assert events[-1]["child_id"] == doomed["child_id"]


@pytest.mark.asyncio
async def test_outcome_idempotency_does_not_publish_twice(tmp_path, monkeypatch):
    host = _Host(tmp_path / "children.sqlite3")
    manager = _manager(
        str(tmp_path / "children.sqlite3"),
        host,
        ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    monkeypatch.setattr(manager, "_enqueue", AsyncMock(return_value="held"))
    events = _bind(manager)
    child = await manager.spawn("parent", task="report")
    await _wait_for(events, 1)
    with manager._lock, manager._connect() as conn:
        conn.execute(
            "UPDATE astb_child_handle SET status='running',outcome_run_id='run' "
            "WHERE child_id=?",
            (child["child_id"],),
        )
    events.clear()

    first = manager.report_outcome(
        child["child_chat_id"], "run",
        status="continuing", summary="working",
    )
    await _wait_for(events, 1)
    second = manager.report_outcome(
        child["child_chat_id"], "run",
        status="continuing", summary="working",
    )
    await asyncio.sleep(0)
    assert first == second
    assert len(events) == 1
    with pytest.raises(ToolError, match="already reported"):
        manager.report_outcome(
            child["child_chat_id"], "run",
            status="completed", summary="different",
        )
    await asyncio.sleep(0)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_lifespan_binds_child_publisher_before_work_starts():
    order = []

    class Children:
        def bind_change_publisher(self, publisher, *, loop):
            assert callable(publisher)
            assert loop is asyncio.get_running_loop()
            order.append("children")

        async def reconcile_runtime_sagas(self):
            order.append("child-sagas")

    class SessionRuntimes:
        async def startup_reconcile(self, _sessions):
            order.append("sessions")
            return {
                "chats": 0,
                "tickets_requeued": 0,
                "tickets_completed": 0,
                "deletions_deferred": 0,
            }

    class Work:
        async def start(self):
            order.append("work")

    runtime = SimpleNamespace(
        catalog=SimpleNamespace(children=Children()),
        lifecycle=SimpleNamespace(
            dev_reset_on_launch=lambda: order.append("lifecycle")
        ),
        session_runtimes=SessionRuntimes(),
        sessions=object(),
        work=Work(),
    )
    hub = SimpleNamespace(broadcast=AsyncMock())
    host = SimpleNamespace(require_runtime=lambda: runtime, hub=hub)

    await start_critical_services(host)
    assert order == ["children", "lifecycle", "sessions", "child-sagas", "work"]
