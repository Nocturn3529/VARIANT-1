"""Host-owned lifecycle manager for persistent CPython workers."""

from __future__ import annotations

import asyncio
from contextlib import suppress, nullcontext
import base64
import json
import os
import re
import secrets
import sys
import time
import uuid
from typing import Any, Callable

from capability_broker import CapabilityRef
from core_invariants import canonical_digest
from session_catalog.profiles import is_action_surface
from work_fabric.scope import coerce_work_scope

from .cell_ledger import CELL_LEDGER_SCHEMA, KernelCellLedgerStore
from .contracts import (
    ExecutionAdmission,
    KernelAutoRestoreError,
    KernelContinuityError,
    KernelExecutionError,
    KernelExecutionResult,
    KernelLimits,
    KernelUnavailable,
)
from .continuity import KernelContinuityCoordinator
from .lease import KernelLease, _restrict_path, _safe_chat_dir
from .output import OUTPUT_EVENT_SCHEMA, CellOutput
from .runtime_profile import CORE_RUNTIME_PROFILE, runtime_profile
from .worker_path import packaged_kernel_executable
from .capsules import (
    KERNEL_CAPSULE_APP_VERSION,
    KernelCapsuleCompatibility,
    KernelCapsuleForkResult,
    KernelCapsuleInspection,
    KernelCapsuleLimits,
    KernelCapsuleManifest,
    KernelCapsuleRestoreResult,
    KernelCheckpointPolicy,
)


class KernelRuntimeManager:
    """Process service mapping durable chat IDs to lazy kernel leases."""

    def __init__(
        self,
        *,
        registry: Any,
        broker: Any,
        artifact_store: Any,
        root: str,
        instance_id: str,
        app_root: str,
        limits: KernelLimits | None = None,
        worker_executable: str = "",
        catalog_service: Any = None,
        new_kernel_allowed: Callable[[], bool] | None = None,
        fanout_limit_resolver: Callable[[str], int] | None = None,
        capsule_limits: KernelCapsuleLimits | None = None,
        checkpoint_policy: KernelCheckpointPolicy | None = None,
        runtime_profile_id: str = CORE_RUNTIME_PROFILE,
        app_version: str = KERNEL_CAPSULE_APP_VERSION,
        cell_ledger_path: str = "",
    ) -> None:
        self.registry = registry
        self.broker = broker
        self.artifact_store = artifact_store
        self.root = os.path.abspath(root)
        self.instance_id = str(instance_id or uuid.uuid4().hex)
        self.app_root = os.path.abspath(app_root)
        self.limits = limits or KernelLimits()
        self.worker_executable = os.path.abspath(worker_executable) if worker_executable else ""
        self.catalog_service = catalog_service
        self.new_kernel_allowed = new_kernel_allowed or (lambda: True)
        self.fanout_limit_resolver = fanout_limit_resolver
        self.capsule_limits = capsule_limits or KernelCapsuleLimits()
        self.checkpoint_policy = checkpoint_policy or KernelCheckpointPolicy()
        self.runtime_profile = runtime_profile(runtime_profile_id)
        self.app_version = str(app_version or KERNEL_CAPSULE_APP_VERSION)
        ledger_path = (
            os.path.abspath(cell_ledger_path)
            if str(cell_ledger_path or "").strip()
            else os.path.join(os.path.dirname(self.root), "kernel-cells.sqlite3")
        )
        self.cell_ledger = KernelCellLedgerStore(ledger_path)
        self._recover_interrupted_cell_evidence()
        self._leases: dict[str, KernelLease] = {}
        self.continuity = KernelContinuityCoordinator(self)
        self._boot_locks: dict[str, asyncio.Lock] = {}
        self._boot_slots = asyncio.Semaphore(max(1, self.limits.max_boot_concurrency))
        self._boot_tasks: set[asyncio.Task] = set()
        self._guard = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._closed = False
        self._shutting_down = False
        self._sweep_stale_roots()

    def fanout_limit(self, chat_id: str) -> int:
        """Return the host-owned nested batch cap for this chat's pinned route."""

        broker_limit = max(1, int(getattr(self.broker, "max_fanout", 8) or 8))
        resolver = self.fanout_limit_resolver
        if resolver is None:
            return broker_limit
        try:
            return max(1, min(int(resolver(str(chat_id)) or 1), broker_limit))
        except Exception:
            return 1

    def _recover_interrupted_cell_evidence(self) -> None:
        for pending in self.cell_ledger.unsettled():
            if pending.pop('instance_id') == self.instance_id:
                continue
            # This is evidence reconciliation only. Nothing from the source
            # is evaluated, and neither effects nor execution duration are known.
            result = {
                'schema': 'variant1.kernel-execution.v1', 'status': 'unknown_effect',
                'execution_id': pending['execution_id'], 'chat_id': pending['chat_id'],
                'kernel_generation': pending['kernel_generation'], 'duration_ms': None,
                'text': 'Host exited before cell completion was observed. Inspect existing effects; do not replay this source automatically.',
                'error': {'code':'kernel_host_interrupted', 'message':'Cell completion and effects are unknown after host exit.'},
            }
            try:
                artifact = self.artifact_store.put_json(result, kind='kernel_cell_result', scope=pending['chat_id'])
                self.cell_ledger.append(**pending, result_ref=artifact.ref, result_sha256=artifact.sha256,
                    status='unknown_effect', execution_count=0, completed_at=time.time(), duration_ms=0,
                    error_code='kernel_host_interrupted')
            except Exception as exc:
                self.emit('kernel:cell_ledger_failed', status='unknown_effect',
                          execution_id=pending['execution_id'], error=str(exc)[:500])

    @staticmethod
    def emit(event: str, **fields: Any) -> None:
        try:
            from observability.trace_events import record_trace_event

            record_trace_event(event, **fields)
        except Exception:
            pass

    def _sweep_stale_roots(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        _restrict_path(self.root, directory=True)
        from file_paths import remove_tree

        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            with suppress(OSError):
                remove_tree(path)

    def worker_command(self, _control_path: str = "") -> list[str]:
        if self.worker_executable:
            if not os.path.isfile(self.worker_executable):
                raise KernelUnavailable(
                    f"configured Variant1Kernel executable is missing: {self.worker_executable}"
                )
            return [self.worker_executable]
        if getattr(sys, "frozen", False):
            candidate = packaged_kernel_executable()
            if not os.path.isfile(candidate):
                raise KernelUnavailable(
                    "packaged Variant1Kernel executable is missing; system Python fallback is forbidden"
                )
            return [candidate]
        # Source development uses the exact interpreter already running the
        # backend venv.  It never resolves ``python`` through PATH.
        worker_main = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "worker_main.py"
        )
        return [sys.executable, worker_main]

    def namespace_document(
        self,
        chat_id: str,
        identity: Any,
        *,
        query: str = "",
    ) -> tuple[dict[str, Any], dict[str, CapabilityRef]]:
        if not is_action_surface(str(identity.action_surface)):
            raise KernelUnavailable(
                f"chat profile {identity.action_surface!r} does not admit ASTB"
            )
        if self.catalog_service is None:
            raise KernelUnavailable("capability catalog service is unavailable")
        document, refs = self.catalog_service.namespace_document(
            str(chat_id), identity, query=str(query or "")
        )
        document = dict(document)
        document["runtime_profile"] = self.runtime_profile.to_dict()
        return document, refs

    @staticmethod
    def _workspace_fingerprint(
        workspace_roots: tuple[str, ...],
        workspace_revision: int = 0,
    ) -> str:
        normalized = [
            os.path.normcase(os.path.abspath(str(path)))
            for path in workspace_roots
            if str(path or "").strip()
        ]
        return canonical_digest({
            "schema": "variant1.kernel-workspace-binding.v1",
            "revision": max(0, int(workspace_revision or 0)),
            "roots": normalized,
        })

    @staticmethod
    def _lease_identity_matches(
        lease: KernelLease,
        identity: Any,
        workspace_fingerprint: str = "",
    ) -> bool:
        pinned = lease.identity
        identity_matches = all(
            str(getattr(pinned, name, "") or "")
            == str(getattr(identity, name, "") or "")
            for name in (
                "action_surface",
                "provider_tool_schema_revision",
                "graph_revision",
                "catalog_release_id",
                "environment_digest",
                "trust_profile",
            )
        )
        return bool(
            identity_matches
            and lease.runtime_profile_digest == lease.manager.runtime_profile.digest
            and (
                not str(workspace_fingerprint or "")
                or lease.workspace_fingerprint == str(workspace_fingerprint)
            )
        )

    def _automatic_eviction_blocked(self, lease: KernelLease) -> bool:
        registry = getattr(getattr(self, "catalog_service", None), "runtime_registry", None)
        check = getattr(registry, "automatic_kernel_eviction_blocked", None)
        return callable(check) and check(lease.chat_id) is True

    async def _evict_for_capacity(self) -> None:
        await self.reap_idle()
        limit = int(self.limits.max_live_kernels)
        if limit <= 0:
            return
        while True:
            live = [lease for lease in self._leases.values() if not lease._closed]
            if len(live) < limit:
                # _lease installs the new candidate without another await. This
                # fresh count therefore reserves the slot until it is published.
                return
            candidates = [lease for lease in live if lease.state == "ready" and not self._automatic_eviction_blocked(lease)]
            if not candidates:
                raise KernelUnavailable("all admitted kernel slots are busy or pinned by active runs")
            victim = min(candidates, key=lambda item: item.last_used_at)
            await self._close_lease_serialized(
                victim,
                reason="capacity_eviction",
            )
            # Another boot may have selected this same victim while its
            # checkpoint awaited namespace synchronization, then filled its slot.

    async def _close_lease_serialized(
        self,
        lease: KernelLease,
        *,
        reason: str,
        hard: bool = False,
    ) -> dict[str, Any]:
        """Own teardown under the same per-chat lock used for boot/install."""

        lock = self._boot_locks.setdefault(str(lease.chat_id), asyncio.Lock())
        async with lock:
            current = self._leases.get(str(lease.chat_id))
            if current is not lease or lease._closed:
                return {}
            automatic = reason in {"capacity_eviction", "idle_or_absolute_eviction"}
            registry = getattr(getattr(self, "catalog_service", None), "runtime_registry", None)
            claim = getattr(registry, "claim_kernel_retirement", None)
            fence = claim(lease.chat_id) if automatic and callable(claim) else nullcontext(True)
            with fence as allowed:
                if not allowed:
                    return {}
                return await self.continuity.close_lease_with_checkpoint(
                    lease, reason=reason, hard=hard,
                )

    def _require_kernel_admission(self) -> None:
        if self._closed or self._shutting_down:
            raise KernelUnavailable("kernel manager is shutting down")

    async def _lease(
        self,
        chat_id: str,
        identity: Any,
        workspace_root: str,
        workspace_fingerprint: str,
        *,
        workspace_roots: tuple[str, ...] = (),
        workspace_revision: int = 0,
        auto_restore: bool = True,
    ) -> KernelLease:
        clean = str(chat_id or "").strip()
        if not clean:
            raise ValueError("chat_id is required for a kernel")
        self._require_kernel_admission()
        effective_roots = tuple(
            os.path.abspath(str(path))
            for path in workspace_roots
            if str(path or "").strip()
        ) or (os.path.abspath(workspace_root),)
        effective_revision = max(0, int(workspace_revision or 0))
        lock = self._boot_locks.setdefault(clean, asyncio.Lock())
        async with lock:
            self._require_kernel_admission()
            existing = self._leases.get(clean)
            if (
                existing is not None
                and not existing._closed
                and (
                    existing.process is None
                    or existing.process.poll() is not None
                )
            ):
                await existing.close(reason="dead_generation_reaped", hard=True)
                existing = None
            if existing is not None and not existing._closed:
                if (
                    existing.state in {"ready", "busy"}
                    and self._lease_identity_matches(
                        existing, identity, workspace_fingerprint
                    )
                ):
                    if auto_restore:
                        await self.continuity.maybe_auto_restore(existing)
                    else:
                        existing._auto_restore_attempted = True
                    return existing
                if existing.state == "busy":
                    async with existing.execution_lock:
                        self._require_kernel_admission()
                        if not existing._closed:
                            await existing.close(
                                reason="runtime_identity_changed", hard=True
                            )
                if not existing._closed:
                    await existing.close(
                        reason="runtime_identity_changed", hard=True
                    )
            if not bool(self.new_kernel_allowed()):
                raise KernelUnavailable(
                    "starting new VARIANT-1 kernels is disabled by host policy"
                )
            async with self._boot_slots:
                self._require_kernel_admission()
                current_task = asyncio.current_task()
                if current_task is None:
                    raise KernelUnavailable("kernel boot has no owning task")
                async with self._guard:
                    self._require_kernel_admission()
                    self._boot_tasks.add(current_task)
                try:
                    last_error: BaseException | None = None
                    for boot_attempt in range(2):
                        self._require_kernel_admission()
                        await self._evict_for_capacity()
                        self._require_kernel_admission()
                        generation = int(self.registry.advance_kernel_generation(clean))
                        chat_root = os.path.join(self.root, _safe_chat_dir(clean))
                        os.makedirs(chat_root, exist_ok=True)
                        _restrict_path(chat_root, directory=True)
                        generation_root = os.path.join(
                            chat_root,
                            f"generation-{generation:08d}-{secrets.token_hex(8)}",
                        )
                        lease = KernelLease(
                            manager=self,
                            chat_id=clean,
                            generation=generation,
                            identity=identity,
                            generation_root=generation_root,
                            workspace_root=workspace_root,
                            workspace_fingerprint=workspace_fingerprint,
                            workspace_roots=effective_roots,
                            workspace_revision=effective_revision,
                        )
                        try:
                            # Registry admission is authoritative. Publish the
                            # candidate in the manager map only after it owns that
                            # durable chat's live-lease slot.
                            self.registry.install_kernel_lease(clean, lease)
                        except Exception as exc:
                            await lease.close(
                                reason="registry_install_failed", hard=True
                            )
                            raise KernelUnavailable(
                                "kernel lease registration was rejected"
                            ) from exc
                        self._leases[clean] = lease
                        try:
                            self._require_kernel_admission()
                            await lease.start()
                            self._require_kernel_admission()
                            if auto_restore:
                                await self.continuity.maybe_auto_restore(lease)
                            else:
                                lease._auto_restore_attempted = True
                            return lease
                        except KernelUnavailable as exc:
                            if isinstance(exc, KernelAutoRestoreError):
                                raise
                            last_error = exc
                            self.emit(
                                "kernel:boot_retry",
                                status="error",
                                chat_id=clean,
                                kernel_generation=generation,
                                attempt=boot_attempt + 1,
                                error=str(exc)[:500],
                            )
                            if boot_attempt:
                                raise
                    assert last_error is not None
                    raise last_error
                finally:
                    async with self._guard:
                        self._boot_tasks.discard(current_task)

    def lease_closed(self, chat_id: str, lease: KernelLease) -> None:
        if self._leases.get(str(chat_id)) is lease:
            self._leases.pop(str(chat_id), None)
        with suppress(Exception):
            self.registry.remove_kernel_lease(str(chat_id), lease)

    async def execute(
        self,
        *,
        chat_id: str,
        code: str,
        run_id: str,
        outer_tool_call_id: str,
        workspace_roots: tuple[str, ...] = (),
        work_scope: dict[str, Any] | None = None,
        cancellation: Any = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        timeout_s: float | None = None,
    ) -> KernelExecutionResult:
        if self._closed:
            raise KernelUnavailable("kernel manager is shut down")
        roots = tuple(os.path.abspath(path) for path in workspace_roots if str(path or "").strip())
        workspace_root = roots[0] if roots else self.app_root
        effective_roots = roots or (workspace_root,)
        requested_scope = coerce_work_scope(work_scope)
        workspace_revision = int(requested_scope.workspace_revision or 0)
        workspace_fingerprint = self._workspace_fingerprint(
            effective_roots, workspace_revision
        )
        lease = None
        namespace_document: dict[str, Any] = {}
        refs: dict[str, Any] = {}
        identity = None
        # Lease boot/restore and namespace projection may follow a handler-only
        # catalog publication or recover an LKG pin. Repeat from the durable
        # identity until the exact lease/document/admission revision is stable.
        for _identity_attempt in range(3):
            record = self.registry.ensure_runtime(chat_id)
            identity = record.identity
            if not is_action_surface(identity.action_surface):
                raise KernelUnavailable(
                    f"chat profile {identity.action_surface!r} does not admit persistent Python"
                )
            lease = await self._lease(
                chat_id,
                identity,
                workspace_root,
                workspace_fingerprint,
                workspace_roots=effective_roots,
                workspace_revision=workspace_revision,
            )
            namespace_document, refs = self.namespace_document(chat_id, identity)
            current_identity = self.registry.ensure_runtime(chat_id).identity
            if current_identity == identity:
                identity = current_identity
                break
        else:
            raise KernelUnavailable(
                "chat runtime identity did not stabilize during kernel admission"
            )
        assert lease is not None and identity is not None
        # Mount/category revisions do not require a new process, but the live
        # lease must still advertise the exact identity used by this cell.
        lease.identity = identity
        # Keep historical ref metadata so a proxy captured by an older cell can
        # fail with a precise stale-mount error. Category mounts control
        # disclosure, not an independent capability admission list.
        lease._refs.update(refs)
        admitted_scope = requested_scope.with_updates(
            chat_id=str(chat_id),
            kernel_generation=int(lease.generation),
            catalog_release_id=str(identity.catalog_release_id),
        )
        admission = ExecutionAdmission(
            execution_id="cell_" + uuid.uuid4().hex,
            chat_id=str(chat_id),
            run_id=str(run_id or "kernel-run"),
            outer_tool_call_id=str(outer_tool_call_id or "ipython-call"),
            generation=lease.generation,
            catalog_release_id=str(identity.catalog_release_id),
            mount_revision=int(identity.mount_revision or 0),
            selected_category_id=str(identity.selected_category_id or ""),
            overlay_revision=int(identity.overlay_revision or 0),
            environment_digest=str(identity.environment_digest),
            workspace_root_ids=effective_roots,
            retained_capability_ref_ids=tuple(
                str(ref_id)
                for ref_id in (
                    namespace_document.get("retained_capability_ref_ids") or ()
                )
                if str(ref_id)
            ),
            work_scope=admitted_scope,
            namespace_document=namespace_document,
            cancellation=cancellation,
        )
        source_artifact = None
        ledger_pre_error = ""
        try:
            source_artifact = self.artifact_store.put_text(
                str(code),
                kind="kernel_cell_source",
                scope=str(chat_id),
            )
        except Exception as exc:
            # Evidence storage is deliberately outside the execution effect.
            # A full artifact disk must not convert an otherwise executable
            # cell into a preflight failure.
            ledger_pre_error = f"{type(exc).__name__}: {exc}"[:500]
        started_at = time.time()
        evidence = dict(execution_id=admission.execution_id, chat_id=str(chat_id),
            run_id=str(run_id or 'kernel-run'), outer_tool_call_id=str(outer_tool_call_id or 'ipython-call'),
            kernel_generation=lease.generation, workspace_revision=workspace_revision,
            workspace_fingerprint=workspace_fingerprint, workspace_root_ids=effective_roots,
            work_scope=admitted_scope.to_dict(), source_ref=str(source_artifact.ref) if source_artifact else '',
            source_sha256=str(source_artifact.sha256) if source_artifact else '', started_at=started_at)
        try:
            self.cell_ledger.admit(self.instance_id, evidence)
        except Exception as exc:
            ledger_pre_error = f'{type(exc).__name__}: {exc}'[:500]
            self.emit('kernel:cell_admission_evidence_failed', status='unknown_effect',
                      execution_id=admission.execution_id, error=ledger_pre_error)
        interrupted = None
        try:
            result = await lease.execute(str(code), admission, on_chunk=on_chunk, timeout_s=timeout_s)
        except BaseException as exc:
            interrupted = exc
            result = getattr(exc, 'kernel_result', None)
            if not isinstance(result, KernelExecutionResult):
                result = KernelExecutionResult(execution_id=admission.execution_id, chat_id=str(chat_id),
                    generation=lease.generation, status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'unknown_effect',
                    output=CellOutput(), error_code='cancelled' if isinstance(exc, asyncio.CancelledError) else 'kernel_execution_interrupted',
                    error_message='Execution was interrupted; inspect existing effects before retrying.',
                    duration_ms=(time.time()-started_at)*1000, hard_restarted=lease._closed)
        completed_at = time.time()
        try:
            output_evidence = self.artifact_store.put_json(
                result.output.evidence(),
                kind="kernel_output_evidence",
                scope=str(chat_id),
            )
            result.output_evidence_ref = str(output_evidence.ref)
            result.output_evidence_sha256 = str(output_evidence.sha256)
            result.output_evidence_bytes = int(output_evidence.bytes)
            if source_artifact is None:
                raise RuntimeError(
                    "kernel cell source artifact is unavailable: "
                    + ledger_pre_error
                )
            result_artifact = self.artifact_store.put_json(
                result.to_dict(),
                kind="kernel_cell_result",
                scope=str(chat_id),
            )
            record = self.cell_ledger.append(
                execution_id=result.execution_id,
                chat_id=str(chat_id),
                run_id=str(run_id or "kernel-run"),
                outer_tool_call_id=str(outer_tool_call_id or "ipython-call"),
                kernel_generation=int(result.generation),
                workspace_revision=workspace_revision,
                workspace_fingerprint=workspace_fingerprint,
                workspace_root_ids=effective_roots,
                work_scope=admitted_scope.to_dict(),
                source_ref=str(source_artifact.ref),
                source_sha256=str(source_artifact.sha256),
                result_ref=str(result_artifact.ref),
                result_sha256=str(result_artifact.sha256),
                status=result.status,
                execution_count=int(result.execution_count),
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=float(result.duration_ms),
                error_code=result.error_code,
            )
            result.ledger_sequence = int(record.sequence)
            result.source_ref = str(source_artifact.ref)
            result.result_ref = str(result_artifact.ref)
        except Exception as exc:
            # The cell has already taken effect. Preserve that truth and make
            # the missing evidence explicit instead of converting it into a
            # false execution failure that callers might replay.
            result.source_ref = (
                str(source_artifact.ref) if source_artifact is not None else ""
            )
            result.ledger_error = (
                ledger_pre_error or f"{type(exc).__name__}: {exc}"[:500]
            )
            self.emit(
                "kernel:cell_ledger_failed",
                status="unknown_effect",
                chat_id=str(chat_id),
                execution_id=result.execution_id,
                error=result.ledger_error,
            )
        if interrupted is not None:
            raise interrupted
        return result

    def continuity_status(self, runtime_chat_id: str) -> dict[str, Any]:
        return self.continuity.continuity_status(runtime_chat_id)

    def configure_continuity(
        self,
        runtime_chat_id: str,
        *,
        checkpoint_enabled: bool | None = None,
        restore_on_boot: bool | None = None,
        inherit_defaults: bool = False,
        configured_by: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.continuity.configure_continuity(
            runtime_chat_id,
            checkpoint_enabled=checkpoint_enabled,
            restore_on_boot=restore_on_boot,
            inherit_defaults=inherit_defaults,
            configured_by=configured_by,
        )

    async def create_capsule(
        self,
        *,
        runtime_chat_id: str,
        workspace_roots: tuple[str, ...] = (),
        workspace_digest: str = "",
        _lifecycle_checkpoint: bool = False,
    ) -> KernelCapsuleManifest:
        return await self.continuity.create_capsule(
            runtime_chat_id=runtime_chat_id,
            workspace_roots=workspace_roots,
            workspace_digest=workspace_digest,
            _lifecycle_checkpoint=_lifecycle_checkpoint,
        )

    def _load_capsule_manifest(
        self,
        *,
        runtime_chat_id: str,
        capsule_ref: str,
        verify_values: bool,
    ) -> KernelCapsuleManifest:
        return self.continuity._load_capsule_manifest(
            runtime_chat_id=runtime_chat_id,
            capsule_ref=capsule_ref,
            verify_values=verify_values,
        )

    def _record_capsule_pointer(
        self,
        manifest: KernelCapsuleManifest,
        *,
        reason: str,
    ) -> str:
        return self.continuity._record_capsule_pointer(
            manifest,
            reason=reason,
        )

    def _persist_checkpoint_outcome(
        self,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        return self.continuity._persist_checkpoint_outcome(outcome)

    def _capsule_compatibility(
        self,
        manifest: KernelCapsuleManifest,
        *,
        identity: Any,
        workspace_digest: str,
    ) -> KernelCapsuleCompatibility:
        return self.continuity._capsule_compatibility(
            manifest,
            identity=identity,
            workspace_digest=workspace_digest,
        )

    def inspect_capsule(
        self,
        *,
        runtime_chat_id: str,
        capsule_ref: str,
        workspace_digest: str = "",
    ) -> KernelCapsuleInspection:
        return self.continuity.inspect_capsule(
            runtime_chat_id=runtime_chat_id,
            capsule_ref=capsule_ref,
            workspace_digest=workspace_digest,
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
        return await self.continuity.restore_capsule(
            runtime_chat_id=runtime_chat_id,
            capsule_ref=capsule_ref,
            workspace_roots=workspace_roots,
            workspace_digest=workspace_digest,
            workspace_revision=workspace_revision,
            require_compatible=require_compatible,
            _suppress_auto_restore=_suppress_auto_restore,
            _target_lease=_target_lease,
        )

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
        return await self.continuity.fork_capsule(
            source_runtime_chat_id=source_runtime_chat_id,
            target_runtime_chat_id=target_runtime_chat_id,
            capsule_ref=capsule_ref,
            source_workspace_roots=source_workspace_roots,
            source_workspace_digest=source_workspace_digest,
            target_workspace_roots=target_workspace_roots,
            target_workspace_digest=target_workspace_digest,
        )

    async def checkpoint(
        self,
        runtime_chat_id: str,
        *,
        reason: str = "model_requested",
    ) -> dict[str, Any]:
        return await self.continuity.checkpoint(
            runtime_chat_id,
            reason=reason,
        )

    async def bounded_namespace_view(
        self,
        runtime_chat_id: str,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await self.continuity.bounded_namespace_view(
            runtime_chat_id,
            limit=limit,
        )

    async def interrupt(
        self,
        runtime_chat_id: str,
        *,
        intent: str = "stop",
    ) -> dict[str, Any]:
        """Request a targeted active-cell interrupt.

        Explicit cell interruption with steer intent waits for an
        uncooperative cell's safe boundary.  Generic/direct interruption
        defaults to terminal Stop and retains the lease's hard-escalation path.
        Ordinary chat steering does not invoke this control.
        """

        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise ValueError("runtime_chat_id is required")
        lease = self._leases.get(chat_id)
        if lease is None or lease._closed:
            return {
                "schema": "variant1.kernel-control.v1",
                "runtime_chat_id": chat_id,
                "operation": "interrupt",
                "status": "absent",
                "kernel_generation": 0,
            }
        if lease.state != "busy":
            return {
                "schema": "variant1.kernel-control.v1",
                "runtime_chat_id": chat_id,
                "operation": "interrupt",
                "status": "idle",
                "kernel_generation": int(lease.generation),
            }
        if not lease._admissions:
            return {
                "schema": "variant1.kernel-control.v1",
                "runtime_chat_id": chat_id,
                "operation": "interrupt",
                "status": "host_operation_not_interruptible",
                "kernel_generation": int(lease.generation),
            }
        selected_intent = "steer" if str(intent) == "steer" else "stop"
        lease._request_interrupt(intent=selected_intent, send=False)
        self.emit(
            "kernel:interrupt_requested",
            status="ok",
            chat_id=chat_id,
            kernel_generation=int(lease.generation),
            intent=selected_intent,
        )
        return {
            "schema": "variant1.kernel-control.v1",
            "runtime_chat_id": chat_id,
            "operation": "interrupt",
            "status": "requested",
            "intent": selected_intent,
            "hard_kill_on_grace": selected_intent == "stop",
            "kernel_generation": int(lease.generation),
            "execution_ids": sorted(str(item) for item in lease._admissions),
        }

    async def restart(
        self,
        runtime_chat_id: str,
        *,
        reason: str = "operator_restart",
    ) -> dict[str, Any]:
        """Fence and close one live generation; the next cell boots a new one."""

        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise ValueError("runtime_chat_id is required")
        lease = self._leases.get(chat_id)
        if lease is None or lease._closed:
            return {
                "schema": "variant1.kernel-control.v1",
                "runtime_chat_id": chat_id,
                "operation": "restart",
                "status": "already_absent",
                "previous_generation": 0,
                "next_generation": "lazy",
            }
        previous = int(lease.generation)
        if lease.state == "busy" and lease._admissions:
            with suppress(Exception):
                lease._send_interrupt(intent="stop")
        selected_reason = str(reason or "operator_restart")
        checkpoint = await self._close_lease_serialized(
            lease,
            reason=selected_reason,
            hard=True,
        )
        return {
            "schema": "variant1.kernel-control.v1",
            "runtime_chat_id": chat_id,
            "operation": "restart",
            "status": "closed",
            "previous_generation": previous,
            "next_generation": "lazy",
            "checkpoint": checkpoint,
        }

    def execution_history(
        self,
        runtime_chat_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return a bounded monotonic page from the durable cell ledger."""

        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise ValueError("runtime_chat_id is required")
        rows = self.cell_ledger.list(
            chat_id,
            after_sequence=max(0, int(after_sequence or 0)),
            limit=max(1, min(int(limit or 100), 500)),
        )
        items = [row.to_dict() for row in rows]
        return {
            "schema": CELL_LEDGER_SCHEMA,
            "runtime_chat_id": chat_id,
            "items": items,
            "after_sequence": max(0, int(after_sequence or 0)),
            "next_sequence": (
                int(rows[-1].sequence)
                if rows else max(0, int(after_sequence or 0))
            ),
            "limit": max(1, min(int(limit or 100), 500)),
        }

    def _output_descriptor_value(
        self,
        descriptor: Any,
        *,
        chat_id: str,
        binary_as_base64: bool = False,
    ) -> tuple[bool, Any]:
        """Resolve one bounded output body without guessing its representation."""

        if not isinstance(descriptor, dict):
            return False, None
        storage = str(descriptor.get("storage") or "")
        encoding = str(descriptor.get("encoding") or "")
        if storage == "inline":
            return True, descriptor.get("data")
        if storage != "artifact":
            return False, None
        artifact = descriptor.get("artifact")
        if not isinstance(artifact, dict):
            return False, None
        ref = str(artifact.get("ref") or "")
        if not ref:
            return False, None
        raw = self.artifact_store.read_bytes_scoped(ref, chat_id)
        if encoding == "binary":
            return (
                True,
                base64.b64encode(raw).decode("ascii")
                if binary_as_base64
                else raw,
            )
        if encoding == "json":
            return True, json.loads(raw.decode("utf-8", errors="strict"))
        return True, raw.decode("utf-8", errors="strict")

    @staticmethod
    def _notebook_output_metadata(
        value: Any,
        *,
        display_id: str = "",
    ) -> dict[str, Any]:
        metadata = dict(value) if isinstance(value, dict) else {}
        if display_id:
            existing = metadata.get("variant1")
            variant1 = dict(existing) if isinstance(existing, dict) else {}
            variant1["display_id"] = display_id
            metadata["variant1"] = variant1
        return metadata

    def _notebook_outputs_from_evidence(
        self,
        evidence: Any,
        *,
        chat_id: str,
    ) -> list[dict[str, Any]]:
        """Reduce raw output events to valid final nbformat output objects."""

        if not isinstance(evidence, dict):
            return []
        events = evidence.get("events")
        if not isinstance(events, list):
            return []
        outputs: list[dict[str, Any]] = []
        clear_on_next = False
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            if event_type == "clear_output":
                if bool(event.get("wait")):
                    clear_on_next = True
                else:
                    outputs.clear()
                    clear_on_next = False
                continue
            if event_type not in {
                "display_data",
                "error",
                "execute_result",
                "stream",
                "update_display_data",
            }:
                continue
            if clear_on_next:
                outputs.clear()
                clear_on_next = False

            if event_type == "stream":
                present, body = self._output_descriptor_value(
                    event.get("body"), chat_id=chat_id
                )
                if present:
                    outputs.append({
                        "name": str(event.get("name") or "stdout"),
                        "output_type": "stream",
                        "text": str(body).splitlines(keepends=True),
                    })
                continue

            if event_type == "error":
                name_present, name = self._output_descriptor_value(
                    event.get("ename"), chat_id=chat_id
                )
                value_present, value = self._output_descriptor_value(
                    event.get("evalue"), chat_id=chat_id
                )
                trace_present, traceback_value = self._output_descriptor_value(
                    event.get("traceback"), chat_id=chat_id
                )
                traceback_rows = (
                    [str(item) for item in traceback_value]
                    if trace_present and isinstance(traceback_value, list)
                    else []
                )
                outputs.append({
                    "ename": str(name) if name_present else "ExecutionError",
                    "evalue": str(value) if value_present else "",
                    "output_type": "error",
                    "traceback": traceback_rows,
                })
                continue

            raw_bundle = event.get("data")
            bundle: dict[str, Any] = {}
            if isinstance(raw_bundle, dict):
                for raw_media_type, descriptor in raw_bundle.items():
                    media_type = str(raw_media_type or "")
                    present, value = self._output_descriptor_value(
                        descriptor,
                        chat_id=chat_id,
                        binary_as_base64=True,
                    )
                    if media_type and present:
                        bundle[media_type] = value
            metadata_present, metadata_value = self._output_descriptor_value(
                event.get("metadata"), chat_id=chat_id
            )
            display_id = str(event.get("display_id") or "")
            metadata = self._notebook_output_metadata(
                metadata_value if metadata_present else {},
                display_id=display_id,
            )
            if event_type == "update_display_data":
                if not display_id:
                    continue
                for previous in outputs:
                    if str(previous.get("_variant1_display_id") or "") != display_id:
                        continue
                    if previous.get("output_type") not in {
                        "display_data",
                        "execute_result",
                    }:
                        continue
                    previous["data"] = bundle
                    previous["metadata"] = metadata
                continue

            output: dict[str, Any] = {
                "data": bundle,
                "metadata": metadata,
                "output_type": event_type,
            }
            if event_type == "execute_result":
                count = int(event.get("execution_count") or 0)
                output["execution_count"] = count if count else None
            if display_id:
                output["_variant1_display_id"] = display_id
            outputs.append(output)

        for output in outputs:
            output.pop("_variant1_display_id", None)
        return outputs

    def export_notebook(
        self,
        runtime_chat_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Materialize a bounded notebook projection from immutable ledger artifacts."""

        chat_id = str(runtime_chat_id or "").strip()
        if not chat_id:
            raise ValueError("runtime_chat_id is required")
        rows = self.cell_ledger.list(
            chat_id,
            after_sequence=max(0, int(after_sequence or 0)),
            limit=max(1, min(int(limit or 200), 500)),
        )
        cells: list[dict[str, Any]] = []
        for row in rows:
            source = self.artifact_store.read_bytes_scoped(
                row.source_ref, chat_id
            ).decode("utf-8", errors="strict")
            raw_result = self.artifact_store.read_bytes_scoped(
                row.result_ref, chat_id
            )
            decoded = json.loads(raw_result.decode("utf-8", errors="strict"))
            outputs: list[dict[str, Any]] = []
            output_evidence_ref = ""
            output_evidence_sha256 = ""
            output_event_bounds: dict[str, Any] = {}
            if isinstance(decoded, dict):
                output_evidence = decoded.get("output_evidence")
                if isinstance(output_evidence, dict):
                    output_evidence_ref = str(output_evidence.get("ref") or "")
                    output_evidence_sha256 = str(
                        output_evidence.get("sha256") or ""
                    )
            if output_evidence_ref:
                raw_evidence = self.artifact_store.read_bytes_scoped(
                    output_evidence_ref, chat_id
                )
                evidence = json.loads(raw_evidence.decode("utf-8", errors="strict"))
                outputs = self._notebook_outputs_from_evidence(
                    evidence,
                    chat_id=chat_id,
                )
                if isinstance(evidence, dict) and isinstance(evidence.get("bounds"), dict):
                    output_event_bounds = dict(evidence["bounds"])
            cells.append({
                "cell_type": "code",
                "execution_count": (
                    int(row.execution_count) if row.execution_count else None
                ),
                "metadata": {
                    "variant1": {
                        "execution_id": row.execution_id,
                        "ledger_sequence": int(row.sequence),
                        "kernel_generation": int(row.kernel_generation),
                        "status": row.status,
                        "workspace_revision": int(row.workspace_revision),
                        "workspace_fingerprint": row.workspace_fingerprint,
                        "source_ref": row.source_ref,
                        "result_ref": row.result_ref,
                        "output_event_schema": (
                            OUTPUT_EVENT_SCHEMA if output_evidence_ref else None
                        ),
                        "output_evidence_ref": output_evidence_ref or None,
                        "output_evidence_sha256": output_evidence_sha256 or None,
                        "output_event_bounds": output_event_bounds,
                    }
                },
                "outputs": outputs,
                "source": source.splitlines(keepends=True),
            })
        notebook = {
            "cells": cells,
            "metadata": {
                "kernelspec": {
                    "display_name": "VARIANT-1 Python",
                    "language": "python",
                    "name": "python3",
                },
                "language_info": {"name": "python"},
                "variant1": {
                    "schema": "variant1.kernel-notebook-export.v1",
                    "runtime_chat_id": chat_id,
                    "after_sequence": max(0, int(after_sequence or 0)),
                    "last_sequence": int(rows[-1].sequence) if rows else 0,
                },
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        artifact = self.artifact_store.put_json(
            notebook,
            kind="kernel_notebook",
            scope=chat_id,
        )
        return {
            "schema": "variant1.kernel-notebook-export.v1",
            "runtime_chat_id": chat_id,
            "artifact_ref": str(artifact.ref),
            "sha256": str(artifact.sha256),
            "bytes": int(artifact.bytes),
            "cells": len(cells),
            "last_sequence": int(rows[-1].sequence) if rows else 0,
        }

    async def reap_idle(self) -> int:
        now = time.monotonic()
        victims = [
            lease for lease in list(self._leases.values())
            if lease.state == "ready"
            and not self._automatic_eviction_blocked(lease)
            and (
                (self.limits.idle_lifetime_s > 0
                 and now - lease.last_used_at >= self.limits.idle_lifetime_s)
                or (self.limits.absolute_lifetime_s > 0
                    and now - lease.created_at >= self.limits.absolute_lifetime_s)
            )
        ]
        closed = 0
        for lease in victims:
            await self._close_lease_serialized(
                lease,
                reason="idle_or_absolute_eviction",
            )
            closed += int(lease._closed)
        return closed

    async def close_chat(self, chat_id: str, *, reason: str = "chat_closed") -> bool:
        lease = self._leases.get(str(chat_id))
        if lease is None:
            return False
        await self._close_lease_serialized(
            lease,
            reason=str(reason or "chat_closed"),
            hard=True,
        )
        return True

    def _lease_resource_status(self, lease: KernelLease) -> dict[str, Any]:
        snapshot = json.loads(json.dumps(lease._resource_snapshot or {}))
        process = (
            snapshot.get("process")
            if isinstance(snapshot.get("process"), dict)
            else {}
        )
        if lease.process is not None and lease.process.poll() is None:
            try:
                import psutil

                live = psutil.Process(int(lease.process.pid))
                memory = live.memory_info()
                cpu = live.cpu_times()
                process.update({
                    "rss_bytes": int(memory.rss),
                    "virtual_bytes": int(memory.vms),
                    "cpu_user_s": float(cpu.user),
                    "cpu_system_s": float(cpu.system),
                    "threads": int(live.num_threads()),
                })
            except Exception:
                pass
        rss = max(0, int(process.get("rss_bytes") or 0))
        memory_limit = max(1, int(self.limits.process_memory_bytes))
        namespace = (
            snapshot.get("namespace")
            if isinstance(snapshot.get("namespace"), dict)
            else {
                "values": 0,
                "estimated_bytes": 0,
                "contributors": [],
                "contributors_truncated": False,
            }
        )
        namespace_bytes = max(0, int(namespace.get("estimated_bytes") or 0))
        capsule_limit = max(1, int(self.capsule_limits.max_total_value_bytes))
        return {
            "schema": "variant1.kernel-resource-status.v1",
            "process": process,
            "namespace": namespace,
            "output": dict(lease._last_output_pressure),
            "pressure": {
                "process_memory_ratio": round(min(1.0, rss / memory_limit), 6),
                "process_memory_limit_bytes": memory_limit,
                "capsule_estimate_ratio": round(
                    min(1.0, namespace_bytes / capsule_limit), 6
                ),
                "capsule_limit_bytes": capsule_limit,
            },
            "snapshot_received_at": snapshot.get("received_at"),
        }

    def status(self, chat_id: str) -> dict[str, Any]:
        lease = self._leases.get(str(chat_id))
        if lease is None or lease._closed:
            return {
                "state": "absent",
                "generation": 0,
                "pid": None,
                "runtime_profile": self.runtime_profile.to_dict(),
                "latest_checkpoint": self.continuity.durable_checkpoint_outcome(str(chat_id)),
                "latest_capsule": self.continuity.compact_latest_capsule(str(chat_id)),
                "latest_auto_restore": self.continuity.durable_auto_restore_outcome(
                    str(chat_id)
                ),
            }
        return {
            "state": str(lease.state),
            "generation": int(lease.generation),
            "pid": int(lease.process.pid) if lease.process is not None else None,
            "workspace_root": lease.workspace_root,
            "workspace_roots": list(lease.workspace_roots),
            "workspace_revision": int(lease.workspace_revision),
            "workspace_fingerprint": lease.workspace_fingerprint,
            "runtime_profile": lease.runtime_profile.to_dict(),
            "namespace_sync": {
                "performed": int(lease._namespace_sync_count),
                "skipped": int(lease._namespace_sync_skipped),
            },
            "resources": self._lease_resource_status(lease),
            "background_output": {
                "events": len(lease._background_events),
                "latest_at": (
                    lease._background_events[-1].get("received_at")
                    if lease._background_events else None
                ),
            },
            "created_at_monotonic": float(lease.created_at),
            "last_used_at_monotonic": float(lease.last_used_at),
            "latest_checkpoint": self.continuity.durable_checkpoint_outcome(str(chat_id)),
            "latest_capsule": self.continuity.compact_latest_capsule(str(chat_id)),
            "latest_auto_restore": self.continuity.durable_auto_restore_outcome(str(chat_id)),
        }

    def continuation_context(
        self, chat_id: str, *, current_user_text: str = "", mutation_enabled: bool = False,
    ) -> str:
        """Bounded factual next-turn context; never boots or executes a worker."""
        latest = self.cell_ledger.latest(str(chat_id))
        if latest is None:
            return ''
        outcomes = self.cell_ledger.outcome_snapshot(str(chat_id))
        lease = self._leases.get(str(chat_id))
        live = bool(lease is not None and not lease._closed and lease.process is not None and lease.process.poll() is None)
        state = (f'Live CPython generation {lease.generation}; state {lease.state}.' if live
                 else 'No live CPython worker is currently retained; inspect runtime state before claiming continuity.')
        names = []
        requested = list(dict.fromkeys(
            token for token in re.findall(r'\w+', str(current_user_text)) if token.isidentifier()
        ))
        explicit_continuation = bool(re.search(
            r'^\s*(?:continue|resume)\b|\bfollow[ -]up\b', current_user_text, re.IGNORECASE,
        ))
        if live:
            delta = lease._last_namespace_delta
            recent = list(dict.fromkeys(
                n for field in ('updated', 'retained') for n in delta.get(field, ())
                if isinstance(n, str) and n.isidentifier() and not n.startswith('_')
            ))
            inventory = getattr(lease, '_namespace_inventory', None)
            known = set(inventory if inventory is not None else recent)
            preferred = [name for name in requested if name in known]
            fallback = recent if explicit_continuation or not current_user_text else []
            names = list(dict.fromkeys(preferred + fallback))[:24]
        recovery = (
            'Mutation authoring is ON; toolbelt.promote_helper can validate/publish a reusable helper when useful.'
            if mutation_enabled else
            'Mutation authoring is OFF or frozen. Native tools and ordinary Python remain available; '
             'already activated tools remain usable, with further authoring/revision frozen.'
        )
        outcome_context = ''
        if outcomes.non_ok_count:
            outcome_context = (
                'Durable-chat-scoped cell ledger: '
                f'non-OK cells={outcomes.non_ok_count}; '
                f'latest non-OK={outcomes.latest_non_ok_execution_id} '
                f'(sequence={outcomes.latest_non_ok_sequence}, '
                f'generation={outcomes.latest_non_ok_kernel_generation}, '
                f'status={outcomes.latest_non_ok_status}); '
                f'later cells={outcomes.later_cell_count}. '
                'These execution facts do not establish state loss, unresolved work, or recovery.\n'
            )
        return ('[Host kernel continuation]\n'+state+
                f' Last recorded cell: {latest.execution_id}, generation {latest.kernel_generation}, status {latest.status}.\n'+
                outcome_context+
                ('Recorded namespace names after the last cell (bounded, values omitted): '+', '.join(names)+'.\n' if names else '')+
                'Existing state may belong to earlier tasks; use it only when relevant to this request. '
                + ('This request refers to earlier work; inspect relevant retained objects before claiming continuity. '
                   if explicit_continuation else '') +
                'Prior assistant prose is not execution evidence. Do not recreate missing state and describe it as continuity. '
                'Use toolbelt.describe(...), toolbelt.search(...) and help(proxy) for contracts. Ordinary Python helpers are available. '
                + recovery)

    async def kill_all(self, *, reason: str = "operator_kill_all") -> int:
        leases = [lease for lease in list(self._leases.values()) if not lease._closed]
        for lease in leases:
            await self._close_lease_serialized(
                lease,
                reason=reason,
                hard=True,
            )
        return len(leases)

    async def shutdown(self) -> dict[str, Any]:
        async with self._shutdown_lock:
            return await self._shutdown_owned()

    async def _shutdown_owned(self) -> dict[str, Any]:
        async with self._guard:
            if self._closed:
                return {"ok": True, "failures": []}
            self._shutting_down = True
            current = asyncio.current_task()
            boot_tasks = [
                task for task in self._boot_tasks
                if task is not current and not task.done()
            ]
        for task in boot_tasks:
            task.cancel()
        if boot_tasks:
            await asyncio.gather(*boot_tasks, return_exceptions=True)
        leases = list(self._leases.values())
        results = await asyncio.gather(*(
            self._close_lease_serialized(
                lease,
                reason="backend_shutdown",
                hard=True,
            )
            for lease in leases
        ), return_exceptions=True)
        failures = []
        for lease, result in zip(leases, results):
            if isinstance(result, BaseException):
                failures.append({"chat_id": lease.chat_id, "error": str(result), "error_type": type(result).__name__})
                self._leases[str(lease.chat_id)] = lease
                self.emit("kernel:shutdown_failed", status="error", chat_id=lease.chat_id,
                          error=str(result), error_type=type(result).__name__)
            elif self._leases.get(str(lease.chat_id)) is lease:
                self._leases.pop(str(lease.chat_id), None)
        async with self._guard:
            self._closed = not failures
            self._shutting_down = bool(failures)
            self._boot_tasks.clear()
        return {"ok": not failures, "failures": failures}
