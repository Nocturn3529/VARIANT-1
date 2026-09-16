"""Pause retains the admitted task and queue; Resume and Stop stay distinct."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.test_ws_dispatch_chat import _chat_stack
import ws_dispatch


def runtime(tmp_path):
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / 'pause.sqlite3')))
    registry.ensure_runtime('chat')
    admission = registry.try_reserve_run('chat')
    return registry, admission


@pytest.mark.asyncio
async def test_pause_resume_keeps_same_run_and_queued_messages(tmp_path):
    registry, admission = runtime(tmp_path)
    ticket = registry.enqueue_input('chat', 'new instruction', delivery='steer')
    requested = registry.set_run_paused('chat', True, expected_admission_id=admission)
    assert requested['state'] == 'pausing'
    reached = asyncio.Event()
    notices = []
    async def notify(payload):
        notices.append(payload)
        reached.set()
    task = asyncio.create_task(registry.wait_if_paused('chat', admission, notify))
    registry.bind_admission_task(admission, task)
    await asyncio.wait_for(reached.wait(), 2)
    assert not task.done()
    assert registry.snapshot('chat')['pause_state'] == 'paused'
    assert registry.queued_input_count('chat') == 1 and registry.is_busy('chat')
    assert registry.set_run_paused('chat', True)['pause_revision'] == notices[-1]['pause_revision']
    resumed = registry.set_run_paused('chat', False, expected_admission_id=admission)
    await asyncio.wait_for(task, 2)
    assert resumed['state'] == 'running' and resumed['pause_revision'] > notices[-1]['pause_revision']
    assert registry.active_admission('chat') == admission
    assert registry.claim_input('chat', 'steer', run_id='run')['id'] == ticket.ticket_id
    assert registry.claim_input('chat', 'steer', run_id='run') is None
    registry.finish_run(admission, status='complete')


@pytest.mark.asyncio
async def test_resume_then_pause_during_notification_reaches_new_paused_state(tmp_path):
    registry, admission = runtime(tmp_path)
    registry.set_run_paused('chat', True)
    ready = asyncio.Event()
    notices = []
    async def notify(payload):
        notices.append(payload)
        if len(notices) == 1:
            registry.set_run_paused('chat', False)
            registry.set_run_paused('chat', True)
        else:
            ready.set()
    task = asyncio.create_task(registry.wait_if_paused('chat', admission, notify))
    await asyncio.wait_for(ready.wait(), 2)
    assert len(notices) == 2 and notices[1]['pause_revision'] > notices[0]['pause_revision']
    assert registry.pause_snapshot('chat')['state'] == 'paused' and not task.done()
    registry.set_run_paused('chat', False)
    await asyncio.wait_for(task, 2)
    registry.finish_run(admission, status='complete')


@pytest.mark.asyncio
async def test_old_pause_waiter_and_stale_resume_cannot_control_replacement(tmp_path):
    registry, old = runtime(tmp_path)
    registry.set_run_paused('chat', True)
    ready = asyncio.Event()
    async def notify(_payload): ready.set()
    waiter = asyncio.create_task(registry.wait_if_paused('chat', old, notify))
    await asyncio.wait_for(ready.wait(), 2)
    registry.finish_run(old, status='cancelled')
    new = registry.try_reserve_run('chat')
    with pytest.raises(RuntimeError, match='stale_run'):
        registry.set_run_paused('chat', False, expected_admission_id=old)
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert registry.active_admission('chat') == new
    assert registry.pause_snapshot('chat')['state'] == 'running'
    registry.finish_run(new, status='complete')


@pytest.mark.asyncio
async def test_notification_failure_does_not_cancel_paused_task_or_other_chat(tmp_path):
    registry, admission = runtime(tmp_path)
    registry.ensure_runtime('other')
    other = registry.try_reserve_run('other')
    registry.set_run_paused('chat', True)
    failed = asyncio.Event()
    async def notify(_payload):
        failed.set()
        raise RuntimeError('closed observer')
    task = asyncio.create_task(registry.wait_if_paused('chat', admission, notify))
    await asyncio.wait_for(failed.wait(), 2)
    await asyncio.sleep(0)
    assert not task.done()
    await registry.wait_if_paused('other', other, AsyncMock())
    registry.set_run_paused('chat', False)
    await asyncio.wait_for(task, 2)
    registry.finish_run(admission, status='complete')
    registry.finish_run(other, status='complete')


@pytest.mark.asyncio
async def test_websocket_pause_resume_and_separate_stop(tmp_path):
    srv, registry, repository, connection, sid = _chat_stack(tmp_path, AsyncMock())
    admission = registry.try_reserve_run(sid, attachment_id=connection.attachment_id)
    connection.busy = True
    connection.active.runtime_chat_id = sid
    connection.active.runtime_admission_id = admission
    socket = AsyncMock()
    packet = {'session_id':sid, 'admission_id':admission, 'request_id':'pause-1'}
    await ws_dispatch.HANDLERS['chat:pause'](srv, socket, connection, {'type':'chat:pause', **packet})
    assert socket.send_json.await_args.args[0]['state'] == 'pausing'
    assert socket.send_json.await_args.args[0]['accepted'] is True
    srv.require_runtime().kernel.interrupt.assert_not_awaited()
    reached = asyncio.Event()
    async def notify(_payload): reached.set()
    task = asyncio.create_task(registry.wait_if_paused(sid, admission, notify))
    registry.bind_admission_task(admission, task)
    connection.active.turn_task = task
    await asyncio.wait_for(reached.wait(), 2)
    ticket = registry.enqueue_input(sid, 'preserve while paused', delivery='steer')
    assert repository.get_ticket(ticket.ticket_id).state == 'queued'
    assert not task.cancelled() and not connection.interrupt
    await ws_dispatch.HANDLERS['cancel'](srv, socket, connection, {'type':'cancel', **packet})
    assert task.cancelled()
    assert not registry.is_busy(sid)


@pytest.mark.asyncio
async def test_pause_rejects_stale_run_without_touching_current_admission(tmp_path):
    srv, registry, _repo, connection, sid = _chat_stack(tmp_path, AsyncMock())
    admission = registry.try_reserve_run(sid)
    socket = AsyncMock()
    await ws_dispatch.HANDLERS['chat:pause'](srv, socket, connection, {
        'type':'chat:pause', 'session_id':sid, 'admission_id':'stale', 'request_id':'r',
    })
    payload = socket.send_json.await_args.args[0]
    assert payload['accepted'] is False and payload['error'] == 'stale_run'
    assert payload['state'] == 'running' and payload['request_id'] == 'r'
    assert registry.active_admission(sid) == admission
    registry.finish_run(admission, status='complete')


@pytest.mark.asyncio
@pytest.mark.parametrize('pause_after', ['model', 'tools', 'final_model'])
async def test_real_engine_waits_at_boundary_without_replaying_steps(tmp_path, pause_after):
    from tests.test_agent_engine import FakePorts, run_main
    from assistant_turn import AssistantTurn
    from agent_types import ToolBatchResult

    registry, admission = runtime(tmp_path)
    reached = asyncio.Event()
    action = {'tool':'ipython','args':{'code':'print(1)'},'id':'once'}
    batch = ToolBatchResult(text='done', executed=True, outcomes=[{
        'tool':'ipython','call_id':'once','result':'done','model_result':'done',
        'ok':True,'executed':True,
    }])
    class PausablePorts(FakePorts):
        def build(self):
            ports = super().build()
            original_stream, original_actions = ports.loop.stream, ports.loop.run_actions
            async def stream(*args):
                result = await original_stream(*args)
                if (pause_after == 'model' and len(self.stream_messages)==1
                        or pause_after == 'final_model' and len(self.stream_messages)==2):
                    registry.set_run_paused('chat', True)
                return result
            async def actions(*args):
                result = await original_actions(*args)
                if pause_after == 'tools':
                    registry.set_run_paused('chat', True)
                return result
            async def notify(_payload): reached.set()
            ports.loop.stream, ports.loop.run_actions = stream, actions
            ports.loop.wait_if_paused = lambda: registry.wait_if_paused('chat', admission, notify)
            return ports
    fake = PausablePorts([AssistantTurn(tool_calls=(action,)), AssistantTurn(text='Finished')], [batch])
    task = asyncio.create_task(run_main(fake))
    registry.bind_admission_task(admission, task)
    try:
        await asyncio.wait_for(reached.wait(), 2)
        assert not task.done() and not fake.stopped
        assert len(fake.action_batches) == (0 if pause_after=='model' else 1)
        assert len(fake.stream_messages) == (2 if pause_after=='final_model' else 1)
        registry.set_run_paused('chat', False)
        result = await asyncio.wait_for(task, 2)
        assert result.loop_result.reply == 'Finished'
        assert len(fake.action_batches)==1 and len(fake.stream_messages)==2
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        registry.finish_run(admission, status='complete')


@pytest.mark.asyncio
async def test_steering_sent_while_paused_reaches_next_model_one_ticket_at_a_time(tmp_path):
    from tests.test_agent_engine import FakePorts, run_main
    from assistant_turn import AssistantTurn
    from agent_types import ToolBatchResult

    registry, admission = runtime(tmp_path)
    reached = asyncio.Event()
    action = {'tool':'ipython','args':{'code':'print(1)'},'id':'once'}
    batch = ToolBatchResult(text='done', executed=True, outcomes=[{
        'tool':'ipython','call_id':'once','result':'done','model_result':'done','ok':True,'executed':True,
    }])
    class PausablePorts(FakePorts):
        def build(self):
            ports=super().build()
            original=ports.loop.run_actions
            async def actions(items):
                result=await original(items)
                registry.set_run_paused('chat', True)
                return result
            async def notify(_payload): reached.set()
            ports.loop.run_actions=actions
            ports.loop.wait_if_paused=lambda:registry.wait_if_paused('chat',admission,notify)
            return ports
    fake=PausablePorts([AssistantTurn(tool_calls=(action,)),AssistantTurn(text='first acknowledged'),
                        AssistantTurn(text='second acknowledged')],[batch])
    task=asyncio.create_task(run_main(fake))
    try:
        await asyncio.wait_for(reached.wait(),2)
        fake.steering.extend([
            {'id':'first','delivery':'steer','text':'new instruction during pause'},
            {'id':'second','delivery':'steer','text':'following instruction'},
        ])
        assert len(fake.stream_messages)==1
        registry.set_run_paused('chat',False)
        result=await asyncio.wait_for(task,2)
        assert fake.stream_messages[1][-1]['content']=='new instruction during pause'
        assert fake.stream_messages[2][-1]['content']=='following instruction'
        assert len(fake.action_batches)==1
        assert result.loop_result.reply=='second acknowledged'
        assert [row[0]['id'] for row in fake.recorded_inputs]==['first','second']
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        registry.finish_run(admission,status='complete')
