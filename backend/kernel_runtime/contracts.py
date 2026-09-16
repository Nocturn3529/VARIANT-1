"""Stable host-side contracts for persistent kernel execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from contextvars import Context
from typing import Any

from core_invariants import cancellation_is_requested
from tool_core import ToolError
from work_fabric.scope import WorkScope, coerce_work_scope

from .output import OUTPUT_EVENT_SCHEMA, CellOutput, OutputLimits
from .bridge_protocol import DEFAULT_MAX_FRAME_BYTES


MAX_MODEL_ERROR_CHARS = 8_000
MAX_MODEL_TRACEBACK_LINES = 12
MAX_MODEL_STREAM_TAIL_CHARS = 2_000


class KernelUnavailable(RuntimeError):
    """The pinned kernel executable or generation could not be made ready."""


class KernelAutoRestoreError(KernelUnavailable):
    """A configured checkpoint could not be restored before model code."""

    def __init__(self, code: str, message: str, *, outcome: dict[str, Any]):
        super().__init__(str(message))
        self.code = str(code or "kernel_auto_restore_failed")
        self.outcome = dict(outcome)


class KernelContinuityError(RuntimeError):
    """A per-chat continuity policy or checkpoint request was invalid."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "kernel_continuity_error")
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class KernelLimits:
    boot_timeout_s: float = 30.0
    # Ordinary model-authored cells have no wall-clock deadline. Cancellation,
    # explicit Stop/Steer, output bounds, process death, and host shutdown are
    # still enforced independently. A positive value remains available to
    # bounded tests and explicitly configured hosts.
    cell_timeout_s: float = 0.0
    interrupt_grace_s: float = 2.0
    shutdown_grace_s: float = 3.0
    # Connection/idle protection, renewed by authenticated in-flight heartbeats;
    # never a default capability deadline or a cap on an explicit deadline.
    bridge_timeout_s: float = 120.0
    bridge_max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    # Automatic retirement is opt-in. Nonpositive values retain live state;
    # explicit close/shutdown and genuine worker failure remain independent.
    max_live_kernels: int = 0
    max_boot_concurrency: int = 2
    idle_lifetime_s: float = 0.0
    absolute_lifetime_s: float = 0.0
    # Ordinary same-user Python retains process-tree ownership without an
    # arbitrary resource ceiling. Positive host/test overrides remain opt-in.
    max_processes: int = 0
    process_memory_bytes: int = 0
    job_memory_bytes: int = 0
    cpu_percent: int = 0
    worker_stream_chunk_bytes: int = 64 * 1024
    worker_stream_cell_bytes: int = 16 * 1024 * 1024
    worker_rich_message_bytes: int = 8 * 1024 * 1024
    output: OutputLimits = field(default_factory=OutputLimits)


@dataclass(frozen=True)
class ExecutionAdmission:
    execution_id: str
    chat_id: str
    run_id: str
    outer_tool_call_id: str
    generation: int
    catalog_release_id: str
    mount_revision: int
    selected_category_id: str
    overlay_revision: int
    environment_digest: str
    workspace_root_ids: tuple[str, ...]
    retained_capability_ref_ids: tuple[str, ...] = ()
    work_scope: WorkScope = field(default_factory=WorkScope)
    namespace_document: dict[str, Any] | None = field(
        default=None, repr=False, compare=False
    )
    cancellation: Any = field(default=None, repr=False, compare=False)
    # Host-only snapshot for callbacks accepted by the long-lived bridge.
    # Never serialized or sent to the worker.
    callback_context: Context | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_scope", coerce_work_scope(self.work_scope))

    def cancelled(self) -> bool:
        return cancellation_is_requested(self.cancellation)


@dataclass
class KernelExecutionResult:
    execution_id: str
    chat_id: str
    generation: int
    status: str
    output: CellOutput
    execution_count: int = 0
    reply_status: str = ""
    error_code: str = ""
    error_message: str = ""
    duration_ms: float = 0.0
    hard_restarted: bool = False
    ledger_sequence: int = 0
    source_ref: str = ""
    result_ref: str = ""
    ledger_error: str = ""
    output_evidence_ref: str = ""
    output_evidence_sha256: str = ""
    output_evidence_bytes: int = 0
    terminate: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "variant1.kernel-execution.v1",
            "execution_id": self.execution_id,
            "chat_id": self.chat_id,
            "kernel_generation": self.generation,
            "status": self.status,
            "execution_count": self.execution_count,
            "reply_status": self.reply_status,
            "text": self.output.text(),
            "artifacts": self.output.visible_artifacts(),
            "output_evidence": {
                "schema": OUTPUT_EVENT_SCHEMA,
                "ref": self.output_evidence_ref or None,
                "sha256": self.output_evidence_sha256 or None,
                "bytes": int(self.output_evidence_bytes),
                "events": len(self.output.events),
                "dropped_bytes": int(self.output.evidence_dropped_bytes),
                "dropped_events": int(self.output.evidence_dropped_events),
                "dropped_bodies": int(self.output.evidence_dropped_bodies),
            },
            "truncation": {
                "admitted_bytes": self.output.admitted_bytes,
                "dropped_bytes": self.output.dropped_bytes,
                "admitted_events": self.output.admitted_events,
                "dropped_events": self.output.dropped_events,
                "stale_events": self.output.stale_events,
            },
            "error": (
                {
                    "code": self.error_code or "kernel_execution_error",
                    "message": self.error_message or self.output.error_value,
                }
                if not self.ok
                else None
            ),
            "duration_ms": round(float(self.duration_ms), 3),
            "hard_restarted": bool(self.hard_restarted),
            "terminate": bool(self.terminate),
            "ledger": {
                "sequence": int(self.ledger_sequence),
                "source_ref": self.source_ref or None,
                "result_ref": self.result_ref or None,
                "error": self.ledger_error or None,
            },
        }

    def render(self) -> str:
        text = self.output.text()
        # A successful cell already has its authored code and output in the
        # trajectory.  Repeating a truncated inventory of live names adds no
        # new evidence unless the runtime actually lost that namespace.
        namespace_footer = (
            self.output.namespace_footer()
            if not self.ok or self.hard_restarted else ""
        )
        runtime_note = (
            "[Kernel state] Runtime restarted during this cell; inspect any "
            "prior variable before relying on it."
            if self.hard_restarted else ""
        )
        if (
            not self.hard_restarted
            and self.status == "cancelled"
            and self.error_code == "kernel_cell_cancelled"
            and self.generation > 0
        ):
            runtime_note = (
                f"[Kernel state] CPython generation {self.generation} is still live. "
                "Statements completed before interruption are not rolled back. "
                "Reuse relevant existing variables and handles; verify partial external effects."
            )
        if self.ok:
            rendered = text or "Cell completed successfully with no output."
            return "\n".join(
                part for part in (rendered, runtime_note, namespace_footer) if part
            )
        code = self.error_code or "kernel_execution_error"
        message = self.error_message or self.output.error_value or self.status
        if code == "capability_error":
            value = str(self.output.error_value or message or "Capability failed.")
            rendered = f"ERROR capability_error: {value}".strip()
            if len(rendered) > MAX_MODEL_ERROR_CHARS:
                rendered = rendered[:MAX_MODEL_ERROR_CHARS] + "\n[diagnostic truncated]"
            return "\n".join(
                part for part in (rendered, runtime_note, namespace_footer) if part
            )
        if code == "python_exception":
            heading = "ERROR python_exception"
            exception_name = str(self.output.error_name or "PythonError")
            exception_value = str(self.output.error_value or message or "")
            if exception_name or exception_value:
                heading += ": " + ": ".join(
                    item for item in (exception_name, exception_value) if item
                )
            trace = [
                str(line)
                for line in self.output.traceback[-MAX_MODEL_TRACEBACK_LINES:]
                if str(line).strip()
            ]
            streams = "".join(
                str(chunk.get("text") or "")
                for chunk in self.output._visible_chunks()
                if str(chunk.get("kind") or "") in {"stdout", "stderr"}
            )
            parts = [heading]
            if streams.strip():
                parts.extend((
                    "Output tail:",
                    streams[-MAX_MODEL_STREAM_TAIL_CHARS:].strip(),
                ))
            if trace:
                parts.extend(("Traceback tail:", "\n".join(trace)))
            rendered = "\n".join(part for part in parts if part).strip()
            if len(rendered) > MAX_MODEL_ERROR_CHARS:
                tail_budget = max(1, MAX_MODEL_ERROR_CHARS - len(heading) - 2)
                rendered = heading + "\n" + rendered[-tail_budget:]
            return "\n".join(
                part for part in (rendered, runtime_note, namespace_footer) if part
            )
        heading = f"ERROR {code}"
        if text:
            if message and message not in text:
                heading += f": {message}"
            rendered = f"{heading}\n{text}".strip()
        else:
            rendered = f"{heading}: {message}" if message else heading
        if len(rendered) > MAX_MODEL_ERROR_CHARS:
            rendered = rendered[:MAX_MODEL_ERROR_CHARS] + "\n[diagnostic truncated]"
        return "\n".join(
            part for part in (rendered, runtime_note, namespace_footer) if part
        )

    def model_observation(self) -> str:
        """Return concise cell output while the full receipt stays host-side."""

        return self.render()


class KernelExecutionError(ToolError):
    """One cell reached a controlled non-success terminal state."""

    def __init__(self, result: KernelExecutionResult, *, context: str = "") -> None:
        code = str(result.error_code or "kernel_execution_error")
        if code == "python_exception":
            cause_class = "model"
        elif code == "kernel_cell_cancelled":
            cause_class = "user"
        elif code in {"kernel_process_died", "kernel_protocol_error", "kernel_request_error"}:
            cause_class = "harness"
        else:
            cause_class = "capability"
        super().__init__(
            "\n\n".join(part for part in (context, result.model_observation()) if part),
            code=code,
            cause_class=cause_class,
        )
        self.result = result


__all__ = [
    "ExecutionAdmission",
    "KernelAutoRestoreError",
    "KernelContinuityError",
    "KernelExecutionError",
    "KernelExecutionResult",
    "KernelLimits",
    "KernelUnavailable",
]
