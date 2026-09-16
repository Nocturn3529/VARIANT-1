"""Portable kernel state, checkpoint, and continuity orchestration."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import replace
import hashlib
import json
import os
import platform
import sys
import time
from typing import TYPE_CHECKING, Any
import uuid

from core_invariants import canonical_digest
from session_catalog.profiles import is_action_surface
from tool_core import json_safe

from .capsule_contracts import (
    SERIALIZER_REGISTRY_SCHEMA,
    WORKER_CAPSULE_SCHEMA,
    WORKER_CAPTURE_REQUEST_SCHEMA,
    WORKER_NAMESPACE_SCHEMA,
    serializer_registry_document,
)
from .capsules import (
    KERNEL_AUTO_RESTORE_OUTCOME_SCHEMA,
    KERNEL_CAPSULE_POINTER_SCHEMA,
    KERNEL_CAPSULE_SCHEMA,
    KERNEL_CHECKPOINT_OUTCOME_SCHEMA,
    KERNEL_CONTINUITY_POLICY_SCHEMA,
    KernelCapsuleCompatibility,
    KernelCapsuleCompatibilityCheck,
    KernelCapsuleError,
    KernelCapsuleExcludedValue,
    KernelCapsuleForkResult,
    KernelCapsuleInspection,
    KernelCapsuleManifest,
    KernelCapsuleRestoreResult,
    KernelCapsuleValue,
    KernelCheckpointPolicy,
)
from .contracts import (
    KernelAutoRestoreError,
    KernelContinuityError,
    KernelUnavailable,
)
from .lease import KernelLease
from .runtime_profile import runtime_profile

if TYPE_CHECKING:
    from .manager import KernelRuntimeManager


class KernelContinuityCoordinator:
    """Own portable-state lineage, policy, capture, restore, and checkpoints."""

    def __init__(self, manager: "KernelRuntimeManager") -> None:
        self.manager = manager
        self._latest_capsules: dict[str, KernelCapsuleManifest] = {}
        self._capsule_pointer_refs: dict[str, str] = {}
        self._capsule_lineage_errors: dict[str, str] = {}
        self._checkpoint_outcomes: dict[str, dict[str, Any]] = {}
        self._checkpoint_outcome_refs: dict[str, str] = {}
        self._auto_restore_outcomes: dict[str, dict[str, Any]] = {}
        self._auto_restore_outcome_refs: dict[str, str] = {}
        self._continuity_policies: dict[str, dict[str, Any]] = {}
        self._continuity_policy_refs: dict[str, str] = {}

    @staticmethod
    def _capsule_digest(value: Any) -> str:
        return canonical_digest(value)

    def _capsule_worker_limits(self) -> dict[str, int]:
        limits = self.manager.capsule_limits
        return {
            "max_values": max(0, int(limits.max_values)),
            "max_excluded_values": max(0, int(limits.max_excluded_values)),
            "max_depth": max(0, int(limits.max_depth)),
            "max_container_items": max(1, int(limits.max_container_items)),
            "max_value_bytes": max(0, int(limits.max_value_bytes)),
            "max_total_value_bytes": max(
                0, int(limits.max_total_value_bytes)
            ),
            "max_response_bytes": max(
                1024, int(limits.max_worker_response_bytes)
            ),
        }

    def _capsule_workspace_context(
        self,
        workspace_roots: tuple[str, ...],
        workspace_digest: str,
    ) -> tuple[tuple[str, ...], str, str]:
        roots = tuple(
            os.path.abspath(str(path))
            for path in workspace_roots
            if str(path or "").strip()
        )
        workspace_root = roots[0] if roots else self.manager.app_root
        digest = str(workspace_digest or "").strip() or self._capsule_digest({
            "schema": "variant1.kernel-workspace-context.v1",
            "roots": list(roots or (os.path.abspath(self.manager.app_root),)),
        })
        return roots, workspace_root, digest

    @staticmethod
    def _capsule_catalog_digest(identity: Any) -> str:
        release_id = str(getattr(identity, "catalog_release_id", "") or "")
        return hashlib.sha256(
            ("variant1.catalog-release\0" + release_id).encode("utf-8")
        ).hexdigest()

    def _capsule_runtime_context(
        self,
        runtime_chat_id: str,
        *,
        workspace_roots: tuple[str, ...] = (),
        workspace_digest: str = "",
    ) -> tuple[Any, Any, tuple[str, ...], str, str]:
        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise KernelCapsuleError(
                "runtime_chat_id_required",
                "Kernel capsule operations require an explicit runtime_chat_id.",
            )
        record = self.manager.registry.ensure_runtime(chat_id)
        identity = record.identity
        if not is_action_surface(str(identity.action_surface)):
            raise KernelCapsuleError(
                "ipython_not_admitted",
                f"Runtime profile {identity.action_surface!r} does not admit Python capsules.",
            )
        roots, workspace_root, effective_digest = self._capsule_workspace_context(
            workspace_roots, workspace_digest
        )
        return record, identity, roots, workspace_root, effective_digest

    async def maybe_auto_restore(self, lease: KernelLease) -> None:
        async with lease._auto_restore_lock:
            if lease._auto_restore_attempted:
                if lease._auto_restore_error is not None:
                    raise lease._auto_restore_error
                return
            lease._auto_restore_attempted = True
            try:
                await self._perform_auto_restore(lease)
            except KernelAutoRestoreError as exc:
                lease._auto_restore_error = exc
                raise

    async def _perform_auto_restore(self, lease: KernelLease) -> None:
        policy = self._effective_checkpoint_policy(lease.chat_id)
        if not (policy.enabled and policy.restore_on_boot):
            return
        chat_id = lease.chat_id
        checkpoint = self.durable_checkpoint_outcome(chat_id)
        base: dict[str, Any] = {
            "schema": KERNEL_AUTO_RESTORE_OUTCOME_SCHEMA,
            "runtime_chat_id": chat_id,
            "kernel_generation": int(lease.generation),
            "status": "skipped",
            "reason": "checkpoint_absent",
            "capsule_ref": None,
        }
        if checkpoint is None:
            self._persist_auto_restore_outcome(base)
            return
        checkpoint_status = str(checkpoint.get("status") or "")
        capsule_ref = str(checkpoint.get("capsule_ref") or "")
        if checkpoint_status != "captured" or not capsule_ref:
            self._persist_auto_restore_outcome({
                **base,
                "reason": f"checkpoint_{checkpoint_status or 'invalid'}",
                "checkpoint_reason": str(checkpoint.get("reason") or "")[:500],
                "checkpoint_evidence_ref": checkpoint.get("evidence_ref"),
            })
            return
        outcome = {
            **base,
            "status": "restoring",
            "reason": "",
            "capsule_ref": capsule_ref,
            "checkpoint_evidence_ref": checkpoint.get("evidence_ref"),
        }
        try:
            manifest = self._load_capsule_manifest(
                runtime_chat_id=chat_id,
                capsule_ref=capsule_ref,
                verify_values=True,
            )
            try:
                checkpoint_generation = int(
                    checkpoint.get("kernel_generation") or 0
                )
            except (TypeError, ValueError):
                checkpoint_generation = 0
            manifest_generation = int(manifest.kernel_generation)
            target_generation = int(lease.generation)
            if (
                checkpoint_generation <= 0
                or checkpoint_generation != manifest_generation
                or checkpoint_generation >= target_generation
            ):
                raise KernelCapsuleError(
                    "auto_restore_generation_fence",
                    "Automatic restore requires an older checkpoint whose "
                    "recorded generation matches its capsule.",
                    details={
                        "checkpoint_generation": checkpoint_generation,
                        "capsule_generation": manifest_generation,
                        "target_generation": target_generation,
                    },
                )
            record = self.manager.registry.ensure_runtime(chat_id)
            workspace_digest = self._capsule_workspace_context(
                lease.workspace_roots, ""
            )[2]
            compatibility = self._capsule_compatibility(
                manifest,
                identity=record.identity,
                workspace_digest=workspace_digest,
                target_registry=await self._worker_serializer_registry(lease),
            )
            if not compatibility.compatible:
                raise KernelCapsuleError(
                    "auto_restore_exact_compatibility_required",
                    "Automatic restore requires every capsule value to be compatible.",
                    details={"compatibility": compatibility.to_dict()},
                )
            restored = await self.manager.restore_capsule(
                runtime_chat_id=chat_id,
                capsule_ref=capsule_ref,
                workspace_roots=lease.workspace_roots,
                workspace_digest=workspace_digest,
                workspace_revision=lease.workspace_revision,
                require_compatible=True,
                _suppress_auto_restore=True,
                _target_lease=lease,
            )
            outcome.update({
                "status": (
                    "restored"
                    if restored.lineage_persisted
                    else "restored_with_warning"
                ),
                "restored_names": list(restored.restored_names),
                "removed_names": list(restored.removed_names),
                "lineage": restored.to_dict().get("lineage"),
                "compatibility": restored.compatibility.to_dict(),
            })
            self._persist_auto_restore_outcome(outcome)
            self.manager.emit(
                "kernel:auto_restore",
                status=str(outcome["status"]),
                chat_id=chat_id,
                kernel_generation=int(lease.generation),
                capsule_ref=capsule_ref,
                values=len(restored.restored_names),
            )
        except asyncio.CancelledError:
            outcome.update({
                "status": "failed",
                "reason": "auto_restore_cancelled",
                "message": (
                    "Automatic checkpoint restoration was cancelled before "
                    "completion."
                ),
                "details": {},
            })
            self._persist_auto_restore_outcome(outcome)
            self.manager.emit(
                "kernel:auto_restore",
                status="failed",
                chat_id=chat_id,
                kernel_generation=int(lease.generation),
                capsule_ref=capsule_ref,
                reason="auto_restore_cancelled",
            )
            with suppress(BaseException):
                await lease.close(reason="auto_restore_cancelled", hard=True)
            raise
        except Exception as exc:
            code = str(
                getattr(exc, "code", "") or "kernel_auto_restore_failed"
            )
            outcome.update({
                "status": "failed",
                "reason": code,
                "message": f"{type(exc).__name__}: {exc}"[:1_000],
                "details": (
                    dict(getattr(exc, "details", {}) or {})
                    if isinstance(getattr(exc, "details", {}), dict)
                    else {}
                ),
            })
            outcome = self._persist_auto_restore_outcome(outcome)
            self.manager.emit(
                "kernel:auto_restore",
                status="failed",
                chat_id=chat_id,
                kernel_generation=int(lease.generation),
                capsule_ref=capsule_ref,
                reason=code,
            )
            with suppress(BaseException):
                await lease.close(reason="auto_restore_failed", hard=True)
            raise KernelAutoRestoreError(
                code,
                "Configured kernel checkpoint restoration failed before model code.",
                outcome=outcome,
            ) from exc

    def _latest_scoped_artifact_ref(self, chat_id: str, *, kind: str) -> str:
        list_scope = getattr(self.manager.artifact_store, "list_scope", None)
        if not callable(list_scope):
            return ""
        try:
            rows = list_scope(str(chat_id), limit=1, kind=str(kind))
        except TypeError:
            rows = [
                row
                for row in list_scope(str(chat_id), limit=200)
                if isinstance(row, dict) and str(row.get("kind") or "") == kind
            ][:1]
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return ""
        return str(rows[0].get("ref") or "")

    def _read_small_scoped_json(
        self,
        chat_id: str,
        artifact_ref: str,
        *,
        expected_kind: str,
        max_bytes: int = 128 * 1024,
    ) -> dict[str, Any]:
        metadata = self.manager.artifact_store.stat(
            artifact_ref, scope=str(chat_id), verify=True
        )
        if str(metadata.kind) != str(expected_kind):
            raise ValueError(f"artifact kind is not {expected_kind}")
        if int(metadata.bytes) > max(1, int(max_bytes)):
            raise ValueError(f"{expected_kind} artifact exceeds its byte bound")
        payload = self.manager.artifact_store.read_bytes_scoped(
            artifact_ref, str(chat_id)
        )
        decoded = json.loads(payload.decode("utf-8", errors="strict"))
        if not isinstance(decoded, dict):
            raise ValueError(f"{expected_kind} artifact is not an object")
        return dict(decoded)

    def _record_capsule_pointer(
        self,
        manifest: KernelCapsuleManifest,
        *,
        reason: str,
        runtime_chat_id: str = "",
    ) -> str:
        chat_id = str(runtime_chat_id or manifest.runtime_chat_id).strip()
        if not chat_id:
            raise ValueError("capsule pointer requires a target runtime chat")
        pointer = {
            "schema": KERNEL_CAPSULE_POINTER_SCHEMA,
            "runtime_chat_id": chat_id,
            "origin_runtime_chat_id": str(manifest.runtime_chat_id),
            "capsule_ref": manifest.artifact_ref,
            "capsule_id": manifest.capsule_id,
            "kernel_generation": int(manifest.kernel_generation),
            "reason": str(reason or "")[:200],
            "recorded_at": time.time(),
        }
        ref = self.manager.artifact_store.put_json(
            pointer,
            kind="kernel_capsule_pointer",
            scope=chat_id,
        )
        self._latest_capsules[chat_id] = manifest
        self._capsule_pointer_refs[chat_id] = str(ref.ref)
        self._capsule_lineage_errors.pop(chat_id, None)
        return str(ref.ref)

    def _discover_latest_capsule(
        self,
        chat_id: str,
    ) -> KernelCapsuleManifest | None:
        clean = str(chat_id or "").strip()
        if not clean:
            return None
        cached = self._latest_capsules.get(clean)
        if cached is not None:
            return cached
        pointer_ref = self._latest_scoped_artifact_ref(
            clean, kind="kernel_capsule_pointer"
        )
        try:
            if pointer_ref:
                pointer = self._read_small_scoped_json(
                    clean,
                    pointer_ref,
                    expected_kind="kernel_capsule_pointer",
                )
                if (
                    pointer.get("schema") != KERNEL_CAPSULE_POINTER_SCHEMA
                    or str(pointer.get("runtime_chat_id") or "") != clean
                ):
                    raise ValueError("capsule pointer identity is invalid")
                capsule_ref = str(pointer.get("capsule_ref") or "")
                if not capsule_ref:
                    raise ValueError("capsule pointer has no manifest reference")
                manifest = self._load_capsule_manifest(
                    runtime_chat_id=clean,
                    capsule_ref=capsule_ref,
                    verify_values=False,
                )
                self._capsule_pointer_refs[clean] = pointer_ref
            else:
                # Pre-Phase-7 capsules have no pointer event. Recover the most
                # recent immutable manifest without manufacturing a new lineage
                # event merely because status or capture inspected it.
                capsule_ref = self._latest_scoped_artifact_ref(
                    clean, kind="kernel_capsule_manifest"
                )
                if not capsule_ref:
                    return None
                manifest = self._load_capsule_manifest(
                    runtime_chat_id=clean,
                    capsule_ref=capsule_ref,
                    verify_values=False,
                )
            self._latest_capsules[clean] = manifest
            self._capsule_lineage_errors.pop(clean, None)
            return manifest
        except Exception as exc:
            self._capsule_lineage_errors[clean] = (
                f"{type(exc).__name__}: {exc}"[:500]
            )
            return None

    def compact_latest_capsule(self, chat_id: str) -> dict[str, Any] | None:
        manifest = self._discover_latest_capsule(chat_id)
        error = self._capsule_lineage_errors.get(str(chat_id), "")
        if manifest is None:
            return (
                {"status": "unavailable", "error": error}
                if error
                else None
            )
        return {
            "status": "available",
            "capsule_ref": manifest.artifact_ref,
            "capsule_id": manifest.capsule_id,
            "created_at": float(manifest.created_at),
            "kernel_generation": int(manifest.kernel_generation),
            "values": len(manifest.values),
            "excluded_values": len(manifest.excluded_values),
            "parent_capsule_ref": manifest.parent_capsule_ref or None,
            "runtime_profile_id": manifest.runtime_profile_id,
            "lineage_ref": self._capsule_pointer_refs.get(str(chat_id)) or None,
        }

    def _persist_scoped_outcome(
        self,
        outcome: dict[str, Any],
        *,
        schema: str,
        kind: str,
        cache: dict[str, dict[str, Any]],
        refs: dict[str, str],
    ) -> dict[str, Any]:
        clean = json.loads(json.dumps(
            json_safe(outcome),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ))
        chat_id = str(clean.get("runtime_chat_id") or "")
        clean["schema"] = str(schema)
        durable = {**clean, "recorded_at": time.time()}
        try:
            ref = self.manager.artifact_store.put_json(
                durable,
                kind=str(kind),
                scope=chat_id,
            )
            clean["evidence_ref"] = str(ref.ref)
            refs[chat_id] = str(ref.ref)
        except Exception as exc:
            clean["persistence"] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        cache[chat_id] = dict(clean)
        return clean

    def _durable_scoped_outcome(
        self,
        chat_id: str,
        *,
        schema: str,
        kind: str,
        cache: dict[str, dict[str, Any]],
        refs: dict[str, str],
        invalid_reason: str,
    ) -> dict[str, Any] | None:
        clean = str(chat_id or "").strip()
        cached = cache.get(clean)
        if cached is not None:
            return dict(cached)
        ref = self._latest_scoped_artifact_ref(
            clean, kind=str(kind)
        )
        if not ref:
            return None
        try:
            value = self._read_small_scoped_json(
                clean,
                ref,
                expected_kind=str(kind),
            )
            if (
                value.get("schema") != str(schema)
                or str(value.get("runtime_chat_id") or "") != clean
            ):
                raise ValueError(f"{kind} identity is invalid")
            value["evidence_ref"] = ref
            cache[clean] = dict(value)
            refs[clean] = ref
            return value
        except Exception as exc:
            return {
                "schema": str(schema),
                "runtime_chat_id": clean,
                "status": "unavailable",
                "reason": str(invalid_reason),
                "message": f"{type(exc).__name__}: {exc}"[:500],
                "evidence_ref": ref,
            }

    def _persist_checkpoint_outcome(
        self,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        return self._persist_scoped_outcome(
            outcome,
            schema=KERNEL_CHECKPOINT_OUTCOME_SCHEMA,
            kind="kernel_checkpoint_outcome",
            cache=self._checkpoint_outcomes,
            refs=self._checkpoint_outcome_refs,
        )

    def durable_checkpoint_outcome(
        self,
        chat_id: str,
    ) -> dict[str, Any] | None:
        return self._durable_scoped_outcome(
            chat_id,
            schema=KERNEL_CHECKPOINT_OUTCOME_SCHEMA,
            kind="kernel_checkpoint_outcome",
            cache=self._checkpoint_outcomes,
            refs=self._checkpoint_outcome_refs,
            invalid_reason="checkpoint_evidence_invalid",
        )

    def _persist_auto_restore_outcome(
        self,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        return self._persist_scoped_outcome(
            outcome,
            schema=KERNEL_AUTO_RESTORE_OUTCOME_SCHEMA,
            kind="kernel_auto_restore_outcome",
            cache=self._auto_restore_outcomes,
            refs=self._auto_restore_outcome_refs,
        )

    def durable_auto_restore_outcome(
        self,
        chat_id: str,
    ) -> dict[str, Any] | None:
        return self._durable_scoped_outcome(
            chat_id,
            schema=KERNEL_AUTO_RESTORE_OUTCOME_SCHEMA,
            kind="kernel_auto_restore_outcome",
            cache=self._auto_restore_outcomes,
            refs=self._auto_restore_outcome_refs,
            invalid_reason="auto_restore_evidence_invalid",
        )

    def _durable_continuity_policy(
        self,
        chat_id: str,
    ) -> dict[str, Any] | None:
        clean = str(chat_id or "").strip()
        cached = self._continuity_policies.get(clean)
        if cached is not None:
            return dict(cached)
        ref = self._latest_scoped_artifact_ref(
            clean, kind="kernel_continuity_policy"
        )
        if not ref:
            return None
        try:
            value = self._read_small_scoped_json(
                clean,
                ref,
                expected_kind="kernel_continuity_policy",
            )
            mode = str(value.get("mode") or "")
            if (
                value.get("schema") != KERNEL_CONTINUITY_POLICY_SCHEMA
                or str(value.get("runtime_chat_id") or "") != clean
                or mode not in {"override", "inherit"}
                or not str(value.get("policy_id") or "")
                or int(value.get("revision") or 0) <= 0
            ):
                raise ValueError("kernel continuity policy identity is invalid")
            if mode == "override":
                checkpoint_enabled = value.get("checkpoint_enabled")
                restore_on_boot = value.get("restore_on_boot")
                if (
                    type(checkpoint_enabled) is not bool
                    or type(restore_on_boot) is not bool
                    or (restore_on_boot and not checkpoint_enabled)
                ):
                    raise ValueError("kernel continuity override is invalid")
            value["evidence_ref"] = ref
            self._continuity_policies[clean] = dict(value)
            self._continuity_policy_refs[clean] = ref
            return value
        except Exception as exc:
            unavailable = {
                "schema": KERNEL_CONTINUITY_POLICY_SCHEMA,
                "runtime_chat_id": clean,
                "mode": "unavailable",
                "reason": "continuity_policy_evidence_invalid",
                "message": f"{type(exc).__name__}: {exc}"[:500],
                "evidence_ref": ref,
            }
            self._continuity_policies[clean] = dict(unavailable)
            self._continuity_policy_refs[clean] = ref
            return unavailable

    def _continuity_policy_state(self, chat_id: str) -> dict[str, Any]:
        clean = str(chat_id or "").strip()
        defaults = {
            "checkpoint_enabled": bool(self.manager.checkpoint_policy.enabled),
            "restore_on_boot": bool(self.manager.checkpoint_policy.restore_on_boot),
            "reasons": list(self.manager.checkpoint_policy.reasons),
        }
        effective = dict(defaults)
        source = "global_default"
        override = self._durable_continuity_policy(clean)
        if override is not None and override.get("mode") == "override":
            effective.update({
                "checkpoint_enabled": bool(override["checkpoint_enabled"]),
                "restore_on_boot": bool(override["restore_on_boot"]),
            })
            source = "chat_override"
        elif override is not None and override.get("mode") == "unavailable":
            source = "global_fallback"
        return {
            "schema": "variant1.kernel-continuity-policy-state.v1",
            "runtime_chat_id": clean,
            "source": source,
            "defaults": defaults,
            "effective": effective,
            "override": dict(override) if override is not None else None,
        }

    def _effective_checkpoint_policy(self, chat_id: str) -> KernelCheckpointPolicy:
        state = self._continuity_policy_state(chat_id)
        effective = dict(state["effective"])
        return KernelCheckpointPolicy(
            enabled=bool(effective["checkpoint_enabled"]),
            restore_on_boot=bool(effective["restore_on_boot"]),
            reasons=tuple(str(item) for item in effective.get("reasons") or ()),
        )

    def continuity_status(self, runtime_chat_id: str) -> dict[str, Any]:
        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise KernelContinuityError(
                "runtime_chat_id_required",
                "Kernel continuity requires a durable runtime_chat_id.",
            )
        return {
            "schema": "variant1.kernel-continuity.v1",
            "runtime_chat_id": chat_id,
            "policy": self._continuity_policy_state(chat_id),
            "latest_checkpoint": self.durable_checkpoint_outcome(chat_id),
            "latest_auto_restore": self.durable_auto_restore_outcome(chat_id),
            "latest_capsule": self.compact_latest_capsule(chat_id),
        }

    def configure_continuity(
        self,
        runtime_chat_id: str,
        *,
        checkpoint_enabled: bool | None = None,
        restore_on_boot: bool | None = None,
        inherit_defaults: bool = False,
        configured_by: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise KernelContinuityError(
                "runtime_chat_id_required",
                "Kernel continuity requires a durable runtime_chat_id.",
            )
        if type(inherit_defaults) is not bool:
            raise KernelContinuityError(
                "continuity_policy_invalid",
                "inherit_defaults must be a boolean.",
            )
        for name, value in (
            ("checkpoint_enabled", checkpoint_enabled),
            ("restore_on_boot", restore_on_boot),
        ):
            if value is not None and type(value) is not bool:
                raise KernelContinuityError(
                    "continuity_policy_invalid",
                    f"{name} must be a boolean when provided.",
                )
        if inherit_defaults and (
            checkpoint_enabled is not None or restore_on_boot is not None
        ):
            raise KernelContinuityError(
                "continuity_policy_conflict",
                "inherit_defaults cannot be combined with explicit policy fields.",
            )
        if not inherit_defaults and (
            checkpoint_enabled is None and restore_on_boot is None
        ):
            raise KernelContinuityError(
                "continuity_policy_empty",
                "Provide a continuity field or set inherit_defaults=true.",
            )

        current_state = self._continuity_policy_state(chat_id)
        current_effective = dict(current_state["effective"])
        mode = "inherit" if inherit_defaults else "override"
        selected_checkpoint: bool | None = None
        selected_restore: bool | None = None
        if mode == "override":
            selected_checkpoint = bool(current_effective["checkpoint_enabled"])
            selected_restore = bool(current_effective["restore_on_boot"])
            if checkpoint_enabled is not None:
                selected_checkpoint = checkpoint_enabled
                if checkpoint_enabled is False and restore_on_boot is None:
                    selected_restore = False
            if restore_on_boot is not None:
                selected_restore = restore_on_boot
                if restore_on_boot is True and checkpoint_enabled is None:
                    selected_checkpoint = True
            if bool(selected_restore) and not bool(selected_checkpoint):
                raise KernelContinuityError(
                    "continuity_policy_conflict",
                    "restore_on_boot requires checkpoint_enabled.",
                )

        prior = self._durable_continuity_policy(chat_id)
        prior_revision = (
            int(prior.get("revision") or 0)
            if isinstance(prior, dict)
            and prior.get("mode") in {"override", "inherit"}
            else 0
        )
        actor = json_safe(dict(configured_by or {}))
        encoded_actor = json.dumps(
            actor,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
        if len(encoded_actor) > 8 * 1024:
            raise KernelContinuityError(
                "continuity_actor_too_large",
                "Kernel continuity attribution exceeds 8 KiB.",
            )
        document = {
            "schema": KERNEL_CONTINUITY_POLICY_SCHEMA,
            "policy_id": "continuity_" + uuid.uuid4().hex,
            "runtime_chat_id": chat_id,
            "revision": prior_revision + 1,
            "mode": mode,
            "checkpoint_enabled": selected_checkpoint,
            "restore_on_boot": selected_restore,
            "configured_by": actor,
            "recorded_at": time.time(),
        }
        try:
            ref = self.manager.artifact_store.put_json(
                document,
                kind="kernel_continuity_policy",
                scope=chat_id,
            )
        except Exception as exc:
            raise KernelContinuityError(
                "continuity_policy_persistence_failed",
                "Kernel continuity policy could not be persisted.",
                details={"error": f"{type(exc).__name__}: {exc}"[:500]},
            ) from exc
        document["evidence_ref"] = str(ref.ref)
        self._continuity_policies[chat_id] = dict(document)
        self._continuity_policy_refs[chat_id] = str(ref.ref)
        self.manager.emit(
            "kernel:continuity_configured",
            status="ok",
            chat_id=chat_id,
            revision=int(document["revision"]),
            mode=mode,
            checkpoint_enabled=selected_checkpoint,
            restore_on_boot=selected_restore,
        )
        return self.continuity_status(chat_id)

    async def create_capsule(
        self,
        *,
        runtime_chat_id: str,
        workspace_roots: tuple[str, ...] = (),
        workspace_digest: str = "",
        _lifecycle_checkpoint: bool = False,
    ) -> KernelCapsuleManifest:
        """Capture the admitted portable subset of one idle kernel namespace."""

        if self.manager._closed or (self.manager._shutting_down and not _lifecycle_checkpoint):
            raise KernelCapsuleError(
                "kernel_manager_closed", "Kernel manager is shut down."
            )
        chat_id = str(runtime_chat_id or "").strip()
        _record, identity, roots, workspace_root, effective_workspace_digest = (
            self._capsule_runtime_context(
                chat_id,
                workspace_roots=workspace_roots,
                workspace_digest=workspace_digest,
            )
        )
        # Capture is evidence about a currently live persistent namespace. It
        # must never manufacture a fresh empty generation after eviction or a
        # backend restart and mislabel that as preserved state.
        lease = self.manager._leases.get(chat_id)
        if lease is None or lease._closed or lease.state not in {"ready", "busy"}:
            raise KernelCapsuleError(
                "kernel_not_live",
                "No live kernel generation exists to capture for this runtime_chat_id.",
            )
        if not self.manager._lease_identity_matches(lease, identity):
            raise KernelCapsuleError(
                "kernel_identity_stale",
                "Live kernel identity no longer matches the durable runtime identity.",
            )
        document, refs = self.manager.namespace_document(chat_id, identity)
        lease._refs.update(refs)
        previous_manifest = self._discover_latest_capsule(chat_id)
        known_values = (
            [item.to_dict() for item in previous_manifest.values]
            if previous_manifest is not None
            else []
        )
        capture_request = {
            "schema": WORKER_CAPTURE_REQUEST_SCHEMA,
            "limits": self._capsule_worker_limits(),
            "known_values": known_values,
        }
        async with lease.execution_lock:
            if lease.state != "ready" or lease._closed:
                raise KernelCapsuleError(
                    "kernel_not_idle",
                    "Kernel generation became unavailable before capsule capture.",
                )
            await lease._sync_namespace(document)
            capture = await lease._capsule_request(
                "capture",
                capture_request,
                max_response_bytes=self.manager.capsule_limits.max_worker_response_bytes,
            )
        if str(capture.get("schema") or "") != WORKER_CAPSULE_SCHEMA:
            raise KernelCapsuleError(
                "capsule_worker_schema_unsupported",
                "Kernel worker returned an unsupported capsule schema.",
            )
        worker_error = capture.get("error")
        if isinstance(worker_error, dict):
            raise KernelCapsuleError(
                str(worker_error.get("code") or "capsule_worker_error"),
                str(worker_error.get("message") or "Kernel capsule capture failed."),
                details=(
                    dict(worker_error.get("details"))
                    if isinstance(worker_error.get("details"), dict)
                    else {}
                ),
            )
        raw_values = capture.get("values")
        raw_excluded = capture.get("excluded")
        if not isinstance(raw_values, list) or not isinstance(raw_excluded, list):
            raise KernelCapsuleError(
                "capsule_protocol_error",
                "Kernel worker capsule lists are invalid.",
            )
        if len(raw_values) > int(self.manager.capsule_limits.max_values):
            raise KernelCapsuleError(
                "capsule_value_limit",
                "Kernel capsule contains too many serializable values.",
                details={"count": len(raw_values)},
            )
        if len(raw_excluded) > int(self.manager.capsule_limits.max_excluded_values):
            raise KernelCapsuleError(
                "capsule_exclusion_limit",
                "Kernel capsule exclusion ledger exceeds its host-owned bound.",
                details={"count": len(raw_excluded)},
            )

        decoded_values: list[
            tuple[dict[str, Any], bytes | None, KernelCapsuleValue | None]
        ] = []
        seen_names: set[str] = set()
        total_bytes = 0
        previous_values = {
            item.name: item
            for item in (
                previous_manifest.values if previous_manifest is not None else ()
            )
        }
        raw_registry = capture.get("serializer_registry")
        registry_rows = (
            raw_registry.get("serializers")
            if isinstance(raw_registry, dict)
            else None
        )
        serializer_descriptors = {
            str(item.get("id") or ""): dict(item)
            for item in (registry_rows if isinstance(registry_rows, list) else ())
            if isinstance(item, dict)
            and str(item.get("id") or "")
            and bool(item.get("available", True))
        }
        if not serializer_descriptors:
            serializer_descriptors = {
                item: {"id": item, "media_type": media_type}
                for item, media_type in {
                    "json.strict.v1": "application/json",
                    "text.utf8.v1": "text/plain; charset=utf-8",
                    "bytes.v1": "application/octet-stream",
                }.items()
            }
        admitted_serializers = set(serializer_descriptors)
        for raw_item in raw_values:
            if not isinstance(raw_item, dict):
                raise KernelCapsuleError(
                    "capsule_protocol_error", "Kernel capsule value is not an object."
                )
            name = str(raw_item.get("name") or "")
            serializer = str(raw_item.get("serializer") or "")
            if not name or name in seen_names:
                raise KernelCapsuleError(
                    "capsule_protocol_error",
                    "Kernel capsule value names are empty or duplicated.",
                )
            if serializer not in admitted_serializers:
                raise KernelCapsuleError(
                    "capsule_serializer_unsupported",
                    f"Kernel capsule serializer {serializer!r} is unsupported.",
                )
            expected_size = int(raw_item.get("bytes") or 0)
            expected_hash = str(raw_item.get("sha256") or "")
            reused = bool(raw_item.get("reused"))
            previous_value = previous_values.get(name) if reused else None
            payload: bytes | None = None
            if reused:
                if (
                    previous_value is None
                    or previous_value.serializer != serializer
                    or previous_value.sha256 != expected_hash
                    or int(previous_value.bytes) != expected_size
                    or previous_value.artifact_ref
                    != str(raw_item.get("artifact_ref") or "")
                ):
                    raise KernelCapsuleError(
                        "capsule_incremental_reference_invalid",
                        f"Kernel capsule value {name!r} has an invalid reuse reference.",
                    )
                try:
                    metadata = self.manager.artifact_store.stat(
                        previous_value.artifact_ref,
                        scope=chat_id,
                        verify=True,
                    )
                except Exception as exc:
                    raise KernelCapsuleError(
                        "capsule_value_unavailable",
                        f"Reused kernel capsule value {name!r} is unavailable.",
                    ) from exc
                if (
                    str(metadata.sha256) != expected_hash
                    or int(metadata.bytes) != expected_size
                ):
                    raise KernelCapsuleError(
                        "capsule_integrity_error",
                        f"Reused kernel capsule value {name!r} failed verification.",
                    )
            else:
                try:
                    payload = base64.b64decode(
                        str(raw_item.get("data_b64") or ""), validate=True
                    )
                except Exception as exc:
                    raise KernelCapsuleError(
                        "capsule_protocol_error",
                        f"Kernel capsule value {name!r} contains invalid base64.",
                    ) from exc
            if payload is not None and (
                len(payload) != expected_size
                or hashlib.sha256(payload).hexdigest() != expected_hash
            ):
                raise KernelCapsuleError(
                    "capsule_integrity_error",
                    f"Kernel capsule value {name!r} failed worker/host integrity verification.",
                )
            if expected_size > int(self.manager.capsule_limits.max_value_bytes):
                raise KernelCapsuleError(
                    "capsule_value_too_large",
                    f"Kernel capsule value {name!r} exceeds its byte bound.",
                )
            total_bytes += expected_size
            if total_bytes > int(self.manager.capsule_limits.max_total_value_bytes):
                raise KernelCapsuleError(
                    "capsule_total_too_large",
                    "Kernel capsule values exceed the total byte bound.",
                )
            seen_names.add(name)
            decoded_values.append((raw_item, payload, previous_value))

        excluded_values: list[KernelCapsuleExcludedValue] = []
        for raw_item in raw_excluded:
            if not isinstance(raw_item, dict):
                raise KernelCapsuleError(
                    "capsule_protocol_error",
                    "Kernel capsule exclusion is not an object.",
                )
            excluded_values.append(KernelCapsuleExcludedValue.from_dict(raw_item))

        value_records: list[KernelCapsuleValue] = []
        for raw_item, payload, previous_value in decoded_values:
            serializer = str(raw_item["serializer"])
            media_type = str(
                serializer_descriptors[serializer].get("media_type")
                or "application/octet-stream"
            )
            if previous_value is not None:
                artifact_ref = previous_value.artifact_ref
                sha256 = previous_value.sha256
                value_bytes = int(previous_value.bytes)
            else:
                if payload is None:
                    raise KernelCapsuleError(
                        "capsule_protocol_error",
                        "Materialized kernel capsule value has no payload.",
                    )
                try:
                    ref = self.manager.artifact_store.put_bytes(
                        payload,
                        media_type=media_type,
                        kind="kernel_capsule_value",
                        scope=chat_id,
                    )
                except Exception as exc:
                    raise KernelCapsuleError(
                        "capsule_artifact_write_failed",
                        "Could not persist a kernel capsule value in the shared CAS.",
                    ) from exc
                artifact_ref = str(ref.ref)
                sha256 = str(ref.sha256)
                value_bytes = int(ref.bytes)
            value_records.append(KernelCapsuleValue(
                name=str(raw_item["name"]),
                type_name=str(raw_item.get("type") or ""),
                serializer=serializer,
                sha256=sha256,
                bytes=value_bytes,
                artifact_ref=artifact_ref,
                requirements=(
                    dict(raw_item.get("requirements"))
                    if isinstance(raw_item.get("requirements"), dict)
                    else {}
                ),
            ))

        latest_cell = self.manager.cell_ledger.latest(chat_id)
        python_identity = dict(capture.get("python") or {})
        platform_identity = dict(capture.get("platform") or {})
        app_digest = self._capsule_digest({
            "name": "VARIANT-1",
            "version": self.manager.app_version,
        })
        unavailable_serializers = tuple(sorted({
            "cloudpickle",
            *(
                str(item.get("id") or "")
                for item in (
                    registry_rows if isinstance(registry_rows, list) else ()
                )
                if isinstance(item, dict)
                and str(item.get("id") or "")
                and not bool(item.get("available", True))
            ),
        }))
        manifest = KernelCapsuleManifest(
            capsule_id="capsule_" + uuid.uuid4().hex,
            runtime_chat_id=chat_id,
            created_at=time.time(),
            app_version=self.manager.app_version,
            app_digest=app_digest,
            python=python_identity,
            python_digest=self._capsule_digest(python_identity),
            platform=platform_identity,
            platform_digest=self._capsule_digest(platform_identity),
            environment_digest=str(identity.environment_digest or ""),
            catalog_release_id=str(identity.catalog_release_id or ""),
            catalog_digest=self._capsule_catalog_digest(identity),
            workspace_digest=effective_workspace_digest,
            kernel_generation=int(lease.generation),
            cell_sequence=int(latest_cell.sequence if latest_cell is not None else 0),
            cell_execution_id=(
                str(latest_cell.execution_id) if latest_cell is not None else ""
            ),
            values=tuple(value_records),
            excluded_values=tuple(excluded_values),
            artifact_refs=tuple(item.artifact_ref for item in value_records),
            parent_capsule_ref=(
                previous_manifest.artifact_ref
                if previous_manifest is not None
                else ""
            ),
            incremental=(
                dict(capture.get("incremental"))
                if isinstance(capture.get("incremental"), dict)
                else {}
            ),
            serializer_registry=(
                dict(capture.get("serializer_registry"))
                if isinstance(capture.get("serializer_registry"), dict)
                else {}
            ),
            runtime_profile_id=self.manager.runtime_profile.profile_id,
            runtime_profile_digest=self.manager.runtime_profile.digest,
            runtime_profile=self.manager.runtime_profile.to_dict(),
            unsupported_serializers=unavailable_serializers,
        )
        manifest_payload = manifest.to_payload()
        manifest_bytes = json.dumps(
            manifest_payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
        if len(manifest_bytes) > int(self.manager.capsule_limits.max_manifest_bytes):
            raise KernelCapsuleError(
                "capsule_manifest_too_large",
                "Kernel capsule manifest exceeds the host-owned byte bound.",
            )
        try:
            manifest_ref = self.manager.artifact_store.put_json(
                manifest_payload,
                kind="kernel_capsule_manifest",
                scope=chat_id,
            )
        except Exception as exc:
            raise KernelCapsuleError(
                "capsule_manifest_write_failed",
                "Could not persist the kernel capsule manifest in the shared CAS.",
            ) from exc
        manifest = manifest.with_artifact_ref(manifest_ref.ref)
        try:
            self._record_capsule_pointer(manifest, reason="capture")
        except Exception as exc:
            raise KernelCapsuleError(
                "capsule_lineage_write_failed",
                "Capsule values and manifest were stored, but durable lineage could not be recorded.",
                details={"capsule_ref": manifest.artifact_ref},
            ) from exc
        self.manager.emit(
            "kernel:capsule_created",
            status="ok",
            chat_id=chat_id,
            kernel_generation=lease.generation,
            capsule_ref=manifest.artifact_ref,
            values=len(manifest.values),
            excluded_values=len(manifest.excluded_values),
            total_bytes=total_bytes,
            reused_values=int(manifest.incremental.get("reused_values") or 0),
        )
        return manifest

    def _load_capsule_manifest(
        self,
        *,
        runtime_chat_id: str,
        capsule_ref: str,
        verify_values: bool,
    ) -> KernelCapsuleManifest:
        chat_id = str(runtime_chat_id or "").strip()
        ref = str(capsule_ref or "").strip()
        if not chat_id or not ref:
            raise KernelCapsuleError(
                "capsule_identity_required",
                "Capsule lookup requires runtime_chat_id and capsule_ref.",
            )
        try:
            metadata = self.manager.artifact_store.stat(ref, scope=chat_id, verify=True)
            payload = self.manager.artifact_store.read_bytes_scoped(ref, chat_id)
        except PermissionError as exc:
            raise KernelCapsuleError(
                "capsule_scope_denied",
                "Kernel capsule is not granted to this runtime_chat_id.",
            ) from exc
        except Exception as exc:
            raise KernelCapsuleError(
                "capsule_manifest_unavailable",
                "Kernel capsule manifest is unavailable or corrupt.",
            ) from exc
        if str(metadata.kind) != "kernel_capsule_manifest":
            raise KernelCapsuleError(
                "capsule_manifest_invalid",
                "Artifact is not a kernel capsule manifest.",
            )
        if len(payload) > int(self.manager.capsule_limits.max_manifest_bytes):
            raise KernelCapsuleError(
                "capsule_manifest_too_large",
                "Kernel capsule manifest exceeds the host-owned byte bound.",
            )
        try:
            raw = json.loads(payload.decode("utf-8", errors="strict"))
            if not isinstance(raw, dict):
                raise TypeError("manifest is not an object")
            manifest = KernelCapsuleManifest.from_dict(raw).with_artifact_ref(ref)
        except KernelCapsuleError:
            raise
        except Exception as exc:
            raise KernelCapsuleError(
                "capsule_manifest_invalid",
                "Kernel capsule manifest is not valid strict JSON.",
            ) from exc
        if not str(manifest.runtime_chat_id or "").strip():
            raise KernelCapsuleError(
                "capsule_manifest_invalid",
                "Kernel capsule manifest has no origin runtime_chat_id.",
            )
        if len(manifest.values) > int(self.manager.capsule_limits.max_values):
            raise KernelCapsuleError(
                "capsule_value_limit", "Kernel capsule has too many value entries."
            )
        if len(manifest.excluded_values) > int(
            self.manager.capsule_limits.max_excluded_values
        ):
            raise KernelCapsuleError(
                "capsule_exclusion_limit",
                "Kernel capsule has too many exclusion entries.",
            )
        expected_identity_digests = {
            "app_digest": self._capsule_digest({
                "name": "VARIANT-1", "version": manifest.app_version
            }),
            "python_digest": self._capsule_digest(dict(manifest.python)),
            "platform_digest": self._capsule_digest(dict(manifest.platform)),
        }
        for field_name, expected_digest in expected_identity_digests.items():
            if str(getattr(manifest, field_name, "") or "") != expected_digest:
                raise KernelCapsuleError(
                    "capsule_manifest_invalid",
                    f"Kernel capsule {field_name} does not match its identity document.",
                )
        try:
            expected_profile = runtime_profile(manifest.runtime_profile_id)
        except ValueError as exc:
            raise KernelCapsuleError(
                "capsule_manifest_invalid",
                "Kernel capsule runtime profile identifier is unknown.",
            ) from exc
        if manifest.runtime_profile and (
            dict(manifest.runtime_profile) != expected_profile.to_dict()
            or manifest.runtime_profile_digest != expected_profile.digest
        ):
            raise KernelCapsuleError(
                "capsule_manifest_invalid",
                "Kernel capsule runtime profile does not match its pinned digest.",
            )
        expected_refs = tuple(item.artifact_ref for item in manifest.values)
        if expected_refs != manifest.artifact_refs:
            raise KernelCapsuleError(
                "capsule_manifest_invalid",
                "Kernel capsule artifact reference ledger is inconsistent.",
            )
        if verify_values:
            total = 0
            for item in manifest.values:
                try:
                    value_metadata = self.manager.artifact_store.stat(
                        item.artifact_ref, scope=chat_id, verify=True
                    )
                except Exception as exc:
                    raise KernelCapsuleError(
                        "capsule_value_unavailable",
                        f"Kernel capsule value {item.name!r} is unavailable or corrupt.",
                    ) from exc
                if (
                    str(value_metadata.sha256) != item.sha256
                    or int(value_metadata.bytes) != int(item.bytes)
                ):
                    raise KernelCapsuleError(
                        "capsule_integrity_error",
                        f"Kernel capsule value {item.name!r} metadata does not match its manifest.",
                    )
                if int(item.bytes) > int(self.manager.capsule_limits.max_value_bytes):
                    raise KernelCapsuleError(
                        "capsule_value_too_large",
                        f"Kernel capsule value {item.name!r} exceeds its byte bound.",
                    )
                total += int(item.bytes)
                if total > int(self.manager.capsule_limits.max_total_value_bytes):
                    raise KernelCapsuleError(
                        "capsule_total_too_large",
                        "Kernel capsule values exceed the total byte bound.",
                    )
        return manifest

    def _capsule_compatibility(
        self,
        manifest: KernelCapsuleManifest,
        *,
        identity: Any,
        workspace_digest: str,
        target_registry: dict[str, Any] | None = None,
    ) -> KernelCapsuleCompatibility:
        checks: list[KernelCapsuleCompatibilityCheck] = []

        def global_check(
            field_name: str,
            capsule_value: Any,
            runtime_value: Any,
            *,
            blocking: bool,
        ) -> None:
            capsule_text = str(capsule_value or "")
            runtime_text = str(runtime_value or "")
            matches = capsule_text == runtime_text
            checks.append(KernelCapsuleCompatibilityCheck(
                field=field_name,
                capsule_value=capsule_text,
                runtime_value=runtime_text,
                status="match" if matches else "mismatch",
                blocking=bool(blocking),
                message=(
                    ""
                    if matches
                    else (
                        "restore is blocked"
                        if blocking
                        else "portable subset can restore with this warning"
                    )
                ),
            ))

        global_check("schema", manifest.schema, KERNEL_CAPSULE_SCHEMA, blocking=True)
        global_check(
            "python.major_minor",
            manifest.python.get("major_minor"),
            f"{sys.version_info.major}.{sys.version_info.minor}",
            blocking=False,
        )
        global_check(
            "platform.system",
            manifest.platform.get("system"),
            platform.system(),
            blocking=False,
        )
        global_check(
            "platform.machine",
            manifest.platform.get("machine"),
            platform.machine(),
            blocking=False,
        )
        global_check(
            "app_version", manifest.app_version, self.manager.app_version, blocking=False
        )
        global_check(
            "environment_digest",
            manifest.environment_digest,
            str(identity.environment_digest or ""),
            blocking=False,
        )
        global_check(
            "catalog_digest",
            manifest.catalog_digest,
            self._capsule_catalog_digest(identity),
            blocking=False,
        )
        global_check(
            "workspace_digest",
            manifest.workspace_digest,
            workspace_digest or manifest.workspace_digest,
            blocking=False,
        )
        global_check(
            "runtime_profile_id",
            manifest.runtime_profile_id,
            self.manager.runtime_profile.profile_id,
            blocking=False,
        )
        global_check(
            "runtime_profile_digest",
            manifest.runtime_profile_digest,
            self.manager.runtime_profile.digest,
            blocking=False,
        )

        selected_registry = (
            dict(target_registry)
            if isinstance(target_registry, dict)
            else serializer_registry_document(self.manager.runtime_profile.packages)
        )
        target_rows = selected_registry.get("serializers")
        target_serializers = {
            str(item.get("id") or ""): dict(item)
            for item in (target_rows if isinstance(target_rows, list) else ())
            if isinstance(item, dict) and str(item.get("id") or "")
        }

        def version_family(value: Any) -> str:
            text = str(value or "")
            return text.split(".", 1)[0] if text else ""

        restorable: list[str] = []
        skipped: list[str] = []
        schema_matches = manifest.schema == KERNEL_CAPSULE_SCHEMA
        for item in manifest.values:
            requirements = dict(item.requirements or {})
            descriptor = target_serializers.get(item.serializer) or {}
            available = bool(descriptor.get("available", True)) and bool(descriptor)
            expected_revision = int(requirements.get("serializer_revision") or 0)
            runtime_revision = int(descriptor.get("revision") or 0)
            serializer_matches = available and (
                not expected_revision or expected_revision == runtime_revision
            )
            checks.append(KernelCapsuleCompatibilityCheck(
                field=f"value.{item.name}.serializer",
                capsule_value=(
                    f"{item.serializer}@{expected_revision}"
                    if expected_revision
                    else item.serializer
                ),
                runtime_value=(
                    f"{item.serializer}@{runtime_revision}"
                    if available
                    else "unavailable"
                ),
                status="match" if serializer_matches else "mismatch",
                blocking=True,
                message=(
                    ""
                    if serializer_matches
                    else "this value's serializer is unavailable or has a different revision"
                ),
                value_names=(item.name,),
            ))
            value_matches = schema_matches and serializer_matches

            required_packages = requirements.get("packages")
            runtime_packages = descriptor.get("packages")
            if isinstance(required_packages, dict):
                runtime_package_map = (
                    dict(runtime_packages) if isinstance(runtime_packages, dict) else {}
                )
                for package_name, captured_version in sorted(
                    required_packages.items()
                ):
                    runtime_version = str(runtime_package_map.get(package_name) or "")
                    package_matches = bool(runtime_version) and (
                        not str(captured_version or "")
                        or version_family(captured_version)
                        == version_family(runtime_version)
                    )
                    checks.append(KernelCapsuleCompatibilityCheck(
                        field=f"value.{item.name}.package.{package_name}",
                        capsule_value=str(captured_version or ""),
                        runtime_value=runtime_version,
                        status="match" if package_matches else "mismatch",
                        blocking=True,
                        message=(
                            ""
                            if package_matches
                            else "this typed value requires a compatible package family"
                        ),
                        value_names=(item.name,),
                    ))
                    value_matches = value_matches and package_matches

            required_python = str(requirements.get("python_major_minor") or "")
            if required_python:
                runtime_python = f"{sys.version_info.major}.{sys.version_info.minor}"
                python_matches = required_python == runtime_python
                checks.append(KernelCapsuleCompatibilityCheck(
                    field=f"value.{item.name}.python_major_minor",
                    capsule_value=required_python,
                    runtime_value=runtime_python,
                    status="match" if python_matches else "mismatch",
                    blocking=True,
                    message=(
                        "" if python_matches else "this value requires the captured Python ABI"
                    ),
                    value_names=(item.name,),
                ))
                value_matches = value_matches and python_matches

            if bool(requirements.get("workspace_sensitive")):
                runtime_workspace = workspace_digest or manifest.workspace_digest
                workspace_matches = manifest.workspace_digest == runtime_workspace
                checks.append(KernelCapsuleCompatibilityCheck(
                    field=f"value.{item.name}.workspace_digest",
                    capsule_value=manifest.workspace_digest,
                    runtime_value=runtime_workspace,
                    status="match" if workspace_matches else "mismatch",
                    blocking=True,
                    message=(
                        ""
                        if workspace_matches
                        else "this value's reconstructable type is workspace-sensitive"
                    ),
                    value_names=(item.name,),
                ))
                value_matches = value_matches and workspace_matches

            if bool(requirements.get("environment_sensitive")):
                runtime_environment = str(identity.environment_digest or "")
                environment_matches = (
                    manifest.environment_digest == runtime_environment
                )
                checks.append(KernelCapsuleCompatibilityCheck(
                    field=f"value.{item.name}.environment_digest",
                    capsule_value=manifest.environment_digest,
                    runtime_value=runtime_environment,
                    status="match" if environment_matches else "mismatch",
                    blocking=True,
                    message=(
                        ""
                        if environment_matches
                        else "this value's reconstructable type is environment-sensitive"
                    ),
                    value_names=(item.name,),
                ))
                value_matches = value_matches and environment_matches

            (restorable if value_matches else skipped).append(item.name)

        if not schema_matches:
            restorable = []
            skipped = [item.name for item in manifest.values]
        if not schema_matches:
            verdict = "incompatible"
        elif not skipped:
            verdict = "compatible"
        elif restorable:
            verdict = "partial"
        else:
            verdict = "incompatible"
        return KernelCapsuleCompatibility(
            verdict=verdict,
            checks=tuple(checks),
            restorable_names=tuple(sorted(restorable)),
            skipped_names=tuple(sorted(skipped)),
        )

    async def _worker_serializer_registry(
        self,
        lease: KernelLease,
    ) -> dict[str, Any]:
        """Read codec availability from the admitted target worker itself."""

        request = {"limit": 1, "limits": self._capsule_worker_limits()}
        async with lease.execution_lock:
            if lease.state != "ready" or lease._closed:
                raise KernelCapsuleError(
                    "kernel_not_idle",
                    "Kernel generation became unavailable before codec inspection.",
                )
            response = await lease._capsule_request(
                "inspect",
                request,
                max_response_bytes=(
                    self.manager.capsule_limits.max_worker_response_bytes
                ),
            )
        if str(response.get("schema") or "") != WORKER_NAMESPACE_SCHEMA:
            raise KernelCapsuleError(
                "namespace_worker_schema_unsupported",
                "Kernel worker returned an unsupported codec inspection schema.",
            )
        registry = response.get("serializer_registry")
        if (
            not isinstance(registry, dict)
            or str(registry.get("schema") or "") != SERIALIZER_REGISTRY_SCHEMA
            or not isinstance(registry.get("serializers"), list)
        ):
            raise KernelCapsuleError(
                "capsule_serializer_registry_invalid",
                "Kernel worker returned an invalid serializer registry.",
            )
        return dict(registry)

    def inspect_capsule(
        self,
        *,
        runtime_chat_id: str,
        capsule_ref: str,
        workspace_digest: str = "",
    ) -> KernelCapsuleInspection:
        """Verify one scoped manifest and evaluate it against a runtime."""

        chat_id = str(runtime_chat_id or "").strip()
        _record, identity, _roots, _workspace_root, _computed = (
            self._capsule_runtime_context(chat_id)
        )
        manifest = self._load_capsule_manifest(
            runtime_chat_id=chat_id,
            capsule_ref=capsule_ref,
            verify_values=True,
        )
        compatibility = self._capsule_compatibility(
            manifest,
            identity=identity,
            workspace_digest=str(workspace_digest or "") or manifest.workspace_digest,
        )
        return KernelCapsuleInspection(
            manifest=manifest,
            compatibility=compatibility,
            verified=True,
        )

    async def restore_capsule(
        self,
        *,
        runtime_chat_id: str,
        capsule_ref: str,
        workspace_roots: tuple[str, ...] = (),
        workspace_digest: str = "",
        workspace_revision: int = 0,
        require_compatible: bool = True,
        _suppress_auto_restore: bool = True,
        _target_lease: KernelLease | None = None,
    ) -> KernelCapsuleRestoreResult:
        """Replace user-owned values from a verified capsule at idle."""

        if self.manager._closed:
            raise KernelCapsuleError(
                "kernel_manager_closed", "Kernel manager is shut down."
            )
        chat_id = str(runtime_chat_id or "").strip()
        _record, identity, roots, workspace_root, effective_workspace_digest = (
            self._capsule_runtime_context(
                chat_id,
                workspace_roots=workspace_roots,
                workspace_digest=workspace_digest,
            )
        )
        manifest = self._load_capsule_manifest(
            runtime_chat_id=chat_id,
            capsule_ref=capsule_ref,
            verify_values=True,
        )
        effective_roots = roots or (workspace_root,)
        target_fingerprint = self.manager._workspace_fingerprint(
            effective_roots, workspace_revision
        )
        if _target_lease is not None:
            lease = _target_lease
            if (
                lease.chat_id != chat_id
                or lease._closed
                or not self.manager._lease_identity_matches(
                    lease, identity, target_fingerprint
                )
                or lease.workspace_roots != effective_roots
                or lease.workspace_revision != max(
                    0, int(workspace_revision or 0)
                )
            ):
                raise KernelCapsuleError(
                    "kernel_restore_target_stale",
                    "The admitted kernel lease no longer matches the exact restore target.",
                )
        else:
            lease = await self.manager._lease(
                chat_id,
                identity,
                workspace_root,
                target_fingerprint,
                workspace_roots=effective_roots,
                workspace_revision=workspace_revision,
                auto_restore=not bool(_suppress_auto_restore),
            )
        compatibility = self._capsule_compatibility(
            manifest,
            identity=identity,
            workspace_digest=effective_workspace_digest,
            target_registry=await self._worker_serializer_registry(lease),
        )
        if require_compatible and not compatibility.restorable:
            raise KernelCapsuleError(
                "capsule_incompatible",
                "No kernel capsule values are compatible with the target runtime.",
                details={"compatibility": compatibility.to_dict()},
            )
        selected_names = (
            set(compatibility.restorable_names)
            if require_compatible
            else {item.name for item in manifest.values}
        )
        selected_values = [
            item for item in manifest.values if item.name in selected_names
        ]
        restore_values: list[dict[str, Any]] = []
        total = 0
        for item in selected_values:
            try:
                payload = self.manager.artifact_store.read_bytes_scoped(
                    item.artifact_ref, chat_id
                )
            except Exception as exc:
                raise KernelCapsuleError(
                    "capsule_value_unavailable",
                    f"Kernel capsule value {item.name!r} could not be read.",
                ) from exc
            if (
                len(payload) != int(item.bytes)
                or hashlib.sha256(payload).hexdigest() != item.sha256
            ):
                raise KernelCapsuleError(
                    "capsule_integrity_error",
                    f"Kernel capsule value {item.name!r} failed restore integrity verification.",
                )
            total += len(payload)
            if (
                len(payload) > int(self.manager.capsule_limits.max_value_bytes)
                or total > int(self.manager.capsule_limits.max_total_value_bytes)
            ):
                raise KernelCapsuleError(
                    "capsule_total_too_large",
                    "Kernel capsule restore payload exceeds host-owned bounds.",
                )
            restore_values.append({
                "name": item.name,
                "type": item.type_name,
                "serializer": item.serializer,
                "sha256": item.sha256,
                "bytes": int(item.bytes),
                "requirements": dict(item.requirements),
                "data_b64": base64.b64encode(payload).decode("ascii"),
            })
        worker_payload = {
            "schema": WORKER_CAPSULE_SCHEMA,
            "values": restore_values,
        }
        document, refs = self.manager.namespace_document(chat_id, identity)
        lease._refs.update(refs)
        async with lease.execution_lock:
            if lease.state != "ready" or lease._closed:
                raise KernelCapsuleError(
                    "kernel_not_idle",
                    "Kernel generation became unavailable before capsule restore.",
                )
            await lease._sync_namespace(document)
            response = await lease._capsule_request(
                "restore",
                worker_payload,
                max_response_bytes=self.manager.capsule_limits.max_worker_response_bytes,
            )
        if str(response.get("schema") or "") != WORKER_CAPSULE_SCHEMA:
            raise KernelCapsuleError(
                "capsule_restore_unknown_effect",
                "Kernel applied a restore but returned an invalid completion schema.",
            )
        restored_names = tuple(str(item) for item in response.get("restored_names") or ())
        expected_names = tuple(sorted(item.name for item in selected_values))
        if tuple(sorted(restored_names)) != expected_names:
            raise KernelCapsuleError(
                "capsule_restore_unknown_effect",
                "Kernel restore completion does not match the manifest value set.",
            )
        result = KernelCapsuleRestoreResult(
            runtime_chat_id=chat_id,
            capsule_ref=str(capsule_ref),
            kernel_generation=int(lease.generation),
            restored_names=tuple(sorted(restored_names)),
            removed_names=tuple(
                sorted(str(item) for item in response.get("removed_names") or ())
            ),
            namespace_reinstalled=bool(response.get("namespace_reinstalled")),
            compatibility=compatibility,
            skipped_names=(
                tuple(sorted(compatibility.skipped_names))
                if require_compatible
                else ()
            ),
        )
        if not result.namespace_reinstalled:
            raise KernelCapsuleError(
                "capsule_restore_unknown_effect",
                "Kernel restore did not confirm host namespace reinstallation.",
            )
        self._latest_capsules[chat_id] = manifest
        try:
            lineage_ref = self._record_capsule_pointer(
                manifest, reason="restore", runtime_chat_id=chat_id
            )
            result = replace(
                result,
                lineage_persisted=True,
                lineage_ref=lineage_ref,
                lineage_error="",
            )
        except Exception as exc:
            lineage_error = f"{type(exc).__name__}: {exc}"[:500]
            self._capsule_lineage_errors[chat_id] = lineage_error
            result = replace(
                result,
                lineage_persisted=False,
                lineage_ref="",
                lineage_error=lineage_error,
            )
        self.manager.emit(
            "kernel:capsule_restored",
            status="ok",
            chat_id=chat_id,
            kernel_generation=lease.generation,
            capsule_ref=str(capsule_ref),
            values=len(result.restored_names),
        )
        return result

    async def fork_capsule(
        self,
        *,
        source_runtime_chat_id: str,
        target_runtime_chat_id: str,
        capsule_ref: str = "",
        source_workspace_roots: tuple[str, ...] = (),
        source_workspace_digest: str = "",
        target_workspace_roots: tuple[str, ...] = (),
        target_workspace_digest: str = "",
    ) -> KernelCapsuleForkResult:
        """Grant one capsule to a distinct runtime chat and restore it there."""

        source_chat = str(source_runtime_chat_id or "").strip()
        target_chat = str(target_runtime_chat_id or "").strip()
        if not source_chat or not target_chat:
            raise KernelCapsuleError(
                "runtime_chat_id_required",
                "Capsule fork requires explicit source and target runtime_chat_id values.",
            )
        if source_chat == target_chat:
            raise KernelCapsuleError(
                "capsule_fork_target_invalid",
                "Capsule fork target must be a distinct runtime_chat_id.",
            )
        created = not bool(str(capsule_ref or "").strip())
        if created:
            created_manifest = await self.create_capsule(
                runtime_chat_id=source_chat,
                workspace_roots=source_workspace_roots,
                workspace_digest=source_workspace_digest,
            )
            ref = created_manifest.artifact_ref
            manifest = created_manifest
        else:
            ref = str(capsule_ref).strip()
            manifest = self._load_capsule_manifest(
                runtime_chat_id=source_chat,
                capsule_ref=ref,
                verify_values=True,
            )
        # Validate the target runtime before mutating the scoped grant catalog.
        (
            _target_record,
            target_identity,
            target_roots,
            target_workspace_root,
            effective_target_workspace_digest,
        ) = self._capsule_runtime_context(
            target_chat,
            workspace_roots=target_workspace_roots,
            workspace_digest=(
                target_workspace_digest
                or (manifest.workspace_digest if not target_workspace_roots else "")
            ),
        )
        target_effective_roots = target_roots or (target_workspace_root,)
        target_fingerprint = self.manager._workspace_fingerprint(
            target_effective_roots, 0
        )
        target_lease = await self.manager._lease(
            target_chat,
            target_identity,
            target_workspace_root,
            target_fingerprint,
            workspace_roots=target_effective_roots,
            workspace_revision=0,
            auto_restore=False,
        )
        target_compatibility = self._capsule_compatibility(
            manifest,
            identity=target_identity,
            workspace_digest=effective_target_workspace_digest,
            target_registry=await self._worker_serializer_registry(target_lease),
        )
        if not target_compatibility.restorable:
            raise KernelCapsuleError(
                "capsule_incompatible",
                "Kernel capsule is incompatible with the fork target runtime.",
                details={"compatibility": target_compatibility.to_dict()},
            )
        refs_to_grant = (ref, *manifest.artifact_refs)
        granted: list[str] = []
        for artifact_ref in refs_to_grant:
            try:
                grant = self.manager.artifact_store.grant(
                    artifact_ref,
                    target_chat,
                    source_scope=source_chat,
                    verify=True,
                )
            except Exception as exc:
                raise KernelCapsuleError(
                    "capsule_grant_failed",
                    "Could not grant the capsule artifact set to the target runtime.",
                    details={"artifact_ref": artifact_ref},
                ) from exc
            granted.append(str(grant.ref))
        restore = await self.restore_capsule(
            runtime_chat_id=target_chat,
            capsule_ref=ref,
            workspace_roots=target_workspace_roots,
            workspace_digest=(
                target_workspace_digest
                or (manifest.workspace_digest if not target_workspace_roots else "")
            ),
            _target_lease=target_lease,
        )
        result = KernelCapsuleForkResult(
            source_runtime_chat_id=source_chat,
            target_runtime_chat_id=target_chat,
            capsule_ref=ref,
            created_capsule=created,
            granted_artifact_refs=tuple(granted),
            restore=restore,
        )
        self.manager.emit(
            "kernel:capsule_forked",
            status="ok",
            source_chat_id=source_chat,
            target_chat_id=target_chat,
            capsule_ref=ref,
            artifacts=len(granted),
        )
        return result

    async def checkpoint(
        self,
        runtime_chat_id: str,
        *,
        reason: str = "model_requested",
    ) -> dict[str, Any]:
        """Capture one explicit idle checkpoint without closing the lease."""

        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise KernelContinuityError(
                "runtime_chat_id_required",
                "Kernel checkpoint requires a durable runtime_chat_id.",
            )
        selected_reason = str(reason or "model_requested")[:512]
        lease = self.manager._leases.get(chat_id)
        if lease is None or lease._closed:
            outcome = self._persist_checkpoint_outcome({
                "schema": KERNEL_CHECKPOINT_OUTCOME_SCHEMA,
                "runtime_chat_id": chat_id,
                "kernel_generation": 0,
                "boundary": selected_reason,
                "status": "failed",
                "reason": "kernel_not_live",
                "capsule_ref": None,
            })
            self.manager.emit(
                "kernel:checkpoint_outcome",
                status="failed",
                chat_id=chat_id,
                kernel_generation=0,
                boundary=selected_reason,
                reason="kernel_not_live",
                capsule_ref="",
            )
            return outcome
        if lease.state == "busy":
            raise KernelContinuityError(
                "kernel_not_idle",
                "Kernel checkpoint must wait for the current cell to become idle.",
                details={"kernel_generation": int(lease.generation)},
            )
        return await self._checkpoint_lease(
            lease,
            reason=selected_reason,
            force=True,
        )

    async def _checkpoint_lease(
        self,
        lease: KernelLease,
        *,
        reason: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Attempt one configured checkpoint and always report exact truth."""

        selected_reason = str(reason or "lifecycle_close")
        outcome: dict[str, Any] = {
            "schema": KERNEL_CHECKPOINT_OUTCOME_SCHEMA,
            "runtime_chat_id": lease.chat_id,
            "kernel_generation": int(lease.generation),
            "boundary": selected_reason,
            "status": "not_requested",
            "reason": "boundary_not_configured",
            "capsule_ref": None,
        }
        policy = self._effective_checkpoint_policy(lease.chat_id)
        configured = bool(force) or selected_reason in set(policy.reasons)
        if configured and not force and not policy.enabled:
            outcome.update(status="skipped", reason="policy_disabled")
        elif configured and (
            lease.process is None or lease.process.poll() is not None
        ):
            outcome.update(status="failed", reason="kernel_process_died")
        elif configured and (lease._closed or lease.state != "ready"):
            outcome.update(
                status="skipped",
                reason=("kernel_closed" if lease._closed else "kernel_not_idle"),
            )
        elif configured:
            try:
                manifest = await self.create_capsule(
                    runtime_chat_id=lease.chat_id,
                    workspace_roots=lease.workspace_roots,
                    _lifecycle_checkpoint=True,
                )
                outcome.update(
                    status="captured",
                    reason="",
                    capsule_ref=manifest.artifact_ref,
                    values=len(manifest.values),
                    excluded_values=len(manifest.excluded_values),
                )
            except Exception as exc:
                outcome.update(
                    status="failed",
                    reason=(
                        exc.code
                        if isinstance(exc, KernelCapsuleError)
                        else type(exc).__name__
                    ),
                    message=str(exc)[:500],
                )
        outcome = self._persist_checkpoint_outcome(outcome)
        self.manager.emit(
            "kernel:checkpoint_outcome",
            status=str(outcome["status"]),
            chat_id=lease.chat_id,
            kernel_generation=int(lease.generation),
            boundary=selected_reason,
            reason=str(outcome.get("reason") or ""),
            capsule_ref=str(outcome.get("capsule_ref") or ""),
        )
        return outcome

    async def close_lease_with_checkpoint(
        self,
        lease: KernelLease,
        *,
        reason: str,
        hard: bool = False,
    ) -> dict[str, Any]:
        checkpoint = await self._checkpoint_lease(lease, reason=reason)
        await lease.close(reason=reason, hard=hard)
        return checkpoint

    async def bounded_namespace_view(
        self,
        runtime_chat_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Inspect names/types/serializers at idle without returning live values."""

        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise ValueError("runtime_chat_id is required")
        lease = self.manager._leases.get(chat_id)
        if lease is None or lease._closed:
            raise KernelUnavailable("no live kernel generation exists")
        record = self.manager.registry.ensure_runtime(chat_id)
        if not self.manager._lease_identity_matches(lease, record.identity):
            raise KernelUnavailable("live kernel identity is stale")
        cap = max(1, min(int(limit or 100), 500))
        inspect_request = {
            "limit": cap,
            "limits": self._capsule_worker_limits(),
        }
        async with lease.execution_lock:
            capture = await lease._capsule_request(
                "inspect",
                inspect_request,
                max_response_bytes=self.manager.capsule_limits.max_worker_response_bytes,
            )
        if str(capture.get("schema") or "") != WORKER_NAMESPACE_SCHEMA:
            raise KernelCapsuleError(
                "namespace_worker_schema_unsupported",
                "Kernel worker returned an unsupported namespace schema.",
            )
        values = []
        for item in list(capture.get("values") or ())[:cap]:
            if not isinstance(item, dict):
                continue
            values.append({
                "name": str(item.get("name") or ""),
                "type": str(item.get("type") or ""),
                "serializer": str(item.get("serializer") or ""),
                "bytes": (
                    max(0, int(item.get("bytes") or 0))
                    if item.get("bytes") is not None
                    else None
                ),
                "size_known_without_serialization": bool(
                    item.get("size_known_without_serialization")
                ),
                "sha256": str(item.get("sha256") or ""),
            })
        remaining = max(0, cap - len(values))
        excluded = []
        for item in list(capture.get("excluded") or ())[:remaining]:
            if not isinstance(item, dict):
                continue
            excluded.append({
                "name": str(item.get("name") or ""),
                "type": str(item.get("type") or ""),
                "reason": str(item.get("reason") or ""),
            })
        return {
            "schema": "variant1.kernel-namespace-view.v1",
            "runtime_chat_id": chat_id,
            "kernel_generation": int(lease.generation),
            "workspace_fingerprint": lease.workspace_fingerprint,
            "values": values,
            "excluded": excluded,
            "truncated": bool(capture.get("truncated")),
            "inspection_mode": "metadata_only",
        }

__all__ = ["KernelContinuityCoordinator"]
