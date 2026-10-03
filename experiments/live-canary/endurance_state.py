"""Restart-safe observer journal; it never chooses the agents' next work."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import time


@contextmanager
def observer_lease(root):
    """Process-crash-safe local lock; only one observer may own a lab backend."""
    handle = (Path(root)/'.observer.lock').open('a+b')
    handle.seek(0,2)
    if handle.tell() == 0:
        handle.write(b'0');handle.flush()
    handle.seek(0)
    try:
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another observer owns this endurance lab') from None
    try:
        yield
    finally:
        handle.close()


class EnduranceJournal:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS event(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,kind TEXT NOT NULL,identity TEXT UNIQUE,payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as conn:
            conn.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (key,json.dumps(value,allow_nan=False)))

    def append(self, kind, payload, *, identity=None):
        # Callers allowlist metadata. Credentials and transcript content stay outside this journal.
        with self.connect() as conn:
            return bool(conn.execute('INSERT OR IGNORE INTO event(created_at,kind,identity,payload) VALUES(?,?,?,?)',
                (time.time(),kind,identity,json.dumps(payload,allow_nan=False))).rowcount)

    def events(self, *, after=0, limit=200):
        with self.connect() as conn:
            rows = conn.execute('SELECT * FROM event WHERE sequence>? ORDER BY sequence LIMIT ?',
                                (after,max(1,min(limit,500)))).fetchall()
        return [{**dict(row),'payload':json.loads(row['payload'])} for row in rows]


def export_usage(ledger, directory):
    """Stream one consistent metadata-only snapshot; do not accumulate days of rows."""
    import csv
    from model_runtime.usage_ledger import TOKEN_FIELDS
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    fields = ('manifest_id','logical_call_id','attempt','session_id','goal_id','run_id','provider','model',
              'started_at','finished_at','duration_s','outcome',*TOKEN_FIELDS,'cost_usd','measurement','reported_fields')
    json_path, csv_path = destination/'requests.jsonl', destination/'requests.csv'
    # Snapshot isolation is intentional; a live correction lands in the next export.
    json_temp, csv_temp = json_path.with_name(json_path.name+'.tmp'), csv_path.with_name(csv_path.name+'.tmp')
    with ledger._connect() as conn, json_temp.open('w',encoding='utf-8',newline='') as json_file, csv_temp.open('w',encoding='utf-8',newline='') as csv_file:
        conn.execute('BEGIN')
        cursor = conn.execute('SELECT * FROM model_usage_request ORDER BY ordinal')
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        while rows := cursor.fetchmany(200):
            for raw in rows:
                row = ledger._row(raw)
                json_file.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
                flattened = {**row, **(row['usage'] or {})}
                flattened['reported_fields'] = json.dumps(flattened.get('reported_fields',[]))
                writer.writerow({key:flattened.get(key) for key in fields})
    json_temp.replace(json_path)
    csv_temp.replace(csv_path)
    summary = {'schema':'variant1.endurance-usage.v1','totals':ledger.totals(),
               'groups':{kind:ledger.groups(kind) for kind in ('model','session','goal','day')},
               'limitations':'Unknown counters are null. Reported counts show field coverage; reasoning is already included in output where the provider defines it that way. Superseded attempt durations are unknown.'}
    (destination/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary
