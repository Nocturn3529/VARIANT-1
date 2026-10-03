"""Shared records and frozen stream prefixes for the session evidence reader.

Views contain bounds, not another copy of history. Streams only append; a
different canonical ancestry gets a separate membership stream. This is an
index of retained observations, never a transcript writer or replay engine.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid


def initialize(conn):
    if 'text_id' not in {row['name'] for row in conn.execute('PRAGMA table_info(context_text)')}:
        conn.execute('ALTER TABLE context_text ADD COLUMN text_id INTEGER')
    # Resume an interrupted additive schema upgrade, including stores that
    # previously ran without FTS support. IDs must remain independent of rowid.
    largest = conn.execute('SELECT COALESCE(MAX(text_id),0) FROM context_text').fetchone()[0]
    while True:
        missing = conn.execute('SELECT text_key FROM context_text WHERE text_id IS NULL LIMIT 100').fetchall()
        if not missing:
            break
        for row in missing:
            largest += 1
            conn.execute('UPDATE context_text SET text_id=? WHERE text_key=?', (largest, row['text_key']))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS context_record(
          record_id INTEGER PRIMARY KEY,source_chat_id TEXT NOT NULL,source_id TEXT NOT NULL,
          kind TEXT NOT NULL,version TEXT NOT NULL,descriptor_json TEXT NOT NULL,
          text_key TEXT NOT NULL REFERENCES context_text(text_key),parents_json TEXT,
          UNIQUE(source_chat_id,source_id,version));
        CREATE INDEX IF NOT EXISTS context_record_source ON context_record(source_chat_id,source_id);
        CREATE INDEX IF NOT EXISTS context_record_text ON context_record(text_key);
        CREATE INDEX IF NOT EXISTS context_record_scoped_text ON context_record(text_key,source_chat_id);
        CREATE UNIQUE INDEX IF NOT EXISTS context_text_identity ON context_text(text_id);
        CREATE TABLE IF NOT EXISTS context_stream(
          stream_id TEXT PRIMARY KEY,source_chat_id TEXT NOT NULL,kind TEXT NOT NULL,
          identity TEXT NOT NULL,count INTEGER NOT NULL DEFAULT 0,last_sequence INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS context_stream_owner ON context_stream(source_chat_id,kind,identity);
        CREATE TABLE IF NOT EXISTS context_member(
          stream_id TEXT NOT NULL REFERENCES context_stream(stream_id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,record_id INTEGER NOT NULL REFERENCES context_record(record_id),
          PRIMARY KEY(stream_id,ordinal),UNIQUE(stream_id,record_id));
        CREATE INDEX IF NOT EXISTS context_member_record ON context_member(record_id,stream_id,ordinal);
        CREATE TABLE IF NOT EXISTS context_view_span(
          view_id TEXT NOT NULL REFERENCES context_view(view_id) ON DELETE CASCADE,
          position INTEGER NOT NULL,stream_id TEXT NOT NULL REFERENCES context_stream(stream_id),
          count INTEGER NOT NULL,offset INTEGER NOT NULL,PRIMARY KEY(view_id,position));
        CREATE TABLE IF NOT EXISTS context_canonical_head(
          source_chat_id TEXT NOT NULL,conversation_id TEXT NOT NULL,head_node_id TEXT NOT NULL,
          stream_id TEXT NOT NULL REFERENCES context_stream(stream_id) ON DELETE CASCADE,
          count INTEGER NOT NULL,PRIMARY KEY(source_chat_id,conversation_id,head_node_id));
        CREATE TABLE IF NOT EXISTS context_stream_metadata(
          stream_id TEXT PRIMARY KEY REFERENCES context_stream(stream_id) ON DELETE CASCADE,metadata_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS context_stream_thread(
          stream_id TEXT NOT NULL REFERENCES context_stream(stream_id) ON DELETE CASCADE,thread_id TEXT NOT NULL,
          PRIMARY KEY(stream_id,thread_id));
        CREATE TABLE IF NOT EXISTS context_canonical_latest(
          source_chat_id TEXT NOT NULL,conversation_id TEXT NOT NULL,head_node_id TEXT NOT NULL,
          stream_id TEXT NOT NULL REFERENCES context_stream(stream_id) ON DELETE CASCADE,
          count INTEGER NOT NULL,PRIMARY KEY(source_chat_id,conversation_id));
        CREATE INDEX IF NOT EXISTS context_views_scope ON context_view(chat_id,child_id,created_at DESC,view_id DESC);
    """)
    # External content avoids a third stored copy of searchable text. Triggers
    # cover deletion too, and creation backfills databases predating the index.
    existed = conn.execute("SELECT sql FROM sqlite_master WHERE name='context_fts'").fetchone()
    if existed and 'content_rowid' not in existed['sql']:
        conn.executescript('DROP TRIGGER IF EXISTS context_text_fts_insert; DROP TRIGGER IF EXISTS context_text_fts_delete; DROP TABLE context_fts;')
        existed = None
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5(search_folded,content='context_text',content_rowid='text_id',tokenize='trigram case_sensitive 1')")
        conn.executescript("""
          CREATE TRIGGER IF NOT EXISTS context_text_fts_insert AFTER INSERT ON context_text BEGIN
            UPDATE context_text SET text_id=(SELECT COALESCE(MAX(text_id),0)+1 FROM context_text) WHERE text_key=new.text_key AND text_id IS NULL;
            INSERT INTO context_fts(rowid,search_folded) SELECT text_id,search_folded FROM context_text WHERE text_key=new.text_key; END;
          CREATE TRIGGER IF NOT EXISTS context_text_fts_delete AFTER DELETE ON context_text BEGIN
            INSERT INTO context_fts(context_fts,rowid,search_folded) VALUES('delete',old.text_id,old.search_folded); END;
        """)
        if not existed:
            conn.execute("INSERT INTO context_fts(context_fts) VALUES('rebuild')")
        indexed = True
    except sqlite3.OperationalError as exc:
        if 'fts5' not in str(exc).lower() and 'tokenizer' not in str(exc).lower():
            raise
        indexed = False
    migrate(conn)
    return indexed


def stream(conn, owner, kind, identity, *, fresh=False):
    row = None if fresh else conn.execute(
        "SELECT * FROM context_stream WHERE source_chat_id=? AND kind=? AND identity=? ORDER BY rowid DESC LIMIT 1",
        (owner, kind, identity)).fetchone()
    if row is None:
        sid = uuid.uuid4().hex
        conn.execute("INSERT INTO context_stream(stream_id,source_chat_id,kind,identity) VALUES(?,?,?,?)", (sid, owner, kind, identity))
        row = conn.execute("SELECT * FROM context_stream WHERE stream_id=?", (sid,)).fetchone()
    return dict(row)


def record(conn, owner, descriptor, text_key, *, parents=None):
    descriptor = dict(descriptor)
    descriptor.pop('head_node_id', None)
    descriptor.pop('is_head', None)
    descriptor['source_chat_id'] = owner
    encoded = json.dumps(descriptor, ensure_ascii=False, sort_keys=True)
    version = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    conn.execute("INSERT OR IGNORE INTO context_record(source_chat_id,source_id,kind,version,descriptor_json,text_key,parents_json) VALUES(?,?,?,?,?,?,?)",
                 (owner, descriptor['source_id'], descriptor['kind'], version, encoded, text_key,
                  json.dumps(parents) if parents is not None else None))
    row = conn.execute("SELECT record_id FROM context_record WHERE source_chat_id=? AND source_id=? AND version=?", (owner, descriptor['source_id'], version)).fetchone()
    if parents is not None:
        conn.execute("UPDATE context_record SET parents_json=? WHERE record_id=? AND parents_json IS NULL", (json.dumps(parents), row[0]))
    return row[0]


def append(conn, target, rid, *, sequence=0):
    conn.execute("INSERT INTO context_member VALUES(?,?,?)", (target['stream_id'], target['count'], rid))
    target['count'] += 1
    target['last_sequence'] = max(target['last_sequence'], sequence)
    conn.execute("UPDATE context_stream SET count=?,last_sequence=? WHERE stream_id=?", (target['count'], target['last_sequence'], target['stream_id']))


def latest(conn, owner, conversation, head, target):
    conn.execute('INSERT INTO context_canonical_latest VALUES(?,?,?,?,?) ON CONFLICT(source_chat_id,conversation_id) DO UPDATE SET head_node_id=excluded.head_node_id,stream_id=excluded.stream_id,count=excluded.count',
                 (owner, conversation, head, target['stream_id'], target['count']))


def span(conn, view_id, target, position, offset):
    conn.execute("INSERT INTO context_view_span VALUES(?,?,?,?,?)", (view_id, position, target['stream_id'], target['count'], offset))
    return offset + target['count']


def migrate(conn):
    """Preserve old view IDs and membership transactionally, with bounded rows.

    Existing per-view memberships are coalesced when they are exact prefixes.
    Canonical heads in legacy descriptors are view metadata, not source versions.
    """
    if not conn.execute('SELECT 1 FROM context_source LIMIT 1').fetchone():
        return
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    for view in conn.execute('SELECT * FROM context_view ORDER BY created_at,view_id'):
        owner = view['source_chat_id'] or view['chat_id']
        offset, position = 0, 0
        for kind in ('message', 'cell', 'snapshot'):
            target = stream(conn, owner, kind, 'legacy')
            n = 0
            for row in conn.execute('SELECT * FROM context_source WHERE view_id=? AND kind=? ORDER BY ordinal', (view['view_id'], kind)):
                rid = record(conn, owner, json.loads(row['descriptor_json']), row['text_key'])
                old = conn.execute('SELECT record_id FROM context_member WHERE stream_id=? AND ordinal=?', (target['stream_id'], n)).fetchone()
                if old is not None and old[0] != rid:
                    previous = target
                    target = stream(conn, owner, kind, 'legacy', fresh=True)
                    conn.execute('INSERT INTO context_member SELECT ?,ordinal,record_id FROM context_member WHERE stream_id=? AND ordinal<?', (target['stream_id'], previous['stream_id'], n))
                    target['count'] = n
                if n >= target['count']:
                    append(conn, target, rid)
                n += 1
            if n:
                frozen = {**target, 'count': n}
                offset = span(conn, view['view_id'], frozen, position, offset)
                position += 1
                cursor = json.loads(view['watermarks_json']).get('canonical')
                if kind == 'message' and cursor:
                    conn.execute('INSERT OR IGNORE INTO context_canonical_head VALUES(?,?,?,?,?)', (owner, cursor['conversation_id'], cursor['head_node_id'] or '', target['stream_id'], n))
                    latest(conn, owner, cursor['conversation_id'], cursor['head_node_id'] or '', frozen)
        conn.execute('DELETE FROM context_source WHERE view_id=?', (view['view_id'],))


def _spans(conn, view_id, after, kind):
    return conn.execute('SELECT v.*,s.kind FROM context_view_span v JOIN context_stream s ON s.stream_id=v.stream_id WHERE v.view_id=? AND v.offset+v.count>? ' + ('AND s.kind=? ' if kind else '') + 'ORDER BY v.position', (view_id, after, *([kind] if kind else [])))


def rows(conn, view_id, *, after=0, limit=51, kind='', source_id='', folded='', indexed=False):
    owner = conn.execute("SELECT COALESCE(NULLIF(source_chat_id,''),chat_id) FROM context_view WHERE view_id=?", (view_id,)).fetchone()
    owner = owner[0] if owner else ''
    if source_id:
        # Force the exact-ID index before reverse membership, rather than
        # letting the optimizer walk a whole view for one historical handle.
        return conn.execute('SELECT v.offset+m.ordinal AS ordinal,s.descriptor_json,substr(p.search_text,1,600) AS search_text '
            'FROM context_record s CROSS JOIN context_member m CROSS JOIN context_view_span v CROSS JOIN context_text p '
            'WHERE s.source_chat_id=? AND s.source_id=? AND m.record_id=s.record_id AND v.stream_id=m.stream_id '
            'AND m.ordinal<v.count AND p.text_key=s.text_key AND v.view_id=? ORDER BY ordinal LIMIT ?',
            (owner, source_id, view_id, limit)).fetchall()
    candidates = None
    if folded and indexed and len(folded) >= 3 and '\0' not in folded:
        phrase = '"' + folded.replace('"', '""') + '"'
        # Bound the global FTS probe too: a small selected session must not
        # walk every matching document in an unrelated session's archive.
        texts = [row[0] for row in conn.execute('SELECT rowid FROM context_fts WHERE context_fts MATCH ? LIMIT 513', (phrase,))]
        # Bounded selectivity probe counts actual scoped records, not distinct
        # texts: a single deduplicated text can represent millions of events.
        # An incomplete global candidate set falls back to the complete scoped
        # ordered scan; never return a false empty result from a bounded probe.
        if len(texts) <= 512:
            if not texts:
                candidates = []
            else:
                placeholders = ','.join('?' for _ in texts)
                trial = conn.execute('SELECT s.record_id FROM context_text p CROSS JOIN context_record s '
                    'WHERE p.text_id IN (' + placeholders + ') AND s.text_key=p.text_key '
                    'AND s.source_chat_id=? AND instr(p.search_folded,?)>0 LIMIT 129', (*texts, owner, folded)).fetchall()
                if len(trial) <= 128:
                    candidates = [row[0] for row in trial]
    if candidates is not None:
        if not candidates:
            return []
        placeholders = ','.join('?' for _ in candidates)
        # At most 128 records enter the ordering step; source versions outside
        # this frozen view are rejected before pagination.
        return conn.execute('SELECT v.offset+m.ordinal AS ordinal,s.descriptor_json,p.search_text '
            'FROM context_record s CROSS JOIN context_member m CROSS JOIN context_view_span v CROSS JOIN context_text p '
            'WHERE s.record_id IN (' + placeholders + ') AND m.record_id=s.record_id '
            'AND v.view_id=? AND v.stream_id=m.stream_id AND m.ordinal<v.count AND m.ordinal>=max(0,?-v.offset) '
            'AND p.text_key=s.text_key ' + ('AND s.kind=? ' if kind else '') + 'ORDER BY ordinal LIMIT ?',
            (*candidates, view_id, after, *([kind] if kind else []), limit)).fetchall()
    # Common matches and short queries use ordered membership keysets. LIMIT
    # stops immediately after this page; no global computed-ordinal sort and
    # no Python candidate array proportional to history.
    result = []
    for part in _spans(conn, view_id, after, kind):
        remaining = limit - len(result)
        if remaining <= 0:
            break
        args = [part['offset'], part['stream_id'], max(0, after-part['offset']), part['count']]
        where = 'WHERE m.stream_id=? AND m.ordinal>=? AND m.ordinal<? AND s.record_id=m.record_id AND p.text_key=s.text_key '
        if folded:
            where += 'AND instr(p.search_folded,?)>0 '
            args.append(folded)
        args.append(remaining)
        projection = 'p.search_text' if folded else 'substr(p.search_text,1,600) AS search_text'
        result.extend(conn.execute('SELECT ?+m.ordinal AS ordinal,s.descriptor_json,' + projection + ' '
            'FROM context_member m CROSS JOIN context_record s CROSS JOIN context_text p ' + where +
            'ORDER BY m.ordinal LIMIT ?', args).fetchall())
    return result
