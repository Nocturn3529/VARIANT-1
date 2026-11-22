"""Usage comparison checks: exact scope, missing counters, and cache semantics."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from llm_usage import normalize_manifest_usage
from model_runtime.usage_ledger import ModelUsageLedger


spec = importlib.util.spec_from_file_location(
    "prompt_bank_usage", Path(__file__).resolve().parents[2] / "scripts/collect-prompt-bank-usage.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_variant_scope_reported_zero_and_partial_coverage(tmp_path):
    path = tmp_path / "usage.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE model_usage_request(ordinal INTEGER,manifest_id TEXT,"
                     "session_id TEXT,run_id TEXT,provider TEXT,model TEXT,metadata_json TEXT,"
                     "usage_json TEXT,outcome TEXT,started_at REAL,finished_at REAL,duration_s REAL,"
                     "logical_call_id TEXT,attempt INTEGER,goal_id TEXT)")
        usage = {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 60,
                 "reasoning_tokens": 0, "cost_usd": 0,
                 "reported_fields": ["input_tokens", "output_tokens", "cached_input_tokens",
                                     "reasoning_tokens", "cost_usd"], "prompt_text": "DO-NOT-EXPORT"}
        for ordinal, session in [(1, "case-a"), (2, "unrelated")]:
            conn.execute("INSERT INTO model_usage_request VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ordinal, str(ordinal), session, "run", "hermes", "longcat", "{}",
                          json.dumps(usage), "succeeded", 1, 2, 1, "logical", 1, ""))
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
                     "'longcat','{}',NULL,'pending',3,NULL,NULL,'logical-3',1,'')")
    combined = collector.collect("variant", path, ["case-a"])
    assert combined["metrics"]["input_tokens"]["value"] is None
    assert combined["metrics"]["input_tokens"]["known_subtotal"] == 100
    assert combined["pending_requests"] == 1
    delta = collector.collect("variant", path, ["case-a"], first)
    assert delta["api_calls"] == 1
    assert [row["key"] for row in delta["records"]] == ["1", "3"]
    assert [row["key"] for row in delta["delta_records"]] == ["3"]


def record(ledger, key, session, *, goal="", provider="hermes", logical=None, attempt=1,
           category="primary", parent_run_id=""):
    ledger.record({
        "manifest_id": key, "logical_call_id": logical or key, "attempt": attempt,
        "call_category": category, "run": {"session_id": session, "run_id": f"{session}-run",
            "parent_run_id": parent_run_id, "work_scope": {"goal_id": goal}},
        "route": {"provider": provider, "model": "fixture-model"},
    })


def test_variant_chains_snapshots_and_accounts_for_late_usage_and_corrections(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.db")
    record(ledger, "r1", "case-a", logical="call", attempt=1)
    ledger.patch_usage("r1", normalize_manifest_usage("hermes", raw_usage={
        "prompt_tokens": 100, "completion_tokens": 10, "cached_input_tokens": 20}))
    ledger.patch_terminal("r1", outcome="failed", duration_s=1)
    first = collector.collect("variant", ledger.path, ["case-a"])

    record(ledger, "r2", "case-a", logical="call", attempt=2)
    second = collector.collect("variant", ledger.path, ["case-a"], first)
    assert second["api_calls"] == 1
    assert second["pending_requests"] == 1
    assert second["delta_records"][0]["key"] == "r2"
    assert second["delta_records"][0]["attempt"] == 2
    idle = collector.collect("variant", ledger.path, ["case-a"], second)
    assert idle["api_calls"] == 0 and idle["delta_records"] == []
    assert idle["pending_requests"] == 1

    ledger.patch_usage("r2", normalize_manifest_usage("hermes", raw_usage={
        "prompt_tokens": 40, "completion_tokens": 2, "cached_input_tokens": 10}))
    ledger.patch_terminal("r2", outcome="succeeded", duration_s=2.5)
    ledger.patch_usage("r1", normalize_manifest_usage("hermes", raw_usage={
        "prompt_tokens": 100, "completion_tokens": 7, "cached_input_tokens": 20}))
    third = collector.collect("variant", ledger.path, ["case-a"], idle)
    assert third["api_calls"] == 0
    assert third["updated_requests"] == 2
    assert third["pending_requests"] == 0
    assert [row["values"]["output_tokens"] for row in third["delta_records"]] == [-3, 2]
    assert third["metrics"]["input_tokens"]["value"] == 40
    assert third["metrics"]["output_tokens"]["value"] == -1
    assert third["duration_sum_s"] == 2.5
    assert [row["values"]["output_tokens"] for row in third["records"]] == [7, 2]

    record(ledger, "r3", "case-a")
    fourth = collector.collect("variant", ledger.path, ["case-a"], third)
    assert fourth["api_calls"] == 1
    assert [row["key"] for row in fourth["delta_records"]] == ["r3"]
    assert [row["key"] for row in fourth["records"]] == ["r1", "r2", "r3"]
    with pytest.raises(ValueError, match="Capture a new variant baseline"):
        collector.collect("variant", ledger.path, ["case-a"], {**fourth, "schema": collector.LEGACY_SCHEMA})


def test_variant_reads_normalized_cache_split_without_losing_goal_lineage(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.db")
    record(ledger, "owner", "owner", goal="goal-a", provider="anthropic")
    record(ledger, "child", "child", goal="goal-a", provider="anthropic",
           parent_run_id="owner-run", category="summary")
    record(ledger, "peer", "peer", goal="goal-b", category="primary")
    record(ledger, "unrelated", "outside", goal="goal-a")
    ledger.patch_usage("owner", normalize_manifest_usage("anthropic", raw_usage={
        "input_tokens": 20, "output_tokens": 5, "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 7, "reasoning_tokens": 0, "cost": 0}))
    ledger.patch_usage("child", normalize_manifest_usage("anthropic", raw_usage={
        "input_tokens": 20, "output_tokens": 2, "cache_creation_input_tokens": 7}))
    ledger.patch_usage("peer", normalize_manifest_usage("hermes", raw_usage={
        "prompt_tokens": 13, "completion_tokens": 3}))

    actual = collector.collect("variant", ledger.path, ["owner", "child", "peer"])
    owner, child, peer = actual["records"]
    assert owner["values"]["input_tokens"] == 1027
    assert owner["values"]["uncached_input_tokens"] == 27
    assert owner["values"]["total_tokens"] == 1032
    assert owner["values"]["reasoning_tokens"] == owner["values"]["cost_usd"] == 0
    assert child["values"]["input_tokens"] is None
    assert child["values"]["uncached_input_tokens"] == 27
    assert child["values"]["total_tokens"] is None
    assert child["call_category"] == "summary" and child["parent_run_id"] == "owner-run"
    assert [row["goal_id"] for row in actual["records"]] == ["goal-a", "goal-a", "goal-b"]
    assert peer["values"]["input_tokens"] == 13
    assert peer["values"]["uncached_input_tokens"] is None
    assert actual["metrics"]["input_tokens"]["known_subtotal"] == 1040


def test_variant_uses_preserved_cache_counts_in_a_sparse_receipt(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.db")
    record(ledger, "r1", "case-a")
    ledger.patch_usage("r1", normalize_manifest_usage("hermes", raw_usage={
        "prompt_tokens": 100, "completion_tokens": 2, "cached_input_tokens": 60}))
    original = collector.collect("variant", ledger.path, ["case-a"])
    ledger.patch_usage("r1", normalize_manifest_usage("hermes", raw_usage={
        "prompt_tokens": 110, "completion_tokens": 3}), partial=True)

    actual = collector.collect("variant", ledger.path, ["case-a"], original)
    assert actual["records"][0]["values"]["uncached_input_tokens"] == 50
    assert actual["metrics"]["uncached_input_tokens"]["value"] == 10
    assert actual["api_calls"] == 0 and actual["updated_requests"] == 1


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
    legacy = {**first, "schema": collector.LEGACY_SCHEMA}
    assert collector.collect("hermes", path, ["case-a"], legacy)["api_calls"] == 1
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
