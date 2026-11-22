"""Algorithmic and source-integrity checks for shared frozen context views."""
import json
import sqlite3
import time
import tracemalloc
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from external_context import SessionContextService
from tests.test_external_session_context import fixture
from tools import ToolError


def counts(service):
    with sqlite3.connect(service.path) as conn:
        return {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('context_record', 'context_member', 'context_view', 'context_text')}


def test_capture_and_reopen_read_only_delta_for_many_saved_views(tmp_path, monkeypatch):
    service, sessions, chat, artifacts, _, runtimes = fixture(tmp_path)
    sessions.append_messages(chat, [{'role': 'user', 'text': 'item ' + str(i)} for i in range(1200)])
    reads = []
    original = sessions.context_nodes
    def observe(sid, conversation, nodes):
        reads.extend(nodes)
        return original(sid, conversation, nodes)
    monkeypatch.setattr(sessions, 'context_nodes', observe)
    monkeypatch.setattr(sessions, 'context_sources', lambda *a: pytest.fail('Whole history reader used'))
    import psutil
    process = psutil.Process()
    rss_before = process.memory_info().rss
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        old = service.capture(chat)
        initial_seconds = time.perf_counter()-started
        # A preexisting tracer's global peak cannot be attributed to capture.
        python_peak = None if already_tracing else tracemalloc.get_traced_memory()[1]
    finally:
        if not already_tracing:
            tracemalloc.stop()
    first_bytes = Path(service.path).stat().st_size
    assert len(reads) == 1202
    reads.clear()
    refresh_times = []
    for _ in range(25):
        started = time.perf_counter()
        service.capture(chat)
        refresh_times.append(time.perf_counter()-started)
    view_bytes = Path(service.path).stat().st_size
    assert reads == []
    assert counts(service)['context_record'] == counts(service)['context_member'] == 1202
    reopened = SessionContextService(database_path=service.path, sessions=sessions, kernel=service.kernel,
                                     artifacts=artifacts, runtimes=runtimes)
    sessions.append_messages(chat, [{'role': 'user', 'text': 'one new observation'}])
    started = time.perf_counter()
    new = reopened.capture(chat)
    append_seconds = time.perf_counter()-started
    assert len(reads) == 1
    assert counts(service)['context_record'] == counts(service)['context_member'] == 1203
    assert service.status(chat, old)['counts']['message'] == 1202
    assert service.status(chat, new)['counts']['message'] == 1203
    tail = service.read(chat, old, after=1200, limit=2)['items']
    assert [row['ordinal'] for row in tail] == [1200, 1201]
    print('SCALING_RESOURCES ' + json.dumps({'records_first': 1202, 'records_final': 1203, 'views': 27,
          'first_capture_seconds': initial_seconds, 'unchanged_refresh_seconds_median': sorted(refresh_times)[12],
          'append_capture_seconds': append_seconds, 'python_peak_bytes_initial_capture': python_peak,
          'rss_before_bytes': rss_before, 'rss_after_bytes': process.memory_info().rss,
          'index_main_db_bytes_first': first_bytes, 'index_main_db_bytes_after_25_more_views': view_bytes,
          'index_main_db_bytes_after_one_new_record': Path(service.path).stat().st_size}))


def test_divergent_heads_and_merged_dag_keep_frozen_membership(tmp_path):
    service, sessions, chat, *_ = fixture(tmp_path)
    base = sessions.context_head(chat)
    old = service.capture(chat)
    old_rows = service.read(chat, old)['items']
    original_head = base['head_node_id']
    first_node = old_rows[0]['node_id']
    with sessions.repository._write() as conn:
        conn.execute('UPDATE conversation_branch SET head_node_id=? WHERE runtime_chat_id=?', (first_node, chat))
    sessions.append_messages(chat, [{'role': 'user', 'text': 'divergent observation'}])
    divergent_head = sessions.context_head(chat)['head_node_id']
    divergent = service.capture(chat)
    assert [row['preview'] for row in service.read(chat, divergent)['items']] == [old_rows[0]['preview'], 'divergent observation']
    # Old captured observation remains authorized even after a live rewind.
    assert service.expand(chat, old, source_id=old_rows[-1]['source_id'])['text'] == 'The plan is preserved.'
    with sessions.repository._write() as conn:
        repository = sessions.repository
        merged = repository._insert_node_tx(conn, conversation_id=base['conversation_id'], role='assistant',
                  content='merged conclusion', content_ref='', metadata={}, turn_id='', created_at=1.0)
        for parent in (original_head, divergent_head):
            repository._insert_edge_tx(conn, conversation_id=base['conversation_id'], from_node_id=parent,
                  to_node_id=merged, kind='merge', metadata={}, created_at=1.0)
        conn.execute('UPDATE conversation_branch SET head_node_id=? WHERE runtime_chat_id=?', (merged, chat))
    merged_view = service.capture(chat)
    rows = service.read(chat, merged_view)['items']
    assert len(rows) == 4 and rows[-1]['preview'] == 'merged conclusion'
    assert len({row['source_id'] for row in rows}) == 4
    assert service.search(chat, divergent, query='preserved')['items'] == []
    assert len(service.read(chat, old)['items']) == 2


def test_view_list_keysets_are_scoped_and_do_not_capture_sources(tmp_path, monkeypatch):
    service, sessions, chat, *_ = fixture(tmp_path)
    for _ in range(7):
        service.capture(chat)
    monkeypatch.setattr(sessions, 'context_head', lambda *a: pytest.fail('View listing touched source heads'))
    pages, after = [], ''
    while True:
        page = service.list_views(chat, after=after, limit=2)
        pages.extend(row['view_id'] for row in page['items'])
        if not page['has_more']:
            break
        after = page['next_cursor']
    assert len(pages) == len(set(pages)) == 7
    with pytest.raises(ToolError, match='outside'):
        service.list_views(sessions.create_session(), after=after)
    with pytest.raises(ToolError, match='invalid'):
        service.list_views(chat, after='not-a-cursor')


@pytest.mark.parametrize('query', ['A%_B', '"quoted"', '修复登录', 'STRASSE', '😀😀😀', 'a', '%', 'a\x00b'])
def test_fts_matches_literal_casefold_scan_with_exact_unicode_offsets(tmp_path, query):
    service, sessions, chat, *_ = fixture(tmp_path)
    sessions.append_messages(chat, [{'role': 'user', 'text': 'prefix A%_B "quoted" 修复登录 Straße 😀😀😀 a\x00b tail'}])
    view = service.capture(chat)
    indexed = service.search(chat, view, query=query)
    service._search_indexed = False
    scanned = service.search(chat, view, query=query)
    assert indexed['items'] == scanned['items']
    assert indexed['has_more'] == scanned['has_more']
    assert indexed['items']
    for row in indexed['items']:
        text = service.expand(chat, view, source_id=row['source_id'])['text']
        assert text[row['match_offset']:].casefold().startswith(query.casefold())


def test_streamed_artifact_reads_once_and_fails_on_late_corruption(tmp_path, monkeypatch):
    service, _, chat, artifacts, cells, _ = fixture(tmp_path)
    text = 'é😀\n' * 40000
    source = artifacts.put_text(text, kind='kernel_cell_source', scope=chat)
    result = artifacts.put_json({'text': 'done'}, kind='kernel_cell_result', scope=chat)
    cells.append({'sequence': 1, 'execution_id': 'large', 'source_ref': source.ref, 'result_ref': result.ref})
    view = service.capture(chat)
    calls = []
    original = artifacts.iter_bytes
    def observed(ref, **kwargs):
        calls.append(ref)
        yield from original(ref, **kwargs)
    monkeypatch.setattr(artifacts, 'iter_bytes', observed)
    parts = list(service.iter_expansion(chat, view, 'cell:large', part='source', chunk_chars=31))
    assert ''.join(part['text'] for part in parts) == text
    assert all(len(part['text']) <= 31 for part in parts)
    assert parts[-1]['eof'] and parts[-1]['integrity'] == 'verified'
    assert parts[-1]['total_chars'] == len(text) and calls == [source.ref]
    raw = Path(artifacts._path(source.sha256)).read_bytes()
    Path(artifacts._path(source.sha256)).write_bytes(raw[:-1] + b'x')
    seen_eof = False
    with pytest.raises(OSError, match='identity'):
        for part in service.iter_expansion(chat, view, 'cell:large', part='source', chunk_chars=1000):
            seen_eof |= part['eof']
    assert not seen_eof


def test_concurrent_services_publish_one_shared_append_prefix(tmp_path):
    service, sessions, chat, artifacts, _, runtimes = fixture(tmp_path)
    service.capture(chat)
    other = SessionContextService(database_path=service.path, sessions=sessions, kernel=service.kernel,
                                  artifacts=artifacts, runtimes=runtimes)
    sessions.append_messages(chat, [{'role': 'user', 'text': 'concurrent new event'}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(lambda reader: reader.capture(chat), [service, other]))
    assert len(set(views)) == 2
    assert counts(service)['context_record'] == counts(service)['context_member'] == 3
    assert all(service.status(chat, view)['counts']['message'] == 3 for view in views)


def test_legacy_saved_ids_migrate_transactionally_to_shared_prefixes(tmp_path):
    service, sessions, chat, artifacts, _, runtimes = fixture(tmp_path)
    old = service.capture(chat)
    original = service.read(chat, old)['items']
    # Recreate a v1 view-only index, using the original authoritative records.
    with sqlite3.connect(service.path) as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        records = list(conn.execute('SELECT r.descriptor_json,r.text_key FROM context_record r JOIN context_member m ON m.record_id=r.record_id ORDER BY m.ordinal'))
        conn.execute('DELETE FROM context_view_span')
        conn.execute('DELETE FROM context_canonical_head')
        conn.execute('DELETE FROM context_member')
        conn.execute('DELETE FROM context_stream')
        conn.execute('DELETE FROM context_record')
        for ordinal, (descriptor_json, text_key) in enumerate(records):
            descriptor = json.loads(descriptor_json)
            descriptor['head_node_id'] = sessions.context_head(chat)['head_node_id']
            conn.execute('INSERT INTO context_source VALUES(?,?,?,?,?,?)', (old, ordinal, descriptor['source_id'], descriptor['kind'], json.dumps(descriptor), text_key))
    reopened = SessionContextService(database_path=service.path, sessions=sessions, kernel=service.kernel,
                                     artifacts=artifacts, runtimes=runtimes)
    assert [row['source_id'] for row in reopened.read(chat, old)['items']] == [row['source_id'] for row in original]
    assert reopened.search(chat, old, query='STRASSE')['items']
    reopened.capture(chat)
    assert counts(service)['context_record'] == counts(service)['context_member'] == 2
    assert reopened.expand(chat, old, source_id=original[0]['source_id'])['text'] == original[0]['preview']


def native(service, runtimes, tmp_path):
    from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
    store = SQLiteRunSnapshotStore(str(tmp_path / 'native.sqlite3'))
    runtimes.snapshot_store = store
    return store


def commit(store, chat, thread, text):
    from agent_engine.state import new_run_state
    state = new_run_state(source='chat', title='History', goal='Retain evidence', thread_id=thread, run_id=thread)
    state['chat_id'] = chat
    state['messages'] = [{'role': 'user', 'content': text}]
    return store.commit_boundary_sync(state, completed_node='init', next_node='prepare', expected_head_sequence=None)


def test_many_native_threads_share_one_prefix_and_only_ingest_new_commits(tmp_path, monkeypatch):
    service, _, chat, _, _, runtimes = fixture(tmp_path)
    store = native(service, runtimes, tmp_path)
    for i in range(80):
        commit(store, chat, 'thread-' + str(i), 'observation ' + str(i))
    rows = []
    original = store.context_commit_page_sync
    def observed(*args, **kwargs):
        page = original(*args, **kwargs)
        rows.extend(page)
        return page
    monkeypatch.setattr(store, 'context_commit_page_sync', observed)
    monkeypatch.setattr(store, 'context_cursors_sync', lambda *args: pytest.fail('All native cursors materialized'))
    monkeypatch.setattr(store, 'context_heads_page_sync', lambda *args, **kwargs: pytest.fail('All native threads visited'), raising=False)
    first = service.capture(chat)
    assert len(rows) == 80
    rows.clear()
    for _ in range(20):
        service.capture(chat)
    assert rows == []
    new = commit(store, chat, 'one-more-thread', 'new observation')
    latest = service.capture(chat)
    assert len(rows) == 1 and rows[0]['snapshot_id'] == new.snapshot_id
    with sqlite3.connect(service.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM context_view_span').fetchone()[0] == 22 * 2
        assert conn.execute('SELECT COUNT(*) FROM context_member').fetchone()[0] == 83
    assert service.status(chat, first)['counts']['snapshot'] == 80
    assert service.status(chat, latest)['counts']['snapshot'] == 81


def test_native_cursor_survives_vacuum_deletion_row_reuse_and_owner_reassignment(tmp_path):
    service, sessions, chat, _, _, runtimes = fixture(tmp_path)
    store = native(service, runtimes, tmp_path)
    first = commit(store, chat, 'first', 'first observation')
    second = commit(store, chat, 'second', 'second observation')
    old = service.capture(chat)
    bound = store.context_boundary_sync(chat)
    with sqlite3.connect(str(tmp_path / 'native.sqlite3')) as conn:
        conn.execute('VACUUM')
    assert store.context_boundary_sync(chat) == bound
    store.delete_thread_sync('second')
    new = commit(store, chat, 'third', 'third observation')
    newer = service.capture(chat)
    assert store.context_boundary_sync(chat)['through_ordinal'] > bound['through_ordinal']
    ids = [row['source_id'] for row in service.read(chat, newer, kind='snapshot')['items']]
    assert ids == ['snapshot:' + first.snapshot_id, 'snapshot:' + new.snapshot_id]
    assert len(service.read(chat, old, kind='snapshot')['items']) == 2
    with pytest.raises(ToolError) as error:
        service.expand(chat, old, source_id='snapshot:' + second.snapshot_id)
    assert error.value.code == 'context_source_unavailable'
    other = sessions.create_session()
    with sqlite3.connect(str(tmp_path / 'native.sqlite3')) as conn:
        conn.execute('UPDATE agent_threads SET chat_id=? WHERE thread_id=?', (other, 'third'))
    after_move = service.capture(chat)
    assert [row['source_id'] for row in service.read(chat, after_move, kind='snapshot')['items']] == ['snapshot:' + first.snapshot_id]


def test_index_vacuum_and_deletion_leave_literal_fts_identity_consistent(tmp_path):
    service, sessions, chat, *_ = fixture(tmp_path)
    old = service.capture(chat)
    with sqlite3.connect(service.path) as conn:
        conn.execute('VACUUM')
    assert service.search(chat, old, query='STRASSE')['items']
    service.delete_chat(chat)
    other = sessions.create_session()
    sessions.append_messages(other, [{'role': 'user', 'text': 'separate new needle'}])
    fresh = service.capture(other)
    assert service.search(other, fresh, query='new needle')['items']
    assert not service.search(other, fresh, query='STRASSE')['items']


def test_rare_common_late_page_and_exact_id_work_do_not_scan_whole_history(tmp_path, monkeypatch):
    service, sessions, chat, *_ = fixture(tmp_path)
    original = service._connect
    active = [False]
    steps = [0]
    @contextmanager
    def measured():
        with original() as conn:
            if active[0]:
                def progress():
                    steps[0] += 1
                    return 0
                conn.set_progress_handler(progress, 1)
            yield conn
    monkeypatch.setattr(service, '_connect', measured)
    def measure(action):
        steps[0] = 0
        active[0] = True
        try:
            result = action()
            return steps[0], result
        finally:
            active[0] = False
    results = []
    for size in (400, 2000):
        before = 0 if size == 400 else 400
        sessions.append_messages(chat, [{'role': 'user', 'text': 'common repeated observation'} for _ in range(size-before)])
        sessions.append_messages(chat, [{'role': 'user', 'text': 'unique needle marker ' + str(size)}])
        view = service.capture(chat)
        rare, hits = measure(lambda: service.search(chat, view, query='unique needle marker ' + str(size)))
        assert len(hits['items']) == 1
        exact, expanded = measure(lambda: service.expand(chat, view, source_id=hits['items'][0]['source_id']))
        assert expanded['text'].endswith(str(size))
        common, page = measure(lambda: service.search(chat, view, query='common repeated', limit=10))
        assert len(page['items']) == 10 and page['has_more']
        later, page = measure(lambda: service.search(chat, view, query='common repeated', limit=10, after=size//2))
        assert len(page['items']) == 10 and page['has_more']
        read, page = measure(lambda: service.read(chat, view, limit=10, after=size//2))
        assert len(page['items']) == 10
        refresh, _ = measure(lambda: service.capture(chat))
        results.append({'size': size, 'rare': rare, 'common': common, 'later': later, 'read': read, 'exact': exact, 'refresh': refresh})
    for key in ('rare', 'common', 'later', 'read', 'exact', 'refresh'):
        assert results[1][key] < results[0][key]*2 + 300, (key, results)
    print('SCALING_VM_STEPS ' + json.dumps(results))


def test_cold_history_ancestry_uses_edge_key_lookups_as_history_grows(tmp_path, monkeypatch):
    service, sessions, chat, *_ = fixture(tmp_path)
    repository = sessions.repository
    original = repository._read
    steps, active = [0], [False]
    @contextmanager
    def observed():
        with original() as conn:
            if active[0]:
                def count():
                    steps[0] += 1
                    return 0
                conn.set_progress_handler(count, 1)
            yield conn
    monkeypatch.setattr(repository, '_read', observed)
    measured = []
    previous = 0
    for size in (300, 1200):
        sessions.append_messages(chat, [{'role': 'user', 'text': 'cold history ' + str(i)} for i in range(previous, size)])
        head = sessions.context_head(chat)
        steps[0], active[0] = 0, True
        try:
            rows = repository.history_nodes(head['branch_id'], limit=10)
        finally:
            active[0] = False
        assert len(rows) == 10 and rows[-1].content == 'cold history ' + str(size - 1)
        measured.append(steps[0])
        previous = size
    assert measured[1] <= measured[0] * 6 + 1000


def test_global_fts_probe_is_bounded_and_does_not_hide_scoped_matches(tmp_path, monkeypatch):
    service, sessions, chat, *_ = fixture(tmp_path)
    foreign = sessions.create_session()
    sessions.append_messages(foreign, [{'role': 'user', 'text': 'Shared probe keyword foreign ' + str(i)} for i in range(700)])
    service.capture(foreign)
    sessions.append_messages(chat, [{'role': 'user', 'text': 'Shared probe keyword selected evidence'}])
    view = service.capture(chat)
    steps = [0]
    original = service._connect
    @contextmanager
    def observed():
        with original() as conn:
            def count():
                steps[0] += 1
                return 0
            conn.set_progress_handler(count, 1)
            yield conn
    monkeypatch.setattr(service, '_connect', observed)
    first = service.search(chat, view, query='Shared probe keyword')
    first_steps = steps[0]
    assert len(first['items']) == 1 and 'selected evidence' in first['items'][0]['snippet']
    sessions.append_messages(foreign, [{'role': 'user', 'text': 'Shared probe keyword extra ' + str(i)} for i in range(1000)])
    service.capture(foreign)
    steps[0] = 0
    later = service.search(chat, view, query='Shared probe keyword')
    bounded_steps = steps[0]
    assert later['items'] == first['items']
    # FTS segment/page work can change even with a fixed emitted candidate
    # bound. Reject proportional archive scans, rather than demand identical
    # engine-internal work across different index layouts.
    assert bounded_steps <= first_steps * 1.6 + 1000
    steps[0] = 0
    with service._connect() as conn:
        conn.execute('SELECT s.record_id FROM context_fts f CROSS JOIN context_text p CROSS JOIN context_record s '
                     'WHERE context_fts MATCH ? AND p.text_id=f.rowid AND s.text_key=p.text_key '
                     'AND s.source_chat_id=? AND instr(p.search_folded,?)>0 LIMIT 129',
                     ('"shared probe keyword"', chat, 'shared probe keyword')).fetchall()
    assert bounded_steps < steps[0] * .6 + 1000


def test_export_missing_artifacts_is_an_omission_but_corruption_is_fatal(tmp_path):
    from context_export import export_view
    service, _, chat, artifacts, cells, _ = fixture(tmp_path)
    source = artifacts.put_text('saved source', kind='kernel_cell_source', scope=chat)
    result = artifacts.put_json({'text': 'saved result'}, kind='kernel_cell_result', scope=chat)
    cells.append({'sequence': 1, 'execution_id': 'one', 'source_ref': source.ref, 'result_ref': result.ref})
    view = service.capture(chat)
    Path(artifacts._path(source.sha256)).unlink()
    receipt = export_view(service, chat, view, path=str(tmp_path / 'omitted.jsonl'))
    assert receipt['omission_count'] == 2  # Missing source and absent output.
    Path(artifacts._path(result.sha256)).write_bytes(b'broken result evidence')
    target = tmp_path / 'broken.jsonl'
    with pytest.raises(OSError, match='identity'):
        export_view(service, chat, view, path=str(target))
    assert not target.exists()
