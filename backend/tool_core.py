"""Dependency-neutral values shared by provider and Python tool runtimes.

The provider-facing loop consumes :class:`ToolExecutionResult`. Python adds a
strictly richer receipt plane alongside it so native and programmatic calls can
share one execution contract without changing provider-visible native results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


CAPABILITY_RECEIPT_SCHEMA = "variant1.capability-receipt.v1"
CAPABILITY_STATUSES = frozenset({
    "ok",
    "error",
    "cancelled",
    "cancelled_before_start",
    "timed_out",
    "needs_user",
    "needs_reconciliation",
})


class ToolError(RuntimeError):
    """Expected, user-displayable failure from a tool handler."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_error",
        cause_class: str = "capability",
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "tool_error")
        self.cause_class = str(cause_class or "capability")


class ToolProjectionResult(str):
    """Display text carrying a separate programmatic value for Python cells.

    It remains a ``str`` so existing handlers and focused tests keep their
    exact behavior. The broker alone reads the additional projection fields.
    """

    def __new__(
        cls,
        content: str,
        *,
        programmatic_value: Any,
        receipt_metadata: dict[str, Any] | None = None,
        terminate: bool = False,
    ):
        value = str.__new__(cls, str(content))
        value.programmatic_value = programmatic_value
        value.receipt_metadata = dict(receipt_metadata or {})
        value.terminate = bool(terminate)
        return value


@dataclass(frozen=True)
class ProgrammaticArtifactPayload:
    """Complete programmatic bytes that must enter the existing scoped CAS."""

    data: bytes
    media_type: str
    kind: str
    result: dict[str, Any]


@dataclass(frozen=True)
class ToolExecutionResult:
    """A tool observation plus an optional request to end the agent loop."""

    content: str
    terminate: bool = False


def json_safe(value: Any) -> Any:
    """Return deterministic JSON-compatible data without executing serializers."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((json_safe(item) for item in value), key=repr)
    return str(value)


@dataclass(frozen=True)
class ArtifactRef:
    """Opaque content-addressed artifact reference carried by a receipt."""

    ref: str
    sha256: str
    bytes: int
    media_type: str = "application/octet-stream"
    kind: str = "capability_payload"
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "sha256": self.sha256,
            "bytes": int(self.bytes),
            "media_type": self.media_type,
            "kind": self.kind,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class ContentBlock:
    """One internal typed result block.

    Active HTML/SVG and mutable display protocols are intentionally absent from
    this MVP contract.  ``data`` is used only for deterministic JSON values.
    """

    type: str
    text: str = ""
    data: Any = None
    artifact_ref: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {"type": self.type}
        if self.text:
            row["text"] = self.text
        if self.data is not None:
            row["json"] = json_safe(self.data)
        if self.artifact_ref:
            row["artifact_ref"] = self.artifact_ref
        if self.summary:
            row["summary"] = self.summary
        return row


@dataclass(frozen=True)
class CapabilityErrorInfo:
    code: str
    message: str
    retryable: bool = False
    may_have_applied: bool = False
    cause_class: str = "capability"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": bool(self.retryable),
            "may_have_applied": bool(self.may_have_applied),
            "cause_class": str(self.cause_class or "capability"),
        }


@dataclass(frozen=True)
class EffectRecord:
    effect_class: str
    accepted_at: str
    attempted_at: str = ""
    observed_at: str = ""
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_class": self.effect_class,
            "accepted_at": self.accepted_at,
            "attempted_at": self.attempted_at or None,
            "observed_at": self.observed_at or None,
            "idempotency_key": self.idempotency_key or None,
        }


@dataclass(frozen=True)
class TruncationRecord:
    admitted_bytes: int = 0
    dropped_bytes: int = 0
    artifact_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted_bytes": int(self.admitted_bytes),
            "dropped_bytes": int(self.dropped_bytes),
            "artifact_ref": self.artifact_ref or None,
        }


@dataclass
class CapabilityReceipt:
    """Terminal host receipt for one accepted broker invocation."""

    receipt_id: str
    status: str
    capability: dict[str, Any]
    arguments_sha256: str
    content_blocks: tuple[ContentBlock, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    error: CapabilityErrorInfo | None = None
    effect: EffectRecord | None = None
    truncation: TruncationRecord = field(default_factory=TruncationRecord)
    attribution: dict[str, Any] = field(default_factory=dict)
    result_metadata: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    terminate: bool = False
    deduplicated: bool = False
    # The exact Python value is intentionally excluded from persistence and
    # equality.  It is returned only to the in-process caller/kernel proxy.
    result_value: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.status not in CAPABILITY_STATUSES:
            raise ValueError(f"unsupported capability receipt status: {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def content_text(self) -> str:
        """Deterministic internal/provider projection of the typed blocks."""
        parts: list[str] = []
        for block in self.content_blocks:
            if block.type == "text":
                parts.append(block.text)
            elif block.type == "json":
                parts.append(json.dumps(
                    json_safe(block.data), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ))
            elif block.type == "artifact_ref":
                summary = block.summary or "Full result retained as an artifact."
                parts.append(f"{summary} [{block.artifact_ref}]")
        if not parts and self.error is not None:
            return self.error.message
        return "\n".join(part for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "status": self.status,
            "capability": json_safe(self.capability),
            "arguments_sha256": self.arguments_sha256,
            "content_blocks": [block.to_dict() for block in self.content_blocks],
            "artifact_refs": [ref.to_dict() for ref in self.artifact_refs],
            "error": self.error.to_dict() if self.error else None,
            "effect": self.effect.to_dict() if self.effect else None,
            "truncation": self.truncation.to_dict(),
            "attribution": json_safe(self.attribution),
            "result_metadata": json_safe(self.result_metadata),
            "duration_ms": round(float(self.duration_ms), 3),
            "terminate": bool(self.terminate),
            "deduplicated": bool(self.deduplicated),
        }
