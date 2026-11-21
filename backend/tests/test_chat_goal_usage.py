"""Request-time Goal attribution through the same hooks the provider adapters use."""
import json
from types import SimpleNamespace

import pytest

from chat_goals import ChatGoalService, ChatGoalStore
from llm_manifest_bus import ModelRequestManifestBus
from llm_router import LLMRouter
from model_runtime.request_manifest import begin_model_call, end_model_call, model_request_event_hooks
from run_context import Variant1RunContext, bind_run_context


def services(tmp_path):
    goals = ChatGoalService(SimpleNamespace(), ChatGoalStore(str(tmp_path / "work.sqlite")))
    router = object.__new__(LLMRouter)
    router.cfg = {}
    router._kn = lambda provider: provider
    router._cloud_usage = SimpleNamespace(record=lambda *args, **kwargs: {})
    router._manifest_bus = ModelRequestManifestBus()
    router.goal_usage_for_request = goals.request_usage
    return goals, router


async def request(router, context, provider="openrouter"):
    payload = {"model": "fixture", "messages": []}
    with bind_run_context(context):
        hooks = model_request_event_hooks(router, provider=provider, api_style="chat",
            transport="httpx", adapter="fixture", adapter_version="1", model="fixture",
            payload=payload, source_messages=[])
        await hooks["request"][0](SimpleNamespace(content=json.dumps(payload).encode(), headers={}))
    return hooks.request_ref


@pytest.mark.asyncio
async def test_request_identity_survives_replacement_and_late_corrected_receipts(tmp_path):
    goals, router = services(tmp_path)
    a, _ = goals.store.create("owner", "Original", token_budget=100)
    ctx = Variant1RunContext.create(source="chat", work_scope={"chat_id": "owner"})
    goals.admit_chat_turn(ctx, "owner", "admission-a", "peer:helper")
    assert ctx.work_scope.goal_id == a.goal_id
    ref = await request(router, ctx)
    b, _ = goals.store.create("owner", "Replacement", token_budget=50, replace=True, expected_goal_id=a.goal_id)
    for n in (30, 30, 33):
        # A receipt may be delivered outside the original run context.
        router._record_usage("openrouter", manifest_ref=ref, raw_usage={"prompt_tokens": n, "completion_tokens": 0})
    assert goals.store.get(a.goal_id).tokens_used == 33
    assert goals.store.current("owner").tokens_used == 0
    assert goals.store.current("owner").goal_id == b.goal_id
    # Work from the old admission cannot acquire the replacement's budget.
    later = await request(router, ctx)
    assert later.goal_usage is None


@pytest.mark.asyncio
async def test_child_usage_follows_original_parent_and_partial_settles_on_cancel(tmp_path):
    goals, router = services(tmp_path)
    parent, _ = goals.store.create("parent", "Task", token_budget=50)
    ctx = Variant1RunContext.create(source="subagent", work_scope={"chat_id": "child", "goal_id": parent.goal_id})
    call = begin_model_call(requested_route="cloud", selected_mode="cloud")
    ref = await request(router, ctx)
    router._patch_model_request_manifest_partial_usage(ref, {"completion_tokens": 6})
    router._patch_model_request_manifest_partial_usage(ref, {"completion_tokens": 7})
    goals.store.transition(parent.goal_id, "paused")
    end_model_call(call, outcome="cancelled")
    assert goals.store.get(parent.goal_id).tokens_used == 7
    assert goals.store.current("child") is None
    assert (await request(router, ctx)).goal_usage is None
    router._record_usage("openrouter", manifest_ref=ref,
                         raw_usage={"prompt_tokens": 8, "completion_tokens": 7})
    router._patch_model_request_manifest_terminal(ref, outcome="cancelled")
    assert goals.store.get(parent.goal_id).tokens_used == 15
    assert goals.store.get(parent.goal_id).status == "paused"


@pytest.mark.asyncio
async def test_split_cache_buckets_reach_budget_once_and_a_correction_is_resumable(tmp_path):
    goals, router = services(tmp_path)
    g, _ = goals.store.create("owner", "Bounded", token_budget=30)
    ctx = Variant1RunContext.create(source="chat", work_scope={"chat_id": "owner", "goal_id": g.goal_id})
    ref = await request(router, ctx, "anthropic")
    raw = {"input_tokens": 20, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 7, "output_tokens": 4}
    router._record_usage("anthropic", manifest_ref=ref, raw_usage=raw)
    router._record_usage("anthropic", manifest_ref=ref, raw_usage=raw)
    assert goals.store.current("owner").tokens_used == 31
    assert goals.store.current("owner").status == "budget_limited"
    assert len(goals.take_notes("owner", g.goal_id)) == 1
    router._record_usage("anthropic", manifest_ref=ref, raw_usage={**raw, "input_tokens": 17})
    assert goals.store.current("owner").tokens_used == 28
    assert goals.store.current("owner").status == "paused"


@pytest.mark.asyncio
async def test_sparse_final_and_explicit_zero_corrections_keep_reported_components(tmp_path):
    goals, router = services(tmp_path)
    goal, _ = goals.store.create("owner", "Task")
    ctx = Variant1RunContext.create(source="chat", work_scope={"chat_id": "owner", "goal_id": goal.goal_id})
    ref = await request(router, ctx)
    router._patch_model_request_manifest_partial_usage(ref, {"prompt_tokens": 10})
    router._record_usage("openrouter", manifest_ref=ref, raw_usage={"completion_tokens": 5})
    assert goals.store.get(goal.goal_id).tokens_used == 15
    router._record_usage("openrouter", manifest_ref=ref, raw_usage={"prompt_tokens": 0})
    assert goals.store.get(goal.goal_id).tokens_used == 5
