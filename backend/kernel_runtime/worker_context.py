"""Runtime-neutral state owned by one persistent VARIANT-1 CPython worker."""

from __future__ import annotations

import contextvars
import json
import sys
import types
from typing import Any, Callable, Mapping


_ExecutionAdmission = Mapping[str, Any] | None
NAMESPACE_INVENTORY_LIMIT = 500
NAMESPACE_IDENTIFIER_CHARS = 128


class WorkerContext:
    """Plain Python namespace, admission context, and worker event boundary."""

    def __init__(
        self,
        *,
        generation: int,
        runtime_profile: dict[str, Any],
        emit: Callable[[str, str | None], None] | Callable[..., None],
    ) -> None:
        self.generation = int(generation)
        self.runtime_profile = json.loads(json.dumps(runtime_profile))
        self._emit_callback = emit
        self.namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__package__": None,
            "__builtins__": __builtins__,
        }
        self.module = types.ModuleType("__main__")
        self.module.__dict__.update(self.namespace)
        # The module dictionary itself is the single durable namespace.
        self.namespace = self.module.__dict__
        sys.modules["__main__"] = self.module
        self.document: dict[str, Any] = {}
        self.protected_globals: dict[str, Any] = {}
        self.mounted_namespace_names: tuple[str, ...] = ()
        self.python_api_names: tuple[str, ...] = ()
        self.mounted_object_names: tuple[str, ...] = ()
        self.capsule_runtime: Any = None
        self.bridge: Any = None
        self._admission: contextvars.ContextVar[_ExecutionAdmission] = (
            contextvars.ContextVar(
                f"variant1_repl_admission_{self.generation}", default=None
            )
        )
        self._namespace_before: dict[str, int] | None = None
        self._baseline_names: tuple[str, ...] = tuple(sorted(self.namespace))

    def emit(self, frame_type: str, request_id: str | None = None, **fields: Any) -> None:
        self._emit_callback(str(frame_type), request_id, **fields)

    def bind_admission(
        self, admission: Mapping[str, Any]
    ) -> contextvars.Token[_ExecutionAdmission]:
        return self._admission.set(dict(admission))

    def reset_admission(
        self, token: contextvars.Token[_ExecutionAdmission]
    ) -> None:
        try:
            self._admission.reset(token)
        except (RuntimeError, ValueError):
            self._admission.set(None)

    def current_admission(self) -> dict[str, Any] | None:
        value = self._admission.get()
        return dict(value) if isinstance(value, Mapping) else None

    def current_request_id(self) -> str | None:
        admission = self.current_admission() or {}
        return str(admission.get("execution_id") or "") or None

    def install_document(self, document: dict[str, Any]) -> None:
        if self.bridge is None:
            raise RuntimeError("worker capability bridge is absent")
        from .worker_bridge import install_document

        install_document(self, self.bridge, document)

    def repair_protected_globals(self) -> None:
        if self.bridge is None:
            raise RuntimeError("worker capability bridge is absent")
        from .worker_bridge import repair_worker_namespace

        repair_worker_namespace(self, self.bridge)

    def user_namespace_identity(self) -> dict[str, int]:
        baseline = set(self._baseline_names)
        protected = set(self.protected_globals)
        excluded = baseline | protected | {"_"}
        return {
            str(name): id(value)
            for name, value in self.namespace.items()
            if (
                isinstance(name, str)
                and name
                and not name.startswith("_")
                and name not in excluded
            )
        }

    def capture_namespace_before(self) -> None:
        self._namespace_before = self.user_namespace_identity()

    def namespace_delta(self) -> dict[str, Any] | None:
        before = self._namespace_before
        self._namespace_before = None
        if not isinstance(before, dict):
            return None
        after = self.user_namespace_identity()
        updated = sorted(
            name for name, identity in after.items()
            if before.get(name) != identity
        )
        retained = sorted(
            name for name, identity in after.items()
            if before.get(name) == identity
        )
        # Host-only name metadata survives the small display projection. No
        # values/types are exported and prompt preparation never queries Python.
        eligible = [name for name in after if name.isidentifier()
                    and len(name) <= NAMESPACE_IDENTIFIER_CHARS]
        inventory = eligible[:NAMESPACE_INVENTORY_LIMIT]
        return {
            "schema": "variant1.kernel-namespace-delta.v1",
            "updated": updated[:12],
            "retained": retained[:12],
            "updated_omitted": max(0, len(updated) - 12),
            "retained_omitted": max(0, len(retained) - 12),
            "inventory": inventory,
            "inventory_omitted": max(0, len(after) - len(inventory)),
        }

    def execution_control(self, execution_id: str) -> dict[str, Any] | None:
        bridge = self.bridge
        if bridge is None or execution_id not in bridge._terminate_executions:
            return None
        return {
            "schema": "variant1.kernel-execution-control.v1",
            "terminate": True,
            "observation": bridge._terminate_executions.pop(execution_id, ""),
        }

    def resource_snapshot(self) -> dict[str, Any]:
        if self.capsule_runtime is None:
            return {
                "schema": "variant1.kernel-resource-snapshot.v1",
                "process": {},
                "namespace": {},
                "runtime_profile": dict(self.runtime_profile),
            }
        return dict(self.capsule_runtime.resource_snapshot())


__all__ = ["WorkerContext"]
