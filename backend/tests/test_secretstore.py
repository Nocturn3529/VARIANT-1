import base64
from pathlib import Path

import pytest

from security import secretstore


def test_empty_secret_remains_empty():
    assert secretstore.decrypt("") == ""
    assert secretstore.encrypt("") == ""


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


def test_encrypt_uses_dpapi_on_windows(monkeypatch):
    monkeypatch.setattr(secretstore, "IS_WIN", True)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    monkeypatch.setattr(
        secretstore, "_dpapi_protect", lambda data: b"wrapped:" + data,
        raising=False,
    )
    token = secretstore.encrypt("super-secret")
    assert token.startswith("dpapi:")
    blob = base64.b64decode(token[len("dpapi:"):], validate=True)
    assert blob == b"wrapped:super-secret"


def test_fernet_roundtrip_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    key_path = tmp_path / "secretstore.key"
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(key_path))
    token = secretstore.encrypt("unix-secret")
    assert token.startswith("fernet:")
    assert secretstore.decrypt(token) == "unix-secret"
    assert key_path.is_file()
    # Second encrypt reuses the same keyfile
    token2 = secretstore.encrypt("another")
    assert secretstore.decrypt(token2) == "another"


def test_fernet_save_read_remove_style_flow(monkeypatch, tmp_path):
    """Mirrors credentials save/read/remove against the same API."""
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(tmp_path / "k.key"))
    stored = secretstore.encrypt("provider-api-key")
    assert secretstore.decrypt(stored) == "provider-api-key"
    # "remove" is caller-side; store must still decrypt unrelated tokens
    other = secretstore.encrypt("other-key")
    assert secretstore.decrypt(other) == "other-key"


def test_unavailable_store_rejects_encrypt(monkeypatch):
    monkeypatch.setenv("VARIANT1_SECRETSTORE_DISABLED", "1")
    with pytest.raises(secretstore.SecretStoreError, match="disabled"):
        secretstore.encrypt("nope")
    assert secretstore.is_available() is False


def test_is_available_true_when_fernet_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(tmp_path / "avail.key"))
    assert secretstore.is_available() is True


def test_encrypt_never_emits_plain_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(tmp_path / "plaincheck.key"))
    token = secretstore.encrypt("x")
    assert not token.startswith("plain:")


def test_fernet_token_rejected_on_windows(monkeypatch):
    monkeypatch.setattr(secretstore, "IS_WIN", True)
    with pytest.raises(secretstore.SecretStoreError, match="on Windows"):
        secretstore.decrypt("fernet:gAAAAABnot-a-real-token")
