"""Run-to-desktop identity binding for VARIANT-1's persistent CPython core.

Desktop Fabric and the operating system remain the only desktop authorities.
This checkpoint-safe binding remembers only which durable Fabric window a run
last selected and its bounded focus history.  It never owns UIA controls,
screenshots, observations, input state, callbacks, or a second live registry.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
import uuid
from typing import Any, Iterator, Mapping

from work_fabric.scope import WorkScope, coerce_work_scope, scoped_owner_id


DESKTOP_BINDING_SCHEMA = "variant1.desktop-binding.v1"
_HISTORY_LIMIT = 16


def _new_binding_id() -> str:
    return "desktop_binding_" + uuid.uuid4().hex


def _window_id(value: Any) -> str:
    return str(value or "").strip()[:1024]


@dataclass(slots=True)
class DesktopBinding:
    """Checkpoint-safe pointer and focus history for Desktop Fabric."""

    binding_id: str = field(default_factory=_new_binding_id)
    active_window_id: str = ""
    focus_history: list[str] = field(default_factory=list)
    parent_binding_id: str = ""
    owner_kind: str = "run"
    owner_id: str = ""
    scope: WorkScope = field(default_factory=WorkScope)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.binding_id = str(self.binding_id or _new_binding_id())
        self.active_window_id = _window_id(self.active_window_id)
        self.parent_binding_id = str(self.parent_binding_id or "")
        self.owner_kind = (
            "chat" if str(self.owner_kind or "").casefold() == "chat" else "run"
        )
        self.owner_id = str(self.owner_id or "")
        self.scope = coerce_work_scope(self.scope)
        self.owner_id = scoped_owner_id(self.owner_kind, self.owner_id, self.scope)
        seen: set[str] = set()
        clean: list[str] = []
        for raw in self.focus_history:
            value = _window_id(raw)
            if not value or value == self.active_window_id or value in seen:
                continue
            seen.add(value)
            clean.append(value)
            if len(clean) >= _HISTORY_LIMIT:
                break
        self.focus_history = clean

    def attach(self, window_id: Any) -> None:
        value = _window_id(window_id)
        if not value:
            return
        if value != self.active_window_id:
            previous = self.active_window_id
            history = [item for item in self.focus_history if item != value]
            if previous:
                history.insert(0, previous)
            self.focus_history = history[:_HISTORY_LIMIT]
            self.active_window_id = value
        self.updated_at = time.time()

    def forget(self, window_id: Any) -> None:
        value = _window_id(window_id)
        if value == self.active_window_id:
            self.active_window_id = ""
        self.focus_history = [item for item in self.focus_history if item != value]
        self.updated_at = time.time()

    def set_scope(self, scope: WorkScope | Mapping[str, Any] | None) -> None:
        self.scope = coerce_work_scope(scope)
        self.owner_id = scoped_owner_id(self.owner_kind, self.owner_id, self.scope)
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DESKTOP_BINDING_SCHEMA,
            "binding_id": self.binding_id,
            "active_window_id": self.active_window_id or None,
            "focus_history": list(self.focus_history),
            "parent_binding_id": self.parent_binding_id or None,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id or None,
            "scope": self.scope.to_dict(include_empty=False),
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
    ) -> "DesktopBinding":
        raw = dict(value or {})
        effective_scope = coerce_work_scope(scope or raw.get("scope"))
        effective_owner = (
            "chat" if str(source or "").casefold() == "chat"
            else str(raw.get("owner_kind") or "run")
        )
        return cls(
            binding_id=str(raw.get("binding_id") or _new_binding_id()),
            active_window_id=str(
                raw.get("active_window_id")
                or (raw.get("active_window") or {}).get("id")
                or ""
            ),
            focus_history=list(raw.get("focus_history") or ()),
            parent_binding_id=str(raw.get("parent_binding_id") or ""),
            owner_kind=effective_owner,
            owner_id=str(owner_id or raw.get("owner_id") or ""),
            scope=effective_scope,
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
        )


CURRENT_DESKTOP_BINDING: ContextVar[DesktopBinding | None] = ContextVar(
    "variant1_desktop_binding", default=None,
)


def current_desktop_binding() -> DesktopBinding | None:
    return CURRENT_DESKTOP_BINDING.get()


def ensure_desktop_binding(
    snapshot: Mapping[str, Any] | None = None,
    *,
    source: str = "",
    owner_id: str = "",
    scope: WorkScope | Mapping[str, Any] | None = None,
) -> DesktopBinding:
    current = CURRENT_DESKTOP_BINDING.get()
    raw = dict(snapshot or {})
    requested_id = str(raw.get("binding_id") or "")
    if current is not None and (not raw or requested_id == current.binding_id):
        return current
    return DesktopBinding.from_mapping(
        raw, source=source, owner_id=owner_id, scope=scope,
    )


@contextmanager
def bind_desktop_binding(binding: DesktopBinding) -> Iterator[DesktopBinding]:
    token = CURRENT_DESKTOP_BINDING.set(binding)
    try:
        yield binding
    finally:
        CURRENT_DESKTOP_BINDING.reset(token)


def desktop_binding_snapshot(
    binding: DesktopBinding | None = None,
) -> dict[str, Any]:
    current = binding or CURRENT_DESKTOP_BINDING.get()
    if current is None:
        return {}
    current.updated_at = time.time()
    return current.to_dict()


def create_child_desktop_binding_snapshot(
    *,
    scope: WorkScope | Mapping[str, Any] | None = None,
    inherit_target: bool = True,
) -> dict[str, Any]:
    parent = CURRENT_DESKTOP_BINDING.get()
    child = DesktopBinding(
        active_window_id=(
            parent.active_window_id
            if parent is not None and inherit_target else ""
        ),
        parent_binding_id=(parent.binding_id if parent is not None else ""),
        owner_kind="run",
        scope=coerce_work_scope(scope),
    )
    return child.to_dict()


__all__ = [
    "CURRENT_DESKTOP_BINDING",
    "DESKTOP_BINDING_SCHEMA",
    "DesktopBinding",
    "bind_desktop_binding",
    "create_child_desktop_binding_snapshot",
    "current_desktop_binding",
    "desktop_binding_snapshot",
    "ensure_desktop_binding",
]
