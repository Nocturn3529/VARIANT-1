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
  Fernet tokens. The implementation therefore validates the *opened* key
  descriptor (regular file, current-user owner, mode 0600) and a trusted
  parent-directory chain before use. Prefer an OS-backed store when one is
  available in a later phase.

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
import errno
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


def _supports_dir_fd_open() -> bool:
    return hasattr(os, "supports_dir_fd") and os.open in os.supports_dir_fd


def _is_symlink_open_error(exc: OSError) -> bool:
    if getattr(exc, "errno", None) in {errno.ELOOP, errno.ENOTDIR}:
        return True
    text = str(exc).lower()
    return "symbolic link" in text or "symlink" in text


def _validate_key_stat(st: os.stat_result, path: Path, *, via_fd: bool) -> None:
    """Reject non-regular files, wrong owner, and non-private modes."""
    if not via_fd and stat.S_ISLNK(st.st_mode):
        raise SecretStoreError(f"Secret-store key must not be a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise SecretStoreError(f"Secret-store key must be a regular file: {path}")
    if not _UNIX_FS_HARDENING:
        return
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise SecretStoreError(
            f"Secret-store key permissions must be 0600 (found {mode:04o}): {path}"
        )
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise SecretStoreError(
            f"Secret-store key must be owned by the current user: {path}"
        )


def _validate_trusted_dir_stat(st: os.stat_result, path: Path) -> None:
    """Require a user-or-root-owned directory that others cannot write."""
    if stat.S_ISLNK(st.st_mode):
        raise SecretStoreError(
            f"Secret-store parent must not be a symlink: {path}"
        )
    if not stat.S_ISDIR(st.st_mode):
        raise SecretStoreError(f"Secret-store parent must be a directory: {path}")
    if not _UNIX_FS_HARDENING:
        return
    uid = os.getuid()
    if st.st_uid not in (uid, 0):
        raise SecretStoreError(
            f"Secret-store parent must be owned by the current user or root: {path}"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o022:
        sticky = bool(st.st_mode & stat.S_ISVTX)
        if not (sticky and st.st_uid == 0):
            raise SecretStoreError(
                f"Secret-store parent is writable by others (mode {mode:04o}): {path}"
            )


def _validate_trusted_parents(path: Path) -> None:
    """Validate the real parent-directory chain of the configured key path.

    Directory symlinks are resolved; the directories actually used must be
    trusted. A symlink into a world-writable directory is therefore rejected.
    """
    if not _UNIX_FS_HARDENING:
        return
    try:
        real_parent = Path(os.path.realpath(path.parent))
    except OSError as exc:
        raise SecretStoreError(
            f"Cannot resolve secret-store parent for {path}: {exc}"
        ) from exc
    current = real_parent
    while True:
        try:
            st = os.lstat(current)
        except OSError as exc:
            raise SecretStoreError(
                f"Cannot access secret-store parent {current}: {exc}"
            ) from exc
        _validate_trusted_dir_stat(st, Path(current))
        parent = Path(current).parent
        if parent == current:
            break
        current = parent


def _validate_existing_key_path(path: Path) -> None:
    """Reject symlinks, non-regular files, wrong owner, and non-private modes."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise SecretStoreError(f"Cannot access secret-store key at {path}: {exc}") from exc
    _validate_key_stat(st, path, via_fd=False)


def _validate_opened_key_fd(fd: int, path: Path) -> None:
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise SecretStoreError(
            f"Cannot inspect opened secret-store key at {path}: {exc}"
        ) from exc
    _validate_key_stat(st, path, via_fd=True)


def _open_key_fd(path: Path, flags: int, mode: int | None = None) -> int:
    """Open the key through a trusted parent when dir_fd is available."""
    _validate_trusted_parents(path)
    if _UNIX_FS_HARDENING and _supports_dir_fd_open():
        real_parent = Path(os.path.realpath(path.parent))
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            dir_flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        try:
            dir_fd = os.open(real_parent, dir_flags)
        except OSError as exc:
            raise SecretStoreError(
                f"Cannot open secret-store parent {real_parent}: {exc}"
            ) from exc
        try:
            _validate_trusted_dir_stat(os.fstat(dir_fd), real_parent)
            name = path.name
            if not name or name in {os.curdir, os.pardir}:
                raise SecretStoreError(f"Secret-store key path is invalid: {path}")
            if mode is None:
                return os.open(name, flags, dir_fd=dir_fd)
            return os.open(name, flags, mode, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    if mode is None:
        return os.open(path, flags)
    return os.open(path, flags, mode)


def _read_validated_key(path: Path) -> bytes:
    _validate_existing_key_path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = _open_key_fd(path, flags)
        try:
            _validate_opened_key_fd(fd, path)
            key = os.read(fd, 4096).strip()
        finally:
            os.close(fd)
    except SecretStoreError:
        raise
    except OSError as exc:
        if _is_symlink_open_error(exc):
            raise SecretStoreError(
                f"Secret-store key must not be a symlink: {path}"
            ) from exc
        raise SecretStoreError(f"Cannot read secret-store key at {path}: {exc}") from exc
    if not key:
        raise SecretStoreError(f"Secret-store key is empty: {path}")
    return key


def _create_keyfile(path: Path, key: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = _open_key_fd(path, flags, _PRIVATE_MODE)
        try:
            os.write(fd, key)
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            _validate_opened_key_fd(fd, path)
        finally:
            os.close(fd)
    except FileExistsError as exc:
        raise SecretStoreError(
            f"Secret-store key already exists (concurrent init?): {path}"
        ) from exc
    except SecretStoreError:
        raise
    except OSError as exc:
        if _is_symlink_open_error(exc):
            raise SecretStoreError(
                f"Secret-store key must not be a symlink: {path}"
            ) from exc
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
    try:
        _create_keyfile(path, key)
        return Fernet(key)
    except SecretStoreError as exc:
        msg = str(exc).lower()
        if "already exists" in msg or "concurrent" in msg:
            try:
                return Fernet(_read_validated_key(path))
            except (ValueError, TypeError) as invalid:
                raise SecretStoreError(f"Invalid secret-store key at {path}") from invalid
        raise


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
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError:
                return False
        try:
            _validate_trusted_parents(path)
        except SecretStoreError:
            return False
        return os.access(parent, os.W_OK)
    except SecretStoreError:
        return False
    except OSError:
        return False
