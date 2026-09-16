from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from extensions.runtime_v2 import create_extension_v2_runtime
import ws_dispatch


def _skill_plugin(root: Path, version: str = "1.0.0") -> Path:
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: Demo skill\ndescription: A compact test skill.\n---\nDo the demo.",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "id": "com.example.demo-skill",
        "name": "Demo plugin",
        "description": "A manifest-backed plugin.",
        "version": version,
        "compatibility": {},
        "entrypoints": {},
        "contributes": {
            "skills": [{"id": "demo", "path": "skills/demo/SKILL.md"}],
        },
        "permissions": [],
    }
    (root / "variant1.plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )
    return root


def _runtime(tmp_path: Path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    runtime = create_extension_v2_runtime(
        str(tmp_path / "data"),
        plugin_sources=(str(plugins),),
        environment_builder=lambda *_args, **_kwargs: {"mode": "test"},
    )
    return runtime, plugins


def test_rescan_installs_plugins_and_projects_skills(tmp_path):
    runtime, plugins = _runtime(tmp_path)
    _skill_plugin(plugins / "demo")

    scan = runtime.rescan()

    assert scan == {
        "discovered": 1,
        "installed": 1,
        "updated": 0,
        "unchanged": 0,
        "errors": [],
    }
    rows = runtime.plugins()
    assert len(rows) == 1
    assert rows[0]["active"] is True
    assert rows[0]["contribution_kinds"] == ["skills"]
    assert runtime.skills.inspect("Demo skill")["instructions"] == "Do the demo."


def test_rescan_preserves_disabled_state_across_new_revision(tmp_path):
    runtime, plugins = _runtime(tmp_path)
    source = _skill_plugin(plugins / "demo")
    runtime.rescan()
    runtime.set_enabled("com.example.demo-skill", False)

    manifest_path = source / "variant1.plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    scan = runtime.rescan()

    assert scan["updated"] == 1
    assert runtime.plugins()[0]["active"] is False
    assert runtime.skills.list() == []


def test_rescan_reports_invalid_plugin_without_breaking_valid_rows(tmp_path):
    runtime, plugins = _runtime(tmp_path)
    _skill_plugin(plugins / "demo")
    broken = plugins / "broken"
    broken.mkdir()
    (broken / "variant1.plugin.json").write_text("{}", encoding="utf-8")

    scan = runtime.rescan()

    assert len(scan["errors"]) == 1
    rows = runtime.plugins()
    assert {row["status"] for row in rows} == {"ready", "error"}
    assert next(row for row in rows if row["status"] == "error")["name"] == "broken"


def test_legacy_skill_projection_is_removed_with_its_packages(tmp_path):
    runtime, plugins = _runtime(tmp_path)
    source = _skill_plugin(plugins / "demo")
    installed = runtime.packages.install(str(source))
    with sqlite3.connect(runtime.packages.path) as connection:
        connection.execute(
            "CREATE TABLE extension_skill_source("
            "skill_key TEXT PRIMARY KEY, package_id TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO extension_skill_source VALUES (?,?)",
            ("demo", installed["package_id"]),
        )
        connection.execute(
            "CREATE TABLE extension_skill_usage(skill_key TEXT PRIMARY KEY)"
        )

    retired = runtime.packages.retire_legacy_catalog()

    assert retired == ["com.example.demo-skill"]
    assert runtime.packages.list_packages() == []
    with sqlite3.connect(runtime.packages.path) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "extension_skill_source" not in tables
    assert "extension_skill_usage" not in tables


@pytest.mark.asyncio
async def test_plugin_settings_websocket_lists_rescans_and_toggles(tmp_path):
    runtime, plugins = _runtime(tmp_path)
    _skill_plugin(plugins / "demo")

    class Socket:
        def __init__(self):
            self.messages = []

        async def send_json(self, payload):
            self.messages.append(payload)

    socket = Socket()
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(extensions=runtime),
    )
    session = SimpleNamespace(viewed_session_id="chat-plugins", active=None)

    await ws_dispatch.HANDLERS["extension-v2:rescan"](
        host, socket, session,
        {"type": "extension-v2:rescan", "request_id": "scan-1"},
    )
    assert socket.messages[-1]["type"] == "extension-v2:accepted"
    assert socket.messages[-1]["result"]["plugins"][0]["active"] is True

    await ws_dispatch.HANDLERS["extension-v2:set-enabled"](
        host, socket, session,
        {
            "type": "extension-v2:set-enabled",
            "request_id": "toggle-1",
            "package_id": "com.example.demo-skill",
            "enabled": False,
        },
    )
    assert socket.messages[-1]["result"]["active"] is False

    await ws_dispatch.HANDLERS["extension-v2:list"](
        host, socket, session,
        {"type": "extension-v2:list", "request_id": "list-1"},
    )
    assert socket.messages[-1]["result"][0]["active"] is False
