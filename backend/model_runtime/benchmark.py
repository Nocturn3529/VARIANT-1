"""Repeatable OpenAI-compatible local inference benchmarks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import statistics
import time
import uuid

import httpx

from model_runtime.platform_store import AtomicJsonStore
from model_runtime.openai_sse import OpenAIChatSSEDecoder


@asynccontextmanager
async def _null_admission():
    yield


class InferenceBenchmark:
    def __init__(
        self,
        data_dir: str,
        broadcast,
        hardware_snapshot,
        *,
        event_sink=None,
        admission_gate=None,
    ) -> None:
        self.store = AtomicJsonStore(
            os.path.join(data_dir, "data", "inference", "benchmarks.json"),
            {"version": 1, "items": []},
        )
        loaded = self.store.load()
        rows = loaded.get("items") if isinstance(loaded, dict) else []
        self.items: list[dict] = [row for row in (rows or []) if isinstance(row, dict)]
        self.broadcast = broadcast
        self.hardware_snapshot = hardware_snapshot
        self.event_sink = event_sink
        self.admission_gate = admission_gate or _null_admission
        self.job: dict | None = None
        self._task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        return {
            "type": "inference:benchmarks",
            "job": dict(self.job) if self.job else None,
            "items": sorted(self.items, key=lambda row: row.get("created_at", 0), reverse=True)[:100],
            "recommendations": self.recommendations(),
        }

    def start(self, endpoint: str, model: str, *, runtime_id: str = "", rounds: int = 3, prompt: str = "Explain why local inference is useful in three concise sentences.") -> dict:
        if self._task and not self._task.done():
            raise RuntimeError("a benchmark is already running")
        endpoint = str(endpoint or "").strip().rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("benchmark endpoint must be an http:// or https:// URL")
        rounds = max(1, min(10, int(rounds or 3)))
        self.job = {
            "id": uuid.uuid4().hex[:12], "status": "queued", "endpoint": endpoint,
            "model": str(model or "default"), "runtime_id": str(runtime_id or ""),
            "rounds": rounds, "completed_rounds": 0, "progress": 0,
            "error": "", "created_at": time.time(),
        }
        self._task = asyncio.create_task(self._run(dict(self.job), str(prompt or "")[:2000]), name="inference-benchmark")
        return dict(self.job)

    async def cancel(self) -> bool:
        if not self._task or self._task.done():
            return False
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        await self.cancel()

    async def _emit(self) -> None:
        try:
            await self.broadcast(self.snapshot())
        except Exception:
            pass

    async def _run(self, job: dict, prompt: str) -> None:
        assert self.job is not None
        self.job["status"] = "running"
        await self._emit()
        samples = []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=180.0), trust_env=False) as client:
                for index in range(int(job["rounds"])):
                    samples.append(await self._one(client, job["endpoint"], job["model"], prompt))
                    if self.job is None:
                        return
                    self.job["completed_rounds"] = index + 1
                    self.job["progress"] = round((index + 1) / job["rounds"] * 100)
                    await self._emit()
            hardware = self.hardware_snapshot()
            tps = [row["decode_tps"] for row in samples if row["decode_tps"] > 0]
            ttft = [row["ttft_ms"] for row in samples if row["ttft_ms"] >= 0]
            latency = [row["latency_ms"] for row in samples]
            result = {
                "id": job["id"], "created_at": time.time(), "endpoint": job["endpoint"],
                "model": job["model"], "runtime_id": job["runtime_id"], "rounds": len(samples),
                "decode_tps_avg": round(statistics.fmean(tps), 2) if tps else 0.0,
                "decode_tps_min": round(min(tps), 2) if tps else 0.0,
                "ttft_ms_avg": round(statistics.fmean(ttft), 2) if ttft else 0.0,
                "latency_ms_avg": round(statistics.fmean(latency), 2) if latency else 0.0,
                "output_tokens": sum(row["output_tokens"] for row in samples),
                "samples": samples,
                "hardware": {
                    "gpu": ((hardware.get("gpus") or [{}])[0]).get("name") if isinstance(hardware, dict) else "",
                    "vram_peak_mb": max([int(row.get("vram_used_mb") or 0) for row in (hardware.get("gpus") or [])] or [0]) if isinstance(hardware, dict) else 0,
                    "ram_used_mb": int(((hardware.get("memory") or {}).get("used_mb") or 0)) if isinstance(hardware, dict) else 0,
                },
            }
            self.items.append(result)
            self.items = self.items[-250:]
            self.store.save({"version": 1, "items": self.items})
            self.job = {**self.job, "status": "done", "progress": 100, "result_id": result["id"]}
            self._event("ok", f"Benchmark complete: {job['model']}", {"result_id": result["id"]})
        except asyncio.CancelledError:
            if self.job:
                self.job["status"] = "cancelled"
            self._event("info", "Benchmark cancelled", {"job_id": job["id"]})
        except Exception as exc:
            if self.job:
                self.job.update({"status": "error", "error": str(exc)[:500]})
            self._event("error", f"Benchmark failed: {exc}", {"job_id": job["id"]})
        finally:
            await self._emit()
            self._task = None

    async def _one(self, client: httpx.AsyncClient, endpoint: str, model: str, prompt: str) -> dict:
        started = time.perf_counter()
        first = None
        output = 0
        usage = {}
        payload = {
            "model": model, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": prompt}], "max_tokens": 128,
            "temperature": 0,
        }
        decoder = OpenAIChatSSEDecoder()
        async with self.admission_gate():
            async with client.stream("POST", f"{endpoint}/v1/chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    decoded = decoder.decode_line(line)
                    if decoded is None or decoded.done:
                        continue
                    event = decoded.payload or {}
                    chunk = (((event.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
                    if chunk:
                        first = first or time.perf_counter()
                        output += 1
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                decoder.require_terminal("benchmark stream")
        finished = time.perf_counter()
        output_tokens = int(usage.get("completion_tokens") or output)
        decode_s = max(0.0, finished - (first or finished))
        return {
            "ttft_ms": round(((first or finished) - started) * 1000, 2),
            "latency_ms": round((finished - started) * 1000, 2),
            "decode_tps": round(max(0, output_tokens - 1) / decode_s, 2) if decode_s else 0.0,
            "output_tokens": output_tokens,
        }

    def recommendations(self) -> list[dict]:
        latest: dict[tuple[str, str], dict] = {}
        for row in sorted(self.items, key=lambda item: item.get("created_at", 0), reverse=True):
            key = (str(row.get("runtime_id") or ""), str(row.get("model") or ""))
            latest.setdefault(key, row)
        ranked = sorted(latest.values(), key=lambda row: (row.get("decode_tps_avg", 0), -row.get("ttft_ms_avg", 0)), reverse=True)
        return [
            {
                "rank": index + 1, "runtime_id": row.get("runtime_id"), "model": row.get("model"),
                "decode_tps": row.get("decode_tps_avg"), "ttft_ms": row.get("ttft_ms_avg"),
                "reason": "Best measured throughput" if index == 0 else "Measured on this device",
            }
            for index, row in enumerate(ranked[:5])
        ]

    def _event(self, level: str, message: str, metadata: dict | None = None) -> None:
        if self.event_sink:
            self.event_sink("benchmark", level, message, metadata)


__all__ = ["InferenceBenchmark"]
