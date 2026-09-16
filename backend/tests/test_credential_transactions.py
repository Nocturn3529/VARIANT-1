from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from types import SimpleNamespace

import pytest

from model_providers import credentials
import ws_config


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(credentials.secretstore, 'encrypt', lambda value: 'encrypted:' + value)
    monkeypatch.setattr(credentials.secretstore, 'decrypt', lambda value: value.removeprefix('encrypted:'))
    cfg = {'cloud': {'keys': {'openai': 'encrypted:legacy'}, 'unrelated': {'saved': True}}, 'other': {'saved': True}}
    pool = credentials.CredentialPoolStore(cfg, lambda name: str(name).lower(), lambda: True)
    pool.add(provider='openai', secret='first-key', label='First')
    pool.add('other-provider', 'other-key')
    return pool


@pytest.mark.parametrize('failure', ['false', 'raise'])
@pytest.mark.parametrize('operation', ['add', 'replace', 'clear', 'remove', 'remove_legacy', 'enable', 'priority', 'enable_legacy', 'priority_legacy', 'strategy', 'success', 'failure'])
def test_failed_persistence_restores_only_owned_provider_fields(pool, failure, operation):
    first = pool.public_records('openai')[0]
    lease = credentials.CredentialLease('openai', first['id'], 'First', 'first-key')
    pool._runtime_status[('openai', first['id'])] = {'status': 'cooldown', 'cooldown_until': 99}
    if operation == 'success':
        pool.cfg['cloud']['credential_pools']['openai'][0]['status'] = 'error'
    before = copy.deepcopy(pool.cfg)
    runtime = copy.deepcopy(pool._runtime_status)
    def fail():
        # A callback may update unrelated settings; rolling back the complete
        # config would incorrectly discard those changes.
        pool.cfg['other']['concurrent'] = True
        pool.cfg['cloud']['unrelated']['concurrent'] = True
        pool.cfg['cloud']['pool_strategies']['other-provider'] = 'round_robin'
        if failure == 'raise':
            raise OSError('storage exception including secret-fixture')
        return False
    pool.cfg['cloud'].setdefault('pool_strategies', {})
    before = copy.deepcopy(pool.cfg)
    pool.save = fail
    actions = {
        'add': lambda: pool.add('openai', 'new-key'),
        'replace': lambda: pool.replace('openai', 'new-key'),
        'clear': lambda: pool.clear('openai'),
        'remove': lambda: pool.remove('openai', first['id']),
        'remove_legacy': lambda: pool.remove('openai', 'legacy-primary'),
        'enable': lambda: pool.set_enabled('openai', first['id'], False),
        'priority': lambda: pool.set_priority('openai', first['id'], 4),
        'enable_legacy': lambda: pool.set_enabled('openai', 'legacy-primary', False),
        'priority_legacy': lambda: pool.set_priority('openai', 'legacy-primary', 4),
        'strategy': lambda: pool.set_strategy('openai', 'round_robin'),
        'success': lambda: pool.mark_success(lease),
        'failure': lambda: pool.mark_failure(lease, status_code=429),
    }
    with pytest.raises(RuntimeError, match='Credential settings could not be saved') as error:
        actions[operation]()
    assert 'secret-fixture' not in str(error.value)
    for field in ('credential_pools', 'keys', 'pool_strategies', 'credential_revisions'):
        assert (pool.cfg['cloud'].get(field) or {}).get('openai') == (before['cloud'].get(field) or {}).get('openai')
    assert pool._runtime_status == runtime
    assert pool.cfg['other']['concurrent'] and pool.cfg['cloud']['unrelated']['concurrent']
    assert pool.cfg['cloud']['pool_strategies']['other-provider'] == 'round_robin'


def test_snapshot_revision_commits_and_survives_reload(pool):
    prior = pool.snapshot('openai')
    pool.replace('openai', 'replacement')
    current = pool.snapshot('openai')
    assert current['revision'] == prior['revision'] + 1
    assert current['items'][0]['id'] != prior['items'][0]['id']
    reloaded = credentials.CredentialPoolStore(copy.deepcopy(pool.cfg), lambda value: value, lambda: True)
    assert reloaded.snapshot('openai') == current
    assert 'replacement' not in json.dumps(current)
    pool.clear('openai')
    assert pool.snapshot('openai')['revision'] == current['revision'] + 1
    assert pool.snapshot('openai')['items'] == []


@pytest.mark.asyncio
async def test_list_echoes_identity_and_emits_atomic_revision(pool):
    handlers = {}
    def on(*names):
        def register(fn):
            handlers.update({name: fn for name in names})
            return fn
        return register
    ws_config.register(on)
    frames = []
    async def send_json(frame):
        frames.append(frame)
    host = SimpleNamespace(router=SimpleNamespace(_kn=lambda value: value, credential_pools=pool))
    await handlers['cloud:credential:list'](host, SimpleNamespace(send_json=send_json), None,
        {'provider': 'openai', 'request_id': 'list-fixture'})
    assert frames[0]['request_id'] == 'list-fixture'
    assert frames[0]['revision'] == pool.revision('openai')
    assert frames[0]['items'] == pool.public_records('openai')
    assert 'first-key' not in json.dumps(frames)


def test_legacy_metadata_migrates_without_replacing_secret(pool):
    encrypted = pool.cfg['cloud']['keys']['openai']
    assert pool.set_enabled('openai', 'legacy-primary', False)
    row = next(row for row in pool.records('openai') if row['id'] == 'legacy-primary')
    assert row['secret'] == encrypted and row['enabled'] is False
    assert 'openai' not in pool.cfg['cloud']['keys']
    assert pool.set_priority('openai', 'legacy-primary', 9)
    assert pool.set_enabled('openai', 'legacy-primary', True)
    assert pool.remove('openai', 'legacy-primary')
    assert all(row['id'] != 'legacy-primary' for row in pool.records('openai'))


def test_snapshot_cannot_observe_a_key_before_failed_persistence_finishes(pool):
    before = pool.snapshot('openai')
    saving, release, reading = threading.Event(), threading.Event(), threading.Event()
    def fail_save():
        saving.set()
        assert release.wait(3)
        return False
    def read():
        reading.set()
        return pool.snapshot('openai')
    pool.save = fail_save
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(pool.replace, 'openai', 'uncommitted-key')
        assert saving.wait(3)
        reader = executor.submit(read)
        assert reading.wait(3)
        try:
            with pytest.raises(TimeoutError):
                reader.result(timeout=0.1)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match='could not be saved'):
            writer.result(timeout=3)
        assert reader.result(timeout=3) == before
