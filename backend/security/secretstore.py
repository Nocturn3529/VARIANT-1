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

  Protection model: this is a deliberately supported *local file key*, not an
  OS keychain/keyring. Possession of the key file permits reading and forging
  Fernet tokens. The implementation therefore validates that an existing key
  is a regular file (not a symlink), owned by the current user, and mode 0600
  before use. Prefer an OS-backed store when one is available in a later phase.

  Initialization: ``encrypt()`` may create a new key when none exists.
  ``decrypt()`` never creates a key — a missing key raises a recovery error and
  leaves the filesystem unchanged (a replacement key cannot decrypt old tokens).

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
# Filesystem key hardening follows the real OS; tests may flip IS_WIN for API routing.
_UNIX_FS_HARDENING = not sys.platform.startswith("win")

_ENV_DISABLED = "VARIANT1_SECRETSTORE_DISABLED"
_ENV_KEY_PATH = "VARIANT1_SECRETSTORE_KEY"
_DEFAULT_KEY_REL = Path(".variant1") / "secretstore.key"
_PRIVATE_MODE = 0o600


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


def _import_fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SecretStoreError(
            "Unix secret store requires the cryptography package"
        ) from exc
    return Fernet


def _validate_existing_key_path(path: Path) -> None:
    """Reject symlinks, non-regular files, wrong owner, and non-private modes."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise SecretStoreError(f"Cannot access secret-store key at {path}: {exc}") from exc

    if stat.S_ISLNK(st.st_mode):
        raise SecretStoreError(
            f"Secret-store key must not be a symlink: {path}"
        )
    if not stat.S_ISREG(st.st_mode):
        raise SecretStoreError(
            f"Secret-store key must be a regular file: {path}"
        )

    # Permission/ownership hardening is Unix-specific; Windows uses DPAPI.
    if _UNIX_FS_HARDENING:
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            raise SecretStoreError(
                f"Secret-store key permissions must be 0600 (found {mode:04o}): {path}"
            )
        try:
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                raise SecretStoreError(
                    f"Secret-store key must be owned by the current user: {path}"
                )
        except AttributeError:
            pass


def _read_validated_key(path: Path) -> bytes:
    _validate_existing_key_path(path)
    try:
        # Open without following symlinks when O_NOFOLLOW is available.
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        try:
            key = os.read(fd, 4096).strip()
        finally:
            os.close(fd)
    except OSError as exc:
        raise SecretStoreError(f"Cannot read secret-store key at {path}: {exc}") from exc
    if not key:
        raise SecretStoreError(f"Secret-store key is empty: {path}")
    return key


def _create_keyfile(path: Path, key: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, _PRIVATE_MODE)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        _validate_existing_key_path(path)
    except FileExistsError as exc:
        raise SecretStoreError(
            f"Secret-store key already exists (concurrent init?): {path}"
        ) from exc
    except SecretStoreError:
        raise
    except OSError as exc:
        raise SecretStoreError(f"Cannot create secret-store key at {path}: {exc}") from exc


def _load_fernet(*, create: bool):
    """Load Fernet from the keyfile. Create only when ``create`` is True."""
    Fernet = _import_fernet()
    if _disabled():
        raise SecretStoreError("Secret store is disabled (VARIANT1_SECRETSTORE_DISABLED)")

    path = _key_path()
    try:
        os.lstat(path)
        exists = True
    except FileNotFoundError:
        exists = False
    except OSError as exc:
        raise SecretStoreError(f"Cannot access secret-store key at {path}: {exc}") from exc

    if exists:
        try:
            key = _read_validated_key(path)
            return Fernet(key)
        except (ValueError, TypeError) as exc:
            raise SecretStoreError(f"Invalid secret-store key at {path}") from exc

    if not create:
        raise SecretStoreError(
            "Secret-store key missing; restore the key file or re-save credentials "
            f"(path: {path})"
        )

    key = Fernet.generate_key()
    _create_keyfile(path, key)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Return a platform token: ``dpapi:`` on Windows, ``fernet:`` elsewhere."""
    if not plaintext:
        return ""
    if _disabled():
        raise SecretStoreError("Secret store is disabled (VARIANT1_SECRETSTORE_DISABLED)")
    if IS_WIN:
        blob = _dpapi_protect(plaintext.encode("utf-8"))
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    fernet = _load_fernet(create=True)
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
            fernet = _load_fernet(create=False)
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
    """True when encrypt() can succeed on this host without disabling."""
    if _disabled():
        return False
    if IS_WIN:
        return True
    path = _key_path()
    try:
        try:
            os.lstat(path)
            _load_fernet(create=False)
            return True
        except FileNotFoundError:
            pass
        # Missing key: available if parent is writable (do not create the key).
        parent = path.parent
        if not parent.is_dir():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
        return os.access(parent, os.W_OK)
    except SecretStoreError:
        return False
    except OSError:
        return False
