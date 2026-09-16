"""Typed host contract for portable persistent-kernel capsules."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping


KERNEL_CAPSULE_SCHEMA = "variant1.kernel-capsule.v2"
KERNEL_CAPSULE_POINTER_SCHEMA = "variant1.kernel-capsule-pointer.v1"
KERNEL_CHECKPOINT_OUTCOME_SCHEMA = "variant1.kernel-checkpoint-outcome.v1"
KERNEL_AUTO_RESTORE_OUTCOME_SCHEMA = "variant1.kernel-auto-restore-outcome.v1"
KERNEL_CONTINUITY_POLICY_SCHEMA = "variant1.kernel-continuity-policy.v1"
KERNEL_CAPSULE_APP_VERSION = "0.1.0"


@dataclass(frozen=True)
class KernelCapsuleLimits:
    """Host-owned bounds enforced by the worker and verified again by the host."""

    max_values: int = 256
    max_excluded_values: int = 2048
    max_depth: int = 64
    max_container_items: int = 1_000_000
    max_value_bytes: int = 8 * 1024 * 1024
    max_total_value_bytes: int = 32 * 1024 * 1024
    max_worker_response_bytes: int = 48 * 1024 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class KernelCheckpointPolicy:
    """Opt-in lifecycle boundaries that attempt an honest idle checkpoint."""

    enabled: bool = False
    restore_on_boot: bool = False
    reasons: tuple[str, ...] = (
        "backend_shutdown",
        "capacity_eviction",
        "chat_closed",
        "idle_or_absolute_eviction",
        "operator_restart",
        "workspace_rebound",
    )


class KernelCapsuleError(RuntimeError):
    """One capsule operation failed with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "kernel_capsule_error")
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class KernelCapsuleValue:
    name: str
    type_name: str
    serializer: str
    sha256: str
    bytes: int
    artifact_ref: str
    requirements: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type_name,
            "serializer": self.serializer,
            "sha256": self.sha256,
            "bytes": int(self.bytes),
            "artifact_ref": self.artifact_ref,
            "requirements": dict(self.requirements),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "KernelCapsuleValue":
        return cls(
            name=str(value.get("name") or ""),
            type_name=str(value.get("type") or ""),
            serializer=str(value.get("serializer") or ""),
            sha256=str(value.get("sha256") or ""),
            bytes=int(value.get("bytes") or 0),
            artifact_ref=str(value.get("artifact_ref") or ""),
            requirements=dict(value.get("requirements") or {}),
        )


@dataclass(frozen=True)
class KernelCapsuleExcludedValue:
    name: str
    type_name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "type": self.type_name,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "KernelCapsuleExcludedValue":
        return cls(
            name=str(value.get("name") or ""),
            type_name=str(value.get("type") or ""),
            reason=str(value.get("reason") or "unsupported_type"),
        )


@dataclass(frozen=True)
class KernelCapsuleCompatibilityCheck:
    field: str
    capsule_value: str
    runtime_value: str
    status: str
    blocking: bool
    message: str = ""
    value_names: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "capsule_value": self.capsule_value,
            "runtime_value": self.runtime_value,
            "status": self.status,
            "blocking": bool(self.blocking),
            "message": self.message,
            "value_names": list(self.value_names),
        }


@dataclass(frozen=True)
class KernelCapsuleCompatibility:
    verdict: str
    checks: tuple[KernelCapsuleCompatibilityCheck, ...] = ()
    restorable_names: tuple[str, ...] = ()
    skipped_names: tuple[str, ...] = ()

    @property
    def compatible(self) -> bool:
        return self.verdict == "compatible"

    @property
    def restorable(self) -> bool:
        return self.verdict in {"compatible", "partial"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "compatible": self.compatible,
            "restorable": self.restorable,
            "restorable_names": list(self.restorable_names),
            "skipped_names": list(self.skipped_names),
            "checks": [item.to_dict() for item in self.checks],
        }


@dataclass(frozen=True)
class KernelCapsuleManifest:
    capsule_id: str
    runtime_chat_id: str
    created_at: float
    app_version: str
    app_digest: str
    python: Mapping[str, Any]
    python_digest: str
    platform: Mapping[str, Any]
    platform_digest: str
    environment_digest: str
    catalog_release_id: str
    catalog_digest: str
    workspace_digest: str
    kernel_generation: int
    cell_sequence: int
    cell_execution_id: str
    values: tuple[KernelCapsuleValue, ...]
    excluded_values: tuple[KernelCapsuleExcludedValue, ...]
    artifact_refs: tuple[str, ...] = ()
    artifact_ref: str = ""
    parent_capsule_ref: str = ""
    incremental: Mapping[str, Any] = field(default_factory=dict)
    serializer_registry: Mapping[str, Any] = field(default_factory=dict)
    runtime_profile_id: str = "core.v1"
    runtime_profile_digest: str = ""
    runtime_profile: Mapping[str, Any] = field(default_factory=dict)
    compatibility_at_creation: str = "compatible"
    unsupported_serializers: tuple[str, ...] = (
        "cloudpickle",
    )
    schema: str = KERNEL_CAPSULE_SCHEMA

    def to_payload(self) -> dict[str, Any]:
        """Return the immutable manifest bytes (excluding its own CAS ref)."""

        return {
            "schema": self.schema,
            "capsule_id": self.capsule_id,
            "runtime_chat_id": self.runtime_chat_id,
            "created_at": float(self.created_at),
            "app_version": self.app_version,
            "app": {
                "name": "VARIANT-1",
                "version": self.app_version,
                "digest": self.app_digest,
            },
            "app_digest": self.app_digest,
            "python": dict(self.python),
            "python_digest": self.python_digest,
            "platform": dict(self.platform),
            "platform_digest": self.platform_digest,
            "environment_digest": self.environment_digest,
            "catalog_release_id": self.catalog_release_id,
            "catalog_digest": self.catalog_digest,
            "workspace_digest": self.workspace_digest,
            "kernel_generation": int(self.kernel_generation),
            "cell_cursor": {
                "sequence": int(self.cell_sequence),
                "execution_id": self.cell_execution_id,
            } if self.cell_sequence > 0 else None,
            "values": [value.to_dict() for value in self.values],
            "excluded_values": [value.to_dict() for value in self.excluded_values],
            "artifact_refs": list(self.artifact_refs),
            "parent_capsule_ref": self.parent_capsule_ref or None,
            "incremental": dict(self.incremental),
            "serializer_registry": dict(self.serializer_registry),
            "runtime_profile_id": self.runtime_profile_id,
            "runtime_profile_digest": self.runtime_profile_digest,
            "runtime_profile": dict(self.runtime_profile),
            "compatibility_at_creation": self.compatibility_at_creation,
            "compatibility": {
                "verdict": self.compatibility_at_creation,
                "evaluated_against": "capture_runtime",
            },
            "supported_serializers": sorted(
                str(item.get("id") or "")
                for item in (
                    self.serializer_registry.get("serializers", [])
                    if isinstance(self.serializer_registry, Mapping)
                    else []
                )
                if isinstance(item, Mapping)
                and str(item.get("id") or "")
                and bool(item.get("available", True))
            ) or [
                "bytes.v1",
                "json.strict.v1",
                "text.utf8.v1",
            ],
            "unsupported_serializers": list(self.unsupported_serializers),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_payload(), "artifact_ref": self.artifact_ref}

    def with_artifact_ref(self, artifact_ref: str) -> "KernelCapsuleManifest":
        return replace(self, artifact_ref=str(artifact_ref or ""))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "KernelCapsuleManifest":
        schema = str(value.get("schema") or "")
        if schema != KERNEL_CAPSULE_SCHEMA:
            raise KernelCapsuleError(
                "capsule_schema_unsupported",
                f"Unsupported kernel capsule schema {schema!r}.",
            )
        values = value.get("values")
        excluded = value.get("excluded_values")
        refs = value.get("artifact_refs")
        if not isinstance(values, list) or not isinstance(excluded, list):
            raise KernelCapsuleError(
                "capsule_manifest_invalid",
                "Kernel capsule manifest value lists are invalid.",
            )
        app = value.get("app") if isinstance(value.get("app"), dict) else {}
        compatibility = (
            value.get("compatibility")
            if isinstance(value.get("compatibility"), dict)
            else {}
        )
        cursor = value.get("cell_cursor")
        cursor = dict(cursor) if isinstance(cursor, Mapping) else {}
        return cls(
            capsule_id=str(value.get("capsule_id") or ""),
            runtime_chat_id=str(value.get("runtime_chat_id") or ""),
            created_at=float(value.get("created_at") or 0.0),
            app_version=str(value.get("app_version") or app.get("version") or ""),
            app_digest=str(value.get("app_digest") or app.get("digest") or ""),
            python=dict(value.get("python") or {}),
            python_digest=str(value.get("python_digest") or ""),
            platform=dict(value.get("platform") or {}),
            platform_digest=str(value.get("platform_digest") or ""),
            environment_digest=str(value.get("environment_digest") or ""),
            catalog_release_id=str(value.get("catalog_release_id") or ""),
            catalog_digest=str(value.get("catalog_digest") or ""),
            workspace_digest=str(value.get("workspace_digest") or ""),
            kernel_generation=int(value.get("kernel_generation") or 0),
            cell_sequence=int(cursor.get("sequence") or 0),
            cell_execution_id=str(cursor.get("execution_id") or ""),
            values=tuple(KernelCapsuleValue.from_dict(item) for item in values),
            excluded_values=tuple(
                KernelCapsuleExcludedValue.from_dict(item) for item in excluded
            ),
            artifact_refs=tuple(
                str(item) for item in (refs if isinstance(refs, list) else ())
                if str(item or "")
            ),
            artifact_ref=str(value.get("artifact_ref") or ""),
            parent_capsule_ref=str(value.get("parent_capsule_ref") or ""),
            incremental=dict(value.get("incremental") or {}),
            serializer_registry=dict(value.get("serializer_registry") or {}),
            runtime_profile_id=str(
                value.get("runtime_profile_id") or "core.v1"
            ),
            runtime_profile_digest=str(value.get("runtime_profile_digest") or ""),
            runtime_profile=dict(value.get("runtime_profile") or {}),
            compatibility_at_creation=str(
                value.get("compatibility_at_creation")
                or compatibility.get("verdict")
                or "compatible"
            ),
            unsupported_serializers=tuple(
                str(item)
                for item in (value.get("unsupported_serializers") or ())
            ) or ("cloudpickle",),
            schema=schema,
        )


@dataclass(frozen=True)
class KernelCapsuleInspection:
    manifest: KernelCapsuleManifest
    compatibility: KernelCapsuleCompatibility
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.kernel-capsule-inspection.v1",
            "manifest": self.manifest.to_dict(),
            "compatibility": self.compatibility.to_dict(),
            "verified": bool(self.verified),
        }


@dataclass(frozen=True)
class KernelCapsuleRestoreResult:
    runtime_chat_id: str
    capsule_ref: str
    kernel_generation: int
    restored_names: tuple[str, ...]
    removed_names: tuple[str, ...]
    namespace_reinstalled: bool
    compatibility: KernelCapsuleCompatibility
    skipped_names: tuple[str, ...] = ()
    lineage_persisted: bool = True
    lineage_ref: str = ""
    lineage_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.kernel-capsule-restore.v1",
            "runtime_chat_id": self.runtime_chat_id,
            "capsule_ref": self.capsule_ref,
            "kernel_generation": int(self.kernel_generation),
            "restored_names": list(self.restored_names),
            "removed_names": list(self.removed_names),
            "namespace_reinstalled": bool(self.namespace_reinstalled),
            "skipped_names": list(self.skipped_names),
            "compatibility": self.compatibility.to_dict(),
            "lineage": {
                "persisted": bool(self.lineage_persisted),
                "ref": self.lineage_ref or None,
                "error": self.lineage_error or None,
            },
        }


@dataclass(frozen=True)
class KernelCapsuleForkResult:
    source_runtime_chat_id: str
    target_runtime_chat_id: str
    capsule_ref: str
    created_capsule: bool
    granted_artifact_refs: tuple[str, ...]
    restore: KernelCapsuleRestoreResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.kernel-capsule-fork.v1",
            "source_runtime_chat_id": self.source_runtime_chat_id,
            "target_runtime_chat_id": self.target_runtime_chat_id,
            "capsule_ref": self.capsule_ref,
            "created_capsule": bool(self.created_capsule),
            "granted_artifact_refs": list(self.granted_artifact_refs),
            "restore": self.restore.to_dict(),
        }


__all__ = [
    "KERNEL_CAPSULE_APP_VERSION",
    "KERNEL_AUTO_RESTORE_OUTCOME_SCHEMA",
    "KERNEL_CAPSULE_POINTER_SCHEMA",
    "KERNEL_CAPSULE_SCHEMA",
    "KERNEL_CHECKPOINT_OUTCOME_SCHEMA",
    "KERNEL_CONTINUITY_POLICY_SCHEMA",
    "KernelCapsuleLimits",
    "KernelCapsuleCompatibility",
    "KernelCapsuleCompatibilityCheck",
    "KernelCapsuleError",
    "KernelCapsuleExcludedValue",
    "KernelCapsuleForkResult",
    "KernelCapsuleInspection",
    "KernelCapsuleManifest",
    "KernelCheckpointPolicy",
    "KernelCapsuleRestoreResult",
    "KernelCapsuleValue",
]
