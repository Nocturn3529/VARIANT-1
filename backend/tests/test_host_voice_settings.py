from __future__ import annotations

from types import SimpleNamespace

import host_voice
import pytest
from copy import deepcopy


def test_empty_voice_resets_provider_to_its_default():
    saves = []
    router = SimpleNamespace(cfg={
        "voice": {
            "tts_provider": "openai",
            "tts": {"openai": {"voice": "alloy", "voice_id": "legacy"}},
        },
    })

    host_voice.set_tts(router, "voice", "", save=lambda: saves.append(True))

    assert router.cfg["voice"]["tts"]["openai"] == {}
    assert saves == [True]


@pytest.mark.parametrize('failure', ['false', 'raise'])
def test_failed_speech_save_restores_only_previous_voice_setting(failure):
    router = SimpleNamespace(cfg={'voice': {'tts_provider': 'kokoro', 'auto_tts': True}})
    previous = deepcopy(host_voice.voice_cfg(router))
    def save():
        router.cfg['unrelated'] = 'preserved'
        if failure == 'raise':
            raise OSError('disk unavailable')
        return False
    with pytest.raises(OSError):
        host_voice.set_tts(router, 'tts_options', {'provider': 'kokoro',
            'fields': {'base_url': 'http://127.0.0.1:8880/v1'}}, save=save)
    assert router.cfg['voice'] == previous
    assert router.cfg['unrelated'] == 'preserved'


def test_kokoro_url_is_validated_before_save_and_can_be_cleared():
    from speech.providers import SpeechProviderError
    router = SimpleNamespace(cfg={})
    assert host_voice.voice_cfg(router)['auto_tts'] is False
    saved = []
    with pytest.raises(SpeechProviderError):
        host_voice.set_tts(router, 'tts_options', {'provider': 'kokoro',
            'fields': {'base_url': 'file:///speech'}}, save=lambda: saved.append(True))
    assert not saved
    host_voice.set_tts(router, 'tts_options', {'provider': 'kokoro',
        'fields': {'base_url': ' http://127.0.0.1:8880/v1/ '}}, save=lambda: saved.append(True))
    assert router.cfg['voice']['tts']['kokoro']['base_url'] == 'http://127.0.0.1:8880/v1'
    host_voice.set_tts(router, 'tts_options', {'provider': 'kokoro',
        'fields': {'base_url': ' '}}, save=lambda: saved.append(True))
    assert router.cfg['voice']['tts']['kokoro']['base_url'] == ''
