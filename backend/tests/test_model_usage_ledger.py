import json
import pytest

from model_runtime.usage_ledger import ModelUsageLedger


def request(identity="mreq-a", session="chat-a", goal="goal-a"):
    return {"manifest_id": identity, "logical_call_id": "call", "attempt": 1, "captured_at": 123.0,
        "run": {"session_id": session, "run_id": "run", "parent_run_id": "parent",
            "work_scope": {"goal_id": goal, "step_id": "step"}},
        "route": {"provider": "openrouter", "model": "model-a"},
        "generation": {"reasoning_effort": "max"}}


def test_request_accounting_survives_restart_and_keeps_only_allowlisted_metadata(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite3")
    value = request()
    value.update(prompt="private prompt", headers={"Authorization": "private token"})
    value["generation"]["private"] = "private body"
    ledger.record(value)
    ledger.patch_usage("mreq-a", {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140,
        "reasoning_tokens": 10, "reported_fields": ["input_tokens", "output_tokens", "reasoning_tokens"],
        "private": "private usage", "measurement": "provider_reported"})
    ledger.patch_response("mreq-a", {"provider_generation_id": "generation-a", "headers": "private response"})
    restored = ModelUsageLedger(tmp_path / "usage.sqlite3").get("mreq-a")
    assert restored["usage"]["reasoning_tokens"] == 10
    assert restored["metadata"]["reasoning_effort"] == "max"
    assert restored["response"]["provider_generation_id"] == "generation-a"
    assert "private" not in json.dumps(restored)


def test_duplicate_receipts_and_usage_corrections_do_not_double_count(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite3")
    ledger.record(request())
    ledger.record(request())
    ledger.patch_usage("mreq-a", {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "reasoning_tokens": 10})
    ledger.patch_usage("mreq-a", {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "reasoning_tokens": 20})
    totals = ledger.totals()
    assert totals["requests"] == 1 and totals["total_tokens"] == 150
    assert totals["reasoning_tokens"] == 20  # Subset, not added to total.


def test_token_rollups_use_exact_integer_arithmetic_beyond_float_precision(tmp_path):
    ledger=ModelUsageLedger(tmp_path/'usage.sqlite3')
    ledger.record(request())
    exact=2**53+1
    with ledger._connect() as conn:
        conn.execute("UPDATE model_usage_rollup_v2 SET total_tokens=?,total_tokens_known_requests=1 WHERE kind='all'",(exact,))
    assert ledger.totals()['total_tokens']==exact
    assert type(ledger.totals()['total_tokens']) is int


def test_unknown_usage_and_field_coverage_are_not_false_zeroes(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite3")
    ledger.record(request())
    assert ledger.totals()["input_tokens"] is None
    ledger.record(request("mreq-b"))
    ledger.patch_usage("mreq-b", {"input_tokens": 0, "output_tokens": 4, "total_tokens": 4})
    totals = ledger.totals()
    assert totals["requests"] == 2 and totals["usage_observed_requests"] == 1
    assert totals["input_tokens"] == 0 and totals["input_tokens_known_requests"] == 1
    assert totals["reasoning_tokens"] is None and totals["reasoning_tokens_known_requests"] == 0


def test_paging_and_attribution_include_distinct_physical_retries(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite3")
    ledger.record(request())
    retry = request("mreq-b")
    retry["attempt"] = 2
    ledger.record(retry)
    ledger.record(request("mreq-c", "chat-child", "goal-a"))
    ledger.record(request("mreq-d", "chat-other", "goal-other"))
    first = ledger.read(goal_id="goal-a", limit=2)
    second = ledger.read(goal_id="goal-a", after=first["next_cursor"], limit=2)
    assert [row["manifest_id"] for row in first["items"] + second["items"]] == ["mreq-a", "mreq-b", "mreq-c"]
    assert ledger.totals(session_id="chat-a")["requests"] == 2
    assert ledger.totals(goal_id="goal-a")["requests"] == 3


def test_real_request_manifest_carries_goal_scope_into_durable_accounting(tmp_path):
    from run_context import Variant1RunContext, bind_run_context
    from model_runtime.request_manifest import _run_identity
    from work_fabric.scope import WorkScope
    context = Variant1RunContext.create(source="chat", run_id="run", session_id="owner",
        work_scope=WorkScope(chat_id="owner", goal_id="goal-live", step_id="execute"))
    value = request()
    with bind_run_context(context):
        value['run'] = _run_identity()
    ledger = ModelUsageLedger(tmp_path / 'usage.sqlite3')
    ledger.record(value)
    assert ledger.totals(goal_id='goal-live')['requests'] == 1


@pytest.mark.asyncio
async def test_long_request_usage_and_generation_survive_receipt_eviction(tmp_path, monkeypatch):
    from llm_manifest_bus import ModelRequestManifestBus
    from llm_usage import normalize_manifest_usage
    from observability import trace_events
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite3")
    bus = ModelRequestManifestBus(maxlen=2, usage_ledger=ledger)
    for identity in ("mreq-a", "mreq-b", "mreq-c"):
        await bus.record(request(identity))
    traced = []
    monkeypatch.setattr(trace_events, "record_model_usage", lambda value: traced.append(value))
    bus.patch_response_metadata("mreq-a", {"provider_generation_id": "gen-a"})
    bus.patch_usage("mreq-a", normalize_manifest_usage("openrouter", raw_usage={
        "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 12}}))
    await bus.flush_ledger()
    assert bus.snapshot(manifest_id="mreq-a")["items"] == []
    assert traced[0]["run"]["session_id"] == "chat-a"
    assert traced[0]["usage"]["reasoning_tokens"] == 12
    assert ledger.get("mreq-a")["response"]["provider_generation_id"] == "gen-a"
    assert ledger.get("mreq-a")["usage"]["reported_fields"] == ["input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"]
    await bus.stop()


@pytest.mark.asyncio
async def test_stream_identity_is_coalesced_off_loop_and_usage_flushes_in_order(tmp_path):
    import asyncio
    import threading
    import time
    from llm_manifest_bus import ModelRequestManifestBus
    from llm_router import LLMRouter
    from model_runtime.request_manifest import observe_provider_chunk
    class SlowLedger(ModelUsageLedger):
        def patch_response(self, *args):
            writes.append(threading.get_ident())
            time.sleep(0.03)
            return super().patch_response(*args)
    writes = []
    ledger = SlowLedger(tmp_path/'usage.sqlite3')
    bus = ModelRequestManifestBus(maxlen=1, usage_ledger=ledger)
    router = object.__new__(LLMRouter); router._manifest_bus = bus
    await bus.record(request())
    # Eviction must not defeat coalescing for a long-running physical request.
    await bus.record(request('mreq-b'))
    for _ in range(1000):
        observe_provider_chunk(router, 'mreq-a', {'id':'gen-a', 'model':'returned-model'})
    observe_provider_chunk(router, 'mreq-b', {'id':'gen-b', 'model':'returned-model'})
    observe_provider_chunk(router, 'mreq-a', {'id':'gen-a', 'system_fingerprint':'new-fingerprint',
        'usage':{'completion_tokens':4}})
    router._patch_model_request_manifest_usage('mreq-a', {'input_tokens':10,'output_tokens':4,'total_tokens':14})
    router._patch_model_request_manifest_terminal('mreq-a', outcome='cancelled')
    progressed = asyncio.Event()
    asyncio.get_running_loop().call_later(0.01, progressed.set)
    await progressed.wait()
    assert len(writes) < 3, 'loop callback runs during the blocking ledger writes'
    await bus.flush_ledger()
    assert len(writes) == 3 and threading.get_ident() not in writes
    row = ledger.get('mreq-a')
    assert row['response']['provider_generation_id'] == 'gen-a'
    assert row['response']['system_fingerprint'] == 'new-fingerprint'
    assert ledger.get('mreq-b')['response']['provider_generation_id'] == 'gen-b'
    assert row['usage']['total_tokens'] == 14 and row['outcome'] == 'cancelled'
    assert ledger.totals()['total_tokens'] == 14
    assert bus.ledger_failures == 0
    await bus.stop()


@pytest.mark.asyncio
async def test_async_ledger_failure_and_overflow_are_visible():
    from llm_manifest_bus import ModelRequestManifestBus
    bus = ModelRequestManifestBus(usage_ledger=object())
    def fails():
        raise OSError('accounting unavailable')
    for _ in range(257):
        bus.submit_ledger(fails)
    assert bus.ledger_failures == 1
    await bus.flush_ledger()
    assert bus.ledger_failures == 257
    await bus.stop()


@pytest.mark.asyncio
async def test_ledger_boundary_does_not_wait_for_future_swarm_writes():
    import asyncio
    import threading
    from llm_manifest_bus import ModelRequestManifestBus
    gate = threading.Event()
    bus = ModelRequestManifestBus(usage_ledger=object())
    flushed = asyncio.create_task(bus.flush_ledger())
    # Start a writer before the fence is scheduled, then add a future write
    # after the fence. Queue.join would incorrectly wait for that future work.
    bus.submit_ledger(lambda:None)
    await asyncio.sleep(0)
    bus.submit_ledger(lambda:gate.wait(timeout=2))
    try:
        await asyncio.wait_for(flushed, timeout=1)
        assert not gate.is_set()
    finally:
        gate.set()
        await bus.stop()


@pytest.mark.asyncio
async def test_observation_failure_is_visible_without_breaking_inference_receipts():
    from llm_manifest_bus import ModelRequestManifestBus
    class FailedLedger:
        def record(self, value):
            raise OSError("unavailable")
    bus = ModelRequestManifestBus(usage_ledger=FailedLedger())
    await bus.record(request())
    assert len(bus.snapshot()["items"]) == 1
    assert bus.snapshot()["usage_ledger_failures"] == 1


def test_interrupted_stream_retains_generation_and_reported_partial_usage(tmp_path):
    from llm_router import LLMRouter
    from llm_manifest_bus import ModelRequestManifestBus
    from model_runtime.request_manifest import observe_provider_chunk
    ledger=ModelUsageLedger(tmp_path/'usage.sqlite3')
    ledger.record(request())
    router=object.__new__(LLMRouter)
    router._manifest_bus=ModelRequestManifestBus(usage_ledger=ledger)
    observe_provider_chunk(router,'mreq-a',{'id':'gen-observed','usage':{'completion_tokens':4,'completion_tokens_details':{'reasoning_tokens':3}}})
    ledger.patch_terminal('mreq-a',outcome='cancelled',duration_s=2)
    row=ledger.get('mreq-a')
    assert row['response']['provider_generation_id']=='gen-observed'
    assert row['usage']['output_tokens']==4 and row['usage']['input_tokens'] is None
    assert row['usage']['total_tokens'] is None and row['usage']['reasoning_tokens']==3
    assert row['outcome']=='cancelled'
    assert router._manifest_bus.snapshot()['usage_totals']['calls']==0


def test_logical_call_finalization_marks_only_last_attempt_duration_and_keeps_missing_usage(tmp_path):
    import model_runtime.request_manifest as manifests
    ledger=ModelUsageLedger(tmp_path/'usage.sqlite3')
    ledger.record(request())
    ledger.record(request('mreq-b'))
    class Router:
        def _patch_model_request_manifest_terminal(self,identity,**kwargs):
            ledger.patch_terminal(identity,**kwargs)
    router=Router()
    token=manifests.begin_model_call(requested_route='cloud',selected_mode='cloud')
    scope=manifests._MODEL_CALL_SCOPE.get()
    import time
    scope.requests.extend([(router,'mreq-a',time.monotonic()),(router,'mreq-b',time.monotonic())])
    manifests.end_model_call(token,outcome='failed')
    assert ledger.get('mreq-a')['outcome']=='superseded'
    assert ledger.get('mreq-a')['duration_s'] is None
    assert ledger.get('mreq-b')['outcome']=='failed'
    assert ledger.get('mreq-b')['usage'] is None
