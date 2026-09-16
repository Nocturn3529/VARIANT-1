"""Explicit Stop survives scheduler progress without weakening other CAS operations."""
import asyncio
import hashlib

import pytest

from goals import create_goal_service
from goals.models import GoalConflict
from work_fabric.service import WorkService


@pytest.fixture
def service(tmp_path):
    return create_goal_service(WorkService.open(str(tmp_path / 'work.sqlite3'), worker_id='cancel-intent'),
                               register_work_handler=False)


def progressed(service):
    goal = service.create(title='work', objective='owned work', owner_chat_id='chat-a')
    current = service.state_set(goal.goal_id, 'progress', {'count': 1}, expected_version=goal.version)
    return goal, current


@pytest.mark.asyncio
async def test_stop_accepts_progress_and_replay_does_not_repeat_cleanup(service):
    old, current = progressed(service)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def cleanup(goal, reason):
        calls.append(goal.goal_id)
        entered.set()
        await release.wait()
        return {'complete': True, 'proof': 'owned resources settled'}
    service.register_cancellation_handler(cleanup)
    args = dict(owner_chat_id='chat-a', observed_version=old.version, correlation_id='stop-1')
    first = asyncio.create_task(service.request_cancel_async(old.goal_id, **args))
    await entered.wait()
    second = asyncio.create_task(service.request_cancel_async(old.goal_id, **args))
    release.set()
    a, b = await asyncio.gather(first, second)
    assert a.status == b.status == 'cancelled'
    assert calls == [old.goal_id]
    assert service.state_get(old.goal_id, 'resource_cleanup')['complete'] is True
    intent = service.state_get(old.goal_id, 'cancel_request:' + hashlib.sha256(b'stop-1').hexdigest())
    assert intent['observed_version'] == old.version
    assert intent['applied_to_version'] == current.version
    before = service.get(old.goal_id).version
    await service.request_cancel_async(old.goal_id, **args)
    assert service.get(old.goal_id).version == before and len(calls) == 1
    with pytest.raises(GoalConflict, match='different content'):
        await service.request_cancel_async(old.goal_id, **args, reason='different request')


@pytest.mark.asyncio
async def test_stop_rejects_wrong_owner_future_version_and_preserves_other_fences(service):
    old, current = progressed(service)
    for owner, version in [('chat-b', old.version), ('chat-a', current.version + 1), ('chat-a', 0), ('chat-a', True)]:
        with pytest.raises(GoalConflict):
            await service.request_cancel_async(old.goal_id, owner_chat_id=owner,
                observed_version=version, correlation_id='bad')
        assert service.get(old.goal_id).version == current.version
        assert service.state_get(old.goal_id, 'resource_cleanup') is None
    with pytest.raises(GoalConflict):
        service.pause(old.goal_id, expected_version=old.version, reason='stale')
    with pytest.raises(GoalConflict):
        await service.cancel_async(old.goal_id, expected_version=old.version)


@pytest.mark.asyncio
async def test_retry_after_admission_before_cleanup_recovers_same_intent(service):
    old, _ = progressed(service)
    admitted = service.repository.cancel_goal(old.goal_id, owner_chat_id='chat-a',
        expected_version=old.version, correlation_id='durable-stop')
    token = service.state_get(old.goal_id, 'resource_cleanup')['token']
    assert admitted.status == 'cancelled'
    service.register_cancellation_handler(lambda *_: {'complete': True})
    await service.request_cancel_async(old.goal_id, owner_chat_id='chat-a',
        observed_version=old.version, correlation_id='durable-stop')
    receipt = service.state_get(old.goal_id, 'resource_cleanup')
    assert receipt['token'] == token and receipt['complete'] is True


@pytest.mark.asyncio
async def test_stop_after_terminal_progress_cleans_resources_without_rewriting_outcome(service):
    old, _ = progressed(service)
    goal = service.get(old.goal_id)
    goal = service.repository.transition_goal(goal.goal_id, 'cancelled', expected_version=goal.version)
    goal = service.repository.transition_goal(goal.goal_id, 'archived', expected_version=goal.version)
    service.register_cancellation_handler(lambda *_: {'complete': True})
    result = await service.request_cancel_async(goal.goal_id, owner_chat_id='chat-a',
        observed_version=old.version, correlation_id='terminal-stop')
    assert result.status == 'archived'
    assert service.state_get(goal.goal_id, 'resource_cleanup')['complete'] is True


@pytest.mark.asyncio
async def test_native_cancel_routes_stale_observation_to_owned_intent(service):
    from types import SimpleNamespace
    from ws_goals import register
    handlers = {}
    def on(*names):
        def bind(fn):
            handlers.update({name: fn for name in names})
            return fn
        return bind
    register(on)
    old, current = progressed(service)
    service.register_cancellation_handler(lambda *_: {'complete': True})
    runtime = SimpleNamespace(goals=service, sessions=SimpleNamespace(get_session=lambda _: {}))
    host = SimpleNamespace(require_runtime=lambda: runtime)
    replies = []
    class Socket:
        async def send_json(self, value): replies.append(value)
    message = {'type':'goal:cancel', 'session_id':'chat-a', 'goal_id':old.goal_id,
               'expected_version':old.version, 'request_id':'native-stop'}
    await handlers['goal:cancel'](host, Socket(), SimpleNamespace(viewed_session_id='other-chat'), message)
    assert replies[-1]['type'] == 'goal:accepted'
    assert replies[-1]['session_id'] == 'chat-a' and replies[-1]['request_id'] == 'native-stop'
    assert replies[-1]['result']['status'] == 'cancelled'
    assert service.get(old.goal_id).version > current.version
