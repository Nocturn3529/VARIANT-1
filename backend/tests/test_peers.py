from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat_finalize import _durable_turn_messages
from chat_session import ConnectionSession
from host_chat_service import ChatService, NativeChatEventTransport
from peers import PeerCommunicationService, PeerError, PeerRepository
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tests.support.conversation_sessions import open_sessions
from tests.test_session_catalog import catalog_stack


def _stack(tmp_path):
    database = str(tmp_path / "runtime.sqlite3")
    runtime_repository = SessionRuntimeRepository(database)
    runtimes = SessionRuntimeRegistry(runtime_repository)
    sessions = open_sessions(tmp_path / "chats")
    sessions.bind_runtime_lifecycle(runtimes.ensure_runtime)
    first = sessions.create_session("First", make_active=False)
    second = sessions.create_session("Second", make_active=False)
    chat = SimpleNamespace(
        start_next_queued_input=AsyncMock(return_value={"status": "started"}),
    )
    host = SimpleNamespace()
    service = PeerCommunicationService(
        host,
        PeerRepository(database),
        sessions=sessions,
        session_runtimes=runtimes,
        chat_service=chat,
    )
    return service, runtimes, sessions, chat, first, second


async def _settle_service(service, runtimes):
    await service.shutdown()
    await runtimes.shutdown()


@pytest.mark.asyncio
async def test_peer_display_is_canonical_and_sender_trace_survives_reload(tmp_path):
    from chat_finalize import _display_transcript
    from capability_broker import InvocationContext

    service, runtimes, sessions, _chat, first, second = _stack(tmp_path)
    service.host.emit_activity = AsyncMock()
    invocation = InvocationContext(
        chat_id=first, run_id="run-sender", outer_tool_call_id="outer-1",
        cell_execution_id="cell-1", nested_call_id="nested-1", catalog_release_id="catalog",
    )
    try:
        sent = await service.send(
            f"chat:{first}", f"chat:{second}", "Clean **message**\nSecond line",
            request_id="trace-send", _invocation=invocation,
        )
        # Retries and delivery receipts retain the first sender's invocation.
        await service.send(
            f"chat:{first}", f"chat:{second}", sent["content"], request_id="trace-send",
        )
        updated = service.repository.update_message(sent["message_id"], evidence={
            "ticket_state": "delivered", "sender_invocation": {"run_id": "spoof"},
            "sender_display_name": "spoof",
        })
        assert updated["evidence"]["sender_invocation"]["run_id"] == "run-sender"
        assert updated["evidence"]["sender_display_name"] == "First"
        service.host.emit_activity.assert_awaited_once()
        event = service.host.emit_activity.await_args
        assert event.args == ("peer:sent",)
        assert event.kwargs["call_id"] == "nested-1"
        assert event.kwargs["outer_call_id"] == "outer-1"
        assert event.kwargs["cell_execution_id"] == "cell-1"
        assert event.kwargs["peer_message"]["content"] == sent["content"]

        origin = {"kind": "peer", "peer_id": f"chat:{first}", "message_id": sent["message_id"]}
        framed = service._native_text(sent)
        incoming = [{"role": "user", "text": framed, "origin": origin},
                    {"role": "assistant", "text": "Received", "run_id": "receiver-run"}]
        sessions.append_messages(second, incoming)
        assert _display_transcript(sessions, incoming, second)[0]["peer_display"] == {
            "display_name": "First", "content": sent["content"],
        }
        # Read projection resolves older rows even when no display snapshot exists.
        sessions._peer_display = None
        sessions.append_messages(second, incoming)
        sessions.bind_peer_display(service.message_display, service.sent_display)
        rows = sessions.get_session(second)["messages"]
        assert rows[-2]["peer_display"]["content"] == sent["content"]
        model_rows = sessions.context_projection_view(second)["messages"]
        assert model_rows[0]["content"] == framed
        assert model_rows[0]["origin"] == origin
        assert all("peer_display" not in row and "peer_sent" not in row for row in model_rows)
        assert "peer_display" not in sessions.project_messages_for_display([
            {"role": "user", "text": framed},
        ])[0]
        assert service.message_display({**origin, "peer_id": f"chat:{second}"}) is None

        outgoing = [{"role": "user", "text": "Send a message"},
                    {"role": "assistant", "text": "Sent", "run_id": "run-sender"}]
        sessions.append_messages(first, outgoing)
        live = _display_transcript(sessions, outgoing, first)[-1]["peer_sent"]
        stored = sessions.get_session(first)["messages"][-1]
        assert stored["run_id"] == "run-sender"
        assert stored["peer_sent"] == live
        assert live[0]["message_id"] == sent["message_id"]
        assert live[0]["sender_invocation"]["cell_execution_id"] == "cell-1"
        assert service.sent_display(second, "run-sender")["messages"] == []
        assert service.sent_display(first, "different-run")["messages"] == []
        # Receiver/user-triggered sends must not acquire forged agent attribution.
        human = await service.send(f"chat:{first}", f"chat:{second}", "UI message")
        human = service.repository.update_message(human["message_id"], evidence={
            "sender_invocation": {"run_id": "run-sender"},
        })
        assert "sender_invocation" not in human["evidence"]
        assert len(service.sent_display(first, "run-sender")["messages"]) == 1
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_native_discovery_and_busy_delivery_are_chat_owned(tmp_path):
    service, runtimes, _sessions, chat, first, second = _stack(tmp_path)
    admission = runtimes.try_reserve_run(second, attachment_id="")
    running = asyncio.create_task(asyncio.Event().wait())
    runtimes.bind_admission_task(admission, running)
    runtimes.set_run_paused(second, True, expected_admission_id=admission)
    try:
        peers = service.list_peers(kind="variant_chat")
        assert {row["peer_id"] for row in peers} == {
            f"chat:{first}", f"chat:{second}",
        }
        message = await service.send(
            f"chat:{first}", f"chat:{second}", "Review the parser",
            request_id="send-1",
        )
        assert message["message_id"].startswith("peer_message_")
        assert message["state"] == "queued"
        ticket = runtimes.repository.get_ticket(message["delivery_ticket_id"])
        assert ticket is not None
        assert ticket.chat_id == second
        assert ticket.source == f"peer:chat:{first}"
        assert ticket.client_id == f"peer-message:{message['message_id']}"
        assert "Sender: First" in ticket.text
        await asyncio.sleep(0)
        chat.start_next_queued_input.assert_not_awaited()
        runtimes.park_queued_input_tickets(second, reason="explicit_stop")
        assert service.inspect_message(
            f"chat:{first}", message["message_id"],
        )["state"] == "parked"
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        runtimes.finish_run(admission, status="test")
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_idle_native_delivery_schedules_canonical_chat_wake(tmp_path):
    service, runtimes, _sessions, chat, first, second = _stack(tmp_path)
    try:
        message = await service.send(
            f"chat:{first}", f"chat:{second}", "Check this", request_id="idle",
        )
        for _ in range(50):
            if chat.start_next_queued_input.await_count:
                break
            await asyncio.sleep(0.01)
        chat.start_next_queued_input.assert_awaited_once_with(second)
        assert message["state"] == "queued"
        runtimes.repository.transition_ticket(
            message["delivery_ticket_id"], "running", expected=("queued",),
        )
        assert service.inspect_message(
            f"chat:{first}", message["message_id"],
        )["state"] == "queued"
        runtimes.repository.transition_ticket(
            message["delivery_ticket_id"], "transcript_committing",
            expected=("running",),
        )
        observed = service.inspect_message(
            f"chat:{first}", message["message_id"],
        )
        assert observed["state"] == "observed"
        assert observed["evidence"]["receipt_semantics"] == (
            "recipient_transcript_commit_started"
        )
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_sender_request_id_is_durable_and_conflicts_fail_before_rewrite(tmp_path):
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    try:
        first_send = await service.send(
            f"chat:{first}", f"chat:{second}", "Same", request_id="stable",
        )
        replay = await service.send(
            f"chat:{first}", f"chat:{second}", "Same", request_id="stable",
        )
        assert replay["message_id"] == first_send["message_id"]
        assert service.inspect_request(f"chat:{first}", "stable")["message_id"] == first_send["message_id"]
        with pytest.raises(RuntimeError, match="conflicts"):
            await service.send(
                f"chat:{first}", f"chat:{second}", "Different", request_id="stable",
            )
        assert service.inspect_request(f"chat:{first}", "stable")["content"] == "Same"
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_committed_send_returns_unknown_when_native_wake_fails(tmp_path, monkeypatch):
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    monkeypatch.setattr(
        runtimes, "enqueue_input", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("ticket store unavailable")
        ),
    )
    try:
        message = await service.send(
            f"chat:{first}", f"chat:{second}", "Persist this",
            request_id="committed-before-wake",
        )
        assert message["state"] == "unknown"
        assert "wake failed after commit" in message["error"]
        reconciled = service.inspect_request(
            f"chat:{first}", "committed-before-wake",
        )
        assert reconciled["message_id"] == message["message_id"]
        assert reconciled["content"] == "Persist this"
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_reply_and_directional_inbox_preserve_exchange(tmp_path):
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    try:
        original = await service.send(
            f"chat:{first}", f"chat:{second}", "Question", request_id="question",
        )
        reply = await service.reply(
            f"chat:{second}", original["message_id"], "Answer", request_id="answer",
        )
        assert reply["exchange_id"] == original["exchange_id"]
        assert reply["in_reply_to"] == original["message_id"]
        assert service.inspect_message(
            f"chat:{first}", original["message_id"],
        )["state"] == "replied"
        incoming = service.inbox(f"chat:{first}", direction="incoming")
        outgoing = service.inbox(f"chat:{first}", direction="outgoing")
        combined = service.inbox(f"chat:{first}", direction="all")
        assert [row["message_id"] for row in incoming["messages"]] == [reply["message_id"]]
        assert [row["message_id"] for row in outgoing["messages"]] == [original["message_id"]]
        assert len(combined["messages"]) == 2
        assert all("request_id" in row for row in combined["messages"])
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_wait_returns_for_inbound_attention_instead_of_deadlocking(tmp_path):
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    try:
        outgoing = await service.send(
            f"chat:{first}", f"chat:{second}", "Need a reply", request_id="waited",
        )
        waiter = asyncio.create_task(service.wait_message(
            f"chat:{first}", outgoing["message_id"], timeout_s=2,
        ))
        await asyncio.sleep(0.02)
        attention = await service.send(
            f"chat:{second}", f"chat:{first}", "Mutual request", request_id="attention",
        )
        outcome = await asyncio.wait_for(waiter, 1)
        assert outcome["status"] == "attention"
        assert outcome["incoming"]["message_id"] == attention["message_id"]
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_mutual_requests_committed_before_wait_are_attention(tmp_path):
    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)
    try:
        first_request = await service.send(
            f"chat:{first}", f"chat:{second}", "Need B", request_id="mutual-a",
        )
        second_request = await service.send(
            f"chat:{second}", f"chat:{first}", "Need A", request_id="mutual-b",
        )
        first_wait, second_wait = await asyncio.gather(
            service.wait_message(
                f"chat:{first}", first_request["message_id"], timeout_s=1,
            ),
            service.wait_message(
                f"chat:{second}", second_request["message_id"], timeout_s=1,
            ),
        )
        assert first_wait["status"] == "attention"
        assert first_wait["incoming"]["message_id"] == second_request["message_id"]
        assert second_wait["status"] == "attention"
        assert second_wait["incoming"]["message_id"] == first_request["message_id"]
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_pairwise_history_is_not_starved_by_unrelated_peer_traffic(tmp_path):
    service, runtimes, sessions, _chat, first, second = _stack(tmp_path)
    third = sessions.create_session("Third", make_active=False)
    try:
        for index in range(12):
            await service.send(
                f"chat:{first}", f"chat:{third}", f"noise-{index}",
                request_id=f"noise-{index}",
            )
        wanted = await service.send(
            f"chat:{first}", f"chat:{second}", "pairwise",
            request_id="pairwise-wanted",
        )
        page = service.history(f"chat:{first}", f"chat:{second}", limit=1)
        assert [row["message_id"] for row in page["messages"]] == [
            wanted["message_id"]
        ]
        assert page["cursor"] == wanted["sequence"]
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_deleted_sender_still_has_durable_attribution_at_admission(tmp_path):
    service, runtimes, sessions, _chat, first, second = _stack(tmp_path)
    sender_peer_id = f"chat:{first}"
    row, _ = service.repository.persist_message({
        "sender_peer_id": sender_peer_id,
        "target_peer_id": f"chat:{second}",
        "content": "survives sender deletion",
        "delivery": "follow_up",
        "state": "persisted",
        "request_id": "deleted-sender",
    })
    sessions.delete(first)
    try:
        admitted = await service._admit_native(row)
        ticket = runtimes.repository.get_ticket(admitted["delivery_ticket_id"])
        assert ticket is not None
        assert f"Sender: {sender_peer_id} ({sender_peer_id})" in ticket.text
        assert "survives sender deletion" in ticket.text
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_real_chat_service_wakes_without_view_and_chains_peer_turns(tmp_path):
    service, runtimes, sessions, _chat, first, second = _stack(tmp_path)
    hub = SimpleNamespace(broadcast=AsyncMock())
    runtime = SimpleNamespace(sessions=sessions, session_runtimes=runtimes)
    host = SimpleNamespace(hub=hub, require_runtime=lambda: runtime)
    chat = ChatService(host=host, transcript=None, models=None)
    service.host = host
    service.chat_service = chat
    calls = []

    async def provider_backed_turn(transport, text, session, **kwargs):
        assert isinstance(transport, NativeChatEventTransport)
        assert runtimes.attached_transports(second) == []
        calls.append({"text": text, "ticket_id": kwargs["ticket_id"]})
        runtimes.repository.transition_ticket(
            kwargs["ticket_id"], "completed", expected=("preparing",),
            proof={"provider_turn": len(calls)},
        )
        if len(calls) == 1:
            runtimes.park_queued_input_tickets(
                second, reason="turn_ok_before_input_delivery",
            )
        await transport.send_json({"type": "done", "text": "ok"})

    chat.run_task = provider_backed_turn
    try:
        first_message = await service.send(
            f"chat:{first}", f"chat:{second}", "first headless turn",
            request_id="headless-1",
        )
        second_message = await service.send(
            f"chat:{first}", f"chat:{second}", "second headless turn",
            request_id="headless-2",
        )
        for _ in range(200):
            if len(calls) == 2:
                break
            await asyncio.sleep(0.01)
        assert [row["ticket_id"] for row in calls] == [
            first_message["delivery_ticket_id"],
            second_message["delivery_ticket_id"],
        ]
        assert all("[Peer message]" in row["text"] for row in calls)
        assert service.inspect_message(
            f"chat:{first}", first_message["message_id"],
        )["state"] == "observed"
        assert service.inspect_message(
            f"chat:{first}", second_message["message_id"],
        )["state"] == "observed"
        assert hub.broadcast.await_count == 2
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_external_binding_claim_filter_and_settlement_are_epoch_fenced(tmp_path):
    service, runtimes, _sessions, _chat, first, _second = _stack(tmp_path)
    try:
        grok = service.register_external(
            "external:grok:one", "Grok one", "grok", "session-1",
            "terminal-1", "process-1", 4, 7,
            {"live_ingress": True, "structured_replies": True},
        )
        service.register_external(
            "external:grok:two", "Grok two", "grok", "session-2",
            "terminal-2", "process-2", 5, 7,
            {"live_ingress": True},
        )
        with pytest.raises(RuntimeError, match="new connection epoch"):
            service.register_external(
                "external:grok:one", "Grok one", "grok", "session-1",
                "terminal-1", "different-process", 4, 7,
                {"live_ingress": True},
            )
        with pytest.raises(RuntimeError, match="new connection epoch"):
            service.update_external(
                "external:grok:one", 7,
                terminal_id="different-terminal",
            )
        message = await service.send(
            f"chat:{first}", grok["peer_id"], "External work", request_id="grok-send",
        )
        assert message["state"] == "queued"
        assert service.claim_external_delivery(
            "grok", 7, peer_id="external:grok:two",
        ) == []
        claimed = service.claim_external_delivery(
            "grok", 7, peer_id=grok["peer_id"],
        )
        assert [row["message_id"] for row in claimed] == [message["message_id"]]
        with pytest.raises(PeerError, match="epoch changed"):
            await service.settle_external_delivery(
                message["message_id"], 8, "observed",
            )
        written = await service.settle_external_delivery(
            message["message_id"], 7, "transport_written",
            evidence={"write_id": "grok-write-1"},
        )
        assert written["message"]["claim_connection_epoch"] == 7
        observed = await service.settle_external_delivery(
            message["message_id"], 7, "observed",
            evidence={"turn_id": "grok-turn-1"},
        )
        assert observed["message"]["state"] == "observed"
        assert observed["message"]["claim_connection_epoch"] == 7
        replied = await service.settle_external_delivery(
            message["message_id"], 7, "observed",
            evidence={"turn_id": "grok-turn-1"},
            reply_text="External answer", request_id="grok-reply-1",
        )
        assert replied["message"]["state"] == "replied"
        assert replied["message"]["claim_connection_epoch"] == 0
        late = await service.settle_external_delivery(
            message["message_id"], 7, "observed",
            evidence={"late": True},
        )
        assert late["message"]["state"] == "replied"
        assert "late" not in late["message"]["evidence"]

        second_message = await service.send(
            f"chat:{first}", grok["peer_id"], "Reply through MCP",
            request_id="grok-explicit-reply",
        )
        service.claim_external_delivery("grok", 7, peer_id=grok["peer_id"])
        await service.settle_external_delivery(
            second_message["message_id"], 7, "transport_written",
        )
        explicit = await service.reply(
            grok["peer_id"], second_message["message_id"], "MCP answer",
            request_id="mcp-reply",
        )
        original = service.repository.get_message(second_message["message_id"])
        assert explicit["in_reply_to"] == second_message["message_id"]
        assert original["state"] == "replied"
        assert original["claim_connection_epoch"] == 0
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_external_reply_is_not_advertised_before_durable_reply_commit(tmp_path):
    service, runtimes, sessions, _chat, first, _second = _stack(tmp_path)
    external = service.register_external(
        "external:grok:atomic", "Grok", "grok", "session-a",
        "terminal-a", "process-a", 1, 1, {"structured_replies": True},
    )
    original = await service.send(
        f"chat:{first}", external["peer_id"], "Question",
        request_id="atomic-question",
    )
    service.claim_external_delivery("grok", 1, peer_id=external["peer_id"])
    sessions.delete(first)
    try:
        outcome = await service.settle_external_delivery(
            original["message_id"], 1, "replied",
            evidence={"transport": "test"}, reply_text="Could not persist",
            request_id="atomic-answer",
        )
        assert outcome["reply"] is None
        assert outcome["message"]["state"] == "unknown"
        assert outcome["message"]["claim_connection_epoch"] == 0
        assert "not durably committed" in outcome["message"]["error"]
        assert service.repository.find_reply(original["message_id"]) is None
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_live_external_epoch_rollover_fences_prior_claims_unknown(tmp_path):
    service, runtimes, _sessions, _chat, first, _second = _stack(tmp_path)
    external = service.register_external(
        "external:grok:rollover", "Grok", "grok", "session-r",
        "terminal-r1", "process-r1", 1, 1, {"live_ingress": True},
    )
    original = await service.send(
        f"chat:{first}", external["peer_id"], "Possibly delivered",
        request_id="rollover-question",
    )
    service.claim_external_delivery("grok", 1, peer_id=external["peer_id"])
    await service.settle_external_delivery(
        original["message_id"], 1, "transport_written",
        evidence={"write_id": "old-epoch-write"},
    )
    try:
        updated = service.register_external(
            external["peer_id"], "Grok", "grok", "session-r",
            "terminal-r2", "process-r2", 2, 2, {"live_ingress": True},
        )
        assert updated["connection_epoch"] == 2
        fenced = service.repository.get_message(original["message_id"])
        assert fenced["state"] == "unknown"
        assert fenced["claim_connection_epoch"] == 0
        assert "prior connection epoch" in fenced["error"]
        assert service.claim_external_delivery(
            "grok", 2, peer_id=external["peer_id"],
        ) == []
    finally:
        await _settle_service(service, runtimes)


def _portable_connection(
    service, connection_id, native_session_id, runtime_id, *, process_started_at,
    epoch=1, lease_seconds=15.0,
):
    return service.register_connection(
        connection_id, "grok", native_session_id,
        process_id=f"mcp-{connection_id}",
        process_started_at=process_started_at,
        runtime_id=runtime_id,
        runtime_pid=runtime_id.rsplit(":", 1)[-1],
        runtime_started_at=process_started_at - 1,
        epoch=epoch,
        lease_seconds=lease_seconds,
        capabilities={"structured_replies": True},
        metadata={"cwd": "C:/portable"},
    )


@pytest.mark.asyncio
async def test_portable_logical_peer_is_terminal_free_and_same_actor_shares_one_cas_owner(tmp_path):
    service, runtimes, _sessions, _chat, first, _second = _stack(tmp_path)
    wakes = []
    service.register_external_hook("mcp-peer-bridge", lambda row: wakes.append(row["message_id"]))
    try:
        legacy = service.register_external(
            "grok:native-session", "Grok Build", "grok-acp-mcp", "native-session",
            "old-terminal", "old-process", 1, 99,
            {"structured_replies": True},
        )
        assert legacy["terminal_id"] == "old-terminal"
        first_connection = _portable_connection(
            service, "connection-a", "native-session", "grok-runtime:41",
            process_started_at=100.0,
        )
        second_connection = _portable_connection(
            service, "connection-b", "native-session", "grok-runtime:41",
            process_started_at=101.0,
        )
        peer = service.get_peer("grok:native-session")
        assert peer["adapter"] == "mcp-peer-bridge"
        assert peer["terminal_id"] == ""
        assert peer["process_id"] == ""
        assert peer["connection_epoch"] == 0
        assert peer["status"] == "connected"
        assert len(peer["active_connections"]) == 2
        assert first_connection["delivery_owner"] is True
        assert second_connection["delivery_owner"] is True

        message = await service.send(
            f"chat:{first}", peer["peer_id"], "portable work",
            request_id="portable-work",
        )
        assert message["state"] == "queued"
        assert wakes == [message["message_id"]]
        claimed = service.claim_connection_delivery("connection-b", 1)
        assert [row["message_id"] for row in claimed] == [message["message_id"]]
        assert claimed[0]["claim_connection_id"] == "connection-b"
        assert service.claim_connection_delivery("connection-a", 1) == []
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_conflicting_runtime_actors_fence_push_without_blocking_mailbox_reads(tmp_path):
    service, runtimes, _sessions, _chat, first, _second = _stack(tmp_path)
    try:
        _portable_connection(
            service, "actor-a-connection", "shared-session", "grok-runtime:100",
            process_started_at=200.0,
        )
        ambiguous = await service.send(
            f"chat:{first}", "grok:shared-session", "possibly written",
            request_id="conflict-ambiguous",
        )
        service.claim_connection_delivery("actor-a-connection", 1)
        await service.settle_connection_delivery(
            "actor-a-connection", 1, ambiguous["message_id"],
            "transport_written", evidence={"write_id": "write-before-conflict"},
        )
        queued = await service.send(
            f"chat:{first}", "grok:shared-session", "still queued",
            request_id="conflict-queued",
        )

        conflicting = _portable_connection(
            service, "actor-b-connection", "shared-session", "grok-runtime:200",
            process_started_at=201.0,
        )
        assert conflicting["status"] == "conflicted"
        assert set(conflicting["conflict_runtime_ids"]) == {
            "grok-runtime:100", "grok-runtime:200",
        }
        assert service.get_connection("actor-a-connection")["status"] == "conflicted"
        assert service.get_peer("grok:shared-session")["status"] == "conflicted"
        assert {
            row["connection_id"] for row in service.list_connections(
                peer_id="grok:shared-session",
                statuses=("active", "conflicted"),
            )
        } == {"actor-a-connection", "actor-b-connection"}
        assert service.list_active_connections(peer_id="grok:shared-session") == []
        fenced = service.repository.get_message(ambiguous["message_id"])
        assert fenced["state"] == "unknown"
        assert fenced["claim_connection_id"] == ""
        assert service.repository.get_message(queued["message_id"])["state"] == "queued"

        # Conflict affects delivery ownership, not the common read/reply mailbox.
        inbox = service.inbox("grok:shared-session", direction="incoming")
        assert {row["message_id"] for row in inbox["messages"]} == {
            ambiguous["message_id"], queued["message_id"],
        }
        with pytest.raises(PeerError) as error:
            service.claim_connection_delivery("actor-a-connection", 1)
        assert error.value.code == "peer_connection_conflicted"
        with pytest.raises(PeerError) as error:
            service.claim_connection_delivery("actor-b-connection", 1)
        assert error.value.code == "peer_connection_conflicted"

        service.close_connection(
            "actor-b-connection", 1, reason="losing actor closed",
        )
        survivor = service.get_connection("actor-a-connection")
        assert survivor["status"] == "active"
        assert survivor["delivery_owner"] is True
        assert [row["message_id"] for row in service.claim_connection_delivery(
            "actor-a-connection", 1,
        )] == [queued["message_id"]]
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_expired_connection_fences_claim_and_disconnected_peer_retains_queue(tmp_path):
    service, runtimes, _sessions, _chat, first, _second = _stack(tmp_path)
    try:
        connection = _portable_connection(
            service, "expiring-connection", "durable-session", "grok-runtime:300",
            process_started_at=300.0, lease_seconds=3.0,
        )
        ambiguous = await service.send(
            f"chat:{first}", "grok:durable-session", "claimed before expiry",
            request_id="expires-claimed",
        )
        service.claim_connection_delivery("expiring-connection", 1)
        effects = service.expire_connections(now=connection["last_seen"] + 4.0)
        assert [row["connection_id"] for row in effects["connections"]] == [
            "expiring-connection"
        ]
        assert service.get_connection("expiring-connection")["status"] == "expired"
        assert service.repository.get_message(ambiguous["message_id"])["state"] == "unknown"
        assert service.get_peer("grok:durable-session")["status"] == "disconnected"

        retained = await service.send(
            f"chat:{first}", "grok:durable-session", "wait for resume",
            request_id="offline-queued",
        )
        assert retained["state"] == "queued"
        replacement = _portable_connection(
            service, "replacement-connection", "durable-session", "grok-runtime:400",
            process_started_at=400.0,
        )
        assert replacement["status"] == "active"
        assert [row["message_id"] for row in service.claim_connection_delivery(
            "replacement-connection", 1,
        )] == [retained["message_id"]]
        assert service.repository.get_message(ambiguous["message_id"])["state"] == "unknown"
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_replaced_connection_epoch_fences_claim_without_replay(tmp_path):
    service, runtimes, _sessions, _chat, first, _second = _stack(tmp_path)
    try:
        _portable_connection(
            service, "reused-connection", "epoch-session", "grok-runtime:600",
            process_started_at=600.0,
        )
        message = await service.send(
            f"chat:{first}", "grok:epoch-session", "epoch effect",
            request_id="portable-epoch-effect",
        )
        service.claim_connection_delivery("reused-connection", 1)
        await service.settle_connection_delivery(
            "reused-connection", 1, message["message_id"], "observed",
            evidence={"turn_id": "old-runtime-turn"},
        )
        replaced = _portable_connection(
            service, "reused-connection", "epoch-session", "grok-runtime:601",
            process_started_at=601.0, epoch=2,
        )
        assert replaced["epoch"] == 2
        fenced = service.repository.get_message(message["message_id"])
        assert fenced["state"] == "unknown"
        assert fenced["claim_connection_id"] == ""
        assert "epoch was replaced" in fenced["error"]
        assert service.claim_connection_delivery("reused-connection", 2) == []
    finally:
        await _settle_service(service, runtimes)


def test_connection_heartbeat_is_epoch_fenced_and_legacy_terminal_is_optional(tmp_path):
    service, runtimes, _sessions, _chat, _first, _second = _stack(tmp_path)
    try:
        legacy = service.register_external(
            "grok:legacy-session", "Legacy", "legacy-adapter", "legacy-session",
            "", "", 0, 1, {"structured_replies": True},
        )
        assert legacy["terminal_id"] == ""
        assert legacy["process_id"] == ""

        connection = _portable_connection(
            service, "heartbeat-connection", "heartbeat-session", "grok-runtime:500",
            process_started_at=500.0,
        )
        heartbeat = service.touch_connection(connection["connection_id"], 1)
        assert heartbeat["last_seen"] >= connection["last_seen"]
        assert heartbeat["revision"] == connection["revision"]
        touched = service.touch_connection(
            connection["connection_id"], 1,
            metadata={"cwd": "D:/resumed", "cursor": 7},
        )
        assert touched["metadata"] == {"cwd": "D:/resumed", "cursor": 7}
        assert touched["last_seen"] >= heartbeat["last_seen"]
        assert touched["revision"] > heartbeat["revision"]
        with pytest.raises(PeerError) as error:
            service.touch_connection(connection["connection_id"], 2)
        assert error.value.code == "peer_connection_stale"
        service.close_connection(connection["connection_id"], 1, reason="done")
        with pytest.raises(PeerError) as error:
            _portable_connection(
                service, "heartbeat-connection", "heartbeat-session",
                "grok-runtime:500", process_started_at=500.0,
            )
        assert error.value.code == "peer_connection_conflict"
        renewed = _portable_connection(
            service, "heartbeat-connection", "heartbeat-session",
            "grok-runtime:501", process_started_at=501.0, epoch=2,
        )
        assert renewed["status"] == "active"
        assert renewed["epoch"] == 2
    finally:
        asyncio.run(_settle_service(service, runtimes))


@pytest.mark.asyncio
async def test_restart_reconciles_native_persistence_and_external_ambiguity(tmp_path):
    service, runtimes, _sessions, chat, first, second = _stack(tmp_path)
    native, _ = service.repository.persist_message({
        "sender_peer_id": f"chat:{first}",
        "target_peer_id": f"chat:{second}",
        "content": "Persisted before admission",
        "delivery": "follow_up",
        "state": "persisted",
        "request_id": "crash-native",
    })
    external = service.register_external(
        "external:grok:restart", "Grok", "grok", "session-r",
        "terminal-r", "process-r", 1, 11, {"live_ingress": True},
    )
    outgoing = await service.send(
        f"chat:{first}", external["peer_id"], "Claimed before crash",
        request_id="crash-external",
    )
    assert service.claim_external_delivery("grok", 11)

    await service.start()
    try:
        for _ in range(50):
            restored = service.repository.get_message(native["message_id"])
            if restored and restored.get("delivery_ticket_id"):
                break
            await asyncio.sleep(0.01)
        assert service.repository.get_message(native["message_id"])["delivery_ticket_id"]
        assert service.repository.get_message(outgoing["message_id"])["state"] == "unknown"
        assert chat.start_next_queued_input.await_count >= 1
    finally:
        await _settle_service(service, runtimes)


def test_peer_origin_survives_durable_transcript_projection(tmp_path):
    sessions = open_sessions(tmp_path / "chats")
    chat_id = sessions.create_session()
    session = ConnectionSession(viewed_session_id=chat_id)
    session.active.turn_source = "peer:chat:sender"
    session.active.turn_client_id = "peer-message:message-1"
    session.active.turn_ticket_id = "peer-ticket-1"
    transcript = _durable_turn_messages(
        session, "attributed", "done", mood="neutral",
    )
    sessions.append_messages(chat_id, transcript)

    projected = sessions.get_session(chat_id)["messages"][0]
    expected_origin = {
        "kind": "peer", "peer_id": "chat:sender", "message_id": "message-1",
    }
    assert projected["origin"] == expected_origin
    canonical = sessions.canonical_context(chat_id)
    assert canonical["messages"][0]["origin"] == expected_origin
    assert sessions.set_context_projection(
        chat_id, canonical["messages"],
        source_message_count=len(canonical["messages"]),
        source_cursor=canonical["cursor"],
    )
    assert sessions.get_context_projection(chat_id)["messages"][0]["origin"] == expected_origin


@pytest.mark.asyncio
async def test_peer_origin_survives_lossy_compaction_as_verbatim_user_metadata():
    from transcript_economy import COMPACT_SUMMARY_SENTINEL, compress_messages

    origin = {
        "kind": "peer", "peer_id": "chat:sender", "message_id": "message-compact",
    }
    messages = [
        {"role": "user", "content": "head user"},
        {"role": "assistant", "content": "head answer"},
        {"role": "assistant", "content": "older evidence 1"},
        {"role": "user", "content": "peer requirement", "origin": origin},
        {"role": "assistant", "content": "older evidence 2"},
        {"role": "assistant", "content": "older evidence 3"},
        *[
            {"role": "assistant", "content": f"tail {index}"}
            for index in range(6)
        ],
    ]

    async def complete(*_args, **_kwargs):
        return (
            "## Goal\nKeep working.\n"
            "## Confirmed Results\nEvidence retained.\n"
            "## Pending Work\nContinue.\n"
            "## Failed Attempts (not blockers)\nNone observed.\n"
            "## Exact Context\nExact values retained.\n"
            + COMPACT_SUMMARY_SENTINEL
        )

    compacted = await compress_messages(
        messages, complete=complete, protect_first=2, protect_last=6,
    )
    peer_rows = [row for row in compacted if row.get("origin") == origin]
    assert peer_rows == [{
        "role": "user", "content": "peer requirement", "origin": origin,
    }]


@pytest.mark.asyncio
async def test_operate_peer_object_returns_bound_handles(tmp_path, monkeypatch):
    from peers import capabilities as peer_capabilities
    from tools import ToolRegistry

    service, runtimes, _sessions, _chat, first, second = _stack(tmp_path)

    class Ref:
        opaque_id = "ref-dispatch"
        handler_revision = "handler"
        catalog_release_id = "catalog"
        slot_id = ""
        slot_version = 0

    broker = SimpleNamespace(ref_for_name=lambda *_args, **_kwargs: Ref())
    runtime = SimpleNamespace(peers=service, broker=broker)
    service.host.require_runtime = lambda: runtime
    registry = ToolRegistry()
    peer_capabilities.register_peers_tool(
        registry, lambda: runtime, service.host,
    )
    invocation = SimpleNamespace(
        chat_id=first,
        catalog_release_id="catalog",
        mount_revision=1,
        cell_origin=SimpleNamespace(to_dict=lambda: {"chat_id": first}),
    )
    monkeypatch.setattr(
        peer_capabilities, "current_capability_invocation",
        lambda: invocation,
    )
    try:
        listed = await registry.get("peers").handler({
            "operation": "list", "kind": "variant_chat",
        })
        payloads = [row["$variant1_handle"] for row in listed]
        assert {row["metadata"]["peer_id"] for row in payloads} == {
            f"chat:{first}", f"chat:{second}",
        }
        sent = await registry.get("peers").handler({
            "operation": "send", "target_peer_id": f"chat:{second}",
            "text": "From Operate", "request_id": "operate-send",
        })
        message = sent["$variant1_handle"]
        assert message["kind"] == "message"
        assert message["metadata"]["request_id"] == "operate-send"
        assert message["methods"]["items"][-1]["name"] == "wait"
    finally:
        await _settle_service(service, runtimes)


@pytest.mark.asyncio
async def test_real_cpython_broker_front_door_sends_replies_waits_and_breaks_mutual_waits(
    catalog_stack, tmp_path,
):
    from peers.capabilities import register_peers_tool
    from work_fabric.capabilities import register_work_fabric_tools

    registry, enabled, runtimes, _artifacts, broker, catalog, kernel = catalog_stack
    sessions = open_sessions(tmp_path / "peer-kernel-chats")
    sessions.bind_runtime_lifecycle(runtimes.ensure_runtime)
    chat = SimpleNamespace(
        start_next_queued_input=AsyncMock(
            return_value={"status": "parked", "reason": "test_no_provider"},
        ),
    )
    host = SimpleNamespace(remote_handle_routers={})
    service = PeerCommunicationService(
        host, PeerRepository(str(tmp_path / "astb.sqlite3")),
        sessions=sessions, session_runtimes=runtimes, chat_service=chat,
    )
    runtime = SimpleNamespace(peers=service, broker=broker, registry=registry)
    host.require_runtime = lambda: runtime
    service.host = host

    registry.remove("peers")
    registry.remove("remote_handle_dispatch")
    register_work_fabric_tools(host)
    register_peers_tool(registry, lambda: runtime, host)
    assert {"peers", "remote_handle_dispatch"}.issubset(enabled)
    catalog.reconcile_registry()
    first = sessions.create_session("Kernel A", make_active=False)
    second = sessions.create_session("Kernel B", make_active=False)
    catalog.select(first, "operate")
    catalog.select(second, "operate")

    try:
        sent_result = await kernel.execute(
            chat_id=first, run_id="peer-kernel-send",
            outer_tool_call_id="peer-kernel-send-call",
            code=(
                "target = next(p for p in peers.list() "
                f"if p.metadata['peer_id'] == 'chat:{second}')\n"
                "sent = target.send(text='hello through CPython', "
                "request_id='cpython-send')\n"
                "notice = target.send(text='notice through CPython', message_kind='notice', request_id='cpython-notice')\n"
                f"notice2 = peers.send(target_peer_id='chat:{second}', text='object notice', message_kind='notice', request_id='cpython-object-notice')\n"
                "print(sent.metadata['request_id'], sent.metadata['state'])"
            ),
        )
        assert sent_result.ok, sent_result.to_dict()
        assert "cpython-send queued" in sent_result.output.text()
        for request_id in ("cpython-notice", "cpython-object-notice"):
            notice_row = service.repository.get_message_by_request(f"chat:{first}", request_id)
            assert notice_row["message_kind"] == "notice"
            assert not notice_row["delivery_ticket_id"]

        reply_result = await kernel.execute(
            chat_id=second, run_id="peer-kernel-reply",
            outer_tool_call_id="peer-kernel-reply-call",
            code=(
                "page = peers.inbox(direction='incoming')\n"
                "incoming = page['messages'][0]\n"
                "answer = incoming.reply(text='reply through CPython', "
                "request_id='cpython-reply')\n"
                "print(answer.metadata['request_id'])"
            ),
        )
        assert reply_result.ok, reply_result.to_dict()
        assert "cpython-reply" in reply_result.output.text()
        for chat_id, request_id, run_id in (
            (first, "cpython-send", "peer-kernel-send"),
            (second, "cpython-reply", "peer-kernel-reply"),
        ):
            row = service.repository.get_message_by_request(f"chat:{chat_id}", request_id)
            origin = row["evidence"]["sender_invocation"]
            assert origin["chat_id"] == chat_id
            assert origin["run_id"] == run_id
            assert origin["outer_tool_call_id"] == run_id + "-call"
            assert origin["cell_execution_id"] and origin["nested_call_id"]

        wait_result = await kernel.execute(
            chat_id=first, run_id="peer-kernel-wait",
            outer_tool_call_id="peer-kernel-wait-call",
            code=(
                "settled = await sent.wait.async_(timeout_s=1)\n"
                "print(settled['status'], settled['reply']['content'])"
            ),
        )
        assert wait_result.ok, wait_result.to_dict()
        assert "replied reply through CPython" in wait_result.output.text()

        mutual_a = kernel.execute(
            chat_id=first, run_id="peer-mutual-a",
            outer_tool_call_id="peer-mutual-a-call",
            code=(
                "other = next(p for p in peers.list() "
                f"if p.metadata['peer_id'] == 'chat:{second}')\n"
                "mutual = other.send(text='mutual A', request_id='mutual-cpython-a')\n"
                "result = await mutual.wait.async_(timeout_s=2)\n"
                "print('A-' + result['status'])"
            ),
        )
        mutual_b = kernel.execute(
            chat_id=second, run_id="peer-mutual-b",
            outer_tool_call_id="peer-mutual-b-call",
            code=(
                "other = next(p for p in peers.list() "
                f"if p.metadata['peer_id'] == 'chat:{first}')\n"
                "mutual = other.send(text='mutual B', request_id='mutual-cpython-b')\n"
                "result = await mutual.wait.async_(timeout_s=2)\n"
                "print('B-' + result['status'])"
            ),
        )
        result_a, result_b = await asyncio.wait_for(
            asyncio.gather(mutual_a, mutual_b), timeout=15,
        )
        assert result_a.ok, result_a.to_dict()
        assert result_b.ok, result_b.to_dict()
        assert "A-attention" in result_a.output.text()
        assert "B-attention" in result_b.output.text()
    finally:
        await service.shutdown()
        await kernel.shutdown()
        await runtimes.shutdown()
