import base64
import os
import stat
import sys
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
    if not sys.platform.startswith("win"):
        mode = stat.S_IMODE(key_path.stat().st_mode)
        assert mode == 0o600
    token2 = secretstore.encrypt("another")
    assert secretstore.decrypt(token2) == "another"


def test_fernet_save_read_remove_style_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(tmp_path / "k.key"))
    stored = secretstore.encrypt("provider-api-key")
    assert secretstore.decrypt(stored) == "provider-api-key"
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
    assert not (tmp_path / "avail.key").exists()


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


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Unix keyfile mode checks")
def test_rejects_world_readable_existing_key(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    key_path = tmp_path / "loose.key"
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(key_path))
    token = secretstore.encrypt("before")
    os.chmod(key_path, 0o644)
    with pytest.raises(secretstore.SecretStoreError, match="0600"):
        secretstore.encrypt("after")
    with pytest.raises(secretstore.SecretStoreError, match="0600"):
        secretstore.decrypt(token)
    assert secretstore.is_available() is False
    assert key_path.is_file()
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o644


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Unix symlink checks")
def test_rejects_symlink_key(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    real = tmp_path / "real.key"
    link = tmp_path / "link.key"
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(real))
    secretstore.encrypt("seed")
    os.chmod(real, 0o644)  # even if mode ok on target, link itself is rejected
    os.chmod(real, 0o600)
    link.symlink_to(real)
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(link))
    with pytest.raises(secretstore.SecretStoreError, match="symlink"):
        secretstore.encrypt("nope")
    with pytest.raises(secretstore.SecretStoreError, match="symlink"):
        secretstore.decrypt("fernet:" + "x" * 20)


def test_rejects_empty_key_file(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    key_path = tmp_path / "empty.key"
    key_path.write_bytes(b"")
    if not sys.platform.startswith("win"):
        os.chmod(key_path, 0o600)
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(key_path))
    with pytest.raises(secretstore.SecretStoreError, match="empty|Invalid"):
        secretstore.encrypt("x")


def test_decrypt_missing_key_does_not_create_replacement(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    key_path = tmp_path / "gone.key"
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(key_path))
    token = secretstore.encrypt("keep-me")
    assert key_path.is_file()
    key_path.unlink()
    before = list(tmp_path.iterdir())
    with pytest.raises(secretstore.SecretStoreError, match="missing|restore"):
        secretstore.decrypt(token)
    after = list(tmp_path.iterdir())
    assert after == before
    assert not key_path.exists()


def test_decrypt_wrong_key_preserves_keyfile(monkeypatch, tmp_path):
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    key_a = tmp_path / "a.key"
    key_b = tmp_path / "b.key"
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(key_a))
    token = secretstore.encrypt("alpha")
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(key_b))
    secretstore.encrypt("beta")  # create other key
    key_b_bytes = key_b.read_bytes()
    with pytest.raises(secretstore.SecretStoreError, match="Invalid Fernet|missing"):
        secretstore.decrypt(token)
    assert key_b.read_bytes() == key_b_bytes
    assert key_a.is_file()


def test_test_isolation_env_uses_runtime_key_path(monkeypatch, tmp_path):
    """R2.c: when VARIANT1_SECRETSTORE_KEY is set under the test root, HOME is unused."""
    monkeypatch.setattr(secretstore, "IS_WIN", False)
    monkeypatch.delenv("VARIANT1_SECRETSTORE_DISABLED", raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    if sys.platform.startswith("win"):
        monkeypatch.setenv("USERPROFILE", str(fake_home))
    runtime_key = tmp_path / "runtime" / "secretstore.key"
    runtime_key.parent.mkdir()
    monkeypatch.setenv("VARIANT1_SECRETSTORE_KEY", str(runtime_key))
    secretstore.encrypt("isolated")
    assert runtime_key.is_file()
    default_under_home = fake_home / ".variant1" / "secretstore.key"
    assert not default_under_home.exists()
