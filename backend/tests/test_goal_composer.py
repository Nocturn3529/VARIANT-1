import asyncio
from types import SimpleNamespace

import pytest

from goals import create_goal_service
from goals.executor import StepExecutionResult
from goals.models import GoalConflict
from work_fabric.service import WorkService
from ws_goals import register


def stack(tmp_path):
    work=WorkService.open(str(tmp_path/'work.sqlite3'),worker_id='composer-test')
    async def agent(context): return StepExecutionResult()
    return create_goal_service(work,handlers={'agent':agent})


@pytest.mark.asyncio
async def test_submission_is_durable_idempotent_and_owner_scoped(tmp_path):
    service=stack(tmp_path)
    a,b=await asyncio.gather(service.composer.submit('chat-a','r1','Build a report'),
                             service.composer.submit('chat-a','r1','Build a report'))
    assert a['goal']['goal_id']==b['goal']['goal_id']
    assert len(service.list(owner_chat_id='chat-a'))==1
    assert len(a['steps'])==1 and a['steps'][0]['kind']=='agent'
    assert a['completion_basis']=='structured_child_report' and a['capabilities']['pause_active_work'] is False
    assert service.composer.current('chat-b','r1') is None
    assert service.composer.current('chat-a','r1')['goal']['goal_id']==a['goal']['goal_id']
    with pytest.raises(GoalConflict):await service.composer.submit('chat-a','r1','Different objective')
    with pytest.raises(GoalConflict):await service.composer.submit('chat-a','r2','Another goal')
    await service.cancel_async(a['goal']['goal_id'],expected_version=service.get(a['goal']['goal_id']).version)
    fresh=await service.composer.submit('chat-a','r2','Another goal')
    assert service.composer.current('chat-a')['goal']['goal_id']==fresh['goal']['goal_id']
    assert service.composer.current('chat-a','r1')['goal']['goal_id']==a['goal']['goal_id']
    assert fresh['steps'][0]['step_id']!=a['steps'][0]['step_id']


@pytest.mark.asyncio
async def test_retry_after_queued_before_enqueue_recovers_same_goal(tmp_path,monkeypatch):
    service=stack(tmp_path);enqueue=service.supervisor.enqueue
    monkeypatch.setattr(service.supervisor,'enqueue',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('lost before enqueue')))
    with pytest.raises(RuntimeError):await service.composer.submit('chat','r1','Do work')
    stored=service.composer.current('chat','r1');assert stored['goal']['status']=='queued'
    monkeypatch.setattr(service.supervisor,'enqueue',enqueue)
    recovered=await service.composer.submit('chat','r1','Do work')
    assert recovered['goal']['goal_id']==stored['goal']['goal_id']


@pytest.mark.asyncio
async def test_wire_contract_uses_explicit_chat_and_correlation(tmp_path):
    service=stack(tmp_path);handlers={}
    def on(*names):
        def bind(fn):
            for name in names:handlers[name]=fn
            return fn
        return bind
    register(on)
    runtime=SimpleNamespace(goals=service,sessions=SimpleNamespace(get_session=lambda sid: {} if sid in {'a','b'} else None))
    host=SimpleNamespace(require_runtime=lambda:runtime)
    session=SimpleNamespace(viewed_session_id='b')
    replies=[]
    class Socket:
        async def send_json(self,value):replies.append(value)
    ws=Socket()
    await handlers['goal:submit'](host,ws,session,{'session_id':'a','request_id':'r','objective':'Build report'})
    accepted=replies[-1];assert accepted['type']=='goal:accepted' and accepted['session_id']=='a'
    assert accepted['result']['goal']['owner_chat_id']=='a'
    gid=accepted['result']['goal']['goal_id']
    await handlers['goal:get'](host,ws,session,{'session_id':'b','request_id':'bad','goal_id':gid})
    assert replies[-1]['type']=='goal:rejected' and replies[-1]['request_id']=='bad'
    await handlers['goal:current:get'](host,ws,session,{'session_id':'a','request_id':'get','submission_request_id':'r'})
    assert replies[-1]['type']=='goal:current' and replies[-1]['result']['goal']['goal_id']==gid
