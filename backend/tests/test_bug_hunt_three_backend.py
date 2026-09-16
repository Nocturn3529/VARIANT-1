"""Behavior regressions for confirmed backend findings in Grok hunt three."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_managed_search_creation_publishes_loopback_and_defaults_manual(tmp_path, monkeypatch):
    from web_search import searxng as sx
    manager = sx.SearxngServer({}, data_dir=str(tmp_path))
    assert manager.public_status()["autostart"] is False
    monkeypatch.setattr(manager, "_wait_healthy", AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(manager, "docker_available", lambda: True)
    monkeypatch.setattr(manager, "_container_state", AsyncMock(return_value="missing"))
    monkeypatch.setattr(sx, "tcp_port_is_free", lambda *_: True)
    cli = AsyncMock(return_value=(0, "container", ""))
    monkeypatch.setattr(manager, "_run_cli", cli)
    await manager.start(force=True)
    args = cli.await_args.args[0]
    assert args[args.index("-p") + 1] == "127.0.0.1:8888:8080"


def test_trace_credentials_are_redacted_at_both_envelope_levels():
    from observability.trace_exporters import flatten_attributes
    keys = ["api_key", "authorization", "password", "secret", "cookie", "headers",
            "provider.api-key", "refresh_token", "client_secret"]
    for key in keys:
        attributes = flatten_attributes({key: "sensitive-value", "attributes": {key: "sensitive-value"}})
        assert "sensitive-value" not in str(attributes)
        assert attributes[f"variant1.{key}.redacted"] is True
        assert attributes[f"variant1.attributes.{key}.redacted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "x", "x" * 100, "wrong-token", "非ASCII"])
async def test_activity_bad_tokens_close_cleanly(token):
    import server_http
    from tests.test_activity_websocket import FakeWebSocket, host
    socket = FakeWebSocket(token)
    await server_http.activity_websocket_endpoint(host(), socket, activity_token="presence-token")
    assert socket.closed == [1008]
    assert not socket.accepted


def test_oldest_goal_input_survives_global_and_same_chat_flood(tmp_path):
    from clarification import pending_goal_input, pending_interactions
    from work_fabric.service import WorkService
    from work_fabric.scope import WorkScope
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    target = work.interactions.create(kind="goal_input", prompt="Choose", owner_kind="goal",
        owner_id="goal-a", scope=WorkScope(chat_id="chat-a", goal_id="goal-a", step_id="step"))
    for index in range(1002):
        work.interactions.create(kind="clarification", prompt="Other", owner_kind="chat",
            owner_id=f"other-{index}", scope=WorkScope(chat_id="chat-a" if index % 2 else "chat-b"))
    assert pending_goal_input(work.interactions, "chat-a") == target
    assert pending_goal_input(work.interactions, "") is None
    assert target.interaction_id in {row["id"] for row in pending_interactions(work.interactions, "chat-a")}


@pytest.mark.asyncio
async def test_orphan_logging_failure_does_not_abort_scan(monkeypatch, capsys):
    import host_orphan
    from agent_engine import snapshot_utils
    monkeypatch.setattr(host_orphan, "orphaned_task_payload", lambda _: {"goal": None, "updated_at": "bad"})
    def failed(*_, **__):
        raise OSError("log unavailable")
    monkeypatch.setattr(snapshot_utils, "log_snapshot_event", failed)
    await host_orphan.check_orphaned_task(object())
    assert "orphaned in-progress task" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_native_wake_settles_permanent_admission_error(tmp_path, monkeypatch, committed):
    from tests.test_peers import _stack, _settle_service
    service, runtimes, sessions, chat, first, second = _stack(tmp_path)
    monkeypatch.setattr(service, "_schedule_native_wake", lambda _: None)
    original = runtimes.enqueue_input
    # First create a persisted message at a recognized transient boundary.
    def transient(*_, **__):
        raise RuntimeError("session_configuration_pending")
    monkeypatch.setattr(runtimes, "enqueue_input", transient)
    row = await service.send(f"chat:{first}", f"chat:{second}", "Review", request_id="wake-failure")
    attempts = []
    def failure(*args, **kwargs):
        attempts.append(kwargs["ticket_id"])
        if committed:
            original(*args, **kwargs)
        raise ValueError("permanent admission error")
    monkeypatch.setattr(runtimes, "enqueue_input", failure)
    try:
        await service._wake_native(second)
        updated = service.repository.get_message(row["message_id"])
        assert len(attempts) == 1
        assert updated["state"] == ("queued" if committed else "failed")
        assert bool(updated["delivery_ticket_id"]) == committed
        assert "ValueError" in updated["error"]
        assert chat.start_next_queued_input.await_count == int(committed)
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_search_client_ignores_environment_proxy(monkeypatch):
    from web_search import search
    observed = []
    class Client:
        def __init__(self, **kwargs):
            observed.append(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return False
    monkeypatch.setattr(search.httpx, "AsyncClient", Client)
    with pytest.raises(RuntimeError, match="no engine completed"):
        await search._search_uncached("test", 1, [], ("isolated",))
    assert observed[0]["trust_env"] is False


@pytest.mark.asyncio
async def test_native_wake_is_not_hidden_by_other_chats_backlog(tmp_path, monkeypatch):
    from tests.test_peers import _stack, _settle_service
    service, runtimes, _, chat, first, second = _stack(tmp_path)
    monkeypatch.setattr(service, '_schedule_native_wake', lambda _: None)
    for index in range(502):
        service.repository.persist_message({'message_id': f'old-{index}', 'sender_peer_id': f'chat:{second}',
            'target_peer_id': f'chat:{first}', 'content': 'Older work', 'message_kind': 'request'})
    row, _ = service.repository.persist_message({'message_id': 'new-target', 'sender_peer_id': f'chat:{first}',
        'target_peer_id': f'chat:{second}', 'content': 'Target work', 'message_kind': 'request'})
    try:
        await service._wake_native(second)
        assert service.repository.get_message(row['message_id'])['delivery_ticket_id']
        chat.start_next_queued_input.assert_awaited_once_with(second)
    finally:
        await _settle_service(service, runtimes)
