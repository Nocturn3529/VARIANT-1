"""The passive observer must preserve usage coverage without writing runtime data."""
import importlib.util
from pathlib import Path

from model_runtime.usage_ledger import ModelUsageLedger
from tests.test_model_usage_ledger import request


def test_passive_usage_preserves_unknowns_and_does_not_count_reasoning_twice(tmp_path):
    spec = importlib.util.spec_from_file_location('live_resources', Path(__file__).parents[2]/'scripts/observe-live-swarm.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path/'usage.sqlite3'
    ledger = ModelUsageLedger(path)
    ledger.record(request())
    ledger.patch_usage('mreq-a', {'input_tokens':10,'output_tokens':20,'total_tokens':30,
        'reasoning_tokens':12,'reported_fields':['input_tokens','output_tokens','total_tokens','reasoning_tokens']})
    before = ledger.get('mreq-a')
    sample = module.usage_snapshot(path, ['chat-a', 'absent'])
    row = sample['sessions'][0]
    assert row['total_tokens']==30 and row['reasoning_tokens']==12
    assert row['cost_usd'] is None and row['cost_usd_known_requests']==0
    assert row['total_tokens_reported_requests']==1
    assert sample['sessions'][1]['requests']==0
    assert ledger.get('mreq-a')==before


def test_passive_process_observer_rejects_reused_pid_without_process_actions():
    import psutil
    spec = importlib.util.spec_from_file_location('live_resources', Path(__file__).parents[2]/'scripts/observe-live-swarm.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    current = psutil.Process()
    sample = module.process_snapshot({'backend':{'pid':current.pid,'created_at':current.create_time()-1}})
    assert sample['processes']==[]
    assert sample['missing_owners']==[{'owner':'backend','reason':'birth_identity_changed'}]


def test_passive_engagement_is_bounded_metadata_and_does_not_claim_busy(tmp_path):
    import sqlite3
    import json
    spec=importlib.util.spec_from_file_location('live_resources',Path(__file__).parents[2]/'scripts/observe-live-swarm.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    ledger=ModelUsageLedger(tmp_path/'usage.sqlite3');ledger.record(request())
    with sqlite3.connect(tmp_path/'runtime.sqlite3') as conn:
        conn.executescript('''
          CREATE TABLE astb_run_settlement(chat_id,run_id,admission_id,sequence,settled_at,final_reply);
          INSERT INTO astb_run_settlement VALUES('chat-a','run-a','admission-a',1,123,'private reply');
          CREATE TABLE workflow_goal(owner_chat_id,goal_id,status,version,updated_at,objective);
          INSERT INTO workflow_goal VALUES('chat-a','goal-a','blocked',2,123,'private objective');
          CREATE TABLE peer_message(sequence,message_id,exchange_id,in_reply_to,message_kind,state,created_at,sender_peer_id,target_peer_id,content);
        ''')
        conn.executemany('INSERT INTO peer_message VALUES(?,?,?,?,?,?,?,?,?,?)',[
            (i,f'message-{i}','exchange','', 'request','queued',123,'chat:chat-a','chat:chat-b','private prompt') for i in range(40)])
    before=ledger.get('mreq-a')
    sample=module.engagement_snapshot(tmp_path,['chat-a','absent'])
    row=sample['sessions'][0]
    assert row['live_turn_state'] is None
    assert row['last_model_request']['manifest_id']=='mreq-a'
    assert row['last_settlement']['admission_id']=='admission-a'
    assert row['goal']['status']=='blocked'
    assert len(row['peer_exchange_tail'])==16
    assert row['peer_exchange_tail'][0]['sequence']==24
    assert 'private' not in json.dumps(sample)
    assert ledger.get('mreq-a')==before
