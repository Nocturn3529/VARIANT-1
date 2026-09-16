"""Injectable execution boundary for goal steps.

This module does not import or construct the native agent runtime.  Host
composition may inject handlers for agent/python/process/child/integration;
missing handlers return an explicit blocked result.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field, replace
import threading
from collections.abc import Callable, Mapping
from typing import Any

from .models import GoalRecord, StepAttemptRecord, StepRecord


@dataclass(frozen=True)
class StepExecutionContext:
    goal: GoalRecord
    step: StepRecord
    attempt: StepAttemptRecord
    scope: Mapping[str, Any]
    cancellation_requested: Callable[[], bool] = lambda: False


@dataclass(frozen=True)
class StepExecutionResult:
    status: str = "succeeded"
    result_ref: str = ""
    diagnostics_ref: str = ""
    error: str = ""
    wait_source: str = ""
    wait_matcher: Mapping[str, Any] = field(default_factory=dict)
    wake_at: float = 0.0
    snapshot_projection: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


StepHandler = Callable[[StepExecutionContext], Any]


class StepExecutor:
    def __init__(self, handlers: Mapping[str, StepHandler] | None = None) -> None:
        self._handlers: dict[str, StepHandler] = {}
        for kind, handler in dict(handlers or {}).items():
            self.register(kind, handler)

    def register(self, kind: str, handler: StepHandler) -> None:
        clean = str(kind or "").strip()
        if clean not in {"agent", "python", "process", "child", "integration"}:
            raise ValueError(f"{clean!r} is not an injectable goal step kind")
        if not callable(handler):
            raise TypeError("step handler must be callable")
        self._handlers[clean] = handler

    def unregister(self, kind: str) -> None:
        self._handlers.pop(str(kind), None)

    @property
    def supported_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    @staticmethod
    def _coerce(value: Any) -> StepExecutionResult:
        if value is None:
            return StepExecutionResult()
        if isinstance(value, StepExecutionResult):
            return value
        if isinstance(value, str):
            return StepExecutionResult(result_ref=value)
        if isinstance(value, Mapping):
            return StepExecutionResult(
                status=str(value.get("status") or "succeeded"),
                result_ref=str(value.get("result_ref") or ""),
                diagnostics_ref=str(value.get("diagnostics_ref") or value.get("error_ref") or ""),
                error=str(value.get("error") or ""),
                wait_source=str(value.get("wait_source") or ""),
                wait_matcher=dict(value.get("wait_matcher") or {}),
                wake_at=float(value.get("wake_at") or 0),
                snapshot_projection=dict(value.get("snapshot_projection") or {}),
                metadata=dict(value.get("metadata") or {}),
            )
        raise TypeError("goal step handler returned an unsupported result")

    async def execute(self, context: StepExecutionContext) -> StepExecutionResult:
        if context.cancellation_requested():
            return StepExecutionResult(status="cancelled", error="goal cancelled")
        handler = self._handlers.get(context.step.kind)
        if handler is None:
            return StepExecutionResult(
                status="blocked",
                error=f"unsupported_step_handler:{context.step.kind}",
                metadata={
                    "supported_handlers": list(self.supported_kinds),
                    "disclosure": "host composition has not installed this step handler",
                },
            )
        if inspect.iscoroutinefunction(handler):
            raw = await handler(context)
        else:
            cancelled = threading.Event()
            prior_stop = context.cancellation_requested
            owned = replace(context, cancellation_requested=lambda: cancelled.is_set() or prior_stop())
            worker = asyncio.create_task(asyncio.to_thread(handler, owned))
            cancellation = None
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError as exc:
                    cancelled.set()
                    cancellation = cancellation or exc
            try:
                raw = worker.result()
            except BaseException as exc:
                if cancellation is not None:
                    raise cancellation from exc
                raise
            if cancellation is not None:
                raise cancellation
            if inspect.isawaitable(raw):
                raw = await raw
        result = self._coerce(raw)
        if context.cancellation_requested() and result.status not in {
            "cancelled", "failed",
        }:
            return StepExecutionResult(status="cancelled", error="goal cancelled")
        if result.status not in {
            "succeeded", "failed", "blocked", "cancelled", "waiting", "retry_scheduled",
        }:
            raise ValueError(f"unsupported step handler status: {result.status}")
        return result


__all__ = [
    "StepExecutionContext", "StepExecutionResult", "StepExecutor", "StepHandler",
]
