"""Out-of-process Python extension worker entry point.

The worker deliberately knows nothing about AppHost or VARIANT-1's service graph.  It
loads code only from one immutable package revision and speaks correlated JSONL on
stdin/stdout.  ``server.py`` dispatches here before importing the backend when the
same frozen ``Variant1Backend`` executable is launched with ``--extension-worker``.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import traceback
from typing import Any, Mapping

from .manifests_v2 import source_manifest


PROTOCOL_SCHEMA = "variant1.extension-worker.protocol.v1"
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class WorkerRequestError(ValueError):
    pass


def _json_clone(value: Any, *, label: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerRequestError(f"{label} must be strict JSON: {exc}") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise WorkerRequestError(f"{label} exceeds {MAX_MESSAGE_BYTES} bytes")
    return json.loads(encoded)


def _object(value: Any, *, label: str) -> dict[str, Any]:
    cloned = _json_clone(value, label=label)
    if not isinstance(cloned, dict):
        raise WorkerRequestError(f"{label} must be a JSON object")
    return cloned


class _JsonlWriter:
    def __init__(self, stream=None) -> None:
        self._lock = threading.Lock()
        # Keep the protocol stream even after plugin stdout is redirected to the
        # diagnostic channel.  A plugin's ordinary ``print`` must never corrupt
        # correlated JSONL framing.
        self._stream = stream if stream is not None else sys.stdout

    def send(self, value: Mapping[str, Any]) -> None:
        payload = json.dumps(
            _object(value, label="response"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            self._stream.write(payload + "\n")
            self._stream.flush()


class ExtensionWorker:
    def __init__(self, config: Mapping[str, Any], *, protocol_reader=None, protocol_writer=None) -> None:
        self.config = _object(config, label="worker config")
        if str(self.config.get("schema") or "") != "variant1.extension-worker-launch.v1":
            raise WorkerRequestError("worker config schema is unsupported")
        self.package_digest = str(self.config.get("package_digest") or "")
        if not self.package_digest:
            raise WorkerRequestError("package_digest is required")
        self.source = Path(str(self.config.get("source") or "")).resolve()
        if not self.source.is_dir():
            raise WorkerRequestError("immutable package source is unavailable")
        _rows, actual_digest = source_manifest(self.source)
        if actual_digest != self.package_digest:
            raise WorkerRequestError("immutable package source digest changed")

        handlers = self.config.get("allowed_handlers") or []
        if not isinstance(handlers, list) or not handlers:
            raise WorkerRequestError("worker has no admitted handlers")
        self.allowed_handlers = frozenset(str(value) for value in handlers)
        if any(":" not in value for value in self.allowed_handlers):
            raise WorkerRequestError("allowed handler must be module:symbol")

        paths = self.config.get("import_paths") or []
        if not isinstance(paths, list):
            raise WorkerRequestError("import_paths must be a list")
        for value in reversed(paths):
            path = str(Path(str(value)).resolve())
            if path not in sys.path:
                sys.path.insert(0, path)
        self.entry_module = str(self.config.get("entry_module") or "").strip()
        self.maximum_concurrency = max(
            1, min(int(self.config.get("maximum_concurrency") or 4), 32)
        )
        self._handlers: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._semaphore = asyncio.Semaphore(self.maximum_concurrency)
        self._protocol_reader = protocol_reader if protocol_reader is not None else sys.stdin
        self._writer = _JsonlWriter(protocol_writer)
        sys.stdout = sys.stderr
        self._stopping = False

    def _resolve(self, handler_ref: str):
        if handler_ref not in self.allowed_handlers:
            raise WorkerRequestError("handler is not admitted by the immutable package")
        cached = self._handlers.get(handler_ref)
        if cached is not None:
            return cached
        module_name, symbol_path = handler_ref.split(":", 1)
        module = importlib.import_module(module_name)
        value: Any = module
        for segment in symbol_path.split("."):
            if not segment or segment.startswith("_"):
                raise WorkerRequestError("private or empty handler symbols are forbidden")
            value = getattr(value, segment)
        if not callable(value):
            raise WorkerRequestError("extension handler is not callable")
        self._handlers[handler_ref] = value
        return value

    async def _invoke(self, request: Mapping[str, Any]) -> None:
        correlation_id = str(request.get("id") or "")
        try:
            if str(request.get("package_digest") or "") != self.package_digest:
                raise WorkerRequestError("request package digest does not match worker")
            arguments = _object(request.get("arguments") or {}, label="arguments")
            context = _object(request.get("context") or {}, label="context")
            handler_ref = str(request.get("handler") or "")
            handler = self._resolve(handler_ref)
            async with self._semaphore:
                if inspect.iscoroutinefunction(handler):
                    result = await handler(arguments, context)
                else:
                    result = await asyncio.to_thread(handler, arguments, context)
                result = _json_clone(result, label="handler result")
            self._writer.send({
                "schema": PROTOCOL_SCHEMA,
                "type": "result",
                "id": correlation_id,
                "ok": True,
                "result": result,
            })
        except asyncio.CancelledError:
            self._writer.send({
                "schema": PROTOCOL_SCHEMA,
                "type": "result",
                "id": correlation_id,
                "ok": False,
                "error": {"code": "cancelled", "message": "invocation cancelled"},
            })
            raise
        except BaseException as exc:
            diagnostic = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-16_000:]
            print(diagnostic, file=sys.stderr, flush=True)
            self._writer.send({
                "schema": PROTOCOL_SCHEMA,
                "type": "result",
                "id": correlation_id,
                "ok": False,
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc)[:4000],
                },
            })
        finally:
            self._tasks.pop(correlation_id, None)

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def read_lines() -> None:
            try:
                for line in self._protocol_reader:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=read_lines, name="variant1-plugin-input", daemon=True).start()
        self._writer.send({
            "schema": PROTOCOL_SCHEMA,
            "type": "ready",
            "package_digest": self.package_digest,
            "pid": __import__("os").getpid(),
            "maximum_concurrency": self.maximum_concurrency,
        })

        while not self._stopping:
            line = await queue.get()
            if line is None:
                break
            if len(line.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
                print("extension worker rejected oversized JSONL request", file=sys.stderr, flush=True)
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise WorkerRequestError("request must be a JSON object")
                if str(request.get("schema") or "") != PROTOCOL_SCHEMA:
                    raise WorkerRequestError("request protocol schema is unsupported")
                operation = str(request.get("operation") or "")
                correlation_id = str(request.get("id") or "")
                if not correlation_id:
                    raise WorkerRequestError("request id is required")
                if operation == "invoke":
                    if correlation_id in self._tasks:
                        raise WorkerRequestError("request id is already active")
                    task = asyncio.create_task(
                        self._invoke(request), name=f"plugin:{correlation_id}"
                    )
                    self._tasks[correlation_id] = task
                elif operation == "cancel":
                    task = self._tasks.get(correlation_id)
                    if task is not None:
                        task.cancel()
                elif operation == "ping":
                    self._writer.send({
                        "schema": PROTOCOL_SCHEMA, "type": "pong",
                        "id": correlation_id, "ok": True,
                    })
                elif operation == "shutdown":
                    self._stopping = True
                    self._writer.send({
                        "schema": PROTOCOL_SCHEMA, "type": "shutdown",
                        "id": correlation_id, "ok": True,
                    })
                else:
                    raise WorkerRequestError("unsupported worker operation")
            except Exception as exc:
                print(f"extension worker request rejected: {exc}", file=sys.stderr, flush=True)

        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        # Move the host JSONL channel off public fds before any package code is
        # imported. Python print is diagnostic; direct os.read(0)/os.write(1)
        # from a plugin or dependency cannot consume or forge protocol frames.
        protocol_input_fd = os.dup(0)
        protocol_output_fd = os.dup(1)
        null_fd = os.open(os.devnull, os.O_RDWR)
        try:
            os.dup2(null_fd, 0)
            os.dup2(null_fd, 1)
        finally:
            os.close(null_fd)
        with os.fdopen(protocol_input_fd, "r", encoding="utf-8", errors="replace") as reader, \
                os.fdopen(protocol_output_fd, "w", encoding="utf-8", buffering=1) as writer:
            return asyncio.run(ExtensionWorker(
                config, protocol_reader=reader, protocol_writer=writer,
            ).run())
    except BaseException as exc:
        with suppress(Exception):
            print(f"extension worker startup failed: {exc}", file=sys.stderr, flush=True)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ExtensionWorker", "PROTOCOL_SCHEMA", "WorkerRequestError", "main"]
