"""Native Grok session control remains independent of its presentation terminal."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import time

import pytest

from peers.grok import GrokIntegration, GrokPeerError
from peers import PeerRepository


def make_runtime(tmp_path):
    rows = []
    peers = SimpleNamespace(list_connections=lambda **kwargs: [row for row in rows if not kwargs.get("peer_id") or row["peer_id"] == kwargs["peer_id"]],
        get_connection=lambda identity: next(row for row in rows if row["connection_id"] == identity), register_external_hook=Mock(),
        repository=PeerRepository(str(tmp_path / "peers.sqlite3")))
    execution = SimpleNamespace(stop_process=AsyncMock(), close_terminal=AsyncMock())
    host = SimpleNamespace(data_dir=str(tmp_path), app_root=str(tmp_path), hub=SimpleNamespace(broadcast=AsyncMock()),
        require_runtime=lambda: SimpleNamespace(peers=peers, execution=execution))
    integration = GrokIntegration(host)
    return integration, rows, execution


def connection(status="active"):
    return {"peer_id": "grok:old-session", "native_session_id": "old-session", "harness": "grok", "connection_id": "c1",
        "runtime_id": "actor", "runtime_pid": 10, "runtime_started_at": 20, "epoch": 2,
        "status": status, "delivery_owner": status == "active", "metadata": {"cwd": "C:/saved", "delivery_mode": "inbox", "leader_socket": ""}}


@pytest.mark.asyncio
async def test_global_status_uses_viewer_and_connection_without_terminal(tmp_path):
    integration, rows, _execution = make_runtime(tmp_path)
    integration._start = Mock()
    rows.append(connection())
    status = integration.status("another-chat")
    binding = status["items"][0]
    assert binding["viewer_chat_id"] == "another-chat" and binding["owner_chat_id"] == ""
    assert binding["terminal_id"] == "" and binding["status"] == "connected"
    assert binding["delivery_mode"] == "inbox" and not binding["capabilities"]["live_ingress"]
    rows[0]["status"] = "conflicted"
    assert integration.status("another-chat")["items"][0]["status"] == "conflicted"
    integration.viewers.add("first-chat")
    integration._publish()
    await asyncio.sleep(0)
    assert {call.args[0]["chat_id"] for call in integration.host.hub.broadcast.await_args_list} == {"first-chat", "another-chat"}
    await integration.shutdown()


@pytest.mark.asyncio
async def test_ended_residency_never_loads_another_or_recreates_session(tmp_path, monkeypatch):
    integration, _rows, _execution = make_runtime(tmp_path)
    monkeypatch.setattr("peers.grok_runtime.process_matches", lambda *_: True)
    client = SimpleNamespace(request=AsyncMock(return_value={"result": {"sessions": [
        {"sessionId": "old-session", "resident": False, "activity": "dormant"},
        {"sessionId": "new-session", "resident": True, "activity": "idle"}]}}))
    controller = {"client": client, "runtime_pid": 1, "runtime_started_at": 1}
    assert not await integration._resident(controller, "old-session")
    client.request.assert_awaited_once_with("_x.ai/sessions/list", {})
    await integration.shutdown()


@pytest.mark.asyncio
async def test_activity_ignores_stale_turn_completion_and_replayed_updates(tmp_path):
    integration, _rows, _execution = make_runtime(tmp_path)
    integration._wake = Mock()
    integration._event({"method": "_x.ai/queue/changed", "params": {"sessionId": "s", "runningPromptId": "new"}})
    for prompt, replay in (("old", False), ("new", True)):
        integration._event({"method": "_x.ai/session_notification", "params": {"sessionId": "s",
            "_meta": {"isReplay": replay}, "update": {"sessionUpdate": "turn_completed", "prompt_id": prompt}}})
        assert integration.activity["grok:s"]["state"] == "working"
    integration._event({"method": "_x.ai/session_notification", "params": {"sessionId": "s",
        "update": {"sessionUpdate": "turn_completed", "prompt_id": "new"}}})
    assert integration.activity["grok:s"]["state"] == "idle"
    integration._wake.assert_called_once()
    await integration.shutdown()


@pytest.mark.asyncio
async def test_permission_is_exact_and_visible_across_viewers(tmp_path):
    integration, rows, _execution = make_runtime(tmp_path)
    rows.append(connection())
    integration.viewers.update(["viewer-a", "viewer-b"])
    task = asyncio.create_task(integration._permission({"method": "session/request_permission",
        "params": {"sessionId": "old-session", "options": [{"optionId": "once", "name": "Allow once"}]}}))
    await asyncio.sleep(0)
    key = next(iter(integration.permissions))
    with pytest.raises(GrokPeerError):
        integration.answer_permission("viewer-a", "session_other", key, "once")
    with pytest.raises(GrokPeerError):
        integration.answer_permission("viewer-a", "session_old-session", key, "invalid")
    result = integration.answer_permission("viewer-b", "session_old-session", key, "once")
    assert result["viewer_chat_id"] == "viewer-b"
    assert integration.answer_permission("viewer-b", "session_old-session", key, "once") == result
    assert await task == {"outcome": {"outcome": "selected", "optionId": "once"}}
    await integration.shutdown()


@pytest.mark.asyncio
async def test_saved_session_uses_its_native_directory_not_viewer_default(tmp_path):
    integration, _rows, _execution = make_runtime(tmp_path)
    saved = tmp_path / "saved-project"
    saved.mkdir()
    integration.saved_sessions["old-session"] = {"sessionId": "old-session", "cwd": str(saved)}
    integration.sessions = AsyncMock()
    assert await integration._saved_cwd("old-session", "viewer") == str(saved)
    integration.sessions.assert_not_awaited()
    await integration.shutdown()


@pytest.mark.asyncio
async def test_external_controller_is_released_after_last_lease_ends(tmp_path, monkeypatch):
    integration, _rows, execution = make_runtime(tmp_path)
    monkeypatch.setattr("peers.grok_runtime.process_matches", lambda *_: True)
    client = SimpleNamespace(close=AsyncMock())
    integration.controllers["actor"] = {"client": client, "process_id": "only-owned-proxy", "runtime_pid": 11,
        "runtime_started_at": 20, "orphaned_at": time.monotonic() - 6}
    task = asyncio.create_task(integration._monitor())
    for _ in range(100):
        if client.close.await_count:
            break
        await asyncio.sleep(.01)
    task.cancel(); await asyncio.gather(task, return_exceptions=True)
    client.close.assert_awaited_once()
    execution.stop_process.assert_awaited_once_with("only-owned-proxy", force=True)
    execution.close_terminal.assert_not_awaited()
    await integration.shutdown()
