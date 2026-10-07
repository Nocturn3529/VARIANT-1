import asyncio
from types import SimpleNamespace

import pytest

from tests.test_peers import _stack
from peers import PeerError


@pytest.mark.asyncio
async def test_restart_records_uncertain_consumed_peer_request_without_replaying(tmp_path):
    from peers import PeerCommunicationService
    from session_runtime.registry import SessionRuntimeRegistry
    from session_runtime.repository import SessionRuntimeRepository
    service, runtimes, sessions, chat, first, second = _stack(tmp_path)
    service.host.require_runtime=lambda:SimpleNamespace()
    admission=runtimes.try_reserve_run(second)
    runtimes.begin_run(admission,run_id='lost-run',thread_id='lost-run',source='chat')
    request=await service.send('chat:'+first,'chat:'+second,'Review once')
    row=runtimes.claim_input(second,'steer',run_id='lost-run')
    runtimes.complete_transcript_commit(second,[row])
    await service.shutdown()
    # Process-owned reservations vanish across restart; durable proof survives.
    fresh=SessionRuntimeRegistry(SessionRuntimeRepository(runtimes.repository.path))
    recovered=PeerCommunicationService(service.host, service.repository,
        sessions=sessions,session_runtimes=fresh,chat_service=chat)
    try:
        await recovered.start()
        assert 'without a settlement' in recovered.native_wait_failure('chat:'+first,request['message_id'])
        assert recovered.native_completion('chat:'+first,request['message_id']) is None
        assert service.repository.find_reply(request['message_id']) is None
        chat.start_next_queued_input.assert_not_awaited()
        revision=service.repository.get_message(request['message_id'])['revision']
        await recovered.shutdown();await recovered.start()
        assert recovered.native_completion('chat:'+first,request['message_id']) is None
        assert service.repository.get_message(request['message_id'])['revision']==revision
    finally:
        await recovered.shutdown()
        runtimes.finish_run(admission,status='test_cleanup')


@pytest.mark.asyncio
async def test_service_restart_does_not_invalidate_final_persistence_after_release(tmp_path):
    service,runtimes,_,_,first,second=_stack(tmp_path)
    service.host.require_runtime=lambda:SimpleNamespace()
    admission=runtimes.try_reserve_run(second)
    runtimes.begin_run(admission,run_id='finishing-run',thread_id='finishing-run',source='chat')
    gate=asyncio.Event();task=asyncio.create_task(gate.wait());runtimes.bind_admission_task(admission,task)
    request=await service.send('chat:'+first,'chat:'+second,'Review once')
    row=runtimes.claim_input(second,'steer',run_id='finishing-run')
    runtimes.complete_transcript_commit(second,[row]);runtimes.finish_run(admission,status='terminal')
    try:
        await service.shutdown();await service.start()
        assert service.native_wait_failure('chat:'+first,request['message_id']) is None
        runtimes.repository.store_run_settlement(second,admission,{'run_id':'finishing-run','status':'ok','settled':True},'Retained result')
        gate.set();await task;await asyncio.sleep(0)
        assert not runtimes.admission_task_pending(admission)
        assert service.native_completion('chat:'+first,request['message_id'])['final_answer']=='Retained result'
    finally:
        gate.set();await task;await service.shutdown()


@pytest.mark.asyncio
async def test_finished_task_without_settlement_exposes_failure_without_fabricating_completion(tmp_path):
    service, runtimes, _, _, first, second = _stack(tmp_path)
    service.host.require_runtime=lambda:SimpleNamespace()
    admission=runtimes.try_reserve_run(second)
    runtimes.begin_run(admission,run_id='recipient-run',thread_id='recipient-run',source='chat')
    gate=asyncio.Event()
    task=asyncio.create_task(gate.wait())
    runtimes.bind_admission_task(admission,task)
    try:
        request=await service.send('chat:'+first,'chat:'+second,'Review once')
        row=runtimes.claim_input(second,'steer',run_id='recipient-run')
        runtimes.complete_transcript_commit(second,[row])
        runtimes.finish_run(admission,status='terminal')
        assert service.native_wait_failure('chat:'+first,request['message_id']) is None, 'release precedes terminal persistence'
        gate.set();await task;await asyncio.sleep(0)
        assert 'without a settlement' in service.native_wait_failure('chat:'+first,request['message_id'])
        assert service.native_completion('chat:'+first,request['message_id']) is None
        assert service.repository.find_reply(request['message_id']) is None
    finally:
        gate.set();await task
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['ok', 'error', 'cancelled'])
async def test_native_final_answer_resolves_addressed_request_without_prompt_or_fake_reply(tmp_path, terminal):
    service, runtimes, sessions, chat, first, second = _stack(tmp_path)
    service.host.require_runtime=lambda:SimpleNamespace()
    admission=runtimes.try_reserve_run(second)
    runtimes.begin_run(admission,run_id='recipient-run',thread_id='recipient-run',source='chat')
    sender='chat:'+first
    try:
        request=await service.send(sender,'chat:'+second,'Inspect the deliverable',request_id='native-request')
        row=runtimes.claim_input(second,'steer',run_id='recipient-run')
        assert row['id']==request['delivery_ticket_id']
        waiter=asyncio.create_task(service.wait_message(sender,request['message_id'],timeout_s=1))
        await asyncio.sleep(0)
        runtimes.complete_transcript_commit(second,[row])
        receipt={'run_id':'recipient-run','status':terminal,'settled':True,'tool_calls':2}
        runtimes.repository.store_run_settlement(second,admission,receipt,'Verified native answer')
        runtimes.notify_run_settlement(second,'recipient-run',admission)
        result=await waiter
        assert result['status']=='settled'
        assert result['completion']['final_answer']=='Verified native answer'
        assert result['completion']['request_message_id']==request['message_id']
        assert result['completion']['admission_id']==admission
        assert result['completion']['status']==terminal
        assert result['completion']['independently_verified'] is False
        with pytest.raises(PeerError, match='does not belong'):
            service.native_completion('chat:foreign', request['message_id'])
        assert service.repository.find_reply(request['message_id']) is None
        assert len(service.repository.list_messages(sender,direction='all'))==1
        chat.start_next_queued_input.assert_not_awaited()
        await service.reply('chat:'+second, request['message_id'], 'Explicit correlated clarification')
        explicit = await service.wait_message(sender, request['message_id'], timeout_s=0)
        assert explicit['status'] == 'replied'
        assert explicit['reply']['content'] == 'Explicit correlated clarification'
    finally:
        runtimes.finish_run(admission,status='test_cleanup')
        await service.shutdown()


@pytest.mark.asyncio
async def test_another_admission_of_same_logical_run_cannot_complete_a_request(tmp_path):
    service,runtimes,_,_,first,second=_stack(tmp_path)
    admission=runtimes.try_reserve_run(second)
    runtimes.begin_run(admission,run_id='same-run',thread_id='same-run',source='chat')
    try:
        request=await service.send('chat:'+first,'chat:'+second,'Review once')
        row=runtimes.claim_input(second,'steer',run_id='same-run')
        runtimes.complete_transcript_commit(second,[row])
        runtimes.repository.store_run_settlement(second,'different-admission',{'run_id':'same-run','status':'ok','settled':True},'Unrelated result')
        assert service.native_completion('chat:'+first,request['message_id']) is None
        result=await service.wait_message('chat:'+first,request['message_id'],timeout_s=0)
        assert result['status']=='pending'
    finally:
        runtimes.finish_run(admission,status='test_cleanup')
        await service.shutdown()
