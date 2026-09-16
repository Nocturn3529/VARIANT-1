"""Intent, attribution, and stock receiver acceptance through canonical paths."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from peers import PeerRepository, PeerError
from peers.bridge_contract import call_peer_tool
from peers.delivery import grok_delivery_status
from peers.grok import GrokIntegration
from tests.test_peers import _stack, _settle_service, _portable_connection


@pytest.mark.asyncio
async def test_notice_result_stay_inbox_while_requests_wake_and_replies_correlate(tmp_path):
    service, runtimes, _sessions, chat, first, second = _stack(tmp_path)
    service._schedule_native_wake = Mock()
    try:
        a, b = "chat:" + first, "chat:" + second
        request = await service.send(a, b, "Review parser", request_id="review")
        notice = await service.send(a, b, "FYI", message_kind="notice", request_id="fyi")
        result = await service.reply(b, request["message_id"], "Reviewed", request_id="answer")
        assert request["message_kind"] == "request" and request["delivery_ticket_id"]
        assert notice["message_kind"] == "notice" and not notice["delivery_ticket_id"]
        assert result["message_kind"] == "result" and not result["delivery_ticket_id"]
        service._schedule_native_wake.assert_called_once_with(second)
        assert all(row["message_kind"] == "request" for row in service.repository.pending_native())
        settled = await service.wait_message(a, request["message_id"], timeout_s=0)
        assert settled["status"] == "replied" and settled["reply"]["message_id"] == result["message_id"]
        more_work = await service.reply(b, request["message_id"], "Please inspect this edge", message_kind="request")
        assert more_work["delivery_ticket_id"]
        assert service._schedule_native_wake.call_count == 2
        chat.start_next_queued_input.assert_not_awaited()
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_kind_is_part_of_idempotent_identity_and_never_an_authority_grant(tmp_path):
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    try:
        a, b = "chat:" + first, "chat:" + second
        notice = await service.send(a, b, "No work", message_kind="notice", request_id="fixed")
        assert (await service.send(a, b, "No work", request_id="fixed"))["message_id"] == notice["message_id"]
        with pytest.raises(PeerError, match="conflicts"):
            await service.send(a, b, "No work", message_kind="request", request_id="fixed")
        envelope = await call_peer_tool(service, b, "peers_inspect", {"message_id": notice["message_id"]}, connection={})
        assert envelope["origin"]["authority"] == "peer"
        assert envelope["sender"] == {"peer_id": a, "display_name": "First", "kind": "variant_chat"}
        assert envelope["content"] == "No work" and envelope["message_kind"] == "notice"
        for kind in ("system", [], True):
            with pytest.raises((PeerError, ValueError)):
                await service.send(a, b, "Bad", message_kind=kind)
        with pytest.raises(PeerError, match="steer"):
            await service.send(a, b, "No work", message_kind="notice", delivery="steer")
        with pytest.raises(ValueError, match="message_kind"):
            await call_peer_tool(service, a, "peers_send", {
                "peer_id": b, "text": "bad", "request_id": "invalid", "message_kind": [],
            }, connection={})
        assert len(service.inbox(b)["messages"]) == 1
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_stock_grok_default_never_prompts_and_explicit_wake_filters_before_limit(tmp_path):
    service, runtimes, sessions, _chat, first, _second = _stack(tmp_path)
    connection = _portable_connection(service, "c", "stock-session", "grok:17:1", process_started_at=2)
    # Real connection/claim storage, isolated ACP recorder. No live TUI/model.
    host = SimpleNamespace(data_dir=str(tmp_path), app_root=str(tmp_path), hub=SimpleNamespace(broadcast=AsyncMock()))
    host.require_runtime = lambda: SimpleNamespace(peers=service, sessions=sessions)
    integration = GrokIntegration(host)
    integration._connection = lambda _peer: {**service.get_connection("c"), "metadata": {"leader_socket": "fixture.sock"}}
    native_calls = []
    async def request(method, params, **kwargs):
        native_calls.append((method, params))
        await kwargs["on_written"]()
        return {"stopReason": "end_turn"}
    integration._controller = AsyncMock(return_value={"client": SimpleNamespace(request=request)})
    integration._resident = AsyncMock(return_value=True)
    peer_id = connection["peer_id"]
    integration.activity[peer_id] = {"state": "idle"}
    try:
        for index in range(6):
            await service.send("chat:" + first, peer_id, f"notice {index}", message_kind="notice")
        work = await service.send("chat:" + first, peer_id, "PRIVATE_PEER_BODY", request_id="work")
        await integration._deliver(peer_id)
        assert not native_calls
        integration._controller.assert_not_awaited()
        status = integration.connection_status(integration._connection(peer_id))
        assert status["delivery_mode"] == "inbox" and not status["native_agent_origin"]
        service.repository.set_delivery_preference(peer_id, "agent_context_prompt", expected="inbox")
        await integration._deliver(peer_id)
        assert len(native_calls) == 1 and native_calls[0][0] == "session/prompt"
        assert work["message_id"] in str(native_calls[0][1])
        assert "PRIVATE_PEER_BODY" not in str(native_calls[0][1])
        assert service.repository.get_message(work["message_id"])["state"] == "observed"
        assert all(row["claim_connection_epoch"] == 0 for row in service.inbox(peer_id)["messages"] if row["message_kind"] == "notice")
        # A replay/reconnect only sees pending requests; no prompt duplicates.
        await integration._deliver(peer_id)
        assert len(native_calls) == 1
        pending = await service.send("chat:" + first, peer_id, "Next request", request_id="next")
        async def changed_during_attach(_connection):
            service.repository.set_delivery_preference(peer_id, "inbox", expected="agent_context_prompt")
            return {"client": SimpleNamespace(request=request)}
        integration._controller.side_effect = changed_during_attach
        await integration._deliver(peer_id)
        assert len(native_calls) == 1
        assert service.repository.get_message(pending["message_id"])["claim_connection_epoch"] == 0
    finally:
        await integration.shutdown()
        await _settle_service(service, runtimes)


def test_preference_survives_repository_reopen_and_runtime_transport_changes(tmp_path):
    path = str(tmp_path / "peers.sqlite3")
    repo = PeerRepository(path)
    assert repo.delivery_preference("grok:s") == "inbox"
    repo.set_delivery_preference("grok:s", "agent_context_prompt", expected="inbox")
    restored = PeerRepository(path)
    assert restored.delivery_preference("grok:s") == "agent_context_prompt"
    assert restored.delivery_preference("grok:different") == "inbox"
    ordinary = {"status": "active", "metadata": {"cwd": "different-directory"}}
    status = grok_delivery_status(ordinary, restored.delivery_preference("grok:s"))
    assert status["delivery_mode"] == "inbox" and status["preferred_delivery_mode"] == "agent_context_prompt"
    assert not status["native_agent_origin"]
    status = grok_delivery_status({**ordinary, "metadata": {"leader_socket": "new.sock"}}, status["preferred_delivery_mode"])
    assert status["live_ingress"] and not status["native_agent_origin"]


def test_migration_preserves_legacy_message_ids_and_delivery_intent(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    repo = PeerRepository(path)
    row, _ = repo.persist_message({"message_id": "legacy", "sender_peer_id": "grok:old", "target_peer_id": "chat:a",
        "content": "old answer", "in_reply_to": "prior", "request_id": "old-key", "delivery": "follow_up"})
    with repo._connect() as conn:
        conn.execute("ALTER TABLE peer_message DROP COLUMN message_kind")
    restored = PeerRepository(path).get_message("legacy")
    assert restored["message_kind"] == "request"
    for key in ("message_id", "request_id", "exchange_id", "in_reply_to", "state"):
        assert restored[key] == row[key]


@pytest.mark.asyncio
async def test_delivery_preference_websocket_returns_exact_peer_and_persisted_mode(tmp_path):
    from tests.test_grok_peers import _handler
    service, runtimes, sessions, _chat, first, _second = _stack(tmp_path)
    conn = _portable_connection(service, "ui-connection", "ui-session", "grok:23:1", process_started_at=2)
    host = SimpleNamespace(data_dir=str(tmp_path), app_root=str(tmp_path),
        hub=SimpleNamespace(broadcast=AsyncMock()), require_runtime=lambda: SimpleNamespace(peers=service, sessions=sessions))
    integration = GrokIntegration(host)
    host.grok_peer_integration = integration
    integration._connection = lambda _: {**conn, "metadata": {"leader_socket": "fixture.sock"}}
    integration._wake = Mock()
    socket = SimpleNamespace(send_json=AsyncMock())
    request = {"type": "peers:grok:delivery", "chat_id": first, "request_id": "preference-write",
        "peer_id": conn["peer_id"], "delivery_mode": "agent_context_prompt", "expected_delivery_mode": "inbox"}
    try:
        await _handler()(host, socket, None, request)
        result = socket.send_json.call_args.args[0]
        assert result["ok"] and result["request_id"] == "preference-write"
        assert result["result"]["peer_id"] == conn["peer_id"]
        assert result["result"]["preferred_delivery_mode"] == "agent_context_prompt"
        assert not result["result"]["native_agent_origin"]
        await _handler()(host, socket, None, request)
        assert socket.send_json.call_args.args[0]["result"]["revision"] == result["result"]["revision"]
        integration._connection = lambda _: {**conn, "metadata": {}}
        await _handler()(host, socket, None, {**request, "delivery_mode": "inbox", "expected_delivery_mode": "agent_context_prompt"})
        assert socket.send_json.call_args.args[0]["ok"]
        await _handler()(host, socket, None, request)
        rejected = socket.send_json.call_args.args[0]
        assert not rejected["ok"] and rejected["error"]["code"] == "grok_wake_unavailable"
        assert service.repository.delivery_preference(conn["peer_id"]) == "inbox"
    finally:
        await integration.shutdown()
        await _settle_service(service, runtimes)
