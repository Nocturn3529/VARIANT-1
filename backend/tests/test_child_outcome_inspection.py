import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from artifacts.store import ContentAddressedArtifactStore
from tests.test_child_sessions import _Host,_manager
from tools import ToolError
from session_catalog.children import _child_handle_router
from ws_children import register

@pytest.fixture
async def manager(tmp_path,monkeypatch):
    host=_Host(tmp_path/'children.sqlite3')
    m=_manager(str(tmp_path/'children.sqlite3'),host,ContentAddressedArtifactStore(str(tmp_path/'artifacts')))
    monkeypatch.setattr(m,'_enqueue',AsyncMock(return_value='held'))
    yield m
    await host.require_runtime().work.shutdown()

def running(manager,child,run='graph-run'):
    with manager._lock,manager._connect() as conn:
        conn.execute("UPDATE astb_child_handle SET status='running',outcome_run_id=? WHERE child_id=?",(run,child['child_id']))

@pytest.mark.asyncio
async def test_new_goal_catalog_does_not_rebase_parent(manager):
    parent=manager.runtimes.ensure_runtime('parent')
    old=parent.identity.catalog_release_id
    manager.host.require_runtime().catalog=SimpleNamespace(current_release_id='current-goal-catalog')
    child=await manager.spawn('parent',task='new goal',fresh_catalog=True)
    assert manager.runtimes.runtime(child['child_chat_id']).identity.catalog_release_id=='current-goal-catalog'
    assert manager.runtimes.runtime('parent').identity.catalog_release_id==old

@pytest.mark.asyncio
async def test_report_is_generation_fenced_idempotent_and_separate_from_execution(manager):
    child=await manager.spawn('parent',task='goal')
    running(manager,child)
    value=manager.report_outcome(child['child_chat_id'],'graph-run',status='blocked',summary='Need input')
    assert value['status']=='blocked' and value['independently_verified'] is False
    assert manager.inspect('parent',child['child_id'])['status']=='running'
    assert manager.report_outcome(child['child_chat_id'],'graph-run',status='blocked',summary='Need input')==value
    with pytest.raises(ToolError,match='already reported'):
        manager.report_outcome(child['child_chat_id'],'graph-run',status='completed',summary='Changed claim')
    with pytest.raises(ToolError,match='stale'):
        manager.report_outcome(child['child_chat_id'],'old-run',status='blocked',summary='Need input')
    with pytest.raises(ToolError):manager.report_outcome('parent','graph-run',status='completed',summary='No child')
    with manager._connect() as conn:conn.execute("UPDATE astb_child_handle SET status='completed' WHERE child_id=?",(child['child_id'],))
    new=await manager.restart('parent',child['child_id'],expected_generation=1,request_id='continue-1',message='Input supplied')
    again=await manager.restart('parent',child['child_id'],expected_generation=1,request_id='continue-1',message='Input supplied')
    assert new['run_generation']==again['run_generation']==2
    assert new['child_chat_id']==child['child_chat_id'] and new['outcome']['status']=='unreported'
    with pytest.raises(RuntimeError):await manager.restart('parent',child['child_id'],request_id='continue-1',message='Different')

@pytest.mark.asyncio
async def test_run_binding_updates_native_execution_identity_but_preserves_report(manager):
    child=await manager.spawn('parent',task='goal');running(manager,child,'outer-admission')
    manager.bind_outcome_run(child['child_id'],1,'native-worker-run')
    report=manager.report_outcome(child['child_chat_id'],'native-worker-run',status='completed',summary='Verified')
    manager.bind_outcome_run(child['child_id'],1,'recovered-native-run')
    assert manager.outcome_for_chat(child['child_chat_id'])==report
    with pytest.raises(ToolError):manager.bind_outcome_run(child['child_id'],2,'stale')

@pytest.mark.asyncio
async def test_roster_is_scoped_monotonic_and_reveals_real_lineage(manager):
    before=manager.inspection_snapshot('parent')
    a=await manager.spawn('parent',task='first')
    b=await manager.spawn(a['child_chat_id'],task='nested')
    await manager.spawn('unrelated',task='private')
    roster=manager.inspection_snapshot('parent')
    assert roster['revision']>before['revision'] and roster['total']==2
    assert next(c for c in roster['children'] if c['child_id']==b['child_id'])['parent_child_id']==a['child_id']
    assert manager.inspection_snapshot('parent',limit=1)['truncated'] is True
    assert manager.inspection_snapshot('unrelated',child_id=a['child_id'])['children']==[]

@pytest.mark.asyncio
async def test_normal_wait_timeout_returns_nonterminal_handle(manager,monkeypatch):
    from capability_broker import CapabilityBroker,InvocationContext
    from tools import ToolRegistry
    from work_fabric.scope import WorkScope
    from work_fabric.capabilities import register_work_fabric_tools
    from session_catalog.children import register_children_tool
    runtime=manager.host.require_runtime();runtime.registry=ToolRegistry()
    runtime.broker=CapabilityBroker(registry=runtime.registry,runtime_registry=runtime.session_runtimes,
        enabled_resolver=lambda:{t.name for t in runtime.registry.all()},artifact_store=manager.artifact_store)
    runtime.session_artifacts=manager.artifact_store;manager.host.remote_handle_routers={}
    register_work_fabric_tools(manager.host);register_children_tool(runtime.registry,manager)
    child=await manager.spawn('parent',task='wait')
    with manager._connect() as conn:conn.execute("UPDATE astb_child_handle SET work_job_id='job-wait' WHERE child_id=?",(child['child_id'],))
    monkeypatch.setattr(manager.work.jobs,'wait',AsyncMock(side_effect=TimeoutError('pending')))
    context=InvocationContext(chat_id='parent',run_id='run',outer_tool_call_id='outer',cell_execution_id='cell',
        nested_call_id='wait',catalog_release_id='release',work_scope=WorkScope(chat_id='parent'))
    result=await _child_handle_router(manager,context,
        {'service':'children','kind':'child','id':child['child_id'],'generation':1},'wait',{})
    assert result['$variant1_handle']['metadata']['terminal'] is False

@pytest.mark.asyncio
async def test_children_wire_rejects_cross_owner_detail(manager):
    child=await manager.spawn('owner',task='private')
    handlers={}
    def on(*names):
        def register_handler(fn):
            for name in names:handlers[name]=fn
            return fn
        return register_handler
    register(on)
    runtime=SimpleNamespace(sessions=SimpleNamespace(get_session=lambda sid:{}),catalog=SimpleNamespace(children=manager),work=manager.work)
    replies=[]
    class Socket:
        async def send_json(self,value):replies.append(value)
    await handlers['children:detail:get'](SimpleNamespace(require_runtime=lambda:runtime),Socket(),None,
        {'type':'children:detail:get','session_id':'other','request_id':'r','child_id':child['child_id']})
    assert replies[-1]['type']=='children:rejected' and replies[-1]['request_id']=='r'
