import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

import pytest

import llm_cloud_stream as cloud
from llm_profiles import complete
from llm_router import LLMRouter
from llm_stream_diagnostics import StreamDiagnostics
from model_providers import CredentialLease, ProviderRequestError
from model_runtime.provider_recovery import (
    REQUEST_BUDGET, Failure, RecoveryCooldowns, RequestBudget,
    annotate_provider_error, classify_provider_error, normalize_recovery_config,
)
from session_catalog.service import IPYTHON_PROVIDER_SPEC
from tests.support.model_config import with_test_support


PRIMARY = {"mode":"cloud", "provider":"openai", "model":"primary"}
BACKUP = {"mode":"cloud", "provider":"openrouter", "model":"backup"}


def router(tmp_path, monkeypatch, **policy):
    cfg = with_test_support({"mode":"cloud", "cloud":{"provider":"openai", "openai_model":"primary"},
        "provider_recovery":{"enabled":True, "max_wait_seconds":0, "fallback_routes":[BACKUP], **policy}})
    result = LLMRouter(cfg, str(tmp_path), data_dir=str(tmp_path))
    result._credential_leases = lambda provider: [CredentialLease(provider,"one","Test","synthetic",source="environment")]
    async def fresh(_provider): return True
    monkeypatch.setattr(result, "ensure_oauth_fresh", fresh)
    monkeypatch.setattr(result, "oauth_required_for_route", lambda provider: False)
    monkeypatch.setattr("llm_recovery.context_limit_tokens", lambda r, route: 100000)
    return result


async def consume(router, **kwargs):
    return [part async for part in router.stream([{"role":"user","content":"task"}],
                  sampling={"max_tokens":16}, tools=[IPYTHON_PROVIDER_SPEC], **kwargs)]


def test_configuration_disabled_by_default_and_explicit_bounded_only():
    assert normalize_recovery_config()["enabled"] is False
    for value in ({"enabled":"true"}, {"max_attempts":True}, {"max_attempts":13},
                  {"max_wait_seconds":float("nan")}, {"fallback_routes":[{"provider":"openai"}]},
                  {"auxiliary_routes":{"unselected_task":[BACKUP]}},
                  {"fallback_routes":[{**BACKUP,"api_key":"must-not-be-a-route-option"}]}):
        with pytest.raises(ValueError): normalize_recovery_config(value)


@pytest.mark.parametrize("status,code,expected", [(503,"content_filter","content_policy"),
    (429,"insufficient_quota","quota"),(403,"upstream_blocked","upstream"),
    (400,"context_length_exceeded","context"),(401,"","authentication"),(404,"","model_unavailable")])
def test_structured_failures_preserve_cause(status, code, expected):
    error = ProviderRequestError("openai","provider error",status_code=status)
    annotate_provider_error(error,{"error":{"type":"invalid_request_error", "code":code}})
    assert classify_provider_error(error).kind == expected
    if expected in {"content_policy","quota","context","authentication"}: assert not classify_provider_error(error).retryable


@pytest.mark.parametrize('byok,model_specific',[(False,True),(True,False),(None,False)])
def test_shared_openrouter_upstream_throttle_is_distinct_from_account_limits(byok,model_specific):
    error=ProviderRequestError('openrouter','Provider returned error',status_code=429)
    annotate_provider_error(error,{'error':{'message':'Provider returned error','code':429,
        'metadata':{'provider_name':'ModelRun','is_byok':byok,'raw':'unretained upstream error'}}})
    failure=classify_provider_error(error)
    assert failure.kind=='rate_limit' and failure.model_specific is model_specific
    assert not hasattr(error,'raw')


@pytest.mark.asyncio
async def test_upstream_model_throttle_keeps_other_model_credential_usable_without_key_rotation(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch,max_attempts=1,fallback_routes=[])
    marked=[]
    result._credential_leases=lambda provider:[CredentialLease(provider,'one','First','synthetic',source='environment'),
                                             CredentialLease(provider,'two','Second','synthetic',source='environment')]
    monkeypatch.setattr(result.credential_pools,'mark_failure',lambda *args,**kwargs:marked.append(kwargs))
    calls=[]
    async def attempt(r,profile,lease,model,incoming,sampling,*args,**kwargs):
        calls.append((model,lease.credential_id))
        if model=='limited':
            error=ProviderRequestError('openrouter','upstream rate limit',status_code=429)
            annotate_provider_error(error,{'error':{'message':'Provider returned error','code':429,
                'metadata':{'provider_name':'ModelRun','is_byok':False}}})
            raise error
        kwargs['stream_diagnostics'].note_finish_reason('stop')
        yield 'available'
    monkeypatch.setattr(cloud,'call_cloud_once',attempt)
    with result.bind_model_route({**BACKUP,'model':'limited'}):
        with pytest.raises(ProviderRequestError):await consume(result)
    with result.bind_model_route({**BACKUP,'model':'other'}):
        assert await consume(result)==['available']
    assert calls==[('limited','one'),('other','one')] and marked==[]


@pytest.mark.asyncio
async def test_fallback_is_scoped_actual_route_and_portable_error_evidence(tmp_path, monkeypatch):
    result = router(tmp_path, monkeypatch)
    messages = [{"role":"user","content":"previous task"},
        {"role":"assistant","content":"", "tool_calls":[{"id":"call-one", "type":"function",
          "function":{"name":"ipython","arguments":'{"category":"build","code":"value=1"}'},
          "provider_replay":{"responses":{"reasoning_items":[{"type":"reasoning","encrypted_content":"opaque-primary"}]}}}]},
        {"role":"tool","tool_call_id":"call-one","content":"failed observation","is_error":True},
        {"role":"user","content":"continue"}]
    original, calls = deepcopy(messages), []
    async def attempt(r, profile, lease, model, incoming, sampling, *a, **kw):
        calls.append((model,deepcopy(incoming)))
        if model == "primary": raise ProviderRequestError("openai","outage",status_code=503)
        kw["stream_diagnostics"].note_finish_reason("stop")
        yield "recovered"
    monkeypatch.setattr(cloud, "call_cloud_once", attempt)
    with result.bind_model_route(PRIMARY):
        parts = [p async for p in result.stream(messages,sampling={"max_tokens":16},tools=[IPYTHON_PROVIDER_SPEC])]
        assert parts == ["recovered"]
        assert result.bound_model_route()["model"] == "backup"
        from session_projection import projection_model_route
        stored_route = projection_model_route(result,None,"chat")
        assert stored_route["provider"] == "openrouter" and stored_route["model"] == "backup"
        await consume(result)
        assert calls[-1][0] == "backup"
    assert result.cfg["cloud"]["provider"] == "openai" and result.bound_model_route() is None
    portable = next(incoming for model,incoming in calls if model == "backup")
    assert "provider_replay" not in portable[1]["tool_calls"][0]
    assert portable[1]["tool_calls"][0]["id"] == "call-one" and portable[2]["is_error"] is True
    assert messages == original


@pytest.mark.asyncio
async def test_budget_spans_oauth_key_alternatives_and_fallback_no_replay(tmp_path, monkeypatch):
    result = router(tmp_path, monkeypatch, max_attempts=4)
    result._credential_leases = lambda provider: [CredentialLease(provider,str(n),str(n),"synthetic",
                 source="oauth" if n == 0 else "environment") for n in range(5)]
    monkeypatch.setattr(result,"invalidate_oauth_access_token",lambda provider:None)
    calls = []
    async def attempt(r, profile, lease, model, *a, **kw):
        calls.append((model,lease.credential_id))
        raise ProviderRequestError(profile.name,"failure",status_code=401 if model=="primary" else 503)
        yield "never"
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    with result.bind_model_route(PRIMARY), pytest.raises(ProviderRequestError) as raised:
        await consume(result)
    assert calls == [("primary","0"),("primary","1"),("primary","2"),("backup","0")]
    assert raised.value.clean_turn_replay_safe is False and REQUEST_BUDGET.get() is None


@pytest.mark.asyncio
async def test_visible_partial_output_prevents_route_switch(tmp_path, monkeypatch):
    result = router(tmp_path, monkeypatch)
    calls, observed = [], []
    async def attempt(r, profile, lease, model, *a, **kw):
        calls.append(model)
        yield "partial"
        raise ProviderRequestError(profile.name,"disconnect",status_code=503)
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    with result.bind_model_route(PRIMARY), pytest.raises(ProviderRequestError) as raised:
        async for part in result.stream([{"role":"user","content":"task"}],tools=[IPYTHON_PROVIDER_SPEC]): observed.append(part)
    assert observed == ["partial"] and calls == ["primary"]
    assert raised.value.model_output_observed and not raised.value.clean_turn_replay_safe


@pytest.mark.asyncio
async def test_completed_refusal_and_http_filter_never_retry_or_switch(tmp_path, monkeypatch):
    result = router(tmp_path, monkeypatch)
    calls, health_failures = [], []
    monkeypatch.setattr(result.credential_pools,"mark_failure",lambda *a,**kw:health_failures.append(kw))
    async def filtered(r,profile,lease,model,*a,**kw):
        calls.append(model)
        error=ProviderRequestError(profile.name,"filter",status_code=503)
        error.provider_error_type="content_filter"
        raise error
        yield "never"
    monkeypatch.setattr(cloud,"call_cloud_once",filtered)
    with result.bind_model_route(PRIMARY),pytest.raises(ProviderRequestError): await consume(result)
    assert calls == ["primary"] and health_failures == []
    async def refused(r,profile,lease,model,*a,**kw):
        calls.append(model)
        kw["stream_diagnostics"].note_finish_reason("content_filter")
        yield "declined"
    monkeypatch.setattr(cloud,"call_cloud_once",refused)
    with result.bind_model_route(PRIMARY): assert await consume(result) == ["declined"]
    assert calls == ["primary","primary"]


@pytest.mark.asyncio
async def test_quota_cooldown_next_turn_and_credential_generation_fence(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch)
    calls=[]
    async def attempt(r,profile,lease,model,*a,**kw):
        calls.append(model)
        if model=="primary": raise ProviderRequestError(profile.name,"limited",status_code=429,retry_after_seconds=300)
        kw["stream_diagnostics"].note_finish_reason("stop")
        yield "ok"
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    with result.bind_model_route(PRIMARY): assert await consume(result)==["ok"]
    with result.bind_model_route(PRIMARY):
        assert result.bound_model_route()["model"]=="backup"
        assert await consume(result)==["ok"]
    assert calls==["primary","backup","backup"]
    old_key=result._recovery_cooldowns.key(result,PRIMARY)
    monkeypatch.setattr(result.credential_pools,"replace",lambda *a,**kw:{"saved":True})
    result.replace_cloud_credential("openai","new-synthetic-key")
    new_key=result._recovery_cooldowns.key(result,PRIMARY)
    assert old_key!=new_key and result._recovery_cooldowns.remaining(new_key)==0
    # A late failure from the old credential generation cannot bench the new one.
    result._recovery_cooldowns.failed(old_key,Failure("rate_limit",reset_at=time.time()+300))
    assert result._recovery_cooldowns.remaining(new_key)==0
    with result.bind_model_route(PRIMARY): assert result.bound_model_route()["model"]=="primary"


@pytest.mark.asyncio
async def test_auxiliary_routes_check_real_input_reserve_and_leave_main_bound(tmp_path,monkeypatch):
    cheap={**BACKUP,"model":"too-small"}
    secondary={**BACKUP,"model":"summary"}
    result=router(tmp_path,monkeypatch,auxiliary_routes={"internal_prose":[cheap,secondary]})
    monkeypatch.setattr("llm_recovery.context_limit_tokens",lambda r,route:32 if route["model"]=="too-small" else 100000)
    calls=[]
    async def attempt(r,profile,lease,model,*a,**kw):
        calls.append(model)
        kw["stream_diagnostics"].note_finish_reason("stop")
        yield "summary"
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    with result.bind_model_route(PRIMARY):
        prior=result.bound_model_route()
        assert await complete(result,[{"role":"user","content":"summarize"}],profile="internal_prose",require_complete=True)=="summary"
        assert result.bound_model_route()==prior
    assert calls==["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "max"])
async def test_explicit_auxiliary_effort_is_honored_without_changing_profile_defaults(tmp_path, monkeypatch, effort):
    auxiliary = {**BACKUP, "model": "summary"}
    if effort is not None:
        auxiliary["reasoning_effort"] = effort
    result = router(tmp_path, monkeypatch, auxiliary_routes={"internal_prose": [auxiliary]})
    observed = []
    async def attempt(r, profile, lease, model, *args, **kwargs):
        from model_runtime.request_policy import project_reasoning_policy
        payload = {}
        projected = project_reasoning_policy(r, profile, model, payload, args[4])
        observed.append((args[4], projected))
        kwargs["stream_diagnostics"].note_finish_reason("stop")
        yield "summary"
    monkeypatch.setattr(cloud, "call_cloud_once", attempt)
    with result.bind_model_route(PRIMARY):
        primary = result.bound_model_route()
        assert await complete(result, [{"role": "user", "content": "summarize"}], profile="internal_prose", require_complete=True) == "summary"
        assert result.bound_model_route() == primary
    expected = (None, effort) if effort else (0, "low")
    assert observed[0] == expected


@pytest.mark.asyncio
async def test_qualification_optout_suppresses_both_recovery_chains(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch,enabled=False)
    result.set_fallback_chain(["openrouter"])
    calls=[]
    async def attempt(r,profile,lease,model,*a,**kw):
        calls.append(profile.name)
        raise ProviderRequestError(profile.name,"unavailable",status_code=503)
        yield "never"
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    monkeypatch.setattr(cloud,"_cloud_retry_delay",lambda *a,**kw:None)
    with pytest.raises(ProviderRequestError): await consume(result,allow_recovery=False)
    assert calls==["openai"]
    sys.path.insert(0,str(Path(__file__).parents[2]/"experiments"/"live-canary"))
    from run_canary import configure_candidate_route
    cfg={"provider_recovery":{"enabled":True,"fallback_routes":[BACKUP]},"cloud":{"fallback_chain":["openrouter"]}}
    configure_candidate_route(cfg,"openai","primary","openai.chat_completions")
    assert cfg["provider_recovery"]["enabled"] is False and not cfg["cloud"].get("fallback_chain")


def test_wait_budget_reset_parser_and_trace_events(monkeypatch):
    budget=RequestBudget(max_attempts=2,max_wait_seconds=3)
    assert budget.admit_wait(2) and not budget.admit_wait(2)
    error=ProviderRequestError("openai","limited",status_code=429)
    reset=time.time()+200
    annotate_provider_error(error,{"error":{"type":"rate_limit_error","resets_at":reset}})
    assert classify_provider_error(error).reset_at==pytest.approx(reset)
    events=[]
    monkeypatch.setattr("observability.trace_events.record_trace_event",lambda event,**fields:events.append((event,fields)))
    from llm_recovery import _note
    _note("candidate_rejected",provider="openai",model="primary")
    assert events==[("provider:recovery",{"recovery_event":"candidate_rejected","provider":"openai","model":"primary"})]


@pytest.mark.asyncio
async def test_cancellation_during_retry_never_activates_backup(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch,max_wait_seconds=60)
    entered, calls=asyncio.Event(),[]
    real_sleep=asyncio.sleep
    async def wait(delay):
        entered.set()
        await real_sleep(60)
    async def attempt(r,profile,lease,model,*a,**kw):
        calls.append(model)
        raise ProviderRequestError(profile.name,"outage",status_code=503)
        yield "never"
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    monkeypatch.setattr(cloud.asyncio,"sleep",wait)
    with result.bind_model_route(PRIMARY):
        task=asyncio.create_task(consume(result))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert result.bound_model_route()["model"]=="primary"
    assert calls==["primary"] and REQUEST_BUDGET.get() is None


@pytest.mark.asyncio
async def test_emitted_local_tool_call_blocks_fallback(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch)
    monkeypatch.setattr(LLMRouter,"engine_ready",property(lambda self:True))
    monkeypatch.setattr(LLMRouter,"model_name",property(lambda self:"local-model"))
    calls=[]
    class Sink:
        def add_openai_delta(self,calls): pass
    async def local(messages,sampling,*a,**kw):
        calls.append("local")
        kw["tool_call_sink"].add_openai_delta([{"index":0,"id":"local-call","function":{"name":"ipython","arguments":"{}"}}])
        raise ProviderRequestError("local","disconnected",status_code=503)
        yield "never"
    async def backup(*a,**kw):
        calls.append("backup")
        yield "unsafe"
    monkeypatch.setattr(result,"_call_local",local)
    monkeypatch.setattr(cloud,"call_cloud_once",backup)
    with result.bind_model_route({"mode":"local","provider":"local","model":"local-model"}),pytest.raises(ProviderRequestError) as raised:
        await consume(result,tool_call_sink=Sink())
    assert calls==["local"] and raised.value.model_output_observed


@pytest.mark.asyncio
async def test_failed_private_reasoning_and_tools_are_not_blended(tmp_path,monkeypatch):
    import tool_calling
    result=router(tmp_path,monkeypatch)
    sink, reasoning=tool_calling.ToolCallAccumulator(),[]
    async def attempt(r,profile,lease,model,*a,**kw):
        a[-1]("discarded" if model=="primary" else "retained")
        kw["tool_call_sink"].add_openai_delta([{"index":0,"id":model+"-call","type":"function",
            "function":{"name":"ipython","arguments":'{"category":"build","code":"value=1"}'}}])
        if model=="primary": raise ProviderRequestError(profile.name,"outage",status_code=503)
        kw["stream_diagnostics"].note_finish_reason("tool_calls")
        yield "ready"
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    with result.bind_model_route(PRIMARY):
        assert await consume(result,tool_call_sink=sink,reasoning_sink=reasoning.append)==["ready"]
    assert [a["id"] for a in sink.actions()]==["backup-call"]
    assert "".join(reasoning)=="retained"


@pytest.mark.asyncio
async def test_concurrent_chat_scopes_do_not_swap_each_other(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch)
    async def attempt(r,profile,lease,model,messages,*a,**kw):
        await asyncio.sleep(0)
        if model=="primary" and messages[0]["content"]=="left":
            raise ProviderRequestError(profile.name,"outage",status_code=503)
        kw["stream_diagnostics"].note_finish_reason("stop")
        yield "ok"
    monkeypatch.setattr(cloud,"call_cloud_once",attempt)
    async def chat(text):
        with result.bind_model_route(PRIMARY):
            _=[p async for p in result.stream([{"role":"user","content":text}],tools=[IPYTHON_PROVIDER_SPEC])]
            return result.bound_model_route()["model"]
    assert await asyncio.gather(chat("left"),chat("right"))==["backup","primary"]
    assert result.cfg["cloud"]["provider"]=="openai"


def test_same_account_refresh_preserves_quota_but_reconnect_clears(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch)
    monkeypatch.setattr("security.secretstore.encrypt",lambda value:"encrypted-"+value)
    monkeypatch.setattr(result,"save_config",lambda **kw:True)
    route={"mode":"cloud","provider":"openai-codex","model":"gpt-5.6-luna"}
    result.set_oauth_tokens("openai-codex",access_token="synthetic",account_id="same",auth_flow="test",replace=True)
    key=result._recovery_cooldowns.key(result,route)
    result._recovery_cooldowns.failed(key,Failure("quota",reset_at=time.time()+300))
    result.set_oauth_tokens("openai-codex",access_token="refreshed",account_id="same",auth_flow="test")
    assert result._recovery_cooldowns.key(result,route)==key and result._recovery_cooldowns.remaining(key)>0
    result.set_oauth_tokens("openai-codex",access_token="reconnected",account_id="same",auth_flow="test",replace=True)
    assert result._recovery_cooldowns.key(result,route)!=key


def test_failed_recovery_configuration_save_does_not_claim_activation(tmp_path,monkeypatch):
    result=router(tmp_path,monkeypatch)
    previous=deepcopy(result.cfg["provider_recovery"])
    monkeypatch.setattr(result,"save_config",lambda **kw:False)
    with pytest.raises(OSError): result.configure_provider_recovery({"enabled":True,"max_attempts":12})
    assert result.cfg["provider_recovery"]==previous


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_enabled", [True, False])
async def test_actual_native_resume_preserves_effects_and_fences_backup_replay(tmp_path, monkeypatch, recovery_enabled):
    from agent_engine.presets import chat_task_default
    from agent_engine.runner import run_main_chat_task
    from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
    from agent_types import ToolBatchResult
    from llm_profiles import complete_turn
    from test_agent_engine import FakePorts

    first_router = router(tmp_path / "first", monkeypatch)
    attempts = []
    opaque = "opaque-backup-owned-item"
    async def provider(r, profile, lease, model, messages, *args, **kwargs):
        attempts.append((model, deepcopy(messages)))
        if any(message.get("role") == "tool" for message in messages):
            kwargs["stream_diagnostics"].note_finish_reason("stop")
            yield "done"
            return
        if model == "primary":
            raise ProviderRequestError("openai", "outage", status_code=503)
        kwargs["tool_call_sink"].add_openai_delta([{
            "index": 0, "id": "backup-call", "type": "function",
            "function": {"name": "ipython", "arguments": '{"category":"build","code":"value=1"}'},
            "provider_replay": {"responses": {"reasoning_items": [{"type": "reasoning", "encrypted_content": opaque}]}},
        }])
        kwargs["stream_diagnostics"].note_finish_reason("tool_calls")
        if False:
            yield ""
    monkeypatch.setattr(cloud, "call_cloud_once", provider)
    store = SQLiteRunSnapshotStore(str(tmp_path / "native.sqlite3"))
    config = chat_task_default().with_overrides(checkpoints=True)
    cursor, thread_id = None, None
    class SimulatedExit(BaseException):
        pass
    async def commit(completed, next_node, state):
        nonlocal cursor, thread_id
        thread_id = state["thread_id"]
        cursor = store.commit_boundary_sync(state, completed_node=completed, next_node=next_node,
                                             expected_head_sequence=cursor.sequence if cursor else None)
        if completed == "model_step" and next_node == "tool":
            raise SimulatedExit()
    first = FakePorts([])
    first_ports = first.build()
    async def stream_first(messages, limit, images):
        return await complete_turn(first_router, messages, max_tokens=16, tools=[IPYTHON_PROVIDER_SPEC])
    first_ports.loop.stream = stream_first
    with first_router.bind_model_route(PRIMARY), pytest.raises(SimulatedExit):
        await run_main_chat_task(config=config, text="do the task", base_system="system",
            full_tspec=[IPYTHON_PROVIDER_SPEC], convo_tail=[], images=[], is_resume=False,
            resume_snap=None, ports=first_ports, snapshot_store=store, commit=commit)
    saved = store.load_head_sync(thread_id)
    original = deepcopy(saved.state)
    assert saved.next_node == "tool" and saved.state["model_recovery"]["effective"]["model"] == "backup"
    assert opaque in json.dumps(saved.state["messages"]) and first.action_batches == []
    second_router = router(tmp_path / "second", monkeypatch, enabled=recovery_enabled)
    resumed = FakePorts([], results=[ToolBatchResult(executed=True, outcomes=[{
        "tool": "ipython", "call_id": "backup-call", "result": "effect completed",
        "model_result": "effect completed", "ok": True, "executed": True,
    }])])
    resumed_ports = resumed.build()
    async def stream_resumed(messages, limit, images):
        return await complete_turn(second_router, messages, max_tokens=16, tools=[IPYTHON_PROVIDER_SPEC])
    resumed_ports.loop.stream = stream_resumed
    with second_router.bind_model_route(PRIMARY):
        result = await run_main_chat_task(config=config, text="do the task", base_system="system",
            full_tspec=[IPYTHON_PROVIDER_SPEC], convo_tail=[], images=[], is_resume=True,
            resume_snap=saved.state, ports=resumed_ports, snapshot_store=store)
        assert second_router.bound_model_route()["model"] == ("backup" if recovery_enabled else "primary")
    assert result.loop_result.reply == "done" and len(resumed.action_batches) == 1
    assert len(attempts) == 3 and attempts[-1][0] == ("backup" if recovery_enabled else "primary")
    assert opaque not in json.dumps(attempts[-1][1])
    assert saved.state == original and store.load_cursor_sync(saved.cursor).state == original


@pytest.mark.asyncio
async def test_effective_route_promotion_is_visible_to_owning_node_task(tmp_path, monkeypatch):
    result = router(tmp_path, monkeypatch)
    async def provider(r, profile, lease, model, *args, **kwargs):
        if model == "primary":
            raise ProviderRequestError("openai", "outage", status_code=503)
        kwargs["stream_diagnostics"].note_finish_reason("stop")
        yield "ok"
    monkeypatch.setattr(cloud, "call_cloud_once", provider)
    with result.bind_model_route(PRIMARY):
        await asyncio.create_task(consume(result))
        assert result.bound_model_route()["model"] == "backup"
    assert result.bound_model_route() is None and result.cfg["cloud"]["provider"] == "openai"


@pytest.mark.asyncio
async def test_legacy_unbound_chain_reserves_attempts_for_its_configured_backup(tmp_path, monkeypatch):
    result = router(tmp_path, monkeypatch, enabled=False)
    result.cfg["cloud"]["fallback_chain"] = ["anthropic"]
    calls = []
    async def no_wait(seconds):
        pass
    async def provider(r, profile, lease, model, *args, **kwargs):
        calls.append(profile.name)
        if profile.name == "openai":
            raise ProviderRequestError("openai", "outage", status_code=503)
        kwargs["stream_diagnostics"].note_finish_reason("stop")
        yield "ok"
    monkeypatch.setattr(cloud.asyncio, "sleep", no_wait)
    monkeypatch.setattr(cloud, "call_cloud_once", provider)
    assert await consume(result) == ["ok"]
    assert calls == ["openai"] * 3 + ["anthropic"]
    assert REQUEST_BUDGET.get() is None and result.bound_model_route() is None


@pytest.mark.asyncio
async def test_cross_mode_effective_route_is_not_rebound_as_a_new_primary(tmp_path, monkeypatch):
    backup_two = {**BACKUP, "model": "backup-two"}
    result = router(tmp_path, monkeypatch, fallback_routes=[BACKUP, backup_two])
    monkeypatch.setattr(LLMRouter, "engine_ready", property(lambda self: True))
    monkeypatch.setattr(LLMRouter, "model_name", property(lambda self: "local-model"))
    calls, incoming = [], []
    async def local(*args, **kwargs):
        calls.append("local")
        raise ProviderRequestError("local", "outage", status_code=503)
        yield "never"
    async def provider(r, profile, lease, model, messages, *args, **kwargs):
        calls.append(model)
        incoming.append(deepcopy(messages))
        if calls.count("backup") > 1 and model == "backup":
            raise ProviderRequestError(profile.name, "outage", status_code=503)
        kwargs["stream_diagnostics"].note_finish_reason("stop")
        yield "ok"
    monkeypatch.setattr(result, "_call_local", local)
    monkeypatch.setattr(cloud, "call_cloud_once", provider)
    with result.bind_model_route({"mode": "local", "provider": "local", "model": "local-model"}):
        assert await consume(result) == ["ok"]
        assert result.bound_model_route()["model"] == "backup"
        messages = [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "previous", "type": "function", "function": {"name": "ipython", "arguments": "{}"},
            "provider_replay": {"responses": {"reasoning_items": [{"type": "reasoning", "encrypted_content": "opaque"}]}},
        }]}, {"role": "tool", "tool_call_id": "previous", "content": "completed"}]
        assert [token async for token in result.stream(messages, route="cloud", sampling={"max_tokens": 16},
                                                       tools=[IPYTHON_PROVIDER_SPEC])] == ["ok"]
        assert result.bound_model_route()["model"] == "backup-two"
    assert calls == ["local", "backup", "backup", "backup-two"]
    assert "encrypted_content" not in json.dumps(incoming)
