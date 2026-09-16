"""Durable supervisor for out-of-process executable Python extensions."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import json
import os
import signal
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Mapping

from core_invariants import canonical_json as _stable, request_fingerprint
from process_tree import (
    OwnedProcessTree,
    CREATE_SUSPENDED,
    attach_process_and_reap,
    resume_owned_process_and_reap,
    dispose_process_tree,
)
import uuid

from .manifests_v2 import source_manifest
from .packages_v2 import ExtensionPackageService
from .worker_entry import MAX_MESSAGE_BYTES, PROTOCOL_SCHEMA


_SAFE_EFFECTS = frozenset({"pure", "read"})
_EFFECTS = frozenset({"pure", "read", "write", "external_effect"})
_TERMINAL = frozenset({"succeeded", "failed", "unknown_effect"})


class PluginWorkerError(RuntimeError):
    pass


class PluginWorkerLost(PluginWorkerError):
    pass


class PluginWorkerDeadline(PluginWorkerLost):
    pass


class PluginInvocationFailed(PluginWorkerError):
    def __init__(self, message: str, *, operation_id: str = "", code: str = "") -> None:
        super().__init__(message)
        self.operation_id = operation_id
        self.code = code or type(self).__name__


class UnknownPluginEffect(PluginWorkerError):
    def __init__(self, operation_id: str, message: str = "") -> None:
        detail = message or (
            "the plugin worker was lost after dispatch; the effect is unknown and "
            "will not be replayed automatically"
        )
        super().__init__(detail)
        self.operation_id = operation_id
        self.code = "unknown_effect"


class PluginIdempotencyConflict(PluginWorkerError):
    pass


def _json_clone(value: Any, *, label: str, require_object: bool = False) -> Any:
    try:
        encoded = _stable(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginWorkerError(f"{label} must be strict JSON: {exc}") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise PluginWorkerError(f"{label} exceeds {MAX_MESSAGE_BYTES} bytes")
    cloned = json.loads(encoded)
    if require_object and not isinstance(cloned, dict):
        raise PluginWorkerError(f"{label} must be a JSON object")
    return cloned


def _schema_type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "arguments") -> None:
    """Validate the deterministic JSON-Schema subset used by built-in tools.

    Packages can still ship a richer schema for UI/discovery.  Invocation enforces
    types, required/properties, arrays, enums and scalar bounds without importing a
    third-party validator into the trusted host.
    """
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_schema_type_matches(value, str(item)) for item in expected):
            raise PluginWorkerError(f"{path} does not match any admitted JSON type")
    elif expected and not _schema_type_matches(value, str(expected)):
        raise PluginWorkerError(f"{path} must be {expected}")
    if "enum" in schema and value not in list(schema.get("enum") or []):
        raise PluginWorkerError(f"{path} is not an admitted enum value")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = {str(item) for item in schema.get("required") or []}
        missing = sorted(required.difference(value))
        if missing:
            raise PluginWorkerError(f"{path} is missing: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value).difference(properties))
            if unknown:
                raise PluginWorkerError(f"{path} has unknown keys: {', '.join(unknown)}")
        for key, child in properties.items():
            if key in value and isinstance(child, Mapping):
                _validate_json_schema(value[key], child, f"{path}.{key}")
    elif isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            _validate_json_schema(item, schema["items"], f"{path}[{index}]")
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise PluginWorkerError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise PluginWorkerError(f"{path} exceeds maxLength")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise PluginWorkerError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise PluginWorkerError(f"{path} exceeds maximum")


class _WorkerProcess:
    def __init__(
        self,
        *,
        package_digest: str,
        config_path: Path,
        command: list[str],
        cwd: str,
        generation: int,
        startup_deadline_s: float,
    ) -> None:
        self.package_digest = package_digest
        self.config_path = config_path
        self.command = list(command)
        self.cwd = cwd
        self.generation = generation
        self.startup_deadline_s = startup_deadline_s
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[Any] | None = None
        self._stderr_task: asyncio.Task[Any] | None = None
        self._diagnostics: deque[str] = deque(maxlen=100)
        self._closed = False
        self._process_job: OwnedProcessTree | None = None
        self.retire_requested = False

    @property
    def alive(self) -> bool:
        return bool(self.process is not None and self.process.returncode is None and not self._closed)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def has_pending_request(self, correlation_id: str) -> bool:
        return str(correlation_id or "") in self._pending

    @property
    def diagnostics(self) -> str:
        return "\n".join(self._diagnostics)[-16_000:]

    async def start(self) -> None:
        subprocess_module = __import__("subprocess")
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess_module, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess_module, "CREATE_NEW_PROCESS_GROUP", 0)
                | CREATE_SUSPENDED
            )
        inherited = {
            key: value for key, value in os.environ.items()
            if not any(
                marker in key.upper()
                for marker in (
                    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                    "CREDENTIAL",
                )
            )
        }
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_MESSAGE_BYTES + 2,
            cwd=self.cwd,
            env=inherited,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        try:
            self._process_job = await resume_owned_process_and_reap(
                self.process, self._process_job
            )
        except BaseException:
            await self.terminate()
            raise
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"plugin-stderr:{self.package_digest[:12]}"
        )
        assert self.process.stdout is not None
        try:
            line = await asyncio.wait_for(
                self.process.stdout.readline(), timeout=self.startup_deadline_s
            )
            if not line:
                raise PluginWorkerLost(
                    f"plugin worker exited during startup: {self.diagnostics or 'no diagnostic'}"
                )
            if len(line.rstrip(b"\r\n")) > MAX_MESSAGE_BYTES:
                raise PluginWorkerLost("plugin ready frame exceeds the protocol limit")
            ready = json.loads(line)
            if (
                not isinstance(ready, dict)
                or ready.get("type") != "ready"
                or ready.get("schema") != PROTOCOL_SCHEMA
                or str(ready.get("package_digest") or "") != self.package_digest
            ):
                raise PluginWorkerLost("plugin worker returned an invalid ready handshake")
        except BaseException:
            await self.terminate()
            raise
        self._reader_task = asyncio.create_task(
            self._read_stdout(), name=f"plugin-output:{self.package_digest[:12]}"
        )

    async def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            self._diagnostics.append(line.decode("utf-8", errors="replace").rstrip()[:4096])

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if len(line.rstrip(b"\r\n")) > MAX_MESSAGE_BYTES:
                    raise PluginWorkerLost("plugin response exceeds the protocol limit")
                value = _json_clone(json.loads(line), label="worker response", require_object=True)
                if value.get("schema") != PROTOCOL_SCHEMA:
                    raise PluginWorkerLost("invalid worker response schema")
                correlation_id = str(value.get("id") or "")
                future = self._pending.pop(correlation_id, None)
                if future is not None and not future.done():
                    future.set_result(value)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._diagnostics.append(f"invalid worker stdout: {exc}")
            self._fail_pending(PluginWorkerLost(f"plugin response stream failed: {exc}"))
        finally:
            self._closed = True
            self._fail_pending(
                PluginWorkerLost(
                    f"plugin worker response stream closed (exit={process.returncode}): "
                    f"{self.diagnostics or 'no diagnostic'}"
                )
            )
            if process.returncode is None:
                await self.terminate()

    def _fail_pending(self, error: BaseException) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    async def request(
        self, payload: Mapping[str, Any], *, deadline_s: float
    ) -> dict[str, Any]:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise PluginWorkerLost("plugin worker is not running")
        value = _json_clone(payload, label="worker request", require_object=True)
        correlation_id = str(value.get("id") or "")
        if not correlation_id or correlation_id in self._pending:
            raise PluginWorkerError("worker request id is missing or already active")
        future = asyncio.get_running_loop().create_future()
        self._pending[correlation_id] = future
        encoded = (_stable(value) + "\n").encode("utf-8")
        try:
            async with self._write_lock:
                if not self.alive:
                    raise PluginWorkerLost("plugin worker exited before dispatch")
                self.process.stdin.write(encoded)
                await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout=deadline_s)
        except asyncio.TimeoutError as exc:
            self._pending.pop(correlation_id, None)
            raise PluginWorkerDeadline("plugin invocation exceeded its deadline") from exc
        except asyncio.CancelledError:
            # The worker still owns the request even though wait_for cancelled
            # its local future. Send cancellation while that ownership is
            # addressable, before releasing it from the pending map.
            try:
                with suppress(Exception):
                    await asyncio.wait_for(
                        self.cancel_request(correlation_id), timeout=1.0,
                    )
            finally:
                self._pending.pop(correlation_id, None)
            raise
        except BaseException:
            self._pending.pop(correlation_id, None)
            raise

    async def cancel_request(self, correlation_id: str) -> bool:
        identity = str(correlation_id or "")
        if (
            not identity or identity not in self._pending
            or not self.alive or self.process is None or self.process.stdin is None
        ):
            return False
        encoded = (_stable({
            "schema": PROTOCOL_SCHEMA,
            "operation": "cancel",
            "id": identity,
        }) + "\n").encode("utf-8")
        async with self._write_lock:
            if not self.alive:
                return False
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()
        return True

    async def shutdown(self, *, deadline_s: float = 2.0) -> None:
        if not self.alive:
            await self.terminate()
            return
        correlation_id = "shutdown-" + uuid.uuid4().hex
        try:
            await self.request({
                "schema": PROTOCOL_SCHEMA,
                "operation": "shutdown",
                "id": correlation_id,
            }, deadline_s=deadline_s)
            assert self.process is not None
            await asyncio.wait_for(self.process.wait(), timeout=deadline_s)
        except BaseException:
            await self.terminate()
            return
        await self.terminate()

    async def terminate(self) -> None:
        process = self.process
        self._closed = True
        self._fail_pending(PluginWorkerLost("plugin worker was terminated"))
        if process is not None and process.returncode is None:
            if os.name == "nt" and self._process_job is not None:
                with suppress(BaseException):
                    dispose_process_tree(self._process_job, terminate=True)
                self._process_job = None
            elif os.name != "nt":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            else:
                with suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                if os.name != "nt":
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:
                    with suppress(ProcessLookupError):
                        process.kill()
                with suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=1.0)
        if self._process_job is not None:
            with suppress(BaseException):
                dispose_process_tree(self._process_job, terminate=False)
            self._process_job = None
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
                with suppress(BaseException):
                    await task


class PluginWorkerHost:
    """Lazy, multiplexed extension workers with a durable effect ledger."""

    def __init__(
        self,
        packages: ExtensionPackageService,
        database_path: str,
        *,
        maximum_concurrency: int = 8,
        worker_concurrency: int = 4,
        default_deadline_s: float = 30.0,
        startup_deadline_s: float = 10.0,
        max_read_retries: int = 1,
        command_builder: Callable[[Mapping[str, Any], Path], list[str]] | None = None,
    ) -> None:
        self.packages = packages
        self.path = os.path.abspath(database_path)
        self.root = Path(self.path).resolve().parent / "workers"
        self.root.mkdir(parents=True, exist_ok=True)
        self.maximum_concurrency = max(1, min(int(maximum_concurrency), 64))
        self.worker_concurrency = max(1, min(int(worker_concurrency), 32))
        self.default_deadline_s = max(0.1, min(float(default_deadline_s), 300.0))
        self.startup_deadline_s = max(0.5, min(float(startup_deadline_s), 60.0))
        self.max_read_retries = max(0, min(int(max_read_retries), 3))
        self.command_builder = command_builder or self._default_command
        self._lock = threading.RLock()
        self._worker_lock = asyncio.Lock()
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._workers: dict[str, _WorkerProcess] = {}
        self._inflight: dict[str, tuple[_WorkerProcess, str]] = {}
        self._cancelled_requests: set[str] = set()
        self._submitted_by_operation: dict[str, asyncio.Task[Any]] = {}
        self._submitted_by_request: dict[str, asyncio.Task[Any]] = {}
        self._active_invocations: set[asyncio.Task[Any]] = set()
        self._shutting_down = False
        self._generation = 0
        self._semaphore = asyncio.Semaphore(self.maximum_concurrency)
        self.started = False
        self.last_reconciliation: dict[str, int] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS extension_worker_operation_v2(
                  operation_id TEXT PRIMARY KEY,
                  dedupe_key TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_fingerprint TEXT NOT NULL,
                  package_id TEXT NOT NULL,
                  package_digest TEXT NOT NULL,
                  contribution_id TEXT NOT NULL,
                  handler TEXT NOT NULL,
                  effect_class TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN
                    ('prepared','dispatched','succeeded','failed','unknown_effect')),
                  arguments_json TEXT NOT NULL,
                  context_json TEXT NOT NULL,
                  result_json TEXT NOT NULL DEFAULT '',
                  error_json TEXT NOT NULL DEFAULT '',
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  worker_generation INTEGER NOT NULL DEFAULT 0,
                  prepared_at REAL NOT NULL,
                  dispatched_at REAL NOT NULL DEFAULT 0,
                  finished_at REAL NOT NULL DEFAULT 0,
                  UNIQUE(package_digest,contribution_id,dedupe_key)
                );
                CREATE INDEX IF NOT EXISTS extension_worker_operation_status_idx
                ON extension_worker_operation_v2(status,prepared_at);
                """
            )

    @staticmethod
    def _default_command(package: Mapping[str, Any], config_path: Path) -> list[str]:
        worker = dict(package.get("worker") or {})
        launcher = str(worker.get("launcher") or "")
        executable = (
            sys.executable
            if launcher == "same_variant1_backend"
            else str(worker.get("python") or sys.executable)
        )
        if not Path(executable).is_file():
            raise PluginWorkerError("immutable worker executable is unavailable")
        if bool(getattr(sys, "frozen", False)) and Path(executable).resolve() == Path(sys.executable).resolve():
            return [executable, "--extension-worker", "--config", str(config_path)]
        server = Path(__file__).resolve().parents[1] / "server.py"
        if not server.is_file():
            raise PluginWorkerError("development worker launcher server.py is unavailable")
        return [executable, str(server), "--extension-worker", "--config", str(config_path)]

    def _resolve_package(
        self,
        package_id: str,
        contribution_id: str,
        chat_id: str,
        contribution_kind: str = "capabilities",
    ) -> dict[str, Any]:
        contributions = self.packages.list_contributions(package_id, chat_id=chat_id)
        matches = [
            item for item in contributions
            if item.get("kind") == contribution_kind and item.get("id") == contribution_id
        ]
        if len(matches) != 1:
            raise LookupError(
                f"unknown executable {contribution_kind} contribution"
            )
        selected = matches[0]
        digest = str(selected.get("package_digest") or "")
        if not digest or digest.startswith("dev:"):
            raise PluginWorkerError(
                "development mounts are not executable; promote an immutable package first"
            )
        package = self.packages.inspect(package_id, digest=digest)
        descriptor = dict(selected.get("descriptor") or {})
        handler = str(descriptor.get("handler") or "")
        effect_class = str(descriptor.get("effect_class") or "read")
        if ":" not in handler or effect_class not in _EFFECTS:
            raise PluginWorkerError(
                f"{contribution_kind} contribution has an invalid executable contract"
            )
        _files, actual_digest = source_manifest(str(package["source_path"]))
        if actual_digest != digest:
            raise PluginWorkerError("immutable extension package digest changed on disk")
        allowed = sorted({
            str(dict(item.get("descriptor") or {}).get("handler") or "")
            for item in package.get("contributions") or []
            if item.get("kind") == contribution_kind
        })
        allowed = [item for item in allowed if ":" in item]
        if handler not in allowed:
            raise PluginWorkerError("handler is absent from the immutable worker manifest")
        package["selected"] = selected
        package["handler"] = handler
        package["effect_class"] = effect_class
        package["allowed_handlers"] = allowed
        package["contribution_kind"] = contribution_kind
        return package

    def _validate_arguments(self, package: Mapping[str, Any], arguments: dict[str, Any]) -> None:
        descriptor = dict(dict(package.get("selected") or {}).get("descriptor") or {})
        relative = str(descriptor.get("input_schema") or "")
        if not relative:
            return
        source = Path(str(package.get("source_path") or "")).resolve()
        target = (source / relative).resolve()
        try:
            target.relative_to(source)
        except ValueError as exc:
            raise PluginWorkerError("input schema escapes immutable package") from exc
        raw = target.read_bytes()
        expected = str(descriptor.get("input_schema_sha256") or "")
        if not expected or hashlib.sha256(raw).hexdigest() != expected:
            raise PluginWorkerError("immutable input schema digest changed")
        schema = json.loads(raw)
        if not isinstance(schema, Mapping):
            raise PluginWorkerError("input schema must be a JSON object")
        _validate_json_schema(arguments, schema)

    def _write_config(self, package: Mapping[str, Any]) -> Path:
        digest = str(package["package_digest"])
        source = Path(str(package["source_path"])).resolve()
        package_root = source.parent.resolve()
        worker = dict(package.get("worker") or {})
        raw_paths = list(worker.get("import_paths") or [])
        if not raw_paths:
            raw_paths = [str(package_root / "environment"), str(source / "python"), str(source)]
        import_paths: list[str] = []
        for raw in raw_paths:
            path = Path(str(raw)).resolve()
            try:
                path.relative_to(package_root)
            except ValueError as exc:
                raise PluginWorkerError("worker import path escapes immutable package") from exc
            import_paths.append(str(path))
        config = {
            "schema": "variant1.extension-worker-launch.v1",
            "package_id": str(package["package_id"]),
            "package_digest": digest,
            "source": str(source),
            "import_paths": import_paths,
            "entry_module": str(worker.get("module") or ""),
            "allowed_handlers": list(package["allowed_handlers"]),
            "maximum_concurrency": self.worker_concurrency,
        }
        path = self.root / f"{digest}.json"
        encoded = _stable(config)
        if path.is_file() and path.read_text(encoding="utf-8") == encoded:
            return path
        temporary = self.root / f".{digest}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
        return path

    async def _get_worker(self, package: Mapping[str, Any]) -> _WorkerProcess:
        digest = str(package["package_digest"])
        async with self._worker_lock:
            if self._shutting_down:
                raise PluginWorkerLost("plugin worker host is shutting down")
            current = self._workers.get(digest)
            if current is not None and current.alive:
                return current
            if current is not None:
                await current.terminate()
            config_path = await asyncio.to_thread(self._write_config, package)
            self._generation += 1
            worker = _WorkerProcess(
                package_digest=digest,
                config_path=config_path,
                command=self.command_builder(package, config_path),
                cwd=str(Path(str(package["source_path"])).resolve()),
                generation=self._generation,
                startup_deadline_s=self.startup_deadline_s,
            )
            await worker.start()
            self._workers[digest] = worker
            return worker

    async def _drop_worker(self, digest: str, worker: _WorkerProcess) -> None:
        async with self._worker_lock:
            if self._workers.get(digest) is worker:
                self._workers.pop(digest, None)
        await worker.terminate()

    def _prepare(
        self,
        *,
        package: Mapping[str, Any],
        contribution_id: str,
        arguments: Mapping[str, Any],
        context: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        digest = str(package["package_digest"])
        handler = str(package["handler"])
        effect_class = str(package["effect_class"])
        semantic_context = {
            key: value for key, value in context.items()
            if key not in {
                "request_id", "outer_tool_call_id", "nested_call_id",
                "cell_execution_id",
            }
        }
        fingerprint = request_fingerprint("plugin.invoke", {
            "package_digest": digest,
            "contribution_kind": str(
                package.get("contribution_kind") or "capabilities"
            ),
            "contribution_id": contribution_id,
            "handler": handler,
            "effect_class": effect_class,
            "arguments": arguments,
            "context": semantic_context,
        })
        dedupe = str(idempotency_key or "").strip() or "auto:" + uuid.uuid4().hex
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM extension_worker_operation_v2 WHERE "
                "package_digest=? AND contribution_id=? AND dedupe_key=?",
                (digest, contribution_id, dedupe),
            ).fetchone()
            if row is not None:
                if str(row["request_fingerprint"]) != fingerprint:
                    raise PluginIdempotencyConflict(
                        "idempotency key was already used for a different plugin request"
                    )
                conn.commit()
                return dict(row)
            operation_id = "plugin-op-" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO extension_worker_operation_v2(" 
                "operation_id,dedupe_key,idempotency_key,request_fingerprint,"
                "package_id,package_digest,contribution_id,handler,effect_class,status,"
                "arguments_json,context_json,prepared_at) VALUES (?,?,?,?,?,?,?,?,?,"
                "'prepared',?,?,?)",
                (
                    operation_id, dedupe, str(idempotency_key or ""), fingerprint,
                    str(package["package_id"]), digest, contribution_id, handler,
                    effect_class, _stable(arguments), _stable(context), now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM extension_worker_operation_v2 WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            conn.commit()
        return dict(row)

    def _operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM extension_worker_operation_v2 WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise LookupError("unknown plugin operation")
        return dict(row)

    def _transition(
        self,
        operation_id: str,
        expected: tuple[str, ...],
        status: str,
        *,
        worker_generation: int = 0,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status,attempt_count FROM extension_worker_operation_v2 WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise LookupError("unknown plugin operation")
            if str(row["status"]) not in expected:
                conn.commit()
                return self._operation(operation_id)
            dispatched_at = now if status == "dispatched" else 0
            finished_at = now if status in _TERMINAL else 0
            attempt_delta = 1 if status == "dispatched" else 0
            conn.execute(
                "UPDATE extension_worker_operation_v2 SET status=?,"
                "worker_generation=CASE WHEN ?>0 THEN ? ELSE worker_generation END,"
                "attempt_count=attempt_count+?,"
                "dispatched_at=CASE WHEN ?>0 THEN ? ELSE dispatched_at END,"
                "finished_at=CASE WHEN ?>0 THEN ? ELSE finished_at END,"
                "result_json=?,error_json=? WHERE operation_id=?",
                (
                    status, worker_generation, worker_generation, attempt_delta,
                    dispatched_at, dispatched_at, finished_at, finished_at,
                    _stable(result) if status == "succeeded" else "",
                    _stable(dict(error or {})) if error else "",
                    operation_id,
                ),
            )
            conn.commit()
        return self._operation(operation_id)

    def reconcile(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            safe = conn.execute(
                "UPDATE extension_worker_operation_v2 SET status='prepared',"
                "error_json=?,worker_generation=0 WHERE status='dispatched' "
                "AND effect_class IN ('pure','read')",
                (_stable({"code": "startup_recovery", "message": "safe retry is prepared"}),),
            ).rowcount
            unknown = conn.execute(
                "UPDATE extension_worker_operation_v2 SET status='unknown_effect',"
                "finished_at=?,error_json=? WHERE status='dispatched' "
                "AND effect_class NOT IN ('pure','read')",
                (time.time(), _stable({
                    "code": "unknown_effect",
                    "message": "backend restarted after effect dispatch",
                })),
            ).rowcount
            conn.commit()
        self.last_reconciliation = {"safe_prepared": int(safe), "unknown_effect": int(unknown)}
        return dict(self.last_reconciliation)

    async def start(self) -> dict[str, Any]:
        if not self.started:
            await asyncio.to_thread(self.reconcile)
            async with self._worker_lock:
                self._shutting_down = False
            self.started = True
        return self.state()

    @staticmethod
    def _receipt(row: Mapping[str, Any], *, replayed: bool) -> dict[str, Any]:
        result = json.loads(str(row.get("result_json") or "null"))
        return {
            "schema": "variant1.plugin-invocation.v1",
            "operation_id": str(row["operation_id"]),
            "package_id": str(row["package_id"]),
            "package_digest": str(row["package_digest"]),
            "contribution_id": str(row["contribution_id"]),
            "effect_class": str(row["effect_class"]),
            "status": "succeeded",
            "attempt_count": int(row["attempt_count"]),
            "replayed": bool(replayed),
            "result": result,
        }

    @staticmethod
    def _raise_terminal(row: Mapping[str, Any]) -> None:
        status = str(row.get("status") or "")
        error = json.loads(str(row.get("error_json") or "{}"))
        message = str(error.get("message") or f"plugin operation {status}")
        if status == "unknown_effect":
            raise UnknownPluginEffect(str(row["operation_id"]), message)
        if status == "failed":
            raise PluginInvocationFailed(
                message, operation_id=str(row["operation_id"]),
                code=str(error.get("code") or "plugin_failed"),
            )

    async def _prepare_invocation(
        self,
        package_id: str,
        contribution_id: str,
        arguments: Mapping[str, Any] | None,
        *,
        context: Mapping[str, Any] | None,
        chat_id: str,
        idempotency_key: str,
        contribution_kind: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not self.started:
            await self.start()
        clean_arguments = _json_clone(
            arguments or {}, label="arguments", require_object=True,
        )
        clean_context = _json_clone(
            context or {}, label="context", require_object=True,
        )
        package = await asyncio.to_thread(
            self._resolve_package,
            str(package_id),
            str(contribution_id),
            str(chat_id),
            str(contribution_kind or "capabilities"),
        )
        await asyncio.to_thread(
            self._validate_arguments, package, clean_arguments,
        )
        row = await asyncio.to_thread(
            self._prepare,
            package=package,
            contribution_id=str(contribution_id),
            arguments=clean_arguments,
            context=clean_context,
            idempotency_key=str(idempotency_key or ""),
        )
        return clean_arguments, clean_context, package, row

    def _retain_submission(
        self,
        operation_id: str,
        request_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        self._submitted_by_operation[str(operation_id)] = task
        if request_id:
            self._submitted_by_request[str(request_id)] = task

        def completed(done: asyncio.Task[Any]) -> None:
            for registry in (
                self._submitted_by_operation,
                self._submitted_by_request,
            ):
                for key, candidate in tuple(registry.items()):
                    if candidate is done:
                        registry.pop(key, None)
            if done.cancelled():
                return
            with suppress(BaseException):
                done.exception()

        task.add_done_callback(completed)

    def _submitted_task(self, identity: str) -> asyncio.Task[Any] | None:
        clean = str(identity or "")
        return (
            self._submitted_by_operation.get(clean)
            or self._submitted_by_request.get(clean)
        )

    def _submitted_operation_id(self, identity: str) -> str:
        clean = str(identity or "")
        if clean in self._submitted_by_operation:
            return clean
        task = self._submitted_by_request.get(clean)
        if task is None:
            return ""
        return next(
            (
                operation_id
                for operation_id, candidate in self._submitted_by_operation.items()
                if candidate is task
            ),
            "",
        )

    @staticmethod
    def _submission_handle(row: Mapping[str, Any], request_id: str) -> dict[str, Any]:
        status = str(row.get("status") or "")
        return {
            "schema": "variant1.plugin-operation-handle.v1",
            "accepted": True,
            "operation_id": str(row["operation_id"]),
            "request_id": str(request_id or ""),
            "package_id": str(row["package_id"]),
            "package_digest": str(row["package_digest"]),
            "contribution_id": str(row["contribution_id"]),
            "effect_class": str(row["effect_class"]),
            "status": status,
            "terminal": status in _TERMINAL,
        }

    async def submit(
        self,
        package_id: str,
        contribution_id: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        chat_id: str = "",
        idempotency_key: str = "",
        request_id: str = "",
        deadline_s: float | None = None,
        contribution_kind: str = "capabilities",
    ) -> dict[str, Any]:
        """Durably prepare and host one asynchronous plugin invocation."""

        prepared = await self._prepare_invocation(
            package_id,
            contribution_id,
            arguments,
            context=context,
            chat_id=chat_id,
            idempotency_key=idempotency_key,
            contribution_kind=contribution_kind,
        )
        clean_arguments, clean_context, package, row = prepared
        operation_id = str(row["operation_id"])
        status = str(row["status"])
        task = self._submitted_by_operation.get(operation_id)
        request_key = str(request_id or "")
        request_task = self._submitted_by_request.get(request_key) if request_key else None
        if request_task is not None and request_task is not task:
            raise PluginIdempotencyConflict(
                "request id is already attached to another plugin operation"
            )
        if task is None and status not in _TERMINAL:
            task = asyncio.create_task(
                self.invoke(
                    package_id,
                    contribution_id,
                    clean_arguments,
                    context=clean_context,
                    chat_id=chat_id,
                    idempotency_key=idempotency_key,
                    request_id=request_key,
                    deadline_s=deadline_s,
                    contribution_kind=contribution_kind,
                    _prepared=(clean_arguments, clean_context, package, row),
                ),
                name=f"plugin-invoke:{operation_id}",
            )
            self._retain_submission(operation_id, request_key, task)
        elif task is not None and request_key:
            self._submitted_by_request[request_key] = task
        return self._submission_handle(row, request_key)

    async def invoke(
        self,
        package_id: str,
        contribution_id: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        chat_id: str = "",
        idempotency_key: str = "",
        request_id: str = "",
        deadline_s: float | None = None,
        contribution_kind: str = "capabilities",
        _prepared: tuple[
            dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
        ] | None = None,
    ) -> dict[str, Any]:
        """Track both submitted and direct invocations through shutdown."""

        task = asyncio.current_task()
        if task is None:
            raise PluginWorkerLost("plugin invocation has no owning task")
        if self._shutting_down:
            raise PluginWorkerLost("plugin worker host is shutting down")
        self._active_invocations.add(task)
        try:
            return await self._invoke_impl(
                package_id,
                contribution_id,
                arguments,
                context=context,
                chat_id=chat_id,
                idempotency_key=idempotency_key,
                request_id=request_id,
                deadline_s=deadline_s,
                contribution_kind=contribution_kind,
                _prepared=_prepared,
            )
        finally:
            self._active_invocations.discard(task)

    async def _invoke_impl(
        self,
        package_id: str,
        contribution_id: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        chat_id: str = "",
        idempotency_key: str = "",
        request_id: str = "",
        deadline_s: float | None = None,
        contribution_kind: str = "capabilities",
        _prepared: tuple[
            dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]
        ] | None = None,
    ) -> dict[str, Any]:
        prepared = _prepared or await self._prepare_invocation(
            package_id,
            contribution_id,
            arguments,
            context=context,
            chat_id=chat_id,
            idempotency_key=idempotency_key,
            contribution_kind=contribution_kind,
        )
        clean_arguments, clean_context, package, row = prepared
        operation_id = str(row["operation_id"])
        lock = self._operation_locks.setdefault(operation_id, asyncio.Lock())
        timeout = self.default_deadline_s if deadline_s is None else max(
            0.1, min(float(deadline_s), 300.0)
        )
        async with lock, self._semaphore:
            row = await asyncio.to_thread(self._operation, operation_id)
            if row["status"] == "succeeded":
                return self._receipt(row, replayed=True)
            self._raise_terminal(row)
            if row["status"] == "dispatched":
                # A concurrent process cannot own this row while its per-operation
                # lock is held.  Conservatively apply startup recovery semantics.
                target = "prepared" if row["effect_class"] in _SAFE_EFFECTS else "unknown_effect"
                row = await asyncio.to_thread(
                    self._transition, operation_id, ("dispatched",), target,
                    error={"code": target, "message": "orphaned dispatched operation"},
                )
                self._raise_terminal(row)

            launch_retries = 0
            read_retries = 0
            while True:
                worker: _WorkerProcess | None = None
                dispatched = False
                correlation_id = "invoke-" + uuid.uuid4().hex
                external_request_id = str(request_id or operation_id)
                try:
                    worker = await self._get_worker(package)
                    row = await asyncio.to_thread(
                        self._transition, operation_id, ("prepared",), "dispatched",
                        worker_generation=worker.generation,
                    )
                    dispatched = True
                    inflight = (worker, correlation_id)
                    self._inflight[external_request_id] = inflight
                    self._inflight[operation_id] = inflight
                    response = await worker.request({
                        "schema": PROTOCOL_SCHEMA,
                        "operation": "invoke",
                        "id": correlation_id,
                        "package_digest": str(package["package_digest"]),
                        "handler": str(package["handler"]),
                        "arguments": clean_arguments,
                        "context": clean_context,
                    }, deadline_s=timeout)
                    if (
                        external_request_id in self._cancelled_requests
                        or operation_id in self._cancelled_requests
                    ):
                        effect = str(package["effect_class"])
                        target = "failed" if effect in _SAFE_EFFECTS else "unknown_effect"
                        row = await asyncio.to_thread(
                            self._transition, operation_id, ("dispatched",), target,
                            error={"code": "cancelled", "message": "plugin invocation was cancelled"},
                        )
                        self._raise_terminal(row)
                    if not bool(response.get("ok")):
                        error = dict(response.get("error") or {})
                        row = await asyncio.to_thread(
                            self._transition, operation_id, ("dispatched",), "failed",
                            error=error,
                        )
                        self._raise_terminal(row)
                    result = _json_clone(response.get("result"), label="handler result")
                    row = await asyncio.to_thread(
                        self._transition, operation_id, ("dispatched",), "succeeded",
                        result=result,
                    )
                    return self._receipt(row, replayed=False)
                except asyncio.CancelledError:
                    if worker is not None:
                        worker.retire_requested = True
                        # request() has already cancelled and removed this
                        # invocation. Every remaining pending call is a peer.
                        if worker.pending_count == 0:
                            with suppress(BaseException):
                                await self._drop_worker(
                                    str(package["package_digest"]), worker
                                )
                    effect = str(package["effect_class"])
                    target = "failed" if effect in _SAFE_EFFECTS else "unknown_effect"
                    await asyncio.to_thread(
                        self._transition, operation_id, ("dispatched", "prepared"), target,
                        error={"code": "cancelled", "message": "plugin invocation was cancelled"},
                    )
                    raise
                except PluginWorkerLost as exc:
                    if worker is not None:
                        await self._drop_worker(str(package["package_digest"]), worker)
                    effect = str(package["effect_class"])
                    error = {"code": type(exc).__name__, "message": str(exc)}
                    if (
                        external_request_id in self._cancelled_requests
                        or operation_id in self._cancelled_requests
                    ):
                        target = "failed" if effect in _SAFE_EFFECTS else "unknown_effect"
                        row = await asyncio.to_thread(
                            self._transition,
                            operation_id,
                            ("dispatched", "prepared"),
                            target,
                            error={"code": "cancelled", "message": "plugin invocation was cancelled"},
                        )
                        self._raise_terminal(row)
                    if not dispatched:
                        if launch_retries < 1:
                            launch_retries += 1
                            continue
                        row = await asyncio.to_thread(
                            self._transition, operation_id, ("prepared",), "failed",
                            error=error,
                        )
                        self._raise_terminal(row)
                    if effect in _SAFE_EFFECTS and read_retries < self.max_read_retries:
                        read_retries += 1
                        await asyncio.to_thread(
                            self._transition, operation_id, ("dispatched",), "prepared",
                            error=error,
                        )
                        continue
                    target = "failed" if effect in _SAFE_EFFECTS else "unknown_effect"
                    row = await asyncio.to_thread(
                        self._transition, operation_id, ("dispatched", "prepared"), target,
                        error=error,
                    )
                    self._raise_terminal(row)
                    raise PluginInvocationFailed(
                        str(exc), operation_id=operation_id, code=type(exc).__name__
                    ) from exc
                finally:
                    current = self._inflight.get(external_request_id)
                    if current is not None and current[1] == correlation_id:
                        self._inflight.pop(external_request_id, None)
                    current = self._inflight.get(operation_id)
                    if current is not None and current[1] == correlation_id:
                        self._inflight.pop(operation_id, None)
                    self._cancelled_requests.discard(external_request_id)
                    self._cancelled_requests.discard(operation_id)
                    if (
                        worker is not None
                        and worker.retire_requested
                        and worker.pending_count == 0
                    ):
                        with suppress(BaseException):
                            await self._drop_worker(
                                str(package["package_digest"]), worker
                            )

    async def cancel(self, request_id: str) -> bool:
        identity = str(request_id or "")
        task = self._submitted_task(identity)
        operation_id = self._submitted_operation_id(identity)
        current = self._inflight.get(identity)
        if current is None and operation_id:
            current = self._inflight.get(operation_id)
        if current is None:
            if task is None or task.done() or not operation_id:
                return False
            row = await asyncio.to_thread(self._operation, operation_id)
            if str(row.get("status") or "") != "prepared":
                return False
            await asyncio.to_thread(
                self._transition,
                operation_id,
                ("prepared",),
                "failed",
                error={
                    "code": "cancelled",
                    "message": "plugin invocation was cancelled before dispatch",
                },
            )
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return True
        worker, correlation_id = current
        # A response can arrive while the host is committing its operation.
        # That completed request no longer contributes to pending_count; a
        # late cancellation must not terminate the worker's remaining peers.
        if not worker.has_pending_request(correlation_id):
            return False
        self._cancelled_requests.add(identity)
        if operation_id:
            self._cancelled_requests.add(operation_id)
        worker.retire_requested = True
        if worker.pending_count <= 1:
            await self._drop_worker(worker.package_digest, worker)
            cancelled = True
        else:
            cancelled = await worker.cancel_request(correlation_id)
        if cancelled and task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        self._cancelled_requests.discard(identity)
        return bool(cancelled)

    def operation(self, operation_id: str) -> dict[str, Any]:
        row = self._operation(operation_id)
        output = dict(row)
        output["arguments"] = json.loads(output.pop("arguments_json"))
        output["context"] = json.loads(output.pop("context_json"))
        output["result"] = json.loads(output.pop("result_json") or "null")
        output["error"] = json.loads(output.pop("error_json") or "{}")
        return output

    def operations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT operation_id FROM extension_worker_operation_v2 "
                "ORDER BY prepared_at DESC LIMIT ?", (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self.operation(str(row[0])) for row in rows]

    def state(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            counts = {
                str(row["status"]): int(row["n"])
                for row in conn.execute(
                    "SELECT status,COUNT(*) AS n FROM extension_worker_operation_v2 GROUP BY status"
                ).fetchall()
            }
        return {
            "schema": "variant1.extension-worker-host.v1",
            "started": self.started,
            "workers": [
                {
                    "package_digest": digest,
                    "generation": worker.generation,
                    "alive": worker.alive,
                    "pending": worker.pending_count,
                    "diagnostic_tail": worker.diagnostics,
                }
                for digest, worker in sorted(self._workers.items())
            ],
            "operations": counts,
            "submitted": len(self._submitted_by_operation),
            "last_reconciliation": dict(self.last_reconciliation),
            "maximum_concurrency": self.maximum_concurrency,
            "worker_concurrency": self.worker_concurrency,
        }

    async def shutdown(self) -> None:
        async with self._worker_lock:
            self._shutting_down = True
        current = asyncio.current_task()
        submitted = set(self._submitted_by_operation.values())
        active = set(self._active_invocations)
        owned_tasks = tuple(
            task for task in submitted.union(active)
            if task is not current
        )
        for task in owned_tasks:
            if not task.done():
                task.cancel()
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)
        self._submitted_by_operation.clear()
        self._submitted_by_request.clear()
        self._active_invocations.clear()
        async with self._worker_lock:
            workers = tuple(self._workers.values())
            self._workers.clear()
        for worker in workers:
            if worker.pending_count:
                await worker.terminate()
            else:
                await worker.shutdown()
        self.started = False


__all__ = [
    "PluginIdempotencyConflict", "PluginInvocationFailed", "PluginWorkerDeadline",
    "PluginWorkerError", "PluginWorkerHost", "PluginWorkerLost",
    "UnknownPluginEffect",
]
