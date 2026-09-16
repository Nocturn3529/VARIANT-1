"""Sequential, single-attempt execution for native tool-call batches.

The runner validates prepared bindings, invokes each available tool once in
model order, and returns exactly one call-bound outcome for every requested
call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core_invariants import cancellation_is_requested, canonical_digest
from observability import context_lineage
from agent_types import ToolBatchResult
from desktop.catalog import DESKTOP_LOG_TOOLS as _DESKTOP_LOG_TOOLS
from run_context import current_run_context
from tool_core import CapabilityReceipt, ToolError, ToolExecutionResult


def _arguments_sha256(raw_args: dict) -> str:
    return canonical_digest(raw_args or {})


@dataclass
class ToolRunnerPorts:
    """Injected side effects for one tool-batch execution (no server imports)."""

    emit: Callable[..., Awaitable[None]]
    send_running: Callable[[str, dict, str], Awaitable[None]]
    clip: Callable[[str, int], str]
    max_result_chars: int


async def _exec_with_image_provenance(
    tool,
    raw_args,
    should_stop,
    *,
    call_id: str,
    tool_name: str,
    broker,
    invocation_context,
):
    """Run one call once with capture provenance isolated to its context."""
    try:
        from kernel_runtime.integration import bind_outer_tool_call_id
    except Exception:
        from contextlib import nullcontext
        outer_call_scope = nullcontext
    else:
        outer_call_scope = bind_outer_tool_call_id
    try:
        from desktop import service as desktop_service
        token = desktop_service.bind_image_provenance(call_id, tool_name)
    except Exception:
        desktop_service = None
        token = None
    try:
        if cancellation_is_requested(should_stop):
            return False, "cancelled", None, None
        try:
            with outer_call_scope(call_id):
                receipt = await broker.invoke_name(
                    tool_name,
                    raw_args,
                    invocation_context,
                )
                if receipt.ok:
                    return True, receipt.result_value, None, receipt
                message = receipt.error.message if receipt.error else receipt.status
                return False, message, ToolError(message), receipt
        except Exception as exc:
            return False, str(exc), exc, None
    finally:
        if desktop_service is not None and token is not None:
            desktop_service.reset_image_provenance(token)


def _desktop_args_preview(name: str, raw_args: dict) -> str:
    """Short, safe args for desktop log lines (no full trees / code)."""
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    path = args.get("path")
    if (
        str(args.get("action") or "").lower() == "drag"
        and isinstance(path, list)
        and len(path) >= 2
        and isinstance(path[0], dict)
        and isinstance(path[-1], dict)
    ):
        args.setdefault("x", path[0].get("x"))
        args.setdefault("y", path[0].get("y"))
        args.setdefault("x2", path[-1].get("x"))
        args.setdefault("y2", path[-1].get("y"))
    action = str(args.get("action") or "").lower()
    parts = []
    for key in (
        "action", "name", "title", "id", "text", "keys", "query",
        "path", "x", "y", "x2", "y2",
    ):
        if key not in args or args[key] in (None, ""):
            continue
        if key == "path" and action == "drag":
            continue
        val = args[key]
        if key == "text":
            s = " ".join(str(val).split())
            if len(s) > 40:
                s = s[:40] + "…"
            parts.append(f"{key}={s}")
        else:
            parts.append(f"{key}={val}")
    max_parts = 6 if action == "drag" else 4
    return " ".join(parts[:max_parts])


def _safe_clip(ports: ToolRunnerPorts, value, limit: int) -> str:
    try:
        return ports.clip(str(value or ""), limit)
    except Exception:
        text = " ".join(str(value or "").split())
        return text[:limit] + ("…" if len(text) > limit else "")


def _error_summary(value, limit: int = 240) -> str:
    """Return the useful diagnostic, not a long command echoed before it."""
    text = str(value or "").strip()
    if not text:
        return "unknown error"
    lines = [line.strip() for line in text.splitlines()]
    exit_line = next(
        (line for line in lines if line.lower().startswith("exit:")), "")
    detail = ""
    for marker in ("--- stderr ---", "--- stdout ---"):
        try:
            start = lines.index(marker) + 1
        except ValueError:
            continue
        detail = next((line for line in lines[start:] if line), "")
        if detail:
            break
    if not detail:
        metadata = ("$ ", "shell:", "cwd:", "exit:", "duration_s:", "---")
        detail = next(
            (line for line in lines if line and not line.lower().startswith(metadata)),
            text,
        )
    summary = " — ".join(part for part in (exit_line, detail) if part)
    summary = " ".join(summary.split())
    safe_limit = max(1, int(limit or 1))
    return summary[:safe_limit] + ("…" if len(summary) > safe_limit else "")


def _log_desktop_outcome(name: str, raw_args: dict, status: str, ms: int,
                          detail: str = "", result: str = "") -> None:
    if name not in _DESKTOP_LOG_TOOLS:
        return
    try:
        preview = _desktop_args_preview(name, raw_args)
        run_id = ""
        ctx = current_run_context()
        if ctx is not None:
            run_id = str(ctx.run_id or "")
        det = ""
        if detail:
            det = " detail=" + " ".join(str(detail).split())[:120]
        result_preview = ""
        if result:
            result_preview = " result=" + " ".join(str(result).split())[:180]
        target_preview = ""
        try:
            from desktop_fabric.binding import current_desktop_binding

            binding = current_desktop_binding()
            if binding is not None and binding.active_window_id:
                target_preview = f" window_id={binding.active_window_id[:80]!r}"
        except Exception:
            pass
        print(
            f"[desktop] tool={name} status={status} ms={ms}"
            f"{(' ' + preview) if preview else ''}"
            f" run_id={run_id or '-'}{target_preview}"
            f"{det}{result_preview}",
            flush=True,
        )
    except Exception:
        pass


async def execute_tool_batch(
    runnable: list,
    *,
    should_stop,
    ports: ToolRunnerPorts,
    broker,
    desktop_lock_held: bool = False,
) -> ToolBatchResult:
    """Execute a prepared batch once, sequentially, in model-requested order."""
    if broker is None:
        raise RuntimeError("tool execution requires CapabilityBroker")
    br = ToolBatchResult()
    results = []

    def record_outcome(
        name: str,
        args: dict,
        result: str,
        *,
        ok: bool,
        executed: bool,
        source_result: str | None = None,
        projected_result: str | None = None,
        call_id: str = "",
        error_class: str = "",
        terminate: bool = False,
        capability_receipt: CapabilityReceipt | None = None,
    ):
        before = str(result if source_result is None else source_result)
        after = str(result if projected_result is None else projected_result)
        status = str(error_class or "").strip().lower()
        if status not in {
            "cancelled", "cancelled_before_start", "timed_out",
            "needs_reconciliation", "unavailable", "invalid_arguments",
        }:
            status = "ok" if ok else "error"
        outcome = {
            "tool": name,
            "args": dict(args or {}),
            "result": str(result or ""),
            "model_result": after,
            "ok": bool(ok),
            "executed": bool(executed),
            "status": status,
            "call_id": str(call_id or ""),
            "source_chars": len(before),
            "visible_chars": len(after),
            "truncated": before != after,
            "error_class": str(error_class or ""),
            "terminate": bool(terminate),
        }
        if capability_receipt is not None:
            receipt_dict = capability_receipt.to_dict()
            outcome["capability_receipt"] = receipt_dict
            outcome["capability_status"] = capability_receipt.status
            outcome["receipt_id"] = capability_receipt.receipt_id
            br.receipts.append(receipt_dict)
        br.outcomes.append(outcome)
        try:
            context_lineage.add_current_run_item(
                kind="tool_observation",
                source="tool_result",
                trust="tool_output",
                decision="projected",
                reason="tool_execution",
                relevance="selected",
                chars_before=len(before),
                chars_after=len(after),
                bytes_before=len(before.encode("utf-8", errors="replace")),
                bytes_after=len(after.encode("utf-8", errors="replace")),
                producer=name,
            )
            if before != after:
                context_lineage.add_current_run_transform(
                    kind="tool_output_clipped",
                    reason="size_limit",
                    affected_count=1,
                    chars_before=len(before),
                    chars_after=len(after),
                    producer=name,
                    truncated=True,
                )
        except Exception:
            # Observation bookkeeping must not corrupt native tool-call pairing.
            pass
        return outcome

    def replay_outcome(value: dict) -> dict:
        outcome = dict(value or {})
        outcome["durable_replay"] = True
        br.outcomes.append(outcome)
        receipt = outcome.get("capability_receipt")
        if isinstance(receipt, dict):
            br.receipts.append(dict(receipt))
        br.executed = br.executed or bool(outcome.get("executed"))
        br.had_error = br.had_error or not bool(outcome.get("ok"))
        br.cancelled = br.cancelled or str(outcome.get("status") or "") in {
            "cancelled", "cancelled_before_start"
        }
        results.append(
            f"[{outcome.get('tool') or 'tool'}] "
            + str(outcome.get("model_result") or outcome.get("result") or "")
        )
        return outcome

    def finish_outer_call(reservation: dict | None, outcome: dict) -> None:
        if reservation is None:
            return
        try:
            broker.finish_provider_outer_call(reservation, outcome)
        except Exception as exc:
            message = (
                "The tool returned after dispatch, but VARIANT-1 could not persist "
                "its durable outer-call result. Treat the effect as needing "
                f"reconciliation and do not retry automatically. ({type(exc).__name__})"
            )
            outcome.update({
                "ok": False,
                "executed": True,
                "status": "needs_reconciliation",
                "result": message,
                "model_result": message,
                "source_chars": len(message),
                "visible_chars": len(message),
                "truncated": False,
                "error_class": "needs_reconciliation",
                "terminate": False,
                "durable_persistence_error": str(exc)[:500],
            })
            br.had_error = True
            br.terminate = False
            if results:
                results[-1] = f"[{outcome.get('tool') or 'tool'}] {message}"

    async def emit(event: str, **fields):
        ctx = current_run_context()
        if ctx is not None:
            fields.setdefault("run_id", ctx.run_id)
            fields.setdefault("source", ctx.source)
            binding_id = ctx.desktop_binding_id
            if binding_id:
                fields.setdefault("desktop_binding_id", binding_id)
        try:
            await ports.emit(event, **fields)
        except Exception:
            # Activity reporting is auxiliary to the call/result protocol.
            pass

    def call_fields(row: dict, index: int) -> tuple[dict, str, dict, str]:
        action = row.get("a") if isinstance(row, dict) else None
        action = action if isinstance(action, dict) else {}
        name = str(action.get("tool") or "unknown_tool")
        raw_args = action.get("args")
        raw_args = raw_args if isinstance(raw_args, dict) else {}
        call_id = str(action.get("id") or f"call_{index}")
        return action, name, raw_args, call_id

    async def cancel_from(start: int) -> None:
        """Synthesize one explicit result for every call not yet attempted."""
        br.cancelled = True
        for index in range(start, len(runnable)):
            _, name, raw_args, call_id = call_fields(runnable[index], index)
            message = "cancelled before execution"
            results.append(f"[{name}] {message}")
            record_outcome(
                name,
                raw_args,
                message,
                ok=False,
                executed=False,
                call_id=call_id,
                error_class="cancelled",
            )
            await emit(
                "tool:result",
                tool=name,
                call_id=call_id,
                status="cancelled",
                text=message,
            )

    for i, r in enumerate(runnable):
        if cancellation_is_requested(should_stop):
            await cancel_from(i)
            break
        a, name, raw_args, call_id = call_fields(r, i)
        tool = r.get("tool")
        status = str(r.get("status") or "ok")
        if status == "invalid_arguments":
            message = str(
                r.get("validation_error")
                or a.get("argument_error")
                or "invalid tool arguments"
            )
            results.append(f"[{name}] invalid arguments: {message}")
            record_outcome(
                name, raw_args, message, ok=False, executed=False,
                call_id=call_id, error_class="invalid_arguments",
            )
            br.had_error = True
            await emit(
                "tool:result", tool=name, status="invalid_arguments",
                call_id=call_id,
                text=_safe_clip(ports, message, 400),
            )
            continue
        if tool is None:
            message = "tool not available (unknown or disabled)"
            results.append(f"[{name}] error: {message}")
            record_outcome(
                name, raw_args, message,
                ok=False, executed=False, call_id=call_id,
                error_class="unavailable")
            br.had_error = True
            await emit(
                "tool:result",
                tool=name,
                call_id=call_id,
                status="unavailable",
                text=message,
            )
            continue
        invocation_context = None
        outer_reservation = None
        call_started = time.perf_counter()
        admission_started = call_started
        admission_ms = 0
        invocation_context = broker.context_for_provider(
            call_id=call_id,
            should_stop=should_stop,
            desktop_lock_held=desktop_lock_held,
        )
        try:
            outer_reservation = broker.reserve_provider_outer_call(
                tool_name=name,
                args=raw_args,
                context=invocation_context,
            )
        except Exception as exc:
            message = (
                "durable outer-call admission failed before execution: "
                f"{type(exc).__name__}: {exc}"
            )
            results.append(f"[{name}] error: {message}")
            record_outcome(
                name, raw_args, message, ok=False, executed=False,
                call_id=call_id, error_class="needs_reconciliation",
            )
            br.had_error = True
            await emit(
                "tool:result", tool=name, call_id=call_id,
                status="needs_reconciliation", text=message,
                admission_ms=int(
                    max(0.0, time.perf_counter() - admission_started) * 1000
                ),
            )
            continue
        if outer_reservation.get("decision") == "replay":
            replayed = replay_outcome(
                dict(outer_reservation.get("outcome") or {})
            )
            await emit(
                "tool:result", tool=name, call_id=call_id,
                status=str(replayed.get("status") or "error"),
                text=_safe_clip(
                    ports,
                    replayed.get("model_result") or replayed.get("result"),
                    400,
                ),
                durable_replay=True,
                receipt_id=str(replayed.get("receipt_id") or ""),
            )
            continue
        admission_ms = int(
            max(0.0, time.perf_counter() - admission_started) * 1000
        )
        try:
            from observability.trace_events import record_trace_event

            record_trace_event(
                "tool:admission",
                tool=name,
                call_id=call_id,
                status="ok",
                admission_ms=admission_ms,
            )
        except Exception:
            pass
        try:
            await ports.send_running(name, raw_args, call_id)
        except Exception:
            # A UI/activity failure must not prevent the requested tool call.
            pass
        t_tool = time.perf_counter()
        try:
            from observability.trace_events import record_trace_event

            record_trace_event(
                "tool:start",
                tool=name,
                call_id=call_id,
                status="running",
                args_sha256=_arguments_sha256(raw_args),
                admission_ms=admission_ms,
            )
        except Exception:
            pass
        ok, out, exc, capability_receipt = await _exec_with_image_provenance(
            tool,
            raw_args,
            should_stop,
            call_id=str(a.get("id") or f"call_{i}"),
            tool_name=name,
            broker=broker,
            invocation_context=invocation_context,
        )
        tool_ms = int(max(0.0, time.perf_counter() - t_tool) * 1000)
        if not ok and exc is None and out == "cancelled":
            before_cancel = len(br.outcomes)
            await cancel_from(i)
            if len(br.outcomes) > before_cancel:
                finish_outer_call(
                    outer_reservation, br.outcomes[before_cancel]
                )
            break
        if capability_receipt is not None and capability_receipt.status in {
            "cancelled", "cancelled_before_start",
        }:
            attempted = bool(
                capability_receipt.effect
                and capability_receipt.effect.attempted_at
            )
            message = str(out or "cancelled")
            results.append(f"[{name}] {message}")
            cancelled_outcome = record_outcome(
                name, raw_args, message, ok=False, executed=attempted,
                call_id=call_id,
                error_class="cancelled",
                capability_receipt=capability_receipt,
            )
            finish_outer_call(outer_reservation, cancelled_outcome)
            br.executed = br.executed or attempted
            br.cancelled = True
            await emit(
                "tool:result", tool=name, call_id=call_id,
                status="cancelled", duration_ms=tool_ms,
                admission_ms=admission_ms,
                total_duration_ms=int(
                    max(0.0, time.perf_counter() - call_started) * 1000
                ),
                text=_safe_clip(ports, message, 400),
                receipt_id=capability_receipt.receipt_id,
            )
            await cancel_from(i + 1)
            break
        attempted = (
            bool(
                capability_receipt.effect
                and capability_receipt.effect.attempted_at
            )
            if capability_receipt is not None
            else True
        )
        br.executed = br.executed or attempted
        if ok:
            terminate_requested = bool(
                (
                    capability_receipt is not None
                    and capability_receipt.terminate
                )
                or (isinstance(out, ToolExecutionResult) and out.terminate)
            )
            value = out.content if isinstance(out, ToolExecutionResult) else out
            source_out = str(value or "")
            out = source_out
            if len(source_out) > ports.max_result_chars:
                out = (source_out[:ports.max_result_chars]
                       + f"\n…(truncated, {len(source_out)} chars)")
            results.append(f"[{name}] {out}")
            terminal_outcome = record_outcome(
                name, raw_args, out, ok=True, executed=attempted,
                source_result=source_out, projected_result=str(out or ""),
                call_id=call_id,
                terminate=terminate_requested,
                capability_receipt=capability_receipt)
            out_preview = _safe_clip(ports, out, 180)
            _log_desktop_outcome(
                name, raw_args, "ok", tool_ms, result=out_preview)
            await emit("tool:result", tool=name, call_id=call_id, status="ok",
                       duration_ms=tool_ms,
                       admission_ms=admission_ms,
                       total_duration_ms=int(
                           max(0.0, time.perf_counter() - call_started) * 1000
                       ),
                       text=_safe_clip(ports, out, 400),
                       receipt_id=(capability_receipt.receipt_id
                                   if capability_receipt is not None else ""))
        else:
            diagnostic = _error_summary(out, 300)
            results.append(f"[{name}] error: {diagnostic}")
            terminal_outcome = record_outcome(
                name, raw_args, out, ok=False, executed=attempted,
                source_result=str(out or ""),
                projected_result=str(out or ""),
                call_id=call_id,
                error_class=(
                    capability_receipt.status
                    if capability_receipt is not None
                    and capability_receipt.status in {
                        "timed_out", "needs_reconciliation"
                    }
                    else "tool_error"
                ),
                capability_receipt=capability_receipt)
            br.had_error = True
            _log_desktop_outcome(
                name, raw_args, "error", tool_ms,
                detail=_safe_clip(ports, out, 120) if out else "",
                result=_safe_clip(ports, out, 180) if out else "",
            )
            await emit("tool:result", tool=name, call_id=call_id,
                       status=(
                           capability_receipt.status
                           if capability_receipt is not None else "error"
                       ),
                       duration_ms=tool_ms,
                       admission_ms=admission_ms,
                       total_duration_ms=int(
                           max(0.0, time.perf_counter() - call_started) * 1000
                       ),
                       text=_error_summary(out, 400),
                       receipt_id=(capability_receipt.receipt_id
                                   if capability_receipt is not None else ""))
        finish_outer_call(outer_reservation, terminal_outcome)

    br.text = "\n\n".join(results)
    br.terminate = bool(br.outcomes) and all(
        bool(outcome.get("terminate")) for outcome in br.outcomes
    )
    return br
