from __future__ import annotations

import json

import pytest

from model_runtime.telemetry import LocalInferenceTelemetry
from llm_router import LLMRouter


def test_collector_uses_authoritative_llama_timings():
    now = {"perf": 10.0, "wall": 1000.0}
    telemetry = LocalInferenceTelemetry(
        clock=lambda: now["perf"], wall_clock=lambda: now["wall"])

    request_id, started = telemetry.start("model.gguf")
    assert started["state"] == "prefill"
    assert started["active_requests"] == 1

    now["perf"] += 0.3
    first = telemetry.token(request_id, "Hello")
    assert first["state"] == "decode"
    assert first["ttft_ms"] == pytest.approx(300)

    now["perf"] += 0.05
    telemetry.token(request_id, " world")
    now["perf"] += 0.15
    done = telemetry.complete(
        request_id,
        timings={
            "prompt_n": 100,
            "cache_n": 300,
            "prompt_per_second": 1250.5,
            "predicted_n": 20,
            "predicted_ms": 400,
            "predicted_per_token_ms": 20,
            "predicted_per_second": 50,
        },
        usage={"prompt_tokens": 120, "completion_tokens": 20},
    )

    assert done["state"] == "idle"
    assert done["active_requests"] == 0
    assert done["prompt_tokens"] == 120
    assert done["processed_prompt_tokens"] == 100
    assert done["cached_prompt_tokens"] == 300
    assert done["cache_hit_pct"] == pytest.approx(75)
    assert done["output_tokens"] == 20
    assert done["prompt_tps"] == pytest.approx(1250.5)
    assert done["decode_tps"] == pytest.approx(50)
    assert done["tpot_ms"] == pytest.approx(20)
    assert done["generation_time_s"] == pytest.approx(0.4)
    assert done["time_to_last_token_s"] == pytest.approx(0.5)
    assert done["requests_per_second"] == pytest.approx(1 / 60, abs=0.0001)


class _FakeResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_lines(self):
        yield 'data: ' + json.dumps({"choices": [{"delta": {"content": "hello"}}]})
        yield 'data: ' + json.dumps({
            "choices": [],
            "usage": {"prompt_tokens": 44, "completion_tokens": 3, "total_tokens": 47},
            "timings": {
                "prompt_n": 44, "prompt_per_second": 880,
                "predicted_n": 3, "predicted_ms": 75,
                "predicted_per_token_ms": 25, "predicted_per_second": 40,
            },
        })
        yield "data: [DONE]"


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        return _FakeResponse()


@pytest.mark.asyncio
async def test_router_broadcasts_streaming_and_final_llama_metrics(monkeypatch, tmp_path):
    from llm_local_stream import call_local_inner

    monkeypatch.setattr("llm_local_stream.httpx.AsyncClient", _FakeClient)
    router = LLMRouter({"mode": "local", "local": {"model": "model.gguf"}}, str(tmp_path))
    router.engine.ready = True
    snapshots = []

    async def collect(snapshot):
        snapshots.append(snapshot)

    router.set_inference_telemetry_sink(collect)
    output = [token async for token in call_local_inner(router,
        [{"role": "user", "content": "hi"}], {"max_tokens": 8})]

    assert output == ["hello"]
    assert snapshots[0]["state"] == "prefill"
    assert any(item["state"] == "decode" for item in snapshots)
    assert snapshots[-1]["state"] == "idle"
    assert snapshots[-1]["prompt_tokens"] == 44
    assert snapshots[-1]["output_tokens"] == 3
    assert snapshots[-1]["prompt_tps"] == 880
    assert snapshots[-1]["decode_tps"] == 40
