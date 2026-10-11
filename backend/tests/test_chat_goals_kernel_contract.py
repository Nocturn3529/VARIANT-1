"""Goal tools and child outcomes through the real Python proxy and capability broker."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from artifacts import ContentAddressedArtifactStore
from chat_goals import ChatGoalService, ChatGoalStore
from tests.test_child_sessions import _Host, _manager
from tests.test_session_catalog import catalog_stack  # noqa: F401 (fixture)


@pytest.mark.asyncio
async def test_goal_tools_traverse_real_python_proxy_and_broker(catalog_stack, tmp_path):
    _registry, _enabled, runtimes, _artifacts, broker, catalog, kernel = catalog_stack
    service = ChatGoalService(SimpleNamespace(), ChatGoalStore(str(tmp_path / "goals.sqlite3")))
    catalog.host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(goals=service))
    runtimes.ensure_runtime("owner", is_new=True)
    try:
        result = await kernel.execute(chat_id="owner", run_id="goal-run", outer_tool_call_id="goal-cell", code='''
created = session.create_goal(objective="Write the report", token_budget=5000)
print("CREATED=" + created["goal"]["status"])
print("READ=" + session.get_goal()["goal"]["objective"])
done = session.update_goal(status="complete")
print("DONE=" + done["goal"]["status"])
''')
        assert result.ok, result.to_dict()
        text = result.output.text()
        assert "CREATED=active" in text and "READ=Write the report" in text and "DONE=complete" in text
        goal = service.store.current("owner")
        assert goal.status == "complete" and goal.token_budget == 5000
        assert goal.completed_run_id == "goal-run"
        assert any(r.capability["capability_id"] == "session" for r in broker.receipts(limit=10))
    finally:
        await kernel.shutdown()


@pytest.mark.asyncio
async def test_structured_child_outcome_traverses_real_python_proxy_and_broker(catalog_stack, tmp_path, monkeypatch):
    _registry, _enabled, runtimes, _artifacts, broker, catalog, kernel = catalog_stack
    host = _Host(tmp_path / "child-store.sqlite3")
    children = _manager(str(tmp_path / "child-store.sqlite3"), host,
                        ContentAddressedArtifactStore(str(tmp_path / "child-cas")))
    monkeypatch.setattr(children, "_enqueue", AsyncMock(return_value="held"))
    child = await children.spawn("parent", task="Verify the result")
    sid = child["child_chat_id"]
    run_id = "native-child-contract"
    with children._connect() as conn:
        conn.execute("UPDATE astb_child_handle SET status='running',outcome_run_id=? WHERE child_id=?",
                     (run_id, child["child_id"]))
    catalog.children = children
    runtimes.ensure_runtime(sid, is_new=True)
    catalog.select(sid, "build")
    try:
        result = await kernel.execute(
            chat_id=sid, run_id=run_id, outer_tool_call_id="outcome-cell",
            work_scope={"chat_id": sid, "goal_id": "parent-goal"},
            code='receipt = session.report_outcome(status="completed", summary="Verified output", evidence_refs=[])\nprint(receipt["status"])')
        assert result.ok, result.to_dict()
        assert "completed" in result.output.text()
        assert children.inspect("parent", child["child_id"])["outcome"]["run_id"] == run_id
    finally:
        await kernel.shutdown()
        await children.work.shutdown()
