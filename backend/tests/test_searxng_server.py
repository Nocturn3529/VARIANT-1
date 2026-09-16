"""Unit tests for managed SearXNG sidecar (no live Docker required)."""

from __future__ import annotations

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


@pytest.mark.asyncio
async def test_start_reuses_already_healthy(tmp_path: Path):
    mgr = sx.SearxngServer({"autostart": True}, data_dir=str(tmp_path))
    with patch.object(mgr, "_wait_healthy", new=AsyncMock(return_value=True)):
        await mgr.start(force=True)
    assert mgr.ready is True
    assert mgr.last_error == ""


@pytest.mark.asyncio
async def test_healthy_named_managed_container_is_adopted_as_owned(tmp_path: Path):
    mgr = sx.SearxngServer({"autostart": True, "managed": True}, data_dir=str(tmp_path))
    with patch.object(mgr, "_wait_healthy", new=AsyncMock(return_value=True)):
        with patch.object(mgr, "docker_available", return_value=True):
            with patch.object(mgr, "_container_state", new=AsyncMock(return_value="running")):
                await mgr.start(force=False)
    assert mgr.ready is True
    assert mgr._container_running is True
    assert mgr._owned is True


@pytest.mark.asyncio
async def test_start_respects_autostart_false_without_force(tmp_path: Path):
    mgr = sx.SearxngServer({"autostart": False, "managed": True}, data_dir=str(tmp_path))
    with patch.object(mgr, "_wait_healthy", new=AsyncMock(return_value=False)):
        with pytest.raises(sx.SearxngError, match="autostart disabled"):
            await mgr.start(force=False)
    assert mgr.ready is False


@pytest.mark.asyncio
async def test_start_force_without_docker_errors(tmp_path: Path):
    mgr = sx.SearxngServer({"autostart": False, "managed": True}, data_dir=str(tmp_path))
    with patch.object(mgr, "_wait_healthy", new=AsyncMock(return_value=False)):
        with patch.object(mgr, "docker_available", return_value=False):
            with pytest.raises(sx.SearxngError, match="Docker"):
                await mgr.start(force=True)


@pytest.mark.asyncio
async def test_stop_skips_when_not_owned(tmp_path: Path):
    mgr = sx.SearxngServer({}, data_dir=str(tmp_path))
    mgr.ready = True
    mgr._owned = False
    with patch.object(mgr, "_run_cli", new=AsyncMock()) as cli:
        await mgr.stop(force=False)
    cli.assert_not_called()
    assert mgr.ready is False


@pytest.mark.asyncio
async def test_stop_force_stops_container(tmp_path: Path):
    mgr = sx.SearxngServer({}, data_dir=str(tmp_path))
    mgr.ready = True
    mgr._owned = False
    with patch.object(mgr, "docker_available", return_value=True):
        with patch.object(mgr, "_container_state", new=AsyncMock(return_value="running")):
            with patch.object(mgr, "_run_cli", new=AsyncMock(return_value=(0, "", ""))) as cli:
                await mgr.stop(force=True)
    assert cli.await_count >= 1
    args = cli.await_args_list[0].args[0]
    assert args[0] == "stop"
    assert mgr.ready is False


@pytest.mark.asyncio
async def test_stop_failure_keeps_running_container_owned_for_retry(tmp_path: Path):
    mgr = sx.SearxngServer({}, data_dir=str(tmp_path))
    mgr._owned = True
    states = AsyncMock(side_effect=["running", "running"])
    with patch.object(mgr, "docker_available", return_value=True):
        with patch.object(mgr, "_container_state", new=states):
            with patch.object(
                mgr, "_run_cli", new=AsyncMock(side_effect=sx.SearxngError("timeout"))
            ):
                await mgr.stop(force=False)
    assert mgr._owned is True
    assert mgr._container_running is True


@pytest.mark.asyncio
async def test_reconfigure_stops_owned_container_before_address_change(tmp_path: Path):
    mgr = sx.SearxngServer(
        {"base_url": "http://127.0.0.1:8888"}, data_dir=str(tmp_path)
    )
    mgr.ready = True
    mgr._owned = True
    mgr._container_running = True

    async def stopped(*, force=False):
        assert force is True
        mgr._owned = False
        mgr._container_running = False

    with patch.object(mgr, "stop", side_effect=stopped) as stop:
        await mgr.reconfigure({"base_url": "http://127.0.0.1:9999"})
    stop.assert_awaited_once()
    assert mgr.base_url == "http://127.0.0.1:9999"


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
