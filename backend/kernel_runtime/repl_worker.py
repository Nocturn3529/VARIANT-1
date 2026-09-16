"""Persistent CPython serve loop for one VARIANT-1 durable-chat generation."""

from __future__ import annotations

import ast
import asyncio
import base64
import codecs
import _thread
import inspect
import io
import json
import linecache
import os
import signal
import sys
import threading
import time
import traceback
from contextlib import suppress
from typing import Any, Mapping

from .repl_protocol import (
    DEFAULT_MAX_REPL_FRAME_BYTES,
    JsonLineWriter,
    REPL_PROTOCOL_SCHEMA,
    ReplProtocolError,
    decode_line,
    event_frame,
    validate_request,
)
from .worker_context import WorkerContext


_ADMISSION_SCHEMA = "variant1.kernel-execution-admission.v1"
_EOF = object()


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except Exception:
        return max(1, int(default))


class _ProtocolEmitter:
    def __init__(self, stream: io.BufferedWriter) -> None:
        self.max_frame_bytes = _positive_env_int(
            "VARIANT1_REPL_MAX_FRAME_BYTES", DEFAULT_MAX_REPL_FRAME_BYTES
        )
        self.writer = JsonLineWriter(
            stream, max_bytes=self.max_frame_bytes
        )

    def emit(
        self, frame_type: str, request_id: str | None = None, **fields: Any
    ) -> None:
        self.writer.write(event_frame(request_id, frame_type, **fields))


class _EventTextStream(io.TextIOBase):
    """Python-level stream whose ContextVar attribution follows asyncio tasks."""

    def __init__(
        self,
        context: WorkerContext,
        emitter: _ProtocolEmitter,
        kind: str,
        *,
        chunk_bytes: int,
        budget: "_CellStreamBudget",
    ) -> None:
        super().__init__()
        self.context = context
        self.emitter = emitter
        self.kind = str(kind)
        self.chunk_bytes = max(1, int(chunk_bytes))
        self.budget = budget
        self._pending = b""
        self._pending_id: str | None = None
        self._emitted_for_id = False
        self._lock = threading.RLock()

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    @property
    def buffer(self) -> "_EventBinaryStream":
        return _EventBinaryStream(self)

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        # Bootstrap already redirects fd 1/2 into the existing raw-output
        # drains. Libraries such as Playwright pass stderr to child processes;
        # exposing those descriptors preserves capture without exposing the
        # separate framed protocol pipe.
        return 2 if self.kind == "stderr" else 1

    def flush(self) -> None:
        with self._lock:
            self._flush_pending(force=True)

    def _emit_raw(self, raw: bytes) -> None:
        if not raw:
            return
        self.emitter.emit(
            self.kind,
            self._pending_id,
            text=raw.decode("utf-8", errors="replace"),
        )
        self._emitted_for_id = True

    def _flush_pending(self, *, force: bool = False) -> None:
        while self._pending:
            if len(self._pending) >= self.chunk_bytes:
                size = self._utf8_prefix_size(self._pending, self.chunk_bytes)
            elif force:
                size = len(self._pending)
            elif not self._emitted_for_id and b"\n" in self._pending:
                size = self._pending.index(b"\n") + 1
            elif self._pending.count(b"\n") >= 16:
                newline = -1
                start = 0
                for _ in range(16):
                    newline = self._pending.index(b"\n", start)
                    start = newline + 1
                size = newline + 1
            else:
                return
            raw, self._pending = self._pending[:size], self._pending[size:]
            self._emit_raw(raw)

    @staticmethod
    def _utf8_prefix_size(raw: bytes, limit: int) -> int:
        """Choose a bounded prefix ending on a complete UTF-8 code point."""

        cap = max(1, min(len(raw), int(limit)))
        candidate = raw[:cap]
        try:
            candidate.decode("utf-8", errors="strict")
            return cap
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.end == len(candidate):
                if exc.start > 0:
                    return exc.start
                # A deliberately tiny test chunk may be shorter than one code
                # point. Emit that complete point even if it exceeds the byte
                # hint; normal production chunks are 64 KiB.
                for size in range(cap + 1, min(len(raw), cap + 4) + 1):
                    try:
                        raw[:size].decode("utf-8", errors="strict")
                        return size
                    except UnicodeDecodeError:
                        continue
            # Pending text is produced by UTF-8 encoding, while binary writes
            # are normalized through replacement before entering this buffer.
            # Keep progress if a future caller violates that invariant.
            return cap

    def write(self, value: Any) -> int:
        text = str(value)
        if not text:
            return 0
        raw = text.encode("utf-8", errors="replace")
        request_id = self.context.current_request_id()
        raw = self.budget.admit(request_id, raw)
        with self._lock:
            if self._pending and request_id != self._pending_id:
                self._flush_pending(force=True)
            if request_id != self._pending_id:
                self._pending_id = request_id
                self._emitted_for_id = False
            self._pending += raw
            self._flush_pending()
        return len(text)


class _EventBinaryStream(io.RawIOBase):
    def __init__(self, owner: _EventTextStream) -> None:
        self.owner = owner

    def writable(self) -> bool:
        return True

    def write(self, value: Any) -> int:
        raw = bytes(value)
        self.owner.write(raw.decode("utf-8", errors="replace"))
        return len(raw)

    def flush(self) -> None:
        self.owner.flush()


class _CellStreamBudget:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self.request_id: str | None = None
        self.remaining = self.max_bytes
        self.marker_emitted = False
        self._lock = threading.Lock()

    def admit(self, request_id: str | None, raw: bytes) -> bytes:
        if request_id is None:
            return raw
        with self._lock:
            if request_id != self.request_id:
                self.request_id = request_id
                self.remaining = self.max_bytes
                self.marker_emitted = False
            admitted = raw[: self.remaining]
            self.remaining -= len(admitted)
            if len(admitted) < len(raw) and not self.marker_emitted:
                admitted += (
                    b"\n[worker stream output capped before REPL transport]\n"
                )
                self.marker_emitted = True
            return admitted


def _drain_raw_fd(
    fd: int,
    emitter: _ProtocolEmitter,
    kind: str,
    chunk_bytes: int,
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            raw = os.read(fd, max(256, int(chunk_bytes)))
            if not raw:
                break
            text = decoder.decode(raw)
            if text:
                emitter.emit(kind, None, text=text, unattributed=True)
        tail = decoder.decode(b"", final=True)
        if tail:
            emitter.emit(kind, None, text=tail, unattributed=True)
    except Exception as exc:
        with suppress(Exception):
            emitter.emit(
                "diagnostic",
                None,
                code="raw_stream_drain_failed",
                message=type(exc).__name__,
            )
    finally:
        with suppress(OSError):
            os.close(fd)


def _prepare_private_protocol_streams() -> tuple[
    io.BufferedReader, io.BufferedWriter, int, int
]:
    """Keep protocol handles private and isolate native standard streams."""

    control_fd = os.dup(0)
    protocol_fd = os.dup(1)
    # Replacing sys.stdin alone leaves fd 0 (and inherited child stdin)
    # attached to the control pipe. Move native stdin to EOF before any
    # model code or subprocess can read it; explicit child pipes still work.
    null_stdin = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null_stdin, 0)
    finally:
        os.close(null_stdin)
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    os.dup2(stdout_write, 1)
    os.dup2(stderr_write, 2)
    os.close(stdout_write)
    os.close(stderr_write)
    control = os.fdopen(control_fd, "rb", buffering=0)
    protocol = os.fdopen(protocol_fd, "wb", buffering=0)
    # Give Python's text wrapper the same deterministic EOF behavior.
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    return control, protocol, stdout_read, stderr_read


def _strict_copy(value: Any) -> Any:
    return json.loads(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _bounded_rich_bundle(
    data: Any,
    metadata: Any,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(data, Mapping):
        return {}, {}
    admitted: dict[str, Any] = {}
    used = 0
    omitted: list[dict[str, Any]] = []
    for index, (raw_type, value) in enumerate(data.items()):
        if index >= 32:
            omitted.append({"media_type": "*", "reason": "alternative_limit"})
            break
        media_type = str(raw_type or "")[:128]
        if not media_type:
            continue
        if isinstance(value, bytes):
            value = base64.b64encode(value).decode("ascii")
        try:
            clean = _strict_copy(value)
            size = len(
                json.dumps(
                    clean,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except Exception:
            omitted.append({"media_type": media_type, "reason": "not_json"})
            continue
        if size > max_bytes or used + size > max_bytes:
            omitted.append({
                "media_type": media_type,
                "bytes_at_least": size,
                "reason": "worker_rich_message_limit",
            })
            continue
        admitted[media_type] = clean
        used += size
    try:
        clean_metadata = _strict_copy(metadata) if isinstance(metadata, Mapping) else {}
    except Exception:
        clean_metadata = {}
    if omitted:
        marker = "[rich output omitted before REPL transport: " + ", ".join(
            str(item.get("media_type") or "output") for item in omitted
        ) + "]"
        fallback = str(admitted.get("text/plain") or "")
        admitted["text/plain"] = (fallback + "\n" + marker).strip()
        variant1 = (
            dict(clean_metadata.get("variant1"))
            if isinstance(clean_metadata.get("variant1"), dict)
            else {}
        )
        variant1["worker_omitted_mime"] = omitted
        clean_metadata["variant1"] = variant1
    return admitted, clean_metadata


def _mime_bundle(value: Any, *, max_bytes: int) -> tuple[dict[str, Any], dict[str, Any]]:
    data: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    mime_method = getattr(value, "_repr_mimebundle_", None)
    if callable(mime_method):
        rendered = mime_method()
        if isinstance(rendered, tuple) and len(rendered) == 2:
            raw_data, raw_metadata = rendered
        else:
            raw_data, raw_metadata = rendered, {}
        if isinstance(raw_data, Mapping):
            data.update(raw_data)
        if isinstance(raw_metadata, Mapping):
            metadata.update(raw_metadata)
    method_types = (
        ("text/html", "_repr_html_"),
        ("text/markdown", "_repr_markdown_"),
        ("image/svg+xml", "_repr_svg_"),
        ("image/png", "_repr_png_"),
        ("image/jpeg", "_repr_jpeg_"),
        ("application/json", "_repr_json_"),
    )
    for media_type, method_name in method_types:
        if media_type in data:
            continue
        method = getattr(value, method_name, None)
        if callable(method):
            with suppress(Exception):
                rendered = method()
                if rendered is not None:
                    data[media_type] = rendered
    try:
        data.setdefault("text/plain", repr(value))
    except Exception as exc:
        data.setdefault(
            "text/plain",
            f"<{type(value).__name__} repr failed: {type(exc).__name__}: {exc}>",
        )
    return _bounded_rich_bundle(data, metadata, max_bytes=max_bytes)


def _safe_exception_text(exc: BaseException) -> str:
    """Keep exception reporting independent of model-defined formatting."""
    try:
        value = str(exc)
    except BaseException:
        value = "<exception str() failed>"
    # Lone surrogates are valid Python strings but cannot enter strict UTF-8
    # JSONL. Preserve their spelling without letting diagnostics break a frame.
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _clean_traceback(exc: BaseException, filename: str) -> list[str]:
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        normalized = os.path.normcase(os.path.abspath(frame.filename))
        if normalized == os.path.normcase(os.path.abspath(__file__)):
            continue
        frames.append(frame)
    lines = ["Traceback (most recent call last):\n"] if frames else []
    lines.extend(traceback.format_list(frames))
    lines.extend(traceback.format_exception_only(type(exc), exc))
    return [str(line).rstrip("\n") for line in lines[-24:]]


def _compile_cell(source: str, filename: str) -> tuple[Any, bool]:
    tree = ast.parse(source, filename=filename, mode="exec")
    has_result = bool(tree.body and isinstance(tree.body[-1], ast.Expr))
    if has_result:
        expression = tree.body[-1]
        assignment = ast.Assign(
            targets=[ast.Name(id="_variant1_cell_result", ctx=ast.Store())],
            value=expression.value,
        )
        ast.copy_location(assignment, expression)
        tree.body[-1] = assignment
        ast.fix_missing_locations(tree)
    compiled = compile(
        tree,
        filename,
        "exec",
        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        dont_inherit=True,
    )
    return compiled, has_result


class _DisplayHandle:
    """Small update handle returned by the worker's native ``display``."""

    def __init__(self, display_id: str, display_function: Any) -> None:
        self.display_id = str(display_id)
        self._display = display_function

    def update(
        self,
        value: Any = None,
        *,
        raw: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._display(
            value,
            raw=raw,
            metadata=metadata,
            display_id=self.display_id,
            update=True,
        )


class ReplWorker:
    def __init__(
        self,
        *,
        control: io.BufferedReader,
        protocol: io.BufferedWriter,
        raw_stdout_fd: int,
        raw_stderr_fd: int,
    ) -> None:
        self.control = control
        self.emitter = _ProtocolEmitter(protocol)
        self.raw_stdout_fd = raw_stdout_fd
        self.raw_stderr_fd = raw_stderr_fd
        self.generation = int(os.environ["VARIANT1_KERNEL_GENERATION"])
        self.nonce = str(os.environ["VARIANT1_KERNEL_NONCE"])
        self.stream_chunk_bytes = _positive_env_int(
            "VARIANT1_KERNEL_STREAM_CHUNK_BYTES", 64 * 1024
        )
        self.rich_message_bytes = _positive_env_int(
            "VARIANT1_KERNEL_RICH_MESSAGE_BYTES", 8 * 1024 * 1024
        )
        self.stream_cell_bytes = _positive_env_int(
            "VARIANT1_KERNEL_STREAM_CELL_BYTES", 16 * 1024 * 1024
        )
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.reader_thread: threading.Thread | None = None
        self.raw_threads: list[threading.Thread] = []
        self.control_read_allowed = threading.Event()
        self.control_read_allowed.set()
        self.active_task: asyncio.Task | None = None
        self.active_id = ""
        self.in_flight: set[str] = set()
        self._interrupt_lock = threading.Lock()
        self._queued_execution_ids: set[str] = set()
        self._pending_interrupts: set[str] = set()
        self._signal_target = ""
        self._active_phase = "idle"
        self.execution_count = 0
        self.stopping = False
        self.diagnostics = str(
            os.environ.get("VARIANT1_REPL_DIAGNOSTICS") or ""
        ).strip() == "1"
        self.last_phase = "idle"
        self.context = WorkerContext(
            generation=self.generation,
            runtime_profile={},
            emit=self.emitter.emit,
        )

    def _diagnostic(self, phase: str) -> None:
        self.last_phase = str(phase)
        if self.diagnostics:
            self.emitter.emit(
                "diagnostic",
                self.context.current_request_id(),
                code="repl_phase",
                message=str(phase),
            )

    @staticmethod
    def _cell_frame(frame: Any) -> bool:
        """Return whether a signal interrupted model-authored cell code."""

        current = frame
        for _ in range(256):
            if current is None:
                return False
            filename = str(getattr(current.f_code, "co_filename", "") or "")
            if filename.startswith("<variant1-cell:"):
                return True
            current = current.f_back
        return False

    def _sigint_handler(self, _signum: int, frame: Any) -> None:
        """Route one reader-targeted SIGINT only to its exact active cell."""

        task = self.active_task
        target = self._signal_target
        if (
            not target
            or target != self.active_id
            or self._active_phase != "executing"
            or task is None
            or task.done()
        ):
            return
        try:
            running = asyncio.current_task(self.loop)
        except (RuntimeError, TypeError):
            running = None
        if running is task and self._cell_frame(frame):
            raise KeyboardInterrupt
        task.cancel()

    def _deliver_main_thread_interrupt(
        self, target_id: str, task: asyncio.Task[Any]
    ) -> None:
        """Best-effort CPython interrupt without creating another executor."""

        if target_id != self._signal_target or task is not self.active_task:
            return
        try:
            if os.name != "nt" and hasattr(signal, "pthread_kill"):
                signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
            else:
                _thread.interrupt_main()
            if self.loop is not None:
                self.loop.call_soon_threadsafe(lambda: None)
        except BaseException:
            if self.loop is not None:
                self.loop.call_soon_threadsafe(task.cancel)

    def _reader_interrupt(self, request: Mapping[str, Any]) -> None:
        """Handle targeted interrupts on the existing blocking reader thread."""

        request_id = str(request.get("id") or "")
        target_id = str(request.get("target_id") or "")
        task: asyncio.Task[Any] | None = None
        pending = False
        matched = False
        with self._interrupt_lock:
            if (
                target_id
                and target_id == self.active_id
                and self.active_task is not None
                and not self.active_task.done()
            ):
                matched = True
                if self._active_phase == "executing":
                    self._signal_target = target_id
                    task = self.active_task
            elif target_id and (
                target_id in self._queued_execution_ids
                or target_id in self.in_flight
            ):
                self._pending_interrupts.add(target_id)
                pending = True
                matched = True
        self._done(
            request_id,
            status="ok",
            result={
                "matched": matched,
                "pending": pending,
                "target_id": target_id,
            },
        )
        if task is not None:
            self._deliver_main_thread_interrupt(target_id, task)

    def _read_requests(self) -> None:
        assert self.loop is not None
        pending = bytearray()
        oversized = False
        try:
            while True:
                self.control_read_allowed.wait()
                try:
                    chunk = os.read(self.control.fileno(), 64 * 1024)
                except Exception as exc:
                    self.loop.call_soon_threadsafe(
                        self.queue.put_nowait,
                        {"_protocol_error": str(exc) or type(exc).__name__},
                    )
                    return
                if not chunk:
                    if pending:
                        self.loop.call_soon_threadsafe(
                            self.queue.put_nowait,
                            {"_protocol_error": "REPL frame ended before newline"},
                        )
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, _EOF)
                    return
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        if len(pending) > self.emitter.max_frame_bytes:
                            pending.clear()
                            oversized = True
                            self.loop.call_soon_threadsafe(
                                self.queue.put_nowait,
                                {"_protocol_error": "REPL frame is oversized"},
                            )
                        break
                    raw = bytes(pending[: newline + 1])
                    del pending[: newline + 1]
                    if oversized:
                        oversized = False
                        continue
                    try:
                        frame = decode_line(
                            raw, max_bytes=self.emitter.max_frame_bytes
                        )
                    except Exception as exc:
                        self.loop.call_soon_threadsafe(
                            self.queue.put_nowait,
                            {"_protocol_error": str(exc) or type(exc).__name__},
                        )
                        continue
                    frame_type = str(frame.get("type") or "")
                    if frame_type == "interrupt":
                        try:
                            self._reader_interrupt(validate_request(frame))
                        except Exception as exc:
                            self.loop.call_soon_threadsafe(
                                self.queue.put_nowait,
                                {"_protocol_error": str(exc) or type(exc).__name__},
                            )
                        continue
                    if frame_type == "execute":
                        try:
                            validated = validate_request(frame)
                        except Exception:
                            validated = None
                        if validated is not None:
                            with self._interrupt_lock:
                                self._queued_execution_ids.add(
                                    str(validated["id"])
                                )
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, frame)
        except BaseException as exc:
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(
                    self.queue.put_nowait,
                    {"_protocol_error": type(exc).__name__},
                )

    def _start_threads(self) -> None:
        self.reader_thread = threading.Thread(
            target=self._read_requests,
            name="variant1-repl-control",
            daemon=True,
        )
        self.reader_thread.start()
        for fd, kind in (
            (self.raw_stdout_fd, "stdout"),
            (self.raw_stderr_fd, "stderr"),
        ):
            thread = threading.Thread(
                target=_drain_raw_fd,
                args=(fd, self.emitter, kind, self.stream_chunk_bytes),
                name=f"variant1-repl-raw-{kind}",
                daemon=True,
            )
            thread.start()
            self.raw_threads.append(thread)

    def _install_runtime(self) -> None:
        from .worker_bridge import install_worker_namespace

        install_worker_namespace(self.context)
        stream_budget = _CellStreamBudget(self.stream_cell_bytes)
        stdout = _EventTextStream(
            self.context,
            self.emitter,
            "stdout",
            chunk_bytes=self.stream_chunk_bytes,
            budget=stream_budget,
        )
        stderr = _EventTextStream(
            self.context,
            self.emitter,
            "stderr",
            chunk_bytes=self.stream_chunk_bytes,
            budget=stream_budget,
        )
        sys.stdout = stdout
        sys.stderr = stderr
        sys.__stdout__ = stdout
        sys.__stderr__ = stderr

        def display(
            value: Any = None,
            *,
            raw: bool = False,
            metadata: Mapping[str, Any] | None = None,
            display_id: str | None = None,
            update: bool = False,
        ) -> Any:
            if raw and isinstance(value, Mapping):
                bundle, clean_metadata = _bounded_rich_bundle(
                    value,
                    metadata or {},
                    max_bytes=self.rich_message_bytes,
                )
            else:
                bundle, clean_metadata = _mime_bundle(
                    value, max_bytes=self.rich_message_bytes
                )
                if metadata:
                    clean_metadata.update(_strict_copy(dict(metadata)))
            self.emitter.emit(
                "update_display" if update else "display",
                self.context.current_request_id(),
                data=bundle,
                metadata=clean_metadata,
                display_id=str(display_id or ""),
            )
            if display_id and not update:
                return _DisplayHandle(str(display_id), display)
            return None

        def clear_output(*, wait: bool = False) -> None:
            self.emitter.emit(
                "clear_output",
                self.context.current_request_id(),
                wait=bool(wait),
            )

        self.context.namespace.update({
            "display": display,
            "clear_output": clear_output,
        })
        self.context.protected_globals.update({
            "display": display,
            "clear_output": clear_output,
        })
        # This process is a dedicated durable-chat worker.  Install one exact-
        # request signal router on its main thread; the reader thread sets the
        # target before raising SIGINT/interrupt_main.
        signal.signal(signal.SIGINT, self._sigint_handler)
        self.context._baseline_names = tuple(sorted(self.context.namespace))
        if self.context.capsule_runtime is not None:
            self.context.capsule_runtime.refresh_protected_names()

    def _done(
        self,
        request_id: str,
        *,
        status: str,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        payload: dict[str, Any] = {"status": str(status), **fields}
        if result is not None:
            payload["result"] = _strict_copy(result)
        if error is not None:
            payload["error"] = _strict_copy(dict(error))
        self.emitter.emit("done", request_id, **payload)

    async def _execute(self, request: dict[str, Any]) -> None:
        request_id = str(request["id"])
        source = str(request.get("code") or "")
        admission = request.get("admission")
        if (
            not isinstance(admission, dict)
            or str(admission.get("schema") or "") != _ADMISSION_SCHEMA
            or str(admission.get("execution_id") or "") != request_id
            or int(admission.get("generation") or 0) != self.generation
        ):
            self.control_read_allowed.set()
            self._done(
                request_id,
                status="error",
                error={
                    "code": "invalid_execution_admission",
                    "message": "Execution admission is absent, stale, or malformed.",
                },
            )
            return
        self.execution_count += 1
        filename = (
            f"<variant1-cell:{self.generation}:{self.execution_count}:"
            f"{request_id}>"
        )
        linecache.cache[filename] = (
            len(source),
            None,
            source.splitlines(keepends=True),
            filename,
        )
        admission_token = self.context.bind_admission(admission)
        self.context.bridge.bind_execution_origin()
        self.context.capture_namespace_before()
        status = "ok"
        error: dict[str, Any] | None = None
        try:
            compiled, has_result = _compile_cell(source, filename)
            outcome = eval(compiled, self.context.namespace, self.context.namespace)
            if inspect.isawaitable(outcome):
                self.control_read_allowed.set()
                await outcome
            value = self.context.namespace.pop("_variant1_cell_result", None)
            if has_result and value is not None:
                self.context.namespace["_"] = value
                bundle, metadata = _mime_bundle(
                    value, max_bytes=self.rich_message_bytes
                )
                self.emitter.emit(
                    "result",
                    request_id,
                    data=bundle,
                    metadata=metadata,
                    execution_count=self.execution_count,
                )
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            status = "cancelled"
            error = {
                "code": "kernel_cell_cancelled",
                "name": type(exc).__name__,
                "message": "Cell cancelled by the host.",
            }
        except BaseException as exc:
            status = "error"
            name = type(exc).__name__
            value = _safe_exception_text(exc)
            trace = _clean_traceback(exc, filename)
            self.emitter.emit(
                "error",
                request_id,
                name=name,
                message=value,
                traceback=trace,
            )
            error = {
                "code": (
                    "capability_error"
                    if name == "Variant1CapabilityError"
                    else "python_exception"
                ),
                "name": name,
                "message": value,
            }
        finally:
            if self.active_id == request_id:
                self._active_phase = "finishing"
            self._diagnostic("post:flush:start")
            with suppress(Exception):
                sys.stdout.flush()
            with suppress(Exception):
                sys.stderr.flush()
            self._diagnostic("post:repair:start")
            with suppress(Exception):
                self.context.repair_protected_globals()
            self._diagnostic("post:delta:start")
            delta = None
            with suppress(Exception):
                delta = self.context.namespace_delta()
            if delta:
                self.emitter.emit("namespace_delta", request_id, content=delta)
            control = None
            with suppress(Exception):
                control = self.context.execution_control(request_id)
            if control:
                self.emitter.emit(
                    "execution_control", request_id, content=control
                )
            self._diagnostic("post:resource:start")
            with suppress(Exception):
                self.emitter.emit(
                    "resource_snapshot",
                    request_id,
                    content=self.context.resource_snapshot(),
                )
            self._diagnostic("post:reset:start")
            self.context.bridge.reset_execution_origin()
            self.context.reset_admission(admission_token)
            self._diagnostic("post:done")
            self.control_read_allowed.set()
        self._done(
            request_id,
            status=status,
            error=error,
            execution_count=self.execution_count,
        )

    async def _control_operation(self, request: dict[str, Any]) -> None:
        request_id = str(request["id"])
        operation = str(request["type"])
        try:
            if operation == "mount":
                document = request.get("document")
                if not isinstance(document, dict):
                    raise ValueError("mount document must be an object")
                self.context.install_document(document)
                result = {"mounted": True}
            elif operation == "capsule_capture":
                result = self.context.capsule_runtime.capture(
                    request.get("payload")
                )
            elif operation == "capsule_inspect":
                result = self.context.capsule_runtime.inspect(
                    request.get("payload")
                )
            elif operation == "capsule_restore":
                payload = request.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("capsule restore payload must be an object")
                result = self.context.capsule_runtime.restore(payload)
            elif operation == "resource_snapshot":
                result = self.context.resource_snapshot()
            elif operation == "list_names":
                limit = max(1, min(int(request.get("limit") or 100), 500))
                names = sorted(self.context.user_namespace_identity())
                result = {
                    "names": names[:limit],
                    "omitted": max(0, len(names) - limit),
                }
            else:
                raise ValueError(f"unsupported control operation: {operation}")
            self._done(request_id, status="ok", result=result)
        except BaseException as exc:
            message = _safe_exception_text(exc)
            code = (
                "capsule_restore_unknown_effect"
                if "capsule_restore_unknown_effect" in message
                else "repl_control_error"
            )
            self._done(
                request_id,
                status="error",
                error={
                    "code": code,
                    "name": type(exc).__name__,
                    "message": message,
                },
            )

    async def _settle_active(self) -> None:
        task = self.active_task
        if task is None:
            return
        request_id = self.active_id
        try:
            await task
        except BaseException as exc:
            # A handler normally emits its own terminal response. Catch failures
            # in formatting/cleanup as well as cancellation before its first step.
            # If even this minimal response cannot be written, propagate out of
            # serve() so the host observes process death instead of waiting forever.
            cancelled = isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt)
            )
            message = _safe_exception_text(exc)
            self._done(
                request_id,
                status="cancelled" if cancelled else "error",
                error={
                    "code": "kernel_cell_cancelled" if cancelled else "kernel_request_error",
                    "name": type(exc).__name__,
                    "message": message or "Request cancelled before completion.",
                },
            )
        finally:
            self.control_read_allowed.set()
            with self._interrupt_lock:
                self.in_flight.discard(request_id)
                self._queued_execution_ids.discard(request_id)
                self._pending_interrupts.discard(request_id)
                if self._signal_target == request_id:
                    self._signal_target = ""
                self.active_task = None
                self.active_id = ""
                self._active_phase = "idle"

    async def serve(self) -> int:
        self.loop = asyncio.get_running_loop()
        self._start_threads()
        self._install_runtime()
        profile = dict(self.context.runtime_profile)
        self.emitter.emit(
            "ready",
            None,
            protocol=REPL_PROTOCOL_SCHEMA,
            generation=self.generation,
            nonce=self.nonce,
            python_version=sys.version.split()[0],
            runtime_profile_digest=str(profile.get("digest") or ""),
            pid=os.getpid(),
        )
        request_waiter = asyncio.create_task(self.queue.get())
        while not self.stopping:
            waiters = {request_waiter}
            if self.active_task is not None:
                waiters.add(self.active_task)
            done, _pending = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )
            if self.active_task is not None and self.active_task in done:
                await self._settle_active()
            if request_waiter not in done:
                continue
            raw = request_waiter.result()
            request_waiter = asyncio.create_task(self.queue.get())
            if raw is _EOF:
                self.stopping = True
                if self.active_task is not None:
                    self.active_task.cancel()
                    await self._settle_active()
                break
            if isinstance(raw, dict) and raw.get("_protocol_error"):
                self.emitter.emit(
                    "diagnostic",
                    None,
                    code="malformed_request",
                    message=str(raw.get("_protocol_error"))[:1000],
                )
                continue
            try:
                request = validate_request(raw)
            except ReplProtocolError as exc:
                if isinstance(raw, dict) and str(raw.get("type") or "") == "execute":
                    self.control_read_allowed.set()
                request_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
                if request_id:
                    self._done(
                        request_id,
                        status="error",
                        error={
                            "code": "repl_protocol_error",
                            "message": str(exc),
                        },
                    )
                else:
                    self.emitter.emit(
                        "diagnostic",
                        None,
                        code="repl_protocol_error",
                        message=str(exc),
                    )
                continue
            request_id = str(request["id"])
            operation = str(request["type"])
            if request_id in self.in_flight:
                if operation == "execute":
                    self.control_read_allowed.set()
                self._done(
                    request_id,
                    status="error",
                    error={
                        "code": "duplicate_request_id",
                        "message": "REPL request id is already in flight.",
                    },
                )
                continue
            if operation == "interrupt":
                target = str(request.get("target_id") or "")
                matched = bool(
                    self.active_task is not None
                    and not self.active_task.done()
                    and target == self.active_id
                )
                if matched:
                    self.active_task.cancel()
                self._done(
                    request_id,
                    status="ok",
                    result={"matched": matched, "target_id": target},
                )
                continue
            if operation == "shutdown":
                self.stopping = True
                if self.active_task is not None:
                    self.active_task.cancel()
                    await self._settle_active()
                self._done(request_id, status="ok", result={"shutdown": True})
                break
            if self.active_task is not None:
                if operation == "execute":
                    self.control_read_allowed.set()
                    with self._interrupt_lock:
                        self._queued_execution_ids.discard(request_id)
                self._done(
                    request_id,
                    status="error",
                    error={
                        "code": "repl_busy",
                        "message": "The persistent Python worker is busy.",
                    },
                )
                continue
            self.in_flight.add(request_id)
            if operation == "execute":
                signal.signal(signal.SIGINT, self._sigint_handler)
            handler = (
                self._execute(request)
                if operation == "execute"
                else self._control_operation(request)
            )
            task = asyncio.create_task(
                handler, name=f"variant1-repl:{operation}:{request_id}"
            )
            with self._interrupt_lock:
                self._queued_execution_ids.discard(request_id)
                self.active_id = request_id
                self.active_task = task
                self._active_phase = "executing"
                pending_interrupt = request_id in self._pending_interrupts
                if pending_interrupt:
                    self._pending_interrupts.discard(request_id)
                    self._signal_target = request_id
            if pending_interrupt:
                task.cancel()
        request_waiter.cancel()
        with suppress(BaseException):
            await request_waiter
        bridge = self.context.bridge
        if bridge is not None:
            with suppress(Exception):
                # The bridge opens one socket per call, so shutdown only needs
                # to clear retained task-local state.
                bridge._execution_origin.set(None)
        return 0


def _owner_watchdog() -> None:
    try:
        owner = int(os.environ.get("VARIANT1_KERNEL_PARENT_PID") or 0)
    except Exception:
        owner = 0
    if owner <= 0:
        return

    def watch() -> None:
        while True:
            time.sleep(1.0)
            alive = True
            try:
                import psutil

                alive = bool(psutil.pid_exists(owner))
            except Exception:
                try:
                    os.kill(owner, 0)
                except OSError:
                    alive = False
            if not alive:
                os._exit(71)

    threading.Thread(
        target=watch,
        name="variant1-repl-owner-watchdog",
        daemon=True,
    ).start()


def main() -> int:
    control, protocol, raw_stdout_fd, raw_stderr_fd = (
        _prepare_private_protocol_streams()
    )
    _owner_watchdog()
    worker = ReplWorker(
        control=control,
        protocol=protocol,
        raw_stdout_fd=raw_stdout_fd,
        raw_stderr_fd=raw_stderr_fd,
    )
    try:
        return asyncio.run(worker.serve())
    except KeyboardInterrupt:
        return 130


__all__ = ["ReplWorker", "main"]
