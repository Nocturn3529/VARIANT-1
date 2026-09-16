"""
VARIANT-1 secret store — Phase 1, Step 6 (spec 3.6, 9, 11.2)

API keys are NEVER stored in plaintext. On Windows they are encrypted with the
Data Protection API (DPAPI: CryptProtectData / CryptUnprotectData), scoped to
the current user, then base64-encoded and tagged 'dpapi:' for storage.

decrypt() also accepts a legacy 'plain:' token only as a non-Windows DEV escape
hatch; encrypt() never produces plaintext on Windows.
"""

import base64
import binascii
import ctypes
import sys

IS_WIN = sys.platform.startswith("win")


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


def encrypt(plaintext: str) -> str:
    """Return a 'dpapi:<base64>' token. Windows only."""
    if not plaintext:
        return ""
    if not IS_WIN:
        raise SecretStoreError("DPAPI is only available on Windows")
    blob = _dpapi_protect(plaintext.encode("utf-8"))
    return "dpapi:" + base64.b64encode(blob).decode("ascii")


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
    if token.startswith("plain:"):
        if IS_WIN:
            raise SecretStoreError("Plaintext secret tokens are disabled on Windows")
        try:
            return base64.b64decode(
                token[len("plain:"):], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise SecretStoreError("Invalid plaintext development token") from exc
    raise SecretStoreError("Unrecognized secret token format")


def is_available() -> bool:
    return IS_WIN
