import sys
from datetime import datetime, timezone
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from observability.cloud_usage import CloudUsageTelemetry


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc).timestamp()


def test_usage_persists_exact_estimated_and_unpriced_costs(tmp_path):
    path = tmp_path / "cloud_usage.json"
    usage = CloudUsageTelemetry(str(path), wall_clock=lambda: NOW)
    usage.record("gemini", "gemini-3.5-flash", 1000, 500, 1500)
    usage.record("xai", "grok-4.3", 100, 50, 150,
                 raw_usage={"cost_in_usd_ticks": 100_000_000})
    usage.record("nvidia", "custom-model", 800, 200, 1000)

    snapshot = usage.snapshot(budget_usd=10, active_provider="gemini")

    assert snapshot["total"]["calls"] == 3
    assert snapshot["total"]["cost_usd"] == 0.016
    assert snapshot["total"]["cost_status"] == "partial"
    assert snapshot["total"]["remaining_budget_usd"] == 9.984
    assert snapshot["providers"]["gemini"]["cost_status"] == "estimated"
    assert snapshot["providers"]["xai"]["cost_status"] == "exact"
    assert snapshot["providers"]["nvidia"]["cost_status"] == "unavailable"

    restored = CloudUsageTelemetry(str(path), wall_clock=lambda: NOW).snapshot()
    assert restored["total"]["calls"] == 3
    assert restored["providers"]["gemini"]["total_tokens"] == 1500


def test_observed_rate_limit_headers_are_exact_and_do_not_count_as_calls(tmp_path):
    usage = CloudUsageTelemetry(str(tmp_path / "usage.json"), wall_clock=lambda: NOW)
    usage.observe_response("openai", "gpt-5.4", 200, {
        "x-ratelimit-limit-requests": "500",
        "x-ratelimit-remaining-requests": "318",
        "x-ratelimit-reset-requests": "38s",
        "x-ratelimit-limit-tokens": "90,000",
        "x-ratelimit-remaining-tokens": "54,200",
        "x-ratelimit-reset-tokens": "1s",
    })

    snapshot = usage.snapshot(active_provider="openai")
    provider = snapshot["providers"]["openai"]
    assert provider["calls"] == 0
    assert provider["limits"]["health"] == "operational"
    assert provider["limits"]["requests"] == {
        "limit": 500, "remaining": 318, "reset": "38s", "used": 182}
    assert provider["limits"]["tokens"]["limit"] == 90000
    assert provider["limits"]["tokens"]["used"] == 35800


def test_anthropic_headers_and_throttled_health(tmp_path):
    usage = CloudUsageTelemetry(str(tmp_path / "usage.json"), wall_clock=lambda: NOW)
    usage.observe_response("anthropic", "claude-sonnet-4-6", 429, {
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-requests-remaining": "0",
        "anthropic-ratelimit-requests-reset": "2026-07-17T12:01:00Z",
        "retry-after": "12",
    }, error="rate limited")

    limit = usage.snapshot(active_provider="anthropic")["providers"]["anthropic"]["limits"]
    assert limit["health"] == "throttled"
    assert limit["retry_after"] == "12"
    assert limit["requests"]["used"] == 50


def test_model_usage_returns_trailing_30_calendar_days_with_model_totals(tmp_path):
    current = [datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc).timestamp()]
    usage = CloudUsageTelemetry(str(tmp_path / "usage.json"), wall_clock=lambda: current[0])
    usage.record("openai", "old-model", 10, 5, 15, inference_time_s=1.0)

    current[0] = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc).timestamp()
    usage.record("local", "Qwen3-14B", 100, 50, 150, inference_time_s=2.5)
    current[0] = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc).timestamp()
    usage.record("openai", "gpt-5.4", 200, 75, 275, inference_time_s=4.25)

    snapshot = usage.model_usage_snapshot(days=30)

    assert snapshot["start"] == "2026-06-18"
    assert snapshot["end"] == "2026-07-17"
    assert len(snapshot["daily"]) == 30
    assert snapshot["daily"][0]["models"][0]["model"] == "Qwen3-14B"
    assert snapshot["daily"][-1]["models"][0]["model"] == "gpt-5.4"
    assert [row["model"] for row in snapshot["models"]] == ["gpt-5.4", "Qwen3-14B"]
    totals = snapshot["totals"]
    assert totals["requests"] == 2
    assert totals["successful_requests"] == 2
    assert totals["failed_requests"] == 0
    assert totals["cancelled_requests"] == 0
    assert totals["success_rate"] == 100.0
    assert totals["prompt_tokens"] == 300
    assert totals["completion_tokens"] == 125
    assert totals["tokens"] == 425
    assert totals["inference_time_s"] == 6.75
    assert totals["timed_calls"] == 2
    assert totals["avg_latency_ms"] == 3375.0
    assert totals["p50_latency_ms"] == 2500.0
    assert totals["p95_latency_ms"] == 4250.0
    assert totals["cost_usd"] == 0.001625
    assert totals["cost_status"] == "partial"
    assert totals["local_requests"] == 1
    assert totals["cloud_requests"] == 1
    assert snapshot["hourly_available"] is True
    assert len(snapshot["hourly_activity"]) == 7
    assert len(snapshot["hourly_pattern"]) == 24


def test_model_usage_marks_legacy_runtime_as_unavailable_and_cost_snapshot_excludes_local(tmp_path):
    usage = CloudUsageTelemetry(str(tmp_path / "usage.json"), wall_clock=lambda: NOW)
    usage.record("local", "local.gguf", 20, 10, 30)
    usage.record("anthropic", "claude-sonnet-4-6", 40, 20, 60,
                 inference_time_s=3.0)

    models = usage.model_usage_snapshot()["models"]
    local = next(row for row in models if row["provider"] == "local")
    assert local["inference_time_s"] == 0
    assert local["timed_calls"] == 0
    assert "local" not in usage.snapshot()["providers"]
    assert usage.snapshot()["total"]["calls"] == 1


def test_xai_cached_prompt_tokens_are_recorded(tmp_path):
    usage = CloudUsageTelemetry(str(tmp_path / "usage.json"), wall_clock=lambda: NOW)
    event = usage.record(
        "xai", "grok-4.3", 1200, 80, 1280,
        raw_usage={
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 900},
            "cost_in_usd_ticks": 50_000_000,
        },
    )
    assert event["cached_prompt_tokens"] == 900
    snap = usage.snapshot(active_provider="xai")
    row = snap["providers"]["xai"]
    assert row["cached_prompt_tokens"] == 900
    assert row["cache_hit_calls"] == 1
    assert row["models"]["grok-4.3"]["cached_prompt_tokens"] == 900


def test_responses_usage_records_cache_reasoning_and_uncached_input(tmp_path):
    usage = CloudUsageTelemetry(str(tmp_path / "usage.json"), wall_clock=lambda: NOW)
    event = usage.record(
        "openai-codex", "gpt-5.6-luna", 12_000, 900, 12_900,
        raw_usage={
            "input_tokens": 12_000,
            "output_tokens": 900,
            "total_tokens": 12_900,
            "input_tokens_details": {"cached_tokens": 9_000},
            "output_tokens_details": {"reasoning_tokens": 650},
        },
    )

    assert event["prompt_tokens"] == 12_000
    assert event["cached_prompt_tokens"] == 9_000
    assert event["uncached_prompt_tokens"] == 3_000
    assert event["reasoning_tokens"] == 650
    assert event["cache_share"] == 0.75
    row = usage.model_usage_snapshot()["models"][0]
    assert row["cached_prompt_tokens"] == 9_000
    assert row["uncached_prompt_tokens"] == 3_000
    assert row["reasoning_tokens"] == 650
    assert row["cache_share"] == 0.75


def test_model_usage_tracks_outcomes_percentiles_and_rolling_activity(tmp_path):
    current = [datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc).timestamp()]
    usage = CloudUsageTelemetry(str(tmp_path / "usage.json"), wall_clock=lambda: current[0])
    usage.record(
        "local", "Qwen3-14B", 100, 50, 150,
        inference_time_s=1.0, runtime_id="llamacpp",
        ttft_ms=200, prefill_tps=120, generation_tps=48,
    )
    current[0] = datetime(2026, 7, 16, 11, 0, tzinfo=timezone.utc).timestamp()
    usage.record_outcome("local", "Qwen3-14B", status="error", runtime_id="llamacpp")
    current[0] = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc).timestamp()
    usage.record_outcome("local", "Qwen3-14B", status="cancelled", runtime_id="llamacpp")
    current[0] = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc).timestamp()
    usage.record(
        "local", "Qwen3-14B", 200, 100, 300,
        inference_time_s=2.0, runtime_id="llamacpp",
        ttft_ms=400, prefill_tps=180, generation_tps=72,
    )

    snapshot = usage.model_usage_snapshot(days=30)
    model = snapshot["models"][0]
    assert model["requests"] == 4
    assert model["successful_requests"] == 2
    assert model["failed_requests"] == 1
    assert model["cancelled_requests"] == 1
    assert model["success_rate"] == 66.67
    assert model["avg_latency_ms"] == 1500.0
    assert model["p50_latency_ms"] == 1000.0
    assert model["p95_latency_ms"] == 2000.0
    assert model["avg_ttft_ms"] == 300.0
    assert model["p95_ttft_ms"] == 400.0
    assert model["prefill_tps"] == 150.0
    assert model["generation_tps"] == 60.0
    assert model["avg_prompt_tokens"] == 150.0
    assert model["avg_completion_tokens"] == 75.0
    assert model["p50_tokens"] == 150.0
    assert model["p95_tokens"] == 300.0
    assert snapshot["recent_activity"] == {
        "last_24h_requests": 3,
        "prev_24h_requests": 1,
        "last_24h_tokens": 300,
        "change_24h_pct": 200.0,
    }
    assert snapshot["peak_hours"][0]["hour"] == 10
    assert snapshot["peak_hours"][0]["requests"] == 2
