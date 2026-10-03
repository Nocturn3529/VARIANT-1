"""Durable metadata-only physical-request accounting, independent of UI windows."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import time

from core_invariants import sqlite_session_connection

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
    "cached_input_tokens", "cache_write_input_tokens", "tool_prompt_tokens",
    "prompt_token_volume", "uncached_input_tokens", "token_volume")
_IDENTITY_FIELDS = ("provider_returned_model_id", "model_revision", "system_fingerprint", "provider_generation_id")


def _text(value, maximum=300):
    return str(value or "")[:maximum]


def _usage(value):
    result = {}
    for key in TOKEN_FIELDS:
        raw = value.get(key)
        result[key] = min(raw, 10**12) if type(raw) is int and raw >= 0 else None
    for key in ("measurement", "call_category"):
        result[key] = _text(value.get(key), 80)
    for key in ("provider_reported", "estimated"):
        result[key] = value.get(key) is True
    reported = value.get("reported_fields")
    reported = reported if isinstance(reported, (list, tuple)) else ()
    result["reported_fields"] = [key for key in TOKEN_FIELDS if key in reported]
    cost = value.get("cost_usd")
    if type(cost) in (int, float) and 0 <= cost < 10**12:
        result["cost_usd"] = cost
    return result


class ModelUsageLedger:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS model_usage_request(
                  ordinal INTEGER PRIMARY KEY AUTOINCREMENT,manifest_id TEXT NOT NULL UNIQUE,
                  logical_call_id TEXT NOT NULL,attempt INTEGER NOT NULL,session_id TEXT NOT NULL,
                  goal_id TEXT NOT NULL,run_id TEXT NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,
                  started_at REAL NOT NULL,metadata_json TEXT NOT NULL,usage_json TEXT,
                  response_json TEXT NOT NULL DEFAULT '{}',outcome TEXT NOT NULL DEFAULT 'pending');
                CREATE INDEX IF NOT EXISTS model_usage_session ON model_usage_request(session_id,ordinal);
                CREATE INDEX IF NOT EXISTS model_usage_goal ON model_usage_request(goal_id,ordinal);
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite_session_connection(self.path, autocommit=False)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record(self, manifest):
        identity = _text(manifest.get("manifest_id"), 160)
        if not identity:
            raise ValueError("A physical request manifest ID is required")
        run, route, generation = (manifest.get(key) or {} for key in ("run", "route", "generation"))
        scope = run.get("work_scope") or {}
        metadata = {"parent_run_id": _text(run.get("parent_run_id")), "thread_id": _text(run.get("thread_id")),
            "source": _text(run.get("source")), "step_id": _text(scope.get("step_id")),
            "call_category": _text(manifest.get("call_category"), 80),
            "reasoning_effort": _text(generation.get("reasoning_effort"), 24),
            "physical_mode": _text(route.get("physical_mode"), 24), "adapter": _text(route.get("adapter"), 80)}
        stamp = manifest.get("captured_at")
        stamp = float(stamp) if type(stamp) in (int, float) and 0 <= stamp < 10**12 else time.time()
        attempt = manifest.get("attempt")
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO model_usage_request(manifest_id,logical_call_id,attempt,session_id,goal_id,run_id,provider,model,started_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (identity, _text(manifest.get("logical_call_id")), attempt if type(attempt) is int else 0,
                 _text(run.get("session_id")), _text(scope.get("goal_id")), _text(run.get("run_id")),
                 _text(route.get("provider"), 80), _text(route.get("model")), stamp, json.dumps(metadata)))

    def patch_usage(self, identity, usage):
        with self._connect() as conn:
            changed = conn.execute("UPDATE model_usage_request SET usage_json=?,outcome='usage_observed' WHERE manifest_id=?",
                (json.dumps(_usage(usage), allow_nan=False), _text(identity, 160))).rowcount
        return bool(changed)

    def patch_response(self, identity, metadata):
        with self._connect() as conn:
            row = conn.execute("SELECT response_json FROM model_usage_request WHERE manifest_id=?", (_text(identity, 160),)).fetchone()
            if row is None:
                return False
            response = json.loads(row[0])
            response.update({key: _text(metadata[key]) for key in _IDENTITY_FIELDS if metadata.get(key)})
            conn.execute("UPDATE model_usage_request SET response_json=? WHERE manifest_id=?", (json.dumps(response), _text(identity, 160)))
        return True

    def get(self, identity):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM model_usage_request WHERE manifest_id=?", (_text(identity, 160),)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row):
        result = dict(row)
        for name in ("metadata", "usage", "response"):
            raw = result.pop(name + "_json")
            result[name] = json.loads(raw) if raw is not None else None
        return result

    def read(self, *, after=0, limit=100, session_id="", goal_id=""):
        cap = max(1, min(int(limit), 500))
        where, params = "ordinal>?", [max(0, int(after))]
        for key, value in (("session_id", session_id), ("goal_id", goal_id)):
            if value:
                where += " AND " + key + "=?"
                params.append(str(value))
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM model_usage_request WHERE " + where + " ORDER BY ordinal LIMIT ?", (*params, cap + 1)).fetchall()
        return {"items": [self._row(row) for row in rows[:cap]], "has_more": len(rows) > cap,
            "next_cursor": rows[cap-1]["ordinal"] if len(rows) > cap else None}

    def totals(self, *, session_id="", goal_id=""):
        where, params = "1=1", []
        for key, value in (("session_id", session_id), ("goal_id", goal_id)):
            if value:
                where += " AND " + key + "=?"
                params.append(str(value))
        fields = ["COUNT(*) AS requests", "SUM(usage_json IS NOT NULL) AS usage_observed_requests"]
        for key in TOKEN_FIELDS:
            expression = "json_extract(usage_json,'$." + key + "')"
            fields.extend(("SUM(" + expression + ") AS " + key, "COUNT(" + expression + ") AS " + key + "_known_requests"))
        with self._connect() as conn:
            row = conn.execute("SELECT " + ",".join(fields) + " FROM model_usage_request WHERE " + where, params).fetchone()
        return dict(row)
