"""Qualification cannot treat route discovery or an unknown bill as completion/free usage."""
from pathlib import Path
import asyncio
import json
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments" / "live-canary"))
from qualify import assess
from qualification_budget import QualificationBudget, install_budget
from run_canary import configure_candidate_route
import run_canary
from session_catalog.support import SupportMatrix, UnsupportedModelRoute


def test_all_repetitions_require_full_outcomes_and_measured_latency():
    full = {"results": [{"case_id": "DESK", "full_passed": True, "duration_ms": 100}]}
    routing = {"results": [{"case_id": "DESK", "passed": True, "full_passed": False, "duration_ms": 100}]}
    assert assess([full] * 3, 3, ["DESK"], 1000)["qualified"]
    assert not assess([full, routing, full], 3, ["DESK"], 1000)["qualified"]
    assert not assess([full], 3, ["DESK"], 1000)["qualified"]
    assert not assess([full] * 3, 3, ["DESK"], 50)["qualified"]
    assert not assess([{"results": full["results"] * 2}] * 3, 3, ["DESK"], 1000)["qualified"]
    for duration in (-1, float("nan"), float("inf"), True):
        invalid = {"results": [{**full["results"][0], "duration_ms": duration}]}
        assert not assess([invalid] * 3, 3, ["DESK"], 1000)["qualified"]


@pytest.mark.asyncio
async def test_http_meter_preserves_stream_and_blocks_before_network(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"model": "grok-4.7", "reasoning_effort": "low", "context_tokens": 500000,
        "input_per_million": 4, "output_per_million": 12, "max_cost_usd": 0.05,
        "upper_spend_usd": 0, "requests": []}))
    monkeypatch.setattr(httpx.AsyncClient, "send", httpx.AsyncClient.send)
    install_budget(path)
    frames = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":100,"output_tokens":200}}}\r\n\r\ndata: [DONE]\n\n'
    received = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (frames[:17], frames[17:57], frames[57:]):
                await asyncio.sleep(0)
                yield chunk
        async def aclose(self):
            received.append("closed")

    def dispatch(request):
        received.append(request.url.path)
        return httpx.Response(200, stream=Chunks())

    payload = {"model": "grok-4.7", "reasoning": {"effort": "low"}, "max_output_tokens": 1000, "input": "test"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as client:
        async with client.stream("POST", "https://example.test/responses", json=payload) as response:
            assert await response.aread() == frames
        ledger = json.loads(path.read_text())
        assert ledger["upper_spend_usd"] == pytest.approx(0.0028)
        assert ledger["requests"][0]["cost_kind"] == "conservative_api_equivalent"
        before = received[:]
        with pytest.raises(RuntimeError, match="exhausted"):
            await client.post("https://example.test/responses", json={**payload, "max_output_tokens": 10000})
        assert received == before
        # Model discovery does not consume a request reservation.
        await client.get("https://example.test/models")
        assert len(json.loads(path.read_text())["requests"]) == 1


def test_budget_blocks_before_dispatch_and_retains_unknown_reservations(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"model": "grok-4.7", "reasoning_effort": "low", "context_tokens": 500000,
        "input_per_million": 4, "output_per_million": 12, "max_cost_usd": 0.04,
        "upper_spend_usd": 0, "requests": []}))
    budget = QualificationBudget(path)
    payload = {"model": "grok-4.7", "reasoning": {"effort": "low"}, "max_output_tokens": 1000}
    request, reservation = budget.reserve(payload, 10)
    budget.settle(request, None)
    assert json.loads(path.read_text())["upper_spend_usd"] == reservation
    with pytest.raises(RuntimeError, match="exhausted"):
        budget.reserve(payload, 10)
    budget.settle(request, {"cost_in_usd_ticks": 10_000_000})
    assert json.loads(path.read_text())["upper_spend_usd"] == pytest.approx(0.001)
    with pytest.raises(RuntimeError, match="effort"):
        budget.reserve({**payload, "reasoning": {"effort": "high"}}, 10)
    with pytest.raises(RuntimeError, match="model"):
        budget.reserve({**payload, "model": "other"}, 10)
    with pytest.raises(RuntimeError, match="exhausted"):
        budget.reserve({**payload, "input": [{"type": "input_image", "image_url": "https://example.test/image"}]}, 10)
    invalid = json.loads(path.read_text())
    invalid["max_cost_usd"] = float("nan")
    path.write_text(json.dumps(invalid))
    with pytest.raises(RuntimeError, match="finite"):
        budget.reserve(payload, 10)


def test_isolated_candidate_admission_preserves_explicit_denies(monkeypatch):
    monkeypatch.setenv("VARIANT1_QUALIFICATION_CANDIDATE_ROUTE", "1")
    coordinates = dict(profile="trusted-local.v1", provider="xai", model="grok-4.7", adapter="xai.responses")
    candidate = {"action_surface": {"support_matrix": []}}
    configure_candidate_route(candidate, "xai", "grok-4.7", "xai.responses")
    assert SupportMatrix.from_config(candidate).validate(**coordinates).status == "canary"
    denied = {"action_surface": {"support_matrix": [{**coordinates, "status": "unqualified"}]}}
    with pytest.raises(UnsupportedModelRoute):
        configure_candidate_route(denied, "xai", "grok-4.7", "xai.responses")


def test_candidate_backend_config_uses_the_selected_adapter_and_oauth_only(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"action_surface": {"support_matrix": []}, "cloud": {
        "keys": {"xai": "synthetic-key"}, "credential_pools": {"xai": ["synthetic-key"]},
        "oauth": {"xai": {"access_token": "synthetic-encrypted-token"}}}}))
    monkeypatch.setattr(run_canary, "SOURCE_CONFIG", source)
    monkeypatch.setenv("VARIANT1_QUALIFICATION_CANDIDATE_ROUTE", "1")
    monkeypatch.setenv("VARIANT1_QUALIFICATION_OAUTH_ONLY", "1")
    backend = run_canary.BackendProcess(tmp_path, seed="synthetic", enable_mcp=False,
        route={"mode": "cloud", "provider": "xai", "model": "grok-4.7"})
    backend._prepare_config()
    config = json.loads(backend.config_path.read_text())
    assert config["action_surface"]["support_matrix"][0]["adapter"] == "xai.responses"
    assert config["cloud"]["xai_credential_policy"] == "subscription_only"
    assert "xai" not in config["cloud"]["keys"]
    assert "xai" not in config["cloud"]["credential_pools"]
    assert json.loads(source.read_text())["cloud"]["keys"]["xai"] == "synthetic-key"
