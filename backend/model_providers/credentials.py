"""DPAPI-backed credential pools with cooldown-aware rotation."""

from __future__ import annotations

import re
import time
import uuid
import copy
import threading
from functools import wraps
from dataclasses import dataclass

from security import secretstore


@dataclass(frozen=True)
class CredentialLease:
    provider: str
    credential_id: str
    label: str
    secret: str
    base_url: str = ""
    source: str = "pool"


def _retry_after_seconds(detail: str, default: int) -> int:
    text = str(detail or "")
    for pattern in (r"retry[- ]after\D{0,8}(\d+)", r"try again in\D{0,8}(\d+)"):
        match = re.search(pattern, text, re.I)
        if match:
            return max(1, min(86400, int(match.group(1))))
    return default


def _credential_write(method):
    """Rollback only this provider's credential state, not other config edits."""
    @wraps(method)
    def write(self, *args, **kwargs):
        subject = args[0] if args else kwargs.get('provider', kwargs.get('lease'))
        provider = self.canonicalize(subject.provider if isinstance(subject, CredentialLease) else subject)
        with self._lock:
            fields = ('credential_pools', 'keys', 'pool_strategies', 'credential_revisions')
            had_cloud = 'cloud' in self.cfg
            cloud = self.cfg.get('cloud') or {}
            existed = {name: name in cloud for name in fields}
            before = {name: (provider in (cloud.get(name) or {}), copy.deepcopy((cloud.get(name) or {}).get(provider))) for name in fields}
            runtime = {key: copy.deepcopy(value) for key, value in self._runtime_status.items() if key[0] == provider}
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                cloud = self._cloud()
                for name, (present, value) in before.items():
                    target = cloud.get(name)
                    if present:
                        cloud.setdefault(name, {})[provider] = value
                    elif isinstance(target, dict):
                        target.pop(provider, None)
                    if not existed[name] and isinstance(cloud.get(name), dict) and not cloud[name]:
                        cloud.pop(name, None)
                self.clear_runtime_status(provider)
                self._runtime_status.update(runtime)
                if not had_cloud and not cloud:
                    self.cfg.pop('cloud', None)
                raise
    return write


def _credential_read(method):
    @wraps(method)
    def read(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return read


class CredentialPoolStore:
    """Operate directly on the router config so atomic save stays centralized."""

    def __init__(self, cfg: dict, canonicalize, save):
        self.cfg = cfg
        self.canonicalize = canonicalize
        self.save = save
        self._lock = threading.RLock()
        self._runtime_status: dict[tuple[str, str], dict] = {}

    @_credential_read
    def revision(self, provider: str) -> int:
        value = (self._cloud().get('credential_revisions') or {}).get(self.canonicalize(provider), 0)
        return value if type(value) is int and value >= 0 else 0

    def snapshot(self, provider: str) -> dict:
        with self._lock:
            return {'items': self.public_records(provider), 'strategy': self.strategy(provider), 'revision': self.revision(provider)}

    def _persist(self, provider: str) -> None:
        name = self.canonicalize(provider)
        self._cloud().setdefault('credential_revisions', {})[name] = self.revision(name) + 1
        try:
            if self.save() is False:
                raise RuntimeError('Persistence rejected the update')
        except Exception:
            raise RuntimeError('Credential settings could not be saved.') from None

    def _cloud(self) -> dict:
        return self.cfg.setdefault("cloud", {})

    def _pools(self) -> dict:
        return self._cloud().setdefault("credential_pools", {})

    @_credential_read
    def strategy(self, provider: str) -> str:
        strategies = self._cloud().setdefault("pool_strategies", {})
        return str(strategies.get(self.canonicalize(provider)) or "priority")

    @_credential_write
    def set_strategy(self, provider: str, strategy: str) -> None:
        if strategy not in {"priority", "round_robin"}:
            raise ValueError("credential strategy must be priority or round_robin")
        self._cloud().setdefault("pool_strategies", {})[self.canonicalize(provider)] = strategy
        self._persist(provider)

    @_credential_read
    def pool_names(self) -> list[str]:
        names = set(self._pools())
        names.update(self._cloud().get("keys") or {})
        return sorted(str(name) for name in names if name)

    @_credential_read
    def records(self, provider: str, *, include_legacy: bool = True) -> list[dict]:
        provider = self.canonicalize(provider)
        raw = self._pools().get(provider) or []
        rows = [dict(item) for item in raw if isinstance(item, dict) and item.get("secret")]
        if include_legacy:
            legacy = (self._cloud().get("keys") or {}).get(provider)
            if legacy and not any(item.get("id") == "legacy-primary" for item in rows):
                rows.append({
                    "id": "legacy-primary", "label": "Primary", "secret": legacy,
                    "priority": -1000, "enabled": True, "source": "legacy",
                })
        return rows

    @_credential_read
    def public_records(self, provider: str) -> list[dict]:
        now = time.time()
        result = []
        for item in self.records(provider):
            runtime = self._runtime_status.get((self.canonicalize(provider), str(item.get("id") or ""))) or {}
            cooldown = float(runtime.get("cooldown_until") or item.get("cooldown_until") or 0)
            result.append({
                "id": item.get("id"),
                "label": item.get("label") or "Credential",
                "priority": int(item.get("priority") or 0),
                "enabled": item.get("enabled", True) is not False,
                "base_url": item.get("base_url") or "",
                "source": item.get("source") or "pool",
                "status": (runtime.get("status") or "cooldown") if cooldown > now else item.get("status") or "ready",
                "cooldown_until": int(cooldown) if cooldown > now else 0,
                "failures": int(runtime.get("failures") or item.get("failures") or 0),
                "last_error": str(runtime.get("last_error") or item.get("last_error") or "")[:160],
            })
        return result

    @_credential_write
    def add(self, provider: str, secret: str, *, label: str = "", base_url: str = "",
            priority: int | None = None) -> dict:
        provider = self.canonicalize(provider)
        if not secret:
            raise ValueError("credential secret is required")
        rows = self._pools().setdefault(provider, [])
        if priority is None:
            priority = max([int(item.get("priority") or 0) for item in rows] + [-1]) + 1
        record = {
            "id": uuid.uuid4().hex[:12],
            "label": (label or f"Credential {len(rows) + 1}")[:80],
            "secret": secretstore.encrypt(secret),
            "base_url": str(base_url or "")[:500],
            "priority": int(priority),
            "enabled": True,
            "status": "ready",
            "failures": 0,
            "cooldown_until": 0,
        }
        rows.append(record)
        self._persist(provider)
        return {k: v for k, v in record.items() if k != "secret"}

    @_credential_write
    def replace(self, provider: str, secret: str, *, label: str = "API key") -> dict:
        """Atomically expose the single-key Settings contract over the pool."""
        provider = self.canonicalize(provider)
        if not secret:
            raise ValueError("credential secret is required")
        record = {
            "id": uuid.uuid4().hex[:12],
            "label": str(label or "API key")[:80],
            "secret": secretstore.encrypt(secret),
            "base_url": "",
            "priority": 0,
            "enabled": True,
            "status": "ready",
            "failures": 0,
            "cooldown_until": 0,
        }
        self._pools()[provider] = [record]
        (self._cloud().get("keys") or {}).pop(provider, None)
        self.clear_runtime_status(provider)
        self._persist(provider)
        return {key: value for key, value in record.items() if key != "secret"}

    @_credential_write
    def clear(self, provider: str) -> bool:
        provider = self.canonicalize(provider)
        pools = self._pools()
        changed = bool(pools.pop(provider, None))
        legacy = self._cloud().get("keys") or {}
        changed = bool(legacy.pop(provider, None)) or changed
        self.clear_runtime_status(provider)
        if changed:
            self._persist(provider)
        return changed

    @_credential_write
    def remove(self, provider: str, credential_id: str) -> bool:
        provider = self.canonicalize(provider)
        if credential_id == "legacy-primary" and provider in (self._cloud().get("keys") or {}):
            changed = bool((self._cloud().get("keys") or {}).pop(provider, None))
        else:
            rows = self._pools().get(provider) or []
            kept = [item for item in rows if item.get("id") != credential_id]
            changed = len(kept) != len(rows)
            self._pools()[provider] = kept
        if changed:
            self.clear_runtime_status(provider, credential_id)
            self._persist(provider)
        return changed

    def _editable_record(self, provider: str, credential_id: str):
        for item in self._pools().get(provider) or []:
            if item.get('id') == credential_id:
                return item
        legacy = self._cloud().get('keys') or {}
        if credential_id != 'legacy-primary' or provider not in legacy:
            return None
        # Preserve the existing encrypted bytes and public identity. The outer
        # transaction restores the legacy key if migrating its metadata fails.
        item = {'id': 'legacy-primary', 'label': 'Primary', 'secret': legacy.pop(provider),
                'priority': -1000, 'enabled': True, 'status': 'ready', 'failures': 0, 'cooldown_until': 0}
        self._pools().setdefault(provider, []).append(item)
        return item

    @_credential_write
    def set_enabled(self, provider: str, credential_id: str, enabled: bool) -> bool:
        provider = self.canonicalize(provider)
        item = self._editable_record(provider, credential_id)
        if item is not None:
            item["enabled"] = bool(enabled)
            if enabled:
                self.clear_runtime_status(provider, credential_id)
            self._persist(provider)
            return True
        return False

    @_credential_write
    def set_priority(self, provider: str, credential_id: str, priority: int) -> bool:
        provider = self.canonicalize(provider)
        value = int(priority)
        if not -10_000 <= value <= 10_000:
            raise ValueError("credential priority must be between -10000 and 10000")
        item = self._editable_record(provider, credential_id)
        if item is not None:
            item["priority"] = value
            self._persist(provider)
            return True
        return False

    @_credential_read
    def leases(self, provider: str) -> list[CredentialLease]:
        provider = self.canonicalize(provider)
        now = time.time()
        rows = [item for item in self.records(provider)
                if item.get("enabled", True) is not False
                and float(item.get("cooldown_until") or 0) <= now
                and float((self._runtime_status.get(
                    (provider, str(item.get("id") or ""))) or {}).get("cooldown_until") or 0) <= now]
        rows.sort(key=lambda item: (int(item.get("priority") or 0), str(item.get("id") or "")))
        if self.strategy(provider) == "round_robin" and rows:
            cursors = self._cloud().setdefault("pool_cursors", {})
            start = int(cursors.get(provider) or 0) % len(rows)
            rows = rows[start:] + rows[:start]
            cursors[provider] = (start + 1) % len(rows)
        out = []
        for item in rows:
            try:
                secret = secretstore.decrypt(item.get("secret") or "")
            except Exception:
                continue
            if secret:
                out.append(CredentialLease(
                    provider=provider, credential_id=str(item.get("id") or ""),
                    label=str(item.get("label") or "Credential"), secret=secret,
                    base_url=str(item.get("base_url") or ""),
                    source=str(item.get("source") or "pool"),
                ))
        return out

    @_credential_read
    def available(self, lease: CredentialLease) -> bool:
        state = self._runtime_status.get((lease.provider, lease.credential_id)) or {}
        return float(state.get("cooldown_until") or 0) <= time.time()

    @_credential_read
    def clear_runtime_status(
        self, provider: str, credential_id: str | None = None,
    ) -> None:
        name = self.canonicalize(provider)
        if credential_id is not None:
            self._runtime_status.pop((name, str(credential_id)), None)
            return
        for key in tuple(self._runtime_status):
            if key[0] == name:
                self._runtime_status.pop(key, None)

    @_credential_write
    def mark_success(self, lease: CredentialLease) -> None:
        if lease.source != "pool":
            self._runtime_status.pop((lease.provider, lease.credential_id), None)
            return
        for item in self._pools().get(lease.provider) or []:
            if item.get("id") == lease.credential_id:
                if item.get("failures") or item.get("status") != "ready" or item.get("cooldown_until"):
                    item.update(status="ready", failures=0, cooldown_until=0, last_error="")
                    self._persist(lease.provider)
                return

    @_credential_write
    def mark_failure(self, lease: CredentialLease, *, status_code: int = 0,
                     detail: str = "") -> None:
        if status_code in {400, 404, 413, 415, 422}:
            # Invalid model/input requests do not change credential usability.
            return
        if status_code in {401, 403}:
            cooldown = 3600
            status = "auth_error"
        elif status_code in {402}:
            cooldown = 21600
            status = "exhausted"
        elif status_code == 429:
            cooldown = _retry_after_seconds(detail, 60)
            status = "rate_limited"
        else:
            cooldown = 30
            status = "error"
        if lease.source != "pool":
            # OAuth authorization failures are token lifecycle signals, not a
            # one-hour provider outage. The router invalidates the rejected
            # access generation so the next preflight can refresh it.
            if status_code in {401, 403} and "oauth" in lease.source:
                cooldown = 0
            key = (lease.provider, lease.credential_id)
            prior = self._runtime_status.get(key) or {}
            self._runtime_status[key] = {
                "status": status, "cooldown_until": int(time.time() + cooldown),
                "failures": int(prior.get("failures") or 0) + 1,
                "last_error": str(detail or status)[:500],
            }
            return
        for item in self._pools().get(lease.provider) or []:
            if item.get("id") == lease.credential_id:
                item["failures"] = int(item.get("failures") or 0) + 1
                item["status"] = status
                item["cooldown_until"] = int(time.time() + cooldown)
                item["last_error"] = str(detail or status)[:500]
                self._persist(lease.provider)
                return
