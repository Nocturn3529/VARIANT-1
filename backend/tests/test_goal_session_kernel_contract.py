from unittest.mock import AsyncMock
import pytest
from tests.test_session_catalog import catalog_stack
from tests.test_child_sessions import _Host,_manager
from artifacts import ContentAddressedArtifactStore

@pytest.mark.asyncio
async def test_parent_goal_outcome_traverses_real_python_proxy_and_broker(catalog_stack,tmp_path):
    from types import SimpleNamespace
    from tests.test_goal_parent_session import stack, launch, finish
    _registry,_enabled,_runtimes,_artifacts,broker,catalog,kernel=catalog_stack
    work,goals,parent,registry,gate,runs,_=stack(tmp_path/'parent')
    goal_id=await launch(goals,runs)
    run=runs[0][0]
    catalog.host=SimpleNamespace(require_runtime=lambda:SimpleNamespace(goals=goals))
    _runtimes.ensure_runtime('owner',is_new=True)
    try:
        result=await kernel.execute(chat_id='owner',run_id=run.run_id,outer_tool_call_id='parent-report',
            work_scope=run.work_scope.to_dict(),
            code='receipt = session.report_outcome(status="completed", summary="Verified parent output", evidence_refs=[])\nprint(receipt["status"])')
        assert result.ok,result.to_dict()
        assert 'completed' in result.output.text()
        assert (await finish(goals,goal_id,gate,registry))['status']=='succeeded'
        assert any(r.capability['capability_id']=='session' for r in broker.receipts(limit=10))
    finally:
        if registry.is_busy('owner'):
            await finish(goals,goal_id,gate,registry)
        await kernel.shutdown();await work.shutdown()

@pytest.mark.asyncio
async def test_structured_child_outcome_traverses_real_python_proxy_and_broker(catalog_stack,tmp_path,monkeypatch):
    _registry,_enabled,runtimes,_artifacts,broker,catalog,kernel=catalog_stack
    host=_Host(tmp_path/'child-store.sqlite3')
    children=_manager(str(tmp_path/'child-store.sqlite3'),host,ContentAddressedArtifactStore(str(tmp_path/'child-cas')))
    monkeypatch.setattr(children,'_enqueue',AsyncMock(return_value='held'))
    child=await children.spawn('parent',task='Verify the result')
    sid=child['child_chat_id'];run_id='native-goal-contract'
    with children._connect() as conn:
        conn.execute("UPDATE astb_child_handle SET status='running',outcome_run_id=? WHERE child_id=?",(run_id,child['child_id']))
    catalog.children=children
    runtimes.ensure_runtime(sid,is_new=True);catalog.select(sid,'build')
    try:
        result=await kernel.execute(chat_id=sid,run_id=run_id,outer_tool_call_id='outcome-cell',
            code='receipt = session.report_outcome(status="completed", summary="Verified output", evidence_refs=[])\nprint(receipt["status"])')
        assert result.ok,result.to_dict()
        assert 'completed' in result.output.text()
        assert children.inspect('parent',child['child_id'])['outcome']['run_id']==run_id
        assert any(r.capability['capability_id']=='session' for r in broker.receipts(limit=10))
    finally:
        await kernel.shutdown();await children.work.shutdown()

@pytest.mark.asyncio
async def test_supported_thread_context_path_keeps_capabilities_admitted(catalog_stack):
    _registry,_enabled,runtimes,_artifacts,_broker,catalog,kernel=catalog_stack
    sid='thread-context';runtimes.ensure_runtime(sid,is_new=True);catalog.select(sid,'build')
    try:
        result=await kernel.execute(chat_id=sid,run_id='thread-proof',outer_tool_call_id='threads',code='''
import asyncio
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=1) as pool:
    try:
        pool.submit(session.status).result()
    except Exception as exc:
        print(str(exc))
scoped = await asyncio.to_thread(session.status)
print("PRESERVED=" + scoped["chat_id"])
''')
        assert result.ok,result.to_dict()
        assert 'ThreadPoolExecutor' in result.output.text()
        assert 'PRESERVED=thread-context' in result.output.text()
    finally:await kernel.shutdown()
