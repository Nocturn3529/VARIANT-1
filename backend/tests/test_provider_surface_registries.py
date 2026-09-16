from messaging_gateway.base import MessageEnvelope
from messaging_gateway.catalog import BY_ID, PLATFORMS
from messaging_gateway.gateway import MessagingGateway
from speech import providers as speech_providers
from web_search import providers as web_providers


def test_web_registry_contains_full_hermes_provider_set():
    ids = {row["id"] for row in web_providers.provider_definitions()}
    assert ids == {
        "variant1", "ddgs", "brave-free", "exa", "firecrawl",
        "keenable", "parallel", "searxng", "tavily", "xai",
    }


def test_speech_registries_contain_full_provider_sets():
    assert {row["id"] for row in speech_providers.TTS_PROVIDERS} == {
        "kokoro", "edge", "elevenlabs", "openai", "minimax", "xai",
        "mistral", "gemini", "neutts", "kittentts", "piper", "deepinfra",
    }
    assert {row["id"] for row in speech_providers.STT_PROVIDERS} == {
        "local", "groq", "openai", "mistral", "xai", "elevenlabs", "deepinfra",
    }


def test_messaging_catalog_is_complete_and_secrets_are_typed():
    assert len(PLATFORMS) == 28
    for name in (
        "telegram", "discord", "slack", "whatsapp", "whatsapp_cloud",
        "signal", "bluebubbles", "email", "homeassistant", "mattermost",
        "matrix", "dingtalk", "feishu", "wecom", "wecom_callback",
        "weixin", "qqbot", "google_chat", "teams", "line", "irc", "ntfy",
        "sms", "simplex", "a2a", "buzz", "raft", "photon",
    ):
        assert name in BY_ID
    assert next(field for field in BY_ID["telegram"]["fields"]
                if field["key"] == "TELEGRAM_BOT_TOKEN")["secret"] is True


def test_pairing_approves_and_revokes_without_bypassing_channel_policy(tmp_path):
    gateway = MessagingGateway(str(tmp_path / "messaging.json"))
    gateway.update(enabled=True, adapter="telegram", values={
        "enabled": True, "allowed_conversations": ["room-1"],
    })
    envelope = MessageEnvelope(
        adapter="telegram", message_id="m1", conversation_id="room-1",
        user_id="user-1", user_name="Ada", text="hello",
    )
    gateway._record_pairing_request(envelope)
    request = gateway.pairing_state()["pending"][0]
    assert gateway.approve_pairing("telegram", request["request_id"]) is True
    assert gateway.authorized(envelope) == (True, "")
    wrong_room = MessageEnvelope(
        adapter="telegram", message_id="m2", conversation_id="room-2",
        user_id="user-1", text="hello",
    )
    assert gateway.authorized(wrong_room)[0] is False
    assert gateway.revoke_pairing("telegram", "user-1") is True


def test_public_platform_state_never_exposes_secret_values(tmp_path):
    gateway = MessagingGateway(
        str(tmp_path / "messaging.json"),
        credential_fields_getter=lambda name: (
            {"TELEGRAM_BOT_TOKEN": "plain-secret"} if name == "telegram" else {}
        ),
    )
    state = gateway.public_state()
    assert "plain-secret" not in repr(state)
    telegram = next(row for row in state["adapters"] if row["id"] == "telegram")
    assert telegram["configured_fields"] == ["TELEGRAM_BOT_TOKEN"]
