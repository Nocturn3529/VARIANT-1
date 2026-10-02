import asyncio
import base64
from contextlib import closing
import json
from types import SimpleNamespace

import pytest

from session_catalog.children import ChildSessionManager
from tools import ToolError, ToolRegistry


@pytest.fixture
def manager(tmp_path, monkeypatch):
    host = SimpleNamespace(router=SimpleNamespace(mode="local", cfg={}))
    result = ChildSessionManager(str(tmp_path / "children.sqlite3"), host, None)
    monkeypatch.setattr(result, "_rollup_usage", lambda child: True)
    return result


def add(manager, child="one", *, parent="parent", status="running", generation=1, deleted=""):
    with closing(manager._connect()) as conn, conn:
        conn.execute("INSERT INTO astb_child_handle(child_id,parent_chat_id,name,task_text,context_text,"
            "action_surface,status,created_at,updated_at,run_generation,deletion_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (child, parent, child, "task", "", "ipython", status, 1, 1, generation, deleted))


def finish(manager, child, text="DONE", *, status="completed"):
    manager._finish_child(child, status=status, usage={}, completed=2, result_text=text,
                          parent_message=status == "completed")


@pytest.mark.asyncio
async def test_shared_wait_returns_first_result_then_deduplicates_cursor(manager):
    add(manager, "one")
    add(manager, "two")
    initial = await manager.wait("parent", targets=["one", "two"], timeout_s=0)
    assert initial["updates"] == []
    waiting = asyncio.create_task(manager.wait("parent", targets=["one", "two"], timeout_s=2,
                                               after_cursor=initial["cursor"]))
    await asyncio.sleep(0)
    finish(manager, "two", "SECOND RESULT")
    first = await asyncio.wait_for(waiting, 1)
    assert [r["child_id"] for r in first["updates"]] == ["two"]
    assert not first["settled"] and first["updates"][0]["reported_text"] == "SECOND RESULT"
    next_wait = asyncio.create_task(manager.wait("parent", targets=["one", "two"], timeout_s=2,
                                                 after_cursor=first["cursor"]))
    await asyncio.sleep(0)
    assert not next_wait.done()
    finish(manager, "one", "FIRST RESULT")
    second = await asyncio.wait_for(next_wait, 1)
    assert [r["child_id"] for r in second["updates"]] == ["one"]
    final = await manager.wait("parent", targets=["one", "two"], after_cursor=second["cursor"])
    assert final["settled"] and final["updates"] == []
    assert all("reported_text" not in r for r in final["items"])
    assert manager.inspect("parent", "two")["reported_text"] == "SECOND RESULT"


@pytest.mark.asyncio
async def test_timeout_and_cancellation_never_cancel_child(manager):
    add(manager)
    result = await manager.wait("parent", timeout_s=.01)
    assert result["reason"] == "timeout" and result["items"][0]["status"] == "running"
    waiting = asyncio.create_task(manager.wait("parent", timeout_s=30))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert manager._wait_subscribers == {}
    assert manager.inspect("parent", "one")["status"] == "running"
    assert manager.capacity()["max_active"] == 1


@pytest.mark.asyncio
async def test_committed_change_wakes_from_worker_thread(manager):
    add(manager)
    waiting = asyncio.create_task(manager.wait("parent", timeout_s=2))
    await asyncio.sleep(0)
    await asyncio.to_thread(finish, manager, "one")
    assert (await asyncio.wait_for(waiting, 1))["updates"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_foreign_scope_and_stale_generation_are_rejected(manager):
    add(manager)
    add(manager, "foreign", parent="elsewhere")
    for targets in (["foreign"], [{"service":"children", "kind":"child", "id":"one", "generation":2}]):
        with pytest.raises(ToolError):
            await manager.wait("parent", targets=targets, timeout_s=0)
    result = await manager.wait("parent", targets=["one", {"service":"children", "kind":"child", "id":"one", "generation":1}], timeout_s=0)
    assert result["items"][0]["run_generation"] == 1
    with pytest.raises(ToolError, match="cursor"):
        await manager.wait("elsewhere", after_cursor=result["cursor"], timeout_s=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,deleted", [("interrupted", ""), ("failed", ""), ("cancelled", "deleted"), ("running", "failed")])
async def test_terminal_and_attention_are_distinct_retained_observations(manager, status, deleted):
    add(manager, status=status, deleted=deleted)
    result = await manager.wait("parent", targets=["one"], timeout_s=0)
    assert result["updates"][0]["status"] == status
    assert result["updates"][0]["deletion_state"] == deleted
    assert result["settled"]


@pytest.mark.asyncio
async def test_partial_roster_and_bounded_report_budget_are_explicit(manager):
    for n in range(21):
        add(manager, str(n))
        finish(manager, str(n), "x" * 3000)
    result = await manager.wait("parent", timeout_s=0)
    assert result["truncated"] and len(result["items"]) == 20
    assert result["roster_total"] == 21 and result["selected_count"] == 20
    assert sum(len(r["reported_text"]) for r in result["updates"]) <= 16000
    assert all(r["report_truncated"] for r in result["updates"])
    assert len(manager.inspect("parent", "0")["reported_text"]) == 3000
    empty = await manager.wait("parent", targets=[], timeout_s=0)
    assert empty["items"] == [] and empty["settled"]


@pytest.mark.asyncio
async def test_cursor_rejects_malformed_payloads_and_nonfinite_waits(manager):
    malformed = base64.urlsafe_b64encode(json.dumps([]).encode()).decode()
    for cursor in (malformed, "not-base64"):
        with pytest.raises(ToolError, match="cursor"):
            await manager.wait("parent", after_cursor=cursor, timeout_s=0)
    for timeout in (float("nan"), float("inf"), -1, 31, True):
        with pytest.raises(ToolError):
            await manager.wait("parent", timeout_s=timeout)


@pytest.mark.asyncio
async def test_mounted_seed_returns_bound_handles_without_repeating_reports(manager):
    import capability_broker
    from capability_broker import InvocationContext
    from session_catalog.children import register_children_tool
    from work_fabric.scope import WorkScope
    add(manager)
    finish(manager, "one", "exact child report")
    ref = SimpleNamespace(opaque_id="dispatch", handler_revision="dispatch-v1",
                          catalog_release_id="catalog", slot_id="slot", slot_version=1)
    manager.host.require_runtime = lambda: SimpleNamespace(broker=SimpleNamespace(ref_for_name=lambda *a, **kw: ref))
    registry = ToolRegistry()
    register_children_tool(registry, manager)
    context = InvocationContext(chat_id="parent", run_id="run", outer_tool_call_id="outer",
        cell_execution_id="cell", nested_call_id="nested", catalog_release_id="catalog",
        surface="ipython", work_scope=WorkScope(chat_id="parent"))
    token = capability_broker._CURRENT_INVOCATION.set(context)
    try:
        result = await registry.get("children").handler({"operation":"wait", "targets":["one"]})
        handle = result["items"][0]["handle"]["$variant1_handle"]
        assert handle["id"] == "one" and handle["generation"] == 1
        assert handle["metadata"]["reported_text"] == ""
        assert result["updates"][0]["reported_text"] == "exact child report"
        identity = {key: handle[key] for key in ("service", "kind", "id", "generation", "revision")}
        repeated = await registry.get("children").handler({"operation":"wait", "targets":[identity],
                                                           "after_cursor":result["cursor"]})
        assert repeated["updates"] == []
    finally:
        capability_broker._CURRENT_INVOCATION.reset(token)
