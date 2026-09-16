from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kernel_runtime.bridge import KernelBridgeServer
from kernel_runtime.bridge_protocol import (
    BRIDGE_SCHEMA, BridgeProtocolError, canonical_json, encode_frame,
    read_async_response, response_frames, sign_envelope, verify_envelope,
)
from kernel_runtime.worker_bridge import KernelBridgeClient
from kernel_runtime.wire_values import pack_value, unpack_value


@pytest.mark.asyncio
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_healthy_long_call_outlives_bridge_idle_timeout(asynchronous):
    calls = []
    frames = []

    async def invoke(request):
        calls.append(request['request_id'])
        await asyncio.sleep(1.25)
        return {'ok': True, 'result': 'completed once'}

    server = KernelBridgeServer(secret=b'test', nonce='long-call', generation=1,
                                invoke_handler=invoke, wait_heartbeat_s=.05)
    host, port = await server.start()
    client = KernelBridgeClient(host=host, port=port, secret=b'test', nonce='long-call',
                                generation=1, kernel=SimpleNamespace(), timeout_s=1)
    verify = client._verified_response

    def observed(raw, request_id):
        result = verify(raw, request_id)
        frames.append(result)
        return result

    client._verified_response = observed
    try:
        await asyncio.to_thread(client.handshake)
        request = {'op': 'invoke', 'execution_id': 'cell-a', 'outer_tool_call_id': 'outer-a'}
        result = (await client._roundtrip_async(request) if asynchronous
                  else await asyncio.to_thread(client._roundtrip, request))
        assert result['ok'] is True
        assert result['result'] == 'completed once'
        assert len(calls) == 1
        progress = [frame for frame in frames if frame.get('in_flight') is True]
        assert progress
        assert all(frame.get('waiting_for_user') is False for frame in progress)
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_large_response_preserves_value_receipt_and_single_effect(asynchronous):
    value = {'text': 'Zażółć — 東京 🙂\n' * 100_000, 'tail': {'count': 1000}}
    receipt = {'status': 'ok', 'artifact_refs': [{'ref': 'artifact://sha256/full-result'}]}
    calls = []

    async def invoke(request):
        calls.append(request['request_id'])
        return {'ok': True, 'result': value, 'receipt': receipt}

    server = KernelBridgeServer(secret=b'test', nonce='generation-one', generation=1, invoke_handler=invoke, max_frame_bytes=32768)
    host, port = await server.start()
    client = KernelBridgeClient(host=host, port=port, secret=b'test', nonce='generation-one', generation=1, kernel=SimpleNamespace(), max_frame_bytes=32768)
    try:
        await asyncio.to_thread(client.handshake)
        request = {'op': 'invoke', 'execution_id': 'cell-a', 'outer_tool_call_id': 'outer-a'}
        result = (await client._roundtrip_async(request) if asynchronous
                  else await asyncio.to_thread(client._roundtrip, request))
        assert result['ok'] is True
        assert result['result'] == value
        assert result['receipt'] == receipt
        assert len(calls) == 1
        # The same channel contract still admits a later cell; no reset/replay.
        again = await client._roundtrip_async({**request, 'execution_id': 'cell-b'})
        assert again['result']['tail'] == {'count': 1000}
        assert len(set(calls)) == 2
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['truncate', 'reorder', 'wrong_request'])
async def test_incomplete_or_cross_request_stream_never_returns_partial_success(damage):
    secret = b'test'
    response = sign_envelope(secret, {'schema': BRIDGE_SCHEMA, 'nonce': 'nonce', 'generation': 1, 'request_id': 'wanted', 'ok': True, 'result': 'x' * 20000})
    frames = list(response_frames(response, secret=secret, max_bytes=1024))
    assert all(len(canonical_json(frame)) <= 1024 for frame in frames)
    if damage == 'truncate':
        frames.pop()
    elif damage == 'reorder':
        frames[1], frames[2] = frames[2], frames[1]
    else:
        frames[1] = sign_envelope(secret, {**frames[1], 'request_id': 'wrong!'})
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(encode_frame(frame, max_bytes=1024))
    reader.feed_eof()

    def verify(frame):
        result = verify_envelope(secret, frame, expected_nonce='nonce')
        if result['request_id'] != 'wanted':
            raise BridgeProtocolError('bridge response request ID mismatch')
        return result

    with pytest.raises(BridgeProtocolError):
        await read_async_response(reader, verify=verify, max_bytes=1024)


def test_browser_compaction_retains_all_handle_identities_and_independent_metadata():
    from browser_fabric.handles import element_handle_envelope
    from browser_fabric.models import ElementRef
    from capability_broker import InvocationContext
    from kernel_runtime.worker_bridge import _decode_host_result

    ref = SimpleNamespace(opaque_id='cap-example', handler_revision='handler-v1', catalog_release_id='catalog-v1', slot_id='', slot_version=0)
    broker = SimpleNamespace(ref_for_name=lambda *a, **k: ref)
    context = InvocationContext(chat_id='chat-a', run_id='run-a', cell_execution_id='cell-a', outer_tool_call_id='outer-a', nested_call_id='nested-a', catalog_release_id='catalog-v1')
    elements = [element_handle_envelope(
        ElementRef(session_id='browser-a', target_id='page-a', generation=1, document_epoch=1, observation_revision=2, backend_ref=f'b1-{i}', role='link', name=f'日本語 {i}'),
        capabilities={'click', 'keys'}, actions={'click', 'keys'}, broker=broker, context=context,
    ) for i in range(1000)]
    original = {'schema': 'variant1.browser-observation-result.v1', 'elements': elements, 'snapshot': {'title': 'Example', 'elements': []}, 'data_ref': 'artifact://sha256/retained'}
    packed = pack_value(original)
    restored = unpack_value(packed)
    assert restored == original
    assert len(canonical_json(packed)) < len(canonical_json(original)) / 2
    restored['elements'][0]['$variant1_handle']['metadata']['cell_origin']['run_id'] = 'changed'
    assert restored['elements'][1]['$variant1_handle']['metadata']['cell_origin']['run_id'] == 'run-a'

    calls = []
    def invoke(descriptor, arguments):
        calls.append((descriptor, arguments))
        return {'clicked': arguments['handle']['id']}
    class Bridge:
        pass
    bridge = Bridge()
    bridge.invoke = invoke
    observation = _decode_host_result(packed, bridge)
    assert observation['elements'][-1].click() == {'clicked': 'page-a:b1-999'}
    assert observation['elements'][-1].metadata['cell_origin']['run_id'] == 'run-a'
    assert observation['data_ref'] == original['data_ref']
