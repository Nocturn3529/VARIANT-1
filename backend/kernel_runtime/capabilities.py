"""Scoped persistent-Python surfaces and deferred kernel control jobs."""

from __future__ import annotations

import json
from typing import Any

from capability_broker import InvocationContext, current_capability_invocation
from tools import ToolError
from work_fabric.handles import job_handle_envelope
from work_fabric.jobs import JobExecutionContext, JobResult, RetryJob
from work_fabric.scope import effective_work_scope as _scope

from .contracts import KernelContinuityError
from . import control as kernel_control


KERNEL_CHECKPOINT_JOB = "kernel.checkpoint.v1"
KERNEL_RESTART_JOB = "kernel.restart.v1"

SESSION_KERNEL_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "continuity",
        "description": (
            "Inspect this chat's effective checkpoint and automatic-recovery policy."
        ),
        "effect_class": "read",
        "params": {},
    },
    {
        "name": "configure_continuity",
        "description": (
            "Set or clear this chat's durable checkpoint and recovery override."
        ),
        "effect_class": "write",
        "params": {
            "checkpoint_enabled": {"type": "boolean", "required": False},
            "restore_on_boot": {"type": "boolean", "required": False},
            "inherit_defaults": {"type": "boolean", "required": False},
        },
    },
    {
        "name": "checkpoint",
        "description": (
            "Enqueue an explicit namespace checkpoint after the current cell is idle."
        ),
        "effect_class": "write",
        "params": {
            "reason": {"type": "string", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
    {
        "name": "restart",
        "description": "Enqueue a restart after this cell is idle, honoring the chat's continuity policy. The optional reason is an explanation, not a policy selector.",
        "effect_class": "write",
        "params": {
            "reason": {"type": "string", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
)

def _context() -> InvocationContext:
    context = current_capability_invocation()
    if context is None or not str(context.chat_id or "").strip():
        raise ToolError("kernel capabilities require an admitted chat cell")
    return context


def _manager(host: Any) -> Any:
    return kernel_control.manager(host)


def _work(host: Any) -> Any:
    runtime = getattr(host, "require_runtime", lambda: None)()
    work = getattr(runtime, "work", None)
    if work is None:
        raise ToolError("Work Fabric is unavailable")
    return work


def register_kernel_control_job_handlers(host: Any) -> None:
    """Install control jobs before the Work scheduler begins leasing."""

    async def checkpoint_kernel(execution: JobExecutionContext) -> JobResult:
        manifest = dict(execution.job.input_manifest or {})
        chat_id = str(manifest.get("runtime_chat_id") or "")
        state = kernel_control.status(host, chat_id)
        if str(state.get("state") or "") == "busy":
            raise RetryJob("kernel is still executing the admitting cell", delay_s=0.25)
        result = await _manager(host).checkpoint(
            chat_id,
            reason=str(manifest.get("reason") or "model_requested"),
        )
        artifact = host.require_runtime().session_artifacts.put_json(
            result,
            kind="kernel_control_result",
            scope=chat_id,
        )
        return JobResult(
            result_ref=str(artifact.ref),
            progress={"phase": "complete", **result},
        )

    async def restart_kernel(execution: JobExecutionContext) -> JobResult:
        chat_id = str(execution.job.input_manifest.get("runtime_chat_id") or "")
        state = kernel_control.status(host, chat_id)
        if str(state.get("state") or "") == "busy":
            raise RetryJob("kernel is still executing the admitting cell", delay_s=0.25)
        result = await kernel_control.restart(
            host, chat_id,
            reason=str(execution.job.input_manifest.get("reason") or "job_restart"),
        )
        artifact = host.require_runtime().session_artifacts.put_json(
            result,
            kind="kernel_control_result",
            scope=chat_id,
        )
        return JobResult(
            result_ref=str(artifact.ref),
            progress={"phase": "complete", **result},
        )

    work = _work(host)
    work.register_job_handler(KERNEL_CHECKPOINT_JOB, checkpoint_kernel)
    work.register_job_handler(KERNEL_RESTART_JOB, restart_kernel)


async def session_kernel_operation(
    host: Any, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch one immutable-session kernel lifecycle operation."""

    async def kernel_continuity(_args: dict[str, Any]) -> dict[str, Any]:
        context = _context()
        return _manager(host).continuity_status(str(context.chat_id))

    async def kernel_configure_continuity(
        args: dict[str, Any],
    ) -> dict[str, Any]:
        context = _context()
        try:
            return _manager(host).configure_continuity(
                str(context.chat_id),
                checkpoint_enabled=args.get("checkpoint_enabled"),
                restore_on_boot=args.get("restore_on_boot"),
                inherit_defaults=bool(args.get("inherit_defaults", False)),
                configured_by={
                    "principal_actor_id": str(context.principal_actor_id),
                    "run_id": str(context.run_id),
                    "outer_tool_call_id": str(context.outer_tool_call_id),
                    "cell_execution_id": str(context.cell_execution_id),
                    "nested_call_id": str(context.nested_call_id),
                    "kernel_generation": str(context.kernel_generation),
                },
            )
        except KernelContinuityError as exc:
            raise ToolError(json.dumps(
                {
                    "schema": "variant1.kernel-continuity-error.v1",
                    "error": exc.to_dict(),
                },
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )) from exc

    async def kernel_checkpoint(args: dict[str, Any]) -> dict[str, Any]:
        context = _context()
        scope = _scope(context)
        request_key = str(
            args.get("idempotency_key")
            or context.idempotency_key
            or context.outer_tool_call_id
            or context.nested_call_id
        ).strip()
        job = _work(host).jobs.create(
            KERNEL_CHECKPOINT_JOB,
            owner_kind="chat",
            owner_id=str(context.chat_id),
            scope=scope,
            input_manifest={
                "runtime_chat_id": str(context.chat_id),
                "reason": str(args.get("reason") or "model_requested")[:512],
            },
            retry_policy={"base_delay_s": 0.25, "max_delay_s": 2.0},
            max_attempts=120,
            idempotency_key=(
                f"kernel.checkpoint:{context.chat_id}:{request_key}"
                if request_key else ""
            ),
        )
        return job_handle_envelope(
            job, broker=host.require_runtime().broker, context=context
        )

    async def kernel_restart(args: dict[str, Any]) -> dict[str, Any]:
        context = _context()
        scope = _scope(context)
        request_key = str(
            args.get("idempotency_key")
            or context.idempotency_key
            or context.outer_tool_call_id
            or context.nested_call_id
        ).strip()
        job = _work(host).jobs.create(
            KERNEL_RESTART_JOB,
            owner_kind="chat",
            owner_id=str(context.chat_id),
            scope=scope,
            input_manifest={
                "runtime_chat_id": str(context.chat_id),
                "reason": str(args.get("reason") or "ipython_restart")[:512],
            },
            retry_policy={"base_delay_s": 0.25, "max_delay_s": 2.0},
            max_attempts=120,
            idempotency_key=(
                f"kernel.restart:{context.chat_id}:{request_key}"
                if request_key else ""
            ),
        )
        return job_handle_envelope(
            job, broker=host.require_runtime().broker, context=context
        )

    handlers = {
        "continuity": kernel_continuity,
        "configure_continuity": kernel_configure_continuity,
        "checkpoint": kernel_checkpoint,
        "restart": kernel_restart,
    }
    selected = str(operation or "")
    handler = handlers.get(selected)
    if handler is None:
        raise ToolError(f"unsupported session kernel operation: {selected}")
    return await handler(dict(args or {}))


__all__ = [
    "KERNEL_CHECKPOINT_JOB",
    "KERNEL_RESTART_JOB",
    "SESSION_KERNEL_METHODS",
    "register_kernel_control_job_handlers",
    "session_kernel_operation",
]
