"""Internal invocation runtime for the Python action and nested capabilities.

The outer action enters through ``action_executor``; nested calls enter through
the persistent kernel bridge. Both resolve an immutable capability reference
and produce one typed, correlated terminal receipt. Category mounts determine
disclosure only; this runtime carries no capability-admission policy.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Mapping

from core_invariants import (
    CellOrigin,
    canonical_digest,
    canonical_json,
    probe_cancellation,
    request_fingerprint,
)
from tool_core import (
    ArtifactRef,
    CapabilityErrorInfo,
    CapabilityReceipt,
    ContentBlock,
    EffectRecord,
    ToolError,
    ToolExecutionResult,
    ToolProjectionResult,
    TruncationRecord,
    json_safe,
)
from work_fabric.scope import WorkScope, coerce_work_scope


CAPABILITY_REF_SCHEMA = "variant1.capability-ref.v1"
DEFAULT_INLINE_RESULT_BYTES = 64 * 1024
DEFAULT_MAX_BATCH_CALLS = 100
DEFAULT_MAX_FANOUT = 8


_CURRENT_INVOCATION: ContextVar["InvocationContext | None"] = ContextVar(
    "variant1_capability_invocation", default=None
)


def current_capability_invocation() -> "InvocationContext | None":
    """Return the host-owned invocation snapshot inside a tool handler."""
    return _CURRENT_INVOCATION.get()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return canonical_json(json_safe(value))


def _sha256_json(value: Any) -> str:
    return canonical_digest(value)


def capability_request_fingerprint(
    tool_name: str,
    args: Mapping[str, Any] | None,
) -> str:
    """Canonical fingerprint shared by broker admission and internal WALs."""

    return request_fingerprint(str(tool_name or ""), dict(args or {}))


def uncertain_outer_call_outcome(tool_name: str, args: Mapping[str, Any], call_id: str) -> dict:
    message = (
        "This call crossed its durable dispatch boundary before the prior "
        "process stopped, but no terminal result was committed. VARIANT-1 will "
        "not execute it again automatically. Inspect the owning service or "
        "reconcile the effect before issuing a new call."
    )
    return {"tool": str(tool_name), "args": dict(args), "result": message,
            "model_result": message, "ok": False, "executed": True,
            "status": "needs_reconciliation", "call_id": str(call_id),
            "source_chars": len(message), "visible_chars": len(message),
            "truncated": False, "error_class": "needs_reconciliation",
            "terminate": False, "durable_replay": True}


_WORK_SCOPE_KEYS = frozenset({
    "chat_id",
    "conversation_id",
    "branch_id",
    "workspace_id",
    "workspace_revision",
    "goal_id",
    "goal_run_id",
    "step_id",
    "attempt",
    "worktree_id",
    "kernel_generation",
    "catalog_release_id",
})


def _scope_dict(value: Any) -> dict[str, Any]:
    """Project only the bounded, durable WorkScope attribution fields."""
    if value is None:
        return {}
    projector = getattr(value, "to_dict", None)
    if callable(projector):
        try:
            value = projector()
        except Exception:
            return {}
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): json_safe(item)
        for key, item in value.items()
        if str(key) in _WORK_SCOPE_KEYS and item not in (None, "")
    }


@dataclass(frozen=True)
class CapabilityMetadata:
    capability_id: str
    schema_revision: str
    handler_revision: str
    effect_class: str
    parallel_safe: bool
    idempotency: str
    touches_desktop: bool
    may_return_secrets: bool
    default_deadline_ms: int
    result_projection: str = "typed-content-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "schema_revision": self.schema_revision,
            "handler_revision": self.handler_revision,
            "effect_class": self.effect_class,
            "parallel_safe": bool(self.parallel_safe),
            "idempotency": self.idempotency,
            "touches_desktop": bool(self.touches_desktop),
            "may_return_secrets": bool(self.may_return_secrets),
            "default_deadline_ms": int(self.default_deadline_ms),
            "result_projection": self.result_projection,
        }


@dataclass(frozen=True)
class CapabilityRef:
    capability_id: str
    schema_revision: str
    handler_revision: str
    catalog_release_id: str
    slot_id: str = ""
    slot_version: int = 0

    def __post_init__(self) -> None:
        if not str(self.catalog_release_id or "").strip():
            raise ValueError("CapabilityRef requires a catalog release")

    @property
    def opaque_id(self) -> str:
        basis = (
            f"{self.catalog_release_id}\0{self.capability_id}\0"
            f"{self.schema_revision}\0{self.handler_revision}\0"
            f"{self.slot_id}\0{self.slot_version}"
        )
        return "cap_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:28]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAPABILITY_REF_SCHEMA,
            "ref_id": self.opaque_id,
            "capability_id": self.capability_id,
            "schema_revision": self.schema_revision,
            "handler_revision": self.handler_revision,
            "catalog_release_id": self.catalog_release_id,
            "slot_id": self.slot_id or None,
            "slot_version": int(self.slot_version),
        }


@dataclass(frozen=True)
class InvocationContext:
    chat_id: str
    run_id: str
    outer_tool_call_id: str
    cell_execution_id: str
    nested_call_id: str
    catalog_release_id: str
    principal_actor_id: str = "model"
    workspace_root_ids: tuple[str, ...] = ()
    kernel_generation: str = ""
    mount_revision: int = 0
    slot_id: str = ""
    slot_version: int = 0
    connector_config_revision: str = ""
    environment_digest: str = ""
    deadline_ms: int = 0
    idempotency_key: str = ""
    batch_id: str = ""
    batch_index: int = -1
    surface: str = "provider"
    work_scope: WorkScope = field(default_factory=WorkScope)
    desktop_lock_held: bool = False
    cancellation: Any = field(default=None, repr=False, compare=False)
    user_wait: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not str(self.catalog_release_id or "").strip():
            raise ValueError("InvocationContext requires a catalog release")
        object.__setattr__(self, "work_scope", coerce_work_scope(self.work_scope))

    def cancelled(self) -> bool:
        return self.cancellation_status().requested

    def cancellation_status(self):
        return probe_cancellation(self.cancellation)

    @property
    def cell_origin(self) -> CellOrigin:
        return CellOrigin(
            chat_id=self.chat_id,
            run_id=self.run_id,
            outer_tool_call_id=self.outer_tool_call_id,
            cell_execution_id=self.cell_execution_id,
            nested_call_id=self.nested_call_id,
            kernel_generation=self.kernel_generation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "run_id": self.run_id,
            "outer_tool_call_id": self.outer_tool_call_id,
            "cell_execution_id": self.cell_execution_id,
            "nested_call_id": self.nested_call_id,
            "principal_actor_id": self.principal_actor_id,
            "workspace_root_ids": list(self.workspace_root_ids),
            "kernel_generation": self.kernel_generation,
            "catalog_release_id": self.catalog_release_id,
            "mount_revision": int(self.mount_revision),
            "slot_id": self.slot_id or None,
            "slot_version": int(self.slot_version),
            "connector_config_revision": self.connector_config_revision or None,
            "environment_digest": self.environment_digest,
            "deadline_ms": int(self.deadline_ms),
            "idempotency_key": self.idempotency_key or None,
            "batch_id": self.batch_id or None,
            "batch_index": self.batch_index if self.batch_index >= 0 else None,
            "surface": self.surface,
            "work_scope": _scope_dict(self.work_scope),
            "cell_origin": self.cell_origin.to_dict(),
        }


@dataclass(frozen=True)
class CapabilityCall:
    ref: CapabilityRef
    args: Mapping[str, Any]
    nested_call_id: str = ""
    deadline_ms: int = 0
    idempotency_key: str = ""


class _BrokerTimedOut(RuntimeError):
    pass


class _BrokerCancelled(RuntimeError):
    pass


class _BrokerCancellationAuthorityFailed(_BrokerCancelled):
    pass


class CapabilityBroker:
    """Validate, execute, correlate, and project host invocations.

    The historical class name remains internal. It does not grant authority,
    apply permission policy, or reject calls based on category disclosure.
    """

    def __init__(
        self,
        *,
        registry: Any,
        runtime_registry: Any,
        enabled_resolver: Callable[[], set[str]],
        artifact_store: Any = None,
        outer_call_repository: Any = None,
        uses_desktop_surface: Callable[[str], bool] | None = None,
        desktop_action_lock: Any = None,
        inline_result_bytes: int = DEFAULT_INLINE_RESULT_BYTES,
        max_batch_calls: int = DEFAULT_MAX_BATCH_CALLS,
        max_fanout: int = DEFAULT_MAX_FANOUT,
        dedupe_capacity: int = 4096,
    ) -> None:
        self.registry = registry
        if runtime_registry is None:
            raise TypeError("CapabilityBroker requires SessionRuntimeRegistry")
        self.runtime_registry = runtime_registry
        self.enabled_resolver = enabled_resolver
        self.artifact_store = artifact_store
        self.outer_call_repository = outer_call_repository
        self.uses_desktop_surface = uses_desktop_surface or (lambda _name: False)
        self.desktop_action_lock = desktop_action_lock
        self.inline_result_bytes = max(1024, int(inline_result_bytes))
        self.max_batch_calls = max(1, int(max_batch_calls))
        self.max_fanout = max(1, int(max_fanout))
        self.dedupe_capacity = max(128, int(dedupe_capacity))
        self._lock = threading.RLock()
        self._ledger: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._receipt_sinks: list[tuple[Callable[[CapabilityReceipt], Any], bool]] = []

    def register_receipt_sink(
        self,
        callback: Callable[[CapabilityReceipt], Any],
        *,
        required: bool = True,
    ) -> None:
        if not callable(callback):
            raise TypeError("receipt sink must be callable")
        with self._lock:
            entry = (callback, bool(required))
            if entry not in self._receipt_sinks:
                self._receipt_sinks.append(entry)

    def metadata_for_tool(
        self, tool: Any, args: Mapping[str, Any] | None = None,
    ) -> CapabilityMetadata:
        raw = (
            tool.broker_metadata()
            if callable(getattr(tool, "broker_metadata", None))
            else {}
        )
        argument_metadata = getattr(tool, "broker_metadata_for_args", None)
        if args is not None and callable(argument_metadata):
            raw = {**raw, **dict(argument_metadata(dict(args)) or {})}
        capability_id = str(raw.get("capability_id") or getattr(tool, "name", ""))
        touches = raw.get("touches_desktop")
        if touches is None:
            touches = bool(self.uses_desktop_surface(str(getattr(tool, "name", ""))))
        effect_class = str(raw.get("effect_class") or "external_side_effect")
        parallel_safe = bool(raw.get("parallel_safe")) and not bool(touches)
        return CapabilityMetadata(
            capability_id=capability_id,
            schema_revision=str(raw.get("schema_revision") or "unversioned-schema"),
            handler_revision=str(raw.get("handler_revision") or "unversioned-handler"),
            effect_class=effect_class,
            parallel_safe=parallel_safe,
            idempotency=str(raw.get("idempotency") or "none"),
            touches_desktop=bool(touches),
            may_return_secrets=bool(raw.get("may_return_secrets")),
            default_deadline_ms=max(0, int(raw.get("default_deadline_ms") or 0)),
            result_projection=str(raw.get("result_projection") or "typed-content-v1"),
        )

    async def admits_control(
        self, ref: CapabilityRef, args: dict[str, Any], context: InvocationContext,
    ) -> bool:
        """Validate host-owned control scheduling; normal broker admission follows."""
        tool, _ = self._resolve(ref)
        validator = getattr(tool, "control_admission", None)
        if not callable(validator):
            return False
        decision = validator(context, args)
        if inspect.isawaitable(decision):
            decision = await decision
        return decision is True

    def ref_for_name(
        self,
        name: str,
        *,
        catalog_release_id: str,
        slot_id: str = "",
        slot_version: int = 0,
    ) -> CapabilityRef:
        tool = self.registry.get(str(name or ""))
        if tool is None:
            raise LookupError(f"unknown capability: {name}")
        metadata = self.metadata_for_tool(tool)
        return CapabilityRef(
            capability_id=metadata.capability_id,
            schema_revision=metadata.schema_revision,
            handler_revision=metadata.handler_revision,
            catalog_release_id=str(catalog_release_id or ""),
            slot_id=str(slot_id or ""),
            slot_version=max(0, int(slot_version or 0)),
        )

    def _resolve(self, ref: CapabilityRef) -> tuple[Any, CapabilityMetadata]:
        getter = getattr(self.registry, "get_capability", None)
        tool = getter(ref.capability_id) if callable(getter) else self.registry.get(ref.capability_id)
        if tool is None:
            raise ToolError(f"capability is unavailable: {ref.capability_id}")
        metadata = self.metadata_for_tool(tool)
        if (
            metadata.schema_revision != ref.schema_revision
            or metadata.handler_revision != ref.handler_revision
        ):
            raise ToolError(
                "stale capability reference: schema or handler revision changed"
            )
        return tool, metadata

    def context_for_provider(
        self,
        *,
        call_id: str,
        should_stop: Any = None,
        desktop_lock_held: bool = False,
    ) -> InvocationContext:
        try:
            from run_context import current_run_context

            current = current_run_context()
        except Exception:
            current = None
        run_id = str(getattr(current, "run_id", "") or "provider-unbound")
        thread_id = str(getattr(current, "thread_id", "") or run_id)
        metadata = dict(getattr(current, "metadata", {}) or {})
        chat_id = str(metadata.get("chat_id") or thread_id)
        if not chat_id:
            raise RuntimeError("provider action has no durable chat identity")
        record = self.runtime_registry.ensure_runtime(chat_id)
        catalog_release_id = str(record.identity.catalog_release_id or "").strip()
        if not catalog_release_id:
            raise RuntimeError("provider action has no pinned catalog release")
        tool = self.registry.get("ipython")
        if tool is None:
            raise RuntimeError("provider Python action is unavailable")
        def cancelled() -> bool:
            checks = (should_stop, getattr(current, "cancellation", None))
            for check in checks:
                if check is None:
                    continue
                status = probe_cancellation(check)
                if status.authority_failed:
                    raise RuntimeError(
                        "cancellation authority failed: " + status.error
                    )
                if status.requested:
                    return True
            return False

        clean_call_id = str(call_id or "provider-call")
        work_scope = coerce_work_scope(getattr(current, "work_scope", None))
        if chat_id:
            work_scope = work_scope.with_updates(chat_id=chat_id)
        return InvocationContext(
            chat_id=chat_id,
            run_id=run_id,
            outer_tool_call_id=clean_call_id,
            cell_execution_id=f"provider:{run_id}",
            nested_call_id=f"provider:{clean_call_id}",
            catalog_release_id=catalog_release_id,
            principal_actor_id="model",
            kernel_generation=str(record.kernel_generation),
            mount_revision=int(record.identity.mount_revision or 0),
            environment_digest=str(record.identity.environment_digest or ""),
            surface="provider",
            work_scope=work_scope,
            desktop_lock_held=bool(desktop_lock_held),
            cancellation=cancelled,
        )

    @staticmethod
    def _outer_request_fingerprint(tool_name: str, args: Mapping[str, Any]) -> str:
        return capability_request_fingerprint(tool_name, args)

    def reserve_provider_outer_call(
        self,
        *,
        tool_name: str,
        args: Mapping[str, Any],
        context: InvocationContext,
    ) -> dict[str, Any]:
        """Fence one provider call before dispatch and classify a replay."""

        repository = self.outer_call_repository
        fingerprint = self._outer_request_fingerprint(tool_name, args)
        token = {
            "chat_id": str(context.chat_id),
            "run_id": str(context.run_id),
            "call_id": str(context.outer_tool_call_id),
            "tool_name": str(tool_name),
            "request_fingerprint": fingerprint,
        }
        if repository is None:
            return {"decision": "execute", "durable": False, **token}
        record, replay = repository.reserve_outer_tool_call(**token)
        if not replay:
            return {"decision": "execute", "durable": True, **token}
        outcome = dict(record.get("outcome") or {})
        if record.get("state") in {"succeeded", "failed", "unknown_effect"}:
            return {
                "decision": "replay",
                "durable": True,
                "state": str(record.get("state") or ""),
                "outcome": outcome,
                **token,
            }

        uncertain = uncertain_outer_call_outcome(tool_name, args or {}, context.outer_tool_call_id)
        repository.finish_outer_tool_call(
            chat_id=token["chat_id"],
            run_id=token["run_id"],
            call_id=token["call_id"],
            request_fingerprint=token["request_fingerprint"],
            state="unknown_effect",
            outcome=uncertain,
            error="prior process stopped after durable dispatch",
        )
        return {
            "decision": "replay",
            "durable": True,
            "state": "unknown_effect",
            "outcome": uncertain,
            **token,
        }

    def finish_provider_outer_call(
        self,
        reservation: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        repository = self.outer_call_repository
        if repository is None or not bool(reservation.get("durable")):
            return {"state": "ephemeral", "outcome": dict(outcome or {})}
        terminal = (
            "succeeded" if bool(outcome.get("ok")) else
            "unknown_effect" if str(outcome.get("status") or "")
            == "needs_reconciliation" else "failed"
        )
        return repository.finish_outer_tool_call(
            chat_id=str(reservation.get("chat_id") or ""),
            run_id=str(reservation.get("run_id") or ""),
            call_id=str(reservation.get("call_id") or ""),
            request_fingerprint=str(
                reservation.get("request_fingerprint") or ""
            ),
            state=terminal,
            outcome=dict(outcome or {}),
            error=(
                str(outcome.get("result") or "")[:4000]
                if terminal != "succeeded" else ""
            ),
        )

    @staticmethod
    def _ledger_key(context: InvocationContext) -> str:
        return "\0".join((
            context.chat_id,
            context.run_id,
            context.cell_execution_id,
            context.nested_call_id,
        ))

    @staticmethod
    def _fingerprint(ref: CapabilityRef, arguments_sha256: str) -> str:
        return request_fingerprint("capability.invoke", {
            "ref_id": ref.opaque_id,
            "arguments_sha256": arguments_sha256,
        })

    @staticmethod
    def _effectful(metadata: CapabilityMetadata) -> bool:
        return metadata.effect_class not in {"pure", "read"}

    def _receipt_id(self, context: InvocationContext, accepted_at: str) -> str:
        seed = f"{self._ledger_key(context)}\0{accepted_at}"
        return "rcpt_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:28]

    def _emit(self, event: str, **fields: Any) -> None:
        try:
            from observability.trace_events import record_trace_event

            record_trace_event(event, **fields)
        except Exception:
            pass

    def _finalize(
        self,
        *,
        key: str,
        fingerprint: str,
        receipt: CapabilityReceipt,
    ) -> CapabilityReceipt:
        required_failures: list[str] = []
        for sink, required in tuple(self._receipt_sinks):
            try:
                sink(receipt)
            except Exception as exc:
                self._emit(
                    "broker:receipt_sink_error",
                    status="error",
                    receipt_id=receipt.receipt_id,
                    required=bool(required),
                )
                if required:
                    required_failures.append(
                        f"sink:{type(exc).__name__}: {exc}"[:500]
                    )
        effect_class = str(receipt.capability.get("effect_class") or "")
        if required_failures and effect_class not in {"pure", "read"}:
            receipt = replace(
                receipt,
                status="needs_reconciliation",
                error=CapabilityErrorInfo(
                    code="receipt_persistence_failed",
                    message=(
                        "The capability may have applied, but its mandatory "
                        "durable receipt could not be persisted. Do not retry "
                        "automatically."
                    ),
                    retryable=False,
                    may_have_applied=True,
                    cause_class="harness",
                ),
                result_metadata={
                    **dict(receipt.result_metadata or {}),
                    "receipt_persistence_failures": required_failures,
                },
                terminate=False,
                result_value=None,
            )
        with self._lock:
            self._ledger[key] = {
                "fingerprint": fingerprint,
                "receipt": receipt,
                "status": "terminal",
            }
            self._ledger.move_to_end(key)
            terminal_keys = [
                identity for identity, entry in self._ledger.items()
                if entry.get("status") == "terminal"
            ]
            for identity in terminal_keys[:-self.dedupe_capacity]:
                self._ledger.pop(identity, None)
        self._emit(
            "broker:result",
            status=receipt.status,
            call_id=receipt.attribution.get("nested_call_id"),
            outer_call_id=receipt.attribution.get("outer_tool_call_id"),
            receipt_id=receipt.receipt_id,
            capability_id=receipt.capability.get("capability_id"),
            error_code=(receipt.error.code if receipt.error else ""),
            cause_class=(receipt.error.cause_class if receipt.error else ""),
            retryable=(receipt.error.retryable if receipt.error else False),
            may_have_applied=(receipt.error.may_have_applied if receipt.error else False),
        )
        if receipt.error is not None:
            if receipt.error.code == "facility_disabled_by_user":
                self._emit(
                    "broker:facility_blocked",
                    status=receipt.status,
                    receipt_id=receipt.receipt_id,
                    capability_id=receipt.capability.get("capability_id"),
                    cause_class=receipt.error.cause_class,
                )
            if receipt.error.code == "stale_mcp_handle":
                self._emit(
                    "broker:stale_reacquisition_required",
                    status=receipt.status,
                    receipt_id=receipt.receipt_id,
                    capability_id=receipt.capability.get("capability_id"),
                    cause_class=receipt.error.cause_class,
                )
            if receipt.error.cause_class == "harness":
                self._emit(
                    "broker:harness_fault",
                    status=receipt.status,
                    receipt_id=receipt.receipt_id,
                    capability_id=receipt.capability.get("capability_id"),
                    error_code=receipt.error.code,
                )
        return receipt

    def _error_receipt(
        self,
        *,
        receipt_id: str,
        status: str,
        ref: CapabilityRef,
        metadata: CapabilityMetadata,
        arguments_sha256: str,
        context: InvocationContext,
        accepted_at: str,
        attempted_at: str = "",
        observed_at: str = "",
        code: str,
        message: str,
        retryable: bool,
        may_have_applied: bool,
        duration_ms: float,
        cause_class: str = "",
    ) -> CapabilityReceipt:
        inferred_cause_class = (
            "harness"
            if code in {
                "cancellation_authority_failed",
                "capability_handler_error",
                "receipt_persistence_failed",
            }
            else "model"
            if code in {"invalid_arguments", "duplicate_nested_call_conflict"}
            # A boolean cancellation predicate or cancelled parent task does
            # not establish who requested cancellation (e.g. bridge EOF).
            else "unknown"
            if "cancelled" in code
            else "user"
            if code == "facility_disabled_by_user"
            else "capability"
        )
        return CapabilityReceipt(
            receipt_id=receipt_id,
            status=status,
            capability={**ref.to_dict(), **metadata.to_dict()},
            arguments_sha256=arguments_sha256,
            error=CapabilityErrorInfo(
                code=code,
                message=str(message or code),
                retryable=retryable,
                may_have_applied=may_have_applied,
                cause_class=str(cause_class or inferred_cause_class),
            ),
            effect=EffectRecord(
                effect_class=metadata.effect_class,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                observed_at=observed_at,
                idempotency_key=context.idempotency_key,
            ),
            attribution=context.to_dict(),
            duration_ms=duration_ms,
        )

    async def _run_controlled(
        self,
        handler: Callable[[], Any],
        *,
        context: InvocationContext,
        deadline_ms: int,
    ) -> Any:
        task = asyncio.create_task(handler())
        deadline_at = (
            time.monotonic() + (deadline_ms / 1000.0)
            if deadline_ms > 0
            else None
        )
        pause_clock = getattr(context.user_wait, 'elapsed', lambda: 0.0)
        initial_pause = pause_clock()
        try:
            while not task.done():
                effective_deadline = deadline_at + max(0.0, pause_clock() - initial_pause) if deadline_at is not None else None
                cancellation = context.cancellation_status()
                if cancellation.authority_failed:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise _BrokerCancellationAuthorityFailed(
                        "cancellation authority failed after dispatch: "
                        + cancellation.error
                    )
                if cancellation.requested:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise _BrokerCancelled("capability cancelled after dispatch")
                if effective_deadline is not None and time.monotonic() >= effective_deadline:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise _BrokerTimedOut("capability deadline exceeded after dispatch")
                wait_for = 0.05
                if effective_deadline is not None:
                    wait_for = max(0.001, min(wait_for, effective_deadline - time.monotonic()))
                await asyncio.wait({task}, timeout=wait_for)
            if task.cancelled() and not asyncio.current_task().cancelling():
                # A service control can cancel its operation without cancelling
                # this broker/bridge task. Preserve a typed cancellation receipt.
                raise _BrokerCancelled("capability operation cancelled after dispatch")
            return await task
        except BaseException:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            raise

    async def _dispatch(self, tool: Any, args: dict, metadata: CapabilityMetadata,
                        context: InvocationContext) -> Any:
        async def run_handler():
            token = _CURRENT_INVOCATION.set(context)
            try:
                return await tool.run(args)
            finally:
                _CURRENT_INVOCATION.reset(token)

        async def controlled():
            deadline = int(context.deadline_ms or metadata.default_deadline_ms or 0)
            return await self._run_controlled(
                run_handler,
                context=context,
                deadline_ms=deadline,
            )

        if (
            metadata.touches_desktop
            and self.desktop_action_lock is not None
            and not context.desktop_lock_held
        ):
            async with self.desktop_action_lock:
                return await controlled()
        return await controlled()

    def _project_result(
        self,
        value: Any,
        *,
        receipt_id: str,
        scope: str,
    ) -> tuple[tuple[ContentBlock, ...], tuple[ArtifactRef, ...], TruncationRecord]:
        if isinstance(value, bytes):
            raw = value
            media_type = "application/octet-stream"
            kind = "capability_binary_result"
            inline_value: Any = None
        elif isinstance(value, str):
            raw = value.encode("utf-8", errors="replace")
            media_type = "text/plain; charset=utf-8"
            kind = "capability_text_result"
            inline_value = value
        else:
            inline_value = json_safe(value)
            raw = _stable_json(inline_value).encode("utf-8")
            media_type = "application/json"
            kind = "capability_json_result"

        if len(raw) <= self.inline_result_bytes and not isinstance(value, bytes):
            block = (
                ContentBlock(type="text", text=inline_value)
                if isinstance(inline_value, str)
                else ContentBlock(type="json", data=inline_value)
            )
            return (
                (block,),
                (),
                TruncationRecord(admitted_bytes=len(raw), dropped_bytes=0),
            )

        artifact: ArtifactRef | None = None
        if self.artifact_store is not None:
            artifact = self.artifact_store.put_bytes(
                raw,
                media_type=media_type,
                kind=kind,
                scope=scope,
            )
        if artifact is not None:
            block = ContentBlock(
                type="artifact_ref",
                artifact_ref=artifact.ref,
                summary=f"Full capability result retained ({len(raw)} bytes).",
            )
            return (
                (block,),
                (artifact,),
                TruncationRecord(
                    admitted_bytes=min(len(raw), self.inline_result_bytes),
                    dropped_bytes=max(0, len(raw) - self.inline_result_bytes),
                    artifact_ref=artifact.ref,
                ),
            )

        prefix = raw[:self.inline_result_bytes].decode("utf-8", errors="replace")
        return (
            (ContentBlock(type="text", text=prefix),),
            (),
            TruncationRecord(
                admitted_bytes=min(len(raw), self.inline_result_bytes),
                dropped_bytes=max(0, len(raw) - self.inline_result_bytes),
            ),
        )

    async def invoke_name(
        self,
        name: str,
        args: Mapping[str, Any] | None,
        context: InvocationContext,
    ) -> CapabilityReceipt:
        if not context.nested_call_id:
            context = replace(context, nested_call_id="nested_" + uuid.uuid4().hex)
        try:
            ref = self.ref_for_name(
                name,
                catalog_release_id=context.catalog_release_id,
                slot_id=context.slot_id,
                slot_version=context.slot_version,
            )
        except Exception as exc:
            # Preserve one structured result for direct broker callers even when
            # resolution fails before admission. Provider batches normally mark
            # unavailable calls before reaching this method.
            synthetic = CapabilityRef(
                capability_id=str(name or "unknown"),
                schema_revision="unavailable",
                handler_revision="unavailable",
                catalog_release_id=context.catalog_release_id,
            )
            return await self.invoke(synthetic, args, context)
        return await self.invoke(ref, args, context)

    async def invoke(
        self,
        ref: CapabilityRef,
        args: Mapping[str, Any] | None,
        context: InvocationContext,
    ) -> CapabilityReceipt:
        if not context.nested_call_id:
            context = replace(context, nested_call_id="nested_" + uuid.uuid4().hex)
        ownership = {}
        try:
            return await self._invoke_owned(ref, args, context, ownership)
        except BaseException as exc:
            entry = ownership.get("entry")
            key = self._ledger_key(context)
            with self._lock:
                ours = entry is not None and self._ledger.get(key) is entry
            if not ours:
                raise
            metadata = ownership.get("metadata") or CapabilityMetadata(
                capability_id=ref.capability_id, schema_revision=ref.schema_revision,
                handler_revision=ref.handler_revision, effect_class="external_side_effect",
                parallel_safe=False, idempotency="none", touches_desktop=False,
                may_return_secrets=False, default_deadline_ms=0,
            )
            attempted = str(ownership.get("attempted_at") or "")
            uncertain = bool(attempted and self._effectful(metadata))
            try:
                detail = str(exc)
            except BaseException:
                detail = "exception text unavailable"
            receipt = self._error_receipt(
                receipt_id=ownership["receipt_id"], status="needs_reconciliation" if uncertain else "error",
                ref=ref, metadata=metadata, arguments_sha256=ownership["arguments_sha"], context=context,
                accepted_at=ownership["accepted_at"], attempted_at=attempted, observed_at=_utcnow(),
                code="capability_admission_interrupted", message=f"{type(exc).__name__}: {detail}",
                retryable=False, may_have_applied=uncertain,
                duration_ms=(time.perf_counter() - ownership["started"]) * 1000, cause_class="harness",
            )
            receipt = self._finalize(key=key, fingerprint=entry["fingerprint"], receipt=receipt)
            if not isinstance(exc, Exception):
                raise
            return receipt

    async def _invoke_owned(self, ref, args, context, ownership) -> CapabilityReceipt:
        raw_args = dict(args or {})
        arguments_sha = _sha256_json(raw_args)
        key = self._ledger_key(context)
        fingerprint = self._fingerprint(ref, arguments_sha)
        accepted_at = _utcnow()
        receipt_id = self._receipt_id(context, accepted_at)
        with self._lock:
            prior = self._ledger.get(key)
            if prior is not None:
                old_receipt = prior.get("receipt")
                if prior.get("fingerprint") == fingerprint and isinstance(
                    old_receipt, CapabilityReceipt
                ):
                    self._emit(
                        "broker:dedupe_hit",
                        status="ok",
                        call_id=context.nested_call_id,
                        receipt_id=old_receipt.receipt_id,
                    )
                    return replace(old_receipt, deduplicated=True)
                metadata = CapabilityMetadata(
                    capability_id=ref.capability_id,
                    schema_revision=ref.schema_revision,
                    handler_revision=ref.handler_revision,
                    effect_class="external_side_effect",
                    parallel_safe=False,
                    idempotency="none",
                    touches_desktop=False,
                    may_return_secrets=False,
                    default_deadline_ms=0,
                )
                message = (
                    "duplicate nested call ID conflicts with an existing admission"
                    if old_receipt is not None
                    else "duplicate nested call ID is already in flight"
                )
                return self._error_receipt(
                    receipt_id=(old_receipt.receipt_id if old_receipt else receipt_id),
                    status="error",
                    ref=ref,
                    metadata=metadata,
                    arguments_sha256=arguments_sha,
                    context=context,
                    accepted_at=accepted_at,
                    observed_at=accepted_at,
                    code=(
                        "duplicate_nested_call_conflict"
                        if old_receipt is not None
                        else "duplicate_nested_call_in_flight"
                    ),
                    message=message,
                    retryable=False,
                    may_have_applied=False,
                    duration_ms=0.0,
                )
            self._ledger[key] = {
                "fingerprint": fingerprint,
                "receipt": None,
                "status": "admitted",
            }
            ownership.update(entry=self._ledger[key], receipt_id=receipt_id,
                             arguments_sha=arguments_sha, accepted_at=accepted_at,
                             started=time.perf_counter())

        started = time.perf_counter()
        self._emit(
            "broker:admitted",
            status="running",
            call_id=context.nested_call_id,
            outer_call_id=context.outer_tool_call_id,
            receipt_id=receipt_id,
            capability_id=ref.capability_id,
            arguments_sha256=arguments_sha,
        )
        try:
            tool, metadata = self._resolve(ref)
        except Exception as exc:
            metadata = CapabilityMetadata(
                capability_id=ref.capability_id,
                schema_revision=ref.schema_revision,
                handler_revision=ref.handler_revision,
                effect_class="external_side_effect",
                parallel_safe=False,
                idempotency="none",
                touches_desktop=False,
                may_return_secrets=False,
                default_deadline_ms=0,
            )
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="error",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                observed_at=_utcnow(),
                code="capability_unavailable" if ref.schema_revision == "unavailable" else "capability_resolution_error",
                message=str(exc),
                retryable=False,
                may_have_applied=False,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)

        # Resolution verifies the stable capability identity and revisions.
        # Once the operation is known, mounted-object dispatchers may refine
        # effect, idempotency, and parallelism without creating another tool.
        metadata = self.metadata_for_tool(tool, raw_args)
        ownership["metadata"] = metadata

        cancellation = context.cancellation_status()
        if cancellation.requested:
            authority_failed = cancellation.authority_failed
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="cancelled_before_start",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                observed_at=_utcnow(),
                code=(
                    "cancellation_authority_failed"
                    if authority_failed else "broker_cancelled_before_start"
                ),
                message=(
                    "cancellation authority failed before handler dispatch: "
                    + cancellation.error
                    if authority_failed
                    else "capability cancelled before handler dispatch"
                ),
                retryable=metadata.effect_class in {"pure", "read"},
                may_have_applied=False,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)

        try:
            normalized_args = tool.validate_args(raw_args)
        except Exception as exc:
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="error",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                observed_at=_utcnow(),
                code="invalid_arguments",
                message=str(exc) or "invalid capability arguments",
                retryable=True,
                may_have_applied=False,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)

        attempted_at = _utcnow()
        ownership["attempted_at"] = attempted_at
        self._emit(
            "broker:dispatched",
            status="running",
            call_id=context.nested_call_id,
            outer_call_id=context.outer_tool_call_id,
            receipt_id=receipt_id,
            capability_id=ref.capability_id,
        )
        try:
            raw_result = await self._dispatch(tool, normalized_args, metadata, context)
        except asyncio.CancelledError as exc:
            # Parent task cancellation is distinct from the broker's explicit
            # cancellation predicate, but it crosses the same uncertainty
            # boundary once dispatch began. Finalize synchronously before
            # propagating cancellation so no admission is left receiptless.
            effectful = self._effectful(metadata)
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="needs_reconciliation" if effectful else "cancelled",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                observed_at=_utcnow(),
                code="broker_parent_cancelled_after_dispatch",
                message="parent task cancelled capability after dispatch",
                retryable=not effectful,
                may_have_applied=effectful,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)
            raise exc
        except _BrokerCancellationAuthorityFailed as exc:
            effectful = self._effectful(metadata)
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="needs_reconciliation" if effectful else "cancelled",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                code="cancellation_authority_failed",
                observed_at=_utcnow(),
                message=str(exc),
                retryable=not effectful,
                may_have_applied=effectful,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)
        except _BrokerCancelled as exc:
            effectful = self._effectful(metadata)
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="needs_reconciliation" if effectful else "cancelled",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                code="broker_cancelled_after_dispatch",
                observed_at=_utcnow(),
                message=str(exc),
                retryable=not effectful,
                may_have_applied=effectful,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)
        except _BrokerTimedOut as exc:
            effectful = self._effectful(metadata)
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="needs_reconciliation" if effectful else "timed_out",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                code="broker_deadline_exceeded",
                observed_at=_utcnow(),
                message=str(exc),
                retryable=not effectful,
                may_have_applied=effectful,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)
        except Exception as exc:
            effectful = self._effectful(metadata)
            cause_class = ""
            if isinstance(exc, ToolError):
                code = str(getattr(exc, "code", "tool_error") or "tool_error")
                cause_class = str(
                    getattr(exc, "cause_class", "capability") or "capability"
                )
                if (
                    code == "tool_error"
                    and "[desktop_error:TOOL_UNAVAILABLE]" in str(exc)
                ):
                    code = "facility_disabled_by_user"
                    cause_class = "user"
            else:
                code = "capability_handler_error"
                cause_class = "harness"
            may_have_applied = bool(
                effectful
                and code not in {
                    "facility_disabled_by_user",
                    "stale_mcp_handle",
                    "stale_execution_handle",
                    "artifact_destination_exists",
                    "desktop_invalid_target",
                }
            )
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="error",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                observed_at=_utcnow(),
                code=code,
                message=str(exc) or type(exc).__name__,
                retryable=metadata.effect_class in {"pure", "read"},
                may_have_applied=may_have_applied,
                duration_ms=(time.perf_counter() - started) * 1000,
                cause_class=cause_class,
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)

        programmatic_artifact = None
        try:
            terminate = bool(getattr(raw_result, "terminate", False))
            display_value = (
                raw_result.content
                if isinstance(raw_result, ToolExecutionResult)
                else raw_result
            )
            value = (
                raw_result.programmatic_value
                if context.surface in {"ipython", "astb"}
                and isinstance(raw_result, ToolProjectionResult)
                else display_value
            )
            from tool_core import ProgrammaticArtifactPayload
            if isinstance(value, ProgrammaticArtifactPayload):
                if self.artifact_store is None:
                    raise RuntimeError("complete file artifact store is unavailable")
                saved = self.artifact_store.put_bytes(
                    value.data,
                    media_type=value.media_type,
                    kind=value.kind,
                    scope=context.chat_id,
                )
                programmatic_artifact = saved
                value = {**value.result, "artifact_ref": saved.ref, "bytes": len(value.data)}
            result_metadata = (
                dict(raw_result.receipt_metadata)
                if isinstance(raw_result, ToolProjectionResult)
                else {}
            )
            blocks, artifact_refs, truncation = self._project_result(
                display_value,
                receipt_id=receipt_id,
                scope=context.chat_id,
            )
            if programmatic_artifact is not None:
                artifact_refs = (programmatic_artifact, *artifact_refs)
                result_metadata.update({
                    "programmatic_artifact_ref": programmatic_artifact.ref,
                    "programmatic_artifact_retained": True,
                })
        except Exception as exc:
            effectful = self._effectful(metadata)
            receipt = self._error_receipt(
                receipt_id=receipt_id,
                status="needs_reconciliation" if effectful else "error",
                ref=ref,
                metadata=metadata,
                arguments_sha256=arguments_sha,
                context=context,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                observed_at=_utcnow(),
                code="capability_result_projection_failed",
                message=(
                    "The capability returned, but its result could not be retained: "
                    f"{type(exc).__name__}: {exc}"
                ),
                retryable=not effectful,
                may_have_applied=effectful,
                duration_ms=(time.perf_counter() - started) * 1000,
                cause_class="harness",
            )
            return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)
        receipt = CapabilityReceipt(
            receipt_id=receipt_id,
            status="ok",
            capability={**ref.to_dict(), **metadata.to_dict()},
            arguments_sha256=arguments_sha,
            content_blocks=blocks,
            artifact_refs=artifact_refs,
            effect=EffectRecord(
                effect_class=metadata.effect_class,
                accepted_at=accepted_at,
                attempted_at=attempted_at,
                observed_at=_utcnow(),
                idempotency_key=context.idempotency_key,
            ),
            truncation=truncation,
            attribution=context.to_dict(),
            result_metadata=result_metadata,
            duration_ms=(time.perf_counter() - started) * 1000,
            terminate=terminate,
            result_value=value,
        )
        return self._finalize(key=key, fingerprint=fingerprint, receipt=receipt)

    async def invoke_many(
        self,
        calls: Iterable[CapabilityCall],
        context: InvocationContext,
        *,
        max_concurrency: int | None = None,
    ) -> list[CapabilityReceipt]:
        rows = list(calls or ())
        if not 1 <= len(rows) <= self.max_batch_calls:
            raise ValueError(
                f"invoke_many requires 1-{self.max_batch_calls} calls"
            )
        requested = self.max_fanout if max_concurrency is None else int(max_concurrency)
        admitted = max(1, min(requested, self.max_fanout, len(rows)))
        metadata: list[CapabilityMetadata | None] = []
        for call in rows:
            try:
                tool, _ = self._resolve(call.ref)
                item = self.metadata_for_tool(tool, call.args)
            except Exception:
                item = None
            metadata.append(item)
        parallel = bool(metadata) and all(
            item is not None
            and item.effect_class in {"pure", "read"}
            and item.parallel_safe
            for item in metadata
        )
        if not parallel:
            admitted = 1
        batch_id = "batch_" + uuid.uuid4().hex[:20]
        self._emit(
            "broker:batch_start",
            status="running",
            batch_id=batch_id,
            calls=len(rows),
            requested_concurrency=requested,
            admitted_concurrency=admitted,
            parallel=parallel,
        )

        async def invoke_one(index: int, call: CapabilityCall) -> CapabilityReceipt:
            nested_id = call.nested_call_id or f"{batch_id}:{index}"
            child = replace(
                context,
                nested_call_id=nested_id,
                deadline_ms=int(call.deadline_ms or context.deadline_ms or 0),
                idempotency_key=str(call.idempotency_key or context.idempotency_key),
                batch_id=batch_id,
                batch_index=index,
            )
            return await self.invoke(call.ref, call.args, child)

        if admitted == 1:
            results = [
                await invoke_one(index, call)
                for index, call in enumerate(rows)
            ]
        else:
            semaphore = asyncio.Semaphore(admitted)

            async def bounded(index: int, call: CapabilityCall) -> CapabilityReceipt:
                async with semaphore:
                    return await invoke_one(index, call)

            results = list(await asyncio.gather(*(
                bounded(index, call) for index, call in enumerate(rows)
            )))
        self._emit(
            "broker:batch_result",
            status="ok" if all(item.ok for item in results) else "error",
            batch_id=batch_id,
            calls=len(rows),
            admitted_concurrency=admitted,
            parallel=parallel,
            failures=sum(1 for item in results if not item.ok),
        )
        return results

    def receipts(self, limit: int = 100) -> list[CapabilityReceipt]:
        cap = max(1, min(int(limit), self.dedupe_capacity))
        with self._lock:
            return [
                row["receipt"]
                for row in list(self._ledger.values())[-cap:]
                if isinstance(row.get("receipt"), CapabilityReceipt)
            ]
