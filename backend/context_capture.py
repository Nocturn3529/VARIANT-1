"""Incremental source-owner ingestion; pending DAG walks spill into SQLite."""
from __future__ import annotations

import hashlib
import json

import context_index as index
from tools import ToolError


def canonical(service, conn, owner, head, text_projection):
    if not head:
        return None, None
    conversation, node = head['conversation_id'], head['head_node_id'] or ''
    exact = conn.execute('SELECT * FROM context_canonical_head WHERE source_chat_id=? AND conversation_id=? AND head_node_id=?', (owner, conversation, node)).fetchone()
    if exact:
        target = dict(conn.execute('SELECT * FROM context_stream WHERE stream_id=?', (exact['stream_id'],)).fetchone())
        target['count'] = exact['count']
        index.latest(conn, owner, conversation, node, target)
        return target, {**head, 'node_count': target['count'], 'index_order': 'stable causal prefix'}
    previous = conn.execute('SELECT * FROM context_canonical_latest WHERE source_chat_id=? AND conversation_id=?', (owner, conversation)).fetchone()
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS context_frontier(node_id TEXT PRIMARY KEY)')
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS context_seen(node_id TEXT PRIMARY KEY)')
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS context_pending(node_id TEXT PRIMARY KEY,record_id INTEGER NOT NULL,stamp REAL NOT NULL,indegree INTEGER NOT NULL DEFAULT 0)')
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS context_edges(parent TEXT,child TEXT,PRIMARY KEY(parent,child))')
    conn.execute('CREATE INDEX IF NOT EXISTS context_pending_ready ON context_pending(indegree,stamp,node_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS context_edges_child ON context_edges(child,parent)')

    def walk(prefix):
        for table in ('context_frontier', 'context_seen', 'context_pending', 'context_edges'):
            conn.execute('DELETE FROM ' + table)
        if node:
            conn.execute('INSERT INTO context_frontier VALUES(?)', (node,))
        reached = False
        while True:
            batch = [row[0] for row in conn.execute('SELECT node_id FROM context_frontier LIMIT 100')]
            if not batch:
                break
            for current in batch:
                conn.execute('DELETE FROM context_frontier WHERE node_id=?', (current,))
                if conn.execute('SELECT 1 FROM context_seen WHERE node_id=?', (current,)).fetchone():
                    continue
                conn.execute('INSERT INTO context_seen VALUES(?)', (current,))
                if prefix and conn.execute(
                    'SELECT 1 FROM context_record r JOIN context_member m ON m.record_id=r.record_id '
                    'WHERE r.source_chat_id=? AND r.source_id=? AND m.stream_id=? AND m.ordinal<?',
                    (owner, 'message:' + current, prefix['stream_id'], prefix['count'])).fetchone():
                    reached |= current == prefix['head_node_id']
                    continue
                cached = conn.execute('SELECT * FROM context_record WHERE source_chat_id=? AND source_id=? AND parents_json IS NOT NULL ORDER BY record_id DESC LIMIT 1', (owner, 'message:' + current)).fetchone()
                if cached:
                    parents = json.loads(cached['parents_json'])
                    descriptor = json.loads(cached['descriptor_json'])
                    rid, stamp = cached['record_id'], descriptor['timestamp']
                else:
                    rows = service.sessions.context_nodes(owner, conversation, [current])
                    if len(rows) != 1:
                        raise ToolError('Captured canonical context source is unavailable')
                    row = rows[0]
                    parents, stamp = row['parents'], row['created_at']
                    meta = json.loads(row['metadata_json'])
                    descriptor = {'source_id': 'message:' + current, 'kind': 'message', 'role': row['role'],
                                  'node_id': current, 'conversation_id': conversation,
                                  'run_id': meta.get('run_id'), 'turn_id': row.get('turn_id'), 'timestamp': stamp,
                                  'coverage': 'captured canonical content; readable-text search only',
                                  'sha256': hashlib.sha256(row['content_json'].encode('utf-8')).hexdigest()}
                    rid = index.record(conn, owner, descriptor, service._index_text(conn, text_projection(json.loads(row['content_json']))), parents=parents)
                conn.execute('INSERT INTO context_pending(node_id,record_id,stamp) VALUES(?,?,?)', (current, rid, stamp))
                for parent in parents:
                    conn.execute('INSERT OR IGNORE INTO context_edges VALUES(?,?)', (parent, current))
                    conn.execute('INSERT OR IGNORE INTO context_frontier SELECT ? WHERE NOT EXISTS(SELECT 1 FROM context_seen WHERE node_id=?)', (parent, parent))
        return reached

    reached = walk(previous)
    if previous and not reached:
        # Fork/rewind/merge with a different prefix: reuse immutable records, but
        # certify this ancestry separately. No giant Python graph is allocated.
        walk(None)
        previous = None
    if previous:
        target = dict(conn.execute('SELECT * FROM context_stream WHERE stream_id=?', (previous['stream_id'],)).fetchone())
        if target['count'] != previous['count']:
            original = target
            target = index.stream(conn, owner, 'message', conversation, fresh=True)
            conn.execute('INSERT INTO context_member SELECT ?,ordinal,record_id FROM context_member WHERE stream_id=? AND ordinal<?', (target['stream_id'], original['stream_id'], previous['count']))
            target['count'] = previous['count']
            conn.execute('UPDATE context_stream SET count=? WHERE stream_id=?', (target['count'], target['stream_id']))
    else:
        target = index.stream(conn, owner, 'message', conversation, fresh=True)
    conn.execute('UPDATE context_pending SET indegree=(SELECT COUNT(*) FROM context_edges e JOIN context_pending p ON p.node_id=e.parent WHERE e.child=context_pending.node_id)')
    while True:
        ready = conn.execute('SELECT * FROM context_pending WHERE indegree=0 ORDER BY stamp,node_id LIMIT 1').fetchone()
        if ready is None:
            if conn.execute('SELECT 1 FROM context_pending LIMIT 1').fetchone():
                raise ToolError('Session context ancestry contains a cycle')
            break
        index.append(conn, target, ready['record_id'])
        conn.execute('UPDATE context_pending SET indegree=indegree-1 WHERE node_id IN (SELECT child FROM context_edges WHERE parent=?)', (ready['node_id'],))
        conn.execute('DELETE FROM context_pending WHERE node_id=?', (ready['node_id'],))
    conn.execute('INSERT INTO context_canonical_head VALUES(?,?,?,?,?)', (owner, conversation, node, target['stream_id'], target['count']))
    index.latest(conn, owner, conversation, node, target)
    return target, {**head, 'node_count': target['count'], 'index_order': 'stable causal prefix'}


def cells(service, conn, owner, upper):
    target = index.stream(conn, owner, 'cell', 'ledger')
    after = target['last_sequence']
    while after < upper:
        page = service.kernel.execution_history(owner, after_sequence=after, through_sequence=upper, limit=100)
        for cell in page['items']:
            descriptor = {**cell, 'source_id': 'cell:' + cell['execution_id'], 'kind': 'cell',
                          'coverage': 'retained cell evidence; omissions reported on expansion'}
            texts = []
            for part in ('source', 'result'):
                try:
                    raw = service._artifact_text(cell[part + '_ref'], owner, str(cell.get(part + '_sha256') or ''))
                    if part == 'source':
                        texts.append(raw)
                    else:
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError('Cell result must be an object')
                        texts.append(str(value.get('text') or value.get('error') or ''))
                        evidence = value.get('output_evidence') or {}
                        descriptor['output_evidence'] = {key: evidence[key] for key in ('ref', 'sha256') if isinstance(evidence, dict) and key in evidence}
                except (KeyError, ValueError, OSError, ToolError):
                    descriptor['search_incomplete'] = True
            rid = index.record(conn, owner, descriptor, service._index_text(conn, '\n'.join(texts)))
            index.append(conn, target, rid, sequence=int(cell['sequence']))
        next_sequence = int(page['next_sequence'])
        if next_sequence <= after:
            raise ToolError('Cell history cursor did not advance during context capture')
        after = next_sequence
    return target


def snapshots(service, conn, owner):
    store = getattr(service.runtimes, 'snapshot_store', None)
    if store is None:
        return None, {}, None
    native = hasattr(store, 'context_boundary_sync')
    boundary = store.context_boundary_sync(owner) if native else {'through_ordinal': 0, 'epoch': 0}
    target = index.stream(conn, owner, 'snapshot', 'commits:' + str(boundary['epoch']))
    saved = conn.execute('SELECT metadata_json FROM context_stream_metadata WHERE stream_id=?', (target['stream_id'],)).fetchone()
    metadata = json.loads(saved[0]) if saved else {'snapshot_heads': {}, 'snapshot_head_count': 0}
    after = target['last_sequence']

    def ingest(raw, ordinal):
        clean = {key: value for key, value in raw.items() if key not in {'ordinal', 'is_head'}}
        desc = {**clean, 'source_id': 'snapshot:' + raw['snapshot_id'], 'kind': 'snapshot',
                'coverage': 'retained committed native projection; may repeat earlier records; metadata search only'}
        index.append(conn, target, index.record(conn, owner, desc, service._index_text(conn, json.dumps(clean, ensure_ascii=False))), sequence=ordinal)
        inserted = conn.execute('INSERT OR IGNORE INTO context_stream_thread VALUES(?,?)', (target['stream_id'], raw['thread_id'])).rowcount
        metadata['snapshot_head_count'] += inserted
        heads = metadata['snapshot_heads']
        if raw['thread_id'] in heads or len(heads) < 200:
            heads[raw['thread_id']] = {key: raw[key] for key in ('sequence', 'snapshot_id')}

    if native:
        upper = boundary['through_ordinal']
        while after < upper:
            page = store.context_commit_page_sync(owner, after_ordinal=after, through_ordinal=upper, limit=100)
            if not page:
                break  # Ordinals can have deleted/tombstoned gaps.
            for raw in page:
                ingest(raw, int(raw['ordinal']))
            after = int(page[-1]['ordinal'])
        current = store.context_boundary_sync(owner)
        if current['epoch'] != boundary['epoch']:
            raise ToolError('Native source ownership changed during context capture')
        # A deleted last ordinal need not be observed; epoch changes select a
        # new membership stream. Append-only records remain bounded by upper.
        target['last_sequence'] = upper
        conn.execute('UPDATE context_stream SET last_sequence=? WHERE stream_id=?', (upper, target['stream_id']))
    else:
        # Older adapter compatibility. Production native owner uses stable,
        # incrementally maintained ordinals and never enumerates all cursors.
        for ordinal, raw in enumerate(store.context_cursors_sync(owner), 1):
            if ordinal > after:
                ingest(raw, ordinal)
        boundary['through_ordinal'] = target['last_sequence']
    metadata['snapshot_heads_complete'] = metadata['snapshot_head_count'] <= 200
    conn.execute('INSERT INTO context_stream_metadata VALUES(?,?) ON CONFLICT(stream_id) DO UPDATE SET metadata_json=excluded.metadata_json', (target['stream_id'], json.dumps(metadata)))
    return target, metadata, boundary
