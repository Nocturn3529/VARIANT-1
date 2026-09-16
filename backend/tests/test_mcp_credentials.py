"""MCP configuration belongs only to MCP v2's extension database."""

from __future__ import annotations

import json

import pytest

from extensions.mcp_v2 import child_environment, command_transport_spec
import tools


def test_stdio_environment_uses_one_v2_builder(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("AMBIENT_API_KEY", "hidden")
    environment = child_environment({"SERVER_TOKEN": "server-specific"})
    assert environment["PATH"] == "/usr/bin"
    assert "AMBIENT_API_KEY" not in environment
    assert environment["SERVER_TOKEN"] == "server-specific"
    assert command_transport_spec(
        'python -m demo.server', environment={"SERVER_TOKEN": "value"}
    ) == {
        "transport": "stdio",
        "command": ["python", "-m", "demo.server"],
        "env": {"SERVER_TOKEN": "value"},
    }


def test_wrongly_typed_tools_sections_are_normalized_on_load(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({
        "enabled": ["not", "a", "mapping"],
        "desktop": "wrong",
        "web_search": 7,
    }), encoding="utf-8")
    config = tools.ToolsConfig(str(path))
    assert "enabled" not in config.data
    assert config.data["desktop"] == {}
    assert config.web_search["provider"] == "variant1"


def test_failed_tools_persistence_rolls_back_live_setting(tmp_path, monkeypatch):
    path = tmp_path / "tools.json"
    config = tools.ToolsConfig(str(path))
    config.set_web_search_config({"provider": "variant1"})
    baseline = json.loads(json.dumps(config.data))

    monkeypatch.setattr(
        config, "_persist", lambda _value: (_ for _ in ()).throw(OSError("disk unavailable"))
    )
    with pytest.raises(OSError, match="disk unavailable"):
        config.set_web_search_config({"provider": "searxng"})
    assert config.data == baseline
