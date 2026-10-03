import sys
from types import SimpleNamespace
import pytest

from artifacts import ContentAddressedArtifactStore
from execution_hosts import ExecutionOwner, create_execution_runtime
from goals import create_goal_service
from goals.host_handlers import build_goal_host_handlers
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService


@pytest.mark.asyncio
async def test_parent_goal_cleanup_stops_only_managed_goal_scope_and_retains_parent_kernel(tmp_path):
    artifacts=ContentAddressedArtifactStore(str(tmp_path/'artifacts'))
    execution=create_execution_runtime(data_dir=str(tmp_path/'execution'),artifact_store=artifacts,backend_instance_id='parent-resource-test')
    work=WorkService.open(str(tmp_path/'work.sqlite3'))
    goals=create_goal_service(work,register_work_handler=False)
    runtime=SimpleNamespace(execution=execution,kernel=SimpleNamespace(),
        catalog=SimpleNamespace(children=SimpleNamespace(goal_roots=lambda *args:[])))
    host=SimpleNamespace(require_runtime=lambda:runtime)
    adapters=build_goal_host_handlers(host,goals)
    goals.register_cancellation_handler(adapters.cancel_goal_resources)
    goal=goals.create(title='Owned process',objective='Work',owner_chat_id='owner',
        completion_policy={'entrypoint':'composer_goal','execution_owner':'parent'})
    command=[sys.executable,'-u','-c','import time; time.sleep(60)']
    owned=await execution.start_process(command,owner=ExecutionOwner('chat','owner',WorkScope(chat_id='owner',goal_id=goal.goal_id)),cwd=str(tmp_path))
    unrelated=await execution.start_process(command,owner=ExecutionOwner('chat','owner',WorkScope(chat_id='owner')),cwd=str(tmp_path))
    try:
        ended=await goals.cancel_async(goal.goal_id,expected_version=goals.get(goal.goal_id).version)
        assert ended.status=='cancelled'
        assert not execution.processes.get(owned.process_id).live
        assert execution.processes.get(unrelated.process_id).live
        cleanup=goals.repository.state_get(goal.goal_id,'resource_cleanup')
        assert cleanup['complete'] is True
        assert cleanup['closed_kernel_chat_ids']==[]
        assert cleanup['stopped_process_ids']==[owned.process_id]
    finally:
        execution.shutdown();await work.shutdown()
