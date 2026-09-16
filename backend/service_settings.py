"""Settings projection over existing service registries and credential authority."""
from __future__ import annotations

import hashlib
import json
import os

import service_credentials


def definitions():
    from web_search.providers import provider_definitions
    from speech.providers import STT_PROVIDERS, TTS_PROVIDERS
    from browser_fabric.settings import settings_catalog

    rows = []
    for service, group, category, registry in (
        ('web', 'Web search', 'search', provider_definitions()),
        ('stt', 'Speech input', 'voice', STT_PROVIDERS),
        ('tts', 'Speech output', 'voice', TTS_PROVIDERS),
    ):
        for provider in registry:
            rows.append({**provider, 'service': service, 'provider': provider['id'],
                         'group': group, 'settings_category': category})
    for provider in settings_catalog()['cloud_providers']:
        rows.append({'service': provider['credential_service'], 'provider': provider['id'],
                     'name': {'browserbase': 'Browserbase', 'browser-use': 'Browser Use', 'firecrawl': 'Firecrawl'}.get(provider['id'], provider['id']),
                     'group': 'Browser', 'settings_category': 'browser', 'auth': 'api_key',
                     'description': 'Credential for cloud browser sessions.'})
    rows.append({'service': 'models', 'provider': 'huggingface', 'name': 'Hugging Face',
                 'group': 'Model downloads', 'settings_category': 'local-models', 'auth': 'optional',
                 'env_vars': ('HF_TOKEN',), 'description': 'Access token for gated model downloads.'})
    return rows


def public_row(router, definition):
    service, provider = definition['service'], definition['provider']
    key = service_credentials.credential_key(service, provider)
    records = router.credential_pools.public_records(key)
    env_vars = definition.get('env_vars', ())
    shared = definition.get('shared_provider', '')
    configured = service_credentials.configured(router, service, provider, env_vars=env_vars, shared_provider=shared)
    if records:
        source = 'stored'
    elif shared and router.has_cloud_key(shared):
        source = 'account'
    elif any(os.environ.get(name, '').strip() for name in env_vars):
        source = 'environment'
    else:
        source = ''
    # Key replacement changes its random ID even when the masked status stays
    # configured. This also observes edits through the original service pages.
    identity = [[row.get('id'), row.get('enabled'), row.get('priority')] for row in records]
    revision = hashlib.sha256(json.dumps([key, identity, source], sort_keys=True).encode()).hexdigest()
    return {'id': f'{service}:{provider}', 'service': service, 'provider': provider,
            'name': definition['name'], 'group': definition['group'],
            'description': definition.get('description', ''),
            'settings_category': definition['settings_category'],
            'editable': definition.get('auth') in {'api_key', 'optional', 'shared'},
            'configured': configured, 'stored': bool(records), 'source': source,
            'revision': revision}


def snapshot(host):
    rows = [public_row(host.router, definition) for definition in definitions()]
    gateway = getattr(host, 'gateway', None)
    if gateway is not None:
        for adapter in gateway.public_state().get('adapters', []):
            rows.append({'id': 'messaging:' + adapter['id'], 'service': 'messaging', 'provider': adapter['id'],
                         'name': adapter.get('display_name') or adapter['id'], 'group': 'Messaging',
                         'description': adapter.get('description', ''), 'settings_category': 'messaging',
                         'editable': False, 'configured': bool(adapter.get('credential_configured')),
                         'stored': False, 'source': '', 'revision': ''})
    return {'items': rows}


class SettingsValidation(ValueError):
    pass


class SettingsConflict(SettingsValidation):
    pass


def mutate(host, operation, message):
    definition = next((item for item in definitions()
                       if item['service'] == message.get('service') and item['provider'] == message.get('provider')), None)
    if definition is None or definition.get('auth') not in {'api_key', 'optional', 'shared'}:
        raise SettingsValidation('Choose a service that accepts an API key.')
    before = public_row(host.router, definition)
    if message.get('expected_revision') != before['revision']:
        raise SettingsConflict('This credential changed. Refresh and review it before saving.')
    if operation == 'set':
        secret = message.get('key')
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 65536:
            raise SettingsValidation('Enter an API key.')
        service_credentials.replace(host.router, definition['service'], definition['provider'], secret)
    elif operation == 'clear':
        service_credentials.clear(host.router, definition['service'], definition['provider'])
    else:
        raise SettingsValidation('Unsupported credential operation.')
    return {'item': public_row(host.router, definition)}
