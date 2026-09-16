from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from desktop import vision
from model_runtime.image_fallback import (
    append_visual_description,
    describe_for_text_retry,
    looks_like_image_rejection,
)


def _image() -> dict:
    return {
        "data_b64": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii"),
        "media_type": "image/png",
        "origin": "tool_result",
        "tool_call_id": "call_1",
        "tool_call_ids": ["call_1"],
    }


def test_image_rejection_classifier_is_specific_to_multimodal_failures():
    assert looks_like_image_rejection(
        RuntimeError("No endpoints found that support image input")
    )
    assert looks_like_image_rejection(
        RuntimeError("unsupported input_image content part")
    )
    assert not looks_like_image_rejection(RuntimeError("HTTP 429 rate limited"))


@pytest.mark.asyncio
async def test_text_retry_uses_another_vision_route_and_caches_description(
    monkeypatch,
):
    calls = []
    image = _image()
    router = SimpleNamespace()

    monkeypatch.setattr(
        vision,
        "available_vision_routes",
        lambda _router, exclude=(): ("local",),
    )

    async def fake_describe(_router, raw, *, prompt, route):
        calls.append((raw, prompt, route))
        return "A consent dialog covers the playback controls."

    monkeypatch.setattr(vision, "describe", fake_describe)

    first = await describe_for_text_retry(
        router,
        [image],
        rejected_route="cloud",
    )
    second = await describe_for_text_retry(
        router,
        [image],
        rejected_route="cloud",
    )

    assert first == second
    assert "consent dialog" in first
    assert len(calls) == 1
    assert calls[0][2] == "local"


def test_visual_description_is_request_local_user_observation():
    source = [{"role": "user", "content": "Continue"}]
    projected = append_visual_description(source, "[Visual observation]\nDialog open")

    assert source == [{"role": "user", "content": "Continue"}]
    assert projected[-1] == {
        "role": "user",
        "content": "[Visual observation]\nDialog open",
    }
