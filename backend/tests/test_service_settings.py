from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import service_credentials
import service_settings
import ws_service_settings
from model_providers import credentials


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(credentials.secretstore, 'encrypt', lambda value: 'fixture:' + value)
    monkeypatch.setattr(credentials.secretstore, 'decrypt', lambda value: value.removeprefix('fixture:'))
    cfg = {}
    pools = credentials.CredentialPoolStore(cfg, lambda name: name, lambda: None)
    return SimpleNamespace(router=SimpleNamespace(credential_pools=pools, has_cloud_key=lambda _: False))


def definition(service, provider):
    return next(row for row in service_settings.definitions() if row['service'] == service and row['provider'] == provider)


def test_catalog_uses_supported_registries_and_never_reveals_secrets(host):
    service_credentials.replace(host.router, 'models', 'huggingface', 'private-fixture-token')
    result = service_settings.snapshot(host)
    rows = {row['id']: row for row in result['items']}
    assert rows['models:huggingface']['configured']
    assert rows['models:huggingface']['stored']
    assert all('browser:' + name in rows for name in ('browserbase', 'browser-use', 'firecrawl'))
    assert 'tts:elevenlabs' in rows and 'stt:groq' in rows and 'web:exa' in rows
    assert not rows['web:variant1']['editable']
    assert 'private-fixture-token' not in json.dumps(result)
    assert 'secret' not in rows['models:huggingface']


def test_revision_detects_edits_through_original_service_path(host):
    spec = definition('web', 'exa')
    original = service_settings.public_row(host.router, spec)
    service_credentials.replace(host.router, 'web', 'exa', 'other-window-key')
    with pytest.raises(service_settings.SettingsConflict):
        service_settings.mutate(host, 'set', {'service': 'web', 'provider': 'exa', 'key': 'stale-key', 'expected_revision': original['revision']})
    assert asyncio.run(service_credentials.secret(host.router, 'web', 'exa')) == 'other-window-key'
    current = service_settings.public_row(host.router, spec)
    saved = service_settings.mutate(host, 'set', {'service': 'web', 'provider': 'exa', 'key': 'updated-key', 'expected_revision': current['revision']})
    assert saved['item']['revision'] != current['revision']
    assert asyncio.run(service_credentials.secret(host.router, 'web', 'exa')) == 'updated-key'


def test_remove_only_dedicated_key_keeps_environment_fallback(host, monkeypatch):
    monkeypatch.setenv('HF_TOKEN', 'env-fixture-key')
    service_credentials.replace(host.router, 'models', 'huggingface', 'stored-fixture-key')
    before = service_settings.public_row(host.router, definition('models', 'huggingface'))
    result = service_settings.mutate(host, 'clear', {'service': 'models', 'provider': 'huggingface', 'expected_revision': before['revision']})
    assert result['item']['configured'] and not result['item']['stored']
    assert result['item']['source'] == 'environment'
    assert asyncio.run(service_credentials.secret(host.router, 'models', 'huggingface', env_vars=('HF_TOKEN',))) == 'env-fixture-key'


def test_unknown_and_keyless_services_cannot_write(host):
    for service, provider in [('web', 'variant1'), ('web', 'unknown'), ('provider', 'openai')]:
        with pytest.raises(service_settings.SettingsValidation):
            service_settings.mutate(host, 'set', {'service': service, 'provider': provider, 'key': 'fixture'})


def test_ws_correlates_and_redacts_storage_exceptions(host, monkeypatch):
    handlers = {}
    def on(*names):
        def register(fn):
            handlers.update({name: fn for name in names})
            return fn
        return register
    ws_service_settings.register(on)
    frames = []
    async def send_json(frame):
        frames.append(frame)
    socket = SimpleNamespace(send_json=send_json)
    before = service_settings.public_row(host.router, definition('models', 'huggingface'))
    def fail(*args, **kwargs):
        raise ValueError('storage failure containing fixture-secret')
    monkeypatch.setattr(service_credentials, 'replace', fail)
    asyncio.run(handlers['service-settings:set'](host, socket, None, {'type': 'service-settings:set', 'request_id': 'fixture-request',
        'service': 'models', 'provider': 'huggingface', 'expected_revision': before['revision'], 'key': 'fixture-secret'}))
    assert frames[0]['request_id'] == 'fixture-request' and frames[0]['operation'] == 'set'
    assert not frames[0]['ok'] and frames[0]['error']['code'] == 'service_setting_failed'
    assert 'fixture-secret' not in json.dumps(frames)
