"""Persistent cloud API usage, cost estimates, and observed rate limits.

Token counts and response-header limits are provider-reported measurements.
Responses, Chat Completions, Anthropic, and Gemini cache/reasoning buckets are
normalized into full prompt volume, cached input, uncached input, cache writes,
and reasoning. xAI can additionally return exact billed cost per response.
Other costs are estimates from a small, dated standard-price catalog (or a user
override); unknown models remain explicitly unpriced.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
import json
import math
import os
import threading
import time

from llm_usage import normalize_manifest_usage


# USD per 1M tokens. Verified against provider docs on 2026-07-17.
# These are standard synchronous rates; account discounts/free tiers cannot be
# inferred from an ordinary API key, so non-xAI values are always "estimated".
STANDARD_PRICING = {
    ("gemini", "gemini-3.5-flash"): (1.50, 9.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    ("anthropic", "claude-sonnet-4-5"): (3.00, 15.00),
    ("openai", "gpt-5.4"): (2.50, 15.00),
    ("openai", "gpt-5.2"): (1.75, 14.00),
    ("xai", "grok-4.3"): (1.25, 2.50),
}

PROVIDER_NAMES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google AI",
    "xai": "xAI", "nvidia": "NVIDIA NIM", "local": "Local",
}

PERFORMANCE_SAMPLE_LIMIT = 256
PERFORMANCE_SAMPLE_FIELDS = (
    "latency_ms_samples",
    "ttft_ms_samples",
    "prefill_tps_samples",
    "generation_tps_samples",
    "tokens_per_request_samples",
)


def _number(value, default=0.0):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _integer(value):
    try:
        return max(0, int(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _finite_non_negative(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _percentile(values, quantile):
    usable = sorted(
        number for value in (values or [])
        if (number := _finite_non_negative(value)) is not None
    )
    if not usable:
        return None
    index = max(0, min(len(usable) - 1, math.ceil(len(usable) * quantile) - 1))
    return usable[index]


def _average(values):
    usable = [
        number for value in (values or [])
        if (number := _finite_non_negative(value)) is not None
    ]
    return sum(usable) / len(usable) if usable else None


def _change_pct(current, previous):
    current = _number(current)
    previous = _number(previous)
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100.0, 2)


def _safe_count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _extend_samples(target, source):
    for field in PERFORMANCE_SAMPLE_FIELDS:
        values = source.get(field) if isinstance(source, dict) else None
        if not isinstance(values, list):
            continue
        target.setdefault(field, []).extend(
            number for value in values
            if (number := _finite_non_negative(value)) is not None
        )


def _finalize_performance(row):
    successful = _safe_count(row.get("successful_requests", row.get("calls")))
    failed = _safe_count(row.get("failed_requests", row.get("failed_calls")))
    cancelled = _safe_count(row.get("cancelled_requests", row.get("cancelled_calls")))
    attempts = successful + failed + cancelled
    decided = successful + failed
    row["requests"] = attempts
    row["successful_requests"] = successful
    row["failed_requests"] = failed
    row["cancelled_requests"] = cancelled
    row["success_rate"] = round(successful / decided * 100.0, 2) if decided else 0.0
    row["failure_rate"] = round(failed / decided * 100.0, 2) if decided else 0.0

    latency = row.get("latency_ms_samples") or []
    ttft = row.get("ttft_ms_samples") or []
    prefill = row.get("prefill_tps_samples") or []
    generation = row.get("generation_tps_samples") or []
    token_samples = row.get("tokens_per_request_samples") or []
    average_latency = _average(latency)
    if average_latency is None and _safe_count(row.get("timed_calls")):
        average_latency = (
            _number(row.get("inference_time_s"))
            / _safe_count(row.get("timed_calls")) * 1000.0
        )
    row["avg_latency_ms"] = round(average_latency, 2) if average_latency is not None else None
    row["p50_latency_ms"] = _rounded_percentile(latency, .50)
    row["p95_latency_ms"] = _rounded_percentile(latency, .95)
    row["p99_latency_ms"] = _rounded_percentile(latency, .99)
    row["avg_ttft_ms"] = _rounded_average(ttft)
    row["p50_ttft_ms"] = _rounded_percentile(ttft, .50)
    row["p95_ttft_ms"] = _rounded_percentile(ttft, .95)
    row["p99_ttft_ms"] = _rounded_percentile(ttft, .99)
    row["prefill_tps"] = _rounded_average(prefill)
    row["generation_tps"] = _rounded_average(generation)
    row["avg_tokens"] = round(_number(row.get("tokens")) / successful, 2) if successful else 0.0
    row["avg_prompt_tokens"] = round(
        _number(row.get("prompt_tokens")) / successful, 2) if successful else 0.0
    row["avg_completion_tokens"] = round(
        _number(row.get("completion_tokens")) / successful, 2) if successful else 0.0
    row["avg_reasoning_tokens"] = round(
        _number(row.get("reasoning_tokens")) / successful, 2) if successful else 0.0
    prompt_volume = _number(row.get("prompt_tokens"))
    cached = _number(row.get("cached_prompt_tokens"))
    if "uncached_prompt_tokens" not in row:
        row["uncached_prompt_tokens"] = max(0, int(prompt_volume - cached))
    row["cache_share"] = round(cached / prompt_volume, 8) if prompt_volume else 0.0
    row["p50_tokens"] = _rounded_percentile(token_samples, .50) or 0.0
    row["p95_tokens"] = _rounded_percentile(token_samples, .95) or 0.0
    row["max_tokens"] = round(max(token_samples), 2) if token_samples else 0.0
    row["performance_samples"] = len(latency)
    for field in PERFORMANCE_SAMPLE_FIELDS:
        row.pop(field, None)
    row.pop("calls", None)
    row.pop("failed_calls", None)
    row.pop("cancelled_calls", None)
    return row


def _rounded_average(values):
    value = _average(values)
    return round(value, 2) if value is not None else None


def _rounded_percentile(values, quantile):
    value = _percentile(values, quantile)
    return round(value, 2) if value is not None else None


class CloudUsageTelemetry:
    def __init__(self, path=None, *, wall_clock=time.time):
        self.path = path
        self._clock = wall_clock
        self._lock = threading.Lock()
        self._state = {"version": 3, "days": {}, "limits": {}}
        self._load()

    def record(self, provider, model, prompt_tokens=0, completion_tokens=0,
               total_tokens=None, *, raw_usage=None, custom_pricing=None,
               inference_time_s=None, runtime_id="", latency_ms=None,
               ttft_ms=None, prefill_tps=None, generation_tps=None):
        provider = str(provider or "unknown").lower()
        model = str(model or "unknown")
        raw_prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
        normalized = normalize_manifest_usage(
            provider,
            raw_prompt,
            completion,
            total_tokens,
            raw_usage=raw_usage,
        )
        prompt = max(0, int(normalized.get("prompt_token_volume") or raw_prompt))
        uncached = max(0, int(normalized.get("uncached_input_tokens") or 0))
        cached = max(0, int(normalized.get("cached_input_tokens") or 0))
        cache_write = max(0, int(normalized.get("cache_write_input_tokens") or 0))
        reasoning = max(0, int(normalized.get("reasoning_tokens") or 0))
        reported_total = max(0, int(normalized.get("total_tokens") or 0))
        total = max(
            reported_total,
            int(normalized.get("token_volume") or 0),
            prompt + completion,
        )
        timed = inference_time_s is not None
        runtime = _number(inference_time_s) if timed else 0.0
        exact_ticks = raw_usage.get("cost_in_usd_ticks")
        cost = None
        cost_kind = "unpriced"
        if exact_ticks is not None:
            cost = _number(exact_ticks) / 10_000_000_000
            cost_kind = "exact"
        else:
            price = self._price_for(provider, model, custom_pricing)
            if price:
                cost = prompt / 1_000_000 * price[0] + completion / 1_000_000 * price[1]
                cost_kind = "estimated"

        now_timestamp = self._clock()
        now = datetime.fromtimestamp(now_timestamp, tz=timezone.utc)
        day_key = now.date().isoformat()
        measured_latency = _finite_non_negative(latency_ms)
        if measured_latency is None and timed:
            measured_latency = runtime * 1000.0
        measured_ttft = _finite_non_negative(
            ttft_ms if ttft_ms is not None else raw_usage.get("ttft_ms"))
        measured_prefill = _finite_non_negative(
            prefill_tps if prefill_tps is not None else raw_usage.get("prefill_tps"))
        measured_generation = _finite_non_negative(
            generation_tps if generation_tps is not None else raw_usage.get("generation_tps"))
        with self._lock:
            day = self._state["days"].setdefault(day_key, {"providers": {}, "hours": {}})
            day.setdefault("hours", {})
            row = day["providers"].setdefault(provider, self._empty_row())
            model_row = row["models"].setdefault(model, self._empty_model())
            if runtime_id:
                model_row["runtime_id"] = str(runtime_id)
                row["last_runtime_id"] = str(runtime_id)
            for target in (row, model_row):
                target["calls"] = target.get("calls", 0) + 1
                target["prompt_tokens"] = target.get("prompt_tokens", 0) + prompt
                target["completion_tokens"] = target.get("completion_tokens", 0) + completion
                target["total_tokens"] = target.get("total_tokens", 0) + total
                target["uncached_prompt_tokens"] = (
                    target.get("uncached_prompt_tokens", 0) + uncached
                )
                target["reasoning_tokens"] = (
                    target.get("reasoning_tokens", 0) + reasoning
                )
                target["cache_write_prompt_tokens"] = (
                    target.get("cache_write_prompt_tokens", 0) + cache_write
                )
                if cached:
                    target["cached_prompt_tokens"] = target.get("cached_prompt_tokens", 0) + cached
                    target["cache_hit_calls"] = target.get("cache_hit_calls", 0) + 1
                target[f"{cost_kind}_calls"] = target.get(f"{cost_kind}_calls", 0) + 1
                if cost is not None:
                    target["cost_usd"] = target.get("cost_usd", 0) + cost
                if timed:
                    target["inference_time_s"] = target.get("inference_time_s", 0) + runtime
                    target["timed_calls"] = target.get("timed_calls", 0) + 1
            self._append_performance(model_row, "tokens_per_request_samples", total)
            self._append_performance(model_row, "latency_ms_samples", measured_latency)
            self._append_performance(model_row, "ttft_ms_samples", measured_ttft)
            self._append_performance(model_row, "prefill_tps_samples", measured_prefill)
            self._append_performance(model_row, "generation_tps_samples", measured_generation)
            hour = day["hours"].setdefault(f"{now.hour:02d}", self._empty_hour())
            hour["requests"] += 1
            hour["successful"] += 1
            hour["prompt_tokens"] += prompt
            hour["completion_tokens"] += completion
            hour["tokens"] += total
            hour["cached_prompt_tokens"] += cached
            hour["uncached_prompt_tokens"] += uncached
            hour["reasoning_tokens"] += reasoning
            hour["cache_write_prompt_tokens"] += cache_write
            hour["inference_time_s"] += runtime
            if cost is not None:
                hour["cost_usd"] += cost
            row["last_model"] = model
            row["last_request_at"] = now_timestamp
            self._prune(now)
            self._save_locked()
        return {
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cached_prompt_tokens": cached,
            "uncached_prompt_tokens": uncached,
            "reasoning_tokens": reasoning,
            "cache_write_prompt_tokens": cache_write,
            "cache_share": (
                round(cached / prompt, 8) if prompt > 0 else 0.0
            ),
            "cost_usd": cost,
            "cost_kind": cost_kind,
            "inference_time_s": runtime if timed else None,
            "timed": timed,
            "runtime_id": str(runtime_id or ""),
        }

    def record_outcome(self, provider, model, *, status="error", latency_ms=None,
                       runtime_id=""):
        """Persist a failed or cancelled provider attempt without fabricating usage."""
        provider = str(provider or "unknown").lower()
        model = str(model or "unknown")
        outcome = "cancelled_calls" if str(status).lower() == "cancelled" else "failed_calls"
        now_timestamp = self._clock()
        now = datetime.fromtimestamp(now_timestamp, tz=timezone.utc)
        day_key = now.date().isoformat()
        with self._lock:
            day = self._state["days"].setdefault(day_key, {"providers": {}, "hours": {}})
            day.setdefault("hours", {})
            row = day["providers"].setdefault(provider, self._empty_row())
            model_row = row["models"].setdefault(model, self._empty_model())
            if runtime_id:
                model_row["runtime_id"] = str(runtime_id)
                row["last_runtime_id"] = str(runtime_id)
            for target in (row, model_row):
                target[outcome] = target.get(outcome, 0) + 1
            hour = day["hours"].setdefault(f"{now.hour:02d}", self._empty_hour())
            hour["requests"] += 1
            hour["cancelled" if outcome == "cancelled_calls" else "failed"] += 1
            row["last_model"] = model
            row["last_request_at"] = now_timestamp
            self._prune(now)
            self._save_locked()
        return {
            "provider": provider,
            "model": model,
            "status": "cancelled" if outcome == "cancelled_calls" else "failed",
            "latency_ms": _finite_non_negative(latency_ms),
            "runtime_id": str(runtime_id or ""),
        }

    def observe_performance(self, provider, model, *, latency_ms=None, ttft_ms=None,
                            prefill_tps=None, generation_tps=None):
        """Attach late-arriving measurements to an already-counted successful call."""
        samples = {
            "latency_ms_samples": latency_ms,
            "ttft_ms_samples": ttft_ms,
            "prefill_tps_samples": prefill_tps,
            "generation_tps_samples": generation_tps,
        }
        if not any(_finite_non_negative(value) is not None for value in samples.values()):
            return False
        provider = str(provider or "unknown").lower()
        model = str(model or "unknown")
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        with self._lock:
            day = self._state["days"].get(now.date().isoformat()) or {}
            row = (day.get("providers") or {}).get(provider) or {}
            model_row = (row.get("models") or {}).get(model)
            if not isinstance(model_row, dict) or not model_row.get("calls"):
                return False
            for field, value in samples.items():
                self._append_performance(model_row, field, value)
            self._save_locked()
        return True

    def observe_response(self, provider, model, status, headers=None, error=""):
        provider = str(provider or "unknown").lower()
        headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
        if provider == "anthropic":
            limits = {
                "requests": self._limit(headers, "anthropic-ratelimit-requests"),
                "tokens": self._limit(headers, "anthropic-ratelimit-tokens"),
            }
        else:
            limits = {
                "requests": self._limit(headers, "x-ratelimit", kind="requests"),
                "tokens": self._limit(headers, "x-ratelimit", kind="tokens"),
            }
        status = int(status or 0)
        health = "operational" if 200 <= status < 400 else "throttled" if status == 429 else "error"
        with self._lock:
            self._state["limits"][provider] = {
                "model": str(model or ""), "status_code": status,
                "health": health, "error": str(error or "")[:160],
                "requests": limits["requests"], "tokens": limits["tokens"],
                "retry_after": headers.get("retry-after", ""),
                "updated_at": self._clock(),
            }
            self._save_locked()

    def snapshot(self, *, budget_usd=None, active_provider="") -> dict:
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        month_key = now.strftime("%Y-%m")
        today_key = now.date().isoformat()
        with self._lock:
            days = json.loads(json.dumps(self._state.get("days", {})))
            limits = json.loads(json.dumps(self._state.get("limits", {})))
        providers = {}
        today_cost = 0.0
        today_known = False
        for day_key, day in days.items():
            if not day_key.startswith(month_key):
                continue
            for provider, source in (day.get("providers") or {}).items():
                if provider == "local":
                    continue
                target = providers.setdefault(provider, self._empty_row())
                self._merge_row(target, source)
                if day_key == today_key and source.get("exact_calls", 0) + source.get("estimated_calls", 0):
                    today_cost += _number(source.get("cost_usd"))
                    today_known = True
        for provider, limit in limits.items():
            providers.setdefault(provider, self._empty_row())["limits"] = limit
        if active_provider:
            providers.setdefault(str(active_provider).lower(), self._empty_row())

        total = self._empty_row()
        total.pop("models", None)
        for provider, row in providers.items():
            row["name"] = PROVIDER_NAMES.get(provider, provider.replace("_", " ").title())
            row.setdefault("limits", limits.get(provider, {}))
            self._merge_totals(total, row)
            row["cost_status"] = self._cost_status(row)
            row["cost_usd"] = round(_number(row.get("cost_usd")), 8)
            prompt_volume = _number(row.get("prompt_tokens"))
            cached = _number(row.get("cached_prompt_tokens"))
            row["cache_share"] = (
                round(cached / prompt_volume, 8) if prompt_volume else 0.0
            )

        total["cost_status"] = self._cost_status(total)
        total["cost_usd"] = round(_number(total.get("cost_usd")), 8)
        total_prompt = _number(total.get("prompt_tokens"))
        total_cached = _number(total.get("cached_prompt_tokens"))
        total["cache_share"] = (
            round(total_cached / total_prompt, 8) if total_prompt else 0.0
        )
        total["today_cost_usd"] = round(today_cost, 8) if today_known else None
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        total["projected_cost_usd"] = round(total["cost_usd"] / max(1, now.day) * days_in_month, 8) \
            if total["cost_status"] != "unavailable" else None
        try:
            budget = max(0.0, float(budget_usd)) if budget_usd is not None else None
        except (TypeError, ValueError):
            budget = None
        total["budget_usd"] = budget
        total["remaining_budget_usd"] = max(0.0, budget - total["cost_usd"]) if budget is not None else None
        total["budget_used_pct"] = min(100.0, total["cost_usd"] / budget * 100.0) if budget and budget > 0 else None
        total["month_elapsed_pct"] = now.day / days_in_month * 100.0
        total["days_remaining"] = days_in_month - now.day
        return {"period": month_key, "today": today_key, "providers": providers, "total": total}

    def model_usage_snapshot(self, *, days=30) -> dict:
        """Return trailing usage, performance, reliability, and activity rhythm."""
        try:
            days = min(45, max(1, int(days)))
        except (TypeError, ValueError):
            days = 30
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        start = now.date() - timedelta(days=days - 1)
        with self._lock:
            stored_days = json.loads(json.dumps(self._state.get("days", {})))

        daily = []
        model_totals = {}
        totals = {
            "successful_requests": 0, "failed_requests": 0, "cancelled_requests": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0,
            "inference_time_s": 0.0, "timed_calls": 0,
            "cached_prompt_tokens": 0, "cache_hit_calls": 0,
            "uncached_prompt_tokens": 0, "reasoning_tokens": 0,
            "cache_write_prompt_tokens": 0,
            "cost_usd": 0.0, "exact_calls": 0,
            "estimated_calls": 0, "unpriced_calls": 0,
            "local_requests": 0, "cloud_requests": 0,
            **{field: [] for field in PERFORMANCE_SAMPLE_FIELDS},
        }
        additive_fields = (
            "successful_requests", "failed_requests", "cancelled_requests",
            "prompt_tokens", "completion_tokens", "tokens", "timed_calls",
            "cached_prompt_tokens", "cache_hit_calls", "exact_calls",
            "uncached_prompt_tokens", "reasoning_tokens",
            "cache_write_prompt_tokens",
            "estimated_calls", "unpriced_calls",
        )
        for offset in range(days):
            date = start + timedelta(days=offset)
            date_key = date.isoformat()
            day_models = {}
            day_summary = {
                "successful_requests": 0, "failed_requests": 0, "cancelled_requests": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0,
                "inference_time_s": 0.0, "timed_calls": 0,
                "cached_prompt_tokens": 0, "cache_hit_calls": 0,
                "uncached_prompt_tokens": 0, "reasoning_tokens": 0,
                "cache_write_prompt_tokens": 0,
                "cost_usd": 0.0, "exact_calls": 0,
                "estimated_calls": 0, "unpriced_calls": 0,
                **{field: [] for field in PERFORMANCE_SAMPLE_FIELDS},
            }
            for provider, provider_row in ((stored_days.get(date_key, {}).get("providers") or {}).items()):
                provider = str(provider or "unknown").lower()
                for model, source in (provider_row.get("models") or {}).items():
                    successful = _safe_count(source.get("calls"))
                    failed = _safe_count(source.get("failed_calls"))
                    cancelled = _safe_count(source.get("cancelled_calls"))
                    attempts = successful + failed + cancelled
                    tokens = _safe_count(source.get("total_tokens"))
                    prompt = _safe_count(source.get("prompt_tokens"))
                    completion = _safe_count(source.get("completion_tokens"))
                    runtime = _number(source.get("inference_time_s"))
                    timed_calls = _safe_count(source.get("timed_calls"))
                    cached = _safe_count(source.get("cached_prompt_tokens"))
                    uncached = _safe_count(source.get("uncached_prompt_tokens"))
                    reasoning = _safe_count(source.get("reasoning_tokens"))
                    cache_write = _safe_count(source.get("cache_write_prompt_tokens"))
                    cache_hits = _safe_count(source.get("cache_hit_calls"))
                    cost = _number(source.get("cost_usd"))
                    exact_calls = _safe_count(source.get("exact_calls"))
                    estimated_calls = _safe_count(source.get("estimated_calls"))
                    unpriced_calls = _safe_count(source.get("unpriced_calls"))
                    runtime_id = str(source.get("runtime_id") or "")
                    if not (attempts or tokens or timed_calls):
                        continue
                    key = f"{provider}:{model}"
                    item = {
                        "key": key, "model": str(model), "provider": provider,
                        "provider_name": PROVIDER_NAMES.get(provider, provider.replace("_", " ").title()),
                        "successful_requests": successful,
                        "failed_requests": failed, "cancelled_requests": cancelled,
                        "prompt_tokens": prompt,
                        "completion_tokens": completion, "tokens": tokens,
                        "inference_time_s": round(runtime, 6), "timed_calls": timed_calls,
                        "cached_prompt_tokens": cached,
                        "uncached_prompt_tokens": uncached,
                        "reasoning_tokens": reasoning,
                        "cache_write_prompt_tokens": cache_write,
                        "cache_hit_calls": cache_hits,
                        "cost_usd": round(cost, 8),
                        "cost_status": self._cost_status(source),
                        "exact_calls": exact_calls,
                        "estimated_calls": estimated_calls,
                        "unpriced_calls": unpriced_calls,
                        "runtime_id": runtime_id,
                    }
                    _extend_samples(item, source)
                    day_models[key] = _finalize_performance(item)
                    summary = model_totals.setdefault(key, {
                        "key": key, "model": str(model), "provider": provider,
                        "provider_name": item["provider_name"],
                        "successful_requests": 0, "failed_requests": 0,
                        "cancelled_requests": 0,
                        "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0,
                        "inference_time_s": 0.0, "timed_calls": 0,
                        "cached_prompt_tokens": 0, "cache_hit_calls": 0,
                        "uncached_prompt_tokens": 0, "reasoning_tokens": 0,
                        "cache_write_prompt_tokens": 0,
                        "cost_usd": 0.0, "exact_calls": 0,
                        "estimated_calls": 0, "unpriced_calls": 0,
                        "runtime_id": runtime_id,
                        **{field: [] for field in PERFORMANCE_SAMPLE_FIELDS},
                    })
                    source_projection = {
                        "successful_requests": successful,
                        "failed_requests": failed,
                        "cancelled_requests": cancelled,
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "tokens": tokens,
                        "timed_calls": timed_calls,
                        "cached_prompt_tokens": cached,
                        "uncached_prompt_tokens": uncached,
                        "reasoning_tokens": reasoning,
                        "cache_write_prompt_tokens": cache_write,
                        "cache_hit_calls": cache_hits,
                        "exact_calls": exact_calls,
                        "estimated_calls": estimated_calls,
                        "unpriced_calls": unpriced_calls,
                    }
                    for field in additive_fields:
                        summary[field] += source_projection[field]
                        day_summary[field] += source_projection[field]
                        totals[field] += source_projection[field]
                    summary["inference_time_s"] += runtime
                    summary["cost_usd"] += cost
                    day_summary["inference_time_s"] += runtime
                    day_summary["cost_usd"] += cost
                    totals["inference_time_s"] += runtime
                    totals["cost_usd"] += cost
                    totals["local_requests" if provider == "local" else "cloud_requests"] += attempts
                    _extend_samples(summary, source)
                    _extend_samples(day_summary, source)
                    _extend_samples(totals, source)
            day_summary["cost_usd"] = round(day_summary["cost_usd"], 8)
            day_summary["inference_time_s"] = round(day_summary["inference_time_s"], 6)
            day_summary["cost_status"] = self._cost_status(day_summary)
            _finalize_performance(day_summary)
            daily.append({"date": date_key, "models": list(day_models.values()), **day_summary})

        models = sorted(
            model_totals.values(),
            key=lambda row: (
                -row["tokens"],
                -(
                    _safe_count(row.get("successful_requests"))
                    + _safe_count(row.get("failed_requests"))
                    + _safe_count(row.get("cancelled_requests"))
                ),
                row["model"].lower(),
            ),
        )
        for row in models:
            row["inference_time_s"] = round(row["inference_time_s"], 6)
            row["cost_usd"] = round(row["cost_usd"], 8)
            row["cost_status"] = self._cost_status(row)
            _finalize_performance(row)
        totals["inference_time_s"] = round(totals["inference_time_s"], 6)
        totals["cost_usd"] = round(totals["cost_usd"], 8)
        totals["cost_status"] = self._cost_status(totals)
        _finalize_performance(totals)

        def sum_days(rows):
            return {
                "requests": sum(_safe_count(row.get("requests")) for row in rows),
                "successful": sum(_safe_count(row.get("successful_requests")) for row in rows),
                "failed": sum(_safe_count(row.get("failed_requests")) for row in rows),
                "tokens": sum(_safe_count(row.get("tokens")) for row in rows),
            }

        this_week = sum_days(daily[-7:])
        last_week = sum_days(daily[-14:-7])
        week_over_week = {
            "this_week": this_week,
            "last_week": last_week,
            "change_pct": {
                "requests": _change_pct(this_week["requests"], last_week["requests"]),
                "tokens": _change_pct(this_week["tokens"], last_week["tokens"]),
            },
        }

        hourly_pattern = [
            {"hour": hour, "requests": 0, "successful": 0, "failed": 0,
             "cancelled": 0, "tokens": 0, "inference_time_s": 0.0,
             "cached_prompt_tokens": 0, "uncached_prompt_tokens": 0,
             "reasoning_tokens": 0, "cache_write_prompt_tokens": 0,
             "cost_usd": 0.0}
            for hour in range(24)
        ]
        hourly_activity = []
        observed_hour_buckets = []
        for offset in range(days):
            date = start + timedelta(days=offset)
            date_key = date.isoformat()
            stored_hours = stored_days.get(date_key, {}).get("hours") or {}
            activity_hours = []
            for hour in range(24):
                source = stored_hours.get(f"{hour:02d}") or {}
                item = {
                    "hour": hour,
                    "requests": _safe_count(source.get("requests")),
                    "successful": _safe_count(source.get("successful")),
                    "failed": _safe_count(source.get("failed")),
                    "cancelled": _safe_count(source.get("cancelled")),
                    "tokens": _safe_count(source.get("tokens")),
                    "inference_time_s": round(_number(source.get("inference_time_s")), 6),
                    "cached_prompt_tokens": _safe_count(source.get("cached_prompt_tokens")),
                    "uncached_prompt_tokens": _safe_count(
                        source.get("uncached_prompt_tokens")
                    ),
                    "reasoning_tokens": _safe_count(source.get("reasoning_tokens")),
                    "cache_write_prompt_tokens": _safe_count(
                        source.get("cache_write_prompt_tokens")
                    ),
                    "cost_usd": round(_number(source.get("cost_usd")), 8),
                }
                activity_hours.append(item)
                pattern = hourly_pattern[hour]
                for field in ("requests", "successful", "failed", "cancelled", "tokens",
                              "cached_prompt_tokens", "uncached_prompt_tokens",
                              "reasoning_tokens", "cache_write_prompt_tokens"):
                    pattern[field] += item[field]
                pattern["inference_time_s"] += item["inference_time_s"]
                pattern["cost_usd"] += item["cost_usd"]
                if item["requests"] or item["tokens"]:
                    observed_hour_buckets.append((
                        datetime(date.year, date.month, date.day, hour, tzinfo=timezone.utc),
                        item,
                    ))
            if offset >= max(0, days - 7):
                hourly_activity.append({"date": date_key, "hours": activity_hours})
        for item in hourly_pattern:
            item["inference_time_s"] = round(item["inference_time_s"], 6)
            item["cost_usd"] = round(item["cost_usd"], 8)

        current_hour = now.replace(minute=0, second=0, microsecond=0)
        last_start = current_hour - timedelta(hours=23)
        previous_start = current_hour - timedelta(hours=47)
        previous_end = current_hour - timedelta(hours=24)

        def sum_hours(window_start, window_end):
            rows = [item for timestamp, item in observed_hour_buckets
                    if window_start <= timestamp <= window_end]
            return {
                "requests": sum(item["requests"] for item in rows),
                "tokens": sum(item["tokens"] for item in rows),
            }

        recent = sum_hours(last_start, current_hour)
        previous = sum_hours(previous_start, previous_end)
        recent_activity = {
            "last_24h_requests": recent["requests"],
            "prev_24h_requests": previous["requests"],
            "last_24h_tokens": recent["tokens"],
            "change_24h_pct": _change_pct(recent["requests"], previous["requests"]),
        }
        peak_hours = [
            {"hour": item["hour"], "requests": item["requests"], "tokens": item["tokens"]}
            for item in sorted(
                hourly_pattern, key=lambda row: (-row["requests"], -row["tokens"], row["hour"]))
            if item["requests"] or item["tokens"]
        ][:3]
        peak_days = [
            {"date": row["date"], "requests": row["requests"], "tokens": row["tokens"]}
            for row in sorted(
                daily, key=lambda item: (-item["requests"], -item["tokens"], item["date"]))
            if row["requests"] or row["tokens"]
        ][:3]
        return {
            "days": days,
            "start": start.isoformat(),
            "end": now.date().isoformat(),
            "daily": daily,
            "models": models,
            "totals": totals,
            "week_over_week": week_over_week,
            "recent_activity": recent_activity,
            "peak_days": peak_days,
            "peak_hours": peak_hours,
            "hourly_pattern": hourly_pattern,
            "hourly_activity": hourly_activity,
            "hourly_available": bool(observed_hour_buckets),
            "hour_timezone": "UTC",
            "current_hour_utc": current_hour.isoformat().replace("+00:00", "Z"),
        }

    @staticmethod
    def _empty_model():
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cached_prompt_tokens": 0, "cache_hit_calls": 0,
                "uncached_prompt_tokens": 0, "reasoning_tokens": 0,
                "cache_write_prompt_tokens": 0,
                "cost_usd": 0.0, "exact_calls": 0,
                "estimated_calls": 0, "unpriced_calls": 0,
                "inference_time_s": 0.0, "timed_calls": 0,
                "failed_calls": 0, "cancelled_calls": 0,
                **{field: [] for field in PERFORMANCE_SAMPLE_FIELDS}}

    @staticmethod
    def _empty_hour():
        return {"requests": 0, "successful": 0, "failed": 0, "cancelled": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0,
                "cached_prompt_tokens": 0, "inference_time_s": 0.0,
                "uncached_prompt_tokens": 0, "reasoning_tokens": 0,
                "cache_write_prompt_tokens": 0,
                "cost_usd": 0.0}

    @staticmethod
    def _append_performance(target, field, value):
        number = _finite_non_negative(value)
        if number is None:
            return
        values = target.setdefault(field, [])
        if not isinstance(values, list):
            values = target[field] = []
        values.append(round(number, 6))
        if len(values) > PERFORMANCE_SAMPLE_LIMIT:
            del values[:-PERFORMANCE_SAMPLE_LIMIT]

    @classmethod
    def _empty_row(cls):
        return {**cls._empty_model(), "models": {}, "last_model": "",
                "last_request_at": 0}

    @staticmethod
    def _merge_totals(target, source):
        for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens",
                    "cached_prompt_tokens", "cache_hit_calls",
                    "uncached_prompt_tokens", "reasoning_tokens",
                    "cache_write_prompt_tokens",
                    "cost_usd", "exact_calls", "estimated_calls", "unpriced_calls",
                    "inference_time_s", "timed_calls", "failed_calls",
                    "cancelled_calls"):
            target[key] = target.get(key, 0) + source.get(key, 0)

    @classmethod
    def _merge_row(cls, target, source):
        cls._merge_totals(target, source)
        if source.get("last_request_at", 0) >= target.get("last_request_at", 0):
            target["last_request_at"] = source.get("last_request_at", 0)
            target["last_model"] = source.get("last_model", "")
        for model, model_source in (source.get("models") or {}).items():
            model_target = target["models"].setdefault(model, cls._empty_model())
            cls._merge_totals(model_target, model_source)

    @staticmethod
    def _cost_status(row):
        if row.get("unpriced_calls", 0):
            return "partial" if row.get("exact_calls", 0) + row.get("estimated_calls", 0) else "unavailable"
        if row.get("estimated_calls", 0):
            return "estimated"
        if row.get("exact_calls", 0):
            return "exact"
        return "unavailable"

    @staticmethod
    def _limit(headers, root, kind=""):
        if kind:
            limit = _integer(headers.get(f"{root}-limit-{kind}"))
            remaining = _integer(headers.get(f"{root}-remaining-{kind}"))
            reset = headers.get(f"{root}-reset-{kind}", "")
        else:
            limit = _integer(headers.get(f"{root}-limit"))
            remaining = _integer(headers.get(f"{root}-remaining"))
            reset = headers.get(f"{root}-reset", "")
        return {"limit": limit, "remaining": remaining, "reset": reset,
                "used": max(0, limit - remaining) if limit is not None and remaining is not None else None}

    @staticmethod
    def _price_for(provider, model, custom):
        custom = custom if isinstance(custom, dict) else {}
        candidate = custom.get(model) or (custom.get(provider, {}) if isinstance(custom.get(provider), dict) else {}).get(model)
        if isinstance(candidate, dict):
            input_price = candidate.get("input_per_million")
            output_price = candidate.get("output_per_million")
            if input_price is not None and output_price is not None:
                return _number(input_price), _number(output_price)
        return STANDARD_PRICING.get((provider, model.lower()))

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._state = {"version": 3, "days": data.get("days", {}),
                               "limits": data.get("limits", {})}
        except Exception:
            pass

    def _save_locked(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, indent=2)
            os.replace(temporary, self.path)
        except Exception:
            pass

    def _prune(self, now):
        cutoff = now.timestamp() - 45 * 86400
        kept = {}
        for key, value in self._state.get("days", {}).items():
            try:
                timestamp = datetime.fromisoformat(key).replace(tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError):
                continue
            if timestamp >= cutoff:
                kept[key] = value
        self._state["days"] = kept
