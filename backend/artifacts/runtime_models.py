"""Versioned user-facing artifact runtime contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from work_fabric.scope import WorkScope, coerce_work_scope


@dataclass(frozen=True, slots=True)
class ArtifactObjectRecord:
    artifact_id: str
    kind: str
    title: str
    scope: WorkScope
    current_revision: int
    version: int
    created_at: float
    updated_at: float
    tombstoned_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.artifact-object.v1",
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "title": self.title,
            "scope": self.scope.to_dict(include_empty=False),
            "current_revision": self.current_revision,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tombstoned_at": self.tombstoned_at or None,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRevisionRecord:
    artifact_id: str
    revision: int
    parent_revision: int
    content_ref: str
    media_type: str
    source_ref: str
    manifest_ref: str
    metadata: Mapping[str, Any]
    producer: Mapping[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.artifact-revision.v1",
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "parent_revision": self.parent_revision or None,
            "content_ref": self.content_ref,
            "media_type": self.media_type,
            "source_ref": self.source_ref or None,
            "manifest_ref": self.manifest_ref or None,
            "metadata": dict(self.metadata),
            "producer": dict(self.producer),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRenderRecord:
    render_id: str
    artifact_id: str
    revision: int
    format: str
    renderer: str
    renderer_version: str
    output_ref: str
    preview_refs: tuple[str, ...]
    diagnostics_ref: str
    normalized_sha256: str
    status: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.artifact-render.v1",
            "render_id": self.render_id,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "format": self.format,
            "renderer": self.renderer,
            "renderer_version": self.renderer_version,
            "output_ref": self.output_ref or None,
            "preview_refs": list(self.preview_refs),
            "diagnostics_ref": self.diagnostics_ref or None,
            "normalized_sha256": self.normalized_sha256 or None,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactValidationRecord:
    validation_id: str
    artifact_id: str
    revision: int
    validator: str
    validator_version: str
    status: str
    findings: tuple[Mapping[str, Any], ...]
    report_ref: str
    created_at: float
    render_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.artifact-validation.v1",
            "validation_id": self.validation_id,
            "render_id": self.render_id or None,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "validator": self.validator,
            "validator_version": self.validator_version,
            "status": self.status,
            "findings": [dict(item) for item in self.findings],
            "report_ref": self.report_ref or None,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactAliasRecord:
    scope_key: str
    alias: str
    artifact_id: str
    revision: int
    version: int
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.artifact-alias.v1",
            "scope_key": self.scope_key,
            "alias": self.alias,
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "version": self.version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    artifact: ArtifactObjectRecord
    revision: ArtifactRevisionRecord
    renders: tuple[ArtifactRenderRecord, ...] = ()
    validations: tuple[ArtifactValidationRecord, ...] = ()
    aliases: tuple[ArtifactAliasRecord, ...] = ()
    links: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.artifact-snapshot.v1",
            "artifact": self.artifact.to_dict(),
            "revision": self.revision.to_dict(),
            "renders": [item.to_dict() for item in self.renders],
            "validations": [item.to_dict() for item in self.validations],
            "aliases": [item.to_dict() for item in self.aliases],
            "links": [dict(item) for item in self.links],
        }


class ArtifactRuntimeError(RuntimeError):
    pass


class ArtifactRuntimeNotFound(ArtifactRuntimeError):
    pass


class ArtifactRuntimeConflict(ArtifactRuntimeError):
    pass


class ArtifactRuntimeValidationError(ArtifactRuntimeError, ValueError):
    pass


__all__ = [
    "ArtifactAliasRecord", "ArtifactObjectRecord", "ArtifactRenderRecord",
    "ArtifactRevisionRecord", "ArtifactRuntimeConflict", "ArtifactRuntimeError",
    "ArtifactRuntimeNotFound", "ArtifactRuntimeValidationError", "ArtifactSnapshot",
    "ArtifactValidationRecord",
]
