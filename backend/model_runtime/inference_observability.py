"""Unified inference operations history for Hardware and Overview.

Live widgets previously received unrelated point-in-time payloads.  This store
keeps a bounded request history, a short hardware series, and operational events
so the Main Deck can render rates, percentiles, cache efficiency, model mix,
resource peaks, jobs, and recent failures as one coherent surface.
"""

from __future__ import annotations

from collections import Counter, deque
import math
import os
import threading
import time
from typing import Any

from model_runtime.platform_store import AtomicJsonStore


def _num(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _percentile(values: list[float], quantile: float) -> float:
    usable = sorted(value for value in values if value >= 0)
    if not usable:
        return 0.0
    index = max(0, min(len(usable) - 1, math.ceil(len(usable) * quantile) - 1))
    return usable[index]


class InferenceObservability:
    """Bounded persistent request history plus volatile high-rate telemetry."""

    REQUEST_LIMIT = 2000
    EVENT_LIMIT = 400
    HARDWARE_LIMIT = 900

    def __init__(self, data_dir: str) -> None:
        path = os.path.join(data_dir, "data", "inference", "request_history.json")
        self._store = AtomicJsonStore(path, {"version": 1, "items": []})
        loaded = self._store.load()
        items = loaded.get("items") if isinstance(loaded, dict) else []
        self._requests: deque[dict] = deque(
            [item for item in (items or []) if isinstance(item, dict)],
            maxlen=self.REQUEST_LIMIT,
        )
        self._hardware: deque[dict] = deque(maxlen=self.HARDWARE_LIMIT)
        self._events: deque[dict] = deque(maxlen=self.EVENT_LIMIT)
        self._seen_terminal: set[str] = {
            str(item.get("request_id") or "") for item in self._requests
            if item.get("request_id")
        }
        self._last_inference: dict = {}
        self._lock = threading.RLock()

    def event(
        self,
        source: str,
        level: str,
        message: str,
        metadata: dict | None = None,
    ) -> None:
        with self._lock:
            self._events.appendleft({
                "ts": time.time(),
                "source": str(source or "runtime")[:80],
                "level": str(level or "info")[:20],
                "message": str(message or "")[:1000],
                "metadata": dict(metadata or {}),
            })

    def observe_inference(self, snapshot: dict | None) -> None:
        if not isinstance(snapshot, dict):
            return
        with self._lock:
            self._last_inference = dict(snapshot)
            request_id = str(snapshot.get("request_id") or "")
            status = str(snapshot.get("status") or "")
            if not request_id or status not in {"complete", "error", "cancelled"}:
                return
            if request_id in self._seen_terminal:
                return
            self._seen_terminal.add(request_id)
            record = {
                "request_id": request_id,
                "finished_at": _num(snapshot.get("ts")) or time.time(),
                "started_at": _num(snapshot.get("started_at")),
                "status": status,
                "runtime_id": str(snapshot.get("runtime_id") or "llamacpp"),
                "runtime_name": str(snapshot.get("runtime_name") or ""),
                "model": str(snapshot.get("model") or "local model")[:300],
                "prompt_tokens": int(_num(snapshot.get("prompt_tokens"))),
                "cached_prompt_tokens": int(_num(snapshot.get("cached_prompt_tokens"))),
                "processed_prompt_tokens": int(_num(snapshot.get("processed_prompt_tokens"))),
                "output_tokens": int(_num(snapshot.get("output_tokens"))),
                "ttft_ms": _num(snapshot.get("ttft_ms")),
                "tpot_ms": _num(snapshot.get("tpot_ms")),
                "decode_tps": _num(snapshot.get("decode_tps")),
                "prompt_tps": _num(snapshot.get("prompt_tps")),
                "latency_ms": _num(snapshot.get("time_to_last_token_s")) * 1000.0,
                "error": str(snapshot.get("error") or "")[:500],
            }
            self._requests.append(record)
            if status == "error":
                self.event(
                    "inference", "error",
                    record["error"] or f"{record['runtime_name'] or record['runtime_id']} request failed",
                    {"request_id": request_id, "model": record["model"]},
                )
            self._persist_locked()

    def record_request(self, record: dict) -> None:
        """Record a completed request from the local OpenAI gateway.

        Gateway calls bypass ``LLMRouter.stream`` by design, so they do not
        naturally pass through ``LocalInferenceTelemetry``.  Normalizing them
        here keeps Overview totals and model/runtime mix complete.
        """
        if not isinstance(record, dict):
            return
        request_id = str(record.get("request_id") or "")
        if not request_id:
            return
        with self._lock:
            if request_id in self._seen_terminal:
                return
            self._seen_terminal.add(request_id)
            row = {
                "request_id": request_id,
                "finished_at": _num(record.get("finished_at")) or time.time(),
                "started_at": _num(record.get("started_at")),
                "status": str(record.get("status") or "complete"),
                "runtime_id": str(record.get("runtime_id") or "llamacpp"),
                "runtime_name": str(record.get("runtime_name") or ""),
                "model": str(record.get("model") or "local model")[:300],
                "prompt_tokens": int(_num(record.get("prompt_tokens"))),
                "cached_prompt_tokens": int(_num(record.get("cached_prompt_tokens"))),
                "processed_prompt_tokens": int(_num(record.get("processed_prompt_tokens"))),
                "output_tokens": int(_num(record.get("output_tokens"))),
                "ttft_ms": _num(record.get("ttft_ms")),
                "tpot_ms": _num(record.get("tpot_ms")),
                "decode_tps": _num(record.get("decode_tps")),
                "prompt_tps": _num(record.get("prompt_tps")),
                "latency_ms": _num(record.get("latency_ms")),
                "error": str(record.get("error") or "")[:500],
                "source": str(record.get("source") or "gateway")[:80],
            }
            self._requests.append(row)
            if row["status"] == "error":
                self.event("gateway", "error", row["error"] or "Gateway inference failed", {"request_id": request_id})
            self._persist_locked()

    def observe_hardware(self, snapshot: dict | None) -> None:
        if not isinstance(snapshot, dict):
            return
        gpus = snapshot.get("gpus") if isinstance(snapshot.get("gpus"), list) else []
        gpu_rows = []
        for raw in gpus:
            if not isinstance(raw, dict):
                continue
            gpu_rows.append({
                "index": int(_num(raw.get("index"))),
                "name": str(raw.get("name") or "GPU")[:200],
                "utilization_pct": _num(raw.get("utilization_pct")),
                "variant1_utilization_pct": _num(raw.get("variant1_utilization_pct")),
                "vram_used_mb": _num(raw.get("vram_used_mb")),
                "vram_total_mb": _num(raw.get("vram_total_mb")),
                "temperature_c": _num(raw.get("temperature_c")),
                "power_draw_w": _num(raw.get("power_draw_w")),
            })
        sample = {
            "ts": _num(snapshot.get("ts")) or time.time(),
            "gpus": gpu_rows,
            "cpu_pct": _num((snapshot.get("cpu") or {}).get("utilization_pct") if isinstance(snapshot.get("cpu"), dict) else 0),
            "ram_used_mb": max(0.0, _num(snapshot.get("ram_total_mb")) - _num(snapshot.get("ram_available_mb"))),
            "variant1_rss_mb": _num(snapshot.get("variant1_rss_mb")),
        }
        with self._lock:
            # Overview can poll faster than the graph needs. One point/second is
            # sufficient and keeps a 15-minute history inside a small payload.
            if self._hardware and sample["ts"] - _num(self._hardware[-1].get("ts")) < 0.75:
                self._hardware[-1] = sample
            else:
                self._hardware.append(sample)

    def _persist_locked(self) -> None:
        try:
            self._store.save({"version": 1, "items": list(self._requests)})
        except Exception as exc:
            print(f"[inference-observability] history save failed: {exc}", flush=True)

    def _window(self, seconds: int, now: float) -> list[dict]:
        cutoff = now - seconds
        return [item for item in self._requests if _num(item.get("finished_at")) >= cutoff]

    @staticmethod
    def _aggregate(items: list[dict], seconds: int) -> dict:
        complete = [row for row in items if row.get("status") == "complete"]
        failed = [row for row in items if row.get("status") == "error"]
        cancelled = [row for row in items if row.get("status") == "cancelled"]
        decided = len(complete) + len(failed)
        ttft = [_num(row.get("ttft_ms")) for row in complete if _num(row.get("ttft_ms")) > 0]
        latency = [_num(row.get("latency_ms")) for row in complete if _num(row.get("latency_ms")) > 0]
        decode = [_num(row.get("decode_tps")) for row in complete if _num(row.get("decode_tps")) > 0]
        prompt = sum(int(_num(row.get("prompt_tokens"))) for row in complete)
        cached = sum(int(_num(row.get("cached_prompt_tokens"))) for row in complete)
        output = sum(int(_num(row.get("output_tokens"))) for row in complete)
        processed = sum(int(_num(row.get("processed_prompt_tokens"))) for row in complete)
        cache_base = cached + processed
        return {
            "window_s": seconds,
            "requests": len(items),
            "completed": len(complete),
            "failed": len(failed),
            "cancelled": len(cancelled),
            "success_pct": round(len(complete) / decided * 100.0, 2) if decided else 0.0,
            "requests_per_minute": round(len(items) / max(seconds / 60.0, 1.0), 3),
            "prompt_tokens": prompt,
            "cached_prompt_tokens": cached,
            "output_tokens": output,
            "total_tokens": prompt + output,
            "tokens_per_minute": round((prompt + output) / max(seconds / 60.0, 1.0), 2),
            "cache_hit_pct": round(cached / cache_base * 100.0, 2) if cache_base else 0.0,
            "avg_ttft_ms": round(sum(ttft) / len(ttft), 2) if ttft else 0.0,
            "p50_ttft_ms": round(_percentile(ttft, .50), 2),
            "p95_ttft_ms": round(_percentile(ttft, .95), 2),
            "avg_latency_ms": round(sum(latency) / len(latency), 2) if latency else 0.0,
            "p95_latency_ms": round(_percentile(latency, .95), 2),
            "avg_decode_tps": round(sum(decode) / len(decode), 2) if decode else 0.0,
            "p50_decode_tps": round(_percentile(decode, .50), 2),
        }

    def _minute_series(self, now: float, minutes: int = 60) -> list[dict]:
        buckets: dict[int, dict] = {}
        start = int(now // 60) - minutes + 1
        for minute in range(start, start + minutes):
            buckets[minute] = {
                "ts": minute * 60,
                "requests": 0,
                "errors": 0,
                "tokens": 0,
                "ttft_total": 0.0,
                "ttft_count": 0,
            }
        for row in self._requests:
            minute = int(_num(row.get("finished_at")) // 60)
            bucket = buckets.get(minute)
            if bucket is None:
                continue
            bucket["requests"] += 1
            bucket["errors"] += 1 if row.get("status") == "error" else 0
            bucket["tokens"] += int(_num(row.get("prompt_tokens"))) + int(_num(row.get("output_tokens")))
            value = _num(row.get("ttft_ms"))
            if value > 0:
                bucket["ttft_total"] += value
                bucket["ttft_count"] += 1
        series = []
        for minute in sorted(buckets):
            bucket = buckets[minute]
            series.append({
                "ts": bucket["ts"],
                "requests": bucket["requests"],
                "errors": bucket["errors"],
                "tokens": bucket["tokens"],
                "avg_ttft_ms": round(bucket["ttft_total"] / bucket["ttft_count"], 2) if bucket["ttft_count"] else 0.0,
            })
        return series

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            windows = {
                "5m": self._aggregate(self._window(300, now), 300),
                "1h": self._aggregate(self._window(3600, now), 3600),
                "24h": self._aggregate(self._window(86400, now), 86400),
            }
            model_counts: Counter[str] = Counter()
            model_tokens: Counter[str] = Counter()
            runtime_counts: Counter[str] = Counter()
            for row in self._window(86400, now):
                model = str(row.get("model") or "unknown")
                runtime = str(row.get("runtime_id") or "unknown")
                model_counts[model] += 1
                model_tokens[model] += int(_num(row.get("prompt_tokens"))) + int(_num(row.get("output_tokens")))
                runtime_counts[runtime] += 1

            hardware = list(self._hardware)
            gpu_samples = [gpu for sample in hardware for gpu in sample.get("gpus", [])]
            peaks = {
                "gpu_utilization_pct": round(max((_num(row.get("utilization_pct")) for row in gpu_samples), default=0.0), 2),
                "variant1_gpu_utilization_pct": round(max((_num(row.get("variant1_utilization_pct")) for row in gpu_samples), default=0.0), 2),
                "vram_used_mb": round(max((_num(row.get("vram_used_mb")) for row in gpu_samples), default=0.0), 2),
                "temperature_c": round(max((_num(row.get("temperature_c")) for row in gpu_samples), default=0.0), 2),
                "power_draw_w": round(max((_num(row.get("power_draw_w")) for row in gpu_samples), default=0.0), 2),
                "variant1_rss_mb": round(max((_num(row.get("variant1_rss_mb")) for row in hardware), default=0.0), 2),
            }
            return {
                "type": "inference:operations",
                "generated_at": now,
                "live": dict(self._last_inference),
                "windows": windows,
                "throughput_series": self._minute_series(now),
                "hardware_series": hardware[-180:],
                "resource_peaks": peaks,
                "model_mix": [
                    {"model": model, "requests": count, "tokens": model_tokens[model]}
                    for model, count in model_counts.most_common(12)
                ],
                "runtime_mix": [
                    {"runtime_id": runtime, "requests": count}
                    for runtime, count in runtime_counts.most_common()
                ],
                "recent_requests": list(reversed(list(self._requests)[-40:])),
                "events": list(self._events)[:80],
            }


__all__ = ["InferenceObservability"]
