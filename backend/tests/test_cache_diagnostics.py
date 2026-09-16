"""Equality evidence must explain churn without retaining model input."""
from copy import deepcopy
import json

from model_runtime.cache_diagnostics import CacheEqualityRecorder


SECRET = 'private-prompt-and-argument-sentinel'
PIXELS = 'data:image/png;base64,private-pixels-sentinel'
ENCRYPTED = 'opaque-reasoning-replay-sentinel'


def _payload():
    return {
        'model':'model-a', 'instructions':SECRET, 'reasoning':{'effort':'xhigh'},
        'input':[
            {'role':'user','content':SECRET},
            {'type':'reasoning','encrypted_content':ENCRYPTED},
            {'type':'function_call','name':'ipython','arguments':json.dumps({'code':SECRET})},
            {'type':'message','role':'user','content':[{'type':'input_image','image_url':PIXELS}]},
        ],
    }


def _record(recorder, payload, *, owner='chat-a', lane=('provider','model-a','agent'), manifest='request-a', headers=None):
    return recorder.record(payload=payload, transport='responses', owner=owner,
                           lane=lane, manifest_id=manifest, header_names=headers)


def test_equality_is_private_process_scoped_and_does_not_modify_requests():
    recorder = CacheEqualityRecorder()
    payload = _payload()
    before = deepcopy(payload)
    first = _record(recorder, payload, headers=('Authorization','X-Client-Request-ID'))
    repeated = _record(recorder, payload, manifest='request-b')
    assert payload == before
    encoded = json.dumps([first,repeated,dict(recorder._previous)])
    for value in (SECRET,PIXELS,ENCRYPTED,'chat-a','Authorization'):
        assert value not in encoded
    assert first['comparison'] == {'available':False}
    assert repeated['comparison']['previous_manifest_id'] == 'request-a'
    assert repeated['comparison']['input_unchanged'] is True
    assert repeated['comparison']['instructions_unchanged'] is True
    assert first['instructions_id'] == repeated['instructions_id']
    assert first['instructions_id'] != _record(CacheEqualityRecorder(),payload)['instructions_id']
    assert first['reasoning_replay'] == {'items':1,'encrypted_utf8_bytes':len(ENCRYPTED)}
    assert first['cache_affinity_header_presence']['x-client-request-id'] is True
    assert first['cache_affinity_header_presence']['session_id'] is False
    assert repeated['cache_affinity_header_presence']['session_id'] is None


def test_append_and_image_eviction_are_distinguished_from_instruction_changes():
    recorder = CacheEqualityRecorder()
    payload = _payload()
    _record(recorder,payload)
    extended = deepcopy(payload)
    extended['input'].append({'type':'function_call_output','output':'done'})
    second = _record(recorder,extended,manifest='request-b')
    assert second['comparison']['input_append_only'] is True
    assert second['comparison']['first_changed_item_index'] == 4
    assert second['image_item_transition'] == {'added_items':0,'removed_items':0,'retained_items':1}
    evicted = deepcopy(extended)
    del evicted['input'][3]
    third = _record(recorder,evicted,manifest='request-c')
    assert third['comparison']['input_append_only'] is False
    assert third['comparison']['first_changed_item_index'] == 3
    assert third['comparison']['instructions_unchanged'] is True
    assert third['image_item_transition']['removed_items'] == 1
    evicted['instructions'] += ' changed time'
    fourth = _record(recorder,evicted,manifest='request-d')
    assert fourth['comparison']['instructions_unchanged'] is False
    assert fourth['comparison']['input_unchanged'] is True
    evicted['reasoning']['effort'] = 'high'
    fifth = _record(recorder,evicted,manifest='request-e')
    assert fifth['comparison']['prefix_settings_unchanged'] is False


def test_comparisons_do_not_cross_chat_or_auxiliary_lanes_and_are_bounded():
    recorder = CacheEqualityRecorder(max_scopes=2)
    payload = _payload()
    _record(recorder,payload)
    assert not _record(recorder,payload,owner='chat-b')['comparison']['available']
    assert _record(recorder,payload,manifest='request-a2')['comparison']['previous_manifest_id'] == 'request-a'
    assert not _record(recorder,payload,lane=('provider','model-a','memory'))['comparison']['available']
    assert len(recorder._previous) == 2
    assert not _record(recorder,payload,owner='chat-b')['comparison']['available']


def test_long_history_comparison_reports_its_coverage_limit():
    recorder = CacheEqualityRecorder(max_items=4,ordered_limit=2)
    payload = {'instructions':'stable','input':[{'role':'user','content':str(i)} for i in range(8)]}
    first = _record(recorder,payload)
    assert first['ordered_items_omitted'] == 6
    assert [row['index'] for row in first['ordered_item_ids']] == [0,7]
    payload['input'].append({'role':'user','content':'new'})
    second = _record(recorder,payload)
    assert second['comparison']['input_append_only'] is None
    assert second['comparison']['comparison_truncated'] is True
    payload['input'][2]['content'] = 'changed'
    third = _record(recorder,payload)
    assert third['comparison']['first_changed_item_index'] == 2
    assert third['comparison']['comparison_truncated'] is False
    assert third['comparison']['input_append_only'] is False


def test_chat_completion_system_and_unsupported_transport_are_explicit():
    recorder = CacheEqualityRecorder()
    payload = {'messages':[{'role':'system','content':SECRET},{'role':'user','content':'task'}]}
    first = recorder.record(payload=payload,transport='chat_completions',owner='chat',lane=(),manifest_id='a')
    payload['messages'][0]['content'] = 'new instructions'
    second = recorder.record(payload=payload,transport='chat_completions',owner='chat',lane=(),manifest_id='b')
    assert first['instruction_source'] == 'messages.system_and_developer'
    assert not second['comparison']['instructions_unchanged']
    assert second['comparison']['first_changed_item_index'] == 0
    unsupported = recorder.record(payload=payload,transport='unknown',owner='chat',lane=(),manifest_id='c')
    assert unsupported == {'available':False,'reason':'transport_not_projected'}
