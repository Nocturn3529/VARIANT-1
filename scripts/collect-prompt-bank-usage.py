"""Read-only, session-scoped usage evidence for manual prompt-bank comparisons.

Never imports either harness, opens credentials, reads messages, or submits work.
Hermes aggregates cannot prove that a stored zero was reported by the provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time


FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens",
          "cache_write_input_tokens", "uncached_input_tokens", "reasoning_tokens",
          "total_tokens", "cost_usd")
HERMES_COUNTERS = ("api_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd",
                  "actual_cost_usd")


def connection(path):
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    deadline = time.monotonic() + 5
    return conn


def variant_records(conn, session_ids):
    records = []
    for identity in session_ids:
        for row in conn.execute(
            "SELECT manifest_id,session_id,run_id,provider,model,metadata_json,usage_json,"
            "outcome,started_at,finished_at,duration_s FROM model_usage_request "
            "WHERE session_id=? ORDER BY ordinal", (identity,)
        ):
            usage = json.loads(row["usage_json"] or "{}")
            reported = set(usage.get("reported_fields") or [])
            values = {key: usage.get(key) if key in reported else None for key in FIELDS}
            # Nous uses inclusive input. Do not derive uncached from a missing
            # cache counter: the normalizer's fallback zero is not evidence.
            if values["input_tokens"] is not None and values["cached_input_tokens"] is not None:
                values["uncached_input_tokens"] = max(
                    0, values["input_tokens"] - values["cached_input_tokens"])
            metadata = json.loads(row["metadata_json"] or "{}")
            records.append({
                "key": row["manifest_id"], "session_id": identity, "run_id": row["run_id"],
                "provider": row["provider"], "model": row["model"],
                "source": metadata.get("source"), "parent_run_id": metadata.get("parent_run_id"),
                "reasoning_effort": metadata.get("reasoning_effort"),
                "outcome": row["outcome"], "duration_s": row["duration_s"],
                "finished_at": row["finished_at"], "values": values,
                "reported_fields": sorted(reported),
            })
    return records


def hermes_records(conn, session_ids):
    records = []
    for identity in session_ids:
        for row in conn.execute(
            "SELECT * FROM session_model_usage WHERE session_id=?", (identity,)
        ):
            # Hash routing identity for baseline matching; never export its URL.
            key = hashlib.sha256(json.dumps(
                [identity, row["model"], row["billing_provider"], row["billing_base_url"],
                 row["billing_mode"], row["task"]]).encode()).hexdigest()
            records.append({
                "key": key, "session_id": identity, "model": row["model"],
                "provider": row["billing_provider"], "task": row["task"],
                "raw_counters": {field: row[field] for field in HERMES_COUNTERS},
                "cost_status": row["cost_status"], "cost_source": row["cost_source"],
            })
    return records


def summarize(harness, records, before=None):
    previous = {row["key"]: row for row in (before or {}).get("records", [])}
    if harness == "variant":
        records = [row for row in records if row["key"] not in previous]
        values = [row["values"] for row in records]
        count = len(records)
        metrics = {}
        for field in FIELDS:
            known = [row[field] for row in values if row.get(field) is not None]
            metrics[field] = {
                "value": sum(known) if count and len(known) == count else None,
                "known_subtotal": sum(known) if known else None,
                "coverage": f"{len(known)}/{count} physical requests",
            }
        pending = sum(row["finished_at"] is None for row in records)
        return {"records": records, "metrics": metrics, "api_calls": count,
                "pending_requests": pending, "scope": "only explicit session IDs",
                "duration_sum_s": sum(row["duration_s"] or 0 for row in records),
                "duration_coverage": f"{sum(row['duration_s'] is not None for row in records)}/{count}",
                "timing_note": "API durations can overlap; not task wall time"}
    deltas = []
    for row in records:
        prior = previous.get(row["key"], {}).get("raw_counters", {})
        delta = {key: (row["raw_counters"][key] or 0) - (prior.get(key) or 0)
                 for key in HERMES_COUNTERS}
        if any(value < 0 for value in delta.values()):
            raise ValueError("Hermes counters decreased; baseline no longer matches")
        deltas.append({**row, "raw_counters": delta})
    total = {key: sum(row["raw_counters"][key] for row in deltas) for key in HERMES_COUNTERS}
    prompt = total["input_tokens"] + total["cache_read_tokens"] + total["cache_write_tokens"]
    raw_values = {
        "input_tokens": prompt, "output_tokens": total["output_tokens"],
        "cached_input_tokens": total["cache_read_tokens"],
        "cache_write_input_tokens": total["cache_write_tokens"],
        "uncached_input_tokens": total["input_tokens"] + total["cache_write_tokens"],
        "reasoning_tokens": total["reasoning_tokens"],
        "total_tokens": prompt + total["output_tokens"],
    }
    metrics = {key: {"value": value if value > 0 else None, "raw_aggregate": value,
                     "coverage": "aggregate only; missing fields can be stored as zero"}
               for key, value in raw_values.items()}
    metrics["cost_usd"] = {"value": None, "raw_actual_aggregate": total["actual_cost_usd"],
                          "raw_estimated_aggregate": total["estimated_cost_usd"],
                          "coverage": "verify cost_status/source; default zero is ambiguous"}
    return {"records": records, "delta_records": deltas, "metrics": metrics,
            "api_calls": total["api_call_count"], "pending_requests": None,
            "scope": "only explicit session IDs; do not add child aggregates without checking rollup",
            "timing_note": "use external send-to-settlement timestamps for task wall time"}


def collect(harness, database, session_ids, before=None):
    session_ids = list(dict.fromkeys(session_ids))
    if not session_ids or any(not identity.strip() for identity in session_ids):
        raise ValueError("Explicit nonempty benchmark session IDs are required")
    if before and (before.get("harness") != harness or before.get("session_ids") != session_ids):
        raise ValueError("Baseline harness/session identities do not match")
    conn = connection(database)
    try:
        conn.execute("BEGIN")
        rows = (variant_records if harness == "variant" else hermes_records)(conn, session_ids)
    finally:
        conn.close()
    return {"schema": "variant1.prompt-bank-usage.v1", "harness": harness,
            "session_ids": session_ids, "captured_at_unix": time.time(),
            "captured_monotonic_ns": time.monotonic_ns(),
            **summarize(harness, rows, before)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=("variant", "hermes"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--session-id", required=True, action="append")
    parser.add_argument("--before", help="Optional earlier snapshot for this exact case")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError("Choose a new evidence path; existing files are retained")
    if output.resolve() == Path(args.database).resolve():
        raise ValueError("Evidence output cannot replace the usage database")
    before = json.loads(Path(args.before).read_text(encoding="utf-8-sig")) if args.before else None
    result = collect(args.harness, args.database, args.session_id, before)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "api_calls": result["api_calls"],
                      "pending_requests": result["pending_requests"]}))


if __name__ == "__main__":
    main()
