"""Observe a Goal-owned, multi-model swarm through the real VARIANT-1 host.

Initialize once, then run/resume the same disposable lab. The controller supplies
task resources and records evidence; the owning agent decides the team's work.
Provider keys come from the process environment or a platform-encrypted file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import time
import urllib.request
import uuid

import websockets

from run_canary import BACKEND, ROOT, BackendProcess, write_json
from endurance_mission import grade, mission, publish_feed
from endurance_state import EnduranceJournal, export_usage, observer_lease

DEFAULT_MODELS = ('stealth/space-bunny-alpha','qwen/qwen3.8-27b:free','hermes::meituan/longcat-2.5-preview:free')


def parse_route(value):
    """Explicit mixed routes; unqualified model strings retain the v1 OpenRouter meaning."""
    if isinstance(value, dict):
        provider, model = value.get('provider', 'openrouter'), value.get('model', '')
    else:
        parts = str(value).split('::', 1)
        provider, model = parts if len(parts) == 2 else ('openrouter', parts[0])
    provider, model = str(provider).strip().lower(), str(model).strip()
    if provider not in {'openrouter', 'hermes'} or not model:
        raise ValueError('Use openrouter::MODEL or hermes::MODEL for Nous OAuth')
    route = {'mode':'cloud', 'provider':provider, 'model':model}
    if provider == 'openrouter':
        route['reasoning_effort'] = 'max'
    elif model != 'meituan/longcat-2.5-preview:free':
        raise ValueError('Only the explicitly qualified LongCat Nous route is admitted in this observer')
    return route


def require_credentials(routes):
    if any(route.get('provider','openrouter') == 'openrouter' for route in routes):
        load_disposable_credential()
        if not os.environ.get('OPENROUTER_API_KEY','').strip():
            raise ValueError('A disposable OpenRouter credential is required for OpenRouter routes')


def load_disposable_credential():
    if os.environ.get('OPENROUTER_API_KEY','').strip():
        return
    secret_file=Path(os.environ.get('VARIANT1_ENDURANCE_CREDENTIAL_FILE') or Path.home()/'.variant1-endurance'/'openrouter.secret')
    if secret_file.is_file():
        sys.path.insert(0,str(BACKEND))
        from security import secretstore
        token=secret_file.read_text(encoding='utf-8').strip()
        if not token.startswith(('dpapi:','fernet:')):
            raise ValueError('Endurance credentials must use the platform encrypted secret format')
        os.environ['OPENROUTER_API_KEY']=secretstore.decrypt(token)


def source_commit():
    return subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()


def require_clean_source():
    for args in (['git','diff','--quiet'],['git','diff','--cached','--quiet']):
        if subprocess.run(args,cwd=ROOT,check=False).returncode:
            raise ValueError('Commit the source changes before a pinned live endurance phase')


def initialize(root, *, models=DEFAULT_MODELS, duration=7200, interval=900, project=None):
    root = Path(root).resolve()
    routes = [parse_route(model) for model in models]
    if duration <= 0 or interval <= 0 or len(routes) < 2 or len(routes) > 4 or len({route['model'] for route in routes}) != len(routes):
        raise ValueError('Use 2–4 distinct models and positive observation/feed intervals')
    journal = EnduranceJournal(root/'observer.sqlite3')
    if journal.get('plan') is not None:
        raise ValueError('Lab already initialized; run it again to retain the same state')
    project = Path(project).resolve() if project else root/'project'
    if project.exists() and any(project.iterdir()):
        raise ValueError('A new lab needs an empty project directory')
    if project == ROOT or ROOT in project.parents or project in ROOT.parents:
        raise ValueError('The disposable project must be outside the VARIANT-1 checkout')
    project.mkdir(parents=True,exist_ok=True)
    plan = {'schema':'variant1.endurance-plan.v2','run_id':uuid.uuid4().hex,
            'commit':source_commit(),'project':str(project),'duration_s':float(duration),'feed_interval_s':float(interval),
            'routes':routes,
            'reasoning_policy':'OpenRouter requests maximum effort. LongCat uses its advertised default reasoning without inventing an effort scale. Wire receipts and reported counters are qualification evidence, not a guarantee of model-internal effort.'}
    journal.set('plan',plan)
    publish_feed(project,0)
    write_json(root/'plan.json',plan)
    return plan


def free_model_preflight(routes):
    catalogs = {}
    providers = {route.get('provider','openrouter') for route in routes}
    if providers - {'openrouter','hermes'}:
        raise ValueError('Unsupported endurance provider')
    if 'openrouter' in providers:
        with urllib.request.urlopen('https://openrouter.ai/api/v1/models',timeout=20) as response:
            catalogs['openrouter'] = json.load(response)['data']
    if 'hermes' in providers:
        if str(BACKEND) not in sys.path: sys.path.insert(0,str(BACKEND))
        from model_runtime.hermes_proxy import available_model_catalog
        catalogs['hermes'] = asyncio.run(available_model_catalog())
    result = []
    for route in routes:
        provider = route.get('provider','openrouter')
        row = next((item for item in catalogs[provider] if item.get('id') == route['model']),None)
        if row is None:
            raise ValueError('Requested model is no longer listed: '+route['model'])
        pricing = row.get('pricing') or {}
        if any(float(pricing.get(key,'nan')) != 0 for key in ('prompt','completion')):
            raise ValueError('Requested route is no longer free: '+route['model'])
        supported = row.get('supported_parameters') or []
        if 'tools' not in supported or not any(key in supported for key in ('reasoning','reasoning_effort')):
            raise ValueError('Route lacks declared tools/reasoning: '+route['model'])
        result.append({'provider':provider,'model':route['model'],'pricing':{key:pricing.get(key) for key in ('prompt','completion')},
            'context_length':row.get('context_length'),'supported_parameters':supported,
            'input_modalities':(row.get('architecture') or {}).get('input_modalities',[]),
            'qualification':'catalog_only','checked_at':time.time()})
    if not any('image' in row['input_modalities'] for row in result):
        raise ValueError('At least one selected free route must provide image input for vision auxiliary calls')
    return result


class EnduranceBackend(BackendProcess):
    def __init__(self, root, plan):
        super().__init__(Path(root),seed=plan['run_id'],enable_mcp=False,route=plan['routes'][0])
        self.plan = plan

    def _prepare_config(self):
        if self.config_path.exists():
            return  # Resume must not reset routes, catalogs, or user-applied lab settings.
        routes = self.plan['routes']
        write_json(self.config_path.parent/'plugins'/'model-providers'/'endurance-openrouter'/'provider.json',
            {'name':'openrouter','request_defaults':{'provider':{'max_price':{'prompt':0,'completion':0},'require_parameters':True}}})
        cloud = {'provider':routes[0]['provider'],'fallback_chain':[]}
        for route in routes:
            cloud.setdefault(route['provider']+'_model',route['model'])
        catalog = self.plan.get('catalog') or []
        cloud['context_windows'] = {row.get('provider','openrouter')+'/'+row['model']:row['context_length']
            for row in catalog if type(row.get('context_length')) is int and row['context_length'] > 0}
        vision = next((route for route in routes if any(row.get('provider','openrouter') == route['provider']
            and row['model'] == route['model'] and 'image' in row.get('input_modalities',[]) for row in catalog)),routes[0])
        write_json(self.config_path,{'mode':'cloud','reasoning':True,'subagent_enabled':True,
            'sampling':{'max_tokens':16384},'cloud':cloud,
            'local':{'autostart':False,'prewarm':False},
            'provider_recovery':{'enabled':True,'max_attempts':4,'max_wait_seconds':60,'fallback_routes':[],
                                 'auxiliary_routes':{'vision':[vision]}},
            'action_surface':{'support_matrix':[{'profile':'trusted-local.v1','provider':route['provider'],'model':route['model'],
                'adapter':'openai.*','status':'canary','evidence':'Disposable endurance qualification; not a general support claim.'} for route in routes]}})

    def _prepare_tools_config(self):
        if not self.tools_config_path.exists():
            super()._prepare_tools_config()

    def start(self):
        self.run_root.mkdir(parents=True,exist_ok=True)
        if self.log_path.exists():
            self.log_path.rename(self.run_root/f'backend-{time.time_ns()}.log')
        self.port_file.unlink(missing_ok=True)
        return super().start()

    def ensure_started(self, journal):
        """Adopt a surviving exact lab backend after an observer crash."""
        import psutil
        if self.port_file.is_file():
            connection=json.loads(self.port_file.read_text(encoding='utf-8'))
            try:
                process=psutil.Process(int(connection['pid']))
                command=process.cmdline()
                marker=command.index('--port-file') if '--port-file' in command else -1
                owned=(marker >= 0 and marker+1 < len(command)
                       and Path(command[marker+1]).resolve()==self.port_file.resolve()
                       and any(Path(item).resolve()==(BACKEND/'server.py').resolve() for item in command if item.endswith('server.py')))
                if not owned:
                    raise RuntimeError('Backend PID identity does not match this lab; refusing takeover')
                previous=journal.get('backend_owner')
                if previous and previous['pid']==process.pid and previous['created_at']!=process.create_time():
                    raise RuntimeError('Backend PID was reused; refusing takeover')
                self.process=AdoptedBackendProcess(process)
                with urllib.request.urlopen(f'http://127.0.0.1:{int(connection["port"])}/health',timeout=5) as response:
                    if response.status != 200:
                        raise RuntimeError('Surviving lab backend is not healthy')
                journal.append('observer_intervention',{'action':'reconnected_existing_backend'})
                return connection
            except psutil.NoSuchProcess:
                pass
        connection=self.start()
        journal.set('backend_owner',{'pid':self.process.pid,'created_at':psutil.Process(self.process.pid).create_time()})
        return connection


class AdoptedBackendProcess:
    """Popen-compatible controls fenced by process birth identity."""
    def __init__(self, process):
        self.pid=process.pid
        self.process=process
        self.created_at=process.create_time()

    def _owned(self):
        import psutil
        try:
            return self.process.is_running() and self.process.create_time()==self.created_at
        except psutil.NoSuchProcess:
            return False

    def poll(self):
        return None if self._owned() else 0

    def wait(self,timeout=None):
        import psutil
        if not self._owned():return 0
        try:return self.process.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            raise subprocess.TimeoutExpired('owned endurance backend',timeout) from None

    def terminate(self):
        if self._owned():self.process.terminate()

    def kill(self):
        if self._owned():self.process.kill()


class ObserverClient:
    """One bounded receiver; asynchronous events never compete with command replies."""
    def __init__(self, ws, journal):
        self.ws, self.journal = ws, journal
        self.waiters = {}
        self.task = asyncio.create_task(self.receive())

    async def receive(self):
        try:
            async for raw in self.ws:
                row = json.loads(raw)
                if not isinstance(row,dict):
                    continue
                for request_id,(predicate,future) in tuple(self.waiters.items()):
                    if not future.done() and predicate(row):
                        future.set_result(row)
                kind = str(row.get('type') or '')
                if kind in {'run:settled','peer:changed','work:event','model:request_manifest','error'}:
                    # No prompt bodies, full replies, credentials, or error strings.
                    value = {key:row[key] for key in ('session_id','chat_id','run_id','status','source','message_id','revision','manifest_id','code') if key in row}
                    if kind == 'work:event':
                        event = row.get('event') or {}
                        value = {key:event.get(key) for key in ('event_type','aggregate_kind','aggregate_id','aggregate_version','sequence')}
                    self.journal.append('host:'+kind,value)
        except Exception as exc:
            for _,future in self.waiters.values():
                if not future.done():
                    future.set_exception(ConnectionError(type(exc).__name__))

    async def command(self, message, predicate=None, *, timeout=30):
        request_id = str(message.get('request_id') or 'observer-'+uuid.uuid4().hex)
        message = {**message,'request_id':request_id}
        predicate = predicate or (lambda row:row.get('request_id')==request_id)
        future = asyncio.get_running_loop().create_future()
        self.waiters[request_id] = (predicate,future)
        try:
            await self.ws.send(json.dumps(message))
            row = await asyncio.wait_for(future,timeout)
            if row.get('type') == 'error' or str(row.get('type','')).endswith(':rejected') or row.get('ok') is False:
                raise RuntimeError('Host rejected '+message['type'])
            return row
        finally:
            self.waiters.pop(request_id,None)

    async def close(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass


async def prepare_sessions(client, journal, plan):
    sessions = journal.get('sessions',[])
    for index,route in enumerate(plan['routes']):
        if index < len(sessions):
            saved=sessions[index]
            snapshot=await client.command({'type':'session:settings:get','session_id':saved['id']})
            actual=snapshot.get('route') or {}
            if any(actual.get(key)!=route.get(key) for key in ('mode','provider','model')):
                raise ValueError('Retained session route changed; record a new phase instead of silently switching it')
            if (actual.get('reasoning_effort') or '') != (route.get('reasoning_effort') or ''):
                raise ValueError('Retained session reasoning policy changed')
            await client.command({'type':'chat:project:set','chat_id':saved['id'],'root':plan['project']})
            continue
        request_id = f'endurance:{plan["run_id"]}:session:{index}'
        result = await client.command({'type':'chat:session:new','request_id':request_id},
            lambda row:row.get('type')=='chat:session' and (row.get('navigation') or {}).get('request_id')==request_id)
        chat_id = result['session']['id']
        # The durable session creation request can be repeated after a lost reply.
        await client.command({'type':'chat:project:set','chat_id':chat_id,'root':plan['project']})
        selected=await client.command({'type':'mode:set','scope':'session','id':chat_id,**route},
            lambda row:row.get('type')=='error' or (row.get('type')=='chat:context' and row.get('session_id')==chat_id),timeout=90)
        if any(selected.get(key)!=route.get(key) for key in ('provider','model')):
            raise ValueError('Host did not acknowledge the pinned provider/model')
        name = 'Endurance coordinator' if index == 0 else f'Endurance collaborator {index}'
        await client.ws.send(json.dumps({'type':'chat:session:rename','id':chat_id,'title':name}))
        sessions.append({'id':chat_id,'peer_id':'chat:'+chat_id,'role':'coordinator' if index==0 else 'collaborator','route':route})
        journal.set('sessions',sessions)
    return sessions


async def submit_goal(client, journal, plan, sessions):
    start = journal.get('started_at')
    if start is None:
        start = time.time()
        journal.set('started_at',start)
    deadline = start + plan['duration_s']
    request_id = 'endurance:'+plan['run_id']+':goal'
    row = await client.command({'type':'goal:submit','session_id':sessions[0]['id'],'request_id':request_id,
        'objective':mission(plan['project'],sessions,deadline)},timeout=90)
    goal_id = row['result']['goal']['goal_id']
    journal.set('goal_id',goal_id)
    return goal_id,deadline


def resource_sample(backend, project):
    sample = {'backend_alive':backend.process.poll() is None,'sampled_at':time.time()}
    try:
        import psutil
        root = psutil.Process(backend.process.pid)
        processes = [root,*root.children(recursive=True)]
        measurements=[(p.memory_info().rss,p.cpu_times()) for p in processes if p.is_running()]
        sample.update(process_count=len(processes),tree_rss_bytes=sum(row[0] for row in measurements),
                      tree_cpu_time_s=sum(row[1].user+row[1].system for row in measurements))
    except Exception:
        sample.update(process_count=None,tree_rss_bytes=None,tree_cpu_time_s=None)
    for name,directory in (('data',backend.data_dir),('project',Path(project))):
        total, count, complete = 0, 0, True
        deadline=time.monotonic()+1
        for parent,dirs,files in os.walk(directory,followlinks=False):
            dirs[:] = [item for item in dirs if not (Path(parent)/item).is_symlink()]
            for name_in_dir in files:
                path=Path(parent)/name_in_dir
                if not path.is_symlink():
                    try:total += path.stat().st_size
                    except OSError:complete=False
                count += 1
            if count >= 20000 or time.monotonic() >= deadline:
                complete=False;break
        sample[name+'_bytes']=total
        sample[name+'_measurement_complete']=complete
    return sample


def peer_summary(data_dir, sessions):
    """Count real request/result links without exporting agent-message content."""
    database = Path(data_dir)/'data'/'astb'/'astb.sqlite3'
    if not database.is_file():
        return {'available':False, 'exchanges':[], 'limitations':'Peer evidence not available yet.'}
    ids = [session['peer_id'] for session in sessions]
    placeholders = ','.join('?' for _ in ids)
    try:
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            deadline = time.monotonic()+1
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline),10000)
            rows = conn.execute(f'''SELECT m.sender_peer_id,m.target_peer_id,m.message_kind,m.state,count(*) AS messages,
                sum(EXISTS(SELECT 1 FROM peer_message r WHERE r.in_reply_to=m.message_id
                    AND r.message_kind='result' AND r.sender_peer_id=m.target_peer_id
                    AND r.target_peer_id=m.sender_peer_id)) AS correlated_results
                FROM peer_message m WHERE m.sender_peer_id IN ({placeholders}) AND m.target_peer_id IN ({placeholders})
                GROUP BY m.sender_peer_id,m.target_peer_id,m.message_kind,m.state''',(*ids,*ids)).fetchall()
        return {'available':True,'measurement_complete':True,'exchanges':[dict(row) for row in rows],
            'limitations':'Metadata-only message delivery and correlated-result evidence. It does not certify contribution quality.'}
    except sqlite3.Error:
        return {'available':False,'measurement_complete':False,'exchanges':[], 'limitations':'Peer evidence unavailable or exceeded its bounded observation time.'}


async def run(root, *, tick=30, final_grace=300):
    with observer_lease(Path(root).resolve()):
        return await run_owned(root,tick=tick,final_grace=final_grace)


async def admit(root, *, timeout=600, only=None, retry_blocked=False):
    """Short real-provider route/Goal gate; this is not a swarm endurance grade."""
    root=Path(root).resolve()
    with observer_lease(root):
        journal=EnduranceJournal(root/'observer.sqlite3')
        plan=journal.get('plan')
        if plan is None or source_commit()!=plan['commit']:
            raise ValueError('Initialize an admission lab at the current committed source')
        selected=set(only or [route['model'] for route in plan['routes']])
        if not selected or selected-set(route['model'] for route in plan['routes']):
            raise ValueError('Admission selection must use models pinned in this lab')
        require_clean_source()
        require_credentials(plan['routes'])
        catalog=await asyncio.to_thread(free_model_preflight,plan['routes'])
        plan = {**plan,'catalog':catalog}
        backend=EnduranceBackend(root,plan)
        connection,client,ledger=None,None,None
        report={'schema':'variant1.endurance-admission.v1','commit':plan['commit'],'catalog':catalog,
                'cases':[],'limitations':'Short individual Goal qualification. No swarm collaboration, browser/desktop coverage, or long-duration claim.'}
        previous=root/'admission.json'
        if previous.exists():
            archived=root/f'admission-{time.time_ns()}.json'
            previous.rename(archived)
            report['previous_report']=archived.name
        sys.path.insert(0,str(BACKEND))
        from model_runtime.usage_ledger import ModelUsageLedger
        try:
            connection=await asyncio.to_thread(backend.ensure_started,journal)
            url=f'ws://127.0.0.1:{connection["port"]}/ws?token={connection["token"]}'
            async with websockets.connect(url,open_timeout=20,max_size=8*1024*1024) as ws:
                client=ObserverClient(ws,journal)
                sessions=await prepare_sessions(client,journal,plan)
                ledger=ModelUsageLedger(backend.data_dir/'data'/'model-usage.sqlite3')
                for index,session in enumerate(sessions):
                    if session['route']['model'] not in selected:
                        continue
                    workspace=Path(plan['project'])/f'admission-{index}'
                    workspace.mkdir(parents=True,exist_ok=True)
                    await client.command({'type':'chat:project:set','chat_id':session['id'],'root':str(workspace)})
                    request_id=f'endurance:{plan["run_id"]}:admission:{index}'
                    accepted=await client.command({'type':'goal:submit','session_id':session['id'],'request_id':request_id,
                        'objective':'Work only in this project. Create a small program that sums squares of the integers 1 through 20. '
                            'Write its computed result to proof.json with an integer value field and a verified boolean field. '
                            'Verify the calculation and inspect the saved file. Complete only after the deliverable is verified.'},timeout=90)
                    goal_id=accepted['result']['goal']['goal_id']
                    if accepted['result']['goal']['status']=='blocked' and retry_blocked:
                        await client.command({'type':'goal:continue','session_id':session['id'],'goal_id':goal_id,
                            'expected_version':accepted['result']['goal']['version'],'message':''},timeout=90)
                        journal.append('operator_control',{'action':'admission_retry','model':session['route']['model'],'goal_id':goal_id})
                    started=time.monotonic()
                    status='queued'
                    while time.monotonic()-started < timeout:
                        row=await client.command({'type':'goal:status:get','session_id':session['id'],'goal_id':goal_id})
                        status=row['result']['goal']['status']
                        if status in {'succeeded','failed','blocked','cancelled','archived','paused'}:
                            break
                        await asyncio.sleep(2)
                    if status in {'queued','running','waiting_external','waiting_user'}:
                        row=await client.command({'type':'goal:status:get','session_id':session['id'],'goal_id':goal_id})
                        await client.command({'type':'goal:cancel','session_id':session['id'],'goal_id':goal_id,
                            'expected_version':row['result']['goal']['version'],'reason':'Admission timeout'})
                        journal.append('observer_intervention',{'action':'admission_timeout','model':session['route']['model']})
                    try:
                        proof=json.loads((workspace/'proof.json').read_text(encoding='utf-8'))
                        artifact_ok=proof.get('value')==sum(value*value for value in range(1,21)) and proof.get('verified') is True
                    except (OSError,ValueError,AttributeError):
                        artifact_ok=False
                    requests=ledger.read(goal_id=goal_id,limit=500)['items']
                    checks={'goal_completed':status=='succeeded','artifact_verified':artifact_ok,'physical_requests_observed':bool(requests),
                            'pinned_model':bool(requests) and all(row['provider']==session['route']['provider'] and row['model']==session['route']['model'] for row in requests),
                            'reasoning_policy_on_wire':bool(requests) and all(row['metadata']['reasoning_effort']==session['route'].get('reasoning_effort','') for row in requests)}
                    case={'provider':session['route']['provider'],'model':session['route']['model'],'goal_id':goal_id,'status':status,'checks':checks,
                          'reasoning_policy':'maximum_requested' if session['route'].get('reasoning_effort') else 'provider_default_no_declared_effort_scale',
                          'passed':all(checks.values()),'seconds':time.monotonic()-started,'usage':ledger.totals(goal_id=goal_id)}
                    report['cases'].append(case)
                    write_json(root/'admission.json',report)
                    print(json.dumps(case,ensure_ascii=False),flush=True)
                report['passed']=all(case['passed'] for case in report['cases'])
                return 0 if report['passed'] else 1
        finally:
            if client:await client.close()
            if connection:
                try:
                    request=urllib.request.Request(f'http://127.0.0.1:{connection["port"]}/shutdown',data=b'',method='POST',
                        headers={'Authorization':'Bearer '+connection['token']})
                    await asyncio.to_thread(urllib.request.urlopen,request,timeout=10)
                    await asyncio.to_thread(backend.process.wait,15)
                except Exception:
                    journal.append('observer_intervention',{'action':'forced_backend_stop'})
            backend.stop()
            if ledger:export_usage(ledger,root/'usage')
            write_json(root/'admission.json',report)


async def run_owned(root, *, tick=30, final_grace=300):
    root = Path(root).resolve()
    journal = EnduranceJournal(root/'observer.sqlite3')
    plan = journal.get('plan')
    if plan is None:
        raise ValueError('Initialize the lab first')
    if source_commit() != plan['commit']:
        raise ValueError('Source commit changed; use the pinned checkout or initialize a new phase')
    require_clean_source()
    require_credentials(plan['routes'])
    catalog = await asyncio.to_thread(free_model_preflight,plan['routes'])
    journal.append('preflight',catalog)
    plan = {**plan,'catalog':catalog}
    backend = EnduranceBackend(root,plan)
    connection, client, ledger, report = None, None, None, {}
    sys.path.insert(0,str(BACKEND))
    from model_runtime.usage_ledger import ModelUsageLedger
    try:
        connection = await asyncio.to_thread(backend.ensure_started,journal)
        journal.append('backend_started',{'pid':backend.process.pid,'commit':plan['commit'],'restart':journal.get('goal_id') is not None})
        url = f'ws://127.0.0.1:{connection["port"]}/ws?token={connection["token"]}'
        async with websockets.connect(url,open_timeout=20,max_size=8*1024*1024) as ws:
            client = ObserverClient(ws,journal)
            sessions = await prepare_sessions(client,journal,plan)
            goal_id,deadline = await submit_goal(client,journal,plan,sessions)
            ledger = ModelUsageLedger(backend.data_dir/'data'/'model-usage.sqlite3')
            prior_state = None
            last_price_check=time.monotonic()
            while True:
                now = time.time()
                if time.monotonic()-last_price_check >= 3600:
                    journal.append('preflight',await asyncio.to_thread(free_model_preflight,plan['routes']))
                    last_price_check=time.monotonic()
                cycle = min(int(max(0,now-journal.get('started_at'))/plan['feed_interval_s']),
                            int(plan['duration_s']/plan['feed_interval_s']))
                expected = publish_feed(plan['project'],cycle)
                journal.append('feed_revision',{'revision':cycle},identity=f'feed:{cycle}')
                status_started=time.monotonic()
                goal = await client.command({'type':'goal:status:get','session_id':sessions[0]['id'],'goal_id':goal_id})
                status_latency=time.monotonic()-status_started
                state = goal['result']['goal']['status']
                sample=await asyncio.to_thread(resource_sample,backend,plan['project'])
                sample['goal_status_latency_s']=status_latency
                sample['runtime']=goal['result'].get('runtime')
                sample['accounting']=goal['result'].get('accounting')
                journal.append('sample',sample)
                artifact_grade = await asyncio.to_thread(grade,plan['project'],expected,now=now,deadline=deadline)
                report = {'schema':'variant1.endurance-status.v1','run_id':plan['run_id'],'commit':plan['commit'],
                    'goal_id':goal_id,'goal_status':state,'elapsed_s':now-journal.get('started_at'),'deadline':deadline,
                    'usage':ledger.totals(),'artifact_grade':artifact_grade,
                    'latest_sample':sample,
                    'collaboration':await asyncio.to_thread(peer_summary,backend.data_dir,sessions),
                    'full_mission_qualified':False,'review_required':['citations','test quality','browser/desktop coverage','operating records','peer collaboration'],
                    'capability_availability':{'native_browser':'requires an attached browser host','desktop':'environment dependent; not certified by controller'}}
                write_json(root/'status.json',report)
                if state != prior_state:
                    print(json.dumps({'goal_status':state,'elapsed_s':round(report['elapsed_s']),'artifact_checks':artifact_grade['checks']}),flush=True)
                    prior_state = state
                control = journal.get('control')
                if control:
                    if control['action'] in {'pause','resume','cancel','continue'}:
                        action = control['action']
                        await client.command({'type':'goal:'+action,'session_id':sessions[0]['id'],'goal_id':goal_id,
                            'expected_version':goal['result']['goal']['version'],'reason':'Explicit endurance operator control',
                            'message':control.get('guidance','')})
                        journal.append('operator_control',{'action':action},identity=control['id'])
                        journal.set('control',None)
                        if action=='cancel':
                            break
                if state in {'succeeded','failed','cancelled','archived'} or now >= deadline+final_grace:
                    break
                await asyncio.sleep(max(1,tick))
            if state not in {'succeeded','failed','cancelled','archived'}:
                goal = await client.command({'type':'goal:status:get','session_id':sessions[0]['id'],'goal_id':goal_id})
                await client.command({'type':'goal:cancel','session_id':sessions[0]['id'],'goal_id':goal_id,
                    'expected_version':goal['result']['goal']['version'],'reason':'Observation window ended'})
                journal.append('observer_intervention',{'action':'deadline_cancel'})
            report['end_reason'] = state if state in {'succeeded','failed','cancelled','archived'} else 'observation_deadline'
            return 0 if state == 'succeeded' and artifact_grade['passed'] else 1
    except BaseException as exc:
        journal.append('observer_failure',{'type':type(exc).__name__})
        report['end_reason']='observer_failure'
        report['failure_type']=type(exc).__name__
        raise
    finally:
        if client:
            await client.close()
        if connection:
            try:
                request = urllib.request.Request(f'http://127.0.0.1:{connection["port"]}/shutdown',data=b'',method='POST',
                    headers={'Authorization':'Bearer '+connection['token']})
                await asyncio.to_thread(urllib.request.urlopen,request,timeout=10)
                await asyncio.to_thread(backend.process.wait,15)
            except Exception:
                journal.append('observer_intervention',{'action':'forced_backend_stop'})
        backend.stop()
        if ledger is not None:
            try:
                report['usage']=export_usage(ledger,root/'usage')['totals']
            except Exception as exc:
                report['usage_export_error']=type(exc).__name__
        if report:
            write_json(root/'report.json',report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['init','preflight','admit','run','status','pause','resume','continue','stop','export','reconcile'])
    parser.add_argument('--root',required=True)
    parser.add_argument('--models','--routes',nargs='+',default=list(DEFAULT_MODELS),help='MODEL (OpenRouter), openrouter::MODEL, or hermes::MODEL (Nous OAuth)')
    parser.add_argument('--duration',type=float,default=7200)
    parser.add_argument('--feed-interval',type=float,default=900)
    parser.add_argument('--tick',type=float,default=30)
    parser.add_argument('--project')
    parser.add_argument('--guidance',default='')
    parser.add_argument('--timeout',type=float,default=600)
    parser.add_argument('--only',nargs='+',help='Admit only these models from the existing lab plan')
    parser.add_argument('--retry-blocked',action='store_true',help='Explicitly continue a blocked admission Goal; record the operator intervention')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.operation=='init':
        print(json.dumps(initialize(root,models=args.models,duration=args.duration,interval=args.feed_interval,project=args.project),indent=2))
        return 0
    journal = EnduranceJournal(root/'observer.sqlite3')
    plan = journal.get('plan')
    if plan is None:
        raise ValueError('Initialize this lab first')
    if args.operation=='run':
        return asyncio.run(run(root,tick=args.tick))
    if args.operation=='admit':
        return asyncio.run(admit(root,timeout=args.timeout,only=args.only,retry_blocked=args.retry_blocked))
    if args.operation=='preflight':
        print(json.dumps(free_model_preflight(plan['routes']),indent=2))
    elif args.operation in {'pause','resume','continue','stop'}:
        journal.set('control',{'id':uuid.uuid4().hex,'action':'cancel' if args.operation=='stop' else args.operation,'guidance':args.guidance})
    elif args.operation=='status':
        print((root/'status.json').read_text(encoding='utf-8') if (root/'status.json').exists() else json.dumps({'status':'prepared','plan':plan}))
    elif args.operation in {'export','reconcile'}:
        sys.path.insert(0,str(BACKEND))
        from model_runtime.usage_ledger import ModelUsageLedger
        path = root/'runtime-data'/'data'/'model-usage.sqlite3'
        if not path.is_file():
            raise ValueError('No usage ledger exists yet')
        ledger=ModelUsageLedger(path)
        if args.operation=='reconcile':
            from endurance_reconcile import reconcile_usage
            load_disposable_credential()
            key=os.environ.get('OPENROUTER_API_KEY','').strip()
            if not key:raise ValueError('A disposable OpenRouter credential is required')
            result=reconcile_usage(ledger,key)
            journal.append('usage_reconciliation',result)
            print(json.dumps(result,indent=2))
        print(json.dumps(export_usage(ledger,root/'usage')['totals'],indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
