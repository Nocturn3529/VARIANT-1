"""Goal tools and child outcomes through the real Python proxy and capability broker."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat_goals import ChatGoalService, ChatGoalStore
from run_context import Variant1RunContext, bind_run_context
from session_catalog.children import register_children_tool
from tests.test_chat_goal_usage import request, services
from tests.test_child_sessions import _Host, _manager
from tests.test_mutation import _enable_mutation
from tests.test_session_catalog import _api_method_aliases
from tests.test_session_catalog import catalog_stack  # noqa: F401 (fixture)
from work_fabric.capabilities import register_work_fabric_tools


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
async def test_stale_cell_cannot_finish_a_new_objective_and_keeps_its_namespace(catalog_stack, tmp_path):
    from tests.test_chat_goals import Host

    _registry, _enabled, runtimes, _artifacts, _broker, catalog, kernel = catalog_stack
    host = Host()
    host.runtime.session_runtimes = runtimes
    goals = ChatGoalService(host, ChatGoalStore(str(tmp_path / "goals.sqlite")))
    host.runtime.goals = goals
    catalog.host = host
    runtimes.ensure_runtime("owner", is_new=True)
    context = Variant1RunContext.create(source="chat", session_id="owner", work_scope={"chat_id": "owner"})
    admission = runtimes.try_reserve_run("owner")
    runtimes.begin_run(admission, run_id=context.run_id, thread_id=context.run_id)
    goal = goals.tool_create("owner", "Objective A")["goal"]
    try:
        goals.admit_chat_turn(context, "owner", admission, "chat")
        async def cell(call_id, code):
            return await kernel.execute(chat_id="owner", run_id=context.run_id, outer_tool_call_id=call_id,
                                        work_scope=context.work_scope.to_dict(), code=code)
        with bind_run_context(context):
            assert "Objective A" in goals.take_notes("owner", goal["goal_id"])[0]
            assert (await cell("setup", "kept = [42]")).ok
            await goals.control("owner", goal["goal_id"], "edit", objective="Objective B")
            denied = await cell("old", "session.get_goal(); session.update_goal(status='complete')")
            assert not denied.ok and "goal objective changed" in denied.output.text()
            assert goals.current("owner")["status"] == "active"
            assert "Objective B" in goals.take_notes("owner", goal["goal_id"])[0]
            done = await cell("new", "assert kept == [42]; print(session.update_goal(status='complete')['goal']['status'])")
            assert done.ok and "complete" in done.output.text()
    finally:
        runtimes.finish_run(admission, status="finished")
        await kernel.shutdown()


@pytest.mark.asyncio
async def test_structured_child_outcome_traverses_real_python_proxy_and_broker(catalog_stack, tmp_path, monkeypatch):
    _registry, _enabled, runtimes, artifacts, broker, catalog, kernel = catalog_stack
    host = _Host(tmp_path / "work-host.sqlite3")
    host.require_runtime().session_runtimes = runtimes
    children = _manager(runtimes.repository.path, host, artifacts)
    monkeypatch.setattr(children, "_enqueue", AsyncMock(return_value="held"))
    catalog.children = children
    child = await children.spawn("parent", task="Verify the result")
    sid = child["child_chat_id"]
    run_id = "native-child-contract"
    catalog.select(sid, "build")

    def assert_disclosed(chat_id, expected):
        document, _ = catalog.namespace_document(chat_id, query="session.report_outcome")
        assert ("report_outcome" in _api_method_aliases(document, "session")) is expected
        index = next(row for row in document["catalog_index"] if row["qualified_alias"] == "session")
        assert any(method["alias"] == "report_outcome" for method in index["methods"]) is expected
        matches = [method["alias"] for hit in document["top_k"] if hit["namespace"] == "session"
                   for method in hit.get("matched_methods", ())]
        assert ("report_outcome" in matches) is expected
        prompt = catalog.runtime_prompt(chat_id, "reply to the requesting peer", allow_auto_mount=False)
        assert ("session.report_outcome" in prompt) is expected
        release = catalog.repository.load(document["catalog_release_id"])
        assert ("session.report_outcome" in catalog.mutation._proxy_contracts(release, chat_id)) is expected

    try:
        assert_disclosed(sid, False)
        queued = await kernel.execute(chat_id=sid, run_id="queued-view", outer_tool_call_id="queued-view",
            code="marker = object()\nassert 'report_outcome' not in session.methods() and not hasattr(session, 'report_outcome')")
        assert queued.ok, queued.to_dict()
        with children._connect() as conn:
            conn.execute("UPDATE astb_child_handle SET status='running',outcome_run_id=? WHERE child_id=?",
                         (run_id, child["child_id"]))
        assert_disclosed(sid, True)
        assert_disclosed("parent", False)
        assert_disclosed("childchat_peer_with_a_child_looking_name", False)
        peer = await kernel.execute(
            chat_id="childchat_peer_with_a_child_looking_name", run_id="peer", outer_tool_call_id="peer-view",
            code="assert not hasattr(session, 'report_outcome')\n"
                 "assert 'report_outcome' not in repr(toolbelt.search('session.report_outcome'))",
        )
        assert peer.ok, peer.to_dict()
        runtimes.ensure_worker_runtime("scheduled", identity=catalog.identity(environment_digest="test"), source="automation")
        assert_disclosed("scheduled", False)
        result = await kernel.execute(
            chat_id=sid, run_id=run_id, outer_tool_call_id="outcome-cell",
            work_scope={"chat_id": sid, "goal_id": "parent-goal"},
            code='assert marker is not None\nsaved_report = session.report_outcome\nreceipt = saved_report(status="completed", summary="Verified output", evidence_refs=[])\nprint(receipt["status"])')
        assert result.ok, result.to_dict()
        assert "completed" in result.output.text()
        assert children.inspect("parent", child["child_id"])["outcome"]["run_id"] == run_id
        stale = await kernel.execute(chat_id=sid, run_id="unbound", outer_tool_call_id="stale",
                                     code="saved_report(status='completed', summary='Verified output')")
        assert not stale.ok and "stale child run" in stale.error_message
        with children._connect() as conn:
            conn.execute("UPDATE astb_child_handle SET status='completed' WHERE child_id=?", (child["child_id"],))
        assert_disclosed(sid, False)
        finished = await kernel.execute(chat_id=sid, run_id=run_id, outer_tool_call_id="finished-view",
            code="assert marker is not None and 'report_outcome' not in session.documentation()['methods']")
        assert finished.ok and finished.generation == queued.generation, finished.to_dict()
    finally:
        await kernel.shutdown()
        await children.work.shutdown()


@pytest.mark.asyncio
async def test_pinned_v5_owner_keeps_active_mutation_and_can_resume_goal(catalog_stack, tmp_path):
    registry, _enabled, runtimes, _artifacts, _broker, catalog, kernel = catalog_stack
    goals = ChatGoalService(SimpleNamespace(), ChatGoalStore(str(tmp_path / "goals.sqlite")))
    catalog.host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(goals=goals))
    tool = registry.get("session")
    current = (tool.schema_revision, tool.handler_revision, tool.object_methods)
    goal_methods = {"get_goal", "create_goal", "update_goal"}
    tool.schema_revision, tool.handler_revision = "variant1.session.v5", "variant1.session-handler.v6"
    tool.object_methods = tuple(method for method in tool.object_methods if method["name"] not in goal_methods)
    old_release = catalog.reconcile_registry()
    _enable_mutation(runtimes, "owner")
    catalog.select("owner", "build")
    try:
        await catalog.mutation.synthesize(
            "owner", slot="build/8", alias="saved_owner", purpose="Get the current chat from the session.",
            schema={"type": "object", "properties": {}},
            source="def run(arguments):\n    return session.status()['chat_id']",
        )
        activation = catalog.mutation.status("owner")["active"]
        before = await kernel.execute(chat_id="owner", run_id="before-upgrade", outer_tool_call_id="old",
                                      code="print(tools.saved_owner())")
        assert before.ok and "owner" in before.output.text(), before.to_dict()
        old_receipt = next(r for r in _broker.receipts(limit=20)
                           if r.capability["capability_id"] == "session")
        tool.schema_revision, tool.handler_revision, tool.object_methods = current
        new_release = catalog.reconcile_registry()
        assert new_release != old_release
        document, refs = catalog.namespace_document("owner")
        assert document["catalog_release_id"] == old_release
        assert goal_methods <= _api_method_aliases(document, "session")
        assert "report_outcome" not in _api_method_aliases(document, "session")
        session_index = next(row for row in document["catalog_index"] if row["qualified_alias"] == "session")
        indexed = {method["alias"] for method in session_index["methods"]}
        assert goal_methods <= indexed and "report_outcome" not in indexed
        assert session_index["slot_id"] == f"{new_release}/base/api-session"
        assert any(ref.catalog_release_id == new_release and ref.capability_id == "session" for ref in refs.values())
        assert old_receipt.capability["catalog_release_id"] == old_release
        assert all(ref.opaque_id != old_receipt.capability["ref_id"] for ref in refs.values())
        goal, _ = goals.store.create("owner", "Resume carried-over work")
        goals.pause_for_stop("owner")
        await kernel.restart("owner")
        paused = await kernel.execute(chat_id="owner", run_id="inspect-upgrade", outer_tool_call_id="read",
                                      code="print(session.get_goal()['goal']['status'], tools.saved_owner())")
        assert paused.ok and "paused owner" in paused.output.text(), paused.to_dict()
        goals.store.transition(goal.goal_id, "active", from_statuses={"paused"})
        done = await kernel.execute(chat_id="owner", run_id="finish-upgrade", outer_tool_call_id="done",
                                    work_scope={"chat_id": "owner", "goal_id": goal.goal_id},
                                    code="print(session.update_goal(status='complete')['goal']['status'])")
        assert done.ok and "complete" in done.output.text(), done.to_dict()
        assert catalog.mutation.status("owner")["active"] == activation
        assert runtimes.runtime("owner").identity.catalog_release_id == old_release
    finally:
        tool.schema_revision, tool.handler_revision, tool.object_methods = current
        await kernel.shutdown()


@pytest.mark.parametrize("marker", ["child:parent", "worker:review"])
@pytest.mark.asyncio
async def test_hidden_runtime_rejects_goal_tools_even_with_parent_scope(catalog_stack, tmp_path, marker):
    _registry, _enabled, runtimes, _artifacts, _broker, catalog, kernel = catalog_stack
    goals = ChatGoalService(SimpleNamespace(), ChatGoalStore(str(tmp_path / "goals.sqlite")))
    catalog.host = SimpleNamespace(require_runtime=lambda: SimpleNamespace(goals=goals))
    identity = catalog.identity(environment_digest="test")
    runtimes.repository.ensure_runtime("hidden", identity, creation_saga_state=marker)
    parent, _ = goals.store.create("parent", "Owner work")
    try:
        document, _ = catalog.namespace_document("hidden")
        assert {"get_goal", "create_goal", "update_goal"}.isdisjoint(_api_method_aliases(document, "session"))
        result = await kernel.execute(chat_id="hidden", run_id="child-run", outer_tool_call_id="child",
                                      work_scope={"chat_id": "hidden", "goal_id": parent.goal_id},
                                      code="session.create_goal('Unexpected child-local Goal')")
        assert not result.ok
        assert goals.store.current("hidden") is None
        assert goals.store.get(parent.goal_id).status == "active"
    finally:
        await kernel.shutdown()


@pytest.mark.asyncio
async def test_goal_created_in_cell_reaches_its_child_and_next_physical_request(catalog_stack, tmp_path, monkeypatch):
    registry, _enabled, runtimes, artifacts, broker, catalog, kernel = catalog_stack
    goals, router = services(tmp_path)
    host = _Host(tmp_path / "children.sqlite")
    children = _manager(str(tmp_path / "children.sqlite"), host, artifacts)
    monkeypatch.setattr(children, "_enqueue", AsyncMock(return_value="held"))
    host.require_runtime().broker = broker
    host.require_runtime().registry = registry
    host.require_runtime().session_artifacts = artifacts
    host.require_runtime().goals = goals
    catalog.host = host
    register_work_fabric_tools(host)
    registry.remove("children")
    register_children_tool(registry, children)
    catalog.select("owner", "operate")
    context = Variant1RunContext.create(source="chat", session_id="owner", work_scope={"chat_id": "owner"})
    admission = runtimes.try_reserve_run("owner")
    runtimes.begin_run(admission, run_id=context.run_id, thread_id=context.run_id)
    try:
        goals.admit_chat_turn(context, "owner", admission, "chat")
        assert (await request(router, context)).goal_usage is None
        with bind_run_context(context):
            result = await kernel.execute(chat_id="owner", run_id=context.run_id, outer_tool_call_id="delegate",
                work_scope=context.work_scope.to_dict(), code="""
created = session.create_goal("Investigate with a child")
worker = children.spawn(task="Check the input")
""")
        assert result.ok, result.to_dict()
        goal = goals.store.current("owner")
        child = children.list("owner")[0]
        assert child["work_scope"]["goal_id"] == goal.goal_id
        assert child["work_scope"]["chat_id"] == child["child_chat_id"]
        assert (await request(router, context)).goal_usage.goal_id == goal.goal_id
    finally:
        runtimes.finish_run(admission, status="ok")
        await kernel.shutdown()
        await children.work.shutdown()
