"""Encrypted, field-aware credentials for messaging platforms."""

from __future__ import annotations

import json
import os

from security import secretstore
from .catalog import BY_ID


SUPPORTED_ADAPTERS = frozenset(BY_ID)
_FORMAT_VERSION = 2


def _adapter_name(value: str) -> str:
    name = str(value or "").strip().lower().replace("-", "_")
    if name not in SUPPORTED_ADAPTERS:
        raise ValueError(f"unsupported messaging adapter: {name or '<empty>'}")
    return name


def _secret_fields(adapter: str) -> list[str]:
    definition = BY_ID.get(_adapter_name(adapter)) or {}
    return [str(item["key"]) for item in definition.get("fields") or ()
            if item.get("secret")]


def _primary_field(adapter: str) -> str:
    fields = _secret_fields(adapter)
    return fields[0] if fields else "token"


class MessagingCredentialStore:
    """Persist arbitrary catalog-declared secret fields using Windows DPAPI."""

    def __init__(self, path: str, *, legacy_connector_path: str = ""):
        self.path = path
        self.values: dict[str, dict[str, str]] = {}
        if not os.path.isfile(path) and legacy_connector_path:
            self._migrate_legacy(legacy_connector_path)
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            raw = payload.get("values") if isinstance(payload, dict) else None
            if not isinstance(raw, dict):
                # v1 stored one token per platform.
                tokens = payload.get("tokens") if isinstance(payload, dict) else {}
                raw = {_adapter_name(name): {_primary_field(name): value}
                       for name, value in (tokens or {}).items()
                       if name in SUPPORTED_ADAPTERS and isinstance(value, str) and value}
            self.values = {
                name: {str(field): value for field, value in fields.items()
                       if isinstance(value, str) and value}
                for name, fields in raw.items()
                if name in SUPPORTED_ADAPTERS and isinstance(fields, dict)
            }
        except FileNotFoundError:
            self.values = {}
        except Exception as exc:
            print(f"[messaging-credentials] load failed ({exc}); empty", flush=True)
            self.values = {}

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = self.path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"version": _FORMAT_VERSION, "values": self.values}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except Exception:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise

    def supports(self, adapter: str) -> bool:
        try:
            name = _adapter_name(adapter)
        except ValueError:
            return False
        return any(field.get("required") and field.get("secret")
                   for field in (BY_ID.get(name) or {}).get("fields") or ())

    def configured(self, adapter: str, field: str = "") -> bool:
        try:
            name = _adapter_name(adapter)
        except ValueError:
            return False
        key = str(field or _primary_field(name))
        return bool((self.values.get(name) or {}).get(key))

    def configured_fields(self, adapter: str) -> list[str]:
        try:
            name = _adapter_name(adapter)
        except ValueError:
            return []
        return sorted(field for field, value in (self.values.get(name) or {}).items() if value)

    def set(self, adapter: str, field: str, value: str) -> None:
        name = _adapter_name(adapter)
        key = str(field or "").strip()
        if key not in _secret_fields(name):
            raise ValueError(f"{key or '<empty>'} is not a secret field for {name}")
        secret = str(value or "").strip()
        if not secret:
            raise ValueError("credential must not be empty")
        self.values.setdefault(name, {})[key] = secretstore.encrypt(secret)
        self.save()

    def set_token(self, adapter: str, token: str) -> None:
        name = _adapter_name(adapter)
        self.set(name, _primary_field(name), token)

    def clear(self, adapter: str, field: str = "") -> None:
        name = _adapter_name(adapter)
        if field:
            (self.values.get(name) or {}).pop(str(field), None)
            if not self.values.get(name):
                self.values.pop(name, None)
        else:
            self.values.pop(name, None)
        self.save()

    def get(self, adapter: str, field: str) -> str:
        try:
            name = _adapter_name(adapter)
            encrypted = (self.values.get(name) or {}).get(str(field)) or ""
            return secretstore.decrypt(encrypted) if encrypted else ""
        except Exception as exc:
            print(f"[messaging-credentials] decrypt failed for {adapter}/{field} ({exc})", flush=True)
            return ""

    def credentials(self, adapter: str) -> dict[str, str]:
        try:
            name = _adapter_name(adapter)
        except ValueError:
            return {}
        return {field: self.get(name, field) for field in self.configured_fields(name)}

    def token(self, adapter: str) -> str:
        try:
            name = _adapter_name(adapter)
        except ValueError:
            return ""
        return self.get(name, _primary_field(name))

    def _migrate_legacy(self, legacy_path: str) -> None:
        if not os.path.isfile(legacy_path):
            return
        try:
            with open(legacy_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            migrated: dict[str, dict[str, str]] = {}
            for name in ("telegram", "discord"):
                token = str((payload.get(name) or {}).get("token") or "")
                if not token:
                    continue
                if token.startswith(("dpapi:", "plain:")):
                    stored = token
                    plaintext = secretstore.decrypt(stored)
                else:
                    stored = secretstore.encrypt(token)
                    plaintext = secretstore.decrypt(stored)
                    if plaintext != token:
                        raise RuntimeError("legacy messaging credential encrypt failed")
                if not plaintext:
                    continue
                migrated.setdefault(name, {})[_primary_field(name)] = stored
            if not migrated:
                return
            self.values = migrated
            self.save()
            os.remove(legacy_path)
            print("[messaging-credentials] migrated legacy connector auth", flush=True)
        except Exception as exc:
            print(f"[messaging-credentials] legacy migration failed ({exc})", flush=True)
