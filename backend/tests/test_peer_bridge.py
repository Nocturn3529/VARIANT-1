"""Portable MCP discovery, identity, reconnect and durable messaging boundaries."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import psutil
import pytest
from fastapi import FastAPI
import uvicorn

from peers.bridge_api import publish_bridge, register_bridge_routes
from peers.bridge_contract import call_peer_tool
from peers.bridge_discovery import BridgeUnavailable, PublishedBridge, profile_identity, resolve_bridge
from peers.bridge_worker import BridgeClient
from peers.grok_install import install, mcp_definition
from peers.grok_install import GrokPeerError, installed
from tests.test_peers import _stack, _settle_service


def test_discovery_fences_ambiguous_profile_and_replaces_dead_instance(tmp_path):
    directory = tmp_path / "registry"
    first = PublishedBridge(data_dir=tmp_path / "profile", url="http://127.0.0.1:5000", token="first", directory=directory)
    assert resolve_bridge(first.profile_id, directory)["token"] == "first"
    second = PublishedBridge(data_dir=tmp_path / "profile", url="http://127.0.0.1:5001", token="second", directory=directory)
    with pytest.raises(BridgeUnavailable, match="More than one"):
        resolve_bridge(first.profile_id, directory)
    first.close()
    assert resolve_bridge(second.profile_id, directory)["token"] == "second"
    second.close()
    with pytest.raises(BridgeUnavailable, match="offline"):
        resolve_bridge(second.profile_id, directory)


def test_mcp_tools_remain_available_without_backend_or_launcher_environment(tmp_path):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "peers_connection", "arguments": {}}},
    ]
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "server.py"), "--peer-bridge",
        "--harness", "grok", "--profile-id", profile_identity(tmp_path), "--discovery-dir", str(tmp_path / "registry")],
        input="".join(json.dumps(row) + "\n" for row in messages).encode(), capture_output=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr.decode()
    replies = {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}
    assert {row["name"] for row in replies[2]["result"]["tools"]} >= {"peers_send", "peers_reply", "peers_inbox"}
    error = json.loads(replies[3]["result"]["content"][0]["text"])
    assert error["code"] == "service_offline" and error["commit_state"] == "not_committed"
    assert not (tmp_path / "config").exists()  # No AppHost bootstrap in the MCP worker.


def test_client_lost_send_ack_is_not_automatically_replayed(tmp_path, monkeypatch):
    descriptor = {"instance_id": "host-one"}
    monkeypatch.setattr("peers.bridge_worker.resolve_bridge", lambda *_: descriptor)
    client = BridgeClient(profile_id="a" * 32, harness="grok", identity={"native_session_id": "existing"})
    calls = []
    def post(_descriptor, payload, timeout=15):
        calls.append(payload)
        if payload["operation"] == "register":
            return {"ok": True, "result": {"epoch": 3}}
        raise TimeoutError("response lost after commit")
    monkeypatch.setattr(client, "_post", post)
    response = client.call("peers_send", {"peer_id": "chat:target", "text": "once", "request_id": "stable"})
    assert response["isError"]
    assert json.loads(response["content"][0]["text"])["commit_state"] == "unknown"
    assert [row["operation"] for row in calls] == ["register", "call"]
    assert calls[-1]["epoch"] == 3 and calls[-1]["arguments"]["request_id"] == "stable"


@pytest.mark.asyncio
async def test_bridge_registration_resume_keeps_peer_and_fences_stale_calls(tmp_path, monkeypatch):
    peers, runtimes, sessions, _chat, first, second = _stack(tmp_path)
    runtime = SimpleNamespace(peers=peers, sessions=sessions)
    grok = SimpleNamespace(note_connection=Mock(), connection_status=lambda row: row)
    host = SimpleNamespace(data_dir=str(tmp_path), require_runtime=lambda: runtime, grok_peer_integration=grok)
    publication = publish_bridge(host, "http://127.0.0.1:5555", tmp_path / "registry")
    monkeypatch.setattr("peers.bridge_api._identity", lambda payload: {"cwd": payload.get("cwd", ""), "delivery_mode": "inbox", "leader_socket": ""})
    app = FastAPI()
    register_bridge_routes(app, host)
    identity = {"operation": "register", "connection_id": "mcp-one", "epoch": 1, "harness": "grok", "native_session_id": "original",
        "process_id": str(os.getpid()), "process_started_at": psutil.Process().create_time(),
        "runtime_id": "grok-real-actor", "runtime_pid": os.getpid(), "runtime_started_at": psutil.Process().create_time(), "cwd": "A"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://localhost",
            headers={"Authorization": "Bearer " + host.peer_bridge_token}) as client:
            registered = (await client.post("/peers/bridge", json=identity)).json()
            assert registered["ok"], registered
            connection = registered["result"]
            message = {"operation": "call", "connection_id": "mcp-one", "epoch": connection["epoch"],
                "name": "peers_send", "arguments": {"peer_id": "chat:" + first, "text": "hello", "request_id": "once"}}
            sent = (await client.post("/peers/bridge", json=message)).json()
            assert sent["ok"] and sent["result"]["origin"]["kind"] == "agent"
            await client.post("/peers/bridge", json={"operation": "disconnect", "connection_id": "mcp-one", "epoch": connection["epoch"]})
            ended = (await client.post("/peers/bridge", json={**identity, "cwd": "B"})).json()
            assert ended["error"]["code"] == "peer_connection_epoch_expired"
            resumed = (await client.post("/peers/bridge", json={**identity, "epoch": ended["error"]["next_epoch"], "cwd": "B"})).json()["result"]
            assert resumed["peer_id"] == "grok:original" and resumed["epoch"] > connection["epoch"]
            stale = (await client.post("/peers/bridge", json=message)).json()
            assert not stale["ok"] and stale["error"]["commit_state"] == "not_committed"
            again = (await client.post("/peers/bridge", json={**message, "epoch": resumed["epoch"]})).json()
            assert again["result"]["message_id"] == sent["result"]["message_id"]
            assert len(peers.inbox("chat:" + first)["messages"]) == 1
            invalid = (await client.post("/peers/bridge", json={**message, "epoch": resumed["epoch"],
                "arguments": {"unexpected": True}})).json()
            assert invalid["error"]["commit_state"] == "not_committed"
            await client.post("/peers/bridge", json={**identity, "connection_id": "mcp-other", "runtime_id": "another-actor"})
            conflict = (await client.post("/peers/bridge", json={**message, "epoch": resumed["epoch"]})).json()
            assert conflict["error"]["code"] == "peer_connection_conflicted" and conflict["error"]["commit_state"] == "not_committed"
            read = (await client.post("/peers/bridge", json={**message, "epoch": resumed["epoch"], "name": "peers_inbox", "arguments": {}})).json()
            assert read["ok"]
    finally:
        publication.close()
        await _settle_service(peers, runtimes)


@pytest.mark.asyncio
async def test_tool_inbox_preserves_agent_origin_not_human_prompt(tmp_path):
    peers, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    try:
        row = await peers.send("chat:" + first, "chat:" + second, "Please review the parser", request_id="message")
        result = await call_peer_tool(peers, "chat:" + second, "peers_inbox", {}, connection={})
        incoming = result["messages"][0]
        assert incoming["origin"] == {"kind": "agent", "peer_id": "chat:" + first,
            "message_id": row["message_id"], "authority": "peer"}
        assert incoming["sender"]["display_name"] == "First"
        assert incoming["message_kind"] == "request"
        assert incoming["content"] == "Please review the parser"
        with pytest.raises(ValueError, match="Unexpected"):
            await call_peer_tool(peers, "chat:" + second, "peers_send", {"sender_peer_id": "forged"}, connection={})
    finally:
        await _settle_service(peers, runtimes)


@pytest.mark.asyncio
async def test_retired_terminal_binding_does_not_claim_to_be_online(tmp_path):
    peers, runtimes, _sessions, _chat, _first, _second = _stack(tmp_path)
    try:
        peers.register_external("grok:legacy", "Old Grok", "grok-acp-mcp", "legacy", "old-terminal", "123", 1, 1,
            {"live_ingress": True})
        row = peers.get_peer("grok:legacy")
        assert row["status"] == "disconnected" and not row["capabilities"]["live_ingress"]
    finally:
        await _settle_service(peers, runtimes)


@pytest.mark.asyncio
async def test_install_refreshes_owned_stdio_config_without_launch_environment(tmp_path, monkeypatch):
    grok_home = tmp_path / "grok"
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    source = grok_home / "plugins" / "variant1-peers"
    target = grok_home / "installed-plugins" / "entry"
    target.mkdir(parents=True)
    (target / ".variant1-managed").write_text("variant1-grok-peer-adapter-v1")
    (target / "unrelated.txt").write_text("keep")
    async def command(argv, **kwargs):
        assert "install" not in argv
        result = [{"name": "variant1-peers", "source": str(source), "path": str(target)}]
        return SimpleNamespace(stdout=json.dumps(result).encode(), stderr=b"", timed_out=False, cancelled=False,
            truncated=False, process=SimpleNamespace(exit_code=0))
    host = SimpleNamespace(data_dir=str(tmp_path / "profile"))
    await install(host, SimpleNamespace(run_bounded_process=AsyncMock(side_effect=command)), "owner", str(tmp_path))
    config = json.loads((target / ".mcp.json").read_text())
    assert config["mcpServers"]["variant1-peers"] == mcp_definition(host)
    assert "--profile-id" in config["mcpServers"]["variant1-peers"]["args"]
    assert "url" not in config["mcpServers"]["variant1-peers"] and "env" not in config["mcpServers"]["variant1-peers"]
    assert (target / "unrelated.txt").read_text() == "keep"
    assert installed(host)
    before = (target / ".mcp.json").read_bytes()
    other = SimpleNamespace(data_dir=str(tmp_path / "another-profile"))
    with pytest.raises(GrokPeerError) as refused:
        await install(other, SimpleNamespace(run_bounded_process=AsyncMock(side_effect=command)), "owner", str(tmp_path))
    assert refused.value.code == "grok_adapter_profile_conflict"
    assert (target / ".mcp.json").read_bytes() == before and installed(host)
    await install(other, SimpleNamespace(run_bounded_process=AsyncMock(side_effect=command)), "owner", str(tmp_path),
        replace_profile_id=profile_identity(host.data_dir))
    assert installed(other) and not installed(host)


def test_installer_lock_excludes_another_process(tmp_path):
    from peers import install_lock
    path = tmp_path / "install.lock"
    lock = install_lock.acquire(path)
    try:
        code = "from peers.install_lock import acquire; acquire(" + repr(str(path)) + ", timeout=.1)"
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode != 0 and b"Another Grok adapter setup" in result.stderr
    finally:
        install_lock.release(lock)
    install_lock.release(install_lock.acquire(path, timeout=.1))


@pytest.mark.asyncio
async def test_cancelled_setup_releases_lock_acquired_during_cancellation(tmp_path, monkeypatch):
    started = threading.Event()
    token = object()
    def acquire(_path, _timeout, cancelled):
        started.set()
        assert cancelled.wait(2)
        return token
    release = Mock()
    monkeypatch.setattr("peers.grok_install.install_lock.acquire", acquire)
    monkeypatch.setattr("peers.grok_install.install_lock.release", release)
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok"))
    task = asyncio.create_task(install(SimpleNamespace(data_dir=str(tmp_path)), object(), "owner", str(tmp_path)))
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(.01)
    assert started.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.assert_called_once_with(token)
    assert not (tmp_path / "grok/plugins/variant1-peers/plugin.json").exists()


@pytest.mark.asyncio
async def test_live_client_reconnects_after_backend_port_and_token_change(tmp_path, monkeypatch):
    peers, runtimes, sessions, _chat, first, _second = _stack(tmp_path)
    host = SimpleNamespace(data_dir=str(tmp_path), require_runtime=lambda: SimpleNamespace(peers=peers, sessions=sessions),
        grok_peer_integration=SimpleNamespace(note_connection=Mock(), connection_status=lambda row: row))
    monkeypatch.setattr("peers.bridge_api._identity", lambda _: {"cwd": str(tmp_path), "delivery_mode": "inbox", "leader_socket": ""})
    app = FastAPI(); register_bridge_routes(app, host)
    identity = {"harness": "grok", "native_session_id": "persistent", "process_id": str(os.getpid()),
        "process_started_at": psutil.Process().create_time(), "runtime_id": "runtime", "runtime_pid": os.getpid(),
        "runtime_started_at": psutil.Process().create_time(), "cwd": str(tmp_path)}
    client = BridgeClient(profile_id=profile_identity(tmp_path), harness="grok", directory=tmp_path / "registry", identity=identity)
    running = []
    async def start():
        listener = socket.socket(); listener.bind(("127.0.0.1", 0))
        publication = publish_bridge(host, f"http://127.0.0.1:{listener.getsockname()[1]}", tmp_path / "registry")
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        running.append((server, task, listener, publication))
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        return running[-1]
    async def stop(row):
        server, task, listener, publication = row
        publication.close(); server.should_exit = True
        await task; listener.close(); running.remove(row)
    try:
        first_server = await start()
        arguments = {"peer_id": "chat:" + first, "text": "once", "request_id": "across-restart"}
        sent = await asyncio.to_thread(client.call, "peers_send", arguments)
        assert not sent["isError"], sent
        first_epoch = client.connection_epoch
        await stop(first_server)
        offline = await asyncio.to_thread(client.call, "peers_send", {**arguments, "request_id": "offline"})
        assert json.loads(offline["content"][0]["text"])["commit_state"] == "not_committed"
        await start()
        repeated = await asyncio.to_thread(client.call, "peers_send", arguments)
        assert not repeated["isError"], repeated
        assert client.connection_epoch > first_epoch
        before = json.loads(sent["content"][0]["text"])
        after = json.loads(repeated["content"][0]["text"])
        assert before["message_id"] == after["message_id"] and len(peers.inbox("chat:" + first)["messages"]) == 1
    finally:
        await asyncio.to_thread(client.close)
        for row in list(running):
            await stop(row)
        await _settle_service(peers, runtimes)
