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
    assert bus.snapshot(manifest_id="mreq-a")["items"] == []
    assert traced[0]["run"]["session_id"] == "chat-a"
    assert traced[0]["usage"]["reasoning_tokens"] == 12
    assert ledger.get("mreq-a")["response"]["provider_generation_id"] == "gen-a"
    assert ledger.get("mreq-a")["usage"]["reported_fields"] == ["input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"]


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
