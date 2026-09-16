"""Run-to-browser identity binding for VARIANT-1's persistent CPython core.

The Browser Fabric repository and live adapter map are the only browser state
authorities.  A binding is deliberately small: it lets a run checkpoint point
back to one authoritative Fabric session, or remember a starting URL when a
child has not opened its isolated session yet.  It never owns Playwright,
pages, observations, tabs, cleanup tasks, or a second live registry.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
import uuid
from typing import Any, Iterator, Mapping

from work_fabric.scope import WorkScope, coerce_work_scope, scoped_owner_id


BROWSER_BINDING_SCHEMA = "variant1.browser-binding.v1"


def _new_binding_id() -> str:
    return "browser_binding_" + uuid.uuid4().hex


@dataclass(slots=True)
class BrowserBinding:
    """Checkpoint-safe pointer to Browser Fabric state."""

    binding_id: str = field(default_factory=_new_binding_id)
    fabric_session_id: str = ""
    parent_binding_id: str = ""
    owner_kind: str = "run"
    owner_id: str = ""
    scope: WorkScope = field(default_factory=WorkScope)
    resume_url: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.binding_id = str(self.binding_id or _new_binding_id())
        self.fabric_session_id = str(self.fabric_session_id or "")
        self.parent_binding_id = str(self.parent_binding_id or "")
        self.owner_kind = (
            "chat" if str(self.owner_kind or "").casefold() == "chat" else "run"
        )
        self.owner_id = str(self.owner_id or "")
        self.scope = coerce_work_scope(self.scope)
        self.owner_id = scoped_owner_id(self.owner_kind, self.owner_id, self.scope)
        self.resume_url = str(self.resume_url or "")[:16_000]

    @property
    def run_owned(self) -> bool:
        return self.owner_kind == "run"

    def attach(self, session_id: str, *, resume_url: str = "") -> None:
        self.fabric_session_id = str(session_id or "")
        if resume_url:
            self.resume_url = str(resume_url)[:16_000]
        self.updated_at = time.time()

    def set_scope(self, scope: WorkScope | Mapping[str, Any] | None) -> None:
        self.scope = coerce_work_scope(scope)
        self.owner_id = scoped_owner_id(self.owner_kind, self.owner_id, self.scope)
        self.updated_at = time.time()

    def detach(self, *, resume_url: str = "") -> None:
        self.fabric_session_id = ""
        if resume_url:
            self.resume_url = str(resume_url)[:16_000]
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BROWSER_BINDING_SCHEMA,
            "binding_id": self.binding_id,
            "fabric_session_id": self.fabric_session_id or None,
            "parent_binding_id": self.parent_binding_id or None,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id or None,
            "scope": self.scope.to_dict(include_empty=False),
            "resume_url": self.resume_url or None,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        source: str = "",
        owner_id: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> "BrowserBinding":
        raw = dict(value or {})
        effective_scope = coerce_work_scope(scope or raw.get("scope"))
        effective_owner = "chat" if str(source or "").casefold() == "chat" else (
            str(raw.get("owner_kind") or "run")
        )
        return cls(
            binding_id=str(raw.get("binding_id") or _new_binding_id()),
            fabric_session_id=str(raw.get("fabric_session_id") or ""),
            parent_binding_id=str(raw.get("parent_binding_id") or ""),
            owner_kind=effective_owner,
            owner_id=str(owner_id or raw.get("owner_id") or ""),
            scope=effective_scope,
            resume_url=str(raw.get("resume_url") or ""),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
        )


CURRENT_BROWSER_BINDING: ContextVar[BrowserBinding | None] = ContextVar(
    "variant1_browser_binding",
    default=None,
)


def current_browser_binding() -> BrowserBinding | None:
    return CURRENT_BROWSER_BINDING.get()


def ensure_browser_binding(
    snapshot: Mapping[str, Any] | None = None,
    *,
    source: str = "",
    owner_id: str = "",
    scope: WorkScope | Mapping[str, Any] | None = None,
) -> BrowserBinding:
    current = CURRENT_BROWSER_BINDING.get()
    raw = dict(snapshot or {})
    requested_id = str(raw.get("binding_id") or "")
    # An explicit checkpoint is authoritative.  In particular, a delegated
    # worker is commonly constructed while its parent's ContextVar is still
    # bound; blindly returning that parent would make both runs drive and own
    # the same browser session.  Reuse the live object only when the checkpoint
    # identifies that exact binding.
    if current is not None and (not raw or requested_id == current.binding_id):
        return current
    return BrowserBinding.from_mapping(
        raw,
        source=source,
        owner_id=owner_id,
        scope=scope,
    )


@contextmanager
def bind_browser_binding(binding: BrowserBinding) -> Iterator[BrowserBinding]:
    token = CURRENT_BROWSER_BINDING.set(binding)
    try:
        yield binding
    finally:
        CURRENT_BROWSER_BINDING.reset(token)


def _authoritative_url(binding: BrowserBinding, fabric: Any) -> str:
    session_id = str(binding.fabric_session_id or "")
    if not session_id or fabric is None:
        return binding.resume_url
    try:
        session = fabric.session(session_id, scope=binding.scope)
        target_id = str(session.current_target_id or "")
        if target_id:
            target = fabric.store.get_target(target_id)
            if target.session_id == session_id and target.state != "closed":
                return str(target.url or binding.resume_url)
    except Exception:
        pass
    return binding.resume_url


def browser_binding_snapshot(
    binding: BrowserBinding | None = None,
    *,
    fabric: Any = None,
) -> dict[str, Any]:
    current = binding or CURRENT_BROWSER_BINDING.get()
    if current is None:
        return {}
    if fabric is None:
        from .access import current_browser_fabric

        fabric = current_browser_fabric()
    current.resume_url = _authoritative_url(current, fabric)
    current.updated_at = time.time()
    return current.to_dict()


def create_child_browser_binding_snapshot(
    *,
    scope: WorkScope | Mapping[str, Any] | None = None,
    inherit_target: bool = True,
    fabric: Any = None,
) -> dict[str, Any]:
    parent = CURRENT_BROWSER_BINDING.get()
    if fabric is None:
        from .access import current_browser_fabric

        fabric = current_browser_fabric()
    child = BrowserBinding(
        parent_binding_id=(parent.binding_id if parent is not None else ""),
        owner_kind="run",
        scope=coerce_work_scope(scope),
        resume_url=(
            _authoritative_url(parent, fabric)
            if parent is not None and inherit_target
            else ""
        ),
    )
    return child.to_dict()


async def close_browser_binding(
    binding: BrowserBinding | None,
    *,
    fabric: Any = None,
    force: bool = False,
) -> None:
    """Close the actual run-owned Fabric session despite repeated cancellation."""

    if binding is None or (not force and not binding.run_owned):
        return
    session_id = str(binding.fabric_session_id or "")
    if not session_id:
        return
    if fabric is None:
        from .access import current_browser_fabric

        fabric = current_browser_fabric()
    if fabric is None:
        return
    try:
        session = fabric.session(session_id, scope=binding.scope)
    except Exception:
        binding.detach()
        return
    if session.state == "closed":
        binding.detach()
        return
    session_binding_id = str((session.metadata or {}).get("binding_id") or "")
    session_owner_kind = str((session.metadata or {}).get("owner_kind") or "")
    session_owner_id = str((session.metadata or {}).get("owner_id") or "")
    same_stable_owner = bool(
        session_owner_kind
        and session_owner_id
        and session_owner_kind == binding.owner_kind
        and session_owner_id == binding.owner_id
    )
    if (
        (session_binding_id or session_owner_kind or session_owner_id)
        and session_binding_id != binding.binding_id
        and not same_stable_owner
    ):
        # A binding can inspect or intentionally select another session, but
        # only the binding recorded at creation owns its lifecycle.
        binding.detach(resume_url=_authoritative_url(binding, fabric))
        return

    cleanup = asyncio.create_task(
        fabric.close_session(session_id, scope=binding.scope),
        name=f"browser-binding-close:{binding.binding_id}",
    )
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            continue
    try:
        cleanup.result()
    finally:
        binding.detach(resume_url=_authoritative_url(binding, fabric))
    if cancellation is not None:
        raise cancellation


__all__ = [
    "BROWSER_BINDING_SCHEMA",
    "BrowserBinding",
    "CURRENT_BROWSER_BINDING",
    "bind_browser_binding",
    "browser_binding_snapshot",
    "close_browser_binding",
    "create_child_browser_binding_snapshot",
    "current_browser_binding",
    "ensure_browser_binding",
]
