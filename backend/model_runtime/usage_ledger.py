"""Durable metadata-only physical-request accounting, independent of UI windows."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import time
from datetime import datetime, timezone

from core_invariants import sqlite_session_connection

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens",
    "cached_input_tokens", "cache_write_input_tokens", "tool_prompt_tokens",
    "prompt_token_volume", "uncached_input_tokens", "token_volume")
_IDENTITY_FIELDS = ("provider_returned_model_id", "model_revision", "system_fingerprint", "provider_generation_id")
_COUNT_FIELDS = TOKEN_FIELDS + ('cost_usd',)


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
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
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
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(model_usage_request)')}
            for name in ('finished_at', 'duration_s'):
                if name not in columns:
                    conn.execute('ALTER TABLE model_usage_request ADD COLUMN ' + name + ' REAL')
            fields = ','.join(f'{key} REAL NOT NULL DEFAULT 0,{key}_known_requests INTEGER NOT NULL DEFAULT 0,{key}_reported_requests INTEGER NOT NULL DEFAULT 0' for key in _COUNT_FIELDS)
            conn.execute('CREATE TABLE IF NOT EXISTS model_usage_rollup(kind TEXT NOT NULL,identity TEXT NOT NULL,requests INTEGER NOT NULL DEFAULT 0,usage_observed_requests INTEGER NOT NULL DEFAULT 0,' + fields + ',PRIMARY KEY(kind,identity))')
            conn.execute('CREATE TABLE IF NOT EXISTS model_usage_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL)')
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute("SELECT value FROM model_usage_meta WHERE key='rollups-v1'").fetchone() is None:
                for row in conn.execute('SELECT * FROM model_usage_request'):
                    self._rollup(conn, row, request_delta=1, after=json.loads(row['usage_json']) if row['usage_json'] else None)
                conn.execute("INSERT INTO model_usage_meta VALUES('rollups-v1',1)")

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
            conn.execute('BEGIN IMMEDIATE')
            inserted = conn.execute("INSERT OR IGNORE INTO model_usage_request(manifest_id,logical_call_id,attempt,session_id,goal_id,run_id,provider,model,started_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (identity, _text(manifest.get("logical_call_id")), attempt if type(attempt) is int else 0,
                 _text(run.get("session_id")), _text(scope.get("goal_id")), _text(run.get("run_id")),
                 _text(route.get("provider"), 80), _text(route.get("model")), stamp, json.dumps(metadata))).rowcount
            if inserted:
                row = conn.execute('SELECT * FROM model_usage_request WHERE manifest_id=?', (identity,)).fetchone()
                self._rollup(conn, row, request_delta=1)

    def patch_usage(self, identity, usage, *, partial=False):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM model_usage_request WHERE manifest_id=?', (_text(identity,160),)).fetchone()
            if row is None:
                return False
            cleaned = _usage(usage)
            prior = json.loads(row['usage_json']) if row['usage_json'] else None
            if partial and prior:
                for key in _COUNT_FIELDS:
                    if cleaned.get(key) is None and prior.get(key) is not None:
                        cleaned[key] = prior[key]
                cleaned['reported_fields'] = [key for key in TOKEN_FIELDS
                    if key in cleaned['reported_fields'] or key in prior.get('reported_fields',[])]
            changed = conn.execute("UPDATE model_usage_request SET usage_json=?,outcome=CASE WHEN finished_at IS NULL THEN 'usage_observed' ELSE outcome END WHERE manifest_id=?",
                (json.dumps(cleaned, allow_nan=False), _text(identity, 160))).rowcount
            self._rollup(conn, row, before=prior, after=cleaned)
        return bool(changed)

    def patch_response(self, identity, metadata):
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
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

    def patch_terminal(self, identity, *, outcome, duration_s=None):
        if outcome not in {'succeeded', 'failed', 'cancelled', 'superseded', 'unknown'}:
            raise ValueError('Invalid physical request outcome')
        duration = float(duration_s) if type(duration_s) in (int, float) and 0 <= duration_s < 10**12 else None
        with self._connect() as conn:
            return bool(conn.execute('UPDATE model_usage_request SET outcome=?,finished_at=?,duration_s=? WHERE manifest_id=?',
                (outcome, time.time(), duration, _text(identity, 160))).rowcount)

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

    @staticmethod
    def _rollup(conn, row, *, request_delta=0, before=None, after=None):
        day = datetime.fromtimestamp(row['started_at'], timezone.utc).strftime('%Y-%m-%d')
        identities = [('all',''), ('session',row['session_id']), ('goal',row['goal_id']),
                      ('model',json.dumps([row['provider'],row['model']])), ('day',day)]
        deltas = {'requests':request_delta, 'usage_observed_requests':int(after is not None)-int(before is not None)}
        for key in _COUNT_FIELDS:
            old, new = (before or {}).get(key), (after or {}).get(key)
            deltas[key] = (new or 0) - (old or 0)
            deltas[key+'_known_requests'] = int(new is not None) - int(old is not None)
            deltas[key+'_reported_requests'] = int(key in ((after or {}).get('reported_fields') or [])) - int(key in ((before or {}).get('reported_fields') or []))
        columns = ','.join(deltas)
        updates = ','.join(f'{key}={key}+excluded.{key}' for key in deltas)
        for kind, identity in identities:
            conn.execute('INSERT INTO model_usage_rollup(kind,identity,' + columns + ') VALUES(' + ','.join('?' for _ in range(len(deltas)+2)) + ') ON CONFLICT(kind,identity) DO UPDATE SET ' + updates,
                         (kind,identity,*deltas.values()))

    @staticmethod
    def _totals_row(row):
        result = dict(row or {'requests':0,'usage_observed_requests':0})
        for key in _COUNT_FIELDS:
            if not result.get(key+'_known_requests'):
                result[key] = None
            result.setdefault(key+'_known_requests',0)
            result.setdefault(key+'_reported_requests',0)
        result.pop('kind',None)
        result.pop('identity',None)
        return result

    def groups(self, kind):
        if kind not in {'session','goal','model','day'}:
            raise ValueError('Unknown usage grouping')
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM model_usage_rollup WHERE kind=? ORDER BY identity', (kind,)).fetchall()
        return [{'identity':row['identity'], **self._totals_row(row)} for row in rows]

    def totals(self, *, session_id="", goal_id=""):
        if not (session_id and goal_id):
            kind, identity = ('session',session_id) if session_id else ('goal',goal_id) if goal_id else ('all','')
            with self._connect() as conn:
                row = conn.execute('SELECT * FROM model_usage_rollup WHERE kind=? AND identity=?', (kind,identity)).fetchone()
            return self._totals_row(row)
        where, params = "1=1", []
        for key, value in (("session_id", session_id), ("goal_id", goal_id)):
            if value:
                where += " AND " + key + "=?"
                params.append(str(value))
        fields = ["COUNT(*) AS requests", "COUNT(usage_json) AS usage_observed_requests"]
        for key in _COUNT_FIELDS:
            expression = "json_extract(usage_json,'$." + key + "')"
            fields.extend(("SUM(" + expression + ") AS " + key, "COUNT(" + expression + ") AS " + key + "_known_requests"))
            fields.append("SUM(EXISTS(SELECT 1 FROM json_each(json_extract(usage_json,'$.reported_fields')) WHERE value='" + key + "')) AS " + key + "_reported_requests")
        with self._connect() as conn:
            row = conn.execute("SELECT " + ",".join(fields) + " FROM model_usage_request WHERE " + where, params).fetchone()
        return self._totals_row(row)
