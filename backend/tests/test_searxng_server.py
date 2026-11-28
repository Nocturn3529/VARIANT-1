"""Unit tests for managed SearXNG sidecar (no live Docker required)."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from web_search import searxng as sx


def test_settings_yml_enables_json():
    body = sx.settings_yml_body("abc123secret")
    assert "formats:" in body
    assert "- json" in body
    assert "abc123secret" in body
    assert "use_default_settings: true" in body


def test_ensure_settings_dir_writes_once(tmp_path: Path):
    dest = sx.ensure_settings_dir(str(tmp_path), secret_key="fixedkey")
    path = Path(dest) / "settings.yml"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "fixedkey" in text
    # Second call must not overwrite.
    path.write_text("KEEP\n", encoding="utf-8")
    sx.ensure_settings_dir(str(tmp_path), secret_key="other")
    assert path.read_text(encoding="utf-8") == "KEEP\n"


def test_ensure_settings_dir_seeds_bundled(tmp_path: Path):
    bundled = tmp_path / "bundled.yml"
    bundled.write_text(
        "use_default_settings: true\nserver:\n  secret_key: \"variant1-dev-replace-on-first-run\"\n",
        encoding="utf-8",
    )
    dest = sx.ensure_settings_dir(
        str(tmp_path / "cfg"),
        bundled_settings=str(bundled),
        secret_key="rotated",
    )
    text = (Path(dest) / "settings.yml").read_text(encoding="utf-8")
    assert "rotated" in text
    assert "variant1-dev-replace-on-first-run" not in text


def test_parse_base_url_defaults():
    assert sx.parse_base_url("") == ("127.0.0.1", 8888)
    assert sx.parse_base_url("http://127.0.0.1:9999") == ("127.0.0.1", 9999)


def test_public_status_defaults():
    mgr = sx.SearxngServer({"autostart": False}, data_dir=".")
    st = mgr.public_status()
    assert st["autostart"] is False
    assert st["managed"] is True
    assert st["ready"] is False
    assert st["base_url"] == "http://127.0.0.1:8888"
    assert "docker_available" in st


class FakeRuntime:
    def __init__(self, kind, root):
        self.kind = kind
        self.path = str(root / (kind + (".exe" if os.name == "nt" else "")))
        self.rows = {}
        self.calls = []
        self.fail_stop = False

    async def run(self, args, *, runtime, timeout):
        assert runtime == self.path
        self.calls.append(list(args))
        command = args[0]
        if command == "container":
            key = args[-1]
            row = next((r for r in self.rows.values() if key in (r["Id"], r["Name"].lstrip("/"))), None)
            return (0, json.dumps([row]), "") if row else (1, "", "No such container")
        if command == "run":
            cid = "a" * 64
            labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
            self.rows[cid] = {"Id": cid, "Name": "/" + args[args.index("--name") + 1],
                              "Config": {"Labels": labels}, "State": {"Running": True}}
            Path(args[args.index("--cidfile") + 1]).write_text(cid, encoding="ascii")
            return 0, cid, ""
        assert command in {"stop", "start"} and args[1] in self.rows
        if command == "stop" and self.fail_stop:
            return 1, "", "fixture stop failure"
        self.rows[args[1]]["State"]["Running"] = command == "start"
        return 0, "", ""

    def use(self, manager, monkeypatch):
        monkeypatch.setattr(sx.shutil, "which", lambda name: self.path if name == self.kind else None)
        monkeypatch.setattr(manager, "_run_cli", self.run)
        monkeypatch.setattr(sx, "tcp_port_is_free", lambda *args: True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["docker", "podman"])
async def test_verified_local_manager_starts_and_stops_only_its_full_container_id(tmp_path, monkeypatch, kind):
    runtime = FakeRuntime(kind, tmp_path)
    cfg = {"autostart": True, "container_name": "custom-search"}
    profile = tmp_path / "profile"
    mgr = sx.SearxngServer(cfg, data_dir=str(profile))
    runtime.use(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_wait_healthy", AsyncMock(side_effect=[False, True]))
    await mgr.ensure_started_if_autostart()
    cid = (profile / "data" / "searxng" / (kind + ".cid")).read_text()
    assert mgr.public_status()["owned"] and mgr.ready
    # The original name is now occupied by an independent container.
    runtime.rows[cid]["Name"] = "/renamed-owner"
    foreign = "b" * 64
    runtime.rows[foreign] = {"Id": foreign, "Name": "/custom-search", "Config": {"Labels": {}},
                             "State": {"Running": True}}
    fresh = sx.SearxngServer(cfg, data_dir=str(profile))
    runtime.use(fresh, monkeypatch)
    await fresh.stop(force=True)
    assert [call for call in runtime.calls if call[0] == "stop"] == [["stop", cid]]
    assert runtime.rows[foreign]["State"]["Running"]
    runtime.rows[cid]["Name"] = "/custom-search"
    runtime.rows[foreign]["Name"] = "/independent"
    monkeypatch.setattr(fresh, "_wait_healthy", AsyncMock(side_effect=[False, True]))
    await fresh.start(force=True)
    assert ["start", cid] in runtime.calls
    runtime.fail_stop = True
    with pytest.raises(sx.SearxngError, match="still running"):
        await fresh.stop()
    assert fresh.public_status()["owned"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["attach", "external", "missing-proof", "foreign-profile", "wrong-id"])
async def test_probe_or_force_stop_cannot_claim_an_independent_server(tmp_path, monkeypatch, mode):
    cfg = {"managed": mode != "attach", "container_name": "search"}
    if mode == "external":
        cfg["base_url"] = "http://192.0.2.42:8888"
        cfg["host"] = "127.0.0.1"  # A saved local default does not authorize a stop for an external URL.
    mgr = sx.SearxngServer(cfg, data_dir=str(tmp_path))
    runtime = FakeRuntime("podman", tmp_path)
    runtime.use(mgr, monkeypatch)
    cid = "c" * 64
    runtime.rows[cid] = {"Id": cid, "Name": "/search", "State": {"Running": True},
        "Config": {"Labels": {"com.variant1.managed":"searxng",
                              "com.variant1.profile":"foreign" if mode == "foreign-profile" else mgr._profile_label}}}
    if mode != "missing-proof":
        mgr._cid_dir.mkdir(parents=True)
        (mgr._cid_dir / "podman.cid").write_text("d"*64 if mode == "wrong-id" else cid)
    monkeypatch.setattr(mgr, "_wait_healthy", AsyncMock(return_value=True))
    assert await mgr.probe()
    await mgr.start(force=True)
    if mode == "external":
        assert mgr.base_url == cfg["base_url"]
    assert not mgr.public_status()["owned"]
    await mgr.stop(force=True)
    assert all(call[0] == "container" for call in runtime.calls)
    assert runtime.rows[cid]["State"]["Running"]


@pytest.mark.parametrize("kind", ["docker", "podman"])
def test_packaged_entry_cleanup_verifies_profile_runtime_and_id(tmp_path, monkeypatch, kind):
    profile = tmp_path / "fixture-profile"
    config = profile / "config" / "tools.json"
    config.parent.mkdir(parents=True)
    monkeypatch.setenv("VARIANT1_TOOLS_CONFIG", str(config))
    cid = "e" * 64
    proof = profile / "data" / "searxng" / (kind + ".cid")
    proof.parent.mkdir(parents=True)
    proof.write_text(cid)
    mgr = sx.SearxngServer({}, data_dir=str(profile))
    fake = FakeRuntime(kind, tmp_path)
    fake.rows[cid] = {"Id":cid, "Name":"/not-the-default", "State":{"Running":True},
        "Config":{"Labels":{"com.variant1.managed":"searxng","com.variant1.profile":mgr._profile_label}}}
    monkeypatch.setattr(sx.shutil, "which", lambda name: fake.path if name == kind else None)
    async def run(self, args, **kwargs):
        return await fake.run(args, **kwargs)
    monkeypatch.setattr(sx.SearxngServer, "_run_cli", run)
    server_py = Path(sx.__file__).parents[1] / "server.py"
    monkeypatch.setattr(sys, "argv", [str(server_py), "--cleanup-searxng", str(profile)])
    for managed in (False, True):
        config.write_text(json.dumps({"web_search":{"searxng":{"managed":managed}}}))
        with pytest.raises(SystemExit) as result:
            runpy.run_path(str(server_py), run_name="__main__")
        assert result.value.code == 0
        assert fake.rows[cid]["State"]["Running"] is (not managed)
    assert [call for call in fake.calls if call[0] == "stop"] == [["stop", cid]]
    assert sx.cleanup_main(["relative"]) == 2


@pytest.mark.asyncio
async def test_start_requires_managed_autostart_or_manual_start(tmp_path, monkeypatch):
    mgr = sx.SearxngServer({}, data_dir=str(tmp_path))
    monkeypatch.setattr(mgr, "_wait_healthy", AsyncMock(return_value=False))
    monkeypatch.setattr(sx.shutil, "which", lambda _: None)
    with pytest.raises(sx.SearxngError, match="autostart disabled"):
        await mgr.start()
    with pytest.raises(sx.SearxngError, match="Docker/Podman not found"):
        await mgr.start(force=True)


def test_web_search_public_status_includes_runtime():
    from web_search import providers as wsp

    st = wsp.public_status(
        {"provider": "searxng", "searxng": {"base_url": "http://127.0.0.1:8888", "autostart": True}},
        runtime={"ready": True, "docker_available": True, "error": ""},
    )
    assert st["provider"] == "searxng"
    assert st["searxng"]["ready"] is True
    assert st["searxng"]["autostart"] is True
    assert st["searxng"]["docker_available"] is True


def test_tools_config_searxng_defaults(tmp_path: Path):
    import tools

    path = tmp_path / "tools.json"
    path.write_text("{}", encoding="utf-8")
    cfg = tools.ToolsConfig(str(path))
    ws = cfg.web_search
    assert ws["provider"] == "variant1"
    # SearXNG is optional; autostart off so Docker is not required by default.
    assert ws["searxng"]["autostart"] is False
    assert ws["searxng"]["managed"] is True
    assert ws["searxng"]["container_name"] == "variant1-searxng"


def test_set_web_search_autostart(tmp_path: Path):
    import tools

    path = tmp_path / "tools.json"
    path.write_text("{}", encoding="utf-8")
    cfg = tools.ToolsConfig(str(path))
    cfg.set_web_search_config({"searxng": {"autostart": False}})
    assert cfg.web_search["searxng"]["autostart"] is False
    cfg.set_web_search_config({"searxng": {"autostart": "true"}})
    assert cfg.web_search["searxng"]["autostart"] is True
