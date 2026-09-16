"""Durable Browser Fabric orchestration and public runtime factory."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from typing import Any, Callable, Mapping, Sequence
import uuid

from core_invariants import request_fingerprint
from artifacts import ContentAddressedArtifactStore
from work_fabric.scope import (
    WorkScope,
    coerce_work_scope,
    current_work_scope,
    work_scope_visible,
)

from .adapters import (
    AdapterObservation,
    BrowserAdapter,
    EMBEDDED_CAPABILITIES,
    MANAGED_CAPABILITIES,
    EmbeddedBrowserAdapter,
    ManagedPlaywrightAdapter,
)
from .models import (
    BROWSER_KINDS,
    BrowserConflict,
    BrowserExpectedState,
    BrowserFabricError,
    BrowserNotFound,
    BrowserScopeMismatch,
    BrowserStaleReference,
    BrowserUnavailable,
    BrowserUnknownEffect,
    BrowserValidationError,
    DownloadRecord,
    ElementRef,
    EventRecord,
    ObservationRecord,
    OperationRecord,
    PageRef,
    ProfileRecord,
    SessionRecord,
    TargetRecord,
    TraceRecord,
    clean_identifier,
    json_value,
)
from .store import BrowserFabricStore, default_profile_root, new_id
from .keyboard import normalize_browser_keys
from .viewport import image_dimensions, viewport_request


_PROFILE_ID_RE = re.compile(r"^profile_[0-9a-f]{32}$")
_EFFECTFUL_ACTIONS = frozenset({
    "navigate", "back", "forward", "reload", "click", "fill", "select",
    "hover", "keys", "evaluate", "set_viewport",
})


def _scope(value: WorkScope | Mapping[str, Any] | None) -> WorkScope:
    return coerce_work_scope(current_work_scope() if value is None else value)


def _optional_scope(
    value: WorkScope | Mapping[str, Any] | None,
) -> WorkScope | None:
    resolved = _scope(value)
    return None if resolved.empty else resolved


def _scope_grant(scope: WorkScope, session_id: str) -> str:
    return scope.chat_id or scope.conversation_id or f"browser:{session_id}"


def _action_arguments(values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate fabric preconditions from adapter-specific action parameters."""

    remaining = dict(values)
    raw_params = remaining.pop("params", {}) or {}
    if not isinstance(raw_params, Mapping):
        raise BrowserValidationError("action params must be a mapping")
    params = dict(raw_params)
    controls: dict[str, Any] = {}
    for key in ("target_id", "expected", "idempotency_key", "scope"):
        if key in remaining:
            controls[key] = remaining.pop(key)
    params.update(remaining)
    if "timeout" in params and "timeout_ms" not in params:
        params["timeout_ms"] = params.pop("timeout")
    return params, controls


class BrowserFabric:
    """Authority for profiles, sessions, pages, observations, and side effects."""

    def __init__(
        self,
        store: BrowserFabricStore,
        *,
        profile_root: str,
        artifact_store: Any,
        adapter_factory: Callable[[str, ProfileRecord, SessionRecord], BrowserAdapter],
        download_staging_root: str | None = None,
    ) -> None:
        self.store = store
        self.profile_root = os.path.realpath(os.path.abspath(profile_root))
        self.artifact_store = artifact_store
        self.download_staging_root = os.path.realpath(os.path.abspath(
            download_staging_root or os.path.join(os.path.dirname(store.path), "download-staging")
        ))
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, BrowserAdapter] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._embedded_lock = asyncio.Lock()
        self._shutting_down = False
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._target_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._active_traces: dict[str, str] = {}
        self._download_registration_errors: dict[tuple[str, str], dict[str, Any]] = {}
        os.makedirs(self.profile_root, exist_ok=True)
        from .preferences import BrowserPreferences
        self.preferences = BrowserPreferences(self)

    @staticmethod
    def capability_matrix() -> dict[str, dict[str, Any]]:
        return {
            "managed": {
                "supported": True,
                "capabilities": sorted(MANAGED_CAPABILITIES),
                "transport": "Playwright persistent Chromium context",
            },
            "embedded": {
                "supported": True,
                "capabilities": sorted(EMBEDDED_CAPABILITIES),
                "transport": "Main Deck workbench Chromium-tab RPC",
                "limitations": ["visible Deck required", "no Playwright trace protocol"],
            },
        }

    def _profile_path(self, profile_id: str) -> str:
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise BrowserValidationError("invalid generated browser profile ID")
        path = os.path.realpath(os.path.abspath(os.path.join(self.profile_root, profile_id)))
        try:
            if os.path.commonpath([self.profile_root, path]) != self.profile_root:
                raise BrowserValidationError("browser profile path escapes the managed root")
        except ValueError as exc:
            raise BrowserValidationError("browser profile path crosses filesystem roots") from exc
        return path

    @staticmethod
    def _assert_scope(owner: WorkScope, supplied: WorkScope, *, mutation: bool) -> None:
        if owner.empty:
            return
        if supplied.empty:
            if mutation:
                raise BrowserScopeMismatch("browser mutation requires the owning WorkScope")
            return
        if not work_scope_visible(owner, supplied):
            raise BrowserScopeMismatch("browser resource is outside the owning WorkScope")

    @staticmethod
    def _shared_embedded_profile(profile: ProfileRecord) -> bool:
        # Chats share the persistent guest partition, not tab/session ownership.
        return profile.kind == "embedded"

    def shared_embedded_session(self, session: SessionRecord) -> bool:
        return session.kind == "embedded" and not session.scope.chat_id

    async def _retire_duplicate_embedded_sessions(self, keep_id: str) -> None:
        owner = self.store.get_session(keep_id).scope.chat_id
        for duplicate in self.store.list_sessions(
            include_closed=False, limit=500, scope=WorkScope(chat_id=owner) if owner else None,
        ):
            if (duplicate.kind != "embedded" or duplicate.session_id == keep_id
                    or duplicate.scope.chat_id != owner):
                continue
            adapter = self._adapters.get(duplicate.session_id)
            if adapter is not None:
                await self._close_adapter_owner(duplicate.session_id, adapter)
            for target in self.store.list_targets(
                duplicate.session_id, include_closed=False
            ):
                self.store.update_target(target.target_id, state="closed")
            self.store.update_session(
                duplicate.session_id, state="closed", current_target_id=""
            )

    def create_profile(
        self,
        name: str,
        *,
        kind: str = "managed",
        persistent: bool = True,
        scope: WorkScope | Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProfileRecord:
        clean_name = clean_identifier(name, "profile name")
        clean_kind = clean_identifier(kind, "browser kind")
        if clean_kind not in BROWSER_KINDS:
            supported = ", ".join(sorted(BROWSER_KINDS))
            raise BrowserValidationError(
                f"unknown browser kind {clean_kind!r}; supported kinds: {supported}"
            )
        resolved = _scope(scope)
        existing = self.store.find_profile(
            clean_name, clean_kind, scope=resolved,
        )
        if existing is not None:
            self._assert_scope(existing.scope, resolved, mutation=False)
            return existing
        profile_id = "profile_" + uuid.uuid4().hex
        profile_dir = self._profile_path(profile_id) if clean_kind == "managed" else ""
        return self.store.create_profile(
            profile_id=profile_id, name=clean_name, kind=clean_kind,
            persistent=bool(persistent), user_data_dir=profile_dir,
            scope=resolved, metadata=metadata,
        )

    def profile(
        self,
        profile_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> ProfileRecord:
        record = self.store.get_profile(profile_id)
        if scope is not None:
            self._assert_scope(record.scope, _scope(scope), mutation=False)
        return record

    def profiles(
        self,
        *,
        kind: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> tuple[ProfileRecord, ...]:
        return self.store.list_profiles(
            kind=kind,
            scope=_optional_scope(scope),
            limit=limit,
        )

    def sessions(
        self,
        *,
        state: str = "",
        include_closed: bool = False,
        scope: WorkScope | Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> tuple[SessionRecord, ...]:
        return self.store.list_sessions(
            state=state,
            include_closed=include_closed,
            scope=_optional_scope(scope),
            limit=limit,
        )

    def session(
        self,
        session_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        record = self.store.get_session(session_id)
        if scope is not None:
            supplied = _scope(scope)
            if record.kind == "embedded" and record.scope.chat_id != supplied.chat_id:
                raise BrowserScopeMismatch("embedded browser belongs to another chat")
            self._assert_scope(record.scope, supplied, mutation=False)
        return record

    def session_for_owner(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> SessionRecord | None:
        return self.store.find_session_for_owner(
            owner_kind=str(owner_kind or ""),
            owner_id=str(owner_id or ""),
            scope=_optional_scope(scope),
        )

    def targets(self, session_id: str, *, include_closed: bool = False) -> tuple[TargetRecord, ...]:
        return self.store.list_targets(session_id, include_closed=include_closed)

    def page_ref(self, session_id: str, target_id: str = "") -> PageRef:
        session = self.store.get_session(session_id)
        wanted = target_id or session.current_target_id
        if not wanted:
            active = self.store.list_targets(session_id)
            if not active:
                raise BrowserNotFound("browser session has no active page")
            wanted = active[0].target_id
        target = self.store.get_target(wanted)
        if target.session_id != session_id or target.state == "closed":
            raise BrowserNotFound("browser page is not active in this session")
        return target.page_ref(session.generation)

    def _new_adapter(self, session: SessionRecord, profile: ProfileRecord) -> BrowserAdapter:
        if session.kind == "managed":
            stored_path = os.path.realpath(os.path.abspath(profile.user_data_dir))
            try:
                contained = os.path.commonpath([self.profile_root, stored_path]) == self.profile_root
            except ValueError:
                contained = False
            if not contained:
                raise BrowserValidationError(
                    "managed browser profile is outside the pinned profile root"
                )
        adapter = self._adapter_factory(session.kind, profile, session)
        if not isinstance(adapter, BrowserAdapter):
            # Structural fake adapters are useful in focused tests and host
            # integrations, but require the complete protocol surface.
            for name in ("launch", "close", "targets", "observe", "perform"):
                if not callable(getattr(adapter, name, None)):
                    raise TypeError(f"browser adapter is missing {name}()")
        set_current = getattr(adapter, "set_current_target_id", None)
        if callable(set_current):
            set_current(session.current_target_id)
        set_download_sink = getattr(adapter, "set_download_sink", None)
        if callable(set_download_sink):
            set_download_sink(
                lambda backend_id, operation_id, download: self._record_adapter_download(
                    session.session_id, backend_id, operation_id, download
                )
            )
        return adapter

    async def _own_adapter(
        self, session_id: str, adapter: BrowserAdapter,
    ) -> None:
        async with self._lifecycle_lock:
            if self._shutting_down:
                raise BrowserUnavailable("browser fabric is shutting down")
            prior = self._adapters.get(session_id)
            if prior is not None and prior is not adapter:
                raise BrowserConflict("browser session already has a live adapter owner")
            self._adapters[session_id] = adapter

    async def _close_adapter_owner(
        self, session_id: str, adapter: BrowserAdapter,
    ) -> asyncio.CancelledError | None:
        """Join adapter cleanup and release the map only after it succeeds."""

        closing = asyncio.create_task(adapter.close())
        cancellation: asyncio.CancelledError | None = None
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
        try:
            closing.result()
        except BaseException as close_error:
            if cancellation is not None:
                try:
                    close_error.add_note("caller cancellation also occurred during close")
                except Exception:
                    pass
            raise
        if self._adapters.get(session_id) is adapter:
            self._adapters.pop(session_id, None)
            self._download_registration_errors = {
                key: error for key, error in self._download_registration_errors.items()
                if key[0] != session_id
            }
        return cancellation

    def _event(
        self, session: SessionRecord, kind: str, payload: Mapping[str, Any], *, target_id: str = "",
    ) -> EventRecord:
        return self.store.append_event(
            session_id=session.session_id, target_id=target_id, kind=kind,
            payload=payload, scope=session.scope,
        )

    async def open_session(
        self,
        *,
        kind: str = "managed",
        profile_id: str = "",
        profile_name: str = "",
        persistent_profile: bool = True,
        headless: bool = True,
        initial_url: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        _shared_lock_held: bool = False,
    ) -> SessionRecord:
        clean_kind = clean_identifier(kind, "browser kind")
        if clean_kind not in BROWSER_KINDS:
            supported = ", ".join(sorted(BROWSER_KINDS))
            raise BrowserValidationError(
                f"unknown browser kind {clean_kind!r}; supported kinds: {supported}"
            )
        resolved = _scope(scope)
        if clean_kind == "embedded":
            if profile_id:
                supplied = self.store.get_profile(profile_id)
                if supplied.kind != "embedded":
                    raise BrowserConflict(
                        "browser profile kind differs from requested session kind"
                    )
            profile = self.create_profile(
                "main-deck", kind="embedded", persistent=True,
                scope=WorkScope(), metadata={"host_scoped": True},
            )
            headless = False
            # Browser tabs outlive individual cells and kernel generations.
            resolved = WorkScope(chat_id=resolved.chat_id)
        elif profile_id:
            profile = self.store.get_profile(profile_id)
            if profile.kind != clean_kind:
                raise BrowserConflict("browser profile kind differs from requested session kind")
            if not self._shared_embedded_profile(profile):
                self._assert_scope(profile.scope, resolved, mutation=True)
        else:
            if profile_name:
                name = profile_name
            elif clean_kind == "embedded":
                name = "main-deck"
            else:
                name = f"{clean_kind}-{uuid.uuid4().hex[:12]}"
            profile = self.create_profile(
                name, kind=clean_kind, persistent=persistent_profile,
                scope=(
                    WorkScope()
                    if clean_kind == "embedded" and name == "main-deck"
                    else resolved
                ),
                metadata={"created_for": "browser_session"},
            )
        if self._shared_embedded_profile(profile) and not _shared_lock_held:
            async with self._embedded_lock:
                existing = next((
                    row for row in self.store.list_sessions(
                        include_closed=False, limit=500, scope=resolved if not resolved.empty else None,
                    )
                    if row.kind == "embedded" and row.scope.chat_id == resolved.chat_id
                ), None)
                if existing is not None:
                    await self._retire_duplicate_embedded_sessions(
                        existing.session_id
                    )
                    existing = await self.acquire_session(
                        existing.session_id,
                        scope=resolved,
                    )
                    if initial_url:
                        existing = await self.reconcile_current_page(existing.session_id, scope=resolved)
                        page = self.page_ref(existing.session_id)
                        await self.perform(
                            page,
                            "navigate",
                            params={"url": str(initial_url)},
                            scope=resolved,
                        )
                        existing = self.store.get_session(existing.session_id)
                    return existing
                return await self.open_session(
                    kind="embedded",
                    profile_id=profile.profile_id,
                    persistent_profile=persistent_profile,
                    headless=False,
                    initial_url=initial_url,
                    scope=resolved,
                    metadata={**dict(metadata or {}), "owner_kind": "chat" if resolved.chat_id else "host",
                              "owner_id": resolved.chat_id, "shared_embedded": not bool(resolved.chat_id)},
                    _shared_lock_held=True,
                )
        capabilities = tuple(self.capability_matrix()[clean_kind]["capabilities"])
        session_id = new_id("browser")
        session = self.store.create_session(
            session_id=session_id, profile_id=profile.profile_id, kind=clean_kind,
            headless=bool(headless), capabilities=capabilities, scope=resolved,
            metadata=metadata,
        )
        target_id = new_id("page")
        target = self.store.create_target(
            target_id=target_id, session_id=session_id, backend_target_id=target_id,
            url=str(initial_url or ""), state="opening",
        )
        session = self.store.update_session(session_id, current_target_id=target_id)
        self._event(session, "session.opening", {
            "profile_id": profile.profile_id, "kind": clean_kind, "headless": bool(headless),
        }, target_id=target_id)
        adapter = self._new_adapter(session, profile)
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                await self._own_adapter(session_id, adapter)
                live_targets = await adapter.launch((target,))
                if initial_url and clean_kind == "embedded":
                    await adapter.perform(target.backend_target_id, "navigate", {"url": initial_url})
                    live_targets = await adapter.targets()
                async with self._lifecycle_lock:
                    if self._shutting_down:
                        raise BrowserUnavailable("browser fabric is shutting down")
                await self._sync_targets(session_id, live_targets)
                session = self.store.update_session(
                    session_id, state="active", capabilities=sorted(adapter.capabilities), last_error="",
                )
                self._event(session, "session.opened", {
                    "generation": session.generation, "capabilities": list(session.capabilities),
                }, target_id=session.current_target_id)
                return session
            except BaseException as exc:
                try:
                    await self._close_adapter_owner(session_id, adapter)
                except BaseException as close_error:
                    try:
                        exc.add_note(f"adapter cleanup failed: {close_error}")
                    except Exception:
                        pass
                diagnostic = str(exc) or (
                    "browser launch cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else type(exc).__name__
                )
                self.store.update_target(
                    target_id, state="orphaned", last_error=diagnostic
                )
                session = self.store.update_session(
                    session_id, state="unavailable", last_error=diagnostic
                )
                self._event(
                    session, "session.unavailable", {"error": diagnostic},
                    target_id=target_id,
                )
                raise

    async def _sync_targets(
        self, session_id: str, live_targets: Sequence[Any], *, navigation_target: str = "",
    ) -> tuple[TargetRecord, ...]:
        session = self.store.get_session(session_id)
        existing = {item.backend_target_id: item for item in self.store.list_targets(
            session_id, include_closed=True
        )}
        seen: set[str] = set()
        active_target_id = ""
        for live in live_targets:
            backend_id = str(live.backend_target_id)
            seen.add(backend_id)
            record = existing.get(backend_id)
            if record is None:
                record = self.store.create_target(
                    target_id=new_id("page"), session_id=session_id,
                    backend_target_id=backend_id, title=str(live.title or ""),
                    url=str(live.url or ""), state="active",
                )
                self._event(session, "target.opened", record.to_dict(), target_id=record.target_id)
            else:
                changed = (
                    record.state != "active" or record.title != str(live.title or "")
                    or record.url != str(live.url or "")
                )
                if changed:
                    record = self.store.update_target(
                        record.target_id, state="active", title=str(live.title or ""),
                        url=str(live.url or ""),
                        navigated=(record.target_id == navigation_target or record.state == "closed"
                                   or record.url != str(live.url or "")), last_error="",
                    )
            viewport = dict(getattr(live, 'viewport', {}) or {})
            if viewport and viewport != dict(record.viewport):
                record = self.store.update_target(record.target_id, viewport=viewport)
            if bool(getattr(live, "active", False)):
                active_target_id = record.target_id
        for backend_id, record in existing.items():
            if backend_id not in seen and record.state != "closed":
                retired = self.store.update_target(record.target_id, state="closed")
                self._event(session, "target.closed", retired.to_dict(), target_id=record.target_id)
        if not active_target_id:
            current = session.current_target_id
            active_ids = [item.target_id for item in self.store.list_targets(session_id)]
            active_target_id = current if current in active_ids else next(iter(active_ids), "")
        if active_target_id != session.current_target_id:
            session = self.store.update_session(session_id, current_target_id=active_target_id)
        return self.store.list_targets(session_id)

    async def reconcile_current_page(
        self, session_id: str, *, scope: WorkScope | Mapping[str, Any] | None = None,
        create_if_empty: bool = True,
    ) -> SessionRecord:
        """Resolve an implicit embedded page from its owner's live inventory.

        Explicit PageRef/ElementRef operations never call this method: they keep
        their exact target identity and must fail when that page has closed.
        """
        session = self.session(session_id, scope=scope)
        if session.kind != "embedded":
            return session
        async with self._session_locks.setdefault(session_id, asyncio.Lock()):
            session = self.session(session_id, scope=scope)
            if session.state == "closed":
                raise BrowserUnavailable("closed browser sessions cannot be reconciled")
            adapter = self._adapters.get(session_id)
            acquired = adapter is None
            if adapter is None:
                adapter = self._new_adapter(session, self.store.get_profile(session.profile_id))
                await self._own_adapter(session_id, adapter)
            previous = session.current_target_id
            try:
                inventory = await adapter.targets()
            except BrowserUnavailable as exc:
                if acquired:
                    await self._close_adapter_owner(session_id, adapter)
                await self.preferences.connection_failed(session.scope.chat_id, session, exc)
                raise
            targets = await self._sync_targets(session_id, inventory)
            if acquired or session.state != "active":
                self.store.update_session(session_id, state="active", bump_generation=True, last_error="")
            # A different selected tab does not retarget an ongoing model workflow.
            if (previous and previous != self.store.get_session(session_id).current_target_id
                    and any(row.target_id == previous for row in targets)):
                self.store.update_session(session_id, current_target_id=previous)
        if not targets and create_if_empty:
            await self.new_page(session_id, scope=scope, _only_if_empty=True)
        return self.session(session_id, scope=scope)

    async def recover_session(self, session_id: str) -> SessionRecord:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            live = self._adapters.get(session_id)
            session = self.store.get_session(session_id)
            if live is not None and session.state == "active":
                return session
            if session.state == "closed":
                raise BrowserUnavailable("closed browser sessions cannot be recovered")
            profile = self.store.get_profile(session.profile_id)
            if not self._shared_embedded_profile(profile):
                self._assert_scope(profile.scope, session.scope, mutation=True)
            session = self.store.update_session(
                session_id, state="recovering", bump_generation=True, last_error="",
            )
            self._event(session, "session.recovering", {"generation": session.generation})
            adapter = self._new_adapter(session, profile)
            try:
                await self._own_adapter(session_id, adapter)
                live_targets = await adapter.launch(self.store.list_targets(session_id))
                async with self._lifecycle_lock:
                    if self._shutting_down:
                        raise BrowserUnavailable("browser fabric is shutting down")
                await self._sync_targets(session_id, live_targets)
                session = self.store.update_session(
                    session_id, state="active", capabilities=sorted(adapter.capabilities),
                )
                self._event(session, "session.recovered", {"generation": session.generation})
                return session
            except BaseException as exc:
                try:
                    await self._close_adapter_owner(session_id, adapter)
                except BaseException as close_error:
                    try:
                        exc.add_note(f"adapter cleanup failed: {close_error}")
                    except Exception:
                        pass
                diagnostic = str(exc) or (
                    "browser recovery cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else type(exc).__name__
                )
                session = self.store.update_session(
                    session_id, state="unavailable", last_error=diagnostic,
                )
                self._event(session, "session.unavailable", {"error": diagnostic})
                raise

    async def _adapter(self, session_id: str) -> tuple[SessionRecord, BrowserAdapter]:
        session = self.store.get_session(session_id)
        adapter = self._adapters.get(session_id)
        if adapter is None or session.state != "active":
            session = await self.recover_session(session_id)
            adapter = self._adapters.get(session_id)
        if adapter is None:
            raise BrowserUnavailable("browser adapter recovery did not produce a live adapter")
        return session, adapter

    async def acquire_session(
        self,
        session_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        """Acquire the sole live adapter generation for a durable session."""

        session = self.session(session_id, scope=scope)
        if session.state == "closed":
            raise BrowserUnavailable("closed browser sessions cannot be acquired")
        session, _adapter = await self._adapter(session.session_id)
        return self.session(session.session_id, scope=scope)

    @staticmethod
    def _check_expected(
        session: SessionRecord, target: TargetRecord, expected: BrowserExpectedState | None,
    ) -> None:
        if expected is None:
            return
        values = expected.to_dict()
        actual = {
            "session_generation": session.generation,
            "session_revision": session.revision,
            "target_revision": target.revision,
            "document_epoch": target.document_epoch,
            "observation_revision": target.observation_revision,
        }
        for key, value in values.items():
            if actual[key] != value:
                raise BrowserStaleReference(
                    f"browser {key} changed: expected {value}, observed {actual[key]}"
                )

    def _resolve_page(
        self, page: PageRef | str, *, target_id: str = "",
    ) -> tuple[SessionRecord, TargetRecord]:
        if isinstance(page, PageRef):
            session_id = page.session_id
            wanted = page.target_id
        else:
            session_id = clean_identifier(page, "session_id")
            session = self.store.get_session(session_id)
            wanted = target_id or session.current_target_id
        session = self.store.get_session(session_id)
        if not wanted:
            raise BrowserNotFound("browser session has no selected page")
        target = self.store.get_target(wanted)
        if target.session_id != session_id or target.state == "closed":
            raise BrowserNotFound("browser page is not active in this session")
        if isinstance(page, PageRef):
            if page.generation != session.generation:
                raise BrowserStaleReference("PageRef belongs to a previous browser generation")
            if page.document_epoch != target.document_epoch:
                raise BrowserStaleReference("PageRef belongs to a previous top-level document")
        return session, target

    def _resolve_element(self, element: ElementRef) -> tuple[SessionRecord, TargetRecord]:
        session = self.store.get_session(element.session_id)
        target = self.store.get_target(element.target_id)
        if target.session_id != session.session_id or target.state == "closed":
            raise BrowserNotFound("element page is not active in this session")
        if element.generation != session.generation:
            raise BrowserStaleReference("ElementRef belongs to a previous browser generation")
        if element.document_epoch != target.document_epoch:
            raise BrowserStaleReference("ElementRef belongs to a previous top-level document")
        observation = self.store.observation_at_revision(
            target.target_id, element.observation_revision
        )
        if observation is None:
            raise BrowserStaleReference("element's durable observation is unavailable")
        if (
            observation.generation != session.generation
            or observation.document_epoch != target.document_epoch
        ):
            raise BrowserStaleReference(
                "ElementRef belongs to a previous browser document"
            )
        observation.element(element.backend_ref)
        return session, target

    def _artifact(self, payload: bytes, *, media_type: str, kind: str, session: SessionRecord,
                  scope: WorkScope | None = None) -> dict[str, Any]:
        digest = hashlib.sha256(payload).hexdigest()
        artifact = self.artifact_store.put_bytes(
            payload, media_type=media_type, kind=kind,
            scope=_scope_grant(session.scope, session.session_id),
        )
        if scope is not None and not scope.empty:
            self.artifact_store.grant(artifact.ref, _scope_grant(scope, session.session_id),
                                      source_scope=artifact.scope, verify=False)
        return {
            "ref": str(artifact.ref), "sha256": digest, "bytes": len(payload),
            "media_type": media_type, "kind": kind,
        }

    async def _record_adapter_download(
        self,
        session_id: str,
        backend_target_id: str,
        operation_id: str,
        download: Any,
    ) -> bool:
        """Persist an adapter-observed download without model ceremony."""

        native_id = str(getattr(download, "download_id", "") or "")
        error_key = (str(session_id), native_id)
        try:
            session = self.store.get_session(str(session_id))
            target = next(
                (
                    row for row in self.store.list_targets(
                        session.session_id, include_closed=False
                    )
                    if row.backend_target_id == str(backend_target_id)
                ),
                None,
            )
            if target is None:
                return False
            download_id = ("download_" + hashlib.sha256(
                (session.session_id + ":" + native_id).encode()
            ).hexdigest()[:32]) if native_id else ""
            if download_id:
                try:
                    existing = self.store.get_download(download_id)
                except BrowserNotFound:
                    existing = None
                if existing is not None:
                    if (existing.session_id, existing.target_id, existing.sha256, existing.bytes) != (
                        session_id, target.target_id, download.sha256, download.bytes_count
                    ):
                        raise BrowserConflict("native download completion changed after it was recorded")
                    # A later chat can acknowledge a prior durable handoff,
                    # but cannot acquire its artifact merely by draining it.
                    self._download_registration_errors.pop(error_key, None)
                    return True
            owner_scope = session.scope if not session.scope.empty else _scope(None)
            if str(operation_id).startswith("browserop_"):
                operation = self.store.get_operation(operation_id)
                if operation.session_id != session_id or operation.target_id != target.target_id:
                    raise BrowserValidationError("download operation does not own this browser target")
                owner_scope = operation.scope
            staged_path = str(getattr(download, "path", "") or "")
            if staged_path:
                staged_path = os.path.realpath(os.path.abspath(staged_path))
                if os.path.commonpath([self.download_staging_root, staged_path]) != self.download_staging_root:
                    raise BrowserValidationError("download path is outside native download staging")
                stored_artifact = await asyncio.to_thread(
                    self.artifact_store.put_file, staged_path,
                    media_type="application/octet-stream", kind="browser_download",
                    scope=_scope_grant(owner_scope, session.session_id), max_bytes=256 * 1024 * 1024,
                )
                stored = stored_artifact.to_dict()
                if (stored["sha256"] != download.sha256 or stored["bytes"] != download.bytes_count):
                    raise BrowserValidationError("staged download differs from its completion receipt")
            else:
                payload = bytes(download.payload)
                if len(payload) > 256 * 1024 * 1024:
                    return False
                stored = self._artifact(
                    payload, media_type="application/octet-stream", kind="browser_download",
                    session=session, scope=owner_scope,
                )
            self.store.record_download(
                session_id=session.session_id,
                target_id=target.target_id,
                operation_id=(
                    str(operation_id or "")
                    or "browser-event-" + uuid.uuid4().hex
                ),
                suggested_filename=str(download.suggested_filename or "download"),
                url=str(download.url or ""),
                artifact_ref=str(stored["ref"]),
                sha256=str(stored["sha256"]),
                bytes_count=int(stored["bytes"]),
                scope=owner_scope,
                download_id=download_id,
            )
            self._download_registration_errors.pop(error_key, None)
            return True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            previous = self._download_registration_errors.get(error_key) if native_id else None
            diagnostic = ({**previous, "attempts": previous["attempts"] + 1}
                          if previous and previous["error"] == error
                          else {"error": error, "event_id": "", "attempts": 1})
            if not diagnostic["event_id"]:
                try:
                    event = self._event(
                        self.store.get_session(str(session_id)),
                        "download.record_failed",
                        {"error": error, "native_download_id": native_id,
                         "operation_id": str(operation_id), "backend_target_id": str(backend_target_id)},
                    )
                    diagnostic["event_id"] = event.event_id
                except Exception:
                    pass
            if native_id:
                self._download_registration_errors.pop(error_key, None)
                self._download_registration_errors[error_key] = diagnostic
                # This is only the current reconciliation diagnostic; durable
                # evidence remains in events. Match the maximum history window.
                if len(self._download_registration_errors) > 1000:
                    self._download_registration_errors.pop(next(iter(self._download_registration_errors)))
            return False

    async def _attachment_document(
        self, session: SessionRecord, target: TargetRecord, adapter: BrowserAdapter,
        scope: WorkScope, *, observation_error: str = "",
    ) -> dict[str, Any]:
        """Resolve a no-document read only from current native and durable evidence.

        Called with the target lock held. A historic download or an uncertain
        later navigation must never disguise a broken page as success.
        """
        status = await adapter.document_status(target.backend_target_id)
        try:
            self._event(session, "document.readiness_checked", {
                "native_status": status, "expected_url": target.url,
            }, target_id=target.target_id)
        except Exception:
            pass  # Diagnostics cannot change the observation outcome.
        if type(status.get("document_ready")) is not bool or "url" not in status:
            return {}
        operation = self.store.latest_navigation(session.session_id, target.target_id)
        if operation is None or operation.state != "committed":
            return {}
        if status["url"] != target.url and not (
            operation.kind == "new_page" and status["url"] == ""
            and status["document_ready"] is False
        ):
            return {}
        self._assert_scope(operation.scope, scope, mutation=False)
        resulting_page = operation.result.get("page") or {}
        if (operation.before_generation != session.generation
                or resulting_page.get("document_epoch") != target.document_epoch):
            return {}
        await adapter.collect_downloads(target.backend_target_id)
        rows = []
        for record in self.store.list_downloads(session.session_id, limit=1000):
            if (record.target_id != target.target_id or record.url != target.url
                    or record.created_at < operation.created_at
                    or not record.artifact_ref or record.state != "completed"):
                continue
            # New-page downloads can precede assignment of a native operation
            # ID. Their new target identity and creation boundary fence them.
            if record.operation_id != operation.operation_id and not (
                operation.kind == "new_page" and record.operation_id.startswith("browser-event-")
            ):
                continue
            if not work_scope_visible(record.scope, scope):
                continue
            rows.append(record.to_dict())
        if not rows:
            return {}
        # Confirm the native target is still the same non-rendered URL after
        # collecting the artifact; an out-of-band navigation is not success.
        if await adapter.document_status(target.backend_target_id) != status:
            return {}
        return {
            "state": "attachment" if status["document_ready"] is False else "unavailable_after_download",
            "native_document_ready": status["document_ready"],
            "native_url": status["url"], "requested_url": target.url,
            "observation_status": "unavailable", "observation_error": observation_error[:2000],
            "download_state": "completed",
            "operation_id": operation.operation_id, "downloads": rows,
            "message": ("Download completed; this target has no rendered document. Use the downloaded artifact."
                        if status["document_ready"] is False else
                        "Download completed; page inspection is unavailable despite native document readiness. Use the downloaded artifact; no document contents were observed."),
        }

    async def observe(
        self,
        page: PageRef | str,
        *,
        target_id: str = "",
        max_chars: int = 200_000,
        max_elements: int = 1_000,
        include_html: bool = False,
        include_screenshot: bool = False,
        expected: BrowserExpectedState | None = None,
        idempotency_key: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> ObservationRecord:
        session, target = self._resolve_page(page, target_id=target_id)
        resolved = _scope(scope)
        self._assert_scope(session.scope, resolved, mutation=False)
        authority_scope = session.scope if not session.scope.empty else resolved
        max_chars = max(1, min(int(max_chars), 2_000_000))
        max_elements = max(0, min(int(max_elements), 5_000))
        lock = self._target_locks.setdefault((session.session_id, target.target_id), asyncio.Lock())
        async with lock:
            session, adapter = await self._adapter(session.session_id)
            if isinstance(page, PageRef):
                session, target = self._resolve_page(page)
            else:
                target = self.store.get_target(target.target_id)
            self._check_expected(session, target, expected)
            adapter.require("observe")
            request = {
                "kind": "observe", "target_id": target.target_id, "generation": session.generation,
                "document_epoch": target.document_epoch, "max_chars": max_chars,
                "max_elements": max_elements, "include_html": bool(include_html),
                "include_screenshot": bool(include_screenshot),
            }
            operation, replay = self.store.prepare_operation(
                session_id=session.session_id, target_id=target.target_id, kind="observe",
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint("browser.observe", request),
                generation=session.generation, document_epoch=target.document_epoch,
                observation_revision=target.observation_revision, scope=authority_scope,
            )
            if replay:
                if operation.state == "committed":
                    observation_id = str(operation.result.get("observation_id") or "")
                    return self.store.get_observation(observation_id)
                if operation.state in {"prepared", "dispatched", "observed", "unknown_effect"}:
                    raise BrowserUnknownEffect("browser observation replay needs reconciliation")
                raise BrowserFabricError(operation.diagnostic or "browser observation failed")
            self.store.transition_operation(operation.operation_id, "dispatched")
            try:
                try:
                    snapshot = await adapter.observe(
                        target.backend_target_id, max_chars=max_chars, max_elements=max_elements,
                        include_html=bool(include_html), include_screenshot=bool(include_screenshot),
                    )
                except BrowserUnavailable as exc:
                    document = (await self._attachment_document(session, target, adapter, authority_scope,
                                                               observation_error=str(exc))
                                if "GUEST_NOT_READY" in str(exc) else {})
                    if not document:
                        raise
                    snapshot = AdapterObservation(title=target.title, url=target.url, text="", document=document)
                text_artifact = self._artifact(
                    snapshot.text.encode("utf-8"), media_type="text/plain; charset=utf-8",
                    kind="browser_text", session=session, scope=authority_scope,
                ) if snapshot.text else {}
                html_artifact = self._artifact(
                    snapshot.html.encode("utf-8"), media_type="text/html; charset=utf-8",
                    kind="browser_html", session=session, scope=authority_scope,
                ) if snapshot.html else {}
                screenshot_artifact = self._artifact(
                    snapshot.screenshot, media_type="image/png", kind="browser_screenshot",
                    session=session, scope=authority_scope,
                ) if snapshot.screenshot else {}
                observation = self.store.append_observation(
                    session_id=session.session_id, target_id=target.target_id,
                    generation=session.generation, title=snapshot.title, url=snapshot.url,
                    text_excerpt=snapshot.text[:65_536], elements=snapshot.elements,
                    text_artifact_ref=str(text_artifact.get("ref") or ""),
                    html_artifact_ref=str(html_artifact.get("ref") or ""),
                    screenshot_artifact_ref=str(screenshot_artifact.get("ref") or ""),
                    viewport=snapshot.viewport,
                    document=snapshot.document,
                )
                result = {"observation_id": observation.observation_id, "revision": observation.revision}
                self.store.transition_operation(operation.operation_id, "observed", result=result)
                self.store.transition_operation(operation.operation_id, "committed", result=result)
                try:
                    if not snapshot.document:
                        await self.preferences.observation(resolved.chat_id, observation)
                except Exception:
                    pass  # Readiness projection must not change the operation outcome.
                return observation
            except asyncio.CancelledError:
                self.store.transition_operation(
                    operation.operation_id,
                    "failed",
                    diagnostic="browser observation cancelled after dispatch",
                )
                raise
            except Exception as exc:
                self.store.transition_operation(operation.operation_id, "failed", diagnostic=str(exc))
                if isinstance(exc, BrowserUnavailable):
                    await self.preferences.connection_failed(resolved.chat_id, session, exc)
                raise

    def _action_replay_result(self, operation: OperationRecord) -> dict[str, Any]:
        if operation.state == "committed":
            # Refresh the projection from durable completions, never repeat
            # the effect because an earlier reply preceded a download event.
            return {**operation.result, **self._operation_downloads(operation)}
        if operation.state in {"prepared", "dispatched", "observed", "unknown_effect"}:
            raise BrowserUnknownEffect(
                f"browser operation {operation.operation_id} has uncertain effect"
            )
        raise BrowserFabricError(operation.diagnostic or "browser operation failed")

    def _replay_action(
        self, page: PageRef | str, action: str, *, target_id: str,
        element: ElementRef | None, payload: Mapping[str, Any],
        idempotency_key: str, scope: WorkScope,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        reference = element if element is not None else page
        if isinstance(reference, (ElementRef, PageRef)):
            session_id = reference.session_id
            wanted_target = reference.target_id
        else:
            session_id = clean_identifier(reference, "session_id")
            wanted_target = target_id
        operation = self.store.find_operation(session_id, idempotency_key)
        if operation is None:
            return None
        session = self.store.get_session(session_id)
        self._assert_scope(session.scope, scope, mutation=action in _EFFECTFUL_ACTIONS)
        self._assert_scope(operation.scope, scope, mutation=action in _EFFECTFUL_ACTIONS)
        # Rebuild the original request from its admitted browser state. A
        # completed action may have advanced that state or closed its page;
        # neither change alters the identity of an idempotent retry.
        request = {
            "kind": action, "target_id": wanted_target or operation.target_id,
            "generation": operation.before_generation,
            "document_epoch": operation.before_document_epoch,
            "params": dict(payload),
        }
        if operation.request_fingerprint != request_fingerprint(
            f"browser.{action}", request,
        ):
            raise BrowserConflict("idempotency key was used for a different browser request")
        return self._action_replay_result(operation)

    async def perform(
        self,
        page: PageRef | str,
        action: str,
        *,
        target_id: str = "",
        element: ElementRef | None = None,
        params: Mapping[str, Any] | None = None,
        expected: BrowserExpectedState | None = None,
        idempotency_key: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_action = clean_identifier(action, "browser action")
        resolved = _scope(scope)
        payload = dict(json_value(params or {}))
        if clean_action == "keys":
            payload["keys"] = normalize_browser_keys(payload.get("keys"))
        if clean_action == 'set_viewport':
            payload = viewport_request(payload)
        if element is not None:
            payload["backend_ref"] = element.backend_ref
            if isinstance(page, PageRef) and (
                page.session_id != element.session_id or page.target_id != element.target_id
            ):
                raise BrowserConflict("ElementRef belongs to another PageRef")
        replay_result = self._replay_action(
            page, clean_action, target_id=target_id, element=element, payload=payload,
            idempotency_key=idempotency_key, scope=resolved,
        )
        if replay_result is not None:
            return replay_result
        if element is not None:
            session, target = self._resolve_element(element)
        else:
            session, target = self._resolve_page(page, target_id=target_id)
        mutation = clean_action in _EFFECTFUL_ACTIONS
        self._assert_scope(session.scope, resolved, mutation=mutation)
        authority_scope = session.scope if not session.scope.empty else resolved
        lock = self._target_locks.setdefault((session.session_id, target.target_id), asyncio.Lock())
        async with lock:
            replay_result = self._replay_action(
                page, clean_action, target_id=target_id, element=element, payload=payload,
                idempotency_key=idempotency_key, scope=resolved,
            )
            if replay_result is not None:
                return replay_result
            session, adapter = await self._adapter(session.session_id)
            if element is not None:
                # Re-run the stale check after waiting for the target mutation lock.
                session, target = self._resolve_element(element)
            elif isinstance(page, PageRef):
                session, target = self._resolve_page(page)
            else:
                target = self.store.get_target(target.target_id)
            self._check_expected(session, target, expected)
            settings = dict(session.metadata.get('browser_settings') or {})
            if clean_action == 'evaluate' and settings.get('evaluate_enabled') is False:
                raise BrowserValidationError('JavaScript evaluation is disabled for this browser selection.')
            timeout_key = ('click_timeout_s' if clean_action == 'click' else
                           'navigation_timeout_s' if clean_action in {'navigate', 'back', 'forward', 'reload'} else 'command_timeout_s')
            adapter.require(clean_action)
            request = {
                "kind": clean_action, "target_id": target.target_id,
                "generation": session.generation, "document_epoch": target.document_epoch,
                "params": payload,
            }
            operation, replay = self.store.prepare_operation(
                session_id=session.session_id, target_id=target.target_id, kind=clean_action,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint(
                    f"browser.{clean_action}", request
                ),
                generation=session.generation, document_epoch=target.document_epoch,
                observation_revision=target.observation_revision, scope=authority_scope,
            )
            if replay:
                self._assert_scope(operation.scope, resolved, mutation=mutation)
                return self._action_replay_result(operation)
            self.store.transition_operation(operation.operation_id, "dispatched")
            try:
                adapter_params = dict(payload)
                if timeout_key in settings and 'timeout_ms' not in payload and 'timeout' not in payload:
                    adapter_params['timeout_ms'] = settings[timeout_key] * 1000
                adapter_result = await adapter.perform(
                    target.backend_target_id,
                    clean_action,
                    {**adapter_params, "_operation_id": operation.operation_id},
                )
                artifact: dict[str, Any] = {}
                if adapter_result.screenshot:
                    media_type = "image/jpeg" if str(payload.get("type") or "png") == "jpeg" else "image/png"
                    artifact = self._artifact(
                        adapter_result.screenshot, media_type=media_type,
                        kind="browser_screenshot", session=session, scope=authority_scope,
                    )
                if adapter_result.download is not None:
                    stored = self._artifact(
                        adapter_result.download.payload, media_type="application/octet-stream",
                        kind="browser_download", session=session, scope=authority_scope,
                    )
                    self.store.record_download(
                        session_id=session.session_id, target_id=target.target_id,
                        operation_id=operation.operation_id,
                        suggested_filename=adapter_result.download.suggested_filename,
                        url=adapter_result.download.url, artifact_ref=str(stored["ref"]),
                        sha256=str(stored["sha256"]), bytes_count=int(stored["bytes"]),
                        scope=authority_scope,
                    )
                target = self.store.update_target(
                    target.target_id, title=adapter_result.title, url=adapter_result.url,
                    navigated=bool(adapter_result.navigated),
                    invalidate_observation=(
                        clean_action in _EFFECTFUL_ACTIONS
                        and not bool(adapter_result.navigated)
                    ),
                )
                await self._sync_targets(
                    session.session_id, adapter_result.targets,
                    navigation_target="",  # target epoch was advanced exactly once above
                )
                session = self.store.get_session(session.session_id)
                target = self.store.get_target(target.target_id)
                result = {
                    "schema": "variant1.browser-action-result.v1",
                    "operation_id": operation.operation_id,
                    "action": clean_action,
                    "value": json_value(adapter_result.value),
                    "page": target.page_ref(session.generation).to_dict(),
                    "artifact": artifact or None,
                    **self._operation_downloads(operation),
                    'viewport': dict(target.viewport),
                    **(image_dimensions(adapter_result.screenshot) if adapter_result.screenshot else {}),
                }
                self.store.transition_operation(operation.operation_id, "observed", result=result)
                self.store.transition_operation(operation.operation_id, "committed", result=result)
                return result
            except asyncio.CancelledError:
                state = (
                    "unknown_effect"
                    if clean_action in _EFFECTFUL_ACTIONS else "failed"
                )
                diagnostic = "browser action cancelled after dispatch"
                self.store.transition_operation(
                    operation.operation_id, state, diagnostic=diagnostic
                )
                raise
            except BrowserStaleReference as exc:
                # Managed adapters validate their durable locator before the
                # effect. A missing same-document element is a known stale
                # precondition, not an uncertain click/fill outcome.
                self.store.transition_operation(
                    operation.operation_id, "failed", diagnostic=str(exc)
                )
                raise
            except Exception as exc:
                uncertain = clean_action in _EFFECTFUL_ACTIONS
                state = "unknown_effect" if uncertain else "failed"
                self.store.transition_operation(operation.operation_id, state, diagnostic=str(exc))
                if isinstance(exc, BrowserUnavailable):
                    await self.preferences.connection_failed(resolved.chat_id, session, exc)
                if uncertain:
                    raise BrowserUnknownEffect(
                        f"{clean_action} was dispatched but its final browser effect is unknown: {exc}"
                    ) from exc
                raise

    async def navigate(self, page: PageRef | str, url: str, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        params["url"] = str(url)
        return await self.perform(page, "navigate", params=params, **controls)

    async def back(self, page: PageRef | str, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        return await self.perform(page, "back", params=params, **controls)

    async def forward(self, page: PageRef | str, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        return await self.perform(page, "forward", params=params, **controls)

    async def reload(self, page: PageRef | str, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        return await self.perform(page, "reload", params=params, **controls)

    async def click(self, element: ElementRef, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        return await self.perform(
            PageRef(element.session_id, element.target_id, element.generation, 0,
                    element.document_epoch, element.observation_revision),
            "click", element=element, params=params, **controls,
        )

    async def fill(self, element: ElementRef, text: str, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        params["text"] = str(text)
        return await self.perform(
            PageRef(element.session_id, element.target_id, element.generation, 0,
                    element.document_epoch, element.observation_revision),
            "fill", element=element, params=params, **controls,
        )

    async def select(self, element: ElementRef, values: str | Sequence[str], **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        params["values"] = values
        return await self.perform(
            PageRef(element.session_id, element.target_id, element.generation, 0,
                    element.document_epoch, element.observation_revision),
            "select", element=element, params=params, **controls,
        )

    async def hover(self, element: ElementRef, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        return await self.perform(
            PageRef(element.session_id, element.target_id, element.generation, 0,
                    element.document_epoch, element.observation_revision),
            "hover", element=element, params=params, **controls,
        )

    async def keys(
        self, page: PageRef | str, keys: str, *, element: ElementRef | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        params["keys"] = keys
        return await self.perform(page, "keys", element=element, params=params, **controls)

    async def evaluate(
        self, page: PageRef | str, expression: str, *, arg: Any = None, **kwargs: Any,
    ) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        params.update({"expression": expression, "arg": json_value(arg)})
        return await self.perform(
            page, "evaluate", params=params, **controls,
        )

    async def wait(self, page: PageRef | str, *, condition: str = "timeout", **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        params["condition"] = condition
        return await self.perform(page, "wait", params=params, **controls)

    async def screenshot(self, page: PageRef | str, **kwargs: Any) -> dict[str, Any]:
        params, controls = _action_arguments(kwargs)
        return await self.perform(page, "screenshot", params=params, **controls)

    async def new_page(
        self,
        session_id: str,
        *,
        url: str = "",
        idempotency_key: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
        _only_if_empty: bool = False,
    ) -> PageRef:
        await self._adapter(session_id)
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self.store.get_session(session_id)
            adapter = self._adapters.get(session_id)
            if adapter is None or session.state != "active":
                raise BrowserUnavailable("browser session stopped before the tab command")
            resolved = _scope(scope)
            self._assert_scope(session.scope, resolved, mutation=True)
            if _only_if_empty and self.store.list_targets(session_id):
                return self.page_ref(session_id)
            authority_scope = session.scope if not session.scope.empty else resolved
            adapter.require("tabs")
            request_hash = request_fingerprint(
                "browser.new_page", {"kind": "new_page", "url": url}
            )
            existing: OperationRecord | None = None
            if idempotency_key:
                # prepare_operation needs a durable target; use the current page
                # for the ledger until the new target identity is allocated.
                anchor = self.store.get_target(session.current_target_id)
                existing, replay = self.store.prepare_operation(
                    session_id=session_id, target_id=anchor.target_id, kind="new_page",
                    idempotency_key=idempotency_key, request_fingerprint=request_hash,
                    generation=session.generation, document_epoch=anchor.document_epoch,
                    observation_revision=anchor.observation_revision, scope=authority_scope,
                )
                if replay:
                    if existing.state == "committed":
                        return self.page_ref(session_id, str(existing.result.get("target_id") or ""))
                    raise BrowserUnknownEffect("new-page replay has an uncertain effect")
            target_id = new_id("page")
            target = self.store.create_target(
                target_id=target_id, session_id=session_id, backend_target_id=target_id,
                url=url, state="opening",
            )
            operation = existing
            if operation is None:
                operation, _ = self.store.prepare_operation(
                    session_id=session_id, target_id=target_id, kind="new_page",
                    idempotency_key="", request_fingerprint=request_hash,
                    generation=session.generation, document_epoch=target.document_epoch,
                    observation_revision=0, scope=authority_scope,
                )
            self.store.transition_operation(operation.operation_id, "dispatched")
            try:
                live = await adapter.new_page(target.backend_target_id, url)
                target = self.store.update_target(
                    target_id, state="active", title=live.title, url=live.url,
                    navigated=bool(url),
                )
                session = self.store.update_session(session_id, current_target_id=target_id)
                result = {"target_id": target_id, "page": target.page_ref(session.generation).to_dict()}
                self.store.transition_operation(operation.operation_id, "observed", result=result)
                self.store.transition_operation(operation.operation_id, "committed", result=result)
                self._event(session, "target.opened", result, target_id=target_id)
                return target.page_ref(session.generation)
            except asyncio.CancelledError:
                self.store.update_target(
                    target_id, state="orphaned",
                    last_error="new page cancelled after dispatch",
                )
                self.store.transition_operation(
                    operation.operation_id, "unknown_effect",
                    diagnostic="new page cancelled after dispatch",
                )
                raise
            except Exception as exc:
                self.store.update_target(target_id, state="orphaned", last_error=str(exc))
                self.store.transition_operation(operation.operation_id, "unknown_effect", diagnostic=str(exc))
                raise BrowserUnknownEffect(f"new page effect is unknown: {exc}") from exc

    async def select_page(
        self, page: PageRef, *, scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> PageRef:
        session, target = self._resolve_page(page)
        resolved = _scope(scope)
        self._assert_scope(session.scope, resolved, mutation=True)
        authority_scope = session.scope if not session.scope.empty else resolved
        await self._adapter(session.session_id)
        target_lock = self._target_locks.setdefault((session.session_id, target.target_id), asyncio.Lock())
        session_lock = self._session_locks.setdefault(session.session_id, asyncio.Lock())
        async with target_lock:
            async with session_lock:
                session, target = self._resolve_page(page)
                adapter = self._adapters.get(session.session_id)
                if adapter is None or session.state != "active":
                    raise BrowserUnavailable("browser session stopped before the tab command")
                adapter.require("tabs")
                request = {"kind": "select_page", "target_id": target.target_id}
                operation, _ = self.store.prepare_operation(
                    session_id=session.session_id, target_id=target.target_id, kind="select_page",
                    idempotency_key="",
                    request_fingerprint=request_fingerprint(
                        "browser.select_page", request
                    ),
                    generation=session.generation, document_epoch=target.document_epoch,
                    observation_revision=target.observation_revision, scope=authority_scope,
                )
                self.store.transition_operation(operation.operation_id, "dispatched")
                try:
                    live = await adapter.activate_page(target.backend_target_id)
                    target = self.store.update_target(target.target_id, title=live.title, url=live.url)
                    session = self.store.update_session(
                        session.session_id, current_target_id=target.target_id
                    )
                    result = {"target_id": target.target_id,
                              "page": target.page_ref(session.generation).to_dict()}
                    self.store.transition_operation(operation.operation_id, "observed", result=result)
                    self.store.transition_operation(operation.operation_id, "committed", result=result)
                    self._event(session, "target.selected", result, target_id=target.target_id)
                    return target.page_ref(session.generation)
                except asyncio.CancelledError:
                    self.store.transition_operation(
                        operation.operation_id, "unknown_effect",
                        diagnostic="select page cancelled after dispatch",
                    )
                    raise
                except Exception as exc:
                    self.store.transition_operation(
                        operation.operation_id, "unknown_effect", diagnostic=str(exc)
                    )
                    raise BrowserUnknownEffect(f"select-page effect is unknown: {exc}") from exc

    async def close_page(
        self, page: PageRef, *, scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> None:
        session, target = self._resolve_page(page)
        resolved = _scope(scope)
        self._assert_scope(session.scope, resolved, mutation=True)
        authority_scope = session.scope if not session.scope.empty else resolved
        await self._adapter(session.session_id)
        target_lock = self._target_locks.setdefault((session.session_id, target.target_id), asyncio.Lock())
        session_lock = self._session_locks.setdefault(session.session_id, asyncio.Lock())
        async with target_lock:
            async with session_lock:
                session, target = self._resolve_page(page)
                adapter = self._adapters.get(session.session_id)
                if adapter is None or session.state != "active":
                    raise BrowserUnavailable("browser session stopped before the tab command")
                adapter.require("tabs")
                request = {"kind": "close_page", "target_id": target.target_id}
                operation, _ = self.store.prepare_operation(
                    session_id=session.session_id, target_id=target.target_id, kind="close_page",
                    idempotency_key="",
                    request_fingerprint=request_fingerprint(
                        "browser.close_page", request
                    ),
                    generation=session.generation, document_epoch=target.document_epoch,
                    observation_revision=target.observation_revision, scope=authority_scope,
                )
                self.store.transition_operation(operation.operation_id, "dispatched")
                try:
                    await adapter.close_page(target.backend_target_id)
                    target = self.store.update_target(target.target_id, state="closed")
                    await self._sync_targets(session.session_id, await adapter.targets())
                    session = self.store.get_session(session.session_id)
                    result = {"target_id": target.target_id, "state": "closed"}
                    self.store.transition_operation(operation.operation_id, "observed", result=result)
                    self.store.transition_operation(operation.operation_id, "committed", result=result)
                    self._event(session, "target.closed", result, target_id=target.target_id)
                except asyncio.CancelledError:
                    self.store.transition_operation(
                        operation.operation_id, "unknown_effect",
                        diagnostic="close page cancelled after dispatch",
                    )
                    raise
                except Exception as exc:
                    self.store.transition_operation(
                        operation.operation_id, "unknown_effect", diagnostic=str(exc)
                    )
                    raise BrowserUnknownEffect(f"close-page effect is unknown: {exc}") from exc

    async def start_trace(
        self,
        session_id: str,
        *,
        name: str = "Browser trace",
        options: Mapping[str, Any] | None = None,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> TraceRecord:
        session, adapter = await self._adapter(session_id)
        resolved = _scope(scope)
        self._assert_scope(session.scope, resolved, mutation=True)
        authority_scope = session.scope if not session.scope.empty else resolved
        adapter.require("trace")
        if session_id in self._active_traces.values():
            raise BrowserConflict("this browser session is already recording a trace")
        trace = self.store.create_trace(session_id=session_id, name=name, scope=authority_scope)
        try:
            await adapter.start_trace(options or {})
            trace = self.store.mark_trace_recording(trace.trace_id)
            self._active_traces[trace.trace_id] = session_id
            return trace
        except asyncio.CancelledError:
            trace = self.store.finish_trace(
                trace.trace_id, state="failed",
                diagnostic="trace start cancelled",
            )
            raise
        except Exception as exc:
            trace = self.store.finish_trace(trace.trace_id, state="failed", diagnostic=str(exc))
            raise

    async def stop_trace(
        self, trace_id: str, *, scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> TraceRecord:
        trace = self.store.get_trace(trace_id)
        session, adapter = await self._adapter(trace.session_id)
        resolved = _scope(scope)
        self._assert_scope(trace.scope, resolved, mutation=True)
        if self._active_traces.get(trace_id) != trace.session_id:
            raise BrowserConflict("trace is not active in this process generation")
        fd, temporary = tempfile.mkstemp(prefix="variant1-browser-trace-", suffix=".zip")
        os.close(fd)
        try:
            await adapter.stop_trace(temporary)
            trace_limit = 1024 * 1024 * 1024
            trace_size = await asyncio.to_thread(os.path.getsize, temporary)
            if trace_size > trace_limit:
                raise BrowserValidationError("browser trace exceeds 1 GiB")
            ingest = asyncio.create_task(asyncio.to_thread(
                self.artifact_store.put_file,
                temporary,
                media_type="application/zip",
                kind="browser_trace",
                scope=_scope_grant(session.scope, session.session_id),
                max_bytes=trace_limit,
            ))
            cancellation = None
            while not ingest.done():
                try:
                    await asyncio.shield(ingest)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
                    continue
            stored = ingest.result()
            artifact = {
                "ref": str(stored.ref),
                "sha256": str(stored.sha256),
                "bytes": int(stored.bytes),
                "media_type": "application/zip",
                "kind": "browser_trace",
            }
            if cancellation is not None:
                raise cancellation
            trace = self.store.finish_trace(
                trace_id, state="completed", artifact_ref=str(artifact["ref"]),
                sha256=str(artifact["sha256"]), bytes_count=int(artifact["bytes"]),
            )
            return trace
        except asyncio.CancelledError:
            trace = self.store.finish_trace(
                trace_id, state="failed", diagnostic="trace stop cancelled",
            )
            raise
        except Exception as exc:
            trace = self.store.finish_trace(trace_id, state="failed", diagnostic=str(exc))
            raise
        finally:
            self._active_traces.pop(trace_id, None)
            try:
                os.remove(temporary)
            except OSError:
                pass

    async def refresh_downloads(self, session_id: str) -> None:
        # Reading history must not launch/reopen a browser. A live native guest
        # can finish downloading after its initiating action has returned.
        adapter = self._adapters.get(session_id)
        if adapter is None:
            return
        for target in self.store.list_targets(session_id, include_closed=False):
            lock = self._target_locks.setdefault((session_id, target.target_id), asyncio.Lock())
            async with lock:
                await adapter.collect_downloads(target.backend_target_id)

    def downloads(self, session_id: str, *, limit: int = 100) -> tuple[DownloadRecord, ...]:
        return self.store.list_downloads(session_id, limit=limit)

    def download_history(
        self, session_id: str, *, limit: int = 100, operation_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        resolved = _scope(scope)
        self._assert_scope(self.session(session_id).scope, resolved, mutation=False)
        if operation_id:
            operation = self.store.get_operation(operation_id)
            if operation.session_id != session_id:
                raise BrowserScopeMismatch("download operation belongs to another browser session")
            self._assert_scope(operation.scope, resolved, mutation=False)
        rows = []
        for record in self.store.list_downloads(session_id, limit=limit, operation_id=operation_id):
            try:
                self._assert_scope(record.scope, resolved, mutation=False)
            except BrowserScopeMismatch:
                continue
            rows.append(record.to_dict())
        known = {row["download_id"] for row in rows}
        adapter = self._adapters.get(session_id)
        if adapter is not None:
            for target in self.store.list_targets(session_id, include_closed=False):
                for progress in adapter.download_progress(target.backend_target_id):
                    owner_id = str(progress.get("operation_id") or "")
                    if operation_id and owner_id != operation_id:
                        continue
                    # Native progress belongs to the operation that initiated
                    # it, even when another chat later inspects this guest.
                    if not owner_id.startswith("browserop_"):
                        continue
                    try:
                        owner = self.store.get_operation(owner_id)
                        if owner.session_id != session_id or owner.target_id != target.target_id:
                            continue
                        self._assert_scope(owner.scope, resolved, mutation=False)
                    except (BrowserNotFound, BrowserScopeMismatch):
                        continue
                    native_id = str(progress.get("download_id") or "")
                    download_id = "download_" + hashlib.sha256(
                        (session_id + ":" + native_id).encode()
                    ).hexdigest()[:32]
                    if not native_id or download_id in known or progress.get("status") == "stored":
                        continue
                    registration_error = (
                        self._download_registration_errors.get((session_id, native_id))
                        if progress.get("status") == "completed" else None
                    )
                    failure = ({
                        "state": "failed", "status": "failed",
                        "native_status": str(progress.get("status") or ""),
                        "error": "Browser download registration failed: " + registration_error["error"],
                        "diagnostic_event_id": registration_error["event_id"],
                        "registration_attempts": registration_error["attempts"],
                        "recovery": "The file remains in native staging. Resolve the registration error, then retry this session's download history; do not repeat the download.",
                    } if registration_error else {})
                    rows.insert(0, {
                        **progress, "download_id": download_id,
                        "session_id": session_id, "target_id": target.target_id,
                        "state": {"completed": "finalizing", "error": "failed"}.get(
                            progress.get("status"), progress.get("status", "in_progress"),
                        ),
                        "artifact_ref": None,
                        **failure,
                    })
        return rows[:max(1, min(int(limit), 1000))]

    def _operation_downloads(self, operation: OperationRecord) -> dict[str, Any]:
        rows = self.download_history(
            operation.session_id, operation_id=operation.operation_id, scope=operation.scope,
        )
        completed = [row for row in rows if row.get("artifact_ref")]
        states = {str(row.get("state") or "") for row in rows}
        state = next((value for value in (
            "in_progress", "finalizing", "interrupted", "cancelled", "failed", "completed",
        ) if value in states), "not_observed_yet")
        return {
            "download": completed[0] if completed else None,
            "downloads": rows, "download_state": state,
        }

    def traces(self, session_id: str, *, limit: int = 100) -> tuple[TraceRecord, ...]:
        return self.store.list_traces(session_id, limit=limit)

    def operations(self, session_id: str, *, limit: int = 200) -> tuple[OperationRecord, ...]:
        return self.store.list_operations(session_id, limit=limit)

    def events(
        self, session_id: str, *, after_sequence: int = 0, limit: int = 200,
    ) -> tuple[Any, ...]:
        return self.store.events(session_id, after_sequence=after_sequence, limit=limit)

    async def close_session(
        self,
        session_id: str,
        *,
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> SessionRecord:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self.store.get_session(session_id)
            resolved = _scope(scope)
            self._assert_scope(session.scope, resolved, mutation=True)
            if self.shared_embedded_session(session) and not resolved.empty:
                self._event(
                    session,
                    "session.lease_released",
                    {"shared_embedded": True},
                    target_id=session.current_target_id,
                )
                return session
            adapter = self._adapters.get(session_id)
            cancellation = None
            if adapter is not None:
                if session.kind == "embedded" and session.scope.chat_id:
                    # Explicit chat/session deletion closes only that owner's tabs.
                    for live in await adapter.targets():
                        await adapter.close_page(live.backend_target_id)
                cancellation = await self._close_adapter_owner(session_id, adapter)
            for target in self.store.list_targets(session_id):
                self.store.update_target(target.target_id, state="closed")
            session = self.store.update_session(session_id, state="closed", current_target_id="")
            self._event(session, "session.closed", {"generation": session.generation})
            if cancellation is not None:
                raise cancellation
            return session

    async def delete_chat(self, chat_id: str) -> int:
        """Close every Browser-core session owned by a deleted chat scope."""

        await self.preferences.delete_chat(chat_id)
        session_ids = self.store.session_ids_for_chat(chat_id)
        if not session_ids:
            return 0

        async def close_one(session_id: str):
            session = self.store.get_session(session_id)
            return await self.close_session(session_id, scope=session.scope)

        outcomes = await asyncio.gather(
            *(close_one(session_id) for session_id in session_ids),
            return_exceptions=True,
        )
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        if failures:
            raise BrowserUnavailable(
                f"browser chat cleanup left {len(failures)} session(s) retryable"
            ) from failures[0]
        return len(session_ids)

    async def shutdown(self) -> None:
        await self.preferences.close()
        async with self._lifecycle_lock:
            self._shutting_down = True
            session_ids = list(self._adapters)

        async def close_one(session_id: str):
            lock = self._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                adapter = self._adapters.get(session_id)
                if adapter is not None:
                    await self._close_adapter_owner(session_id, adapter)

        async def close_all():
            return await asyncio.gather(
                *(close_one(session_id) for session_id in session_ids),
                return_exceptions=True,
            )

        cleanup = asyncio.create_task(close_all())
        cancellation = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                continue
        failures = [item for item in cleanup.result() if isinstance(item, BaseException)]
        if failures:
            raise BrowserUnavailable(
                f"browser shutdown left {len(failures)} adapter owner(s) retryable"
            ) from failures[0]
        if cancellation is not None:
            raise cancellation

    def startup(self) -> dict[str, Any]:
        # Live objects cannot survive a process restart. Preserve durable rows
        # and mark interrupted traces; sessions recover lazily with a bumped
        # generation when first reacquired.
        operations = self.store.reconcile_interrupted_operations()
        interrupted_traces = self.store.reconcile_recording_traces()
        return {
            "sessions": self.store.count_sessions(include_closed=False),
            "profiles": self.store.count_profiles(),
            "interrupted_traces": interrupted_traces,
            "operations_reconciled": operations,
            "database": self.store.path,
            "profile_root": self.profile_root,
        }

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "database": self.store.path,
            "profile_root": self.profile_root,
            "live_sessions": len(self._adapters),
            "durable_sessions": self.store.count_sessions(include_closed=False),
            "adapters": self.capability_matrix(),
        }


def create_browser_fabric(
    *,
    path: str | None = None,
    data_dir: str | None = None,
    profile_root: str | None = None,
    artifact_store: Any | None = None,
    artifact_root: str | None = None,
    embedded_request: Callable[[dict[str, Any]], Any] | None = None,
    playwright_factory: Callable[[], Any] | None = None,
    adapter_factory: Callable[[str, ProfileRecord, SessionRecord], BrowserAdapter] | None = None,
    managed_launch_options: Mapping[str, Any] | None = None,
    cloud_credential_resolver: Callable | None = None,
) -> BrowserFabric:
    """Build a Browser Fabric without mutating host composition or tool catalogs."""

    store = BrowserFabricStore(path=path, data_dir=data_dir)
    managed_root = os.path.abspath(profile_root or default_profile_root(data_dir=data_dir))
    if artifact_store is None:
        cas_root = os.path.abspath(
            artifact_root or os.path.join(os.path.dirname(store.path), "artifacts")
        )
        artifact_store = ContentAddressedArtifactStore(cas_root)

    if adapter_factory is None:
        def adapter_factory(
            kind: str, profile: ProfileRecord, session: SessionRecord,
        ) -> BrowserAdapter:
            if kind == "managed":
                settings = {**dict(session.metadata.get('browser_settings') or {}), '_recording_id': session.session_id}
                cloud_lease = None
                if settings.get('mode') == 'cloud':
                    from .cloud import CloudBrowserLease
                    cloud_lease = CloudBrowserLease(settings, profile.user_data_dir, cloud_credential_resolver)
                return ManagedPlaywrightAdapter(
                    profile.user_data_dir, headless=session.headless,
                    playwright_factory=playwright_factory,
                    launch_options=managed_launch_options,
                    personal_source=profile.metadata.get('personal_source'),
                    browser_settings=settings, cloud_lease=cloud_lease,
                )
            if kind == "embedded":
                adapter = EmbeddedBrowserAdapter(request=embedded_request, owner_chat_id=session.scope.chat_id)
                if (session.metadata.get('browser_settings') or {}).get('evaluate_enabled') is False:
                    adapter.capabilities = adapter.capabilities - {'evaluate'}
                return adapter
            supported = ", ".join(sorted(BROWSER_KINDS))
            raise BrowserValidationError(
                f"unknown browser kind {kind!r}; supported kinds: {supported}"
            )

    return BrowserFabric(
        store, profile_root=managed_root, artifact_store=artifact_store,
        adapter_factory=adapter_factory,
        download_staging_root=os.path.join(data_dir, "browser", "download-staging") if data_dir else None,
    )


__all__ = ["BrowserFabric", "create_browser_fabric"]
