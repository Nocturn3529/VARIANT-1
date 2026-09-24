"""One fenced CPython process, JSONL transport, and bridge per durable chat."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from contextvars import copy_context
from dataclasses import replace
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Callable
import uuid

from capability_broker import CapabilityCall, CapabilityRef, InvocationContext
from core_invariants import cancellation_is_requested
from tool_core import json_safe
from run_context import Variant1RunContext, bind_run_context, current_run_context
from process_tree import CREATE_SUSPENDED, resume_owned_process

from .bridge import KernelBridgeServer
from .capsules import KernelCapsuleError
from .contracts import (
    ExecutionAdmission,
    KernelAutoRestoreError,
    KernelExecutionResult,
    KernelUnavailable,
)
from .job_object import KernelJobObject
from .output import CellOutputCollector
from .repl_protocol import (
    DEFAULT_MAX_REPL_FRAME_BYTES,
    ReplProtocolError,
    encode_line,
    read_line,
    request_frame,
    validate_event,
)

if TYPE_CHECKING:
    from .manager import KernelRuntimeManager


class _ReplTransport:
    """One threaded pipe reader feeding the owning asyncio lease."""

    def __init__(
        self,
        process: subprocess.Popen,
        *,
        max_frame_bytes: int = DEFAULT_MAX_REPL_FRAME_BYTES,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise KernelUnavailable("REPL worker pipes are unavailable")
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.max_frame_bytes = max(1024, int(max_frame_bytes))
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"variant1-repl-host-reader:{process.pid}",
            daemon=True,
        )
        self._reader.start()

    def _deliver(self, event: dict[str, Any]) -> None:
        if not self._closed:
            self.queue.put_nowait(event)

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                frame = read_line(
                    self.stdout, max_bytes=self.max_frame_bytes
                )
                if frame is None:
                    self.loop.call_soon_threadsafe(
                        self._deliver,
                        {"_transport_error": "REPL worker closed its output"},
                    )
                    return
                try:
                    event = validate_event(frame)
                except Exception as exc:
                    self.loop.call_soon_threadsafe(
                        self._deliver,
                        {"_transport_error": str(exc) or type(exc).__name__},
                    )
                    return
                self.loop.call_soon_threadsafe(self._deliver, event)
        except BaseException as exc:
            if not self._closed:
                with suppress(RuntimeError):
                    self.loop.call_soon_threadsafe(
                        self._deliver,
                        {"_transport_error": str(exc) or type(exc).__name__},
                    )

    def send(self, request_id: str, frame_type: str, **fields: Any) -> None:
        if self._closed:
            raise KernelUnavailable("REPL transport is closed")
        raw = encode_line(
            request_frame(request_id, frame_type, **fields),
            max_bytes=self.max_frame_bytes,
        )
        with self._write_lock:
            self.stdin.write(raw)
            self.stdin.flush()

    async def receive(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        if timeout_s is None:
            event = await self.queue.get()
        else:
            event = await asyncio.wait_for(
                self.queue.get(), timeout=max(0.001, float(timeout_s))
            )
        error = str(event.get("_transport_error") or "")
        if error:
            raise ReplProtocolError(error)
        return event

    def close(self) -> None:
        self._closed = True
        with suppress(Exception):
            self.stdin.close()
        with suppress(Exception):
            self.stdout.close()

class _CapabilityConcurrencyGate:
    """Bound reads, serialize effects, and reserve one lane for owned controls."""

    def __init__(self, max_parallel: int = 8) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._condition = asyncio.Condition()
        self._parallel_active = 0
        self._exclusive_active = False
        self._exclusive_waiting = 0
        self._control_lock = asyncio.Lock()

    async def acquire(self, parallel_safe: bool, *, control: bool = False) -> None:
        if control:
            await self._control_lock.acquire()
            return
        async with self._condition:
            if parallel_safe:
                await self._condition.wait_for(
                    lambda: not self._exclusive_active
                    and self._exclusive_waiting == 0
                    and self._parallel_active < self._max_parallel
                )
                self._parallel_active += 1
                return
            self._exclusive_waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._exclusive_active
                    and self._parallel_active == 0
                )
                self._exclusive_active = True
            finally:
                self._exclusive_waiting -= 1
                self._condition.notify_all()

    async def release(self, parallel_safe: bool, *, control: bool = False) -> None:
        if control:
            self._control_lock.release()
            return
        async with self._condition:
            if parallel_safe:
                self._parallel_active = max(0, self._parallel_active - 1)
            else:
                self._exclusive_active = False
            self._condition.notify_all()


def _safe_chat_dir(chat_id: str) -> str:
    digest = hashlib.sha256(str(chat_id).encode("utf-8", errors="replace")).hexdigest()
    return "chat-" + digest[:32]


def _restrict_path(path: str, *, directory: bool = False) -> None:
    """Restrict connection material to the current user.

    ``chmod`` is sufficient on POSIX.  Windows requires an ACL operation;
    failure is fatal because bridge secrets and generation descriptors must
    remain private to the current user.
    """
    if not sys.platform.startswith("win"):
        os.chmod(path, 0o700 if directory else 0o600)
        return
    import psutil

    try:
        user = str(psutil.Process().username()).strip()
    except psutil.Error as exc:
        raise KernelUnavailable("cannot resolve process user for kernel ACL") from exc
    if not user:
        raise KernelUnavailable("cannot resolve current user for kernel ACL")
    grant = f"{user}:(OI)(CI)F" if directory else f"{user}:F"
    windows_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
    icacls = os.path.join(windows_root, "System32", "icacls.exe")
    if not os.path.isfile(icacls):
        raise KernelUnavailable("Windows icacls.exe is unavailable")
    result = subprocess.run(
        [icacls, path, "/inheritance:r", "/grant:r", grant],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise KernelUnavailable(
            "failed to apply private ACL to kernel connection material: "
            + str(result.stderr or "icacls error").strip()[:300]
        )


def _write_private_json(path: str, value: Any) -> None:
    temporary = path + ".tmp-" + uuid.uuid4().hex
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    _restrict_path(temporary)
    os.replace(temporary, path)
    _restrict_path(path)


class KernelLease:
    """One fenced process/client/bridge generation for one durable chat."""

    def __init__(
        self,
        *,
        manager: "KernelRuntimeManager",
        chat_id: str,
        generation: int,
        identity: Any,
        generation_root: str,
        workspace_root: str,
        workspace_fingerprint: str,
        workspace_roots: tuple[str, ...],
        workspace_revision: int,
    ) -> None:
        self.manager = manager
        self.chat_id = str(chat_id)
        self.generation = int(generation)
        self.identity = identity
        self.root = os.path.abspath(generation_root)
        self.workspace_root = os.path.abspath(workspace_root)
        self.workspace_roots = tuple(
            os.path.abspath(str(path))
            for path in workspace_roots
            if str(path or "").strip()
        ) or (self.workspace_root,)
        self.workspace_revision = max(0, int(workspace_revision or 0))
        self.workspace_fingerprint = str(workspace_fingerprint or "")
        self.runtime_profile = manager.runtime_profile
        self.runtime_profile_digest = manager.runtime_profile.digest
        self.state = "absent"
        self.created_at = time.monotonic()
        self.last_used_at = self.created_at
        self.execution_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self.transport: _ReplTransport | None = None
        self.process: subprocess.Popen | None = None
        self.job: KernelJobObject | None = None
        self.bridge: KernelBridgeServer | None = None
        self.nonce = secrets.token_urlsafe(24)
        self.secret = secrets.token_bytes(32)
        self.descriptor_file = os.path.join(self.root, "capabilities.json")
        self.gate_file = os.path.join(self.root, "parent-owned.gate")
        self._gate_token = secrets.token_urlsafe(32)
        self._refs: dict[str, CapabilityRef] = {}
        self._admissions: dict[str, ExecutionAdmission] = {}
        self._cell_cancellations: dict[str, asyncio.Event] = {}
        # Host-originated cell interrupts retain their user intent.  A Steer
        # interrupts only the current cell and must never destroy this durable
        # generation merely because the cell cannot acknowledge quickly.  A
        # terminal Stop may still escalate after its bounded soft attempt.
        self._external_interrupts: dict[str, str] = {}
        self._namespace_key: tuple[str, int, int, int] | None = None
        self._namespace_sync_count = 0
        self._namespace_sync_skipped = 0
        self._last_namespace_delta: dict[str, Any] = {}
        self._namespace_inventory: tuple[str, ...] | None = None
        self._namespace_inventory_omitted = 0
        self._resource_snapshot: dict[str, Any] = {}
        self._last_output_pressure: dict[str, Any] = {}
        self._background_events: list[dict[str, Any]] = []
        self._worker_diagnostics: list[str] = []
        self._auto_restore_lock = asyncio.Lock()
        self._auto_restore_attempted = False
        self._auto_restore_error: KernelAutoRestoreError | None = None
        self._capability_gate = _CapabilityConcurrencyGate(max_parallel=1)
        self._closed = False

    def _descriptor_document(self) -> dict[str, Any]:
        document, refs = self.manager.namespace_document(
            self.chat_id, self.identity
        )
        self._refs = refs
        document = dict(document)
        document["generation"] = self.generation
        document["runtime_profile"] = self.runtime_profile.to_dict()
        self._namespace_key = self._document_key(document)
        return document

    @staticmethod
    def _document_key(
        document: dict[str, Any] | None,
    ) -> tuple[str, int, int, int] | None:
        if not isinstance(document, dict):
            return None
        if not str(document.get("schema") or "").startswith(
            "variant1.astb.namespace"
        ):
            return None
        return (
            str(document.get("catalog_release_id") or ""),
            int(document.get("mount_revision") or 0),
            int((document.get("session") or {}).get("overlay_revision") or 0),
            int(
                (document.get("session") or {}).get(
                    "mutation_authority_revision"
                ) or 0
            ),
        )

    async def _invoke_from_kernel(self, request: dict[str, Any]) -> dict[str, Any]:
        execution_id = str(request.get("execution_id") or "")
        admission = self._admissions.get(execution_id)
        if admission is None or admission.generation != self.generation:
            return {
                "ok": False,
                "error": {
                    "code": "stale_execution_admission",
                    "message": "This capability call is outside the currently admitted cell.",
                },
            }
        # The socket listener was created during an earlier run. Its Tasks must
        # not supply the run, transport, perception sinks or other ambient state
        # for this cell. Copy the admission snapshot for each concurrent request.
        callback_context = admission.callback_context
        if callback_context is None:
            from contextvars import Context
            callback_context = Context()
        return await asyncio.create_task(
            self._invoke_admitted(request, admission),
            context=callback_context.copy(),
        )

    async def _invoke_admitted(
        self, request: dict[str, Any], admission: ExecutionAdmission,
    ) -> dict[str, Any]:
        execution_id = admission.execution_id
        if admission.cancelled():
            return {
                "ok": False,
                "error": {"code": "cell_cancelling", "message": "The cell is cancelling."},
            }
        request_outer = str(request.get("outer_tool_call_id") or "")
        if request_outer != admission.outer_tool_call_id:
            return {
                "ok": False,
                "error": {
                    "code": "outer_call_mismatch",
                    "message": "Kernel capability outer-call attribution is stale.",
                },
            }
        if str(request.get("op") or "") == "invoke_many":
            return await self._invoke_many_from_kernel(request, admission)
        ref_id = str(request.get("ref_id") or "")
        ref = self._refs.get(ref_id)
        if ref is None:
            return {
                "ok": False,
                "error": {
                    "code": "capability_ref_unavailable",
                    "message": "The opaque capability reference is not in this kernel grant.",
                },
            }
        if ref.slot_id:
            requested_catalog = str(request.get("catalog_release_id") or "")
            requested_slot = str(request.get("slot_id") or "")
            requested_category = str(request.get("category_id") or "")
            requested_version = int(request.get("slot_version") or 0)
            requested_mount = int(request.get("mount_revision") or 0)
            slot_parts = ref.slot_id.rsplit("/", 2)
            ref_category = slot_parts[-2] if len(slot_parts) == 3 else ""
            base_categories = {
                str(item)
                for item in (
                    (admission.namespace_document or {}).get("base_category_ids")
                    or ()
                )
                if str(item)
            }
            category_admitted = (
                ref_category == admission.selected_category_id
                or ref_category in base_categories
                or ref.opaque_id
                in set(admission.retained_capability_ref_ids or ())
            )
            mount_admitted = (
                requested_mount == int(admission.mount_revision)
                or ref_category in base_categories
                or ref.opaque_id
                in set(admission.retained_capability_ref_ids or ())
            )
            if (
                requested_catalog != admission.catalog_release_id
                or requested_slot != ref.slot_id
                or requested_version != int(ref.slot_version)
                or not mount_admitted
                or requested_category != ref_category
                or not category_admitted
            ):
                return {
                    "ok": False,
                    "error": {
                        "code": "stale_category_mount",
                        "message": (
                            "This official slot proxy belongs to an old or "
                            "different category mount. Select or inspect the "
                            "current category and reacquire the proxy."
                        ),
                    },
                }
        args = request.get("args")
        if not isinstance(args, dict):
            return {
                "ok": False,
                "error": {"code": "invalid_arguments", "message": "Capability arguments must be an object."},
            }
        invocation_mode = str(request.get("invocation_mode") or "sync")
        if invocation_mode not in {"sync", "async"}:
            return {
                "ok": False,
                "error": {
                    "code": "invalid_invocation_mode",
                    "message": "Capability invocation mode must be sync or async.",
                },
            }
        self.manager.emit(
            "kernel:capability_invocation_policy",
            status="ok",
            chat_id=self.chat_id,
            run_id=admission.run_id,
            cell_execution_id=execution_id,
            request_id=str(request.get("request_id") or ""),
            capability_id=ref.capability_id,
            invocation_mode=invocation_mode,
            host_concurrency_limit=self.manager.fanout_limit(self.chat_id),
        )
        raw_deadline = request.get("deadline_ms")
        if raw_deadline is None:
            # Transport silence is not an operation deadline. The capability
            # may supply its own default; callers may request a positive limit.
            deadline_ms = 0
        else:
            try:
                deadline_ms = int(raw_deadline)
            except (TypeError, ValueError):
                deadline_ms = 0
            if deadline_ms <= 0:
                return {
                    "ok": False,
                    "error": {
                        "code": "invalid_deadline",
                        "message": "Capability deadline_ms must be a positive integer.",
                    },
                }
        call_cancellation = request.get("_bridge_cancel_event")

        def call_cancelled() -> bool:
            return bool(
                admission.cancelled()
                or cancellation_is_requested(call_cancellation)
            )

        nested_id = str(request.get("request_id") or "")
        context = InvocationContext(
            chat_id=self.chat_id,
            run_id=admission.run_id,
            outer_tool_call_id=admission.outer_tool_call_id,
            cell_execution_id=execution_id,
            nested_call_id=nested_id,
            principal_actor_id="model",
            workspace_root_ids=admission.workspace_root_ids,
            kernel_generation=str(self.generation),
            catalog_release_id=admission.catalog_release_id,
            mount_revision=int(admission.mount_revision),
            slot_id=ref.slot_id,
            slot_version=int(ref.slot_version),
            environment_digest=admission.environment_digest,
            deadline_ms=deadline_ms,
            surface="ipython",
            work_scope=admission.work_scope,
            cancellation=call_cancelled,
            user_wait=request.get('_bridge_user_wait') if raw_deadline is None else None,
        )
        parallel_safe = False
        control = False
        try:
            tool, _metadata = self.manager.broker._resolve(ref)
            metadata = self.manager.broker.metadata_for_tool(tool, args)
            parallel_safe = bool(
                metadata.effect_class in {"pure", "read"}
                and metadata.parallel_safe
            )
            control = await self.manager.broker.admits_control(ref, args, context)
        except Exception:
            # Resolution failures still pass through the broker for a receipt,
            # but they use the exclusive lane because their policy is unknown.
            parallel_safe = False
        await self._capability_gate.acquire(parallel_safe, control=control)
        try:
            receipt = await self.manager.broker.invoke(ref, args, context)
            refreshed_document = None
            if (
                receipt.ok
                and self.manager.catalog_service is not None
                and execution_id in self._admissions
            ):
                try:
                    document, refs = self.manager.namespace_document(
                        self.chat_id, self.identity
                    )
                    next_key = self._document_key(document)
                    if next_key is not None and next_key != self._namespace_key:
                        self._refs.update(refs)
                        latest = self.manager.registry.ensure_runtime(self.chat_id)
                        latest_identity = latest.identity
                        admission = replace(
                            admission,
                            catalog_release_id=str(latest_identity.catalog_release_id),
                            mount_revision=int(latest_identity.mount_revision or 0),
                            selected_category_id=str(
                                latest_identity.selected_category_id or ""
                            ),
                            overlay_revision=int(latest_identity.overlay_revision or 0),
                            retained_capability_ref_ids=tuple(
                                str(ref_id)
                                for ref_id in (
                                    document.get("retained_capability_ref_ids")
                                    or ()
                                )
                                if str(ref_id)
                            ),
                            namespace_document=document,
                        )
                        self._admissions[execution_id] = admission
                        self.identity = latest_identity
                        self._namespace_key = next_key
                        refreshed_document = document
                except Exception:
                    # The capability result remains authoritative. A failed live
                    # remount is recovered by the normal pre-cell namespace sync.
                    refreshed_document = None
            if receipt.ok:
                response = {
                    "ok": True,
                    "result": (receipt.result_value if isinstance(receipt.result_value, bytes)
                               else json_safe(receipt.result_value)),
                    "receipt": receipt.to_dict(),
                }
                if refreshed_document is not None:
                    response["namespace"] = refreshed_document
                return response
            error = receipt.error
            return {
                "ok": False,
                "receipt": receipt.to_dict(),
                "error": {
                    "code": str(error.code if error else receipt.status),
                    "message": str(error.message if error else receipt.status),
                    "retryable": bool(error.retryable if error else False),
                    "may_have_applied": bool(
                        error.may_have_applied if error else False
                    ),
                },
            }
        finally:
            await asyncio.shield(
                self._capability_gate.release(parallel_safe, control=control)
            )

    async def _invoke_many_from_kernel(
        self,
        request: dict[str, Any],
        admission: ExecutionAdmission,
    ) -> dict[str, Any]:
        """Validate and execute one ordered read batch through the broker."""

        raw_calls = request.get("calls")
        max_calls = int(getattr(self.manager.broker, "max_batch_calls", 100) or 100)
        if not isinstance(raw_calls, list) or not 1 <= len(raw_calls) <= max_calls:
            return {
                "ok": False,
                "error": {
                    "code": "invalid_batch_size",
                    "message": f"Capability batch needs 1-{max_calls} calls.",
                },
            }
        refs: list[CapabilityRef] = []
        calls: list[CapabilityCall] = []
        request_id = str(request.get("request_id") or "")
        for index, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                return {
                    "ok": False,
                    "error": {
                        "code": "invalid_batch_call",
                        "message": f"Capability batch item {index} must be an object.",
                    },
                }
            ref = self._refs.get(str(item.get("ref_id") or ""))
            if ref is None:
                return {
                    "ok": False,
                    "error": {
                        "code": "capability_ref_unavailable",
                        "message": f"Batch item {index} is not in this kernel grant.",
                    },
                }
            if ref.slot_id:
                slot_parts = ref.slot_id.rsplit("/", 2)
                ref_category = slot_parts[-2] if len(slot_parts) == 3 else ""
                base_categories = {
                    str(value)
                    for value in (
                        (admission.namespace_document or {}).get(
                            "base_category_ids"
                        ) or ()
                    )
                    if str(value)
                }
                category_admitted = (
                    ref_category == admission.selected_category_id
                    or ref_category in base_categories
                    or ref.opaque_id
                    in set(admission.retained_capability_ref_ids or ())
                )
                mount_admitted = (
                    int(item.get("mount_revision") or 0)
                    == int(admission.mount_revision)
                    or ref_category in base_categories
                    or ref.opaque_id
                    in set(admission.retained_capability_ref_ids or ())
                )
                if (
                    str(item.get("catalog_release_id") or "")
                    != admission.catalog_release_id
                    or str(item.get("slot_id") or "") != ref.slot_id
                    or int(item.get("slot_version") or 0) != int(ref.slot_version)
                    or not mount_admitted
                    or str(item.get("category_id") or "")
                    != ref_category
                    or not category_admitted
                ):
                    return {
                        "ok": False,
                        "error": {
                            "code": "stale_category_mount",
                            "message": (
                                f"Batch item {index} belongs to an old or different "
                                "category mount. Reacquire the current proxy."
                            ),
                        },
                    }
            arguments = item.get("args")
            if not isinstance(arguments, dict):
                return {
                    "ok": False,
                    "error": {
                        "code": "invalid_arguments",
                        "message": f"Batch item {index} arguments must be an object.",
                    },
                }
            refs.append(ref)
            calls.append(CapabilityCall(
                ref=ref,
                args=arguments,
                nested_call_id=f"{request_id}:{index}",
            ))

        host_limit = self.manager.fanout_limit(self.chat_id)
        raw_requested = request.get("max_concurrency")
        requested = host_limit if raw_requested is None else max(1, int(raw_requested))
        effective = max(1, min(requested, host_limit))
        self.manager.emit(
            "kernel:batch_policy",
            status="ok",
            chat_id=self.chat_id,
            run_id=admission.run_id,
            calls=len(calls),
            requested_concurrency=requested,
            host_concurrency_limit=host_limit,
            effective_concurrency=effective,
        )
        first_ref = refs[0]
        raw_deadline = request.get("deadline_ms")
        if raw_deadline is None:
            deadline_ms = 0
        else:
            try:
                deadline_ms = int(raw_deadline)
            except (TypeError, ValueError):
                deadline_ms = 0
            if deadline_ms <= 0:
                return {
                    "ok": False,
                    "error": {
                        "code": "invalid_deadline",
                        "message": "Capability deadline_ms must be a positive integer.",
                    },
                }
        call_cancellation = request.get("_bridge_cancel_event")

        def call_cancelled() -> bool:
            return bool(
                admission.cancelled()
                or cancellation_is_requested(call_cancellation)
            )

        context = InvocationContext(
            chat_id=self.chat_id,
            run_id=admission.run_id,
            outer_tool_call_id=admission.outer_tool_call_id,
            cell_execution_id=admission.execution_id,
            nested_call_id=request_id,
            principal_actor_id="model",
            workspace_root_ids=admission.workspace_root_ids,
            kernel_generation=str(self.generation),
            catalog_release_id=admission.catalog_release_id,
            mount_revision=int(admission.mount_revision),
            slot_id=first_ref.slot_id,
            slot_version=int(first_ref.slot_version),
            environment_digest=admission.environment_digest,
            deadline_ms=deadline_ms,
            surface="ipython",
            work_scope=admission.work_scope,
            cancellation=call_cancelled,
            user_wait=request.get('_bridge_user_wait') if raw_deadline is None else None,
        )
        # One batch owns the exclusive lane while the broker applies its own
        # per-method parallel-safety policy inside that ordered batch.
        await self._capability_gate.acquire(False)
        try:
            receipts = await self.manager.broker.invoke_many(
                calls,
                context,
                max_concurrency=effective,
            )
            failed = next((receipt for receipt in receipts if not receipt.ok), None)
            if failed is None:
                return {
                    "ok": True,
                    "results": [
                        (receipt.result_value if isinstance(receipt.result_value, bytes)
                         else json_safe(receipt.result_value)) for receipt in receipts
                    ],
                    "receipt_ids": [receipt.receipt_id for receipt in receipts],
                }
            error = failed.error
            return {
                "ok": False,
                "receipts": [failed.to_dict()],
                "error": {
                    "code": str(error.code if error else failed.status),
                    "message": str(error.message if error else failed.status),
                    "retryable": bool(error.retryable if error else False),
                    "may_have_applied": bool(
                        error.may_have_applied if error else False
                    ),
                },
            }
        finally:
            await asyncio.shield(self._capability_gate.release(False))


    async def start(self) -> None:
        if self.state == "ready":
            return
        if self.state == "starting":
            raise KernelUnavailable("kernel start was re-entered")
        self.state = "starting"
        os.makedirs(self.root, exist_ok=False)
        _restrict_path(self.root, directory=True)
        try:
            _write_private_json(self.descriptor_file, self._descriptor_document())
            self._capability_gate = _CapabilityConcurrencyGate(self.manager.fanout_limit(self.chat_id))
            self.bridge = KernelBridgeServer(
                secret=self.secret,
                nonce=self.nonce,
                generation=self.generation,
                invoke_handler=self._invoke_from_kernel,
                wait_heartbeat_s=self.manager.limits.bridge_timeout_s / 3,
                max_frame_bytes=self.manager.limits.bridge_max_frame_bytes,
                max_in_flight=self.manager.fanout_limit(self.chat_id),
                invoke_handler_limits_concurrency=True,
            )
            host, port = await self.bridge.start()
            env = {
                key: value for key, value in os.environ.items()
                if not any(
                    marker in key.upper()
                    for marker in (
                        "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                        "CREDENTIAL",
                    )
                )
            }
            env.update({
                "VARIANT1_KERNEL_GATE_FILE": self.gate_file,
                "VARIANT1_KERNEL_GATE_TOKEN": self._gate_token,
                "VARIANT1_KERNEL_GATE_TIMEOUT_S": str(
                    self.manager.limits.boot_timeout_s
                ),
                "VARIANT1_KERNEL_BRIDGE_HOST": host,
                "VARIANT1_KERNEL_BRIDGE_PORT": str(port),
                "VARIANT1_KERNEL_BRIDGE_SECRET": base64.urlsafe_b64encode(
                    self.secret
                ).decode("ascii"),
                "VARIANT1_KERNEL_NONCE": self.nonce,
                "VARIANT1_KERNEL_GENERATION": str(self.generation),
                "VARIANT1_KERNEL_DESCRIPTORS": self.descriptor_file,
                "VARIANT1_KERNEL_BRIDGE_TIMEOUT_S": str(
                    self.manager.limits.bridge_timeout_s
                ),
                "VARIANT1_KERNEL_BRIDGE_MAX_BYTES": str(
                    self.manager.limits.bridge_max_frame_bytes
                ),
                "VARIANT1_KERNEL_BRIDGE_ASYNC_CONCURRENCY": str(
                    self.manager.fanout_limit(self.chat_id)
                ),
                "VARIANT1_KERNEL_STREAM_CHUNK_BYTES": str(
                    self.manager.limits.worker_stream_chunk_bytes
                ),
                "VARIANT1_KERNEL_STREAM_CELL_BYTES": str(
                    self.manager.limits.worker_stream_cell_bytes
                ),
                "VARIANT1_KERNEL_RICH_MESSAGE_BYTES": str(
                    self.manager.limits.worker_rich_message_bytes
                ),
                "VARIANT1_REPL_MAX_FRAME_BYTES": str(
                    DEFAULT_MAX_REPL_FRAME_BYTES
                ),
                "VARIANT1_KERNEL_RUNTIME_PROFILE": base64.b64encode(
                    json.dumps(
                        self.runtime_profile.to_dict(),
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8", errors="strict")
                ).decode("ascii"),
                "VARIANT1_BACKEND_INSTANCE_ID": self.manager.instance_id,
                "VARIANT1_KERNEL_PARENT_PID": str(os.getpid()),
                "PYTHONNOUSERSITE": "1",
                "PYTHONUNBUFFERED": "1",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            })
            command = self.manager.worker_command("")
            if self.manager.worker_executable or getattr(sys, "frozen", False):
                # Windows frozen/packaged PATH rewrite only. On POSIX keep the
                # inherited PATH and only prepend the worker directory.
                worker_dir = os.path.dirname(os.path.abspath(command[0]))
                if sys.platform.startswith("win"):
                    windows_root = str(env.get("SystemRoot") or r"C:\Windows")
                    env["PATH"] = os.pathsep.join((
                        worker_dir,
                        os.path.join(windows_root, "System32"),
                        windows_root,
                    ))
                else:
                    env["PATH"] = os.pathsep.join(
                        (worker_dir, str(env.get("PATH") or ""))
                    )
                for inherited in (
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "VIRTUAL_ENV",
                    "CONDA_PREFIX",
                    "CONDA_DEFAULT_ENV",
                ):
                    env.pop(inherited, None)
            # KernelJobObject is OwnedProcessTree: Win32 Job Object on Windows,
            # POSIX process-group ownership on Linux/macOS (no Job Object abort).
            self.job = KernelJobObject(
                max_processes=self.manager.limits.max_processes,
                process_memory_bytes=self.manager.limits.process_memory_bytes,
                job_memory_bytes=self.manager.limits.job_memory_bytes,
                cpu_percent=self.manager.limits.cpu_percent,
            )
            log_path = os.path.join(self.root, "worker.log")
            log_handle = open(log_path, "ab", buffering=0)
            try:
                popen_kwargs: dict[str, Any] = {
                    "cwd": self.workspace_root,
                    "env": env,
                    "stdin": subprocess.PIPE,
                    "stdout": subprocess.PIPE,
                    "stderr": log_handle,
                    "bufsize": 0,
                    "close_fds": True,
                }
                if os.name == "nt":
                    # Hide the console window and start suspended: the launcher
                    # must enter its Job Object before it can start interpreter
                    # children (same ownership fix as the mutation worker).
                    popen_kwargs["creationflags"] = (
                        getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        | CREATE_SUSPENDED
                    )
                else:
                    # POSIX process-group seam (OwnedProcessTree / KernelJobObject).
                    popen_kwargs["start_new_session"] = True
                self.process = subprocess.Popen(command, **popen_kwargs)
            finally:
                log_handle.close()
            try:
                attached = resume_owned_process(self.process, self.job)
            except BaseException:
                with suppress(Exception):
                    self.process.kill()
                with suppress(Exception):
                    self.job.terminate_and_close()
                raise
            if attached is None:
                # The launcher exited before Job assignment; its pipes may hold
                # buffered output but no live worker remains. Retire the job
                # and fail the lease instead of waiting on a dead worker gate.
                with suppress(Exception):
                    self.process.kill()
                with suppress(Exception):
                    self.job.terminate_and_close()
                raise KernelUnavailable(
                    "CPython REPL launcher exited before Job assignment"
                )
            with open(self.gate_file, "x", encoding="utf-8", newline="") as handle:
                handle.write(self._gate_token)
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_path(self.gate_file)
            self.transport = _ReplTransport(
                self.process, max_frame_bytes=DEFAULT_MAX_REPL_FRAME_BYTES
            )
            deadline = time.monotonic() + self.manager.limits.boot_timeout_s
            ready: dict[str, Any] | None = None
            while ready is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("VARIANT-1 CPython REPL readiness deadline expired")
                if self.process.poll() is not None:
                    raise KernelUnavailable("VARIANT-1 CPython REPL exited before ready")
                try:
                    event = await self.transport.receive(
                        timeout_s=min(0.25, remaining)
                    )
                except asyncio.TimeoutError:
                    continue
                if str(event.get("type") or "") == "ready":
                    ready = event
            if (
                int(ready.get("generation") or 0) != self.generation
                or str(ready.get("nonce") or "") != self.nonce
                or str(ready.get("runtime_profile_digest") or "")
                != self.runtime_profile_digest
            ):
                raise KernelUnavailable("CPython REPL ready identity is stale")
            if not self.bridge.handshaken:
                raise KernelUnavailable("REPL did not complete the capability handshake")
            self.state = "ready"
            self.last_used_at = time.monotonic()
            self.manager.emit(
                "kernel:ready",
                status="ok",
                chat_id=self.chat_id,
                kernel_generation=self.generation,
                pid=int(self.process.pid),
                transport="variant1.repl-protocol.v1",
            )
        except asyncio.TimeoutError as exc:
            await self.close(reason="boot_timeout", hard=True)
            raise KernelUnavailable(
                f"VARIANT-1 CPython REPL generation {self.generation} timed out"
            ) from exc
        except BaseException as exc:
            self.state = "unhealthy"
            worker_detail = ""
            worker_exit = self.process.poll() if self.process is not None else None
            with suppress(Exception):
                with open(
                    os.path.join(self.root, "worker.log"),
                    "r",
                    encoding="utf-8",
                    errors="replace",
                ) as handle:
                    worker_detail = handle.read()[-4000:].strip()
            await self.close(reason="boot_failed", hard=True)
            if isinstance(exc, KernelUnavailable):
                raise KernelUnavailable(
                    str(exc) + (f"; worker: {worker_detail}" if worker_detail else "")
                ) from exc
            raise KernelUnavailable(
                f"VARIANT-1 CPython REPL generation {self.generation} failed to start: {exc}"
                + (
                    f"; worker_exit={worker_exit}"
                    if worker_exit is not None
                    else "; worker_exit=running"
                )
                + (f"; worker: {worker_detail}" if worker_detail else "")
            ) from exc

    def _accept_repl_resource(self, content: Any) -> None:
        if (
            not isinstance(content, dict)
            or content.get("schema") != "variant1.kernel-resource-snapshot.v1"
        ):
            return
        process = (
            content.get("process")
            if isinstance(content.get("process"), dict)
            else {}
        )
        namespace = (
            content.get("namespace")
            if isinstance(content.get("namespace"), dict)
            else {}
        )
        contributors = [
            {
                "name": str(item.get("name") or "")[:256],
                "type": str(item.get("type") or "")[:500],
                "estimated_bytes": max(
                    0, int(item.get("estimated_bytes") or 0)
                ),
                "basis": str(item.get("basis") or "unknown")[:80],
            }
            for item in list(namespace.get("contributors") or ())[:20]
            if isinstance(item, dict)
        ]
        self._resource_snapshot = {
            "schema": "variant1.kernel-resource-snapshot.v1",
            "process": {
                "rss_bytes": max(0, int(process.get("rss_bytes") or 0)),
                "virtual_bytes": max(
                    0, int(process.get("virtual_bytes") or 0)
                ),
                "cpu_user_s": max(0.0, float(process.get("cpu_user_s") or 0.0)),
                "cpu_system_s": max(
                    0.0, float(process.get("cpu_system_s") or 0.0)
                ),
                "threads": max(0, int(process.get("threads") or 0)),
            },
            "namespace": {
                "values": max(0, int(namespace.get("values") or 0)),
                "estimated_bytes": max(
                    0, int(namespace.get("estimated_bytes") or 0)
                ),
                "contributors": contributors,
                "contributors_truncated": bool(
                    namespace.get("contributors_truncated")
                ),
            },
            "runtime_profile": dict(content.get("runtime_profile") or {}),
            "received_at": time.time(),
        }

    def _worker_log_tail(self, limit: int = 4000) -> str:
        try:
            with open(
                os.path.join(self.root, "worker.log"),
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                return handle.read()[-max(1, int(limit)):].strip()
        except Exception:
            return ""

    def _record_background_event(self, event: dict[str, Any]) -> None:
        if str(event.get("type") or "") not in {"stdout", "stderr"}:
            return
        self._background_events.append({
            "type": str(event.get("type") or ""),
            "origin_id": str(event.get("id") or "") or None,
            "text": str(event.get("text") or "")[:4000],
            "received_at": time.time(),
        })
        del self._background_events[:-64]

    def _record_worker_diagnostic(self, event: dict[str, Any]) -> None:
        if str(event.get("type") or "") != "diagnostic":
            return
        self._worker_diagnostics.append(
            str(event.get("message") or event.get("code") or "")[:500]
        )
        del self._worker_diagnostics[:-32]

    async def _repl_control(
        self,
        operation: str,
        *,
        payload: dict[str, Any] | None = None,
        document: dict[str, Any] | None = None,
        limit: int | None = None,
        timeout_s: float = 60.0,
    ) -> dict[str, Any]:
        transport = self.transport
        if transport is None:
            raise KernelUnavailable("CPython REPL transport is absent")
        request_id = "ctl-" + uuid.uuid4().hex
        fields: dict[str, Any] = {}
        if payload is not None:
            fields["payload"] = payload
        if document is not None:
            fields["document"] = document
        if limit is not None:
            fields["limit"] = int(limit)
        try:
            transport.send(request_id, operation, **fields)
        except Exception as exc:
            await self.close(reason=f"repl_{operation}_dispatch_failed", hard=True)
            raise KernelUnavailable(
                f"REPL {operation} request could not be dispatched"
            ) from exc
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self.close(reason=f"repl_{operation}_timeout", hard=True)
                raise KernelUnavailable(f"REPL {operation} request timed out")
            try:
                event = await transport.receive(timeout_s=min(0.25, remaining))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                await self.close(
                    reason=f"repl_{operation}_cancelled", hard=True
                )
                raise
            except Exception as exc:
                await self.close(
                    reason=f"repl_{operation}_transport_failed", hard=True
                )
                raise KernelUnavailable(
                    f"REPL {operation} response transport failed"
                ) from exc
            event_id = str(event.get("id") or "")
            event_type = str(event.get("type") or "")
            if event_type == "resource_snapshot":
                self._accept_repl_resource(event.get("content"))
            elif event_type == "diagnostic":
                self._record_worker_diagnostic(event)
            elif event_type in {"stdout", "stderr"} and event_id != request_id:
                self._record_background_event(event)
            if event_id != request_id:
                continue
            if event_type != "done":
                continue
            if str(event.get("status") or "") != "ok":
                error = event.get("error") if isinstance(event.get("error"), dict) else {}
                raise KernelUnavailable(
                    str(error.get("message") or f"REPL {operation} failed")
                )
            result = event.get("result")
            return dict(result) if isinstance(result, dict) else {}

    async def _sync_namespace(
        self,
        document: dict[str, Any] | None,
    ) -> None:
        key = self._document_key(document)
        if key is None:
            return
        if key == self._namespace_key:
            self._namespace_sync_skipped += 1
            return
        await self._repl_control(
            "mount",
            document=document,
            timeout_s=min(15.0, max(2.0, self.manager.limits.boot_timeout_s)),
        )
        self._namespace_key = key
        self._namespace_sync_count += 1

    async def _capsule_request(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        if self.state != "ready" or self._closed:
            raise KernelCapsuleError(
                "kernel_not_idle",
                "Kernel capsule operations require a ready idle generation.",
            )
        mapping = {
            "capture": "capsule_capture",
            "inspect": "capsule_inspect",
            "restore": "capsule_restore",
        }
        request_type = mapping.get(str(operation))
        if not request_type:
            raise KernelCapsuleError(
                "capsule_protocol_error", "Unknown capsule worker operation."
            )
        self.state = "busy"
        try:
            configured_timeout = float(self.manager.limits.cell_timeout_s)
            control_timeout = (
                max(2.0, min(60.0, configured_timeout))
                if configured_timeout > 0
                else 60.0
            )
            result = await self._repl_control(
                request_type,
                payload=payload,
                timeout_s=control_timeout,
            )
            encoded_size = len(
                json.dumps(
                    result,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if encoded_size > int(max_response_bytes):
                raise KernelCapsuleError(
                    "capsule_response_too_large",
                    "Kernel capsule response exceeded the host-owned byte bound.",
                )
            return result
        except KernelCapsuleError:
            if operation == "restore" and not self._closed:
                await self.close(
                    reason="capsule_restore_unknown_effect", hard=True
                )
            raise
        except Exception as exc:
            code = (
                "capsule_restore_unknown_effect"
                if "capsule_restore_unknown_effect" in str(exc)
                else "capsule_worker_error"
            )
            raise KernelCapsuleError(code, str(exc)) from exc
        finally:
            self.last_used_at = time.monotonic()
            if (
                not self._closed
                and self.process is not None
                and self.process.poll() is None
            ):
                self.state = "ready"

    async def _interrupt_repl_or_kill(
        self,
        *,
        execution_id: str,
        collector: CellOutputCollector,
        reason: str,
        terminal: dict[str, Any] | None = None,
        intent: str = "stop",
    ) -> tuple[dict[str, Any] | None, bool]:
        selected_intent = "steer" if str(intent) == "steer" else "stop"
        may_hard_kill = selected_intent == "stop"

        def terminal_intent_requested() -> bool:
            return self._external_interrupts.get(execution_id) == "stop"

        def promote_terminal_intent() -> bool:
            nonlocal may_hard_kill
            if may_hard_kill or not terminal_intent_requested():
                return False
            may_hard_kill = True
            self.state = "stopping"
            return True

        cancellation = self._cell_cancellations.get(execution_id)
        if cancellation is not None:
            cancellation.set()
        transport = self.transport
        if transport is None:
            await self.close(reason=reason, hard=True)
            return None, True
        if may_hard_kill:
            self.state = "stopping"
        grace_s = max(0.1, self.manager.limits.interrupt_grace_s)
        waiting_emitted = False

        def emit_safe_boundary_wait() -> None:
            nonlocal waiting_emitted
            if waiting_emitted:
                return
            waiting_emitted = True
            self.manager.emit(
                "kernel:steer_waiting_safe_boundary",
                status="waiting",
                chat_id=self.chat_id,
                kernel_generation=self.generation,
                execution_id=execution_id,
            )

        # Settle admitted host effects before unwinding Python.  Otherwise a
        # KeyboardInterrupt can abandon a dispatched effect whose outcome is
        # still unknown.  Steer waits for this safe boundary; explicit Stop is
        # allowed to fall back to generation replacement after the soft grace.
        if self.bridge is not None:
            settled = await self.bridge.cancel_execution(
                execution_id,
                timeout_s=grace_s,
            )
            if not settled:
                if may_hard_kill:
                    await self.close(reason=reason, hard=True)
                    return None, True
                emit_safe_boundary_wait()
                while not settled:
                    if promote_terminal_intent():
                        # A later terminal request receives its own soft bridge
                        # settlement window before destructive escalation.
                        settled = await self.bridge.cancel_execution(
                            execution_id,
                            timeout_s=grace_s,
                        )
                        if not settled:
                            await self.close(
                                reason="terminal_interrupt", hard=True
                            )
                            return None, True
                        break
                    settled = await self.bridge.cancel_execution(
                        execution_id,
                        timeout_s=grace_s,
                    )

        promote_terminal_intent()

        if terminal is None:
            interrupt_id = "interrupt-" + uuid.uuid4().hex
            with suppress(Exception):
                transport.send(
                    interrupt_id, "interrupt", target_id=execution_id
                )

        deadline = time.monotonic() + grace_s
        while terminal is None:
            if promote_terminal_intent():
                # The prior Steer may have been caught or deferred by a
                # blocking call.  Give terminal Stop one fresh targeted soft
                # interrupt and one grace window before replacing the worker.
                with suppress(Exception):
                    transport.send(
                        "interrupt-" + uuid.uuid4().hex,
                        "interrupt",
                        target_id=execution_id,
                    )
                deadline = time.monotonic() + grace_s
            if may_hard_kill and time.monotonic() >= deadline:
                break
            if (
                not may_hard_kill
                and not waiting_emitted
                and time.monotonic() >= deadline
            ):
                emit_safe_boundary_wait()
            if self.process is None or self.process.poll() is not None:
                await self.close(reason="process_died", hard=True)
                return None, True
            try:
                event = await transport.receive(timeout_s=0.05)
            except asyncio.TimeoutError:
                continue
            event_type = str(event.get("type") or "")
            event_id = str(event.get("id") or "")
            if event_type == "resource_snapshot":
                self._accept_repl_resource(event.get("content"))
            elif event_id == execution_id and event_type == "done":
                terminal = event
                break
            elif event_id == execution_id:
                collector.accept_event(event)
            elif event_type in {"stdout", "stderr"}:
                self._record_background_event(event)
                collector.stale()
        if terminal is not None:
            self.state = "ready"
            return terminal, False
        # Only an explicit terminal intent reaches this escalation.  A Steer
        # remains attached to the active cell until its safe terminal boundary
        # or until the user separately requests Stop.
        await self.close(
            reason=(
                "terminal_interrupt"
                if terminal_intent_requested()
                else reason
            ),
            hard=True,
        )
        return None, True

    async def execute(
        self,
        code: str,
        admission: ExecutionAdmission,
        *,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        timeout_s: float | None = None,
    ) -> KernelExecutionResult:
        async with self.execution_lock:
            if self.state != "ready" or self._closed:
                raise KernelUnavailable("kernel generation became unavailable")
            transport = self.transport
            if transport is None:
                raise KernelUnavailable("CPython REPL transport is absent")
            self.state = "busy"
            started = time.monotonic()
            collector = CellOutputCollector(
                limits=self.manager.limits.output,
                artifact_store=self.manager.artifact_store,
                artifact_scope=self.chat_id,
                on_chunk=on_chunk,
            )
            terminal: dict[str, Any] | None = None
            hard_restarted = False
            status = "ok"
            error_code = ""
            error_message = ""
            execution_count = 0
            cell_cancellation = asyncio.Event()
            run_cancellation = admission.cancellation
            admission = replace(
                admission,
                cancellation=lambda: (
                    cell_cancellation.is_set()
                    or cancellation_is_requested(run_cancellation)
                ),
            )
            live_context = current_run_context()
            live_context = (
                replace(live_context, run_id=admission.run_id,
                        work_scope=admission.work_scope,
                        metadata={**live_context.metadata, "chat_id": admission.chat_id},
                        cancellation=admission.cancellation)
                if live_context is not None else
                Variant1RunContext.create(
                    source="kernel", run_id=admission.run_id,
                    work_scope=admission.work_scope,
                    cancellation=admission.cancellation,
                )
            )
            with bind_run_context(live_context):
                admission = replace(admission, callback_context=copy_context())
            self._cell_cancellations[admission.execution_id] = cell_cancellation
            try:
                await self._sync_namespace(admission.namespace_document)
                self._admissions[admission.execution_id] = admission
                transport.send(
                    admission.execution_id,
                    "execute",
                    code=str(code),
                    admission={
                        "schema": "variant1.kernel-execution-admission.v1",
                        "execution_id": admission.execution_id,
                        "outer_tool_call_id": admission.outer_tool_call_id,
                        "chat_id": admission.chat_id,
                        "generation": admission.generation,
                    },
                )
                configured_timeout = (
                    float(self.manager.limits.cell_timeout_s)
                    if timeout_s is None
                    else float(timeout_s)
                )
                deadline = (
                    time.monotonic() + max(0.1, configured_timeout)
                    if configured_timeout > 0
                    else None
                )
                while terminal is None:
                    external_intent = self._external_interrupts.get(
                        admission.execution_id
                    )
                    if admission.cancelled() or external_intent:
                        status = "cancelled"
                        error_code = "kernel_cell_cancelled"
                        error_message = "Cell cancelled by the host."
                        terminal, hard_restarted = await self._interrupt_repl_or_kill(
                            execution_id=admission.execution_id,
                            collector=collector,
                            reason=(
                                "steered"
                                if external_intent == "steer"
                                else "cancelled"
                            ),
                            intent=external_intent or "stop",
                        )
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        status = "timed_out"
                        error_code = "kernel_cell_timeout"
                        error_message = "Cell exceeded its host-owned deadline."
                        terminal, hard_restarted = await self._interrupt_repl_or_kill(
                            execution_id=admission.execution_id,
                            collector=collector,
                            reason="timed_out",
                        )
                        break
                    if self.process is None or self.process.poll() is not None:
                        status = "kernel_died"
                        error_code = "kernel_process_died"
                        error_message = "The VARIANT-1 CPython REPL exited during the cell."
                        await self.close(reason="process_died", hard=True)
                        hard_restarted = True
                        break
                    try:
                        event = await transport.receive(timeout_s=0.05)
                    except asyncio.TimeoutError:
                        continue
                    event_id = str(event.get("id") or "")
                    event_type = str(event.get("type") or "")
                    if event_type == "resource_snapshot":
                        self._accept_repl_resource(event.get("content"))
                    elif event_type == "diagnostic":
                        self._record_worker_diagnostic(event)
                    elif event_id == admission.execution_id and event_type == "done":
                        terminal = event
                    elif event_id == admission.execution_id:
                        collector.accept_event(event)
                    elif event_type in {"stdout", "stderr"}:
                        self._record_background_event(event)
                        collector.stale()
                if terminal is not None and status == "ok" and (
                    admission.cancelled()
                    or admission.execution_id in self._external_interrupts
                    or str(terminal.get("status") or "") == "cancelled"
                ):
                    # Interrupt and done can cross while receive() is suspended.
                    # The already received terminal frame does not settle this
                    # cell's independently dispatched host calls.
                    status = "cancelled"
                    error_code = "kernel_cell_cancelled"
                    error_message = "Cell cancelled by the host."
                    terminal, hard_restarted = await self._interrupt_repl_or_kill(
                        execution_id=admission.execution_id,
                        collector=collector,
                        reason=(
                            "steered"
                            if self._external_interrupts.get(
                                admission.execution_id
                            ) == "steer"
                            else "cancelled"
                        ),
                        terminal=terminal,
                        intent=self._external_interrupts.get(
                            admission.execution_id, "stop"
                        ),
                    )
                if terminal is not None:
                    execution_count = int(terminal.get("execution_count") or 0)
                    terminal_status = str(terminal.get("status") or "")
                    if status == "ok" and terminal_status != "ok":
                        status = (
                            "cancelled"
                            if terminal_status == "cancelled"
                            else "error"
                        )
                        error = (
                            terminal.get("error")
                            if isinstance(terminal.get("error"), dict)
                            else {}
                        )
                        error_code = str(
                            error.get("code")
                            or (
                                "capability_error"
                                if collector.result.error_name
                                == "Variant1CapabilityError"
                                else "python_exception"
                            )
                        )
                        error_message = str(
                            error.get("message")
                            or collector.result.error_value
                            or terminal_status
                        )
                if status == "ok" and collector.result.error_name:
                    status = "error"
                    error_code = (
                        "capability_error"
                        if collector.result.error_name == "Variant1CapabilityError"
                        else "python_exception"
                    )
                    error_message = ": ".join(
                        item
                        for item in (
                            collector.result.error_name,
                            collector.result.error_value,
                        )
                        if item
                    )
            except asyncio.CancelledError as cancellation:
                cleanup = asyncio.create_task(
                    self._interrupt_repl_or_kill(
                        execution_id=admission.execution_id,
                        collector=collector,
                        reason="task_cancelled",
                        intent="stop",
                    ),
                    name=(
                        f"repl-cancel-cleanup:{self.chat_id}:"
                        f"{admission.execution_id}"
                    ),
                )
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                with suppress(BaseException):
                    cleanup.result()
                cancellation.kernel_result = KernelExecutionResult(
                    execution_id=admission.execution_id, chat_id=self.chat_id, generation=self.generation,
                    status='cancelled', output=collector.result, execution_count=execution_count,
                    error_code='cancelled', error_message='Cell cancelled; partial effects may already exist.',
                    duration_ms=(time.monotonic()-started)*1000, hard_restarted=self._closed,
                )
                raise cancellation
            except BaseException as exc:
                status = "protocol_error"
                error_code = "kernel_protocol_error"
                worker_detail = self._worker_log_tail()
                error_message = (str(exc) or type(exc).__name__) + (
                    f"; worker: {worker_detail}" if worker_detail else ""
                )
                await self.close(reason="protocol_error", hard=True)
                hard_restarted = True
            finally:
                self._admissions.pop(admission.execution_id, None)
                if self.bridge is not None:
                    await self.bridge.forget_execution(admission.execution_id)
                self._cell_cancellations.pop(admission.execution_id, None)
                self._external_interrupts.pop(admission.execution_id, None)
                self.last_used_at = time.monotonic()
                if not self._closed and self.state != "stopping":
                    self.state = "ready"
            result = KernelExecutionResult(
                execution_id=admission.execution_id,
                chat_id=self.chat_id,
                generation=self.generation,
                status=status,
                output=collector.result,
                execution_count=execution_count,
                reply_status=(
                    str(terminal.get("status") or "") if terminal else ""
                ),
                error_code=error_code,
                error_message=error_message,
                duration_ms=(time.monotonic() - started) * 1000,
                hard_restarted=hard_restarted,
                terminate=bool(collector.result.terminate_requested),
            )
            projection_limit = max(1, int(self.manager.limits.output.max_cell_bytes))
            self._last_namespace_delta = dict(result.output.namespace_delta)
            self._namespace_inventory = (
                tuple(result.output.namespace_inventory)
                if result.output.namespace_inventory is not None else None
            )
            self._namespace_inventory_omitted = result.output.namespace_inventory_omitted
            artifact_limit = max(
                1, int(self.manager.limits.output.max_cell_artifact_bytes)
            )
            self._last_output_pressure = {
                "execution_id": admission.execution_id,
                "projection_bytes": int(result.output.admitted_bytes),
                "projection_limit_bytes": projection_limit,
                "projection_ratio": round(
                    min(1.0, result.output.admitted_bytes / projection_limit), 6
                ),
                "artifact_bytes": int(result.output.evidence_artifact_bytes),
                "artifact_limit_bytes": artifact_limit,
                "artifact_ratio": round(
                    min(
                        1.0,
                        result.output.evidence_artifact_bytes / artifact_limit,
                    ),
                    6,
                ),
                "truncated": bool(result.output.truncated),
            }
            self.manager.emit(
                "kernel:execution_result",
                status=result.status,
                chat_id=self.chat_id,
                kernel_generation=self.generation,
                execution_id=admission.execution_id,
                duration_ms=result.duration_ms,
                dropped_bytes=result.output.dropped_bytes,
                stale_events=result.output.stale_events,
                hard_restarted=result.hard_restarted,
            )
            return result

    def _request_interrupt(self, *, intent: str, send: bool) -> str:
        """Fence one admitted cell for Steer or terminal Stop.

        Steer records intent first and lets ``execute`` settle bridge effects
        before the worker interrupt is written.  Explicit restart/Stop paths
        may request an immediate best-effort write while their owner proceeds
        with terminal cleanup.
        """

        transport = self.transport
        if transport is None or not self._admissions:
            return ""
        execution_id = next(iter(self._admissions))
        selected_intent = "steer" if str(intent) == "steer" else "stop"
        prior = self._external_interrupts.get(execution_id)
        if prior != "stop":
            self._external_interrupts[execution_id] = selected_intent
        cancellation = self._cell_cancellations.get(execution_id)
        if cancellation is not None:
            cancellation.set()
        if send:
            transport.send(
                "interrupt-" + uuid.uuid4().hex,
                "interrupt",
                target_id=execution_id,
            )
        return execution_id

    def _send_interrupt(self, *, intent: str = "stop") -> str:
        """Immediately send one targeted interrupt for terminal control paths."""

        return self._request_interrupt(intent=intent, send=True)

    async def close(self, *, reason: str = "shutdown", hard: bool = False) -> None:
        """Finish the owned teardown before propagating caller cancellation."""
        async with self._close_lock:
            if self._closed:
                return
            cancelled: asyncio.CancelledError | None = None
            force_close = asyncio.Event()
            cleanup = asyncio.create_task(
                self._close_resources(reason=reason, hard=hard, force_close=force_close),
                name=f"kernel-close:{self.chat_id}:{self.generation}",
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancelled = exc
                    force_close.set()
                    # Wake the graceful pipe/process waits promptly, while the
                    # shielded owner continues through every remaining cleanup.
                    process = self.process
                    if process is not None and process.poll() is None:
                        with suppress(Exception):
                            process.kill()
            cleanup.result()
            if cancelled is not None:
                raise cancelled

    async def _close_resources(
        self, *, reason: str, hard: bool, force_close: asyncio.Event
    ) -> None:
        self.state = "stopping"
        for cancellation in self._cell_cancellations.values():
            cancellation.set()
        transport = self.transport
        process = self.process
        if (
            not hard
            and transport is not None
            and process is not None
            and process.poll() is None
        ):
            request_id = "shutdown-" + uuid.uuid4().hex
            deadline = time.monotonic() + self.manager.limits.shutdown_grace_s
            with suppress(Exception):
                transport.send(request_id, "shutdown")
                while time.monotonic() < deadline:
                    if force_close.is_set():
                        break
                    event = await transport.receive(timeout_s=0.1)
                    if (
                        str(event.get("id") or "") == request_id
                        and str(event.get("type") or "") == "done"
                    ):
                        break
            while (process.poll() is None and time.monotonic() < deadline
                   and not force_close.is_set()):
                await asyncio.sleep(0.05)
            hard = process.poll() is None
        hard = hard or force_close.is_set()
        failures = []
        if process is not None and process.poll() is None:
            process_error = None
            try:
                process.kill()
            except Exception as exc:
                process_error = exc
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(process.wait), timeout=2.0
                )
            except Exception as exc:
                process_error = exc
            if process.poll() is None:
                failures.append(process_error or RuntimeError("kernel process remains alive"))
        if process is None or process.poll() is not None:
            self.process = None
        if self.job is not None:
            try:
                self.job.terminate_and_close()
                self.job = None
            except Exception as exc:
                failures.append(exc)
        if transport is not None:
            try:
                transport.close()
                self.transport = None
            except Exception as exc:
                failures.append(exc)
        if self.bridge is not None:
            try:
                await self.bridge.close()
                self.bridge = None
            except Exception as exc:
                failures.append(exc)
        if failures:
            self.state = "close_failed"
            raise ExceptionGroup("kernel resource cleanup failed", failures)
        self._admissions.clear()
        self._cell_cancellations.clear()
        self._external_interrupts.clear()
        for path in (self.gate_file, self.descriptor_file):
            with suppress(OSError):
                os.remove(path)
        with suppress(OSError):
            from file_paths import remove_tree
            remove_tree(self.root)
        self.state = "absent"
        self._closed = True
        self.manager.lease_closed(self.chat_id, self)
        self.manager.emit(
            "kernel:closed",
            status="ok",
            chat_id=self.chat_id,
            kernel_generation=self.generation,
            reason=reason,
            hard=bool(hard),
        )


__all__ = ["KernelLease"]
