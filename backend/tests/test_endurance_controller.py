from pathlib import Path
import asyncio
import json
import sys

import pytest

CANARY = Path(__file__).resolve().parents[2]/'experiments'/'live-canary'
sys.path.insert(0,str(CANARY))
from endurance_state import EnduranceJournal, export_usage, observer_lease
from endurance_mission import grade, mission, publish_feed
from run_endurance import AdoptedBackendProcess, EnduranceBackend, ObserverClient, free_model_preflight, initialize, parse_route, require_credentials, peer_summary, prepare_sessions
from model_runtime.usage_ledger import ModelUsageLedger
from tests.test_model_usage_ledger import request
from endurance_reconcile import generation_usage, reconcile_usage


def test_lab_initialization_is_once_only_and_does_not_reset_retained_state(tmp_path):
    root = tmp_path/'lab'
    plan = initialize(root,duration=100,interval=5)
    journal = EnduranceJournal(root/'observer.sqlite3')
    journal.set('sessions',[{'id':'retained'}])
    assert journal.append('milestone',{'step':1},identity='once')
    assert not journal.append('milestone',{'step':1},identity='once')
    with pytest.raises(ValueError,match='already initialized'):
        initialize(root)
    assert EnduranceJournal(root/'observer.sqlite3').get('sessions') == [{'id':'retained'}]
    assert plan['routes'][0]['model']=='stealth/space-bunny-alpha'
    assert plan['routes'][2]['provider']=='hermes'
    assert plan['routes'][2]['model']=='meituan/longcat-2.5-preview:free'
    assert 'reasoning_effort' not in plan['routes'][2]
    assert EnduranceJournal(root/'observer.sqlite3').events()[0]['payload']=={'step':1}


def test_resume_configuration_preserves_routes_and_user_changes(tmp_path):
    plan=initialize(tmp_path/'lab')
    backend=EnduranceBackend(tmp_path/'lab',plan)
    backend._prepare_config()
    price_policy=json.loads((backend.config_path.parent/'plugins'/'model-providers'/'endurance-openrouter'/'provider.json').read_text(encoding='utf-8'))
    assert price_policy['request_defaults']['provider']['max_price']=={'prompt':0,'completion':0}
    cfg=json.loads(backend.config_path.read_text(encoding='utf-8'))
    assert cfg['cloud']['hermes_model']=='meituan/longcat-2.5-preview:free'
    assert 'oauth' not in cfg['cloud']
    assert {row['provider'] for row in cfg['action_surface']['support_matrix']}=={'hermes','openrouter'}
    custom={'operator_setting':True}
    backend.config_path.write_text(json.dumps(custom),encoding='utf-8')
    backend._prepare_config()
    assert json.loads(backend.config_path.read_text(encoding='utf-8'))==custom


def test_mixed_config_uses_advertised_windows_and_selected_free_vision(tmp_path):
    plan=initialize(tmp_path/'lab',models=['nvidia/nemotron-3-ultra-550b-a55b:free','hermes::meituan/longcat-2.5-preview:free'])
    plan['catalog']=[{'provider':'openrouter','model':plan['routes'][0]['model'],'context_length':262144,'input_modalities':['text']},
                     {'provider':'hermes','model':plan['routes'][1]['model'],'context_length':1048576,'input_modalities':['text','image']}]
    backend=EnduranceBackend(tmp_path/'lab',plan)
    backend._prepare_config()
    cfg=json.loads(backend.config_path.read_text())
    assert cfg['cloud']['context_windows']['hermes/meituan/longcat-2.5-preview:free']==1048576
    assert cfg['provider_recovery']['auxiliary_routes']['vision']==[plan['routes'][1]]


def test_one_observer_owns_the_lab_and_crash_style_close_releases_lease(tmp_path):
    with observer_lease(tmp_path):
        with pytest.raises(RuntimeError,match='Another observer'):
            with observer_lease(tmp_path):
                pass
    with observer_lease(tmp_path):
        pass


def test_adopted_backend_controls_never_act_on_reused_pid():
    calls=[]
    class Process:
        pid=123
        birth=1
        def is_running(self):return True
        def create_time(self):return self.birth
        def terminate(self):calls.append('terminate')
        def kill(self):calls.append('kill')
    process=Process()
    owned=AdoptedBackendProcess(process)
    process.birth=2
    owned.terminate();owned.kill()
    assert calls==[] and owned.poll()==0


def test_feed_updates_are_revisioned_and_grader_rejects_stale_or_early_success(tmp_path,monkeypatch):
    first=publish_feed(tmp_path,0)
    latest=publish_feed(tmp_path,1)
    assert first[0]['status'] != latest[0]['status']
    assert publish_feed(tmp_path,1)==latest
    (tmp_path/'research-index.json').write_text(json.dumps(first),encoding='utf-8')
    (tmp_path/'report.md').write_text('\n'.join(row['url'] for row in latest),encoding='utf-8')
    (tmp_path/'service.json').write_text('{"health_url":"https://example.com"}',encoding='utf-8')
    result=grade(tmp_path,latest,now=9,deadline=10)
    assert result['checks']['exact_latest_feed'] is False
    assert result['checks']['observation_window_complete'] is False
    assert result['checks']['running_dashboard'] is False
    assert 'session.context' not in mission(tmp_path,[{'peer_id':'chat:peer'}],10)


def test_usage_export_streams_null_coverage_corrected_usage_and_group_totals(tmp_path):
    ledger=ModelUsageLedger(tmp_path/'usage.sqlite3')
    ledger.record(request())
    ledger.record(request('mreq-b'))
    ledger.patch_usage('mreq-a',{'input_tokens':10,'output_tokens':5,'total_tokens':15,'reasoning_tokens':3,'reported_fields':['input_tokens','output_tokens','total_tokens','reasoning_tokens']})
    ledger.patch_terminal('mreq-a',outcome='succeeded',duration_s=3)
    ledger.patch_usage('mreq-a',{'input_tokens':10,'output_tokens':6,'total_tokens':16,'reasoning_tokens':4,'reported_fields':['input_tokens','output_tokens','total_tokens','reasoning_tokens']})
    summary=export_usage(ledger,tmp_path/'export')
    assert summary['totals']['requests']==2 and summary['totals']['total_tokens']==16
    assert summary['totals']['reasoning_tokens']==4
    assert summary['totals']['reasoning_tokens_known_requests']==1
    assert summary['totals']['reasoning_tokens_reported_requests']==1
    rows=[json.loads(line) for line in (tmp_path/'export'/'requests.jsonl').read_text(encoding='utf-8').splitlines()]
    assert rows[1]['usage'] is None
    assert rows[0]['outcome']=='succeeded'
    restored=ModelUsageLedger(tmp_path/'usage.sqlite3')
    assert restored.totals()==ledger.totals()
    assert restored.groups('goal')[0]['total_tokens']==16
    assert 'total_tokens' in (tmp_path/'export'/'requests.csv').read_text(encoding='utf-8').splitlines()[0]
    assert '1/2 known' in (tmp_path/'export'/'summary.md').read_text(encoding='utf-8')


def test_reconciliation_preserves_native_token_units_and_never_stores_provider_content(tmp_path):
    ledger=ModelUsageLedger(tmp_path/'usage.sqlite3')
    ledger.record(request())
    ledger.patch_response('mreq-a',{'provider_generation_id':'gen-known'})
    ledger.patch_terminal('mreq-a',outcome='cancelled')
    result=reconcile_usage(ledger,'unused fixture credential',fetch=lambda identity:{'id':identity,
        'native_tokens_prompt':10,'native_tokens_completion':6,'native_tokens_reasoning':4,
        'tokens_prompt':100,'tokens_completion':200,'total_cost':0,'prompt':'private content','external_user':'private identity'})
    row=ledger.get('mreq-a')
    assert result['updated']==1 and row['usage']['total_tokens']==16
    assert row['usage']['cost_usd']==0 and row['outcome']=='cancelled'
    assert 'private' not in json.dumps(row)
    assert generation_usage({'tokens_prompt':100})['input_tokens'] is None


def test_preflight_rejects_paid_missing_or_non_tool_models(tmp_path,monkeypatch):
    import run_endurance
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'data':[{'id':'test','pricing':{'prompt':'0','completion':'0'},
            'supported_parameters':['tools','reasoning'],'architecture':{'input_modalities':['text','image']}}]}).encode()
    monkeypatch.setattr(run_endurance.urllib.request,'urlopen',lambda *args,**kwargs:Reply())
    assert free_model_preflight([{'model':'test'}])[0]['qualification']=='catalog_only'
    with pytest.raises(ValueError,match='no longer listed'):
        free_model_preflight([{'model':'missing'}])


def test_mixed_preflight_uses_credential_owned_nous_catalog_and_rejects_paid_route(monkeypatch):
    import run_endurance
    from model_runtime import hermes_proxy
    row={'id':'meituan/longcat-2.5-preview:free','pricing':{'prompt':'0','completion':'0'},
         'supported_parameters':['tools','reasoning'],'architecture':{'input_modalities':['text','image']}}
    async def catalog(): return [row]
    monkeypatch.setattr(hermes_proxy,'available_model_catalog',catalog)
    monkeypatch.setattr(run_endurance.urllib.request,'urlopen',lambda *_args,**_kwargs:pytest.fail('Nous must not query OpenRouter or receive its key'))
    route=parse_route('hermes::'+row['id'])
    assert free_model_preflight([route])[0]['provider']=='hermes'
    row['pricing']['prompt']='0.1'
    with pytest.raises(ValueError,match='no longer free'):
        free_model_preflight([route])


def test_route_parser_keeps_legacy_model_strings_and_rejects_unsupported_providers(monkeypatch):
    assert parse_route('legacy-model')=={'mode':'cloud','provider':'openrouter','model':'legacy-model','reasoning_effort':'max'}
    with pytest.raises(ValueError): parse_route('nous::some-model')
    with pytest.raises(ValueError): parse_route('hermes::unqualified')
    monkeypatch.delenv('OPENROUTER_API_KEY',raising=False)
    require_credentials([parse_route('hermes::meituan/longcat-2.5-preview:free')])


def test_peer_summary_counts_real_correlated_results_without_content(tmp_path):
    from peers import PeerRepository
    database=tmp_path/'data'/'astb'/'astb.sqlite3'
    PeerRepository(str(database))
    import sqlite3
    with sqlite3.connect(database) as conn:
        for identity,sender,target,kind,reply in [('request','chat:a','chat:b','request',''),('result','chat:b','chat:a','result','request')]:
            conn.execute('''INSERT INTO peer_message(message_id,exchange_id,sender_peer_id,target_peer_id,
                in_reply_to,content,delivery,state,message_kind,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,'private agent work','follow_up','replied',?,1,1,1)''',(identity,'exchange',sender,target,reply,kind))
    summary=peer_summary(tmp_path,[{'peer_id':'chat:a'},{'peer_id':'chat:b'}])
    assert summary['available'] and summary['exchanges'][0]['correlated_results']==1
    assert 'private agent work' not in json.dumps(summary)


@pytest.mark.asyncio
async def test_observer_commands_handle_concurrent_events_without_recording_prompt_or_secrets(tmp_path):
    queue=asyncio.Queue()
    class Socket:
        def __aiter__(self):return self
        async def __anext__(self):return await queue.get()
        async def send(self,raw):
            row=json.loads(raw)
            await queue.put(json.dumps({'type':'model:request_manifest','manifest_id':'request','prompt':'secret content','headers':{'Authorization':'secret key'}}))
            await queue.put(json.dumps({'type':'goal:accepted','request_id':row['request_id'],'result':{'goal':{'status':'running'}}}))
    journal=EnduranceJournal(tmp_path/'observer.sqlite3')
    client=ObserverClient(Socket(),journal)
    try:
        result=await client.command({'type':'goal:submit'})
        assert result['result']['goal']['status']=='running'
        assert 'secret' not in json.dumps(journal.events())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_rejects_operator_model_change_without_rewriting_it(tmp_path):
    journal=EnduranceJournal(tmp_path/'observer.sqlite3')
    journal.set('sessions',[{'id':'retained','peer_id':'chat:retained'}])
    commands=[]
    class Client:
        async def command(self,message):
            commands.append(message)
            return {'route':{'mode':'cloud','provider':'openrouter','model':'changed'}}
    with pytest.raises(ValueError,match='Retained session route changed'):
        await prepare_sessions(Client(),journal,{'routes':[parse_route('original')],'project':str(tmp_path)})
    assert [row['type'] for row in commands]==['session:settings:get']


@pytest.mark.asyncio
async def test_plain_host_error_is_not_a_successful_observer_command(tmp_path):
    queue=asyncio.Queue()
    class Socket:
        def __aiter__(self):return self
        async def __anext__(self):return await queue.get()
        async def send(self,raw):
            message=json.loads(raw)
            await queue.put(json.dumps({'type':'error','request_id':message['request_id'],'error':'private body'}))
    journal=EnduranceJournal(tmp_path/'observer.sqlite3')
    client=ObserverClient(Socket(),journal)
    try:
        with pytest.raises(RuntimeError,match='Host rejected mode:set'):
            await client.command({'type':'mode:set'})
        assert 'private body' not in json.dumps(journal.events())
    finally:
        await client.close()
