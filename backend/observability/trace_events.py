"""Fail-open, vendor-neutral observability trace events for VARIANT-1.

The agent loop never depends on this module succeeding.  It records bounded,
JSON-safe event envelopes locally and can fan the same envelopes out to an
optional exporter installed by the host. Recovery state remains in native
snapshots; this file is operational evidence, not a second state store.
"""

from __future__ import annotations

import atexit
from collections import OrderedDict
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable

from run_context import current_run_context


TRACE_SCHEMA = "variant1.trace.v1"
_DEFAULT_MAX_BYTES = 32 * 1024 * 1024
_DEFAULT_KEEP_FILES = 3
_MAX_DEPTH = 5
_MAX_ITEMS = 64
_MAX_STRING = 4_000


def _falsey(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _configured_path() -> str:
    explicit = str(os.environ.get("VARIANT1_TRACE_PATH") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    data_dir = str(os.environ.get("VARIANT1_DATA_DIR") or "").strip()
    if not data_dir:
        return ""
    return os.path.join(os.path.abspath(data_dir), "data", "traces", "events.jsonl")


def _enabled_by_environment() -> bool:
    setting = os.environ.get("VARIANT1_TRACE_ENABLED")
    if setting is not None:
        return not _falsey(setting)
    # Electron/server startup sets VARIANT1_DATA_DIR.  Leaving traces disabled
    # when it is absent prevents unit tests and library imports from writing
    # into the source checkout.
    return bool(str(os.environ.get("VARIANT1_DATA_DIR") or "").strip())


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except Exception:
        return max(minimum, int(default))


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded JSON value without invoking arbitrary serializers."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if depth >= _MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["_truncated_items"] = max(0, len(value) - _MAX_ITEMS)
                break
            out[str(key)[:160]] = _safe_value(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [_safe_value(item, depth=depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out.append({"_truncated_items": len(items) - _MAX_ITEMS})
        return out
    return str(value)[:_MAX_STRING]


def _hex_id(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:length]


def _event_kind(event: str) -> str:
    prefix = str(event or "").split(":", 1)[0].strip().lower()
    return {
        "agent_graph": "graph",
        "task": "run",
        "tool": "tool",
        "model": "model",
        "context": "context",
        "checkpoint": "checkpoint",
        "memory": "memory",
        "loop": "workflow",
    }.get(prefix, "event")


def _phase(event: str, fields: dict[str, Any]) -> str:
    name = str(event or "").lower()
    status = str(fields.get("status") or "").lower()
    if name.endswith((":start", "_start", ":begin", "_begin")) or status == "running":
        return "start"
    if name.endswith((":done", "_done", ":result", "_result", ":complete", "_complete")):
        return "end"
    if status in {"ok", "error", "failed", "cancelled", "completed", "unavailable"}:
        return "end"
    return "event"


class TraceRecorder:
    """Append-only local recorder with a small optional exporter hook."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sequences: OrderedDict[str, int] = OrderedDict()
        self._sequence_capacity = 2_048
        self._exporter: Callable[[dict[str, Any]], Any] | None = None
        self._path_override: str | None = None
        self._enabled_override: bool | None = None
        self._dropped = 0
        self._queue: queue.Queue = queue.Queue(maxsize=4_096)
        self._worker: threading.Thread | None = None
        self._exporter_dropped = 0
        self._export_queue: queue.Queue = queue.Queue(maxsize=1_024)
        self._export_worker: threading.Thread | None = None

    def configure_for_tests(self, *, path: str | None, enabled: bool) -> None:
        """Override environment configuration; intended for focused tests."""
        self.flush()
        self.flush_exporters()
        with self._lock:
            self._path_override = os.path.abspath(path) if path else ""
            self._enabled_override = bool(enabled)
            self._sequences.clear()
            self._dropped = 0
            self._exporter_dropped = 0

    def reset_configuration(self) -> None:
        self.flush()
        self.flush_exporters()
        with self._lock:
            self._path_override = None
            self._enabled_override = None
            self._sequences.clear()
            self._dropped = 0
            exporter = self._exporter
            self._exporter = None
        close = getattr(exporter, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def install_exporter(self, exporter: Callable[[dict[str, Any]], Any] | None) -> None:
        """Install a fail-open sink (for example an OpenTelemetry adapter)."""
        with self._lock:
            self._exporter = exporter

    def _enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return _enabled_by_environment()

    def _path(self) -> str:
        if self._path_override is not None:
            return self._path_override
        return _configured_path()

    def _next_sequence(self, run_id: str) -> int:
        key = run_id or "unbound"
        with self._lock:
            current = int(self._sequences.pop(key, 0)) + 1
            self._sequences[key] = current
            while len(self._sequences) > self._sequence_capacity:
                self._sequences.popitem(last=False)
            return current

    def envelope(self, event: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        values = dict(fields or {})
        ctx = current_run_context()
        run_id = str(values.pop("run_id", "") or getattr(ctx, "run_id", "") or "")
        source = str(values.pop("source", "") or getattr(ctx, "source", "") or "")
        thread_id = str(values.pop("thread_id", "") or getattr(ctx, "thread_id", "") or "")
        session_id = str(
            values.pop("session_id", "")
            or getattr(ctx, "session_id", "")
            or ""
        )
        desktop_binding_id = str(
            values.get("desktop_binding_id")
            or getattr(ctx, "desktop_binding_id", "")
            or ""
        )
        if desktop_binding_id:
            values.setdefault("desktop_binding_id", desktop_binding_id)
        parent_run_id = str(
            values.pop("parent_run_id", "")
            or getattr(ctx, "parent_run_id", "")
            or ""
        )
        seq = self._next_sequence(run_id)
        event_id = "evt_" + uuid.uuid4().hex
        call_id = str(values.get("call_id") or values.get("logical_call_id") or "")
        span_seed = f"{run_id}:{_event_kind(event)}:{call_id}" if call_id else f"{run_id}:{event}:{seq}"
        trace_seed = thread_id or run_id or event_id
        return {
            "schema": TRACE_SCHEMA,
            "event_id": event_id,
            "sequence": seq,
            "timestamp": round(time.time(), 6),
            "trace_id": _hex_id(trace_seed, 32),
            "span_id": _hex_id(span_seed, 16),
            "parent_span_id": _hex_id(f"{run_id}:root", 16) if call_id else "",
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "thread_id": thread_id,
            "session_id": session_id,
            "source": source,
            "kind": _event_kind(event),
            "event": str(event or "")[:200],
            "phase": _phase(event, values),
            "status": str(values.get("status") or "")[:80],
            "attributes": _safe_value(values),
        }

    def record(self, event: str, **fields: Any) -> dict[str, Any] | None:
        """Record one event.  All failures are swallowed by design."""
        try:
            envelope = self.envelope(event, fields)
            exporter = self._exporter
            path = self._path() if self._enabled() else ""
            if exporter is None and not path:
                return envelope
            self._enqueue(path, envelope, exporter)
            return envelope
        except Exception:
            with self._lock:
                self._dropped += 1
            return None

    def _enqueue(
        self,
        path: str,
        envelope: dict[str, Any],
        exporter: Callable[[dict[str, Any]], Any] | None,
    ) -> None:
        self._ensure_worker()
        item = (path, dict(envelope), exporter)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Tracing cannot apply backpressure to inference/tool execution.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                pass
            with self._lock:
                self._dropped += 1
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                with self._lock:
                    self._dropped += 1

    def _ensure_export_worker(self) -> None:
        with self._lock:
            worker = self._export_worker
            if worker is not None and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._export_loop,
                name="variant1-trace-exporter",
                daemon=True,
            )
            self._export_worker = worker
            worker.start()

    def _enqueue_exporter(
        self,
        exporter: Callable[[dict[str, Any]], Any],
        envelope: dict[str, Any],
    ) -> None:
        self._ensure_export_worker()
        try:
            self._export_queue.put_nowait((exporter, dict(envelope)))
        except queue.Full:
            # Export mirrors may be dropped; canonical JSONL evidence must not
            # wait for or compete with a remote exporter.
            with self._lock:
                self._exporter_dropped += 1

    def _ensure_worker(self) -> None:
        with self._lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._write_loop,
                name="variant1-trace-writer",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _write_loop(self) -> None:
        while True:
            path, envelope, exporter = self._queue.get()
            try:
                if path:
                    line = (
                        json.dumps(
                            envelope,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    # This is the only canonical writer thread. It needs no
                    # producer/configuration lock while performing filesystem
                    # I/O, so model/tool event creation never waits on disk.
                    self._rotate_if_needed(
                        path,
                        len(line.encode("utf-8")),
                    )
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "a", encoding="utf-8", newline="\n") as handle:
                        handle.write(line)
            except Exception:
                with self._lock:
                    self._dropped += 1
            finally:
                # Export only after the canonical local write was attempted,
                # and on a separate bounded worker.
                if exporter is not None:
                    try:
                        self._enqueue_exporter(exporter, envelope)
                    except Exception:
                        with self._lock:
                            self._exporter_dropped += 1
                self._queue.task_done()

    def _export_loop(self) -> None:
        while True:
            exporter, envelope = self._export_queue.get()
            try:
                exporter(dict(envelope))
            except Exception:
                with self._lock:
                    self._exporter_dropped += 1
            finally:
                self._export_queue.task_done()

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait briefly for queued evidence; never raises or blocks forever."""
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        while self._queue.unfinished_tasks:
            self._ensure_worker()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def flush_exporters(self, timeout: float = 0.25) -> bool:
        """Wait briefly for optional mirrors without coupling them to JSONL."""

        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        while self._export_queue.unfinished_tasks:
            self._ensure_export_worker()
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def durability_barrier(self, timeout: float = 0.5) -> bool:
        """Boundedly drain and fsync canonical evidence at a run boundary."""

        if not self.flush(timeout):
            return False
        path = self._path() if self._enabled() else ""
        if not path or not os.path.isfile(path):
            return True
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except OSError:
            with self._lock:
                self._dropped += 1
            return False

    def _rotate_if_needed(self, path: str, incoming_bytes: int) -> None:
        max_bytes = _safe_int(
            os.environ.get("VARIANT1_TRACE_MAX_BYTES"),
            _DEFAULT_MAX_BYTES,
            minimum=1024,
        )
        keep = _safe_int(
            os.environ.get("VARIANT1_TRACE_KEEP_FILES"),
            _DEFAULT_KEEP_FILES,
            minimum=1,
        )
        try:
            current = os.path.getsize(path)
        except OSError:
            current = 0
        if current + max(0, incoming_bytes) <= max_bytes:
            return
        for index in range(keep - 1, 0, -1):
            source = f"{path}.{index}"
            target = f"{path}.{index + 1}"
            if os.path.exists(source):
                os.replace(source, target)
        if os.path.exists(path):
            os.replace(path, f"{path}.1")

    def health(self) -> dict[str, Any]:
        return {
            "schema": TRACE_SCHEMA,
            "enabled": self._enabled(),
            "path": self._path(),
            "dropped": self._dropped,
            "queued": self._queue.qsize(),
            "writer_alive": bool(self._worker and self._worker.is_alive()),
            "exporter_installed": self._exporter is not None,
            "exporter_dropped": self._exporter_dropped,
            "exporter_queued": self._export_queue.qsize(),
            "exporter_alive": bool(
                self._export_worker and self._export_worker.is_alive()
            ),
        }


RECORDER = TraceRecorder()
atexit.register(RECORDER.durability_barrier, 2.0)


def install_configured_exporter() -> bool:
    """Install the explicitly selected optional exporter, fail-open."""
    selected = str(os.environ.get("VARIANT1_TRACE_EXPORTER") or "").strip()
    if not selected:
        return False
    try:
        from observability.trace_exporters import configured_exporter

        exporter = configured_exporter(selected)
        RECORDER.install_exporter(exporter)
        return exporter is not None
    except Exception as exc:
        print(
            f"[trace] exporter disabled name={selected!r} "
            f"reason={type(exc).__name__}: {exc}",
            flush=True,
        )
        return False


def record_trace_event(event: str, **fields: Any) -> dict[str, Any] | None:
    try:
        from observability.operational_log import mirror_trace

        mirror_trace(event, fields)
    except Exception:
        pass
    return RECORDER.record(event, **fields)


def record_activity_message(message: dict[str, Any]) -> dict[str, Any] | None:
    row = dict(message or {})
    try:
        from observability.operational_log import mirror_activity

        mirror_activity(row)
    except Exception:
        pass
    event = str(row.pop("event", "activity"))
    row.pop("type", None)
    return RECORDER.record(event, **row)


def trace_durability_barrier(timeout: float = 0.5) -> bool:
    """Flush/fsync canonical local evidence at an explicit lifecycle boundary."""

    return RECORDER.durability_barrier(timeout)


def record_model_manifest(manifest: dict[str, Any]) -> dict[str, Any] | None:
    row = dict(manifest or {})
    try:
        from observability.operational_log import mirror_model_manifest

        mirror_model_manifest(row)
    except Exception:
        pass
    manifest_id = str(row.get("manifest_id") or "")
    run = row.get("run") if isinstance(row.get("run"), dict) else {}
    route = row.get("route") if isinstance(row.get("route"), dict) else {}
    generation = row.get("generation") if isinstance(row.get("generation"), dict) else {}
    tools = row.get("tools") if isinstance(row.get("tools"), dict) else {}
    budget = row.get("budget") if isinstance(row.get("budget"), dict) else {}
    return RECORDER.record(
        "model:request_manifest",
        run_id=str(run.get("run_id") or ""),
        thread_id=str(run.get("thread_id") or ""),
        session_id=str(run.get("session_id") or ""),
        logical_call_id=str(row.get("logical_call_id") or manifest_id),
        status="running",
        manifest_id=manifest_id,
        provider=route.get("provider"),
        model=route.get("model"),
        api_style=route.get("api_style"),
        attempt=row.get("attempt"),
        call_category=row.get("call_category"),
        max_output_tokens=generation.get("max_output_tokens"),
        rendered_tool_count=tools.get("rendered_count"),
        rendered_schema_sha256=tools.get("rendered_schema_sha256"),
        estimated_input_tokens=budget.get("estimated_input_tokens_lower_bound"),
        remaining_margin_tokens=budget.get("remaining_margin_tokens"),
        over_budget=budget.get("over_budget"),
        privacy=row.get("privacy"),
    )


def record_model_usage(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Record the correlated post-response usage without prompt/tool values."""
    row = dict(manifest or {})
    try:
        from observability.operational_log import mirror_model_usage

        mirror_model_usage(row)
    except Exception:
        pass
    run = row.get("run") if isinstance(row.get("run"), dict) else {}
    route = row.get("route") if isinstance(row.get("route"), dict) else {}
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    return RECORDER.record(
        "model:usage",
        run_id=str(run.get("run_id") or ""),
        thread_id=str(run.get("thread_id") or ""),
        session_id=str(run.get("session_id") or ""),
        logical_call_id=str(
            row.get("logical_call_id") or row.get("manifest_id") or ""
        ),
        status="ok",
        manifest_id=str(row.get("manifest_id") or ""),
        provider=route.get("provider"),
        model=route.get("model"),
        measurement=usage.get("measurement"),
        provider_reported=usage.get("provider_reported"),
        estimated=usage.get("estimated"),
        call_category=usage.get("call_category"),
        prompt_tokens=(
            usage.get("input_tokens")
            if usage.get("input_tokens") is not None
            else usage.get("prompt_tokens")
        ),
        completion_tokens=(
            usage.get("output_tokens")
            if usage.get("output_tokens") is not None
            else usage.get("completion_tokens")
        ),
        total_tokens=usage.get("total_tokens"),
        cached_tokens=(
            usage.get("cached_input_tokens")
            if usage.get("cached_input_tokens") is not None
            else usage.get("cached_tokens")
        ),
        reasoning_tokens=usage.get("reasoning_tokens"),
        cache_write_tokens=usage.get("cache_write_input_tokens"),
        tool_prompt_tokens=usage.get("tool_prompt_tokens"),
        cost_usd=usage.get("cost_usd"),
    )


def read_trace_events(
    *,
    path: str | None = None,
    run_id: str = "",
    thread_id: str = "",
    session_id: str = "",
    event: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Read a bounded newest-first query from local JSONL trace files.

    This is a developer/evaluator inspection surface, not deterministic replay.
    Corrupt or concurrently-written lines are skipped and never affect a run.
    """
    target = os.path.abspath(path) if path else RECORDER.health().get("path") or ""
    if not target:
        return []
    # Include events already accepted by the non-blocking writer when this is
    # called as an immediate developer/evaluator query.
    RECORDER.flush()
    wanted_run = str(run_id or "")
    wanted_thread = str(thread_id or "")
    wanted_session = str(session_id or "")
    wanted_event = str(event or "")
    cap = _safe_int(limit, 500, minimum=1)
    keep_files = _safe_int(
        os.environ.get("VARIANT1_TRACE_KEEP_FILES"),
        _DEFAULT_KEEP_FILES,
        minimum=1,
    )
    paths = [f"{target}.{index}" for index in range(keep_files, 0, -1)] + [target]
    matches: list[dict[str, Any]] = []
    for candidate in paths:
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict) or row.get("schema") != TRACE_SCHEMA:
                        continue
                    if wanted_run and str(row.get("run_id") or "") != wanted_run:
                        continue
                    if wanted_thread and str(row.get("thread_id") or "") != wanted_thread:
                        continue
                    if wanted_session and str(row.get("session_id") or "") != wanted_session:
                        continue
                    if wanted_event and str(row.get("event") or "") != wanted_event:
                        continue
                    matches.append(row)
                    if len(matches) > cap:
                        matches = matches[-cap:]
        except OSError:
            continue
    return matches[-cap:]


# Import-time configuration is side-effect-free unless the host explicitly
# selects an exporter.  File recording itself is still controlled separately.
install_configured_exporter()
