from __future__ import annotations

import json

from messaging_gateway import credentials as messaging_credentials


def _fake_dpapi(monkeypatch):
    monkeypatch.setattr(
        messaging_credentials.secretstore,
        "encrypt",
        lambda value: f"dpapi:fixture:{value}",
    )
    monkeypatch.setattr(
        messaging_credentials.secretstore,
        "decrypt",
        lambda value: value.removeprefix("dpapi:fixture:"),
    )


def test_store_accepts_only_gateway_bot_tokens(tmp_path, monkeypatch):
    _fake_dpapi(monkeypatch)
    path = tmp_path / "messaging_credentials.json"
    store = messaging_credentials.MessagingCredentialStore(str(path))

    store.set_token("discord", "discord-secret")

    assert store.configured("discord") is True
    assert store.supports("discord") is True
    assert store.supports("custom-plugin") is False
    assert store.token("discord") == "discord-secret"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["values"]["discord"]["DISCORD_BOT_TOKEN"] == "dpapi:fixture:discord-secret"
    try:
        store.set_token("gmail", "not-supported")
    except ValueError as exc:
        assert "unsupported messaging adapter" in str(exc)
    else:
        raise AssertionError("non-gateway credentials must be rejected")


def test_corrupt_token_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "messaging_credentials.json"
    path.write_text(json.dumps({"tokens": {"discord": "broken"}}), encoding="utf-8")
    monkeypatch.setattr(
        messaging_credentials.secretstore,
        "decrypt",
        lambda _value: (_ for _ in ()).throw(ValueError("invalid ciphertext")),
    )

    store = messaging_credentials.MessagingCredentialStore(str(path))

    assert store.configured("discord") is True
    assert store.token("discord") == ""


def test_legacy_connector_auth_migrates_only_bot_tokens(tmp_path, monkeypatch):
    _fake_dpapi(monkeypatch)
    legacy = tmp_path / "connector_auth.json"
    legacy.write_text(json.dumps({
        "discord": {"type": "api_token", "token": "dpapi:fixture:discord"},
        "telegram": {"type": "api_token", "token": "dpapi:fixture:telegram"},
        "gmail": {"type": "oauth2", "refresh_token": "dpapi:fixture:gmail"},
    }), encoding="utf-8")
    current = tmp_path / "messaging_credentials.json"

    store = messaging_credentials.MessagingCredentialStore(
        str(current), legacy_connector_path=str(legacy))

    assert store.token("discord") == "discord"
    assert store.token("telegram") == "telegram"
    assert legacy.exists() is False
    payload = json.loads(current.read_text(encoding="utf-8"))
    assert set(payload["values"]) == {"discord", "telegram"}
