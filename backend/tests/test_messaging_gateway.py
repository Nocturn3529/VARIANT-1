import asyncio
from types import SimpleNamespace

import pytest

from tests.support.conversation_sessions import open_sessions
from messaging_gateway import GatewayAdapter, MessageEnvelope, MessagingGateway
from messaging_gateway import MessagingIngressOutcomeUnknown
import host_gateway
from messaging_gateway.media import stage_attachment


class FakeAdapter(GatewayAdapter):
    name = "fake"
    display_name = "Fake"

    def __init__(self, gateway):
        super().__init__(gateway)
        self.sent = []

    async def start(self):
        self.connected = True

    async def stop(self):
        self.connected = False

    async def send_text(self, envelope, text):
        self.sent.append((envelope.conversation_id, text))


def _envelope(message_id="1", user="7", conversation="9", text="hello"):
    return MessageEnvelope(
        adapter="fake", message_id=message_id, conversation_id=conversation,
        user_id=user, text=text)


@pytest.mark.asyncio
async def test_gateway_is_deny_by_default_and_deduplicates(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    calls = []

    async def route(envelope, text):
        calls.append(text)
        return f"reply:{text}"

    gateway.set_router(route)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True})
    await gateway.receive(_envelope())
    assert calls == []
    assert gateway.public_state()["stats"]["rejected"] == 1

    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"], "prefix": "!m"})
    await gateway.receive(_envelope(message_id="2", text="!m hello"))
    await gateway.receive(_envelope(message_id="2", text="!m hello"))
    assert calls == ["hello"]
    assert adapter.sent == [("9", "reply:hello")]
    assert gateway.public_state()["stats"]["duplicates"] == 1


@pytest.mark.asyncio
async def test_message_ids_are_deduplicated_within_each_conversation(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    calls = []

    async def route(envelope, text):
        calls.append((envelope.conversation_id, text))
        return text

    gateway.set_router(route)
    await gateway.receive(_envelope(message_id="42", conversation="a"))
    await gateway.receive(_envelope(message_id="42", conversation="b"))
    await gateway.receive(_envelope(message_id="42", conversation="a"))

    assert calls == [("a", "hello"), ("b", "hello")]
    assert gateway.public_state()["stats"]["duplicates"] == 1


@pytest.mark.asyncio
async def test_each_conversation_is_serialized_but_routes_are_independent(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    active = 0
    peak = 0
    independent_entered = set()
    both_independent = asyncio.Event()

    async def route(envelope, text):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if envelope.conversation_id in {"a", "b"}:
            independent_entered.add(envelope.conversation_id)
            if len(independent_entered) == 2:
                both_independent.set()
            await asyncio.wait_for(both_independent.wait(), timeout=2)
        else:
            await asyncio.sleep(0.01)
        active -= 1
        return text

    gateway.set_router(route)
    await asyncio.gather(
        gateway.receive(_envelope(message_id="1", conversation="same")),
        gateway.receive(_envelope(message_id="2", conversation="same")),
    )
    assert peak == 1

    await asyncio.gather(
        gateway.receive(_envelope(message_id="3", conversation="a")),
        gateway.receive(_envelope(message_id="4", conversation="b")),
    )
    assert peak == 2


@pytest.mark.asyncio
async def test_telegram_offset_advances_only_after_gateway_acceptance(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    telegram = gateway.adapters["telegram"]
    gateway.update(enabled=True)
    gateway.update(adapter="telegram", values={"enabled": True, "allowed_users": ["7"]})
    attempts = 0

    async def no_transport(_envelope):
        return None

    async def no_reply(_envelope, _text):
        return None

    telegram.send_typing = no_transport
    telegram.send_text = no_reply

    async def route(_envelope, _text):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("route unavailable")
        return "accepted"

    gateway.set_router(route)
    update = {
        "update_id": 42,
        "message": {
            "message_id": 9,
            "text": "hello",
            "chat": {"id": 11, "type": "private"},
            "from": {"id": 7, "username": "tester"},
        },
    }

    with pytest.raises(RuntimeError, match="not accepted"):
        await telegram._accept_update(update)
    assert telegram.offset == 0

    await telegram._accept_update(update)
    assert telegram.offset == 43
    assert attempts == 2


@pytest.mark.asyncio
async def test_reply_delivery_failure_does_not_reopen_committed_inbound_route(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    routes = []

    async def route(_envelope, text):
        routes.append(text)
        return "durable reply"

    async def fail_reply(_envelope, _text):
        raise OSError("transport offline")

    gateway.set_router(route)
    adapter.send_text = fail_reply
    envelope = _envelope(message_id="committed")

    assert await gateway.receive(envelope) is True
    assert await gateway.receive(envelope) is True
    assert routes == ["hello"]
    assert gateway.public_state()["stats"]["duplicates"] == 1


@pytest.mark.asyncio
async def test_live_gateway_reconcile_starts_stops_and_rechecks_admission(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    calls = []

    async def route(_envelope, text):
        calls.append(text)
        return "ok"

    gateway.set_router(route)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    await gateway.reconcile()
    assert adapter.connected is True

    gateway.update(adapter="fake", values={"enabled": False})
    await gateway.reconcile()
    assert adapter.connected is False
    assert await gateway.receive(_envelope(message_id="disabled")) is True
    assert calls == []


def test_gateway_chat_session_does_not_steal_deck_active_session(tmp_path):
    store = open_sessions(tmp_path / "chat")
    deck = store.create_session("Deck")
    remote = store.create_session("Telegram", make_active=False)
    assert remote != deck
    assert store.get_active() == deck


def test_only_owned_adapters_require_gateway_credentials(tmp_path):
    gateway = MessagingGateway(
        str(tmp_path / "messaging.json"),
        credential_status_getter=lambda name: name == "telegram",
    )
    gateway.register(FakeAdapter(gateway))

    state = {row["name"]: row for row in gateway.public_state()["adapters"]}

    assert state["discord"]["credential_required"] is True
    assert state["discord"]["credential_configured"] is False
    assert state["telegram"]["credential_required"] is True
    assert state["telegram"]["credential_configured"] is True
    assert state["fake"]["credential_required"] is False
    assert state["fake"]["credential_configured"] is True


@pytest.mark.asyncio
async def test_durable_dedupe_survives_gateway_restart(tmp_path):
    path = str(tmp_path / "messaging.json")
    first = MessagingGateway(path)
    first_adapter = FakeAdapter(first)
    first.register(first_adapter)
    first.update(enabled=True)
    first.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    first_calls = []

    async def first_route(_envelope, text):
        first_calls.append(text)
        return "committed"

    first.set_router(first_route)
    envelope = _envelope(message_id="restart-proof")
    assert await first.receive(envelope) is True

    reopened = MessagingGateway(path)
    reopened_adapter = FakeAdapter(reopened)
    reopened.register(reopened_adapter)
    reopened_calls = []

    async def reopened_route(_envelope, text):
        reopened_calls.append(text)
        return "duplicate"

    reopened.set_router(reopened_route)
    assert await reopened.receive(envelope) is True
    assert first_calls == ["hello"]
    assert reopened_calls == []
    assert reopened.public_state()["stats"]["duplicates"] == 1


@pytest.mark.asyncio
async def test_message_identity_rejects_changed_payload(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    gateway.register(FakeAdapter(gateway))
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    calls = []

    async def route(_envelope, text):
        calls.append(text)
        return "ok"

    gateway.set_router(route)
    assert await gateway.receive(_envelope(message_id="same", text="first")) is True
    assert await gateway.receive(_envelope(message_id="same", text="changed")) is True
    assert calls == ["first"]
    assert gateway.public_state()["stats"]["rejected"] == 1


def test_telegram_cursor_reopens_from_durable_gateway_state(tmp_path):
    path = str(tmp_path / "messaging.json")
    gateway = MessagingGateway(path)
    assert gateway.ingress.set_adapter_cursor("telegram", 413) == 413

    reopened = MessagingGateway(path)
    assert reopened.adapters["telegram"].offset == 413


@pytest.mark.asyncio
async def test_gateway_close_stops_ingress_then_drains_owned_routes(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    gateway.register(FakeAdapter(gateway))
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    started = asyncio.Event()
    release = asyncio.Event()

    async def route(_envelope, _text):
        started.set()
        await release.wait()
        return "done"

    gateway.set_router(route)
    routed = gateway.submit(_envelope(message_id="drain"))
    await started.wait()
    stopping = asyncio.create_task(gateway.stop())
    await asyncio.sleep(0)
    assert stopping.done() is False
    assert await gateway.receive(_envelope(message_id="late")) is False
    release.set()
    await stopping
    assert await routed is True


@pytest.mark.asyncio
async def test_gateway_chat_recovery_uses_ticket_proof_without_rerunning():
    envelope = _envelope(message_id="proof")
    sessions = SimpleNamespace(
        has_session=lambda _sid: True,
        has_message_ticket=lambda _sid, ticket: ticket == envelope.ticket_id,
        reply_for_message_ticket=lambda _sid, _ticket: "durable reply",
    )
    host = SimpleNamespace(
        gateway=SimpleNamespace(session_id=lambda _route: "chat-remote"),
        require_runtime=lambda: SimpleNamespace(
            sessions=sessions
        ),
    )

    reply = await host_gateway.gateway_route(host, envelope, "hello")
    assert reply == "durable reply"

    sessions.has_message_ticket = lambda _sid, _ticket: False
    recovery = MessageEnvelope(
        **{**envelope.__dict__, "metadata": {"_variant1_ingress_reconcile_only": True}}
    )
    with pytest.raises(MessagingIngressOutcomeUnknown):
        await host_gateway.gateway_route(host, recovery, "hello")


@pytest.mark.asyncio
async def test_extension_messaging_contribution_uses_out_of_process_adapter_host(
    tmp_path,
):
    contribution = {
        "package_id": "com.example.messaging",
        "package_digest": "a" * 64,
        "kind": "messaging_adapters",
        "id": "matrix",
        "descriptor_digest": "b" * 64,
        "descriptor": {
            "adapter_name": "matrix",
            "display_name": "Matrix",
            "handler": "adapter:dispatch",
            "effect_class": "external_effect",
            "poll_interval_s": 2,
        },
    }
    packages = SimpleNamespace(
        resolved_contributions=lambda **kwargs: (
            [contribution] if kwargs.get("kind") == "messaging_adapters" else []
        )
    )
    runtime = SimpleNamespace(packages=packages, workers=SimpleNamespace())
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    gateway.bind_extension_runtime(runtime)

    await gateway.reconcile()

    adapter = gateway.adapters["matrix"]
    assert adapter.status()["extension"] is True
    assert adapter.status()["package_id"] == "com.example.messaging"
    assert adapter.task is None


@pytest.mark.asyncio
async def test_router_failure_returns_ingress_to_admitted_for_safe_retry(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    gateway.register(FakeAdapter(gateway))
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})

    async def unavailable(_envelope, _text):
        raise RuntimeError("temporarily unavailable")

    gateway.set_router(unavailable)
    envelope = _envelope(message_id="retryable")
    assert await gateway.receive(envelope) is False
    duplicate = gateway.ingress.admit(envelope, "hello")
    assert duplicate.status == "admitted"
    assert duplicate.terminal is False


@pytest.mark.asyncio
async def test_telegram_caption_and_staged_media_reach_gateway_before_cursor(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    telegram = gateway.adapters["telegram"]
    gateway.update(enabled=True)
    gateway.update(adapter="telegram", values={"enabled": True, "allowed_users": ["7"]})
    staged = stage_attachment(
        str(tmp_path / "attachments"), adapter="telegram",
        conversation_id="11", message_id="9", name="note.txt",
        media_type="text/plain", payload=b"attached body", source_id="file-1",
    )
    seen = []

    async def attachments(_message):
        return [staged], []

    async def route(envelope, text):
        seen.append((envelope, text))
        return "ok"

    async def no_transport(*_args):
        return None

    telegram._message_attachments = attachments
    telegram.send_typing = no_transport
    telegram.send_text = no_transport
    gateway.set_router(route)
    await telegram._accept_update({
        "update_id": 42,
        "message": {
            "message_id": 9,
            "caption": "inspect this",
            "document": {"file_id": "file-1"},
            "chat": {"id": 11, "type": "private"},
            "from": {"id": 7},
        },
    })
    assert telegram.offset == 43
    assert seen[0][1] == "inspect this"
    assert seen[0][0].attachments[0]["path"] == staged["path"]


@pytest.mark.asyncio
async def test_discord_mention_only_bypasses_dms_and_accepts_bot_replies(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    discord = gateway.adapters["discord"]
    discord.bot_user_id = "bot"
    gateway.update(adapter="discord", values={"mention_only": True})
    captured = []

    async def no_attachments(_message):
        return [], []

    discord._message_attachments = no_attachments
    async def admit(envelope):
        captured.append((envelope, True))
        return True

    gateway.admit = admit
    await discord._message({
        "id": "dm-1", "channel_id": "dm", "content": "hello",
        "author": {"id": "7"}, "mentions": [],
    })
    await discord._message({
        "id": "guild-1", "guild_id": "guild", "channel_id": "channel",
        "content": "follow up", "author": {"id": "7"}, "mentions": [],
        "referenced_message": {"author": {"id": "bot"}},
    })
    assert [row[0].message_id for row in captured] == ["dm-1", "guild-1"]
    assert all(row[1] is True for row in captured)


def test_catalog_fields_normalize_into_live_gateway_configuration(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    gateway.update(adapter="discord", values={"fields": {
        "DISCORD_ALLOWED_USERS": "7, 8\n9",
        "DISCORD_PREFIX": "!v1",
        "DISCORD_MENTION_ONLY": "true",
    }})
    gateway.update(adapter="telegram", values={"fields": {
        "TELEGRAM_POLL_TIMEOUT": "31",
    }})

    discord = gateway.adapter_config("discord")
    assert discord["allowed_users"] == ["7", "8", "9"]
    assert discord["prefix"] == "!v1"
    assert discord["mention_only"] is True
    assert "DISCORD_MENTION_ONLY" not in discord.get("fields", {})
    assert gateway.adapter_config("telegram")["poll_timeout"] == 31

    public = next(
        row for row in gateway.public_state()["adapters"]
        if row["id"] == "discord"
    )
    assert public["values"]["DISCORD_ALLOWED_USERS"] == "7, 8, 9"
    assert public["values"]["DISCORD_MENTION_ONLY"] is True


@pytest.mark.asyncio
async def test_pending_outbox_retries_without_rerunning_the_turn(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    calls = []

    async def route(_envelope, _text):
        calls.append("route")
        return "durable reply"

    async def fail(_envelope, _text):
        raise OSError("offline")

    gateway.set_router(route)
    adapter.send_text = fail
    envelope = _envelope(message_id="outbox")
    assert await gateway.receive(envelope) is True
    assert gateway.ingress.outbound_pending(envelope) == "durable reply"

    delivered = []

    async def succeed(_envelope, text):
        delivered.append(text)

    adapter.send_text = succeed
    assert await gateway.receive(envelope) is True
    assert calls == ["route"]
    assert delivered == ["durable reply"]
    assert gateway.ingress.outbound_pending(envelope) == ""


def test_discord_resume_identity_is_durable(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    gateway.ingress.set_adapter_state("discord", "session_id", "session-a")
    gateway.ingress.set_adapter_state("discord", "sequence", 123)
    reopened = MessagingGateway(str(tmp_path / "messaging.json"))
    discord = reopened.adapters["discord"]
    assert discord.session_id == "session-a"
    assert discord.sequence == 123


@pytest.mark.asyncio
async def test_periodic_reconcile_does_not_reclassify_live_routing(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    adapter.connected = True
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    envelope = _envelope(message_id="live-routing")
    gateway.ingress.admit(envelope, "hello")
    gateway.ingress.mark_routing(envelope)
    await gateway.reconcile()
    assert gateway.ingress.admit(envelope, "hello").status == "routing"


@pytest.mark.asyncio
async def test_recovery_drains_multiple_pages_once_without_adapter_restart(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    envelopes = [_envelope(message_id=str(i), conversation=str(i)) for i in range(211)]
    for envelope in envelopes:
        gateway.ingress.admit(envelope, "hello")
    calls, active, peak = [], 0, 0
    finished = asyncio.Event()
    async def route(envelope, text):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(.001)
        calls.append(envelope.message_id)
        active -= 1
        if len(calls) == len(envelopes):
            finished.set()
        return ""
    gateway.set_router(route)
    await gateway.start()
    try:
        await asyncio.wait_for(finished.wait(), 15)
        await gateway.drain()
        assert len(calls) == len(set(calls)) == 211
        assert peak <= gateway._recovery_concurrency
        assert gateway.ingress.pending_ingress() == []
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_recovery_does_not_reset_a_live_route_or_submit_it_twice(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    adapter = FakeAdapter(gateway)
    gateway.register(adapter)
    gateway.update(enabled=True)
    gateway.update(adapter="fake", values={"enabled": True, "allowed_users": ["7"]})
    entered, release = asyncio.Event(), asyncio.Event()
    async def route(envelope, text):
        entered.set()
        await release.wait()
        return ""
    gateway.set_router(route)
    envelope = _envelope()
    task = gateway.submit(envelope, retry=True)
    await entered.wait()
    try:
        assert gateway.submit(envelope, retry=True) is task
        await gateway.start()
        await gateway._pump_ingress_once()
        assert gateway.ingress.admit(envelope, "hello").status == "routing"
        assert len(gateway._submitted) == 1
    finally:
        release.set()
        await task
        await gateway.stop()
