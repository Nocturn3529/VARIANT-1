"""Work job handle dispatch behind VARIANT-1's CPython capability broker."""

from __future__ import annotations

from collections.abc import Mapping
import inspect
from typing import Any

from capability_broker import InvocationContext, current_capability_invocation
from tools import Tool, ToolError

from .handles import job_handle_envelope
from .models import JobRecord, WorkActor
from .scope import effective_work_scope as _effective_scope, work_scope_visible


def _context() -> InvocationContext:
    context = current_capability_invocation()
    if context is None:
        raise ToolError("Work Fabric requires an admitted capability invocation")
    return context


def _runtime(host: Any) -> Any:
    graph = getattr(host, "require_runtime", lambda: None)()
    runtime = getattr(graph, "work", None)
    if runtime is None:
        raise ToolError("Work Fabric is unavailable")
    return runtime


def _scope_allows(job: JobRecord, context: InvocationContext) -> bool:
    return work_scope_visible(job.scope, _effective_scope(context))


def _require_scoped_job(runtime: Any, context: InvocationContext, job_id: str) -> JobRecord:
    job = runtime.jobs.require(str(job_id or ""))
    if not _scope_allows(job, context):
        raise ToolError("work job is outside the current WorkScope")
    return job


def _job_handle(host: Any, context: InvocationContext, job: JobRecord) -> dict[str, Any]:
    return job_handle_envelope(
        job, broker=host.require_runtime().broker, context=context
    )


def register_work_fabric_tools(host: Any) -> None:
    """Register the hidden dispatcher used by returned durable handles."""

    registry = host.require_runtime().registry

    if registry.get("remote_handle_dispatch") is None:
        async def remote_handle_dispatch(
            args: dict[str, Any], *, control_only: bool = False,
            control_context: InvocationContext | None = None,
        ) -> Any:
            context = control_context if control_only else _context()
            identity = args.get("handle")
            arguments = args.get("arguments") or {}
            method = str(args.get("method") or "")
            if not isinstance(identity, Mapping) or not isinstance(arguments, Mapping):
                raise ToolError("remote handle dispatch requires handle and arguments objects")
            service = str(identity.get("service") or "")
            kind = str(identity.get("kind") or "")
            if service != "work":
                routers = getattr(host, "remote_handle_routers", None)
                router = routers.get(service) if isinstance(routers, Mapping) else None
                if not callable(router):
                    raise ToolError("remote handle service is unsupported")
                if control_only:
                    validator = getattr(router, "control_admission", None)
                    if not callable(validator):
                        return False
                    decision = validator(context, identity, method, dict(arguments))
                    return await decision if inspect.isawaitable(decision) else decision
                routed = router(context, identity, method, dict(arguments))
                return await routed if inspect.isawaitable(routed) else routed
            if kind != "job" or int(identity.get("generation") or -1) != 1:
                raise ToolError("remote handle kind or generation is unsupported")
            runtime = _runtime(host)
            job = _require_scoped_job(runtime, context, str(identity.get("id") or ""))
            supplied_revision = int(identity.get("revision") or -1)
            cancel_revision = method == "cancel" and 1 <= supplied_revision <= job.revision
            if method != "refresh" and supplied_revision != job.revision and not cancel_revision:
                raise ToolError(
                    f"stale work.job handle: expected revision {job.revision}, "
                    f"received {supplied_revision}; call refresh()"
                )
            if control_only:
                return method == "cancel" and not (set(arguments) - {"reason"})
            if method == "refresh":
                return _job_handle(host, context, job)
            if method in {"state", "inspect"}:
                return job.to_dict()
            if method == "cancel":
                updated = runtime.cancel_job(
                    job.job_id,
                    reason=str(arguments.get("reason") or ""),
                    expected_revision=job.revision,
                    actor=WorkActor("agent", context.run_id),
                )
                return _job_handle(host, context, updated)
            if method == "wait":
                raw_timeout = arguments.get("timeout_s")
                timeout_s = (
                    30.0 if raw_timeout is None
                    else max(0.0, min(float(raw_timeout), 30.0))
                )
                updated = (
                    job
                    if job.terminal or timeout_s == 0
                    else await runtime.jobs.wait(job.job_id, timeout_s=timeout_s)
                )
                return _job_handle(host, context, updated)
            if method == "events":
                limit = max(1, min(int(arguments.get("limit") or 100), 500))
                events = runtime.events.list(
                    after_sequence=max(0, int(arguments.get("after_sequence") or 0)),
                    limit=limit,
                    aggregate_kind="job",
                    aggregate_id=job.job_id,
                )
                return [event.to_dict() for event in events]
            raise ToolError(f"unsupported work.job method: {method}")

        registry.register(Tool(
            "remote_handle_dispatch",
            "Dispatch a versioned method against a host-owned remote handle.",
            remote_handle_dispatch,
            category="work_infrastructure",
            params={
                "handle": {"type": "object", "required": True},
                "method": {"type": "string", "required": True},
                "arguments": {"type": "object", "required": False},
            },
            hidden=True,
            visibility="broker_only",
            effect_class="external_side_effect",
            parallel_safe=False,
            idempotency="caller_key",
            may_return_secrets=False,
            schema_revision="variant1.remote-handle-dispatch.v1",
            handler_revision="variant1.remote-handle-dispatch-handler.v3",
            control_admission=lambda context, args: remote_handle_dispatch(
                args, control_only=True, control_context=context,
            ),
        ))


__all__ = [
    "register_work_fabric_tools",
]
