from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from speech import local_tts, providers


def configured(url='http://127.0.0.1:8880/v1'):
    return {'tts': {'kokoro': {'base_url': url, 'model': 'kokoro'}}}


@pytest.mark.asyncio
async def test_frozen_missing_engine_is_explicit_even_when_models_are_present(monkeypatch):
    monkeypatch.setattr(local_tts, 'sys', SimpleNamespace(frozen=True))
    monkeypatch.setattr(local_tts, '_onnx_files_present', lambda: True)
    monkeypatch.setattr(local_tts, '_engine', None)
    assert not local_tts.available()
    with pytest.raises(local_tts.TtsUnavailable, match='separately installed'):
        await providers.synthesize('kokoro', None, {}, 'Hello')


@pytest.mark.asyncio
async def test_external_kokoro_uses_configured_server_and_returns_wav_without_local_engine(monkeypatch):
    seen = []
    wav = providers._wav_from_pcm(b'\x00\x00')
    def respond(request):
        seen.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'voices': ['af_nova', 'bf_emma']})
        assert json.loads(request.content) == {'model': 'kokoro', 'voice': 'bf_emma',
            'input': 'Hello', 'response_format': 'wav', 'speed': 1.2}
        return httpx.Response(200, content=wav)
    client = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(local_tts, 'sys', SimpleNamespace(frozen=True))
    assert [r['id'] for r in await providers.list_voices('kokoro', None, configured())] == ['af_nova', 'bf_emma']
    result = await providers.synthesize('kokoro', None, configured(), 'Hello', voice='bf_emma', speed=1.2)
    assert result.data == wav and result.mime_type == 'audio/wav'
    assert [r.url.path for r in seen] == ['/v1/audio/voices', '/v1/audio/speech']


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['file:///speech', 'http://user:secret@localhost/v1', 'https://localhost/v1?key=x'])
async def test_invalid_endpoint_is_rejected_before_local_or_network_work(url):
    with pytest.raises(providers.SpeechProviderError, match='API base URL'):
        await providers.synthesize('kokoro', None, configured(url), 'Hello')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(503, b'not ready'), (200, b'<html>not audio</html>')])
async def test_server_errors_do_not_fall_back_to_in_process_engine(monkeypatch, status, body):
    client = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, 'AsyncClient', lambda **kw: client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body)), **kw))
    async def forbidden(*args, **kwargs):
        raise AssertionError('unexpected local engine fallback')
    monkeypatch.setattr(local_tts, 'synthesize', forbidden)
    with pytest.raises(providers.SpeechProviderError):
        await providers.synthesize('kokoro', None, configured(), 'Hello')


@pytest.mark.asyncio
async def test_old_voice_lookup_cannot_publish_after_same_provider_endpoint_change():
    import ws_config
    import host_voice
    entered, release = asyncio.Event(), asyncio.Event()
    config = {'tts_provider': 'kokoro', **configured('http://server-a/v1')}
    async def voices():
        entered.set()
        await release.wait()
        return [{'id': 'old-server-voice'}]
    voice = SimpleNamespace(config=lambda: config, provider=lambda: 'kokoro',
        list_voices=voices, voice=lambda: 'af_nova', route=lambda: 'local')
    sent = []
    async def send(payload):
        sent.append(payload)
    server = SimpleNamespace(require_runtime=lambda: SimpleNamespace(voice=voice),
        hub=SimpleNamespace(broadcast=send))
    pending = asyncio.create_task(ws_config._send_tts_voices(server, broadcast=True))
    await entered.wait()
    old_key = host_voice.tts_config_key(config)
    config['tts']['kokoro']['base_url'] = 'http://server-b/v1'
    release.set()
    await pending
    assert sent == [] and host_voice.tts_config_key(config) != old_key
    await ws_config._send_tts_voices(server, broadcast=True)
    assert sent[0]['config_key'] == host_voice.tts_config_key(config)


def test_frozen_catalog_has_only_configurable_speech_routes(tmp_path, monkeypatch):
    from tests.test_provider_parity_hunt import _router
    router = _router(tmp_path)
    monkeypatch.setattr(providers, 'sys', SimpleNamespace(frozen=True))
    monkeypatch.setattr(local_tts, 'sys', SimpleNamespace(frozen=True))
    rows = providers.catalog(router, configured('  '), local_stt_available=False)
    tts = {row['id']: row for row in rows['tts_providers']}
    assert not {'piper', 'neutts', 'kittentts'} & tts.keys()
    assert not tts['kokoro']['available']
    assert {'openai', 'edge', 'kokoro'} <= tts.keys()
