import pytest

import llm_profiles
from desktop import vision


class FakeRouter:
    def __init__(self, route):
        self.mode = route
        self.cfg = {
            "local": {"mmproj": "mmproj.gguf"},
            # A contradictory legacy value must not override the main model.
            "vision": {"route": "local" if route == "cloud" else "cloud"},
        }
        self.engine_ready = True

    def cloud_route_ready(self, *, require_vision=False):
        return require_vision


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["local", "cloud"])
async def test_describe_forces_selected_vision_route(monkeypatch, route):
    seen = {}

    async def fake_complete(router, messages, **kwargs):
        seen.update(kwargs)
        return "screen summary"

    monkeypatch.setattr(llm_profiles, "complete", fake_complete)
    result = await vision.describe(FakeRouter(route), b"png")

    assert result == "screen summary"
    assert seen["route"] == route
    assert seen["image_b64"] == "cG5n"
    assert seen["profile"] == "vision"


@pytest.mark.asyncio
async def test_describe_falls_through_to_other_configured_vision_route(monkeypatch):
    router = FakeRouter("cloud")
    router.cloud_route_ready = lambda **_: False
    seen = {}

    async def fake_complete(*args, **kwargs):
        seen.update(kwargs)
        return "local fallback"

    monkeypatch.setattr(llm_profiles, "complete", fake_complete)
    assert await vision.describe(router, b"png") == "local fallback"
    assert seen["route"] == "local"
