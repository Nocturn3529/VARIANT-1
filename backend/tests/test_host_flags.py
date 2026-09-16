"""Unit tests for host_flags pure helpers (no server import)."""

from __future__ import annotations

import host_flags


class _Router:
    def __init__(self, mode="local", cfg=None):
        self.mode = mode
        self.cfg = cfg if cfg is not None else {}
        self.saved = 0

    def save_config(self):
        self.saved += 1


def test_default_agent_mode_is_the_only_interactive_mode():
    assert host_flags.agent_mode({}) == "default"


def test_vision_cfg_follows_router_mode():
    r = _Router(mode="cloud", cfg={"vision": {}, "local": {}})
    snap = host_flags.vision_cfg_from_router(r)
    assert snap["route"] == "cloud"
    assert snap["local_route"] == "single"
    assert snap["cloud_route"] == "single"
    assert "privacy_mode" not in snap
