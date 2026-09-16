"""Disposable same-user worker for session mutations.

Mutation code runs with the same operating-system authority and Python import
surface as VARIANT-1. The separate process, ownership gate, Job Object, framed
protocol, and JSON contract exist for lifecycle cleanup and deterministic tool
behavior; they are not a security sandbox.
"""

from __future__ import annotations

import ast
from contextlib import suppress
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import traceback
import uuid
from typing import Any

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from core_invariants import canonical_digest
from kernel_runtime.worker_bridge import (
    Variant1RemoteHandle,
    REMOTE_HANDLE_DISPATCH_SCHEMA,
    _decode_host_result,
    _encode_host_argument,
)
from session_catalog.mutation_contracts import MUTATION_REMOTE_HANDLE_ROLE

PROTOCOL = "variant1.astb.mutation-worker.v2"
MAX_SOURCE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 32 * 1024


class CandidateContractError(ValueError):
    pass


def validate_source(source: str) -> tuple[ast.Module, str]:
    """Validate only the mutation callable contract, not Python capability."""
    raw = str(source or "")
    if not raw.strip():
        raise CandidateContractError("candidate source is empty")
    if len(raw.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise CandidateContractError(
            f"candidate source exceeds {MAX_SOURCE_BYTES} bytes"
        )
    try:
        tree = ast.parse(raw, filename="<session-mutation>", mode="exec")
    except SyntaxError as exc:
        raise CandidateContractError(
            f"candidate syntax error at line {exc.lineno}: {exc.msg}"
        ) from exc
    run_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run"
    ]
    if len(run_nodes) != 1 or isinstance(run_nodes[0], ast.AsyncFunctionDef):
        raise CandidateContractError(
            "candidate must define exactly one synchronous run(arguments)"
        )
    run_node = run_nodes[0]
    positional = list(run_node.args.posonlyargs) + list(run_node.args.args)
    if [node.arg for node in positional] != ["arguments"]:
        raise CandidateContractError("run must have the exact signature run(arguments)")
    if (
        run_node.args.vararg is not None
        or run_node.args.kwarg is not None
        or run_node.args.kwonlyargs
        or run_node.args.defaults
    ):
        raise CandidateContractError("run must have the exact signature run(arguments)")
    compile(tree, "<session-mutation>", "exec")
    return tree, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        raise ValueError("candidate result nesting exceeds 24")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("candidate result contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _safe_json(item, depth=depth + 1)
            for key, item in value.items()
        }
    raise ValueError(
        f"candidate result is not JSON-serializable: {type(value).__name__}"
    )


def _encode_proxy_argument(value: Any) -> Any:
    """Project returned handles to the identity the live Python worker sends."""

    if isinstance(value, Variant1RemoteHandle):
        return value.identity
    if isinstance(value, dict):
        payload = value.get("$variant1_handle")
        if isinstance(payload, dict):
            return {
                key: payload[key]
                for key in ("service", "kind", "id", "generation", "revision")
                if key in payload
            }
        return {
            str(key): _encode_proxy_argument(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_encode_proxy_argument(item) for item in value]
    return value


class _MutationRemoteHandleBridge:
    """Bind typed worker-side handles to one private host proxy."""

    def __init__(self) -> None:
        self._dispatch: _ProxyMethod | None = None

    def bind(self, dispatch: "_ProxyMethod") -> None:
        if self._dispatch is not None:
            raise RuntimeError("mutation worker received duplicate handle dispatch routes")
        self._dispatch = dispatch

    def invoke(self, descriptor: dict[str, Any], arguments: dict[str, Any]) -> Any:
        if descriptor.get("schema") != REMOTE_HANDLE_DISPATCH_SCHEMA:
            raise RuntimeError("mutation worker received an invalid handle dispatcher")
        if self._dispatch is None:
            raise RuntimeError("mutation worker remote-handle dispatch is unavailable")
        return self._dispatch(**dict(arguments or {}))

    async def invoke_async(
        self,
        descriptor: dict[str, Any],
        arguments: dict[str, Any],
        *,
        deadline_ms: int | None = None,
    ) -> Any:
        del deadline_ms
        return self.invoke(descriptor, arguments)


class _ProxyMethod:
    def __init__(
        self,
        reader: Any,
        writer: Any,
        qualified_name: str,
        parameters: list[str],
        observed: list[dict[str, Any]],
        remote_handles: _MutationRemoteHandleBridge,
    ):
        self.reader = reader
        self.writer = writer
        self.qualified_name = str(qualified_name)
        self.parameters = [str(item) for item in parameters]
        self.observed = observed
        self.remote_handles = remote_handles

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) > len(self.parameters):
            raise TypeError(
                f"{self.qualified_name} accepts at most {len(self.parameters)} "
                "positional arguments"
            )
        arguments = dict(kwargs)
        for index, value in enumerate(args):
            name = self.parameters[index]
            if name in arguments:
                raise TypeError(
                    f"{self.qualified_name} got multiple values for {name!r}"
                )
            arguments[name] = value
        payload = {
            "schema": PROTOCOL,
            "type": "proxy_call",
            "request_id": "mcall_" + uuid.uuid4().hex,
            "proxy": self.qualified_name,
            "arguments": _safe_json(_encode_proxy_argument(arguments)),
        }
        self.writer.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.writer.flush()
        line = self.reader.readline()
        if not line:
            raise RuntimeError("mutation worker proxy channel closed")
        response = json.loads(line)
        if response.get("request_id") != payload["request_id"]:
            raise RuntimeError("mutation worker proxy response was miscorrelated")
        observation = {
            "proxy": self.qualified_name,
            "arguments_sha256": canonical_digest(payload["arguments"]),
            "ok": bool(response.get("ok")),
            "receipt_id": str(response.get("receipt_id") or ""),
        }
        self.observed.append(observation)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "proxy call failed"))
        return _decode_host_result(response.get("result"), self.remote_handles)

    def __repr__(self) -> str:
        return f"<VARIANT-1 mutation proxy {self.qualified_name}>"


class _ProxyRoot:
    def __init__(
        self,
        name: str,
        methods: dict[str, _ProxyMethod],
    ) -> None:
        self._name = str(name)
        self._methods = dict(methods)

    def __getattr__(self, name: str) -> Any:
        if str(name).startswith("_"):
            raise AttributeError(name)
        try:
            return self._methods[str(name)]
        except KeyError:
            raise AttributeError(
                f"{self._name} has no mounted proxy {name!r}"
            ) from None

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._methods))

    def __repr__(self) -> str:
        return f"<VARIANT-1 mutation {self._name}: {', '.join(sorted(self._methods))}>"


class _CandidateOutputCapture:
    """Keep Python, native-library, and subprocess output off the protocol."""

    def __init__(self) -> None:
        self.capture = tempfile.TemporaryFile(mode="w+b")
        self.saved_stdout = -1
        self.saved_stderr = -1

    def __enter__(self) -> "_CandidateOutputCapture":
        self.saved_stdout = os.dup(1)
        self.saved_stderr = os.dup(2)
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(self.capture.fileno(), 1)
        os.dup2(self.capture.fileno(), 2)
        return self

    def __exit__(self, exc_type, exc, traceback_value) -> bool:
        del exc, traceback_value
        with suppress(Exception):
            sys.stdout.flush()
        with suppress(Exception):
            sys.stderr.flush()
        if self.saved_stdout >= 0:
            os.dup2(self.saved_stdout, 1)
            os.close(self.saved_stdout)
        if self.saved_stderr >= 0:
            os.dup2(self.saved_stderr, 2)
            os.close(self.saved_stderr)
        if exc_type is not None:
            self.capture.close()
        return False

    def finish(self) -> str:
        try:
            self.capture.flush()
            self.capture.seek(0)
            raw = self.capture.read(MAX_OUTPUT_BYTES + 1)
        finally:
            self.capture.close()
        if len(raw) > MAX_OUTPUT_BYTES:
            raise RuntimeError(
                f"candidate output exceeded {MAX_OUTPUT_BYTES} bytes"
            )
        return raw.decode("utf-8", errors="replace")


def _execute(request: dict[str, Any], reader: Any, writer: Any) -> dict[str, Any]:
    source = str(request.get("source") or "")
    tree, digest = validate_source(source)
    if request.get("mode") == "validate":
        return {
            "ok": True,
            "source_sha256": digest,
            "language_policy": "full-python.same-user.v1",
            "execution_mode": "same_user",
        }

    raw_workspace_roots = request.get("workspace_roots")
    workspace_roots = [
        os.path.abspath(str(path))
        for path in (
            raw_workspace_roots
            if isinstance(raw_workspace_roots, list)
            else ()
        )
        if str(path or "").strip()
    ]
    if workspace_roots:
        if not os.path.isdir(workspace_roots[0]):
            raise RuntimeError("bound mutation workspace is unavailable")
        os.chdir(workspace_roots[0])

    raw_contracts = request.get("proxy_contracts")
    contracts = dict(raw_contracts) if isinstance(raw_contracts, dict) else {}
    observed_calls: list[dict[str, Any]] = []
    roots: dict[str, dict[str, _ProxyMethod]] = {}
    remote_handles = _MutationRemoteHandleBridge()
    for raw_name, raw_spec in contracts.items():
        qualified = str(raw_name or "").strip()
        root, separator, method = qualified.partition(".")
        if not separator or not root.isidentifier() or not method.isidentifier():
            continue
        spec = dict(raw_spec) if isinstance(raw_spec, dict) else {}
        parameters = [
            str(item) for item in (spec.get("parameters") or ())
            if str(item).isidentifier()
        ]
        proxy = _ProxyMethod(
            reader,
            writer,
            qualified,
            parameters,
            observed_calls,
            remote_handles,
        )
        if str(spec.get("internal_role") or "") == MUTATION_REMOTE_HANDLE_ROLE:
            remote_handles.bind(proxy)
            continue
        roots.setdefault(root, {})[method] = proxy
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__name__": "__mutation__",
        "__file__": "<session-mutation>",
    }
    for root, methods in roots.items():
        namespace[root] = _ProxyRoot(root, methods)
    namespace.setdefault("tools", _ProxyRoot("tools", {}))
    started = time.perf_counter()
    capture = _CandidateOutputCapture()
    with capture:
        exec(compile(tree, "<session-mutation>", "exec"), namespace, namespace)
        result = namespace["run"](
            _safe_json(dict(request.get("arguments") or {}))
        )
    output = capture.finish()
    duration_ms = (time.perf_counter() - started) * 1000
    return {
        "ok": True,
        "source_sha256": digest,
        "result": _safe_json(_encode_host_argument(result)),
        "stdout": output,
        "observed_calls": observed_calls,
        "duration_ms": round(duration_ms, 3),
        "language_policy": "full-python.same-user.v1",
        "execution_mode": "same_user",
        "working_directory": os.getcwd(),
    }


def _wait_for_gate() -> None:
    gate = str(os.environ.get("VARIANT1_KERNEL_GATE_FILE") or "")
    token = str(os.environ.get("VARIANT1_KERNEL_GATE_TOKEN") or "")
    if not gate and not token:
        return
    if not gate or not token:
        raise RuntimeError("mutation worker ownership gate is incomplete")
    deadline = time.monotonic() + float(
        os.environ.get("VARIANT1_KERNEL_GATE_TIMEOUT_S", "10")
    )
    while time.monotonic() < deadline:
        try:
            with open(gate, "r", encoding="utf-8") as handle:
                if handle.read() == token:
                    return
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise TimeoutError("mutation worker ownership gate was not released")


def _protocol_writer() -> Any:
    duplicate = os.dup(sys.stdout.fileno())
    return os.fdopen(
        duplicate, "w", encoding="utf-8", newline="\n", buffering=1
    )


def main() -> int:
    protocol_in = sys.stdin
    protocol_out = _protocol_writer()
    try:
        _wait_for_gate()
        line = protocol_in.readline()
        if not line:
            raise RuntimeError("mutation worker received no request")
        request = json.loads(line)
        if request.get("schema") != PROTOCOL:
            raise RuntimeError("unsupported mutation worker protocol")
        response = _execute(request, protocol_in, protocol_out)
    except BaseException as exc:
        response = {
            "ok": False,
            "error": {
                "code": (
                    "candidate_contract_error"
                    if isinstance(exc, CandidateContractError)
                    else "candidate_execution_error"
                ),
                "message": str(exc)[:2000],
                "type": type(exc).__name__,
            },
            "traceback": traceback.format_exc(limit=8)[-8000:],
        }
    response.update({"schema": PROTOCOL, "type": "result"})
    protocol_out.write(json.dumps(response, separators=(",", ":")) + "\n")
    protocol_out.flush()
    protocol_out.close()
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
