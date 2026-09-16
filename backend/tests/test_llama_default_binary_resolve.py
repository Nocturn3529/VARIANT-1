"""Default llm_config binary pin must resolve per OS."""
from __future__ import annotations

import json
from pathlib import Path

from model_runtime.llama_server import resolve_llama_binary_relpath, LlamaServer


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CFG = ROOT / "config" / "llm_config.default.json"


def test_default_config_local_binary_is_platform_neutral():
    cfg = json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))
    assert cfg["local"]["binary"] == "bin/llama-server"


def test_resolve_maps_exe_and_bare_pins(monkeypatch):
    monkeypatch.setattr("model_runtime.llama_server.sys.platform", "linux")
    assert resolve_llama_binary_relpath("bin/llama-server.exe") == "bin/llama-server"
    assert resolve_llama_binary_relpath("bin/llama-server") == "bin/llama-server"
    assert resolve_llama_binary_relpath("") == "bin/llama-server"
    assert resolve_llama_binary_relpath(None) == "bin/llama-server"

    monkeypatch.setattr("model_runtime.llama_server.sys.platform", "win32")
    assert resolve_llama_binary_relpath("bin/llama-server") == "bin/llama-server.exe"
    assert resolve_llama_binary_relpath("bin/llama-server.exe") == "bin/llama-server.exe"


def test_resolve_preserves_custom_relative_and_absolute(monkeypatch, tmp_path):
    monkeypatch.setattr("model_runtime.llama_server.sys.platform", "linux")
    assert resolve_llama_binary_relpath("bin/custom-llama") == "bin/custom-llama"
    abs_path = str(tmp_path / "llama-server.exe")
    assert resolve_llama_binary_relpath(abs_path) == abs_path


def test_llama_server_uses_default_config_pin(monkeypatch):
    monkeypatch.setattr("model_runtime.llama_server.sys.platform", "linux")
    cfg = json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))
    server = LlamaServer(cfg["local"], app_root=str(ROOT))
    assert server.binary.endswith("llama-server")
    assert not server.binary.lower().endswith(".exe")
