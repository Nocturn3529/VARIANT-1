"""Live local-LLM inference measurements for the Overview widget.

The collector is deliberately independent of FastAPI and the WebSocket hub.
LLMRouter owns it and publishes returned snapshots through an injected sink.
Final values prefer llama.cpp's authoritative ``usage`` and ``timings`` fields;
streaming values are provisional until that final payload arrives.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
import uuid
from typing import Dict, Optional


@dataclass
class _Request:
    request_id: str
    model: str
    started_at: float
    started_perf: float
    first_token_perf: Optional[float] = None
    last_token_perf: Optional[float] = None
    streamed_chunks: int = 0
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    processed_prompt_tokens: int = 0
    cache_hit_pct: float = 0.0
    output_tokens: int = 0
    prompt_tps: float = 0.0
    decode_tps: float = 0.0
    ttft_ms: float = 0.0
    tpot_ms: float = 0.0
    generation_time_s: float = 0.0
    time_to_last_token_s: float = 0.0
    status: str = "running"
    error: str = ""


class LocalInferenceTelemetry:
    """Thread-safe snapshots for local inference requests."""

    def __init__(self, *, clock=time.perf_counter, wall_clock=time.time):
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._active: Dict[str, _Request] = {}
        self._last: Optional[_Request] = None
        self._starts: deque[float] = deque()
        self._finished: deque[dict] = deque(maxlen=512)

    def start(self, model: str = "") -> tuple[str, dict]:
        now = self._clock()
        request_id = uuid.uuid4().hex[:8].upper()
        request = _Request(
            request_id=request_id,
            model=model or "local model",
            started_at=self._wall_clock(),
            started_perf=now,
        )
        with self._lock:
            self._active[request_id] = request
            self._starts.append(now)
            self._trim_starts(now)
            return request_id, self._snapshot_locked(request, now, state="prefill")

    def token(self, request_id: str, text: str = "") -> Optional[dict]:
        now = self._clock()
        with self._lock:
            request = self._active.get(request_id)
            if request is None:
                return None
            if request.first_token_perf is None:
                request.first_token_perf = now
                request.ttft_ms = max(0.0, (now - request.started_perf) * 1000.0)
            request.last_token_perf = now
            request.streamed_chunks += 1
            request.output_tokens = request.streamed_chunks
            decode_elapsed = max(0.0, now - request.first_token_perf)
            request.generation_time_s = decode_elapsed
            request.time_to_last_token_s = max(0.0, now - request.started_perf)
            if request.streamed_chunks > 1 and decode_elapsed > 0:
                request.decode_tps = (request.streamed_chunks - 1) / decode_elapsed
                request.tpot_ms = decode_elapsed * 1000.0 / (request.streamed_chunks - 1)
            return self._snapshot_locked(request, now, state="decode")

    def complete(self, request_id: str, *, timings=None, usage=None) -> Optional[dict]:
        now = self._clock()
        timings = timings if isinstance(timings, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        with self._lock:
            request = self._active.pop(request_id, None)
            if request is None:
                return None
            processed_n = self._number(timings.get("prompt_n"), 0, integer=True)
            cached_n = self._number(timings.get("cache_n"), 0, integer=True)
            prompt_n = self._number(
                usage.get("prompt_tokens"), int(processed_n) + int(cached_n),
                timings.get("prompt_n"), 0, integer=True)
            predicted_n = self._number(usage.get("completion_tokens"), timings.get("predicted_n"), request.streamed_chunks, integer=True)
            request.prompt_tokens = int(prompt_n)
            request.processed_prompt_tokens = int(processed_n)
            request.cached_prompt_tokens = int(cached_n)
            cache_total = int(processed_n) + int(cached_n)
            request.cache_hit_pct = (cached_n / cache_total * 100.0) if cache_total else 0.0
            request.output_tokens = int(predicted_n)
            request.prompt_tps = self._number(timings.get("prompt_per_second"), 0)
            request.decode_tps = self._number(timings.get("predicted_per_second"), request.decode_tps, 0)
            request.tpot_ms = self._number(timings.get("predicted_per_token_ms"), request.tpot_ms, 0)
            request.generation_time_s = self._number(timings.get("predicted_ms"), request.generation_time_s * 1000.0, 0) / 1000.0
            if request.first_token_perf is None:
                prompt_ms = self._number(timings.get("prompt_ms"), 0)
                request.ttft_ms = prompt_ms
            request.last_token_perf = now
            request.time_to_last_token_s = max(0.0, now - request.started_perf)
            request.status = "complete"
            self._last = request
            self._record_finished(request, now)
            self._trim_starts(now)
            return self._snapshot_locked(request, now, state="idle")

    def fail(self, request_id: str, *, status="error", error="") -> Optional[dict]:
        now = self._clock()
        with self._lock:
            request = self._active.pop(request_id, None)
            if request is None:
                return None
            request.last_token_perf = now
            request.time_to_last_token_s = max(0.0, now - request.started_perf)
            request.status = status
            request.error = str(error or "")[:180]
            self._last = request
            self._record_finished(request, now)
            self._trim_starts(now)
            return self._snapshot_locked(request, now, state="idle" if status == "cancelled" else "error")

    def snapshot(self, *, model="", engine_ready=False) -> dict:
        now = self._clock()
        with self._lock:
            self._trim_starts(now)
            request = max(self._active.values(), key=lambda item: item.started_perf) if self._active else self._last
            state = "decode" if request and request.request_id in self._active and request.first_token_perf is not None \
                else "prefill" if request and request.request_id in self._active \
                else "error" if request and request.status == "error" \
                else "idle"
            snap = self._snapshot_locked(request, now, state=state)
            if model:
                snap["model"] = model
            snap["engine_ready"] = bool(engine_ready)
            return snap

    def _snapshot_locked(self, request: Optional[_Request], now: float, *, state: str) -> dict:
        self._trim_starts(now)
        self._trim_finished(now)
        completed = [item for item in self._finished if item["status"] == "complete"]
        failed = [item for item in self._finished if item["status"] == "error"]
        cancelled = [item for item in self._finished if item["status"] == "cancelled"]
        decided = len(completed) + len(failed)
        ttfts = sorted(item["ttft_ms"] for item in completed if item["ttft_ms"] > 0)
        decode_rates = [item["decode_tps"] for item in completed if item["decode_tps"] > 0]
        base = {
            "type": "inference:telemetry",
            "route": "local",
            "state": state,
            "active_requests": len(self._active),
            "queue_depth": max(0, len(self._active) - 1),
            "requests_per_second": round(len(self._starts) / 60.0, 4),
            "rolling_window_s": 300,
            "rolling_completed": len(completed),
            "rolling_failed": len(failed),
            "rolling_cancelled": len(cancelled),
            "rolling_success_pct": round(len(completed) / decided * 100.0, 2) if decided else 0.0,
            "rolling_output_tokens": sum(item["output_tokens"] for item in completed),
            "rolling_avg_decode_tps": round(sum(decode_rates) / len(decode_rates), 3) if decode_rates else 0.0,
            "rolling_avg_ttft_ms": round(sum(ttfts) / len(ttfts), 3) if ttfts else 0.0,
            "rolling_p95_ttft_ms": round(self._percentile(ttfts, .95), 3) if ttfts else 0.0,
            "ts": round(self._wall_clock(), 3),
        }
        if request is None:
            return {
                **base, "request_id": "", "model": "", "status": "idle",
                "prompt_tokens": 0, "output_tokens": 0, "prompt_tps": 0.0,
                "cached_prompt_tokens": 0, "processed_prompt_tokens": 0,
                "cache_hit_pct": 0.0,
                "decode_tps": 0.0, "ttft_ms": 0.0, "tpot_ms": 0.0,
                "generation_time_s": 0.0, "time_to_last_token_s": 0.0,
            }
        return {
            **base,
            "request_id": request.request_id,
            "model": request.model,
            "status": request.status,
            "started_at": round(request.started_at, 3),
            "prompt_tokens": request.prompt_tokens,
            "cached_prompt_tokens": request.cached_prompt_tokens,
            "processed_prompt_tokens": request.processed_prompt_tokens,
            "cache_hit_pct": round(request.cache_hit_pct, 2),
            "output_tokens": request.output_tokens,
            "prompt_tps": round(request.prompt_tps, 3),
            "decode_tps": round(request.decode_tps, 3),
            "ttft_ms": round(request.ttft_ms, 3),
            "tpot_ms": round(request.tpot_ms, 3),
            "generation_time_s": round(request.generation_time_s, 3),
            "time_to_last_token_s": round(request.time_to_last_token_s, 3),
            "error": request.error,
        }

    def _trim_starts(self, now: float) -> None:
        cutoff = now - 60.0
        while self._starts and self._starts[0] < cutoff:
            self._starts.popleft()

    def _record_finished(self, request: _Request, now: float) -> None:
        self._finished.append({
            "finished_perf": now,
            "status": request.status,
            "ttft_ms": request.ttft_ms,
            "decode_tps": request.decode_tps,
            "output_tokens": request.output_tokens,
        })
        self._trim_finished(now)

    def _trim_finished(self, now: float) -> None:
        cutoff = now - 300.0
        while self._finished and self._finished[0]["finished_perf"] < cutoff:
            self._finished.popleft()

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, int((len(values) - 1) * quantile + .999999)))
        return values[index]

    @staticmethod
    def _number(*values, integer=False):
        for value in values:
            try:
                number = int(value) if integer else float(value)
                if number >= 0:
                    return number
            except (TypeError, ValueError):
                continue
        return 0
