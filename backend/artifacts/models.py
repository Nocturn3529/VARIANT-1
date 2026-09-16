"""Typed values for portable content-addressed artifact operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ARTIFACT_MANIFEST_SCHEMA = "variant1.artifact-manifest.v1"


@dataclass(frozen=True)
class ArtifactMetadata:
    """Stable metadata for one artifact object or one scoped grant."""

    ref: str
    sha256: str
    bytes: int
    media_type: str
    kind: str
    created_at: float
    scope: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "sha256": self.sha256,
            "bytes": int(self.bytes),
            "media_type": self.media_type,
            "kind": self.kind,
            "created_at": float(self.created_at),
            "scope": self.scope,
        }


@dataclass(frozen=True)
class ArtifactManifest:
    """One deterministic, bounded page of a scope's artifact grants."""

    scope: str
    items: tuple[ArtifactMetadata, ...]
    has_more: bool
    next_after_ref: str
    total_bytes: int
    digest: str
    schema: str = ARTIFACT_MANIFEST_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "scope": self.scope,
            "items": [item.to_dict() for item in self.items],
            "count": len(self.items),
            "has_more": bool(self.has_more),
            "next_after_ref": self.next_after_ref,
            "total_bytes": int(self.total_bytes),
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ArtifactExport:
    """Receipt for a verified artifact export to a local filesystem path."""

    ref: str
    sha256: str
    bytes: int
    destination: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "sha256": self.sha256,
            "bytes": int(self.bytes),
            "destination": self.destination,
            "verified": bool(self.verified),
        }


class ArtifactIntegrityError(OSError):
    """Stored bytes no longer match their content-addressed identity."""


__all__ = [
    "ARTIFACT_MANIFEST_SCHEMA",
    "ArtifactExport",
    "ArtifactIntegrityError",
    "ArtifactManifest",
    "ArtifactMetadata",
]
