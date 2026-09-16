from __future__ import annotations

from types import SimpleNamespace

import host_voice


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
