"""Usage comparison checks: exact scope, missing counters, and cache semantics."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


spec = importlib.util.spec_from_file_location(
    "prompt_bank_usage", Path(__file__).resolve().parents[2] / "scripts/collect-prompt-bank-usage.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_variant_scope_reported_zero_and_partial_coverage(tmp_path):
    path = tmp_path / "usage.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE model_usage_request(ordinal INTEGER,manifest_id TEXT,"
                     "session_id TEXT,run_id TEXT,provider TEXT,model TEXT,metadata_json TEXT,"
                     "usage_json TEXT,outcome TEXT,started_at REAL,finished_at REAL,duration_s REAL)")
        usage = {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 60,
                 "reasoning_tokens": 0, "cost_usd": 0,
                 "reported_fields": ["input_tokens", "output_tokens", "cached_input_tokens",
                                     "reasoning_tokens", "cost_usd"], "prompt_text": "DO-NOT-EXPORT"}
        for ordinal, session in [(1, "case-a"), (2, "unrelated")]:
            conn.execute("INSERT INTO model_usage_request VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ordinal, str(ordinal), session, "run", "hermes", "longcat", "{}",
                          json.dumps(usage), "succeeded", 1, 2, 1))
    original = path.read_bytes()
    first = collector.collect("variant", path, ["case-a"])
    assert path.read_bytes() == original
    assert first["api_calls"] == 1
    assert first["metrics"]["uncached_input_tokens"]["value"] == 40
    assert first["metrics"]["reasoning_tokens"]["value"] == 0
    assert first["metrics"]["cost_usd"]["value"] == 0
    assert "DO-NOT-EXPORT" not in json.dumps(first)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO model_usage_request VALUES(3,'3','case-a','run','hermes',"
                     "'longcat','{}',NULL,'pending',3,NULL,NULL)")
    combined = collector.collect("variant", path, ["case-a"])
    assert combined["metrics"]["input_tokens"]["value"] is None
    assert combined["metrics"]["input_tokens"]["known_subtotal"] == 100
    assert combined["pending_requests"] == 1
    delta = collector.collect("variant", path, ["case-a"], first)
    assert delta["api_calls"] == 1 and delta["records"][0]["key"] == "3"


def test_hermes_cache_split_baseline_delta_and_unknown_zero(tmp_path):
    path = tmp_path / "hermes.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE session_model_usage(session_id TEXT,model TEXT,"
                     "billing_provider TEXT,billing_base_url TEXT,billing_mode TEXT,task TEXT,"
                     "api_call_count INTEGER,input_tokens INTEGER,output_tokens INTEGER,"
                     "cache_read_tokens INTEGER,cache_write_tokens INTEGER,reasoning_tokens INTEGER,"
                     "estimated_cost_usd REAL,actual_cost_usd REAL,cost_status TEXT,cost_source TEXT)")
        conn.execute("INSERT INTO session_model_usage VALUES('case-a','longcat','nous',"
                     "'https://private.invalid/?token=DO-NOT-EXPORT','oauth','main',"
                     "2,150,50,30,20,7,0,0,NULL,NULL)")
    first = collector.collect("hermes", path, ["case-a"])
    assert first["metrics"]["input_tokens"]["value"] == 200
    assert first["metrics"]["uncached_input_tokens"]["value"] == 170
    assert first["metrics"]["total_tokens"]["value"] == 250
    assert first["metrics"]["cost_usd"]["value"] is None
    assert "DO-NOT-EXPORT" not in json.dumps(first)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE session_model_usage SET api_call_count=3,input_tokens=160,"
                     "output_tokens=55,cache_read_tokens=35")
    delta = collector.collect("hermes", path, ["case-a"], first)
    assert delta["api_calls"] == 1
    assert delta["metrics"]["input_tokens"]["value"] == 15
    assert delta["metrics"]["reasoning_tokens"]["value"] is None
    assert delta["metrics"]["reasoning_tokens"]["raw_aggregate"] == 0
    with pytest.raises(ValueError, match="identities"):
        collector.collect("hermes", path, ["unrelated"], first)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE session_model_usage SET input_tokens=1")
    with pytest.raises(ValueError, match="decreased"):
        collector.collect("hermes", path, ["case-a"], first)


def test_missing_database_is_not_created_and_existing_output_is_retained(tmp_path, monkeypatch):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        collector.collect("variant", missing, ["case-a"])
    assert not missing.exists()
    output = tmp_path / "evidence.json"
    output.write_text("retained")
    monkeypatch.setattr("sys.argv", ["collector", "--harness", "variant", "--database",
                       str(missing), "--session-id", "case-a", "--output", str(output)])
    with pytest.raises(FileExistsError):
        collector.main()
    assert output.read_text() == "retained"
