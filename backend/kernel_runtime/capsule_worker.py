"""Kernel-side inspection, capture, and restore for portable capsule values.

The worker classifies namespace values without serializing them for inspection.
Capture receives explicit host bounds, measures values before hashing/base64,
and can reference an unchanged prior CAS value without returning its body.
"""

from __future__ import annotations

import base64
import dataclasses
import functools
import hashlib
import importlib
import importlib.metadata
import io
import json
import keyword
import math
import platform
import sys
import types
from typing import Any, Callable, Iterable

from core_invariants import canonical_json_bytes as _canonical_json_bytes
from .capsule_contracts import (
    SERIALIZER_REGISTRY_SCHEMA,
    WORKER_CAPSULE_SCHEMA,
    WORKER_CAPTURE_REQUEST_SCHEMA,
    WORKER_NAMESPACE_SCHEMA,
    serializer_registry_document,
)

_RUNTIME_MANAGED = frozenset({"display", "clear_output"})
_SERVICE_PROXY_MODULES = frozenset({
    "kernel_runtime.worker_bridge",
    "kernel_runtime.bridge",
})
_MODEL_CLIENT_MODULE_PREFIXES = (
    "anthropic",
    "google.generativeai",
    "openai",
    "transformers",
)
_DESKTOP_BROWSER_MODULE_MARKERS = (
    "playwright",
    "pywinauto",
    "selenium",
    "uiautomation",
    "win32com",
)
_DATABASE_MODULE_PREFIXES = (
    "asyncpg",
    "duckdb",
    "psycopg",
    "pymongo",
    "redis",
    "sqlite3",
    "sqlalchemy",
)
_DEFAULT_LIMITS = {
    "max_values": 256,
    "max_excluded_values": 2048,
    "max_depth": 64,
    "max_container_items": 1_000_000,
    "max_value_bytes": 8 * 1024 * 1024,
    "max_total_value_bytes": 32 * 1024 * 1024,
    "max_response_bytes": 48 * 1024 * 1024,
}
_JSON_ENCODER = json.JSONEncoder(
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
)


def _strict_json_analysis(
    value: Any,
    *,
    max_depth: int = 64,
    max_items: int = 1_000_000,
) -> tuple[bool, str]:
    """Classify strict JSON while bounding recursion and container traversal."""

    seen: set[int] = set()
    visited = 0

    def visit(item: Any, depth: int) -> tuple[bool, str]:
        nonlocal visited
        if depth > max(0, int(max_depth)):
            return False, "max_depth_exceeded"
        visited += 1
        if visited > max(1, int(max_items)):
            return False, "max_container_items_exceeded"
        if item is None or type(item) in {bool, int, str}:
            return True, ""
        if type(item) is float:
            return (True, "") if math.isfinite(item) else (False, "non_finite_float")
        if type(item) not in {list, dict}:
            return False, "unsupported_type"
        identity = id(item)
        if identity in seen:
            return False, "recursive_value"
        seen.add(identity)
        try:
            if type(item) is list:
                for child in item:
                    valid, reason = visit(child, depth + 1)
                    if not valid:
                        return valid, reason
                return True, ""
            for key, child in item.items():
                if type(key) is not str:
                    return False, "non_string_json_key"
                valid, reason = visit(child, depth + 1)
                if not valid:
                    return valid, reason
            return True, ""
        finally:
            seen.remove(identity)

    return visit(value, 0)


def _strict_json_value(value: Any, *, seen: set[int] | None = None) -> bool:
    """Compatibility wrapper for the original deliberately small JSON set."""

    del seen
    valid, _reason = _strict_json_analysis(
        value,
        max_depth=1_000,
        max_items=10_000_000,
    )
    return valid


def _type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _excluded_reason(value: Any) -> str:
    value_type = type(value)
    module_name = str(getattr(value_type, "__module__", "") or "").casefold()
    class_name = str(getattr(value_type, "__name__", "") or "").casefold()
    if isinstance(value, types.ModuleType):
        return "module"
    if module_name in _SERVICE_PROXY_MODULES or "capabilityproxy" in class_name:
        return "service_proxy"
    if module_name.startswith("socket") or class_name in {"socket", "websocket"}:
        return "socket"
    if module_name.startswith("subprocess") or class_name in {
        "popen",
        "process",
        "processpoolexecutor",
    }:
        return "process"
    if module_name.startswith(_DATABASE_MODULE_PREFIXES) or (
        any(marker in class_name for marker in ("connection", "cursor", "session"))
        and any(marker in module_name for marker in _DATABASE_MODULE_PREFIXES)
    ):
        return "database_client"
    if module_name.startswith(_MODEL_CLIENT_MODULE_PREFIXES):
        return "model_client"
    if any(marker in module_name for marker in _DESKTOP_BROWSER_MODULE_MARKERS):
        return "browser_or_desktop_handle"
    if callable(value):
        return "callable"
    if any(
        marker in module_name
        for marker in ("asyncio", "threading", "multiprocessing", "io")
    ):
        return "live_resource"
    if isinstance(value, (bytearray, memoryview)):
        return "mutable_binary_unsupported"
    return "unsupported_type"


def _is_runtime_managed_name(name: str) -> bool:
    if name in _RUNTIME_MANAGED:
        return True
    if name.startswith("_variant1") or name.startswith("__"):
        return True
    if name == "_":
        return True
    return False


def _package_version(name: str) -> str:
    try:
        return str(importlib.metadata.version(name))
    except Exception:
        return ""


@functools.lru_cache(maxsize=1)
def serializer_registry() -> dict[str, Any]:
    """Return the versioned codec registry available in this exact runtime."""

    return serializer_registry_document({
        name: _package_version(name)
        for name in ("numpy", "pandas", "pyarrow", "safetensors")
    })


def _registry_by_id() -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id") or ""): dict(item)
        for item in serializer_registry()["serializers"]
        if isinstance(item, dict) and str(item.get("id") or "")
    }


def _coerce_limits(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    limits: dict[str, int] = {}
    for name, default in _DEFAULT_LIMITS.items():
        try:
            selected = int(raw.get(name, default))
        except Exception:
            selected = default
        limits[name] = max(0, selected)
    limits["max_response_bytes"] = max(1024, limits["max_response_bytes"])
    return limits


def _portable_tree_analysis(
    value: Any,
    *,
    max_depth: int,
    max_items: int,
) -> tuple[bool, str]:
    seen: set[int] = set()
    visited = 0

    def visit(item: Any, depth: int) -> tuple[bool, str]:
        nonlocal visited
        if depth > max_depth:
            return False, "max_depth_exceeded"
        visited += 1
        if visited > max_items:
            return False, "max_container_items_exceeded"
        if item is None or type(item) in {bool, int, str}:
            return True, ""
        if type(item) is float:
            return (True, "") if math.isfinite(item) else (False, "non_finite_float")
        if type(item) not in {tuple, list, dict}:
            return False, "unsupported_nested_type"
        identity = id(item)
        if identity in seen:
            return False, "recursive_value"
        seen.add(identity)
        try:
            if type(item) in {tuple, list}:
                for child in item:
                    valid, reason = visit(child, depth + 1)
                    if not valid:
                        return valid, reason
                return True, ""
            for key, child in item.items():
                if type(key) is not str:
                    return False, "non_string_json_key"
                valid, reason = visit(child, depth + 1)
                if not valid:
                    return valid, reason
            return True, ""
        finally:
            seen.remove(identity)

    return visit(value, 0)


def _portable_tree_encode(value: Any) -> dict[str, Any]:
    if value is None or type(value) in {bool, int, float, str}:
        return {"kind": "value", "value": value}
    if type(value) is tuple:
        return {
            "kind": "tuple",
            "items": [_portable_tree_encode(item) for item in value],
        }
    if type(value) is list:
        return {
            "kind": "list",
            "items": [_portable_tree_encode(item) for item in value],
        }
    if type(value) is dict:
        return {
            "kind": "dict",
            "items": [
                [key, _portable_tree_encode(value[key])]
                for key in sorted(value)
            ],
        }
    raise TypeError(f"unsupported portable tree value {_type_name(value)}")


def _portable_tree_decode(node: Any) -> Any:
    if not isinstance(node, dict):
        raise ValueError("portable tree node is not an object")
    kind = str(node.get("kind") or "")
    if kind == "value":
        value = node.get("value")
        if value is None or type(value) in {bool, int, float, str}:
            if type(value) is float and not math.isfinite(value):
                raise ValueError("portable tree contains a non-finite float")
            return value
        raise ValueError("portable tree primitive is invalid")
    items = node.get("items")
    if not isinstance(items, list):
        raise ValueError("portable tree items are invalid")
    if kind == "tuple":
        return tuple(_portable_tree_decode(item) for item in items)
    if kind == "list":
        return [_portable_tree_decode(item) for item in items]
    if kind == "dict":
        restored: dict[str, Any] = {}
        for item in items:
            if not isinstance(item, list) or len(item) != 2 or type(item[0]) is not str:
                raise ValueError("portable tree dictionary entry is invalid")
            restored[item[0]] = _portable_tree_decode(item[1])
        return restored
    raise ValueError(f"portable tree kind {kind!r} is unsupported")


def _dataclass_document(value: Any) -> dict[str, Any]:
    value_type = type(value)
    return {
        "schema": "variant1.dataclass-fields.v1",
        "module": str(value_type.__module__),
        "qualname": str(value_type.__qualname__),
        "fields": [
            [field.name, _portable_tree_encode(getattr(value, field.name))]
            for field in dataclasses.fields(value)
        ],
    }


def _resolve_qualname(module_name: str, qualname: str) -> Any:
    value: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        if not part or part == "<locals>":
            raise ValueError("dataclass qualified name is not importable")
        value = getattr(value, part)
    return value


_PANDAS_ARROW_METADATA_KEY = b"variant1:pandas-arrow"


def _pandas_arrow_table(value: Any) -> Any:
    import pyarrow as pa

    class_name = str(type(value).__name__ or "")
    document: dict[str, Any] = {
        "schema": "variant1.pandas-arrow.v1",
    }
    if class_name == "DataFrame":
        document["kind"] = "dataframe"
        table = pa.Table.from_pandas(value, preserve_index=True, safe=True)
    elif class_name == "Series":
        document.update({
            "kind": "series",
            "name": _portable_tree_encode(value.name),
        })
        table = pa.Table.from_pandas(
            value.to_frame(), preserve_index=True, safe=True
        )
    elif class_name == "RangeIndex":
        document.update({
            "kind": "range_index",
            "name": _portable_tree_encode(value.name),
            "start": int(value.start),
            "stop": int(value.stop),
            "step": int(value.step),
        })
        table = pa.table({})
    elif class_name == "MultiIndex":
        document.update({
            "kind": "multi_index",
            "names": _portable_tree_encode(tuple(value.names)),
        })
        table = pa.Table.from_pandas(
            value.to_frame(index=False), preserve_index=False, safe=True
        )
    elif class_name.endswith("Index"):
        document.update({
            "kind": "index",
            "name": _portable_tree_encode(value.name),
        })
        table = pa.Table.from_pandas(
            value.to_frame(index=False), preserve_index=False, safe=True
        )
    else:
        raise TypeError(f"unsupported pandas value {class_name!r}")
    metadata = dict(table.schema.metadata or {})
    metadata[_PANDAS_ARROW_METADATA_KEY] = _canonical_json_bytes(document)
    return table.replace_schema_metadata(metadata)


def _restore_pandas_arrow(table: Any) -> Any:
    import pandas as pd

    metadata = dict(table.schema.metadata or {})
    raw_document = metadata.get(_PANDAS_ARROW_METADATA_KEY)
    if not isinstance(raw_document, bytes):
        raise ValueError("pandas Arrow metadata is missing")
    document = json.loads(raw_document.decode("utf-8", errors="strict"))
    if (
        not isinstance(document, dict)
        or document.get("schema") != "variant1.pandas-arrow.v1"
    ):
        raise ValueError("pandas Arrow metadata is invalid")
    kind = str(document.get("kind") or "")
    if kind == "dataframe":
        return table.to_pandas()
    if kind == "series":
        frame = table.to_pandas()
        if len(frame.columns) != 1:
            raise ValueError("pandas Series payload has an invalid column count")
        value = frame.iloc[:, 0].copy()
        value.name = _portable_tree_decode(document.get("name"))
        return value
    if kind == "range_index":
        return pd.RangeIndex(
            start=int(document.get("start") or 0),
            stop=int(document.get("stop") or 0),
            step=int(document.get("step") or 1),
            name=_portable_tree_decode(document.get("name")),
        )
    frame = table.to_pandas()
    if kind == "multi_index":
        names = _portable_tree_decode(document.get("names"))
        if type(names) is not tuple:
            raise ValueError("pandas MultiIndex names are invalid")
        return pd.MultiIndex.from_frame(frame, names=list(names))
    if kind == "index":
        if len(frame.columns) != 1:
            raise ValueError("pandas Index payload has an invalid column count")
        return pd.Index(
            frame.iloc[:, 0],
            name=_portable_tree_decode(document.get("name")),
        )
    raise ValueError(f"pandas Arrow kind {kind!r} is unsupported")


def _serializer_for(
    value: Any,
    *,
    limits: dict[str, int],
) -> tuple[str, str]:
    if type(value) is str:
        return "text.utf8.v1", ""
    if type(value) is bytes:
        return "bytes.v1", ""
    if type(value) is tuple:
        valid, reason = _portable_tree_analysis(
            value,
            max_depth=limits["max_depth"],
            max_items=limits["max_container_items"],
        )
        return ("tuple.tree.v1", "") if valid else ("", reason)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        module_name = str(getattr(value_type, "__module__", "") or "")
        qualname = str(getattr(value_type, "__qualname__", "") or "")
        if module_name in {"", "__main__"} or "<locals>" in qualname:
            return "", "dataclass_type_not_importable"
        if any(not field.init for field in dataclasses.fields(value)):
            return "", "dataclass_non_init_field_unsupported"
        field_values = tuple(
            getattr(value, field.name) for field in dataclasses.fields(value)
        )
        valid, reason = _portable_tree_analysis(
            field_values,
            max_depth=limits["max_depth"],
            max_items=limits["max_container_items"],
        )
        return ("dataclass.fields.v1", "") if valid else ("", reason)
    module_name = str(type(value).__module__ or "")
    class_name = str(type(value).__name__ or "")
    registry = _registry_by_id()
    if (
        module_name.startswith("numpy")
        and class_name == "ndarray"
        and bool(registry["numpy.npy.v1"].get("available"))
    ):
        if bool(getattr(getattr(value, "dtype", None), "hasobject", False)):
            return "", "numpy_object_dtype_unsupported"
        return "numpy.npy.v1", ""
    if (
        module_name.startswith("pandas")
        and (
            class_name in {"DataFrame", "Series"}
            or class_name.endswith("Index")
        )
        and bool(registry["pandas.arrow.v1"].get("available"))
    ):
        return "pandas.arrow.v1", ""
    if (
        module_name.startswith("pyarrow")
        and class_name in {"Table", "RecordBatch"}
        and bool(registry["arrow.ipc.v1"].get("available"))
    ):
        return "arrow.ipc.v1", ""
    if type(value) is dict and value and bool(
        registry["safetensors.numpy.v1"].get("available")
    ):
        if all(
            type(key) is str
            and str(type(item).__module__).startswith("numpy")
            and str(type(item).__name__) == "ndarray"
            and not bool(getattr(getattr(item, "dtype", None), "hasobject", False))
            for key, item in value.items()
        ):
            return "safetensors.numpy.v1", ""
    valid, reason = _strict_json_analysis(
        value,
        max_depth=limits["max_depth"],
        max_items=limits["max_container_items"],
    )
    if valid:
        return "json.strict.v1", ""
    return "", reason or _excluded_reason(value)


def _serializer_requirements(value: Any, serializer: str) -> dict[str, Any]:
    descriptor = _registry_by_id().get(serializer) or {}
    requirements: dict[str, Any] = {
        "serializer_revision": int(descriptor.get("revision") or 0),
        "portable": bool(descriptor.get("portable")),
        "packages": dict(descriptor.get("packages") or {}),
    }
    if serializer == "dataclass.fields.v1":
        requirements.update({
            "python_major_minor": (
                f"{sys.version_info.major}.{sys.version_info.minor}"
            ),
            "module": str(type(value).__module__),
            "qualname": str(type(value).__qualname__),
            "workspace_sensitive": True,
            "environment_sensitive": True,
        })
    return requirements


def _encoded_chunks(value: Any, serializer: str) -> Iterable[bytes]:
    if serializer == "bytes.v1":
        yield value
        return
    if serializer == "text.utf8.v1":
        # Avoid one unbounded temporary encoding for very large strings.
        for start in range(0, len(value), 64 * 1024):
            yield value[start : start + 64 * 1024].encode("utf-8", errors="strict")
        return
    if serializer == "json.strict.v1":
        for chunk in _JSON_ENCODER.iterencode(value):
            yield chunk.encode("utf-8", errors="strict")
        return
    if serializer == "tuple.tree.v1":
        document = {
            "schema": "variant1.tuple-tree.v1",
            "root": _portable_tree_encode(value),
        }
        for chunk in _JSON_ENCODER.iterencode(document):
            yield chunk.encode("utf-8", errors="strict")
        return
    if serializer == "dataclass.fields.v1":
        for chunk in _JSON_ENCODER.iterencode(_dataclass_document(value)):
            yield chunk.encode("utf-8", errors="strict")
        return
    if serializer == "numpy.npy.v1":
        import numpy as np

        stream = io.BytesIO()
        np.save(stream, value, allow_pickle=False)
        yield stream.getvalue()
        return
    if serializer == "pandas.arrow.v1":
        import pyarrow as pa
        import pyarrow.ipc as ipc

        table = _pandas_arrow_table(value)
        sink = pa.BufferOutputStream()
        with ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
        yield sink.getvalue().to_pybytes()
        return
    if serializer == "arrow.ipc.v1":
        import pyarrow as pa
        import pyarrow.ipc as ipc

        table = (
            value
            if str(type(value).__name__) == "Table"
            else pa.Table.from_batches([value])
        )
        sink = pa.BufferOutputStream()
        with ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)
        yield sink.getvalue().to_pybytes()
        return
    if serializer == "safetensors.numpy.v1":
        from safetensors.numpy import save

        yield save({key: value[key] for key in sorted(value)})
        return
    raise ValueError(f"unsupported serializer {serializer!r}")


def _measure(value: Any, serializer: str, *, byte_limit: int) -> tuple[int, str]:
    total = 0
    if serializer == "numpy.npy.v1":
        raw_bytes = int(getattr(value, "nbytes", 0) or 0)
        if raw_bytes > max(0, int(byte_limit)):
            return raw_bytes, "capsule_value_too_large"
    elif serializer == "pandas.arrow.v1":
        try:
            if str(type(value).__name__) == "DataFrame":
                raw_bytes = int(value.memory_usage(index=True, deep=True).sum())
            else:
                usage = value.memory_usage(deep=True)
                raw_bytes = int(usage.sum() if hasattr(usage, "sum") else usage)
        except Exception:
            raw_bytes = 0
        if raw_bytes > max(0, int(byte_limit)):
            return raw_bytes, "capsule_value_too_large"
    elif serializer == "arrow.ipc.v1":
        raw_bytes = int(getattr(value, "nbytes", 0) or 0)
        if raw_bytes > max(0, int(byte_limit)):
            return raw_bytes, "capsule_value_too_large"
    elif serializer == "safetensors.numpy.v1":
        raw_bytes = sum(
            int(getattr(item, "nbytes", 0) or 0) for item in value.values()
        )
        if raw_bytes > max(0, int(byte_limit)):
            return raw_bytes, "capsule_value_too_large"
    try:
        for chunk in _encoded_chunks(value, serializer):
            total += len(chunk)
            if total > max(0, int(byte_limit)):
                return total, "capsule_value_too_large"
    except Exception:
        return total, "serialization_error"
    return total, ""


def _digest(value: Any, serializer: str) -> str:
    digest = hashlib.sha256()
    for chunk in _encoded_chunks(value, serializer):
        digest.update(chunk)
    return digest.hexdigest()


def _materialize(value: Any, serializer: str, expected_bytes: int) -> bytes:
    raw = b"".join(_encoded_chunks(value, serializer))
    if len(raw) != int(expected_bytes):
        raise RuntimeError("serializer byte count changed during capsule capture")
    return raw


def _worker_error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "schema": WORKER_CAPSULE_SCHEMA,
        "error": {
            "code": str(code),
            "message": str(message),
            "details": dict(details),
        },
    }


class KernelCapsuleWorker:
    """Own the user-namespace portion of the capsule worker protocol."""

    def __init__(
        self,
        context: Any,
        *,
        reinstall_namespace: Callable[[dict[str, Any]], None],
        document: dict[str, Any],
    ) -> None:
        self.context = context
        self._reinstall_namespace = reinstall_namespace
        self._document = json.loads(json.dumps(document))
        self._protected_names: set[str] = set()
        self.refresh_protected_names()

    def update_document(self, document: dict[str, Any]) -> None:
        self._document = json.loads(json.dumps(document))
        self.refresh_protected_names()

    def refresh_protected_names(self) -> None:
        # Ownership belongs to declared runtime roots, not to every variable
        # that has ever held a proxy. User aliases are excluded by value type
        # while live and become ordinary snapshot values when reassigned.
        self._protected_names = {
            "Variant1CapabilityError",
            "Variant1CapabilityFailure",
            "toolbelt",
            "tools",
            *_RUNTIME_MANAGED,
        }
        python_apis = self._document.get("python_apis")
        if isinstance(python_apis, dict):
            self._protected_names.update(str(name) for name in python_apis)
        mounted_objects = self._document.get("mounted_objects")
        if isinstance(mounted_objects, dict):
            self._protected_names.update(str(name) for name in mounted_objects)

    def _name_state(self, raw_name: Any, value: Any) -> tuple[str, str, str]:
        name = str(raw_name)
        type_name = _type_name(value)
        if name in self._protected_names:
            return name, type_name, "host_owned_namespace"
        if _is_runtime_managed_name(name):
            return name, type_name, "runtime_managed"
        if not name.isidentifier() or keyword.iskeyword(name):
            return name, type_name, "invalid_variable_name"
        return name, type_name, ""

    def inspect(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return names/types/serializers without encoding or hashing values."""

        raw_request = request if isinstance(request, dict) else {}
        limits = _coerce_limits(raw_request.get("limits"))
        try:
            row_limit = max(1, min(int(raw_request.get("limit") or 100), 500))
        except Exception:
            row_limit = 100
        values: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        total_rows = 0
        truncated = False
        namespace = self.context.namespace
        for raw_name in sorted(namespace, key=str):
            value = namespace[raw_name]
            name, type_name, excluded_reason = self._name_state(raw_name, value)
            serializer = ""
            if not excluded_reason:
                serializer, excluded_reason = _serializer_for(value, limits=limits)
                if not serializer and excluded_reason == "unsupported_type":
                    excluded_reason = _excluded_reason(value)
            if total_rows >= row_limit:
                truncated = True
                continue
            total_rows += 1
            if serializer:
                known_bytes = len(value) if type(value) is bytes else None
                values.append({
                    "name": name,
                    "type": type_name,
                    "serializer": serializer,
                    "bytes": known_bytes,
                    "size_known_without_serialization": known_bytes is not None,
                })
            else:
                excluded.append({
                    "name": name,
                    "type": type_name,
                    "reason": excluded_reason or _excluded_reason(value),
                })
        return {
            "schema": WORKER_NAMESPACE_SCHEMA,
            "values": values,
            "excluded": excluded,
            "truncated": truncated,
            "serializer_registry": serializer_registry(),
        }

    def resource_snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        """Return bounded worker/process pressure and shallow namespace sizes."""

        try:
            import psutil

            process = psutil.Process()
            memory = process.memory_info()
            cpu = process.cpu_times()
            rss_bytes = int(memory.rss)
            virtual_bytes = int(memory.vms)
            cpu_user_s = float(cpu.user)
            cpu_system_s = float(cpu.system)
            threads = int(process.num_threads())
        except Exception:
            rss_bytes = 0
            virtual_bytes = 0
            cpu_user_s = 0.0
            cpu_system_s = 0.0
            threads = 0

        contributors: list[dict[str, Any]] = []
        namespace = self.context.namespace
        for raw_name in sorted(namespace, key=str):
            value = namespace[raw_name]
            name, type_name, excluded = self._name_state(raw_name, value)
            if excluded:
                continue
            value_type = type(value)
            module_name = str(value_type.__module__ or "")
            estimate = 0
            basis = "unknown"
            if value_type in {
                bool,
                bytes,
                dict,
                float,
                int,
                list,
                str,
                tuple,
            }:
                estimate = max(0, int(sys.getsizeof(value)))
                basis = "shallow_builtin"
            elif module_name.startswith(("numpy", "pyarrow")):
                raw_nbytes = getattr(value, "nbytes", 0)
                if isinstance(raw_nbytes, int) and not isinstance(raw_nbytes, bool):
                    estimate = max(0, int(raw_nbytes))
                    basis = "library_nbytes"
            elif module_name.startswith("pandas"):
                try:
                    usage = value.memory_usage(deep=True)
                    estimate = max(
                        0,
                        int(usage.sum() if hasattr(usage, "sum") else usage),
                    )
                    basis = "pandas_memory_usage"
                except Exception:
                    estimate = 0
            contributors.append({
                "name": name,
                "type": type_name,
                "estimated_bytes": estimate,
                "basis": basis,
            })
        contributors.sort(
            key=lambda item: (-int(item["estimated_bytes"]), item["name"])
        )
        cap = max(1, min(int(limit or 20), 100))
        return {
            "schema": "variant1.kernel-resource-snapshot.v1",
            "process": {
                "rss_bytes": rss_bytes,
                "virtual_bytes": virtual_bytes,
                "cpu_user_s": cpu_user_s,
                "cpu_system_s": cpu_system_s,
                "threads": threads,
            },
            "namespace": {
                "values": len(contributors),
                "estimated_bytes": sum(
                    int(item["estimated_bytes"]) for item in contributors
                ),
                "contributors": contributors[:cap],
                "contributors_truncated": len(contributors) > cap,
            },
            "runtime_profile": dict(
                getattr(self.context, "runtime_profile", {}) or {}
            ),
        }

    def capture(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        raw_request = request if isinstance(request, dict) else {}
        if raw_request and str(raw_request.get("schema") or "") not in {
            "",
            WORKER_CAPTURE_REQUEST_SCHEMA,
        }:
            return _worker_error(
                "capsule_capture_request_unsupported",
                "Kernel capsule capture request schema is unsupported.",
            )
        limits = _coerce_limits(raw_request.get("limits"))
        known_rows = raw_request.get("known_values")
        known: dict[str, dict[str, Any]] = {}
        if isinstance(known_rows, list):
            for item in known_rows[: limits["max_values"]]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if name and name not in known:
                    known[name] = dict(item)

        namespace = self.context.namespace
        values: list[dict[str, Any]] = []
        excluded: list[dict[str, str]] = []
        total_bytes = 0
        estimated_response_bytes = 2048
        reused_values = 0

        for raw_name in sorted(namespace, key=str):
            name = str(raw_name)
            value = namespace[raw_name]
            name, type_name, excluded_reason = self._name_state(raw_name, value)
            if excluded_reason:
                if len(excluded) >= limits["max_excluded_values"]:
                    return _worker_error(
                        "capsule_exclusion_limit",
                        "Kernel capsule exclusion ledger exceeds its worker bound.",
                        count=len(excluded) + 1,
                    )
                entry = {
                    "name": name,
                    "type": type_name,
                    "reason": excluded_reason,
                }
                estimated_response_bytes += len(_canonical_json_bytes(entry)) + 1
                if estimated_response_bytes > limits["max_response_bytes"]:
                    return _worker_error(
                        "capsule_response_too_large",
                        "Kernel capsule response exceeds its worker byte bound.",
                    )
                excluded.append(entry)
                continue

            serializer, serializer_reason = _serializer_for(value, limits=limits)
            if not serializer:
                if len(excluded) >= limits["max_excluded_values"]:
                    return _worker_error(
                        "capsule_exclusion_limit",
                        "Kernel capsule exclusion ledger exceeds its worker bound.",
                        count=len(excluded) + 1,
                    )
                entry = {
                    "name": name,
                    "type": type_name,
                    "reason": (
                        _excluded_reason(value)
                        if serializer_reason == "unsupported_type"
                        else serializer_reason
                    ),
                }
                estimated_response_bytes += len(_canonical_json_bytes(entry)) + 1
                if estimated_response_bytes > limits["max_response_bytes"]:
                    return _worker_error(
                        "capsule_response_too_large",
                        "Kernel capsule response exceeds its worker byte bound.",
                    )
                excluded.append(entry)
                continue

            if len(values) >= limits["max_values"]:
                return _worker_error(
                    "capsule_value_limit",
                    "Kernel capsule contains too many serializable values.",
                    count=len(values) + 1,
                )
            size, measure_error = _measure(
                value,
                serializer,
                byte_limit=limits["max_value_bytes"],
            )
            if measure_error == "capsule_value_too_large":
                return _worker_error(
                    "capsule_value_too_large",
                    f"Kernel capsule value {name!r} exceeds its worker byte bound.",
                    name=name,
                    measured_bytes=size,
                    max_value_bytes=limits["max_value_bytes"],
                )
            if measure_error:
                if len(excluded) >= limits["max_excluded_values"]:
                    return _worker_error(
                        "capsule_exclusion_limit",
                        "Kernel capsule exclusion ledger exceeds its worker bound.",
                    )
                excluded.append({
                    "name": name,
                    "type": type_name,
                    "reason": measure_error,
                })
                continue
            if total_bytes + size > limits["max_total_value_bytes"]:
                return _worker_error(
                    "capsule_total_too_large",
                    "Kernel capsule values exceed the worker total byte bound.",
                    total_bytes=total_bytes + size,
                    max_total_value_bytes=limits["max_total_value_bytes"],
                )
            total_bytes += size
            try:
                sha256 = _digest(value, serializer)
            except Exception:
                excluded.append({
                    "name": name,
                    "type": type_name,
                    "reason": "serialization_error",
                })
                continue
            previous = known.get(name) or {}
            reused = (
                str(previous.get("serializer") or "") == serializer
                and str(previous.get("sha256") or "") == sha256
                and int(previous.get("bytes") or -1) == size
                and bool(str(previous.get("artifact_ref") or ""))
            )
            entry: dict[str, Any] = {
                "name": name,
                "type": type_name,
                "serializer": serializer,
                "sha256": sha256,
                "bytes": size,
                "requirements": _serializer_requirements(value, serializer),
                "reused": reused,
            }
            if reused:
                entry["artifact_ref"] = str(previous["artifact_ref"])
                reused_values += 1
            else:
                encoded_length = 4 * ((size + 2) // 3)
                projected_entry = {**entry, "data_b64": ""}
                estimated = (
                    estimated_response_bytes
                    + len(_canonical_json_bytes(projected_entry))
                    + encoded_length
                    + 1
                )
                if estimated > limits["max_response_bytes"]:
                    return _worker_error(
                        "capsule_response_too_large",
                        "Kernel capsule response exceeds its worker byte bound.",
                        name=name,
                    )
                try:
                    raw = _materialize(value, serializer, size)
                except Exception:
                    return _worker_error(
                        "capsule_value_changed_during_capture",
                        f"Kernel capsule value {name!r} changed during capture.",
                        name=name,
                    )
                if hashlib.sha256(raw).hexdigest() != sha256:
                    return _worker_error(
                        "capsule_value_changed_during_capture",
                        f"Kernel capsule value {name!r} changed during capture.",
                        name=name,
                    )
                entry["data_b64"] = base64.b64encode(raw).decode("ascii")
            estimated_response_bytes += len(_canonical_json_bytes(entry)) + 1
            if estimated_response_bytes > limits["max_response_bytes"]:
                return _worker_error(
                    "capsule_response_too_large",
                    "Kernel capsule response exceeds its worker byte bound.",
                )
            values.append(entry)

        response = {
            "schema": WORKER_CAPSULE_SCHEMA,
            "python": {
                "implementation": platform.python_implementation(),
                "version": platform.python_version(),
                "major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
            },
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python_executable_name": __import__("os").path.basename(sys.executable),
            },
            "serializer_registry": serializer_registry(),
            "values": values,
            "excluded": excluded,
            "incremental": {
                "known_values": len(known),
                "reused_values": reused_values,
                "materialized_values": len(values) - reused_values,
            },
            "bounds": {
                "values": len(values),
                "excluded_values": len(excluded),
                "total_value_bytes": total_bytes,
                "estimated_response_bytes": estimated_response_bytes,
            },
        }
        # This final check verifies the estimate; all body admission decisions
        # happened before the complete response was constructed.
        if len(_canonical_json_bytes(response)) > limits["max_response_bytes"]:
            return _worker_error(
                "capsule_response_too_large",
                "Kernel capsule response exceeds its worker byte bound.",
            )
        return response

    def restore(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("schema") != WORKER_CAPSULE_SCHEMA:
            raise ValueError("kernel capsule restore payload schema is unsupported")
        raw_values = payload.get("values")
        if not isinstance(raw_values, list):
            raise ValueError("kernel capsule restore values must be a list")

        restored: dict[str, Any] = {}
        for item in raw_values:
            if not isinstance(item, dict):
                raise ValueError("kernel capsule value entry must be an object")
            name = str(item.get("name") or "")
            if (
                not name.isidentifier()
                or keyword.iskeyword(name)
                or name in self._protected_names
                or _is_runtime_managed_name(name)
            ):
                raise ValueError(f"kernel capsule variable {name!r} is not restorable")
            try:
                raw = base64.b64decode(str(item.get("data_b64") or ""), validate=True)
            except Exception as exc:
                raise ValueError(
                    f"kernel capsule variable {name!r} has invalid base64"
                ) from exc
            expected_bytes = int(item.get("bytes") or 0)
            expected_hash = str(item.get("sha256") or "")
            if len(raw) != expected_bytes or hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ValueError(
                    f"kernel capsule variable {name!r} failed integrity verification"
                )
            serializer = str(item.get("serializer") or "")
            if serializer == "text.utf8.v1":
                value = raw.decode("utf-8", errors="strict")
            elif serializer == "bytes.v1":
                value = raw
            elif serializer == "json.strict.v1":
                value = json.loads(raw.decode("utf-8", errors="strict"))
                if not _strict_json_value(value):
                    raise ValueError(
                        f"kernel capsule variable {name!r} is not strict JSON"
                    )
            elif serializer == "tuple.tree.v1":
                document = json.loads(raw.decode("utf-8", errors="strict"))
                if (
                    not isinstance(document, dict)
                    or document.get("schema") != "variant1.tuple-tree.v1"
                ):
                    raise ValueError(
                        f"kernel capsule variable {name!r} has an invalid tuple document"
                    )
                value = _portable_tree_decode(document.get("root"))
                if type(value) is not tuple:
                    raise ValueError(
                        f"kernel capsule variable {name!r} did not restore as a tuple"
                    )
            elif serializer == "dataclass.fields.v1":
                document = json.loads(raw.decode("utf-8", errors="strict"))
                if (
                    not isinstance(document, dict)
                    or document.get("schema") != "variant1.dataclass-fields.v1"
                ):
                    raise ValueError(
                        f"kernel capsule variable {name!r} has an invalid dataclass document"
                    )
                value_type = _resolve_qualname(
                    str(document.get("module") or ""),
                    str(document.get("qualname") or ""),
                )
                if not dataclasses.is_dataclass(value_type):
                    raise ValueError(
                        f"kernel capsule variable {name!r} type is not a dataclass"
                    )
                raw_fields = document.get("fields")
                if not isinstance(raw_fields, list):
                    raise ValueError("dataclass field ledger is invalid")
                field_values: dict[str, Any] = {}
                for field_row in raw_fields:
                    if (
                        not isinstance(field_row, list)
                        or len(field_row) != 2
                        or type(field_row[0]) is not str
                    ):
                        raise ValueError("dataclass field entry is invalid")
                    field_values[field_row[0]] = _portable_tree_decode(field_row[1])
                expected_fields = {
                    field.name for field in dataclasses.fields(value_type) if field.init
                }
                if set(field_values) != expected_fields:
                    raise ValueError("dataclass field set does not match its type")
                value = value_type(**field_values)
            elif serializer == "numpy.npy.v1":
                import numpy as np

                value = np.load(io.BytesIO(raw), allow_pickle=False)
            elif serializer == "pandas.arrow.v1":
                import pyarrow.ipc as ipc

                value = _restore_pandas_arrow(
                    ipc.open_file(io.BytesIO(raw)).read_all()
                )
            elif serializer == "arrow.ipc.v1":
                import pyarrow as pa
                import pyarrow.ipc as ipc

                table = ipc.open_file(io.BytesIO(raw)).read_all()
                if str(item.get("type") or "").endswith(".RecordBatch"):
                    batches = table.combine_chunks().to_batches()
                    value = batches[0] if batches else pa.RecordBatch.from_arrays(
                        [pa.array([], type=field.type) for field in table.schema],
                        schema=table.schema,
                    )
                else:
                    value = table
            elif serializer == "safetensors.numpy.v1":
                from safetensors.numpy import load

                value = load(raw)
            else:
                raise ValueError(
                    f"kernel capsule variable {name!r} uses unsupported serializer "
                    f"{serializer!r}"
                )
            restored[name] = value

        namespace = self.context.namespace
        prior_user_values: dict[Any, Any] = {}
        for raw_name in list(namespace):
            name = str(raw_name)
            if name in self._protected_names or _is_runtime_managed_name(name):
                continue
            prior_user_values[raw_name] = namespace[raw_name]
        removed = [str(name) for name in prior_user_values]
        try:
            for raw_name in prior_user_values:
                namespace.pop(raw_name, None)
            namespace.update(restored)
            self._reinstall_namespace(json.loads(json.dumps(self._document)))
            self.refresh_protected_names()
        except BaseException as restore_error:
            rollback_ok = False
            try:
                for raw_name in list(namespace):
                    name = str(raw_name)
                    if name in self._protected_names or _is_runtime_managed_name(name):
                        continue
                    namespace.pop(raw_name, None)
                namespace.update(prior_user_values)
                self._reinstall_namespace(json.loads(json.dumps(self._document)))
                self.refresh_protected_names()
                rollback_ok = True
            except BaseException:
                rollback_ok = False
            if rollback_ok:
                raise RuntimeError(
                    "capsule_restore_rolled_back: namespace reinstall failed"
                ) from restore_error
            raise RuntimeError(
                "capsule_restore_unknown_effect: restore and rollback both failed"
            ) from restore_error
        return {
            "schema": WORKER_CAPSULE_SCHEMA,
            "restored_names": sorted(restored),
            "removed_names": sorted(removed),
            "namespace_reinstalled": True,
        }


__all__ = [
    "KernelCapsuleWorker",
    "SERIALIZER_REGISTRY_SCHEMA",
    "WORKER_CAPSULE_SCHEMA",
    "WORKER_CAPTURE_REQUEST_SCHEMA",
    "WORKER_NAMESPACE_SCHEMA",
    "_strict_json_value",
    "serializer_registry",
]
