"""Authorized, session-aware gateway shared by all messaging adapters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import secrets
import time

from .base import (
    GatewayAdapter,
    MessageEnvelope,
    MessagingIngressOutcomeUnknown,
)
from .store import MessagingIngressConflict, MessagingIngressStore
from .catalog import BY_ID, definitions as platform_definitions


_REMOVE = object()


def _normalize_catalog_value(field: dict, value):
    kind = str(field.get("value_type") or "string")
    if kind == "list":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").replace("\r", "\n")
        return [item.strip() for line in text.split("\n")
                for item in line.split(",") if item.strip()]
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if not text:
            return _REMOVE
        return text in {"1", "true", "yes", "on"}
    if kind == "integer":
        text = str(value or "").strip()
        return int(text) if text else _REMOVE
    text = str(value or "").strip()
    return text if text else _REMOVE


def _catalog_display_value(value):
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return value


class MessagingGateway:
    def __init__(self, config_path: str, *, token_getter=None,
                 credential_status_getter=None,
                 credential_required_getter=None,
                 credential_fields_getter=None, state_path: str = "",
                 attachment_root: str = ""):
        self.config_path = config_path
        self.token_getter = token_getter
        self.credential_status_getter = credential_status_getter
        self.credential_required_getter = credential_required_getter
        self.credential_fields_getter = credential_fields_getter
        self.config = self._load()
        self.adapters: dict[str, GatewayAdapter] = {}
        self.plugin_errors: list[dict] = []
        self.router = None
        self.state_sink = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._outbound_locks: dict[str, asyncio.Lock] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task] = set()
        self._recovery_active: set[str] = set()
        self._submitted: dict[str, tuple[asyncio.Task, MessageEnvelope]] = {}
        self._receiving: dict[asyncio.Task, MessageEnvelope] = {}
        self._recovery_task: asyncio.Task | None = None
        self._recovery_wake = asyncio.Event()
        self._recovery_concurrency = 20
        self._accepting = True
        self._extension_runtime = None
        self._extension_adapter_names: set[str] = set()
        self._extension_watch_task: asyncio.Task | None = None
        self._outbox_task: asyncio.Task | None = None
        self._outbox_wake = asyncio.Event()
        self._outbox_poll_interval_s = 0.5
        self._outbox_batch_size = 20
        self._outbox_concurrency = 4
        self.attachment_root = os.path.abspath(
            attachment_root
            or os.path.join(os.path.dirname(os.path.abspath(config_path)), "attachments")
        )
        os.makedirs(self.attachment_root, exist_ok=True)
        default_state = os.path.splitext(os.path.abspath(config_path))[0] + ".sqlite3"
        self.ingress = MessagingIngressStore(state_path or default_state)
        self._stats = {"received": 0, "replied": 0, "rejected": 0, "duplicates": 0,
                       "errors": 0}
        self._register_builtins()

    def _load(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            if isinstance(value, dict):
                return value
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[gateway] config load failed: {exc}", flush=True)
        return {"enabled": False, "adapters": {}, "sessions": {},
                "pairing": {"pending": [], "approved": []}}

    def save(self) -> None:
        directory = os.path.dirname(self.config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp = self.config_path + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self.config, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.config_path)
        except Exception:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise

    def _register_builtins(self) -> None:
        from .adapters.discord import DiscordAdapter
        from .adapters.telegram import TelegramAdapter
        self.register(TelegramAdapter(self))
        self.register(DiscordAdapter(self))

    def register(self, adapter: GatewayAdapter) -> None:
        self.adapters[str(adapter.name)] = adapter

    def bind_extension_runtime(self, runtime) -> None:
        """Bind the immutable extension catalog without importing package code."""

        self._extension_runtime = runtime

    async def _refresh_extension_adapters(self) -> None:
        runtime = self._extension_runtime
        if runtime is None:
            return
        from .extension_adapter import ExtensionMessagingAdapter

        rows = await asyncio.to_thread(
            runtime.packages.resolved_contributions,
            kind="messaging_adapters",
        )
        desired: dict[str, ExtensionMessagingAdapter] = {}
        errors: list[dict] = []
        for row in rows:
            try:
                adapter = ExtensionMessagingAdapter(self, runtime, row)
                if adapter.name in desired:
                    raise ValueError("duplicate active extension adapter name")
                desired[adapter.name] = adapter
            except Exception as exc:
                errors.append({
                    "plugin": str(row.get("package_id") or row.get("id") or "extension"),
                    "error": str(exc),
                })
        for name in tuple(self._extension_adapter_names):
            current = self.adapters.get(name)
            replacement = desired.get(name)
            unchanged = bool(
                current is not None
                and replacement is not None
                and getattr(current, "descriptor_digest", "")
                == replacement.descriptor_digest
            )
            if unchanged:
                desired.pop(name, None)
                continue
            if current is not None:
                await current.stop()
            self.adapters.pop(name, None)
            self._extension_adapter_names.discard(name)
        for name, adapter in desired.items():
            if name in self.adapters:
                errors.append({
                    "plugin": adapter.package_id,
                    "error": f"adapter name collides with built-in adapter: {name}",
                })
                continue
            self.adapters[name] = adapter
            self._extension_adapter_names.add(name)
        self.plugin_errors = errors

    async def _watch_extensions(self) -> None:
        while self._accepting:
            try:
                await asyncio.sleep(2.0)
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.plugin_errors = [{
                    "plugin": "messaging-extension-catalog",
                    "error": str(exc),
                }]

    def set_router(self, callback) -> None:
        self.router = callback

    def set_state_sink(self, callback) -> None:
        self.state_sink = callback

    async def _publish_state(self) -> None:
        if self.state_sink is None:
            return
        result = self.state_sink(self.public_state())
        if hasattr(result, "__await__"):
            await result

    def adapter_config(self, name: str) -> dict:
        value = (self.config.setdefault("adapters", {}).get(name) or {})
        return dict(value) if isinstance(value, dict) else {}

    async def token(self, name: str) -> str:
        if not self.token_getter:
            return ""
        value = self.token_getter(name)
        if hasattr(value, "__await__"):
            value = await value
        return str(value or "")

    async def credentials(self, name: str) -> dict[str, str]:
        if not self.credential_fields_getter:
            token = await self.token(name)
            return {"token": token} if token else {}
        value = self.credential_fields_getter(name)
        if hasattr(value, "__await__"):
            value = await value
        return dict(value or {})

    def update(self, *, enabled=None, adapter: str = "", values: dict = None) -> None:
        if enabled is not None:
            self.config["enabled"] = bool(enabled)
        if adapter:
            row = self.config.setdefault("adapters", {}).setdefault(adapter, {})
            if not isinstance(values, dict):
                raise ValueError("adapter config must be an object")
            allowed = ("enabled", "allowed_users", "allowed_conversations", "prefix",
                       "mention_only", "poll_timeout", "intents", "allow_all")
            for key in allowed:
                if key in values:
                    if key in {"allowed_users", "allowed_conversations"} and not isinstance(values[key], list):
                        raise ValueError(f"{key} must be a list")
                    row[key] = values[key]
            if "fields" in values:
                if not isinstance(values["fields"], dict):
                    raise ValueError("platform fields must be an object")
                definition = BY_ID.get(adapter) or {}
                definitions = {
                    str(item.get("key")): item
                    for item in definition.get("fields") or ()
                    if not item.get("secret")
                }
                extras = dict(row.get("fields") or {}) if isinstance(row.get("fields"), dict) else {}
                for field_key, value in values["fields"].items():
                    spec = definitions.get(str(field_key))
                    if spec is None:
                        continue
                    config_key = str(spec.get("config_key") or "")
                    normalized = _normalize_catalog_value(spec, value)
                    if config_key:
                        if normalized is _REMOVE:
                            row.pop(config_key, None)
                        else:
                            row[config_key] = normalized
                        extras.pop(str(field_key), None)
                    elif normalized is _REMOVE:
                        extras.pop(str(field_key), None)
                    else:
                        extras[str(field_key)] = normalized
                row["fields"] = extras
        self.save()

    def _pairing(self) -> dict:
        value = self.config.setdefault("pairing", {"pending": [], "approved": []})
        if not isinstance(value, dict):
            value = {"pending": [], "approved": []}
            self.config["pairing"] = value
        value.setdefault("pending", [])
        value.setdefault("approved", [])
        return value

    def pairing_state(self) -> dict:
        pairing = self._pairing()
        return {
            "pending": [dict(row) for row in pairing.get("pending") or () if isinstance(row, dict)],
            "approved": [dict(row) for row in pairing.get("approved") or () if isinstance(row, dict)],
        }

    def _record_pairing_request(self, envelope: MessageEnvelope) -> None:
        pairing = self._pairing()
        pending = [dict(row) for row in pairing.get("pending") or () if isinstance(row, dict)]
        if any(row.get("platform") == envelope.adapter
               and str(row.get("user_id")) == str(envelope.user_id) for row in pending):
            return
        pending.append({
            "request_id": secrets.token_hex(8), "platform": envelope.adapter,
            "user_id": str(envelope.user_id), "user_name": str(envelope.user_name or ""),
            "conversation_id": str(envelope.conversation_id),
        })
        pairing["pending"] = pending[-200:]
        self.save()

    def approve_pairing(self, platform: str, request_id: str) -> bool:
        pairing = self._pairing()
        pending = [dict(row) for row in pairing.get("pending") or () if isinstance(row, dict)]
        row = next((item for item in pending if item.get("platform") == platform
                    and item.get("request_id") == request_id), None)
        if row is None:
            return False
        pairing["pending"] = [item for item in pending if item is not row]
        approved = [dict(item) for item in pairing.get("approved") or () if isinstance(item, dict)]
        if not any(item.get("platform") == platform
                   and str(item.get("user_id")) == str(row.get("user_id")) for item in approved):
            approved.append({key: row.get(key) for key in (
                "platform", "user_id", "user_name", "conversation_id")})
        pairing["approved"] = approved
        self.save()
        return True

    def revoke_pairing(self, platform: str, user_id: str) -> bool:
        pairing = self._pairing()
        approved = [dict(row) for row in pairing.get("approved") or () if isinstance(row, dict)]
        kept = [row for row in approved if not (
            row.get("platform") == platform and str(row.get("user_id")) == str(user_id))]
        if len(kept) == len(approved):
            return False
        pairing["approved"] = kept
        self.save()
        return True

    def authorized(self, envelope: MessageEnvelope) -> tuple[bool, str]:
        cfg = self.adapter_config(envelope.adapter)
        approved = self._pairing().get("approved") or []
        paired = any(
            isinstance(item, dict) and item.get("platform") == envelope.adapter
            and str(item.get("user_id")) == str(envelope.user_id)
            for item in approved
        )
        users = {str(item) for item in (cfg.get("allowed_users") or [])}
        conversations = {str(item) for item in (cfg.get("allowed_conversations") or [])}
        if not (cfg.get("allow_all") is True or paired or users or conversations):
            return False, "no allowlist configured"
        if not (cfg.get("allow_all") is True or paired) and users and str(envelope.user_id) not in users:
            return False, "user is not allowlisted"
        if conversations and str(envelope.conversation_id) not in conversations:
            return False, "conversation is not allowlisted"
        prefix = str(cfg.get("prefix") or "")
        if prefix and not envelope.text.lstrip().startswith(prefix):
            return False, "message does not use the configured prefix"
        return True, ""

    def clean_text(self, envelope: MessageEnvelope) -> str:
        text = str(envelope.text or "").strip()
        prefix = str(self.adapter_config(envelope.adapter).get("prefix") or "")
        if prefix and text.startswith(prefix):
            text = text[len(prefix):].lstrip()
        return text

    async def receive(self, envelope: MessageEnvelope) -> bool:
        task = asyncio.current_task()
        self._receiving[task] = envelope
        try:
            return await self._receive_owned(envelope)
        finally:
            self._receiving.pop(task, None)

    async def _receive_owned(self, envelope: MessageEnvelope) -> bool:
        self._stats["received"] += 1
        if not self._accepting:
            return False
        if (
            not bool(self.config.get("enabled"))
            or not bool(self.adapter_config(envelope.adapter).get("enabled"))
        ):
            self._stats["rejected"] += 1
            return True
        allowed, reason = self.authorized(envelope)
        if not allowed:
            self._stats["rejected"] += 1
            await asyncio.to_thread(self.ingress.reject_admitted, envelope, reason)
            self._record_pairing_request(envelope)
            await self._publish_state()
            print(f"[gateway] rejected {envelope.route_key}: {reason}", flush=True)
            return True
        if not self.router:
            self._stats["errors"] += 1
            return False
        text = self.clean_text(envelope)
        if not text and envelope.attachments:
            text = "Please inspect the attached item or items."
        if not text:
            await asyncio.to_thread(self.ingress.reject_admitted, envelope, "empty message")
            return True
        lock = self._locks.setdefault(envelope.route_key, asyncio.Lock())
        async with lock:
            try:
                admission = await asyncio.to_thread(
                    self.ingress.admit, envelope, text
                )
            except MessagingIngressConflict as exc:
                self._stats["rejected"] += 1
                print(f"[gateway] conflicting replay {envelope.route_key}: {exc}", flush=True)
                return True
            if admission.duplicate:
                self._stats["duplicates"] += 1
            if admission.terminal:
                await self._deliver_outbound(envelope)
                return True
            routed_envelope = envelope
            if admission.needs_reconciliation:
                routed_envelope = replace(
                    envelope,
                    metadata={
                        **dict(envelope.metadata or {}),
                        "_variant1_ingress_reconcile_only": True,
                    },
                )
            else:
                await asyncio.to_thread(self.ingress.mark_routing, envelope)
            adapter = self.adapters.get(envelope.adapter)
            # Typing is a best-effort outbound hint and never owns inbound
            # admission.
            if adapter:
                try:
                    await adapter.send_typing(routed_envelope)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    adapter.last_error = str(exc)
            try:
                reply = await self.router(routed_envelope, text)
            except MessagingIngressOutcomeUnknown as exc:
                self._stats["errors"] += 1
                await asyncio.to_thread(
                    self.ingress.release_routing,
                    envelope,
                    error=str(exc),
                )
                return False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stats["errors"] += 1
                if adapter:
                    adapter.last_error = str(exc)
                await asyncio.to_thread(
                    self.ingress.release_routing, envelope, error=str(exc)
                )
                print(f"[gateway] route failed for {envelope.route_key}: {exc}", flush=True)
                return False
            await asyncio.to_thread(
                self.ingress.complete_routed,
                envelope,
                reply=str(reply or ""),
            )
            await self._deliver_outbound(envelope)
            return True

    async def admit(self, envelope: MessageEnvelope) -> bool:
        """Acknowledge durable inbox admission without awaiting a model reply."""
        if not self._accepting:
            return False
        if (not self.config.get("enabled")
                or not self.adapter_config(envelope.adapter).get("enabled")):
            return True
        allowed, _reason = self.authorized(envelope)
        if not allowed:
            return await self.receive(envelope)
        text = self.clean_text(envelope)
        if not text and envelope.attachments:
            text = "Please inspect the attached item or items."
        if not text:
            return True
        try:
            await asyncio.to_thread(self.ingress.admit, envelope, text)
        except MessagingIngressConflict:
            self._stats["rejected"] += 1
            return True
        self.submit(envelope, retry=True)
        return True

    async def _deliver_outbound(self, envelope: MessageEnvelope) -> bool:
        lock = self._outbound_locks.setdefault(envelope.ticket_id, asyncio.Lock())
        async with lock:
            reply = await asyncio.to_thread(self.ingress.outbound_pending, envelope)
            if not reply:
                return True
            adapter = self.adapters.get(envelope.adapter)
            if adapter is None:
                return False
            try:
                await adapter.send_text(envelope, reply)
                await asyncio.to_thread(self.ingress.mark_outbound_delivered, envelope)
                self._stats["replied"] += 1
                return True
            except asyncio.CancelledError:
                adapter.last_error = "reply delivery cancelled after route commit"
                self._stats["errors"] += 1
                raise
            except Exception as exc:
                self._stats["errors"] += 1
                adapter.last_error = str(exc)
                await asyncio.to_thread(
                    self.ingress.mark_outbound_error, envelope, str(exc)
                )
                self._outbox_wake.set()
                print(
                    f"[gateway] reply delivery failed after committed route "
                    f"for {envelope.route_key}: {exc}",
                    flush=True,
                )
                return False

    async def _receive_with_retry(self, envelope: MessageEnvelope) -> bool:
        delay = 1.0
        while self._accepting:
            if await self.receive(envelope):
                return True
            await asyncio.sleep(delay)
            delay = min(30.0, delay * 2.0)
        return False

    def submit(self, envelope: MessageEnvelope, *, retry: bool = False) -> asyncio.Task:
        existing = self._submitted.get(envelope.ticket_id)
        if existing is not None and not existing[0].done():
            if existing[1] != envelope:
                raise MessagingIngressConflict("active message identity has different content")
            return existing[0]
        task = asyncio.create_task(
            self._receive_with_retry(envelope) if retry else self.receive(envelope),
            name=f"messaging:{envelope.ticket_id}",
        )
        self._inflight.add(task)
        self._submitted[envelope.ticket_id] = (task, envelope)
        def settled(done):
            self._inflight.discard(done)
            if self._submitted.get(envelope.ticket_id, (None,))[0] is done:
                self._submitted.pop(envelope.ticket_id, None)
            self._recovery_active.discard(envelope.ticket_id)
            self._recovery_wake.set()
            if not done.cancelled():
                done.exception()  # Observe failures even after a transport has acknowledged admission.
        task.add_done_callback(settled)
        return task

    def _active_ingress(self):
        envelopes = [envelope for task, envelope in self._submitted.values() if not task.done()]
        envelopes.extend(self._receiving.values())
        return tuple({self.ingress._identity(envelope) for envelope in envelopes})

    async def _recover_durable_routes(self, adapter_name: str = "") -> None:
        await asyncio.to_thread(self.ingress.recoverable_ingress, adapter=adapter_name,
                                limit=1, exclude=self._active_ingress(), reclassify_before=time.time())
        self._recovery_wake.set()

    async def _pump_ingress_once(self) -> None:
        if not self.config.get("enabled"):
            return
        for name, adapter in tuple(self.adapters.items()):
            capacity = self._recovery_concurrency - len(self._recovery_active)
            if capacity <= 0:
                return
            if not self.adapter_config(name).get("enabled") or not (
                adapter.connected or (adapter.task and not adapter.task.done())
            ):
                continue
            rows = await asyncio.to_thread(self.ingress.pending_ingress, adapter=name,
                                           limit=capacity, exclude=self._active_ingress())
            for envelope in rows:
                if not self._accepting:
                    return
                if envelope.ticket_id in self._submitted:
                    continue
                self._recovery_active.add(envelope.ticket_id)
                self.submit(envelope, retry=True)

    async def _run_ingress(self) -> None:
        while self._accepting:
            self._recovery_wake.clear()
            try:
                await self._pump_ingress_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._stats["errors"] += 1
                print(f"[gateway] inbox recovery failed: {exc}", flush=True)
            try:
                await asyncio.wait_for(self._recovery_wake.wait(), 0.5)
            except asyncio.TimeoutError:
                pass

    def _redeliver_outbox(self, adapter_name: str) -> None:
        self._outbox_wake.set()

    async def _pump_outbox_once(self) -> int:
        if not self.config.get("enabled"):
            return 0
        slots = asyncio.Semaphore(self._outbox_concurrency)

        async def deliver(envelope):
            async with slots:
                try:
                    return await asyncio.wait_for(self._deliver_outbound(envelope), 30.0)
                except asyncio.TimeoutError:
                    await asyncio.to_thread(
                        self.ingress.mark_outbound_error, envelope, "reply delivery timed out",
                    )
                    return False

        delivered = 0
        for name, adapter in tuple(self.adapters.items()):
            if not self.adapter_config(name).get("enabled"):
                continue
            if not (adapter.connected or (adapter.task and not adapter.task.done())):
                continue
            rows = await asyncio.to_thread(
                self.ingress.pending_outbound, adapter=name,
                limit=self._outbox_batch_size, ready_only=True,
            )
            if rows:
                results = await asyncio.gather(
                    *(deliver(envelope) for envelope, _ in rows), return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, BaseException):
                        print(f"[gateway] outbox item retry failed: {result}", flush=True)
                    elif result:
                        delivered += 1
        return delivered

    async def _run_outbox(self) -> None:
        while self._accepting:
            self._outbox_wake.clear()
            try:
                delivered = await self._pump_outbox_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[gateway] outbox retry failed: {exc}", flush=True)
                delivered = 0
            if delivered:
                continue
            try:
                await asyncio.wait_for(
                    self._outbox_wake.wait(), self._outbox_poll_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    def session_id(self, route_key: str) -> str:
        return str((self.config.setdefault("sessions", {}) or {}).get(route_key) or "")

    def bind_session(self, route_key: str, session_id: str) -> None:
        self.config.setdefault("sessions", {})[route_key] = session_id
        self.save()

    async def start(self) -> None:
        self._accepting = True
        await self.reconcile()
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._run_ingress(), name="messaging-inbox")
        if self._outbox_task is None or self._outbox_task.done():
            self._outbox_task = asyncio.create_task(
                self._run_outbox(), name="messaging-outbox",
            )
        if (
            self._extension_runtime is not None
            and (
                self._extension_watch_task is None
                or self._extension_watch_task.done()
            )
        ):
            self._extension_watch_task = asyncio.create_task(
                self._watch_extensions(), name="messaging-extension-catalog"
            )

    async def reconcile(self) -> None:
        """Make live adapter loops match the current persisted configuration."""

        async with self._lifecycle_lock:
            await self._refresh_extension_adapters()
            gateway_enabled = bool(self.config.get("enabled"))
            started_names: list[str] = []
            for name, adapter in self.adapters.items():
                desired = gateway_enabled and bool(
                    self.adapter_config(name).get("enabled")
                )
                running = bool(
                    (adapter.task and not adapter.task.done())
                    or adapter.connected
                )
                if desired and not running:
                    await adapter.start()
                    started_names.append(name)
                    self._redeliver_outbox(name)
                elif not desired and running:
                    await adapter.stop()
            if gateway_enabled:
                for name in started_names:
                    await self._recover_durable_routes(name)

    async def close_ingress(self) -> None:
        """Stop transport producers while the routing dependencies are live."""

        self._accepting = False
        recovery, self._recovery_task = self._recovery_task, None
        if recovery is not None:
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)
        outbox, self._outbox_task = self._outbox_task, None
        if outbox is not None:
            outbox.cancel()
            await asyncio.gather(outbox, return_exceptions=True)
        watcher, self._extension_watch_task = self._extension_watch_task, None
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        async with self._lifecycle_lock:
            await asyncio.gather(*(adapter.stop() for adapter in self.adapters.values()),
                                 return_exceptions=True)

    async def drain(self, *, timeout_s: float = 30.0) -> None:
        """Let admitted routes reach a terminal boundary before teardown."""

        tasks = list(self._inflight)
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=max(0.0, float(timeout_s))
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()

    async def stop(self) -> None:
        await self.close_ingress()
        await self.drain()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def public_state(self) -> dict:
        def _credential_required(name: str) -> bool:
            if not self.credential_required_getter:
                return name in {"discord", "telegram"}
            try:
                return bool(self.credential_required_getter(name))
            except Exception:
                return False

        def _credential_configured(name: str) -> bool:
            if not _credential_required(name):
                return True
            if not self.credential_status_getter:
                return False
            try:
                return bool(self.credential_status_getter(name))
            except Exception:
                return False

        adapter_rows = []
        seen = set()
        for definition in platform_definitions():
            name = str(definition["id"])
            seen.add(name)
            adapter = self.adapters.get(name)
            status = adapter.status() if adapter is not None else {
                "name": name, "display_name": definition["name"],
                "running": False, "connected": False, "last_error": "",
            }
            config = self.adapter_config(name)
            configured_fields = (
                list(self.credential_fields_getter(name).keys())
                if self.credential_fields_getter else []
            )
            non_secret = dict(config.get("fields") or {}) if isinstance(config.get("fields"), dict) else {}
            for field in definition.get("fields") or ():
                if field.get("secret"):
                    continue
                config_key = str(field.get("config_key") or "")
                if config_key and config_key in config:
                    non_secret[str(field["key"])] = _catalog_display_value(config[config_key])
            required = [field for field in definition.get("fields") or () if field.get("required")]
            configured = all(
                (field["key"] in configured_fields) if field.get("secret")
                else bool(str(non_secret.get(field["key"]) or "").strip())
                for field in required
            )
            adapter_rows.append({**status, **definition,
                "id": name, "name": name, "display_name": definition["name"],
                "runtime_available": adapter is not None,
                "configured": configured,
                "configured_fields": configured_fields,
                "values": non_secret,
                "credential_required": _credential_required(name),
                "credential_configured": _credential_configured(name),
                "config": {key: value for key, value in config.items()
                           if key not in {"token", "secret", "fields"}}})
        for name, adapter in sorted(self.adapters.items()):
            if name not in seen:
                adapter_rows.append({**adapter.status(), "id": name,
                    "description": "Plugin-contributed messaging platform.",
                    "fields": [], "runtime_available": True, "configured": True,
                    "configured_fields": [], "values": {},
                    "credential_required": False,
                    "credential_configured": True,
                    "config": self.adapter_config(name)})
        return {
            "type": "messaging:gateway", "enabled": bool(self.config.get("enabled")),
            "adapters": adapter_rows,
            "stats": dict(self._stats), "session_count": len(self.config.get("sessions") or {}),
            "plugin_errors": list(self.plugin_errors), "pairing": self.pairing_state(),
        }
