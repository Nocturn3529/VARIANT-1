"""Durable value contracts for the Windows Desktop Fabric."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from core_invariants import (
    StrictJSONError,
    canonical_digest,
    canonical_json as _canonical_json,
    strict_json_value,
)
from work_fabric.scope import WorkScope, coerce_work_scope


DESKTOP_APP_SCHEMA = "variant1.desktop-app.v1"
DESKTOP_WINDOW_SCHEMA = "variant1.desktop-window.v1"
DESKTOP_OBSERVATION_SCHEMA = "variant1.desktop-observation.v1"
DESKTOP_CAPTURE_SCHEMA = "variant1.desktop-capture.v1"
DESKTOP_OPERATION_SCHEMA = "variant1.desktop-operation.v1"
DESKTOP_EVENT_SCHEMA = "variant1.desktop-event.v1"

OPERATION_STATES = frozenset({
    "prepared", "dispatched", "observed", "verified", "no_effect",
    "failed", "unknown_effect",
})
OPERATION_TERMINAL_STATES = frozenset({
    "verified", "no_effect", "failed", "unknown_effect",
})
DELIVERY_MODES = frozenset({"auto", "semantic", "physical"})
OBSERVATION_MODES = frozenset({"uia", "fused", "visual"})


class DesktopFabricError(RuntimeError):
    """Base error for durable desktop operations."""


class DesktopNotFound(DesktopFabricError):
    """A durable app, window, observation, or operation was not found."""


class DesktopStaleReference(DesktopFabricError):
    """A window or element generation no longer matches the live target."""


class DesktopAmbiguousTarget(DesktopFabricError):
    """A semantic target resolved to more than one current element."""


class DesktopConflict(DesktopFabricError):
    """An idempotency or revision constraint was lost."""


class DesktopScopeMismatch(DesktopFabricError):
    """A desktop operation is outside the supplied WorkScope."""


class DesktopUnavailable(DesktopFabricError):
    """The current backend cannot observe or control the requested target."""


class DesktopValidationError(DesktopFabricError, ValueError):
    """A desktop request or persisted payload is malformed."""


def json_value(value: Any, *, path: str = "$") -> Any:
    try:
        return strict_json_value(value, path=path)
    except StrictJSONError as exc:
        raise DesktopValidationError(str(exc)) from exc


def canonical_json(value: Any) -> str:
    return _canonical_json(json_value(value))


def stable_digest(*parts: Any) -> str:
    return canonical_digest([json_value(part) for part in parts])


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    started_at: float

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "started_at": self.started_at}


@dataclass(frozen=True, slots=True)
class AppRecord:
    app_id: str
    executable: str = ""
    package_family: str = ""
    app_user_model_id: str = ""
    display_name: str = ""
    processes: tuple[ProcessIdentity, ...] = ()
    installed: bool | None = None
    running: bool = False
    launch_identity: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DESKTOP_APP_SCHEMA, "id": self.app_id,
            "executable": self.executable or None,
            "package_family": self.package_family or None,
            "app_user_model_id": self.app_user_model_id or None,
            "display_name": self.display_name,
            "processes": [item.to_dict() for item in self.processes],
            "installed": self.installed, "running": self.running,
            "launch_identity": json_value(self.launch_identity),
            "revision": self.revision, "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class WindowRecord:
    window_id: str
    app_id: str
    hwnd: int
    pid: int
    pid_started_at: float
    executable: str = ""
    package_family: str = ""
    app_user_model_id: str = ""
    class_name: str = ""
    title: str = ""
    owner_hwnd: int = 0
    root_owner_hwnd: int = 0
    monitor: str = ""
    virtual_desktop: str = ""
    dpi: int = 96
    bounds: tuple[int, int, int, int] | None = None
    visible: bool = True
    minimized: bool = False
    cloaked: bool | None = None
    occluded: bool | None = None
    foreground: bool = False
    generation: int = 1
    backend_instance_id: str = ""
    recovery: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    created_at: float = 0.0
    updated_at: float = 0.0
    missing_at: float = 0.0

    @property
    def strong_identity(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd, "pid": self.pid,
            "pid_started_at": self.pid_started_at,
            "executable": self.executable, "class_name": self.class_name,
            "package_family": self.package_family,
        }

    @property
    def live(self) -> bool:
        return not bool(self.missing_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DESKTOP_WINDOW_SCHEMA, "id": self.window_id,
            "app_id": self.app_id, "hwnd": self.hwnd, "pid": self.pid,
            "pid_started_at": self.pid_started_at,
            "executable": self.executable or None,
            "package_family": self.package_family or None,
            "app_user_model_id": self.app_user_model_id or None,
            "class_name": self.class_name, "title": self.title,
            "owner_hwnd": self.owner_hwnd or None,
            "root_owner_hwnd": self.root_owner_hwnd or None,
            "monitor": self.monitor or None,
            "virtual_desktop": self.virtual_desktop or None,
            "dpi": self.dpi, "bounds": list(self.bounds) if self.bounds else None,
            "visible": self.visible, "minimized": self.minimized,
            "cloaked": self.cloaked, "occluded": self.occluded,
            "foreground": self.foreground, "generation": self.generation,
            "backend_instance_id": self.backend_instance_id or None,
            "recovery": json_value(self.recovery), "revision": self.revision,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "missing_at": self.missing_at or None,
            "strong_identity": self.strong_identity,
        }


@dataclass(frozen=True, slots=True)
class DesktopElement:
    element_ref: str
    observation_id: str
    window_id: str
    window_generation: int
    element_generation: int
    role: str = ""
    name: str = ""
    text: str = ""
    value: str = ""
    states: dict[str, Any] = field(default_factory=dict)
    patterns: tuple[str, ...] = ()
    bounds: tuple[int, int, int, int] | None = None
    runtime_id: tuple[int, ...] = ()
    automation_id: str = ""
    semantic_path: str = ""
    confidence: float = 1.0
    actionable: bool = False
    provenance: tuple[str, ...] = ("uia",)
    fingerprint: str = ""
    backend_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.element_ref, "observation_id": self.observation_id,
            "window_id": self.window_id,
            "window_generation": self.window_generation,
            "element_generation": self.element_generation,
            "role": self.role, "name": self.name, "text": self.text,
            "value": self.value, "states": json_value(self.states),
            "patterns": list(self.patterns),
            "bounds": list(self.bounds) if self.bounds else None,
            "runtime_id": list(self.runtime_id),
            "automation_id": self.automation_id or None,
            "semantic_path": self.semantic_path or None,
            "confidence": self.confidence, "actionable": self.actionable,
            "provenance": list(self.provenance),
            "fingerprint": self.fingerprint, "backend_key": self.backend_key,
        }


@dataclass(frozen=True, slots=True)
class DesktopObservation:
    observation_id: str
    window_id: str
    window_generation: int
    mode: str
    elements: tuple[DesktopElement, ...]
    capture_id: str = ""
    image_ref: str = ""
    capture: dict[str, Any] = field(default_factory=dict)
    completeness: str = "uia-only"
    uia_generation: int = 0
    event_cursor: int = 0
    fingerprint: str = ""
    created_at: float = 0.0
    scope: WorkScope = field(default_factory=WorkScope)

    def find(
        self, *, role: str = "", name: str = "", text: str = "",
        value: str = "", actionable: bool | None = None,
        provenance: str = "",
    ) -> list[DesktopElement]:
        def match(actual: str, expected: str) -> bool:
            return not expected or expected.casefold() in actual.casefold()
        return [element for element in self.elements if (
            match(element.role, role)
            and match(element.name, name)
            and match(element.text, text)
            and match(element.value, value)
            and (actionable is None or element.actionable is actionable)
            and (not provenance or provenance in element.provenance)
        )]

    def one(self, **criteria: Any) -> DesktopElement:
        matches = self.find(**criteria)
        if not matches:
            raise DesktopNotFound(f"no desktop element matches {criteria}")
        if len(matches) != 1:
            raise DesktopAmbiguousTarget(
                f"{len(matches)} desktop elements match {criteria}")
        return matches[0]

    def changed_since(self, previous: "DesktopObservation") -> bool:
        return self.fingerprint != previous.fingerprint

    def require_scope(self, scope: Any) -> None:
        requested = coerce_work_scope(scope)
        # Views remain reusable across turns/goal steps in their own chat.
        # Legacy unowned observations cannot become evidence for a chat.
        if self.scope.chat_id != requested.chat_id:
            raise DesktopScopeMismatch("desktop observation belongs to a different chat")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DESKTOP_OBSERVATION_SCHEMA, "id": self.observation_id,
            "window_id": self.window_id,
            "window_generation": self.window_generation, "mode": self.mode,
            "elements": [item.to_dict() for item in self.elements],
            "capture_id": self.capture_id or None,
            "image_ref": self.image_ref or None,
            "capture": json_value(self.capture),
            "completeness": self.completeness,
            "uia_generation": self.uia_generation,
            "event_cursor": self.event_cursor,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class DesktopCapture:
    capture_id: str
    window_id: str
    window_generation: int
    artifact_ref: str
    bytes: int
    width: int
    height: int
    provenance: str
    occlusion_independent: bool
    minimized: bool
    stale: bool
    coordinate_transform: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DESKTOP_CAPTURE_SCHEMA, "id": self.capture_id,
            "window_id": self.window_id,
            "window_generation": self.window_generation,
            "artifact_ref": self.artifact_ref, "bytes": self.bytes,
            "width": self.width, "height": self.height,
            "provenance": self.provenance,
            "occlusion_independent": self.occlusion_independent,
            "minimized": self.minimized, "stale": self.stale,
            "coordinate_transform": json_value(self.coordinate_transform),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class DesktopOperation:
    operation_id: str
    window_id: str
    window_generation: int
    scope: WorkScope
    idempotency_key: str
    action: str
    delivery: str
    target: dict[str, Any]
    arguments: dict[str, Any]
    expectation: dict[str, Any]
    state: str
    before_observation_id: str = ""
    after_observation_id: str = ""
    dispatch: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    backend_instance_id: str = ""
    revision: int = 1
    created_at: float = 0.0
    dispatched_at: float = 0.0
    observed_at: float = 0.0
    completed_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        if self.state not in OPERATION_STATES:
            raise DesktopValidationError(f"invalid desktop operation state: {self.state}")
        if self.delivery not in DELIVERY_MODES:
            raise DesktopValidationError(f"invalid desktop delivery mode: {self.delivery}")

    @property
    def terminal(self) -> bool:
        return self.state in OPERATION_TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DESKTOP_OPERATION_SCHEMA, "id": self.operation_id,
            "window_id": self.window_id,
            "window_generation": self.window_generation,
            "scope": self.scope.to_dict(),
            "idempotency_key": self.idempotency_key or None,
            "action": self.action, "delivery": self.delivery,
            "target": json_value(self.target),
            "arguments": json_value(self.arguments),
            "expectation": json_value(self.expectation), "state": self.state,
            "before_observation_id": self.before_observation_id or None,
            "after_observation_id": self.after_observation_id or None,
            "dispatch": json_value(self.dispatch),
            "evidence": json_value(self.evidence), "error": self.error or None,
            "backend_instance_id": self.backend_instance_id or None,
            "revision": self.revision, "created_at": self.created_at,
            "dispatched_at": self.dispatched_at or None,
            "observed_at": self.observed_at or None,
            "completed_at": self.completed_at or None,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class DesktopEvent:
    sequence: int
    event_id: str
    entity_kind: str
    entity_id: str
    event_type: str
    revision: int
    payload: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DESKTOP_EVENT_SCHEMA, "sequence": self.sequence,
            "event_id": self.event_id,
            "entity": {"kind": self.entity_kind, "id": self.entity_id},
            "type": self.event_type, "revision": self.revision,
            "payload": json_value(self.payload), "created_at": self.created_at,
        }


def coerce_scope(value: WorkScope | Mapping[str, Any] | None) -> WorkScope:
    return coerce_work_scope(value)


__all__ = [
    "AppRecord", "DELIVERY_MODES", "DesktopAmbiguousTarget",
    "DesktopCapture", "DesktopConflict", "DesktopElement", "DesktopEvent",
    "DesktopFabricError", "DesktopNotFound", "DesktopObservation",
    "DesktopOperation", "DesktopScopeMismatch", "DesktopStaleReference",
    "DesktopUnavailable",
    "DesktopValidationError", "OBSERVATION_MODES", "OPERATION_STATES",
    "OPERATION_TERMINAL_STATES", "ProcessIdentity", "WindowRecord",
    "canonical_json", "coerce_scope", "json_value", "stable_digest",
]
