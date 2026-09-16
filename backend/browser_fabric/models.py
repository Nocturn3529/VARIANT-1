"""Typed, dependency-light contracts for VARIANT-1's durable Browser Fabric.

The records in this module contain identifiers and observations only.  Live
Playwright pages, Electron sockets, downloads, and tracing objects remain
inside their adapters and are never serialized into SQLite or IPython state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence

from core_invariants import StrictJSONError, strict_json_value
from work_fabric.scope import WorkScope


BROWSER_KINDS = frozenset({"embedded", "managed"})
SESSION_STATES = frozenset({
    "opening", "active", "recovering", "closed", "unavailable", "orphaned",
})
TARGET_STATES = frozenset({"opening", "active", "closed", "orphaned"})
OPERATION_STATES = frozenset({
    "prepared", "dispatched", "observed", "committed", "failed", "unknown_effect",
})


class BrowserFabricError(RuntimeError):
    """Base class for durable browser-domain failures."""


class BrowserValidationError(BrowserFabricError, ValueError):
    """A caller supplied a malformed browser value."""


class BrowserNotFound(BrowserFabricError):
    """A durable browser object does not exist."""


class BrowserConflict(BrowserFabricError):
    """A revision, generation, or idempotency precondition changed."""


class BrowserScopeMismatch(BrowserFabricError):
    """A browser object is outside the supplied WorkScope."""


class BrowserStaleReference(BrowserConflict):
    """A PageRef or ElementRef no longer describes the active document."""


class BrowserUnsupported(BrowserFabricError):
    """The selected browser adapter does not support an operation."""

    def __init__(self, operation: str, kind: str, reason: str = "") -> None:
        message = f"browser kind {kind!r} does not support {operation!r}"
        if reason:
            message += f": {reason}"
        super().__init__(message)
        self.operation = str(operation)
        self.kind = str(kind)
        self.reason = str(reason)


class BrowserUnavailable(BrowserFabricError):
    """A configured adapter cannot currently acquire its browser."""


class BrowserUnknownEffect(BrowserFabricError):
    """A dispatched mutation ended without an authoritative observation."""


def json_value(value: Any, *, path: str = "$") -> Any:
    """Return deterministic JSON data and reject executable/custom values."""

    try:
        return strict_json_value(value, path=path)
    except StrictJSONError as exc:
        raise BrowserValidationError(str(exc)) from exc


def clean_identifier(value: Any, field_name: str, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise BrowserValidationError(f"{field_name} is required")
    if len(text) > 512:
        raise BrowserValidationError(f"{field_name} exceeds 512 characters")
    if "\x00" in text:
        raise BrowserValidationError(f"{field_name} contains a NUL character")
    return text


@dataclass(frozen=True, slots=True)
class BrowserExpectedState:
    """Optional compare-and-set guards applied before adapter dispatch."""

    session_generation: int | None = None
    session_revision: int | None = None
    target_revision: int | None = None
    document_epoch: int | None = None
    observation_revision: int | None = None

    def to_dict(self) -> dict[str, int]:
        output: dict[str, int] = {}
        for key in (
            "session_generation", "session_revision", "target_revision",
            "document_epoch", "observation_revision",
        ):
            value = getattr(self, key)
            if value is not None:
                output[key] = int(value)
        return output


@dataclass(frozen=True, slots=True)
class BrowserSessionRef:
    session_id: str
    generation: int
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-session-ref.v1",
            "session_id": self.session_id,
            "generation": self.generation,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class PageRef:
    session_id: str
    target_id: str
    generation: int
    target_revision: int
    document_epoch: int
    observation_revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-page-ref.v1",
            "session_id": self.session_id,
            "target_id": self.target_id,
            "generation": self.generation,
            "target_revision": self.target_revision,
            "document_epoch": self.document_epoch,
            "observation_revision": self.observation_revision,
        }


@dataclass(frozen=True, slots=True)
class ElementRef:
    session_id: str
    target_id: str
    generation: int
    document_epoch: int
    observation_revision: int
    backend_ref: str
    role: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-element-ref.v1",
            "session_id": self.session_id,
            "target_id": self.target_id,
            "generation": self.generation,
            "document_epoch": self.document_epoch,
            "observation_revision": self.observation_revision,
            "backend_ref": self.backend_ref,
            "role": self.role or None,
            "name": self.name or None,
        }


@dataclass(frozen=True, slots=True)
class BrowserJobRef:
    session_id: str
    job_id: str
    kind: str
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-job-ref.v1",
            "session_id": self.session_id,
            "job_id": self.job_id,
            "kind": self.kind,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    profile_id: str
    name: str
    kind: str
    persistent: bool
    user_data_dir: str
    state: str
    revision: int
    scope: WorkScope = field(default_factory=WorkScope)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-profile.v1",
            "profile_id": self.profile_id,
            "name": self.name,
            "kind": self.kind,
            "persistent": self.persistent,
            "user_data_dir": self.user_data_dir or None,
            "state": self.state,
            "revision": self.revision,
            "scope": self.scope.to_dict(include_empty=False),
            "metadata": json_value(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error or None,
        }


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    profile_id: str
    kind: str
    state: str
    generation: int
    revision: int
    current_target_id: str
    headless: bool
    capabilities: tuple[str, ...] = ()
    scope: WorkScope = field(default_factory=WorkScope)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float = 0.0
    last_error: str = ""

    @property
    def ref(self) -> BrowserSessionRef:
        return BrowserSessionRef(self.session_id, self.generation, self.revision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-session.v1",
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "kind": self.kind,
            "state": self.state,
            "generation": self.generation,
            "revision": self.revision,
            "current_target_id": self.current_target_id or None,
            "headless": self.headless,
            "capabilities": list(self.capabilities),
            "scope": self.scope.to_dict(include_empty=False),
            "metadata": json_value(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at or None,
            "last_error": self.last_error or None,
        }


@dataclass(frozen=True, slots=True)
class TargetRecord:
    target_id: str
    session_id: str
    backend_target_id: str
    state: str
    title: str
    url: str
    document_epoch: int
    observation_revision: int
    revision: int
    created_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float = 0.0
    last_error: str = ""
    viewport: Mapping[str, Any] = field(default_factory=dict)

    def page_ref(self, generation: int) -> PageRef:
        return PageRef(
            session_id=self.session_id,
            target_id=self.target_id,
            generation=int(generation),
            target_revision=self.revision,
            document_epoch=self.document_epoch,
            observation_revision=self.observation_revision,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-target.v1",
            "viewport": json_value(self.viewport),
            "target_id": self.target_id,
            "session_id": self.session_id,
            "backend_target_id": self.backend_target_id,
            "state": self.state,
            "title": self.title,
            "url": self.url,
            "document_epoch": self.document_epoch,
            "observation_revision": self.observation_revision,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at or None,
            "last_error": self.last_error or None,
        }


@dataclass(frozen=True, slots=True)
class ElementRecord:
    backend_ref: str
    role: str
    name: str
    text: str = ""
    value: str = ""
    disabled: bool = False
    checked: bool | None = None
    selected: bool | None = None
    visible: bool = True
    editable: bool = False
    bbox: Mapping[str, float] = field(default_factory=dict)
    actions: tuple[str, ...] = ()
    input_type: str = ""

    def ref_for(self, observation: "ObservationRecord") -> ElementRef:
        return ElementRef(
            session_id=observation.session_id,
            target_id=observation.target_id,
            generation=observation.generation,
            document_epoch=observation.document_epoch,
            observation_revision=observation.revision,
            backend_ref=self.backend_ref,
            role=self.role,
            name=self.name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_ref": self.backend_ref,
            "role": self.role,
            "name": self.name,
            "text": self.text or None,
            "value": self.value or None,
            "disabled": self.disabled,
            "checked": self.checked,
            "selected": self.selected,
            "visible": self.visible,
            "editable": self.editable,
            "bbox": json_value(self.bbox),
            "actions": list(self.actions),
            "input_type": self.input_type,
        }


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    observation_id: str
    session_id: str
    target_id: str
    generation: int
    document_epoch: int
    revision: int
    title: str
    url: str
    text_excerpt: str
    elements: tuple[ElementRecord, ...]
    text_artifact_ref: str = ""
    html_artifact_ref: str = ""
    screenshot_artifact_ref: str = ""
    created_at: float = 0.0
    viewport: Mapping[str, Any] = field(default_factory=dict)
    document: Mapping[str, Any] = field(default_factory=dict)

    def find(
        self,
        *,
        role: str = "",
        name: str = "",
        text: str = "",
        editable: bool | None = None,
        visible: bool | None = True,
    ) -> tuple[ElementRef, ...]:
        role_q = role.casefold().strip()
        name_q = name.casefold().strip()
        text_q = text.casefold().strip()
        matches: list[ElementRef] = []
        for element in self.elements:
            if role_q and element.role.casefold() != role_q:
                continue
            if name_q and name_q not in element.name.casefold():
                continue
            if text_q and text_q not in (element.text or element.name).casefold():
                continue
            if editable is not None and element.editable != bool(editable):
                continue
            if visible is not None and element.visible != bool(visible):
                continue
            matches.append(element.ref_for(self))
        return tuple(matches)

    def one(self, **query: Any) -> ElementRef:
        matches = self.find(**query)
        if not matches:
            raise BrowserNotFound("no observed element matches the query")
        if len(matches) != 1:
            raise BrowserConflict(f"element query matched {len(matches)} controls")
        return matches[0]

    def element(self, backend_ref: str) -> ElementRef:
        for item in self.elements:
            if item.backend_ref == backend_ref:
                return item.ref_for(self)
        raise BrowserNotFound(f"element {backend_ref!r} is absent from the observation")

    def changed_since(self, previous: "ObservationRecord") -> dict[str, Any]:
        if self.session_id != previous.session_id or self.target_id != previous.target_id:
            raise BrowserValidationError("observations belong to different pages")
        before = {item.backend_ref: item.to_dict() for item in previous.elements}
        after = {item.backend_ref: item.to_dict() for item in self.elements}
        return {
            "document_changed": self.document_epoch != previous.document_epoch,
            "url_changed": self.url != previous.url,
            "title_changed": self.title != previous.title,
            "text_changed": self.text_excerpt != previous.text_excerpt,
            "added": [after[key] for key in after.keys() - before.keys()],
            "removed": [before[key] for key in before.keys() - after.keys()],
            "updated": [
                after[key] for key in after.keys() & before.keys()
                if after[key] != before[key]
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-observation.v1",
            "document": json_value(self.document),
            "viewport": json_value(self.viewport),
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "target_id": self.target_id,
            "generation": self.generation,
            "document_epoch": self.document_epoch,
            "revision": self.revision,
            "title": self.title,
            "url": self.url,
            "text_excerpt": self.text_excerpt,
            "elements": [item.to_dict() for item in self.elements],
            "artifacts": {
                "text": self.text_artifact_ref or None,
                "html": self.html_artifact_ref or None,
                "screenshot": self.screenshot_artifact_ref or None,
            },
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    session_id: str
    target_id: str
    kind: str
    state: str
    idempotency_key: str
    request_fingerprint: str
    before_generation: int
    before_document_epoch: int
    before_observation_revision: int
    result: Mapping[str, Any]
    diagnostic: str
    scope: WorkScope
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-operation.v1",
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "target_id": self.target_id or None,
            "kind": self.kind,
            "state": self.state,
            "idempotency_key": self.idempotency_key or None,
            "before": {
                "generation": self.before_generation,
                "document_epoch": self.before_document_epoch,
                "observation_revision": self.before_observation_revision,
            },
            "result": json_value(self.result),
            "diagnostic": self.diagnostic or None,
            "scope": self.scope.to_dict(include_empty=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class EventRecord:
    sequence: int
    event_id: str
    session_id: str
    target_id: str
    kind: str
    payload: Mapping[str, Any]
    scope: WorkScope
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-event.v1",
            "sequence": self.sequence,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "target_id": self.target_id or None,
            "kind": self.kind,
            "payload": json_value(self.payload),
            "scope": self.scope.to_dict(include_empty=False),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    download_id: str
    session_id: str
    target_id: str
    operation_id: str
    suggested_filename: str
    url: str
    artifact_ref: str
    sha256: str
    bytes: int
    state: str
    revision: int
    scope: WorkScope
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-download.v1",
            "download_id": self.download_id,
            "session_id": self.session_id,
            "target_id": self.target_id,
            "operation_id": self.operation_id,
            "suggested_filename": self.suggested_filename,
            "url": self.url,
            "artifact_ref": self.artifact_ref or None,
            "sha256": self.sha256 or None,
            "bytes": self.bytes,
            "state": self.state,
            "revision": self.revision,
            "scope": self.scope.to_dict(include_empty=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TraceRecord:
    trace_id: str
    session_id: str
    name: str
    state: str
    artifact_ref: str
    sha256: str
    bytes: int
    revision: int
    scope: WorkScope
    created_at: float
    updated_at: float
    diagnostic: str = ""

    @property
    def ref(self) -> BrowserJobRef:
        return BrowserJobRef(self.session_id, self.trace_id, "trace", self.revision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.browser-trace.v1",
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "name": self.name,
            "state": self.state,
            "artifact_ref": self.artifact_ref or None,
            "sha256": self.sha256 or None,
            "bytes": self.bytes,
            "revision": self.revision,
            "scope": self.scope.to_dict(include_empty=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "diagnostic": self.diagnostic or None,
        }


def element_records(values: Sequence[Mapping[str, Any]]) -> tuple[ElementRecord, ...]:
    output: list[ElementRecord] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        ref = clean_identifier(raw.get("backend_ref") or raw.get("ref"), f"elements[{index}].ref")
        if ref in seen:
            raise BrowserValidationError(f"duplicate observed element reference {ref!r}")
        seen.add(ref)
        bbox_raw = raw.get("bbox") if isinstance(raw.get("bbox"), Mapping) else {}
        bbox: dict[str, float] = {}
        for key in ("x", "y", "width", "height"):
            if key in bbox_raw:
                value = float(bbox_raw[key])
                if math.isfinite(value):
                    bbox[key] = value
        output.append(ElementRecord(
            backend_ref=ref,
            role=str(raw.get("role") or "control")[:80],
            name=str(raw.get("name") or "")[:500],
            text=str(raw.get("text") or "")[:2_000],
            value=str(raw.get("value") or "")[:2_000],
            disabled=bool(raw.get("disabled")),
            checked=(bool(raw["checked"]) if raw.get("checked") is not None else None),
            selected=(bool(raw["selected"]) if raw.get("selected") is not None else None),
            visible=bool(raw.get("visible", True)),
            editable=bool(raw.get("editable")),
            bbox=bbox,
            actions=tuple(str(item)[:80] for item in raw.get("actions") or ()),
            input_type=str(raw.get('input_type') or '')[:40],
        ))
    return tuple(output)


__all__ = [
    "BROWSER_KINDS",
    "OPERATION_STATES",
    "SESSION_STATES",
    "TARGET_STATES",
    "BrowserConflict",
    "BrowserExpectedState",
    "BrowserFabricError",
    "BrowserJobRef",
    "BrowserNotFound",
    "BrowserScopeMismatch",
    "BrowserSessionRef",
    "BrowserStaleReference",
    "BrowserUnavailable",
    "BrowserUnknownEffect",
    "BrowserUnsupported",
    "BrowserValidationError",
    "DownloadRecord",
    "ElementRecord",
    "ElementRef",
    "EventRecord",
    "ObservationRecord",
    "OperationRecord",
    "PageRef",
    "ProfileRecord",
    "SessionRecord",
    "TargetRecord",
    "TraceRecord",
    "clean_identifier",
    "element_records",
    "json_value",
]
