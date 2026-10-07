"""Read-only resource/usage evidence for a visible app; never drives its agents."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

import psutil


def usage_snapshot(path, session_ids):
    if not Path(path).is_file():
        return {'available': False, 'sessions': []}
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as conn:
        conn.row_factory = sqlite3.Row
        rows = []
        for identity in session_ids:
            row = conn.execute("SELECT * FROM model_usage_rollup_v2 WHERE kind='session' AND identity=?", (identity,)).fetchone()
            if row is None:
                rows.append({'session_id': identity, 'requests': 0})
                continue
            values = dict(row)
            values.pop('kind', None)
            values.pop('identity', None)
            for field in ('input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens',
                          'cached_input_tokens', 'cost_usd'):
                if not values.get(field + '_known_requests'):
                    values[field] = None
            rows.append({'session_id': identity, **values})
    return {'available': True, 'sessions': rows}


def process_snapshot(owners):
    processes, missing = {}, []
    for label, owner in owners.items():
        try:
            root = psutil.Process(owner['pid'])
            if root.create_time() != owner['created_at']:
                missing.append({'owner': label, 'reason': 'birth_identity_changed'})
                continue
            for process in [root, *root.children(recursive=True)]:
                try:
                    with process.oneshot():
                        memory = process.memory_info()
                        processes[(process.pid, process.create_time())] = {
                            'pid': process.pid, 'created_at': process.create_time(),
                            'name': process.name(), 'rss_bytes': memory.rss,
                            'private_bytes': getattr(memory, 'private', None),
                            'threads': process.num_threads(),
                            'handles': process.num_handles() if hasattr(process, 'num_handles') else None,
                        }
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            missing.append({'owner': label, 'reason': 'unavailable'})
    values = list(processes.values())
    return {'processes': values, 'missing_owners': missing,
            'rss_sum_bytes': sum(row['rss_bytes'] for row in values),
            'rss_basis': 'sum_of_process_rss_includes_shared_pages'}


def database_sizes(root):
    files = {}
    for pattern in ('*.sqlite3', '*.sqlite3-wal', '*.sqlite3-shm', '*.db', '*.db-wal'):
        for path in root.rglob(pattern):
            if path.is_file() and not path.is_symlink():
                files[str(path.relative_to(root))] = path.stat().st_size
    return files


def write_status(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run(phase, interval):
    manifest = json.loads((phase/'phase.json').read_text(encoding='utf-8'))
    root = Path(manifest['source_root'])
    owners = {key: manifest[key] for key in ('electron_owner', 'backend_owner')}
    identity = psutil.Process()
    write_status(phase/'resource-observer-owner.json', {
        'pid': identity.pid, 'created_at': identity.create_time(),
        'phase_root': str(phase), 'source_commit': manifest['source_commit'],
        'control_policy': 'read_only; no prompts, model changes, process actions or restarts',
    })
    with (phase/'resource-samples.jsonl').open('a', encoding='utf-8') as output:
        sample = {'source_commit': manifest['source_commit']}
        while not (phase/'STOP_RESOURCE_OBSERVER').exists():
            tick = time.monotonic()
            current = json.loads((phase/'phase.json').read_text(encoding='utf-8'))
            sample = {'captured_at': datetime.now(timezone.utc).isoformat(),
                      'source_commit': manifest['source_commit'], 'status': 'observing'}
            try:
                sample['resources'] = process_snapshot(owners)
                sample['usage'] = usage_snapshot(root/'data/model-usage.sqlite3',
                    [row['id'] for row in current.get('sessions', [])])
                sample['database_bytes'] = database_sizes(root/'data')
                if sample['resources']['missing_owners']:
                    sample['status'] = 'owner_changed_or_exited; inspect_before_continuing'
            except Exception as error:
                sample.update(status='sample_unavailable', exception_type=type(error).__name__)
            sample['sample_elapsed_s'] = round(time.monotonic()-tick, 4)
            output.write(json.dumps(sample, allow_nan=False)+'\n')
            output.flush()
            write_status(phase/'resource-status.json', sample)
            if sample['status'].startswith('owner_changed_or_exited'):
                return
            time.sleep(max(0.1, interval-(time.monotonic()-tick)))
        write_status(phase/'resource-status.json', {**sample, 'status': 'stopped_by_owner_marker',
            'stopped_at': datetime.now(timezone.utc).isoformat()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('interval must be at least one second')
    run(args.phase.resolve(), args.interval)
