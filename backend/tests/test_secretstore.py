import base64

import pytest

from security import secretstore


def test_empty_secret_remains_empty():
    assert secretstore.decrypt("") == ""


def test_unknown_secret_format_fails_closed():
    with pytest.raises(secretstore.SecretStoreError, match="Unrecognized"):
        secretstore.decrypt("legacy-plaintext-api-key")


def test_plain_development_token_is_rejected_on_windows(monkeypatch):
    monkeypatch.setattr(secretstore, "IS_WIN", True)
    token = "plain:" + base64.b64encode(b"dev-key").decode("ascii")
    with pytest.raises(secretstore.SecretStoreError, match="disabled on Windows"):
        secretstore.decrypt(token)


def test_plain_development_token_is_accepted_off_windows(monkeypatch):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    token = "plain:" + base64.b64encode(b"dev-key").decode("ascii")
    assert secretstore.decrypt(token) == "dev-key"


def test_malformed_dpapi_payload_fails_loudly(monkeypatch):
    monkeypatch.setattr(secretstore, "IS_WIN", True)
    with pytest.raises(secretstore.SecretStoreError, match="Invalid DPAPI"):
        secretstore.decrypt("dpapi:not-valid-base64!!!")


def test_valid_dpapi_payload_is_unprotected(monkeypatch):
    monkeypatch.setattr(secretstore, "IS_WIN", True)
    monkeypatch.setattr(
        secretstore, "_dpapi_unprotect", lambda blob: b"key:" + blob,
        raising=False,
    )
    token = "dpapi:" + base64.b64encode(b"ciphertext").decode("ascii")
    assert secretstore.decrypt(token) == "key:ciphertext"
