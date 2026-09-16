"""
VARIANT-1 secret store — Phase 1, Step 6 (spec 3.6, 9, 11.2)

API keys are NEVER stored in plaintext for production paths.

Windows
  encrypt/decrypt use DPAPI (CryptProtectData / CryptUnprotectData), scoped to
  the current user. Tokens are tagged ``dpapi:`` + base64.

Unix / macOS
  encrypt/decrypt use Fernet (cryptography) with a per-user key file at
  ``~/.variant1/secretstore.key`` (mode 0600), or ``VARIANT1_SECRETSTORE_KEY``.
  Tokens are tagged ``fernet:`` + Fernet token (url-safe base64).
  Set ``VARIANT1_SECRETSTORE_DISABLED=1`` to force an unavailable store
  (encrypt raises; is_available is False).

Legacy
  decrypt() still accepts ``plain:`` off Windows as a DEV escape hatch.
  encrypt() never produces ``plain:`` on any platform.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import os
import stat
import sys
from pathlib import Path

IS_WIN = sys.platform.startswith("win")

_ENV_DISABLED = "VARIANT1_SECRETSTORE_DISABLED"
_ENV_KEY_PATH = "VARIANT1_SECRETSTORE_KEY"
_DEFAULT_KEY_REL = Path(".variant1") / "secretstore.key"


class SecretStoreError(RuntimeError):
    pass


if IS_WIN:
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _to_blob(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(data, len(data))
        return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _from_blob(blob: "_DATA_BLOB") -> bytes:
        out = ctypes.string_at(blob.pbData, blob.cbData)
        ctypes.windll.kernel32.LocalFree(blob.pbData)
        return out

    def _dpapi_protect(data: bytes) -> bytes:
        blob_in = _to_blob(data)
        blob_out = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), "variant1", None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            raise SecretStoreError("CryptProtectData failed")
        return _from_blob(blob_out)

    def _dpapi_unprotect(data: bytes) -> bytes:
        blob_in = _to_blob(data)
        blob_out = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            raise SecretStoreError("CryptUnprotectData failed")
        return _from_blob(blob_out)


def _disabled() -> bool:
    return os.environ.get(_ENV_DISABLED, "").strip().lower() in {"1", "true", "yes", "on"}


def _key_path() -> Path:
    override = os.environ.get(_ENV_KEY_PATH, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / _DEFAULT_KEY_REL


def _load_or_create_fernet():
    """Return a Fernet instance, creating a 0600 keyfile when missing."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SecretStoreError(
            "Unix secret store requires the cryptography package"
        ) from exc

    if _disabled():
        raise SecretStoreError("Secret store is disabled (VARIANT1_SECRETSTORE_DISABLED)")

    path = _key_path()
    try:
        if path.is_file():
            key = path.read_bytes().strip()
            return Fernet(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # Write then tighten mode (Windows ignores mode; Unix needs 0600).
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return Fernet(key)
    except SecretStoreError:
        raise
    except OSError as exc:
        raise SecretStoreError(f"Cannot access secret-store key at {path}: {exc}") from exc
    except (ValueError, TypeError) as exc:
        raise SecretStoreError(f"Invalid secret-store key at {path}") from exc


def encrypt(plaintext: str) -> str:
    """Return a platform token: ``dpapi:`` on Windows, ``fernet:`` elsewhere."""
    if not plaintext:
        return ""
    if _disabled():
        raise SecretStoreError("Secret store is disabled (VARIANT1_SECRETSTORE_DISABLED)")
    if IS_WIN:
        blob = _dpapi_protect(plaintext.encode("utf-8"))
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    fernet = _load_or_create_fernet()
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return "fernet:" + token


def decrypt(token: str) -> str:
    if not token:
        return ""
    if token.startswith("dpapi:"):
        if not IS_WIN:
            raise SecretStoreError("Cannot decrypt a DPAPI token off Windows")
        try:
            blob = base64.b64decode(token[len("dpapi:"):], validate=True)
            return _dpapi_unprotect(blob).decode("utf-8")
        except SecretStoreError:
            raise
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise SecretStoreError("Invalid DPAPI secret token") from exc
    if token.startswith("fernet:"):
        if IS_WIN:
            raise SecretStoreError("Cannot decrypt a Fernet token on Windows")
        try:
            from cryptography.fernet import InvalidToken
        except ImportError as exc:
            raise SecretStoreError(
                "Unix secret store requires the cryptography package"
            ) from exc
        try:
            fernet = _load_or_create_fernet()
            raw = token[len("fernet:"):].encode("ascii")
            return fernet.decrypt(raw).decode("utf-8")
        except SecretStoreError:
            raise
        except InvalidToken as exc:
            raise SecretStoreError("Invalid Fernet secret token") from exc
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise SecretStoreError("Invalid Fernet secret token") from exc
    if token.startswith("plain:"):
        if IS_WIN:
            raise SecretStoreError("Plaintext secret tokens are disabled on Windows")
        try:
            return base64.b64decode(
                token[len("plain:"):], validate=True
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise SecretStoreError("Invalid plaintext development token") from exc
    raise SecretStoreError("Unrecognized secret token format")


def is_available() -> bool:
    """True when encrypt() can succeed on this host."""
    if _disabled():
        return False
    if IS_WIN:
        return True
    try:
        _load_or_create_fernet()
        return True
    except SecretStoreError:
        return False
