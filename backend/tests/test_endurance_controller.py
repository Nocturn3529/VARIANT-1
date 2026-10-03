from pathlib import Path
import asyncio
import json
import sys

import pytest

CANARY = Path(__file__).resolve().parents[2]/'experiments'/'live-canary'
sys.path.insert(0,str(CANARY))
from endurance_state import EnduranceJournal, export_usage, observer_lease
from endurance_mission import grade, mission, publish_feed
from run_endurance import AdoptedBackendProcess, EnduranceBackend, ObserverClient, free_model_preflight, initialize
from model_runtime.usage_ledger import ModelUsageLedger
from tests.test_model_usage_ledger import request


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
    assert EnduranceJournal(root/'observer.sqlite3').events()[0]['payload']=={'step':1}


def test_resume_configuration_preserves_routes_and_user_changes(tmp_path):
    plan=initialize(tmp_path/'lab')
    backend=EnduranceBackend(tmp_path/'lab',plan)
    backend._prepare_config()
    custom={'operator_setting':True}
    backend.config_path.write_text(json.dumps(custom),encoding='utf-8')
    backend._prepare_config()
    assert json.loads(backend.config_path.read_text(encoding='utf-8'))==custom


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
