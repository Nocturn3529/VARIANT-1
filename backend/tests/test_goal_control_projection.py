from types import SimpleNamespace
import pytest
from tests.test_goal_composer import stack
from ws_goals import _with_reports
from goals.models import GoalTransitionError

@pytest.mark.asyncio
async def test_end_is_user_ended_and_archived_current_does_not_resurrect_older_goal(tmp_path):
    service=stack(tmp_path)
    old=await service.composer.submit('chat','old','Old objective')
    await service.cancel_async(old['goal']['goal_id'],expected_version=service.get(old['goal']['goal_id']).version)
    new=await service.composer.submit('chat','new','New objective')
    ended=await service.finish_async(new['goal']['goal_id'],expected_version=service.get(new['goal']['goal_id']).version)
    snap=service.composer.snapshot(ended.goal_id)
    assert ended.status=='archived' and snap['termination']['kind']=='user_finished'
    assert snap['objective_outcome']['status']=='unreported'
    assert snap['cleanup']['status']=='complete'
    assert service.composer.current('chat') is None
    assert service.composer.current('chat','old')['goal']['goal_id']==old['goal']['goal_id']
    await service.work.shutdown()

@pytest.mark.asyncio
async def test_failed_cleanup_cannot_be_archived_and_is_retryable(tmp_path):
    service=stack(tmp_path)
    async def fails(*args):return {'complete':False,'issues':[{'error':'still live'}]}
    service.register_cancellation_handler(fails)
    value=await service.composer.submit('chat','one','Task')
    goal=await service.cancel_async(value['goal']['goal_id'],expected_version=service.get(value['goal']['goal_id']).version)
    assert service.composer.snapshot(goal.goal_id)['capabilities']['retry_cleanup']
    with pytest.raises(GoalTransitionError):service.archive(goal.goal_id,expected_version=goal.version)
    async def succeeds(*args):return {'complete':True,'issues':[]}
    service.register_cancellation_handler(succeeds)
    archived=await service.archive_async(goal.goal_id,expected_version=goal.version)
    assert archived.status=='archived'
    await service.work.shutdown()

def test_reports_do_not_duplicate_same_child_after_continuation():
    child={'status':'completed','run_generation':2,'outcome':{'status':'completed'},'result_text':'Verified'}
    host=SimpleNamespace(require_runtime=lambda:SimpleNamespace(catalog=SimpleNamespace(children=SimpleNamespace(inspect=lambda *args:child))))
    snapshot={'goal':{'owner_chat_id':'parent'},'effects':[
        {'kind':'agent.spawn','step_id':'step','response':{'child_id':'child'}} for _ in range(3)]}
    projected=_with_reports(host,snapshot)
    assert len(projected['reports'])==1 and projected['reports'][0]['generation']==2
