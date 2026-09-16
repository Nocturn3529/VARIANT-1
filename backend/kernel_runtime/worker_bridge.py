"""Kernel-side typed proxies for the authenticated host capability bridge."""

from __future__ import annotations

import asyncio
import ast
import base64
from collections.abc import Mapping
import contextvars
from contextlib import suppress
from dataclasses import dataclass, field
import inspect
import json
import math
import keyword
import os
import socket
import sys
import textwrap
import threading
import types
import typing
import uuid
import weakref
from typing import Any

from core_invariants import canonical_json as _canonical_json
from .bridge_protocol import (
    BRIDGE_HANDSHAKE,
    BRIDGE_SCHEMA,
    DEFAULT_MAX_FRAME_BYTES,
    BridgeProtocolError,
    read_async_response,
    read_sync_response,
    sign_envelope,
    verify_envelope,
    write_async_frame,
    write_sync_frame,
)
from .wire_values import unpack_value


@dataclass(frozen=True, slots=True)
class Variant1CapabilityFailure:
    """The latest capability failure, retained locally for direct adaptation."""

    descriptor: dict[str, Any] = field(repr=False)
    arguments: dict[str, Any]
    code: str
    message: str
    receipt: dict[str, Any] = field(default_factory=dict, repr=False)
    result: Any = field(default=None, repr=False)

    @property
    def target(self) -> str:
        return self.qualified_name

    @property
    def qualified_name(self) -> str:
        qualified = str(self.descriptor.get("qualified_alias") or "")
        if qualified:
            if "." in qualified:
                return qualified
            namespace = str(self.descriptor.get("namespace") or "tools")
            return (
                f"{namespace}.{qualified}"
                if namespace and namespace != "tools"
                else f"tools.{qualified}"
            )
        namespace = str(self.descriptor.get("namespace") or "tools")
        alias = str(
            self.descriptor.get("alias")
            or self.descriptor.get("capability_id")
            or self.descriptor.get("ref_id")
            or "capability"
        )
        return (
            f"{namespace}.{alias}"
            if namespace and namespace != "tools"
            else f"tools.{alias}"
        )

    def __repr__(self) -> str:
        arguments = repr(self.arguments)
        if len(arguments) > 1_200:
            arguments = arguments[:1_197] + "..."
        message = " ".join(str(self.message or "").split())
        if len(message) > 400:
            message = message[:397] + "..."
        return (
            "CapabilityFailure("
            f"target={self.qualified_name!r}, code={self.code!r}, "
            f"arguments={arguments}, message={message!r})"
        )


class Variant1CapabilityError(RuntimeError):
    """Structured host capability failure visible to model-authored code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        receipt: dict | None = None,
        failure: Variant1CapabilityFailure | None = None,
    ):
        super().__init__(message)
        self.code = str(code or "capability_error")
        self.receipt = dict(receipt or {})
        self.failure = failure


REMOTE_HANDLE_KEY = "$variant1_handle"
REMOTE_HANDLE_DISPATCH_SCHEMA = "variant1.remote-handle-dispatch.v1"
REMOTE_HANDLE_METHODS_SCHEMA = "variant1.remote-handle-methods.v1"


@dataclass(frozen=True, slots=True)
class _ExecutionOrigin:
    """Immutable admission identity captured before one model cell runs."""

    execution_id: str
    outer_tool_call_id: str
    generation: int

_REMOTE_HANDLE_IDENTITY_FIELDS = (
    "service",
    "kind",
    "id",
    "generation",
    "revision",
)
_REMOTE_HANDLE_DISPATCH_FIELDS = (
    "ref_id",
    "handler_revision",
    "catalog_release_id",
    "category_id",
    "slot_id",
    "slot_version",
    "mount_revision",
)
_SENSITIVE_HANDLE_METADATA_KEYS = frozenset({
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
})
_REMOTE_HANDLE_ROUTERS: weakref.WeakValueDictionary[str, Any] = (
    weakref.WeakValueDictionary()
)
_REMOTE_HANDLE_ROUTERS_LOCK = threading.Lock()


def _contains_sensitive_metadata(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _SENSITIVE_HANDLE_METADATA_KEYS:
                return True
            if _contains_sensitive_metadata(item):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_metadata(item) for item in value)
    return False


def _remote_handle_router_id(bridge: Any) -> str:
    router_id = getattr(bridge, "_variant1_remote_handle_router_id", None)
    if not isinstance(router_id, str) or not router_id:
        router_id = f"router_{uuid.uuid4().hex}"
        setattr(bridge, "_variant1_remote_handle_router_id", router_id)
    with _REMOTE_HANDLE_ROUTERS_LOCK:
        _REMOTE_HANDLE_ROUTERS[router_id] = bridge
    return router_id


def _remote_handle_bridge(router_id: str) -> Any:
    with _REMOTE_HANDLE_ROUTERS_LOCK:
        bridge = _REMOTE_HANDLE_ROUTERS.get(router_id)
    if bridge is None:
        raise Variant1CapabilityError(
            "remote_handle_detached",
            "This VARIANT-1 remote handle is detached from its kernel session.",
        )
    return bridge


def _remote_method_arguments(
    qualified_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    if not args:
        return dict(kwargs)
    if len(args) == 1 and not kwargs and isinstance(args[0], dict):
        return dict(args[0])
    raise TypeError(
        f"{qualified_name} accepts keyword arguments or one argument dictionary"
    )


def _signature_for_remote_method(descriptor: dict[str, Any], *, positional: bool = False) -> inspect.Signature:
    parameters: list[inspect.Parameter] = []
    raw_parameters = descriptor.get("params")
    for item in raw_parameters if isinstance(raw_parameters, list) else ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name.isidentifier() or keyword.iskeyword(name):
            continue
        required = bool(item.get("required"))
        default = (
            inspect.Parameter.empty
            if required
            else item.get("default", None)
        )
        annotation = str(item.get("type") or "") or inspect.Parameter.empty
        parameters.append(inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD if positional else inspect.Parameter.KEYWORD_ONLY,
            default=default,
            annotation=annotation,
        ))
    if bool(descriptor.get("variadic_kwargs")):
        parameters.append(inspect.Parameter(
            "options",
            inspect.Parameter.VAR_KEYWORD,
        ))
    return_annotation = (
        str(descriptor.get("returns") or "") or inspect.Signature.empty
    )
    return inspect.Signature(
        parameters=parameters,
        return_annotation=return_annotation,
    )


class _RemoteHandleMethods(tuple):
    """Descriptor tuple that also matches mounted-object ``methods()`` UX."""

    def __new__(cls, values: Any = ()) -> "_RemoteHandleMethods":
        return super().__new__(cls, tuple(dict(item) for item in values))

    def __call__(self) -> list[str]:
        return [str(item.get("name") or "") for item in self if item.get("name")]


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return repr(value)


def _decoded_json_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{\"-0123456789tfn":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _bounded_display_text(value: Any, *, limit: int = 4_000) -> str:
    """Return one readable, bounded projection without changing stored data."""

    text = str(value or "")
    if len(text) <= max(0, int(limit)):
        return text
    omitted = len(text) - max(0, int(limit))
    return text[:max(0, int(limit))] + f"\n... <{omitted} chars omitted from display>"


class _CallableRawMapping(dict):
    """A raw result view compatible with both ``value.raw`` and ``value.raw()``."""

    def __call__(self) -> dict[str, Any]:
        return {key: value for key, value in dict.items(self)}


class Variant1CommandResult(dict):
    """Lossless command mapping with a compact Python-facing view."""

    @property
    def raw(self) -> _CallableRawMapping:
        return _CallableRawMapping(dict.items(self))

    @property
    def ok(self) -> bool:
        return bool(dict.get(self, "ok"))

    def __getattr__(self, name: str) -> Any:
        """Expose structured fields without giving up ordinary mapping access."""

        if str(name).startswith("_"):
            raise AttributeError(name)
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            raise AttributeError(name) from None

    def __dir__(self) -> list[str]:
        fields = {
            str(key) for key in dict.keys(self)
            if str(key).isidentifier() and not str(key).startswith("_")
        }
        return sorted(set(super().__dir__()) | fields)

    def __repr__(self) -> str:
        details = [
            f"ok={self.ok!r}",
            f"exit_code={dict.get(self, 'exit_code')!r}",
        ]
        duration = dict.get(self, "duration_s")
        if duration is not None:
            details.append(f"duration_s={duration!r}")
        if dict.get(self, 'owned_descendants_running'):
            details.append('owned_descendants_running=True')
        stdout = _bounded_display_text(dict.get(self, "stdout"))
        stderr = _bounded_display_text(dict.get(self, "stderr"))
        if stdout:
            details.append(f"stdout={stdout!r}")
        if stderr:
            details.append(f"stderr={stderr!r}")
        process = dict.get(self, "process")
        if process is not None:
            details.append(f"process={process!r}")
        artifact_refs = dict.get(self, "artifact_refs")
        if artifact_refs:
            details.append(f"artifact_refs={artifact_refs!r}")
        if bool(dict.get(self, "truncated")):
            details.append("truncated=True")
        return f"CommandResult({', '.join(details)})"

    def __str__(self) -> str:
        return repr(self)


class Variant1FileReadResult(dict):
    """Explicit file window or CAS-backed complete read."""

    def __getattr__(self, name: str) -> Any:
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            raise AttributeError(name) from None

    def __str__(self) -> str:
        return repr(self)

    def __repr__(self) -> str:
        details = (
            f"offset={dict.get(self, 'offset')!r}, end={dict.get(self, 'end')!r}, "
            f"total_lines={dict.get(self, 'total_lines')!r}, "
            f"complete_file={dict.get(self, 'complete_file')!r}, "
            f"next_offset={dict.get(self, 'next_offset')!r}"
        )
        text = dict.get(self, "text")
        if isinstance(text, str):
            return f"FileReadResult({details}, text={_bounded_display_text(text)!r})"
        return f"FileReadResult({details}, artifact_ref={dict.get(self, 'artifact_ref')!r})"

class Variant1McpResult(dict):
    """Mapping-compatible MCP value with one non-duplicated model display."""

    def __getitem__(self, key: Any) -> Any:
        if key == "data":
            return self.data
        if key == "raw":
            return self.raw
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            data = self.data
            if isinstance(data, Mapping):
                return data[key]
            raise

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if key in {"data", "raw"} or dict.__contains__(self, key):
            return True
        data = self.data
        return isinstance(data, Mapping) and key in data

    @property
    def ok(self) -> bool:
        return not bool(dict.get(self, "is_error"))

    @property
    def data(self) -> Any:
        structured = dict.get(self, "structured_content")
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return _decoded_json_string(structured.get("result"))
        if structured is not None:
            return structured
        blocks = dict.get(self, "content")
        texts = [
            str(block.get("text") or "")
            for block in blocks if isinstance(block, dict) and block.get("text") is not None
        ] if isinstance(blocks, list) else []
        if len(texts) == 1:
            return _decoded_json_string(texts[0])
        if texts:
            return texts
        return None

    @property
    def raw(self) -> _CallableRawMapping:
        return _CallableRawMapping(dict.items(self))

    @property
    def next_capability(self) -> Any:
        return dict.get(self, "next_capability")

    @property
    def content_types(self) -> list[str]:
        blocks = dict.get(self, "content")
        if not isinstance(blocks, list):
            return []
        return [
            str(block.get("type") or "unknown")
            for block in blocks if isinstance(block, dict)
        ]

    def __repr__(self) -> str:
        details = [f"ok={self.ok!r}"]
        data = self.data
        if data is not None:
            details.append(f"data={_compact_json(data)}")
        non_text = [kind for kind in self.content_types if kind != "text"]
        if non_text:
            details.append(f"content_types={non_text!r}")
        if self.next_capability is not None:
            details.append(f"next_capability={self.next_capability!r}")
        return f"MCPResult({', '.join(details)})"

    def __str__(self) -> str:
        return repr(self)

class Variant1McpSchema(dict):
    """Full MCP schema mapping with a compact exact-call display."""

    @property
    def descriptor(self) -> dict[str, Any]:
        value = self.get("descriptor")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def name(self) -> str:
        return str(self.get("name") or self.descriptor.get("name") or "")

    def __repr__(self) -> str:
        descriptor = self.descriptor
        input_schema = descriptor.get(
            "inputSchema", descriptor.get("input_schema")
        )
        properties = (
            input_schema.get("properties")
            if isinstance(input_schema, dict)
            and isinstance(input_schema.get("properties"), dict)
            else {}
        )
        required = {
            str(item)
            for item in (
                input_schema.get("required")
                if isinstance(input_schema, dict)
                and isinstance(input_schema.get("required"), list)
                else []
            )
        }
        lines = [f"MCPToolSchema(name={self.name!r})"]
        description = " ".join(
            str(descriptor.get("description") or "").split()
        )
        if description:
            lines.append(f"description: {description}")
        lines.append("invoke(arguments={")
        for parameter, raw_spec in properties.items():
            spec = raw_spec if isinstance(raw_spec, dict) else {}
            kind = str(spec.get("type") or "any")
            qualifiers = ["required"] if str(parameter) in required else []
            if "default" in spec and str(parameter) not in required:
                qualifiers.append(f"default={spec.get('default')!r}")
            if isinstance(spec.get("enum"), list):
                qualifiers.append("enum=" + _compact_json(spec.get("enum")))
            suffix = f" ({', '.join(qualifiers)})" if qualifiers else ""
            lines.append(f"  {parameter}: {kind}{suffix}")
        lines.append("})")
        output_schema = descriptor.get(
            "outputSchema", descriptor.get("output_schema")
        )
        if output_schema is not None:
            lines.append("returns: " + _compact_json(output_schema))
        return "\n".join(lines)

    def __str__(self) -> str:
        return repr(self)

class Variant1ConnectorMatch(dict):
    """Mapping-compatible primary MCP match with direct bound operations."""

    @property
    def handle(self) -> Any:
        return self.get("handle")

    def schema(self) -> Any:
        """Return the schema included in the search disclosure event."""

        return self.get("schema")

    @property
    def cancel(self) -> Any:
        """Use the primary handle's exact cancel method, including async_."""
        return self.handle.cancel

    def methods(self) -> list[str]:
        """Delegate method discovery to the already-bound primary handle."""

        handle = self.handle
        methods = getattr(handle, "methods", None)
        if methods is None:
            return []
        values = methods() if callable(methods) else methods
        if isinstance(values, (list, tuple)):
            return [
                str(item.get("name") or "") if isinstance(item, Mapping)
                else str(item)
                for item in values
                if (
                    item.get("name") if isinstance(item, Mapping)
                    else str(item)
                )
            ]
        return []

    def invoke(self, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Invoke the already-bound primary handle without dictionary ceremony."""

        handle = self.handle
        method = getattr(handle, "invoke", None)
        if not callable(method):
            raise AttributeError("connector top match has no callable MCP handle")
        if arguments is None:
            arguments = {}
        return method(arguments=arguments, **kwargs)

    def __repr__(self) -> str:
        handle = self.handle
        metadata = (
            handle.metadata
            if isinstance(handle, Variant1RemoteHandle)
            else {}
        )
        schema = self.schema()
        name = str(
            getattr(schema, "name", "")
            or metadata.get("name")
            or ""
        )
        server = str(metadata.get("server_id") or "")
        identity = [f"name={name!r}"] if name else []
        if server:
            identity.append(f"server={server!r}")
        lines = [f"ConnectorMatch({', '.join(identity)})"]
        if schema is not None:
            lines.extend(str(repr(schema)).splitlines())
        lines.append(
            "ready: .invoke(arguments={...}, conclude=False); "
            ".schema() only if the lease becomes stale; "
            "use .handle.invoke.async_(..., request_id=...) and .cancel.async_(request_id=...) for cancellable work"
        )
        return "\n".join(lines)

    def __str__(self) -> str:
        return repr(self)

class Variant1ConnectorSearchResult(dict):
    """Connector search mapping whose display names every returned handle."""

    def __init__(self, value: dict[str, Any] | None = None, **kwargs: Any):
        super().__init__(value or {}, **kwargs)
        top = self.get("top_match")
        if isinstance(top, dict) and not isinstance(top, Variant1ConnectorMatch):
            dict.__setitem__(self, "top_match", Variant1ConnectorMatch(top))

    @property
    def mcp(self) -> list[Any]:
        value = self.get("mcp")
        return value if isinstance(value, list) else []

    @property
    def plugins(self) -> list[Any]:
        value = self.get("plugins")
        return value if isinstance(value, list) else []

    @property
    def top_match(self) -> Variant1ConnectorMatch | None:
        value = self.get("top_match")
        return value if isinstance(value, Variant1ConnectorMatch) else None

    def __repr__(self) -> str:
        lines = ["ConnectorSearchResult"]
        if self.top_match is not None:
            lines.append("top_match [0] (schema included):")
            lines.extend(
                "  " + line for line in repr(self.top_match).splitlines()
            )
            secondary = self.mcp[1:]
            if secondary:
                lines.append("secondary_mcp (call schema() before invoke()):")
                lines.extend(
                    f"  [{index}] {value!r}"
                    for index, value in enumerate(secondary, start=1)
                )
        elif self.mcp:
            lines.append("mcp:")
            lines.extend(
                f"  [{index}] {value!r}"
                for index, value in enumerate(self.mcp)
            )
            lines.append("MCP handles: call schema() once before invoke().")
        else:
            lines.append("mcp: []")
        if self.plugins:
            lines.append("plugins:")
            lines.extend(
                f"  [{index}] {value!r}"
                for index, value in enumerate(self.plugins)
            )
        else:
            lines.append("plugins: []")
        return "\n".join(lines)

    def __str__(self) -> str:
        return repr(self)

@dataclass(frozen=True, slots=True)
class Variant1RemoteHandle:
    """Immutable identity for host-owned state with descriptor-routed methods.

    The live bridge is deliberately resolved through a weak module registry.
    Consequently a handle contains neither the authenticated bridge secret nor
    a socket, transport, callback, or other live host resource.
    """

    service: str
    kind: str
    id: str
    generation: int
    revision: int
    _metadata_json: str = field(repr=False)
    _dispatch_json: str = field(repr=False)
    _methods_json: str = field(repr=False)
    _router_id: str = field(repr=False)

    @property
    def metadata(self) -> dict[str, Any]:
        # A fresh copy prevents mutations of nested metadata from changing the
        # immutable handle value.
        return json.loads(self._metadata_json)

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "kind": self.kind,
            "id": self.id,
            "generation": self.generation,
            "revision": self.revision,
        }

    @property
    def methods(self) -> _RemoteHandleMethods:
        # ``handle.methods`` remains the immutable rich descriptor tuple used
        # by existing callers.  Making that tuple callable also lets models use
        # the same ``object.methods()`` discovery idiom as mounted APIs.
        return _RemoteHandleMethods(json.loads(self._methods_json))

    def describe(self, name: str | None = None) -> dict[str, Any]:
        """Inspect this handle or one exact method without host dispatch."""

        if name is None:
            return {
                "kind": f"{self.service}.{self.kind}",
                "identity": self.identity,
                "method_signatures": {
                    item["name"]: f"{item['name']}{_signature_for_remote_method(item, positional=isinstance(self, Variant1ResourceHandle))}"
                    for item in self.methods
                },
                "metadata_type": "dict property",
                "metadata_fields": sorted(self.metadata),
                "method_details": "describe(name) returns one complete method contract",
                "serialization": "JSON evidence: {'identity': handle.identity, 'metadata': handle.metadata}. This records handle facts, not a restorable live handle or a job completion receipt.",
            }

        method_name = str(name or "")
        descriptor = self._method_descriptor(method_name)
        if descriptor is None:
            available = ", ".join(item["name"] for item in self.methods) or "none"
            raise AttributeError(
                f"{self.service}.{self.kind} has no method {method_name!r}; "
                f"available: {available}"
            )
        return {
            **descriptor,
            "signature": f"{method_name}{_signature_for_remote_method(descriptor, positional=isinstance(self, Variant1ResourceHandle))}",
        }

    def _method_descriptor(self, name: str) -> dict[str, Any] | None:
        for item in json.loads(self._methods_json):
            if str(item.get("name") or "") == name:
                return dict(item)
        return None

    def _validate_method(self, name: str, arguments: dict[str, Any]) -> None:
        methods = self.methods
        if not methods:
            return
        descriptor = self._method_descriptor(name)
        if descriptor is None:
            available = ", ".join(item["name"] for item in methods)
            raise AttributeError(
                f"{self.service}.{self.kind} has no method {name!r}; "
                f"available: {available}"
            )
        _signature_for_remote_method(descriptor).bind(**arguments)

    def call(
        self,
        method: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        method_name = str(method or "")
        if not method_name or method_name.startswith("_"):
            raise ValueError("remote handle method must be a non-private name")
        if arguments is not None and kwargs:
            raise TypeError("pass an argument dictionary or keyword arguments, not both")
        if arguments is not None and not isinstance(arguments, dict):
            raise TypeError("remote handle arguments must be a dictionary")
        clean_arguments = dict(arguments if arguments is not None else kwargs)
        self._validate_method(method_name, clean_arguments)
        bridge = _remote_handle_bridge(self._router_id)
        descriptor = json.loads(self._dispatch_json)
        return bridge.invoke(descriptor, {
            "handle": self.identity,
            "method": method_name,
            "arguments": clean_arguments,
        })

    def call_async(
        self,
        method: str,
        arguments: dict[str, Any] | None = None,
        *,
        _deadline_ms: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """Return the explicit awaitable form of one remote-handle method."""

        method_name = str(method or "")
        if not method_name or method_name.startswith("_"):
            raise ValueError("remote handle method must be a non-private name")
        if arguments is not None and kwargs:
            raise TypeError("pass an argument dictionary or keyword arguments, not both")
        if arguments is not None and not isinstance(arguments, dict):
            raise TypeError("remote handle arguments must be a dictionary")
        clean_arguments = dict(arguments if arguments is not None else kwargs)
        self._validate_method(method_name, clean_arguments)
        bridge = _remote_handle_bridge(self._router_id)
        descriptor = json.loads(self._dispatch_json)
        descriptor["_control_hint"] = bool((self._method_descriptor(method_name) or {}).get("control"))
        return bridge.invoke_async(
            descriptor,
            {
                "handle": self.identity,
                "method": method_name,
                "arguments": clean_arguments,
            },
            deadline_ms=_deadline_ms,
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or not name.isidentifier() or keyword.iskeyword(name):
            raise AttributeError(name)
        descriptor = self._method_descriptor(name)
        if self.methods and descriptor is None:
            available = ", ".join(item["name"] for item in self.methods)
            raise AttributeError(
                f"{self.service}.{self.kind} has no method {name!r}; "
                f"available: {available}"
            )
        return _RemoteHandleMethod(self, name, descriptor or {"name": name})

    def __dir__(self) -> list[str]:
        return sorted(
            set(object.__dir__(self))
            | {str(item.get("name") or "") for item in self.methods}
        )

    def _repr_mimebundle_(self, **_kwargs: Any) -> dict[str, Any]:
        summary = {
            "schema": "variant1.remote-handle-summary.v1",
            **self.identity,
            "metadata": self.metadata,
            "methods": [item["name"] for item in self.methods],
        }
        return {
            "text/plain": repr(self),
            "application/json": summary,
        }

    def __repr__(self) -> str:
        names = [str(item.get("name") or "") for item in self.methods]
        methods = ", ".join(f"{name}()" for name in names if name) or "none"
        metadata = self.metadata
        attributes = []
        label_key = "name" if metadata.get("name") else "title"
        label = metadata.get(label_key)
        if label:
            attributes.append(f"{label_key}={str(label)[:160]!r}")
        if self.service == "connectors" and metadata.get("server_id"):
            attributes.append(f"server={str(metadata['server_id'])[:160]!r}")
        elif metadata.get("version"):
            attributes.append(f"version={str(metadata['version'])[:80]!r}")
        state = metadata.get("state") or metadata.get("status")
        if state:
            attributes.append(f"state={str(state)[:80]!r}")
        if self.service == "execution" and metadata.get("signal_receipt"):
            attributes.append(f"signal_receipt={metadata['signal_receipt']!r}")
        if self.service == 'execution' and self.kind == 'process':
            health = metadata.get('health') or {}
            if health.get('leader_alive') is False:
                attributes.append('leader_alive=False')
                if state in {'running', 'healthy', 'terminating'}:
                    attributes.append(f"owned_descendants={health.get('owned_descendant_count')!r}")
        attribute_text = (" " + " ".join(attributes)) if attributes else ""
        hint = ""
        if (
            self.service == "connectors"
            and self.kind == "mcp"
            and not bool(metadata.get("schema_included"))
        ):
            hint = "; schema() before invoke()"
        elif self.service == "connectors" and self.kind == "mcp":
            hint = "; invoke(arguments=..., conclude=False)"
        elif self.service == "connectors" and self.kind == "plugin":
            hint = "; invoke(contribution_id=..., conclude=False)"
        return (
            f"<Variant1Handle {self.service}.{self.kind}{attribute_text} "
            f"methods=[{methods}]{hint}>"
        )


class Variant1ResourceHandle(Variant1RemoteHandle):
    """Browser/blob metadata supports the same attribute and mapping reads.

    Identity and declared methods keep precedence over metadata. Other handle
    families retain their existing API (including real methods named get).
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        if self.service == "browser" and self.kind == "page":
            if name == "page":
                return self
            if name == "session" and "session" in self.metadata:
                return _decode_host_result(self.metadata["session"], _remote_handle_bridge(self._router_id))
        if not name.startswith("_") and self._method_descriptor(name) is None:
            metadata = self.metadata
            if name in metadata:
                return metadata[name]
        return super().__getattr__(name)

    def __getitem__(self, name: str) -> Any:
        if (self.service == "browser" and self.kind == "page"
                and (name == "page" or name == "session" and "session" in self.metadata)):
            return getattr(self, name)
        if name in self.identity:
            return self.identity[name]
        return self.metadata[name]

    def get(self, name: str, default: Any = None) -> Any:
        try:
            return self[name]
        except KeyError:
            return default

    def __iter__(self):
        return iter(dict.fromkeys([*self.identity, *self.metadata]))

    def __len__(self) -> int:
        return len(set(self.identity) | set(self.metadata))

    def __dir__(self) -> list[str]:
        related = {"page"} if self.service == "browser" and self.kind == "page" else set()
        return sorted(set(super().__dir__()) | related | {
            key for key in self.metadata if key.isidentifier() and not key.startswith("_")
        })

    def __repr__(self) -> str:
        if self.service == "artifacts" and self.kind == "blob":
            metadata = self.metadata
            return (f"<ArtifactBlob ref={metadata.get('ref', self.id)!r} size={metadata.get('size', 'unknown')} "
                    "methods=[save(path, overwrite=False), read_bytes(max_bytes=4194304)]>")
        if self.service == "browser" and self.kind == "page":
            metadata = self.metadata
            methods = ", ".join(item["name"] for item in self.methods)
            parent_hint = ".session is its parent; " if "session" in metadata else ""
            return (f"<BrowserPage id={self.id!r} title={str(metadata.get('title') or '')[:160]!r} "
                    f"url={str(metadata.get('url') or '')[:300]!r} "
                    f"state={metadata.get('state')!r} methods=[{methods}]; "
                    f".page is this handle; {parent_hint}describe(name) gives exact arguments>")
        return super().__repr__()


class Variant1ArtifactSaveResult(dict):
    """Verified export values plus an original machine-generated receipt."""

    def __getattr__(self, name: str) -> Any:
        if not name.startswith('_') and name in self:
            return self[name]
        raise AttributeError(name)

    def __repr__(self) -> str:
        lines = [f"ArtifactExport(verified={self.get('verified')!r}, destination={self.get('destination')!r}, bytes={self.get('bytes')!r})"]
        if self.get('receipt') is not None:
            lines.append("Exact export receipt JSON: .receipt.save(path). Full fields remain available, including .sha256 and .ref.")
        if self.get('receipt_error'):
            lines.append(f"Receipt unavailable: {self['receipt_error']}. Primary export status is unchanged.")
        return '\n'.join(lines)

    def __str__(self) -> str:
        return repr(self)


class Variant1BrowserResult(dict):
    """A flat Python view that also retains the original nested wire record."""

    _nested_key = "result"

    def __init__(self, value):
        nested = value.get(self._nested_key)
        super().__init__({**(dict(nested) if isinstance(nested, Mapping) else {}), **value})

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("_") and name in self:
            return self[name]
        if name in {"elements", "find_elements", "metadata"}:
            raise AttributeError(
                f"Browser action results have no {name!r}. "
                "Get a fresh observation with result.page.observe() for elements; "
                "inspect action fields with dict(result) or result.keys()."
            )
        raise AttributeError(name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | {
            str(key) for key in self if str(key).isidentifier() and not str(key).startswith("_")
        })

    def __repr__(self) -> str:
        lines = [
            f"BrowserAction(action={self.get('action')!r}, surface={self.get('surface')!r}, "
            f"url={str(self.get('url') or '')[:500]!r}, operation_id={self.get('operation_id')!r})",
            "Continue with .page and .session. Fresh elements: .page.observe(). "
            "Action fields: dict(result) or .keys(); indexing and get() are supported.",
        ]
        if "value" in self and self["value"] is not None:
            value = repr(self["value"])
            lines.append("value=" + value[:800] + (" ... (full result in .value)" if len(value) > 800 else ""))
        if self.get("document_state"):
            lines.append(f"document_state={self['document_state']!r}; {self.get('message', '')}")
        if "download_state" in self:
            downloads = self.get("downloads", [])
            downloads = downloads if isinstance(downloads, list) else []
            lines.append(f"download_state={self['download_state']!r}; downloads={len(downloads)}")
            if downloads:
                for row in downloads[:3]:
                    lines.append(f"  download {row.get('download_id')!r}: {row.get('suggested_filename')!r}, "
                                 f"state={row.get('state')!r}, bytes={row.get('bytes')!r}")
                lines.append("Full records: .downloads; completed artifact: .download['artifact'].save(path).")
            if self["download_state"] != "completed" and self.get("action") in {
                "navigate", "click", "keys", "evaluate", "back", "forward", "reload",
            }:
                lines.append("Check .session.history('downloads', operation_id=...) before retrying a download.")
        if self.get("image") is not None:
            lines.append(f"image={self['image']!r}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return repr(self)


class Variant1BrowserElementMatches(list):
    """A bounded page of original handles selected from one observation."""

    def __init__(self, rows: list[tuple[int, Any]], *, total: int, offset: int):
        super().__init__(element for _index, element in rows)
        self._indices = [index for index, _element in rows]
        self.total = total
        self.offset = offset

    def __repr__(self) -> str:
        lines = [f"BrowserElementMatches(matches={self.total}, shown={len(self)}, offset={self.offset})"]
        for index, element in zip(self._indices, self):
            metadata = element.metadata
            role = ' '.join(str(metadata.get('role') or 'element').split())[:80]
            name = str(metadata.get('name') or '')[:180]
            methods = ', '.join(str(method.get('name') or '') for method in element.methods)
            lines.append(f"  elements[{index}] {role} {name!r} methods=[{methods[:160]}]")
        remaining = max(0, self.total - self.offset - len(self))
        if remaining:
            lines.append(f"  {remaining} further match(es); continue with offset={self.offset + len(self)}.")
        lines.append("Matches are original element handles; call their advertised methods directly.")
        return '\n'.join(lines)

    def __str__(self) -> str:
        return repr(self)


class Variant1BrowserObservation(Variant1BrowserResult):
    """Mapping-compatible browser observation with a bounded Python display."""

    _DISPLAY_ELEMENTS = 16
    _DISPLAY_TEXT_CHARS = 800
    _nested_key = "snapshot"

    def find_elements(
        self, name: str | None = None, role: str | None = None,
        *, offset: int = 0, limit: int = 20,
    ) -> Variant1BrowserElementMatches:
        """Search this observation locally; return its original actionable handles.

        Name is a case-insensitive substring; role is a case-insensitive exact
        match. This never observes, navigates, retries input, or changes refs.
        """
        if name is not None and not isinstance(name, str):
            raise TypeError("element name must be a string")
        if role is not None and not isinstance(role, str):
            raise TypeError("element role must be a string")
        name_filter, role_filter = (name or '').casefold(), (role or '').casefold()
        if not name_filter and not role_filter:
            raise ValueError("find_elements requires a name or role filter; full data is in observation.elements")
        if type(offset) is not int or offset < 0:
            raise ValueError("element offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("element limit must be an integer from 1 to 100")
        elements = self.get('elements') or []
        rows = []
        for index, element in enumerate(elements):
            if not isinstance(element, Variant1RemoteHandle) or element.service != 'browser' or element.kind != 'element':
                continue
            metadata = element.metadata
            if name_filter and name_filter not in str(metadata.get('name') or '').casefold():
                continue
            if role_filter and role_filter != str(metadata.get('role') or '').casefold():
                continue
            rows.append((index, element))
        return Variant1BrowserElementMatches(rows[offset:offset + limit], total=len(rows), offset=offset)

    def __repr__(self) -> str:
        snapshot = self.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        elements = snapshot.get("elements")
        elements = elements if isinstance(elements, list) else []
        lines = [
            "BrowserObservation("
            f"surface={str(self.get('surface') or self.get('browser_kind') or 'unknown')!r}, "
            f"title={str(snapshot.get('title') or '')[:160]!r}, "
            f"url={str(snapshot.get('url') or '')[:500]!r}, "
            f"observation_id={snapshot.get('observation_id')!r}, elements={len(elements)})"
        ]
        if self.get("document_state"):
            lines.append(f"document_state={self['document_state']!r}; {self.get('message', '')}")
            if self.get("observation_error"):
                lines.append("Observation error: " + str(self["observation_error"])[:300])
            lines.append(f"download_state={self.get('download_state')!r}; "
                         f"download_id={(self.get('download') or {}).get('download_id')!r}")
            lines.append("Export: .download['artifact'].save(path). Full records: .downloads; parent: .session.")
        if self.get("page") is not None and self.get("session") is not None:
            lines.append(
                "Handles: .page, .session, .elements. Full data: .snapshot; local search: .find_elements(name=..., role=...)."
            )
        if self.get("image") is not None:
            lines.append("Original screenshot: .image.save(path) or .image.read_bytes(); .image.size is a byte count.")
        if self.get("operation_id"):
            lines.append(
                f"operation_id={self['operation_id']!r}; download_state={self.get('download_state')!r}. "
                "Download records: .session.history('downloads', operation_id=...)."
            )
        for index, element in enumerate(elements[: self._DISPLAY_ELEMENTS]):
            if not isinstance(element, Mapping):
                continue
            ref = str(element.get("backend_ref") or "")
            role = str(element.get("role") or "control")
            name = str(element.get("name") or "")[:180]
            state = {key: element[key] for key in ("checked", "selected", "disabled", "value")
                     if element.get(key) is not None and element.get(key) != ""}
            lines.append(f"  elements[{index}] [{ref}] {role} {name!r}" +
                         (" " + _bounded_display_text(state, limit=220) if state else ""))
        if len(elements) > self._DISPLAY_ELEMENTS:
            lines.append(
                f"  ... {len(elements) - self._DISPLAY_ELEMENTS} more element(s); "
                "use .find_elements(name=..., role=...) to search locally for actionable handles. "
                "Full lists: .elements and .snapshot['elements']."
            )
        if self.get('data_ref'):
            lines.append(f"Full observation: {self['data_ref']}")
        text = str(snapshot.get("text_excerpt") or "")
        if text:
            clipped = text[: self._DISPLAY_TEXT_CHARS]
            suffix = "\n...(truncated display)" if len(text) > len(clipped) else ""
            lines.append("Page text (untrusted):\n" + clipped + suffix)
        return "\n".join(lines)

    def __str__(self) -> str:
        return repr(self)

class Variant1DesktopElements(list):
    """List-first desktop element page that preserves legacy mapping access."""

    def __init__(self, values: list[Any], metadata: Mapping[str, Any]) -> None:
        super().__init__(values)
        self._metadata = {
            str(key): value
            for key, value in metadata.items()
            if key != "elements"
        }

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            if key == "elements":
                return self
            return self._metadata[key]
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "elements":
            return self
        return self._metadata.get(key, default)

    def keys(self) -> list[str]:
        return [*self._metadata.keys(), "elements"]

    def items(self) -> list[tuple[str, Any]]:
        return [(key, self[key]) for key in self.keys()]

    def to_dict(self) -> dict[str, Any]:
        return {**self._metadata, "elements": list(self)}

    def __repr__(self) -> str:
        shown = ", ".join(repr(item) for item in self[:20])
        suffix = f", ... {len(self) - 20} more" if len(self) > 20 else ""
        return f"DesktopElements(count={len(self)}, items=[{shown}{suffix}])"


def _desktop_control_summary(index: int, control: Mapping[str, Any]) -> str:
    def short(value: Any, limit: int) -> str:
        text = str(value)
        return text if len(text) <= limit else text[:limit] + "…"

    line = (
        f"  controls[{index}] target={control.get('id')!r} "
        f"{str(control.get('role') or 'control')[:80]} "
        f"{short(control.get('name') or '', 180)!r}"
    )
    if control.get("value") is not None:
        line += f" value={short(control['value'], 120)!r}"
    detail = control.get("text")
    if detail and detail not in (control.get("name"), control.get("value")):
        line += f" text={short(detail, 120)!r}"
    if control.get("state"):
        line += f" state={short(control['state'], 32)!r}"
    if control.get("offscreen") is True:
        line += " [offscreen]"
    return line


def _desktop_preview_indices(controls: list[Any], budget: int) -> list[int]:
    """Keep late editable fields discoverable without expanding the preview.

    Reserve up to a quarter of the row budget for distinct editable labels,
    then fill in traversal order. This prevents repeated list/grid cells from
    consuming the reserve. The underlying controls and target IDs never move.
    """
    selected: set[int] = set()
    labels: set[tuple[str, str]] = set()
    reserve = max(0, budget // 4)
    for index, control in enumerate(controls):
        if len(selected) >= reserve:
            break
        if not isinstance(control, Mapping) or control.get("offscreen") is True:
            continue
        role = str(control.get("role") or "").casefold()
        if role not in {"edit", "document", "combobox"}:
            continue
        bounds = control.get("bounds")
        try:
            if len(bounds) != 4:
                continue
            left, top, right, bottom = (float(value) for value in bounds)
            if not all(math.isfinite(value) for value in (left, top, right, bottom)):
                continue
            if right <= left or bottom <= top:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        label = str(control.get("name") or "").strip().casefold()
        key = (role, label or f"<unnamed:{control.get('ref') or control.get('id') or index}>")
        if key not in labels:
            labels.add(key)
            selected.add(index)
    for index, control in enumerate(controls):
        if len(selected) >= budget:
            break
        if isinstance(control, Mapping):
            selected.add(index)
    return sorted(selected)


class Variant1DesktopControlMatches(list):
    """A bounded page of exact controls from an existing desktop snapshot."""

    def __init__(self, rows: list[tuple[int, Mapping[str, Any]]], *, total: int, offset: int) -> None:
        super().__init__(control for _index, control in rows)
        self._indices = [index for index, _control in rows]
        self.total = total
        self.offset = offset

    def __repr__(self) -> str:
        lines = [f"ControlMatches: list[dict] (total={self.total}, offset={self.offset}); use result[0] as target"]
        for position, (index, control) in enumerate(zip(self._indices, self)):
            preview = {}
            for key, limit in (("id", 180), ("role", 80), ("name", 180),
                               ("value", 120), ("text", 120), ("state", 32),
                               ("offscreen", 10)):
                if key in control:
                    value = control[key]
                    if isinstance(value, str) and len(value) > limit:
                        value = value[:limit] + "…"
                    preview[key] = value
            lines.append(f"  [{position}] {preview!r}  # view.controls[{index}]")
        remaining = max(0, self.total - self.offset - len(self))
        if remaining:
            lines.append(f"  {remaining} further match(es); continue with offset={self.offset + len(self)}.")
        return "\n".join(lines)

    def __str__(self) -> str:
        return repr(self)


class Variant1DesktopViewResult(dict):
    """Mapping-compatible focus/see result with a concise control display."""

    _DISPLAY_CONTROLS = 16

    def find_controls(
        self, name: str | None = None, role: str | None = None,
        *, offset: int = 0, limit: int = 20,
    ) -> Variant1DesktopControlMatches:
        """Filter this snapshot: name substring and exact role, case-insensitive.

        Returns a list of original control dictionaries: pass result[0] directly
        as an action target; use len(result) to check this page's length.
        This is local inspection; it neither refreshes nor operates the desktop.
        """
        if name is not None and not isinstance(name, str):
            raise TypeError("control name must be a string")
        if role is not None and not isinstance(role, str):
            raise TypeError("control role must be a string")
        name_filter, role_filter = (name or '').casefold(), (role or '').casefold()
        if not name_filter and not role_filter:
            raise ValueError("find_controls requires a name or role filter; full data is in view.controls")
        if type(offset) is not int or offset < 0:
            raise ValueError("control offset must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("control limit must be an integer from 1 to 100")
        controls = self.get('controls') or []
        rows = [
            (index, control) for index, control in enumerate(controls)
            if isinstance(control, Mapping)
            and (not name_filter or name_filter in str(control.get('name') or '').casefold())
            and (not role_filter or role_filter == str(control.get('role') or '').casefold())
        ]
        return Variant1DesktopControlMatches(rows[offset:offset + limit], total=len(rows), offset=offset)

    def __getattr__(self, name: str) -> Any:
        """Expose structured view fields without giving up mapping access."""

        if str(name).startswith("_"):
            raise AttributeError(name)
        try:
            return dict.__getitem__(self, name)
        except KeyError:
            action = dict.get(self, "action")
            if isinstance(action, Mapping):
                if name in {"action_status", "status"}:
                    return action.get("status")
                if name == "input_sent":
                    return action.get("input_sent")
                if name == "action_error":
                    return action.get("error")
            raise AttributeError(name) from None

    def __dir__(self) -> list[str]:
        fields = {
            str(key) for key in dict.keys(self)
            if str(key).isidentifier() and not str(key).startswith("_")
        }
        if isinstance(dict.get(self, "action"), Mapping):
            fields.update({"action_status", "status", "input_sent", "action_error"})
        return sorted(set(super().__dir__()) | fields)

    def __repr__(self) -> str:
        controls = self.get("controls")
        controls = controls if isinstance(controls, list) else []
        action = self.get("action") if isinstance(self.get("action"), Mapping) else {}
        action_text = ""
        if action:
            action_text = (
                f", after={str(action.get('name') or '')!r}, "
                f"input_sent={action.get('input_sent')!r}, status={action.get('status')!r}"
            )
        total = int((self.get("control_page") or {}).get("total") or len(controls))
        lines = [
            "DesktopView("
            f"window_id={str(self.get('window_id') or '')!r}, "
            f"mode={str(self.get('mode') or '')!r}, controls={len(controls)}/{total}"
            f"{action_text})"
        ]
        preview = _desktop_preview_indices(controls, self._DISPLAY_CONTROLS)
        for index in preview:
            lines.append(_desktop_control_summary(index, controls[index]))
        shown = len(preview)
        if self.get("observation_status") == "unavailable":
            lines.append(
                "Post-action observation unavailable. Reacquire a live window with "
                "computer.list_windows()/computer.focus() before observing; "
                "the action may have closed or replaced its window."
            )
        elif self.get("text_included") is False:
            lines.append(str(self.get("controls_hint") or
                "Controls omitted; use computer.observe(window=view.window, include_text=True)."))
        elif total > shown:
            lines.append(
                f"  ... {total - shown} more control(s). "
                "view.controls and view.find_controls(name=..., role=...) contain "
                "dictionaries; matches are exact action targets."
            )
        if action.get("error"):
            lines.append(f"  action error: {str(action['error'])[:500]}")
        if self.get("image") is not None:
            lines.append("Original screenshot: .image.save(path) or .image.read_bytes(); .image.size is a byte count.")
        return "\n".join(lines)

    def __str__(self) -> str:
        return repr(self)

def _encode_host_argument(value: Any) -> Any:
    """Normalize Python paths and handles before strict wire serialization."""

    # Python callers naturally compose paths with pathlib. Normalize at the
    # proxy boundary; signed wire values and persisted JSON remain strict.
    if isinstance(value, os.PathLike):
        path = os.fspath(value)
        if not isinstance(path, str):
            raise TypeError('host capability paths require a text path')
        return path

    if isinstance(value, Variant1RemoteHandle):
        return value.identity
    if isinstance(value, Mapping):
        return {
            str(key): _encode_host_argument(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_encode_host_argument(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class _RemoteHandleMethod:
    handle: Variant1RemoteHandle
    name: str
    descriptor: dict[str, Any]

    @property
    def __name__(self) -> str:
        return self.name

    @property
    def __doc__(self) -> str:
        return str(self.descriptor.get("description") or "Remote handle method.")

    @property
    def __signature__(self) -> inspect.Signature:
        return _signature_for_remote_method(
            self.descriptor, positional=isinstance(self.handle, Variant1ResourceHandle),
        )

    def _arguments(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if isinstance(self.handle, Variant1ResourceHandle) and self.handle.methods:
            # Preserve the existing one-dict calling form, then bind normal
            # Python positional/keyword calls against the exact descriptor.
            if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
                args, kwargs = (), dict(args[0])
            bound = self.__signature__.bind(*args, **kwargs)
            arguments = dict(bound.arguments)
            if self.descriptor.get("variadic_kwargs"):
                arguments.update(arguments.pop("options", {}))
            return arguments
        return _remote_method_arguments(
            f"{self.handle.service}.{self.handle.kind}.{self.name}", args, kwargs,
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        arguments = self._arguments(args, kwargs)
        if self.handle.methods:
            self.__signature__.bind(**arguments)
        return self.handle.call(self.name, arguments)

    def async_(
        self,
        *args: Any,
        _deadline_ms: int | None = None,
        **kwargs: Any,
    ) -> Any:
        arguments = self._arguments(args, kwargs)
        if self.handle.methods:
            self.__signature__.bind(**arguments)
        return self.handle.call_async(
            self.name,
            arguments,
            _deadline_ms=_deadline_ms,
        )

    def documentation(self) -> dict[str, Any]:
        """Return this exact method contract without invoking the host."""
        return self.handle.describe(self.name)

    def describe(self) -> dict[str, Any]:
        """Familiar alias for :meth:`documentation`."""
        return self.documentation()

    def __repr__(self) -> str:
        return (
            f"<VARIANT-1 remote method {self.handle.service}."
            f"{self.handle.kind}.{self.name}{self.__signature__}; "
            "call .describe() for details or .async_(...) to await>"
        )


def _parse_remote_handle(value: Any, bridge: Any) -> Variant1RemoteHandle | None:
    """Decode one exact remote-handle envelope, or leave JSON untouched."""

    if not isinstance(value, dict) or set(value) != {REMOTE_HANDLE_KEY}:
        return None
    payload = value.get(REMOTE_HANDLE_KEY)
    if not isinstance(payload, dict):
        return None
    if any(field_name not in payload for field_name in _REMOTE_HANDLE_IDENTITY_FIELDS):
        return None
    service = payload.get("service")
    kind = payload.get("kind")
    handle_id = payload.get("id")
    generation = payload.get("generation")
    revision = payload.get("revision")
    metadata = payload.get("metadata")
    dispatch = payload.get("_dispatch")
    methods = payload.get("methods")
    if not all(isinstance(item, str) and item for item in (service, kind, handle_id)):
        return None
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(metadata, dict)
        or _contains_sensitive_metadata(metadata)
        or not isinstance(dispatch, dict)
        or dispatch.get("schema") != REMOTE_HANDLE_DISPATCH_SCHEMA
        or not isinstance(dispatch.get("ref_id"), str)
        or not dispatch.get("ref_id")
        or not isinstance(dispatch.get("handler_revision"), str)
        or not dispatch.get("handler_revision")
    ):
        return None
    normalized_methods: list[dict[str, Any]] = []
    if methods is not None:
        if (
            not isinstance(methods, dict)
            or methods.get("schema") != REMOTE_HANDLE_METHODS_SCHEMA
            or not isinstance(methods.get("items"), list)
            or len(methods["items"]) > 64
        ):
            return None
        seen_methods: set[str] = set()
        for raw_method in methods["items"]:
            if not isinstance(raw_method, dict):
                return None
            method_name = str(raw_method.get("name") or "")
            description = str(raw_method.get("description") or "")
            raw_params = raw_method.get("params")
            if (
                not method_name.isidentifier()
                or keyword.iskeyword(method_name)
                or method_name.startswith("_")
                or method_name in seen_methods
                or len(description) > 2000
                or not isinstance(raw_params, list)
                or len(raw_params) > 32
            ):
                return None
            params: list[dict[str, Any]] = []
            seen_params: set[str] = set()
            for raw_param in raw_params:
                if not isinstance(raw_param, dict):
                    return None
                param_name = str(raw_param.get("name") or "")
                if (
                    not param_name.isidentifier()
                    or keyword.iskeyword(param_name)
                    or param_name in seen_params
                ):
                    return None
                param = {
                    "name": param_name,
                    "type": str(raw_param.get("type") or "any")[:100],
                    "required": bool(raw_param.get("required")),
                }
                if "default" in raw_param and not param["required"]:
                    param["default"] = raw_param.get("default")
                params.append(param)
                seen_params.add(param_name)
            normalized_methods.append({
                "name": method_name,
                "description": description,
                "params": params,
                "returns": str(raw_method.get("returns") or "any")[:100],
                "variadic_kwargs": bool(raw_method.get("variadic_kwargs")),
                **({"control": True} if raw_method.get("control") is True else {}),
            })
            seen_methods.add(method_name)
    descriptor = {"schema": REMOTE_HANDLE_DISPATCH_SCHEMA}
    descriptor.update({
        name: dispatch[name]
        for name in _REMOTE_HANDLE_DISPATCH_FIELDS
        if name in dispatch
    })
    try:
        metadata_json = _canonical_json(metadata)
        dispatch_json = _canonical_json(descriptor)
        methods_json = _canonical_json(normalized_methods)
    except (TypeError, ValueError):
        return None
    if len(methods_json.encode("utf-8")) > 64 * 1024:
        return None
    handle_type = (
        Variant1ResourceHandle
        if service == "browser" or (service == "artifacts" and kind == "blob")
        else Variant1RemoteHandle
    )
    return handle_type(
        service=service,
        kind=kind,
        id=handle_id,
        generation=generation,
        revision=revision,
        _metadata_json=metadata_json,
        _dispatch_json=dispatch_json,
        _methods_json=methods_json,
        _router_id=_remote_handle_router_id(bridge),
    )


def _decode_host_result(value: Any, bridge: Any, *, _unpacked: bool = False) -> Any:
    """Recursively decode typed host values while preserving ordinary JSON."""

    if not _unpacked:
        value = unpack_value(value)

    if isinstance(value, (
        Variant1RemoteHandle,
        Variant1ConnectorMatch,
        Variant1ConnectorSearchResult,
        Variant1CommandResult,
        Variant1McpResult,
        Variant1McpSchema,
    )):
        return value
    parsed = _parse_remote_handle(value, bridge)
    if parsed is not None:
        return parsed
    if isinstance(value, list):
        return [_decode_host_result(item, bridge, _unpacked=True) for item in value]
    if isinstance(value, dict):
        decoded = {
            key: _decode_host_result(item, bridge, _unpacked=True) for key, item in value.items()
        }
        if decoded.get("schema") == "variant1.artifact-save-result.v1":
            return Variant1ArtifactSaveResult(decoded)
        if decoded.get("schema") == "variant1.browser-observation-result.v1":
            return Variant1BrowserObservation(decoded)
        if decoded.get("schema") == "variant1.browser-action-view.v1":
            return Variant1BrowserResult(decoded)
        if decoded.get("schema") == "variant1.desktop-elements.v1":
            elements = decoded.get("elements")
            return Variant1DesktopElements(
                list(elements) if isinstance(elements, list) else [],
                decoded,
            )
        if decoded.get("schema") == "variant1.desktop-view-result.v2":
            return Variant1DesktopViewResult(decoded)
        if decoded.get("schema") == "variant1.mcp-result.v2":
            return Variant1McpResult(decoded)
        if decoded.get("schema") == "variant1.command-result.v1":
            return Variant1CommandResult(decoded)
        if decoded.get("schema") == "variant1.file-read-result.v2":
            return Variant1FileReadResult(decoded)
        descriptor = decoded.get("descriptor")
        lease = decoded.get("lease")
        if (
            isinstance(descriptor, dict)
            and isinstance(lease, dict)
            and (
                isinstance(descriptor.get("inputSchema"), dict)
                or isinstance(descriptor.get("input_schema"), dict)
            )
        ):
            return Variant1McpSchema(decoded)
        if "mcp" in decoded and "plugins" in decoded:
            return Variant1ConnectorSearchResult(decoded)
        return decoded
    return value


def _identifier(value: str) -> str:
    clean = str(value or "").replace("-", "_").replace(".", "_")
    if not clean.isidentifier() or keyword.iskeyword(clean):
        raise ValueError(f"capability alias is not a Python identifier: {value!r}")
    return clean


class KernelBridgeClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        secret: bytes,
        nonce: str,
        generation: int,
        kernel: Any,
        timeout_s: float = 120.0,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        async_concurrency: int = 8,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.secret = bytes(secret)
        self.nonce = str(nonce)
        self.generation = int(generation)
        self.kernel = kernel
        self.timeout_s = max(1.0, float(timeout_s))
        self.max_frame_bytes = max(1024, int(max_frame_bytes))
        self.async_concurrency = max(1, min(int(async_concurrency), 64))
        self._counter = 0
        self._lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._async_semaphore_lock = threading.Lock()
        self._async_semaphores: weakref.WeakKeyDictionary[Any, Any] = (
            weakref.WeakKeyDictionary()
        )
        self._async_control_semaphores: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
        self._execution_origin: contextvars.ContextVar[_ExecutionOrigin | None] = (
            contextvars.ContextVar(
                f"variant1_kernel_execution_origin_{uuid.uuid4().hex}",
                default=None,
            )
        )
        self._origin_tokens: list[contextvars.Token] = []
        self._terminate_executions: dict[str, str] = {}
        self._failure_lock = threading.Lock()
        self._last_capability_failure: Variant1CapabilityFailure | None = None

    @staticmethod
    def _public_failure_arguments(
        descriptor: dict[str, Any], arguments: dict[str, Any]
    ) -> dict[str, Any]:
        clean = dict(arguments or {})
        if bool(descriptor.get("argument_envelope")) and isinstance(
            clean.get("arguments"), dict
        ):
            clean = dict(clean["arguments"])
        fixed = descriptor.get("fixed_arguments")
        if isinstance(fixed, dict):
            for name in fixed:
                clean.pop(str(name), None)
        clean.pop("__mutation_method", None)
        return clean

    def _remember_failure(
        self,
        descriptor: dict[str, Any] | str,
        arguments: dict[str, Any],
        *,
        code: str,
        message: str,
        receipt: dict[str, Any] | None = None,
        result: Any = None,
    ) -> Variant1CapabilityFailure:
        details = (
            dict(descriptor)
            if isinstance(descriptor, dict)
            else {"ref_id": str(descriptor)}
        )
        failure = Variant1CapabilityFailure(
            descriptor=details,
            arguments=self._public_failure_arguments(details, arguments),
            code=str(code or "capability_error"),
            message=str(message or "Host capability failed."),
            receipt=dict(receipt or {}),
            result=result,
        )
        with self._failure_lock:
            self._last_capability_failure = failure
        return failure

    def _remember_unsuccessful_result(
        self,
        descriptor: dict[str, Any] | str,
        arguments: dict[str, Any],
        result: Any,
        *,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(result, Mapping):
            return
        action_status = str(result.get("action_status") or "").casefold()
        code = ""
        if action_status in {"no_effect", "unknown_effect", "failed", "error"}:
            code = action_status
        elif result.get("ok") is False:
            code = str(result.get("code") or "unsuccessful_result")
        elif result.get("success") is False:
            code = str(result.get("code") or "unsuccessful_result")
        elif bool(result.get("is_error")):
            code = str(result.get("code") or "tool_result_error")
        if not code:
            return
        message = str(
            result.get("action_error")
            or result.get("error")
            or result.get("message")
            or f"Capability returned {code}."
        )
        self._remember_failure(
            descriptor,
            arguments,
            code=code,
            message=message,
            receipt=receipt,
            result=result,
        )

    def last_failure(self) -> Variant1CapabilityFailure | None:
        with self._failure_lock:
            return self._last_capability_failure

    def _parent_origin(self) -> _ExecutionOrigin | None:
        """Read the immutable admission bound by the CPython worker."""

        current = getattr(self.kernel, "current_admission", None)
        variant1 = current() if callable(current) else None
        if not isinstance(variant1, dict):
            return None
        execution_id = str(variant1.get("execution_id") or "")
        outer_call_id = str(variant1.get("outer_tool_call_id") or "")
        try:
            generation = int(variant1.get("generation") or 0)
        except (TypeError, ValueError):
            return None
        if (
            str(variant1.get("schema") or "")
            != "variant1.kernel-execution-admission.v1"
            or not execution_id
            or not outer_call_id
            or generation != self.generation
        ):
            return None
        return _ExecutionOrigin(
            execution_id=execution_id,
            outer_tool_call_id=outer_call_id,
            generation=generation,
        )

    def bind_execution_origin(self) -> contextvars.Token:
        # Context variables propagate into asyncio tasks and asyncio.to_thread.
        # A raw threading.Thread starts without this context and is rejected
        # rather than borrowing whichever cell later becomes active.
        token = self._execution_origin.set(self._parent_origin())
        self._origin_tokens.append(token)
        return token

    def reset_execution_origin(self) -> None:
        if not self._origin_tokens:
            self._execution_origin.set(None)
            return
        token = self._origin_tokens.pop()
        try:
            self._execution_origin.reset(token)
        except (RuntimeError, ValueError):
            # Never leave the worker with a borrowed admission if a task
            # crosses a context boundary.
            self._execution_origin.set(None)

    def _execution(self) -> tuple[str, str]:
        origin = self._execution_origin.get()
        if origin is None or origin.generation != self.generation:
            return "", ""
        return origin.execution_id, origin.outer_tool_call_id

    def _require_origin(self) -> _ExecutionOrigin:
        origin = self._execution_origin.get()
        if origin is None or origin.generation != self.generation:
            raise Variant1CapabilityError(
                "no_execution_admission",
                "Host capabilities need the current admitted cell context. Raw ThreadPoolExecutor threads do not inherit it; use capability.async_() or asyncio.to_thread() from the admitted cell.",
            )
        return origin

    def _observe_receipt_control(self, response: dict[str, Any]) -> None:
        receipt = (
            response.get("receipt")
            if isinstance(response.get("receipt"), dict)
            else {}
        )
        if not bool(receipt.get("terminate")):
            return
        execution_id, _outer_call_id = self._execution()
        if execution_id:
            result_metadata = (
                receipt.get("result_metadata")
                if isinstance(receipt.get("result_metadata"), dict)
                else {}
            )
            self._terminate_executions[execution_id] = str(
                result_metadata.get("terminal_observation") or ""
            )[:4_000]

    def _async_semaphore(self, *, control: bool = False) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._async_semaphore_lock:
            pool = self._async_control_semaphores if control else self._async_semaphores
            semaphore = pool.get(loop)
            if semaphore is None:
                semaphore = asyncio.Semaphore(1 if control else self.async_concurrency)
                pool[loop] = semaphore
            return semaphore

    def _signed_request(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with self._counter_lock:
            self._counter += 1
        request_id = str(payload.get("request_id") or f"kreq_{uuid.uuid4().hex}")
        return request_id, sign_envelope(self.secret, {
            "schema": BRIDGE_SCHEMA,
            "nonce": self.nonce,
            "generation": self.generation,
            "request_id": request_id,
            **payload,
        })

    def _verified_response(
        self,
        raw: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        response = verify_envelope(
            self.secret,
            raw,
            expected_nonce=self.nonce,
        )
        if str(response.get("request_id") or "") != request_id:
            raise BridgeProtocolError("bridge response request ID mismatch")
        if int(response.get("generation") or -1) != self.generation:
            raise BridgeProtocolError("bridge response generation mismatch")
        return response

    def _roundtrip(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            request_id, envelope = self._signed_request(payload)
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout_s
            ) as channel:
                channel.settimeout(self.timeout_s)
                write_sync_frame(
                    channel, envelope, max_bytes=self.max_frame_bytes
                )
                raw = read_sync_response(
                    channel, max_bytes=self.max_frame_bytes,
                    verify=lambda frame: self._verified_response(frame, request_id),
                )
            return self._verified_response(raw, request_id)

    async def _roundtrip_async(
        self,
        payload: dict[str, Any],
        *,
        timeout_s: float | None = None,
        bounded: bool = True,
        control: bool = False,
    ) -> dict[str, Any]:
        """Exchange one bridge frame without blocking the worker event loop."""

        request_id, envelope = self._signed_request(payload)

        async def exchange() -> dict[str, Any]:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout_s,
            )
            try:
                await write_async_frame(
                    writer,
                    envelope,
                    max_bytes=self.max_frame_bytes,
                )
                return await read_async_response(
                    reader, max_bytes=self.max_frame_bytes,
                    verify=lambda frame: self._verified_response(frame, request_id),
                    idle_timeout_s=self.timeout_s,
                )
            finally:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()

        async def admitted_exchange() -> dict[str, Any]:
            if not bounded and not control:
                return await exchange()
            async with self._async_semaphore(control=control):
                return await exchange()

        raw = await asyncio.wait_for(admitted_exchange(), timeout=(max(0.05, float(timeout_s)) if timeout_s is not None else None))
        return self._verified_response(raw, request_id)

    def handshake(self) -> None:
        response = self._roundtrip({
            "op": "handshake",
            "handshake": BRIDGE_HANDSHAKE,
            "execution_id": "",
            "outer_tool_call_id": "",
        })
        if not response.get("ok"):
            raise BridgeProtocolError(
                str((response.get("error") or {}).get("message") or "handshake rejected")
            )

    def invoke(self, descriptor: dict[str, Any] | str, args: dict[str, Any]) -> Any:
        execution_id, outer_call_id = self._execution()
        if not execution_id:
            raise Variant1CapabilityError(
                "no_execution_admission",
                "Host capabilities need the current admitted cell context. Raw ThreadPoolExecutor threads do not inherit it; use capability.async_() or asyncio.to_thread() from the admitted cell.",
            )
        details = (
            dict(descriptor)
            if isinstance(descriptor, dict)
            else {"ref_id": str(descriptor)}
        )
        response = self._roundtrip({
            "op": "invoke",
            "invocation_mode": "sync",
            "execution_id": execution_id,
            "outer_tool_call_id": outer_call_id,
            "ref_id": str(details.get("ref_id") or ""),
            "catalog_release_id": str(details.get("catalog_release_id") or ""),
            "category_id": str(details.get("category_id") or ""),
            "slot_id": str(details.get("slot_id") or ""),
            "slot_version": int(details.get("slot_version") or 0),
            "mount_revision": int(details.get("mount_revision") or 0),
            "args": _encode_host_argument(dict(args or {})),
        })
        refreshed = response.get("namespace")
        if isinstance(refreshed, dict):
            install_document(self.kernel, self, refreshed)
        if response.get("ok"):
            self._observe_receipt_control(response)
            result = _decode_host_result(response.get("result"), self)
            self._remember_unsuccessful_result(
                details,
                args,
                result,
                receipt=(
                    response.get("receipt")
                    if isinstance(response.get("receipt"), dict)
                    else None
                ),
            )
            return result
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        failure = self._remember_failure(
            details,
            args,
            code=str(error.get("code") or "capability_error"),
            message=str(error.get("message") or "Host capability failed."),
            receipt=(
                response.get("receipt")
                if isinstance(response.get("receipt"), dict)
                else None
            ),
        )
        raise Variant1CapabilityError(
            str(error.get("code") or "capability_error"),
            str(error.get("message") or "Host capability failed."),
            receipt=(
                response.get("receipt")
                if isinstance(response.get("receipt"), dict)
                else None
            ),
            failure=failure,
        )

    async def _signal_cancellation(
        self,
        origin: _ExecutionOrigin,
        target_request_id: str,
    ) -> None:
        try:
            await self._roundtrip_async(
                {
                    "op": "cancel",
                    "target_request_id": str(target_request_id),
                    "execution_id": origin.execution_id,
                    "outer_tool_call_id": origin.outer_tool_call_id,
                },
                timeout_s=min(2.0, self.timeout_s),
                bounded=False,
            )
        except Exception:
            # Cancellation is best effort at the transport boundary. The host's
            # cell admission and generation shutdown remain the final fence.
            return

    def invoke_async(
        self,
        descriptor: dict[str, Any] | str,
        args: dict[str, Any],
        *,
        deadline_ms: int | None = None,
    ) -> Any:
        """Return an awaitable bound to the cell origin at call construction."""

        origin = self._require_origin()
        details = (
            dict(descriptor)
            if isinstance(descriptor, dict)
            else {"ref_id": str(descriptor)}
        )
        selected_deadline = None
        if deadline_ms is not None:
            if isinstance(deadline_ms, bool):
                raise TypeError("_deadline_ms must be a positive integer")
            try:
                selected_deadline = int(deadline_ms)
            except (TypeError, ValueError) as exc:
                raise TypeError("_deadline_ms must be a positive integer") from exc
            if selected_deadline <= 0:
                raise ValueError("_deadline_ms must be a positive integer")
        request_id = f"kreq_{uuid.uuid4().hex}"
        payload: dict[str, Any] = {
            "op": "invoke",
            "invocation_mode": "async",
            "request_id": request_id,
            "execution_id": origin.execution_id,
            "outer_tool_call_id": origin.outer_tool_call_id,
            "ref_id": str(details.get("ref_id") or ""),
            "catalog_release_id": str(details.get("catalog_release_id") or ""),
            "category_id": str(details.get("category_id") or ""),
            "slot_id": str(details.get("slot_id") or ""),
            "slot_version": int(details.get("slot_version") or 0),
            "mount_revision": int(details.get("mount_revision") or 0),
            "args": _encode_host_argument(dict(args or {})),
        }
        if selected_deadline is not None:
            payload["deadline_ms"] = selected_deadline

        async def invoke_bound() -> Any:
            transport_timeout = None
            if selected_deadline is not None:
                transport_timeout = min(
                    self.timeout_s,
                    max(0.05, (selected_deadline / 1000.0) + 2.0),
                )
            try:
                response = await self._roundtrip_async(
                    payload,
                    timeout_s=transport_timeout,
                    # This is only a client-side queue hint. The host resolves
                    # the exact control and ownership before granting capacity.
                    control=bool(details.get("_control_hint")),
                )
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(asyncio.wait_for(
                        self._signal_cancellation(origin, request_id),
                        timeout=min(2.5, self.timeout_s + 0.5),
                    ))
                raise
            except TimeoutError as exc:
                with suppress(Exception):
                    await asyncio.shield(asyncio.wait_for(
                        self._signal_cancellation(origin, request_id),
                        timeout=min(2.5, self.timeout_s + 0.5),
                    ))
                failure = self._remember_failure(
                    details,
                    args,
                    code="bridge_async_timeout",
                    message=(
                        "Awaitable host capability exceeded its bridge transport deadline."
                    ),
                )
                raise Variant1CapabilityError(
                    "bridge_async_timeout",
                    "Awaitable host capability exceeded its bridge transport deadline.",
                    failure=failure,
                ) from exc
            refreshed = response.get("namespace")
            if isinstance(refreshed, dict):
                install_document(self.kernel, self, refreshed)
            if response.get("ok"):
                self._observe_receipt_control(response)
                result = _decode_host_result(response.get("result"), self)
                self._remember_unsuccessful_result(
                    details,
                    args,
                    result,
                    receipt=(
                        response.get("receipt")
                        if isinstance(response.get("receipt"), dict)
                        else None
                    ),
                )
                return result
            error = (
                response.get("error")
                if isinstance(response.get("error"), dict)
                else {}
            )
            failure = self._remember_failure(
                details,
                args,
                code=str(error.get("code") or "capability_error"),
                message=str(error.get("message") or "Host capability failed."),
                receipt=(
                    response.get("receipt")
                    if isinstance(response.get("receipt"), dict)
                    else None
                ),
            )
            raise Variant1CapabilityError(
                str(error.get("code") or "capability_error"),
                str(error.get("message") or "Host capability failed."),
                receipt=(
                    response.get("receipt")
                    if isinstance(response.get("receipt"), dict)
                    else None
                ),
                failure=failure,
            )

        return invoke_bound()

    def invoke_many(
        self,
        calls: list[tuple[dict[str, Any], dict[str, Any]]],
        *,
        max_concurrency: int | None = None,
    ) -> list[Any]:
        """Invoke one authenticated, host-scheduled ordered capability batch."""

        execution_id, outer_call_id = self._execution()
        if not execution_id:
            raise Variant1CapabilityError(
                "no_execution_admission",
                "Host capabilities need the current admitted cell context. Raw ThreadPoolExecutor threads do not inherit it; use capability.async_() or asyncio.to_thread() from the admitted cell.",
            )
        payload_calls = []
        for descriptor, arguments in calls:
            details = dict(descriptor or {})
            payload_calls.append({
                "ref_id": str(details.get("ref_id") or ""),
                "catalog_release_id": str(details.get("catalog_release_id") or ""),
                "category_id": str(details.get("category_id") or ""),
                "slot_id": str(details.get("slot_id") or ""),
                "slot_version": int(details.get("slot_version") or 0),
                "mount_revision": int(details.get("mount_revision") or 0),
                "args": dict(arguments or {}),
            })
        payload: dict[str, Any] = {
            "op": "invoke_many",
            "execution_id": execution_id,
            "outer_tool_call_id": outer_call_id,
            "calls": payload_calls,
        }
        if max_concurrency is not None:
            payload["max_concurrency"] = int(max_concurrency)
        response = self._roundtrip(payload)
        refreshed = response.get("namespace")
        if isinstance(refreshed, dict):
            install_document(self.kernel, self, refreshed)
        if response.get("ok"):
            results = response.get("results")
            if not isinstance(results, list):
                raise BridgeProtocolError("bridge batch response results are invalid")
            return [_decode_host_result(result, self) for result in results]
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        receipts = response.get("receipts") if isinstance(response.get("receipts"), list) else []
        raise Variant1CapabilityError(
            str(error.get("code") or "capability_batch_error"),
            str(error.get("message") or "Host capability batch failed."),
            receipt=(receipts[0] if receipts and isinstance(receipts[0], dict) else None),
        )


class CapabilityProxy:
    def __init__(
        self,
        descriptor: dict[str, Any],
        bridge: KernelBridgeClient,
        *,
        namespace: str = "tools",
    ):
        self._descriptor = dict(descriptor)
        self._bridge = bridge
        self.__name__ = _identifier(str(descriptor.get("alias") or ""))
        self.__qualname__ = f"{namespace}.{self.__name__}"
        description = str(descriptor.get("description") or "")
        self.__doc__ = (
            description
            + ("\n\n" if description else "")
            + f"Awaitable form: await {self.__qualname__}.async_(..., _deadline_ms=None)"
        )
        params = []
        raw_params = descriptor.get("params") if isinstance(descriptor.get("params"), dict) else {}
        required: list[tuple[str, dict]] = []
        optional: list[tuple[str, dict]] = []
        for name, spec in raw_params.items():
            item = (_identifier(str(name)), spec if isinstance(spec, dict) else {})
            (required if item[1].get("required") else optional).append(item)
        ordered = required + optional
        # Catalog artifacts are canonical JSON, so object keys are sorted when
        # they cross the host/worker boundary.  The catalog's explicit
        # signature retains the author-declared Python argument order; recover
        # it here so positional calls do not silently bind to alphabetical
        # fields when a mounted method is called positionally.
        raw_signature = str(descriptor.get("signature") or "").strip()
        left = raw_signature.find("(")
        right = raw_signature.rfind(")")
        if 0 <= left < right:
            # Defaults may contain commas, quoted text or nested containers.
            # Parse only the syntax; no default expression is ever evaluated.
            try:
                parsed = ast.parse("def _contract" + raw_signature[left:right + 1] + ": pass")
                arguments = parsed.body[0].args
                signature_names = [item.arg for item in
                                   arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
            except (SyntaxError, ValueError):
                signature_names = []
            by_name = {name: spec for name, spec in ordered}
            if (
                len(signature_names) == len(by_name)
                and len(set(signature_names)) == len(signature_names)
                and set(signature_names) == set(by_name)
            ):
                ordered = [(name, by_name[name]) for name in signature_names]
        self._ordered_names = [name for name, _ in ordered]
        self._param_specs = {name: dict(spec) for name, spec in ordered}
        for name, spec in ordered:
            annotation = {
                "string": str,
                "integer": int,
                "number": float,
                "boolean": bool,
                "array": list,
                "object": dict,
            }.get(str(spec.get("type") or "").lower(), inspect.Parameter.empty)
            params.append(inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=(inspect.Parameter.empty if spec.get("required") else spec.get("default")),
                annotation=annotation,
            ))
        self.__signature__ = inspect.Signature(params)
        self.async_ = _AsyncCapabilityCall(self)

    def _arguments(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) > len(self._ordered_names):
            raise TypeError(
                f"Invalid call to {self.__qualname__}{self.__signature__}: "
                f"received {len(args)} positional arguments, accepts at most "
                f"{len(self._ordered_names)}. Inspect "
                f"{self.__qualname__}.documentation() for the exact contract."
            )
        values = dict(kwargs)
        positional = list(args)
        if (
            len(positional) == 1
            and isinstance(positional[0], list)
            and "argv" in self._ordered_names
            and "command" in self._ordered_names
            and "argv" not in values
            and "command" not in values
            and all(isinstance(item, str) for item in positional[0])
        ):
            # ``run_command([program, arg, ...], cwd=...)`` is the natural
            # Python spelling of an exact argv invocation.  Do not bind that
            # list to the neighboring ``command`` string parameter.
            values["argv"] = list(positional[0])
            positional = []
        elif len(positional) == 1 and not kwargs and isinstance(positional[0], dict):
            candidate = dict(positional[0])
            if set(candidate).issubset(set(self._ordered_names)):
                # Accept the conventional provider-style argument envelope in
                # addition to normal Python keyword arguments.
                values = candidate
                positional = []
            elif len(self._ordered_names) == 1:
                name = self._ordered_names[0]
                spec = self._param_specs.get(name) or {}
                items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
                if str(spec.get("type") or "").lower() == "array" and str(
                    items.get("type") or ""
                ).lower() == "object":
                    # A single object is an unambiguous one-item array for
                    # batch-shaped capabilities such as apply_patch.
                    values = {name: [candidate]}
                    positional = []
        elif len(positional) == 1 and not kwargs:
            candidate = positional[0]
            if (
                "selection" in self._ordered_names
                and isinstance(candidate, list)
                and len(candidate) == 1
                and isinstance(candidate[0], dict)
            ):
                # Discovery APIs return ranked lists. Passing an unambiguous
                # one-item result directly into a selection-shaped capability
                # should compose without forcing list ceremony on small models.
                values = {"selection": dict(candidate[0])}
                positional = []
            elif (
                "selection" in self._ordered_names
                and "tool_name" in self._ordered_names
                and isinstance(candidate, str)
            ):
                # A lone name is the natural unique-tool shorthand; the host
                # still rejects ambiguity before any connector call.
                values = {"tool_name": candidate}
                positional = []
        for index, value in enumerate(positional):
            name = self._ordered_names[index]
            if name in values:
                raise TypeError(f"{self.__qualname__} got multiple values for {name!r}")
            values[name] = value
        try:
            bound = self.__signature__.bind(**values)
        except TypeError as exc:
            raise TypeError(
                f"Invalid call to {self.__qualname__}{self.__signature__}: {exc}. "
                f"Inspect {self.__qualname__}.documentation() for the exact contract."
            ) from None
        clean = {
            key: value
            for key, value in bound.arguments.items()
            if value is not None
        }
        fixed_arguments = self._descriptor.get("fixed_arguments")
        if isinstance(fixed_arguments, dict):
            # Dispatcher method identity always wins over caller kwargs so a
            # method cannot override ``operation`` by accident.
            clean = {**clean, **dict(fixed_arguments)}
        if self._descriptor.get("argument_envelope"):
            clean = {"arguments": clean}
        return clean

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._bridge.invoke(
            self._descriptor,
            self._arguments(args, kwargs),
        )

    def documentation(self) -> dict[str, Any]:
        """Return this mounted capability's local, non-invoking contract."""

        return {
            "name": self.__qualname__,
            "signature": str(
                self._descriptor.get("signature") or self.__signature__
            ),
            "awaitable": (
                f"await {self.__qualname__}.async_(..., _deadline_ms=None)"
            ),
            "description": str(self._descriptor.get("description") or ""),
            "effect_class": str(self._descriptor.get("effect_class") or ""),
            "params": dict(self._descriptor.get("params") or {}),
            "when": str(self._descriptor.get("when") or ""),
            "avoid": str(self._descriptor.get("avoid") or ""),
        }

    def describe(self) -> dict[str, Any]:
        """Familiar alias for :meth:`documentation` on one direct seed."""

        return self.documentation()

    def __repr__(self) -> str:
        return f"<VARIANT-1 capability {self.__qualname__}{self.__signature__}>"


class _AsyncCapabilityCall:
    """Explicit awaitable view over one existing capability proxy."""

    def __init__(self, capability: CapabilityProxy) -> None:
        self._capability = capability
        self.__name__ = "async_"
        self.__qualname__ = f"{capability.__qualname__}.async_"
        self.__doc__ = (
            f"Await {capability.__qualname__} without blocking the Python loop. "
            "Use _deadline_ms to request a shorter host-owned deadline."
        )
        parameters = list(capability.__signature__.parameters.values())
        parameters.append(inspect.Parameter(
            "_deadline_ms",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=int,
        ))
        self.__signature__ = inspect.Signature(parameters)

    def __call__(
        self,
        *args: Any,
        _deadline_ms: int | None = None,
        **kwargs: Any,
    ) -> Any:
        # This regular (not async) function captures the immutable origin now,
        # when the awaitable is constructed. Awaiting it in a later cell cannot
        # borrow that later cell's admission.
        return self._capability._bridge.invoke_async(
            self._capability._descriptor,
            self._capability._arguments(args, kwargs),
            deadline_ms=_deadline_ms,
        )

    def __repr__(self) -> str:
        return f"<VARIANT-1 awaitable capability {self.__qualname__}{self.__signature__}>"


class ReadOnlyTools:
    _RESERVED = frozenset({"aliases", "methods", "describe", "documentation"})

    def __init__(
        self,
        descriptors: list[dict[str, Any]],
        bridge: KernelBridgeClient,
        *,
        namespace: str = "tools",
        kernel_context: Any = None,
    ):
        object.__setattr__(self, "_locked", False)
        object.__setattr__(self, "_descriptors", {})
        object.__setattr__(self, "_namespace", str(namespace))
        object.__setattr__(self, "_kernel_context", kernel_context)
        for descriptor in descriptors:
            alias = _identifier(str(descriptor.get("alias") or ""))
            if alias in self._RESERVED:
                raise ValueError(
                    f"tools capability alias conflicts with discovery API: {alias}"
                )
            if alias in self._descriptors:
                raise ValueError(f"duplicate kernel capability alias: {alias}")
            if str(descriptor.get("kind") or ""):
                raise ValueError("tools accepts direct seed descriptors only")
            self._descriptors[alias] = dict(descriptor)
            object.__setattr__(
                self,
                alias,
                CapabilityProxy(descriptor, bridge, namespace=namespace),
            )
        object.__setattr__(self, "_locked", True)

    def __getattr__(self, name: str) -> Any:
        # Diagnose a miss against the current protected globals, including when
        # this tools object was retained across a later category mount. Never
        # return an alias, or advertise an unmounted/user-replaced object.
        context = object.__getattribute__(self, "_kernel_context")
        protected = getattr(context, "protected_globals", {})
        target = protected.get(name)
        if (
            isinstance(target, (MountedPythonAPI, MountedSeedObject, ToolbeltNamespace))
            and context.namespace.get(name) is target
        ):
            raise AttributeError(
                f"{name} is a top-level global; use {name}.<method>(...), "
                f"not {self._namespace}.{name}.<method>(...)"
            )
        raise AttributeError(self._missing_message(name))

    def _missing_message(self, name: str) -> str:
        context = object.__getattribute__(self, "_kernel_context")
        protected = getattr(context, "protected_globals", {})
        toolbelt = protected.get("toolbelt")
        if isinstance(toolbelt, ToolbeltNamespace):
            return toolbelt.missing_capability(name, namespace=self._namespace)
        return f"{self._namespace}.{name} is unavailable; use {self._namespace}.methods() for mounted names."

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError(
                f"the official VARIANT-1 {self._namespace} namespace is read-only"
            )
        object.__setattr__(self, name, value)

    def aliases(self) -> list[str]:
        return sorted(self._descriptors)

    def methods(self) -> list[str]:
        """Use the same discovery idiom as every other mounted API."""

        return self.aliases()

    def describe(self, alias: str) -> dict[str, Any]:
        clean = _identifier(alias)
        if clean in self._descriptors:
            return dict(self._descriptors[clean])
        raise KeyError(self._missing_message(alias))

    def documentation(self, alias: str | None = None) -> dict[str, Any]:
        """Return one exact contract or a compact mounted-seed index."""

        if alias is not None:
            clean = _identifier(str(alias))
            capability = getattr(self, clean, None)
            if not isinstance(capability, CapabilityProxy):
                raise KeyError(self._missing_message(alias))
            return capability.documentation()
        return {
            name: {
                key: value
                for key, value in getattr(self, name).documentation().items()
                if key in {"signature", "description", "effect_class"}
            }
            for name in self.aliases()
        }

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self.aliases()))

    def __repr__(self) -> str:
        return (
            f"<VARIANT-1 mounted {self._namespace}: "
            f"{', '.join(self.aliases())}>"
        )


class MountedPythonAPI:
    """Read-only, lazily documented object for one mounted category API."""

    _RESERVED = frozenset({"documentation", "describe", "methods"})

    def __init__(
        self,
        descriptor: dict[str, Any],
        bridge: KernelBridgeClient,
    ) -> None:
        object.__setattr__(self, "_locked", False)
        object.__setattr__(self, "_descriptor", dict(descriptor))
        name = _identifier(str(descriptor.get("name") or ""))
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "__name__", name)
        object.__setattr__(self, "__qualname__", name)
        object.__setattr__(self, "__doc__", str(descriptor.get("summary") or ""))
        method_descriptors: dict[str, dict[str, Any]] = {}
        for method in descriptor.get("methods") or ():
            if not isinstance(method, dict):
                continue
            alias = _identifier(str(method.get("alias") or ""))
            if not alias or alias in method_descriptors or alias in self._RESERVED:
                raise ValueError(f"invalid or duplicate method in {name} API: {alias!r}")
            method_descriptors[alias] = dict(method)
            object.__setattr__(
                self, alias, CapabilityProxy(method, bridge, namespace=name))
        if not method_descriptors:
            raise ValueError(f"{name} API has no admitted methods")
        object.__setattr__(self, "_method_descriptors", method_descriptors)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError(f"the official VARIANT-1 {self._name} API is read-only")
        object.__setattr__(self, name, value)

    @staticmethod
    def _documentation_row(descriptor: dict[str, Any]) -> dict[str, Any]:
        return {
            "signature": str(descriptor.get("signature") or ""),
            "awaitable": "append .async_(..., _deadline_ms=None) to the method",
            "description": str(descriptor.get("description") or ""),
            "effect_class": str(descriptor.get("effect_class") or ""),
            "params": dict(descriptor.get("params") or {}),
        }

    def documentation(self, method: str | None = None) -> dict[str, Any]:
        """Load one exact contract or a compact method index locally."""
        if method is not None:
            clean = _identifier(str(method))
            descriptor = self._method_descriptors.get(clean)
            if descriptor is None:
                raise KeyError(method)
            return {
                "api": self._name,
                "method": clean,
                **self._documentation_row(descriptor),
            }
        return {
            "api": self._name,
            "summary": str(self._descriptor.get("summary") or ""),
            "methods": {
                name: {
                    key: value
                    for key, value in self._documentation_row(descriptor).items()
                    if key in {"signature", "description", "effect_class"}
                }
                for name, descriptor in sorted(self._method_descriptors.items())
            },
        }

    def describe(self, method: str) -> dict[str, Any]:
        return self.documentation(method)

    def methods(self) -> list[str]:
        return sorted(self._method_descriptors)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._method_descriptors))

    def __repr__(self) -> str:
        return (
            f"<VARIANT-1 {self._name} API; "
            f"call {self._name}.methods(), then {self._name}.describe(name)>"
        )


class MountedSeedObject(MountedPythonAPI):
    """One slot-owned thin seed with local method projection."""

    def __repr__(self) -> str:
        return (
            f"<VARIANT-1 {self._name} mounted seed; "
            f"call {self._name}.methods(), then {self._name}.describe(name)>"
        )


def _helper_value_schema(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, (list, tuple)):
        return {"type": "array"}
    if isinstance(value, Mapping):
        return {"type": "object"}
    return None


def _helper_annotation_schema(annotation: Any) -> dict[str, Any] | None:
    if annotation is inspect.Parameter.empty or annotation is None:
        return None
    origin = typing.get_origin(annotation)
    if origin in {list, tuple, set, frozenset}:
        return {"type": "array"}
    if origin in {dict, Mapping}:
        return {"type": "object"}
    if origin in {typing.Union, types.UnionType}:
        options = [
            _helper_annotation_schema(item)
            for item in typing.get_args(annotation)
            if item is not type(None)
        ]
        options = [item for item in options if item is not None]
        return options[0] if len(options) == 1 else None
    direct = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        tuple: "array",
        dict: "object",
    }.get(annotation)
    if direct:
        return {"type": direct}
    text = str(annotation).strip("'\"").casefold()
    text = text.removeprefix("typing.")
    root = text.split("[", 1)[0]
    named = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "list": "array",
        "tuple": "array",
        "dict": "object",
        "mapping": "object",
    }.get(root)
    return {"type": named} if named else None


def _normalized_promotion_tests(
    tests: Any = None,
    *,
    allow_local_assertions: bool,
) -> list[dict[str, Any]]:
    """Normalize one test or an iterable and run local assertions once."""

    normalized: list[dict[str, Any]] = []
    if tests is None:
        supplied: list[Any] = []
    elif callable(tests) or isinstance(tests, (Mapping, str)):
        supplied = [tests]
    else:
        try:
            supplied = list(tests)
        except TypeError as exc:
            raise TypeError(
                "tests must be one declarative case or synchronous zero-argument "
                "assertion, or an iterable of those values"
            ) from exc
    for index, raw in enumerate(supplied):
        value = raw
        if callable(value):
            if not allow_local_assertions:
                raise TypeError(
                    f"tests[{index}] must be a declarative case object for explicit "
                    "source authoring"
                )
            if inspect.iscoroutinefunction(value):
                raise TypeError(
                    f"tests[{index}] must be a synchronous zero-argument assertion"
                )
            signature = inspect.signature(value)
            required = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind not in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }
            ]
            if required:
                raise TypeError(
                    f"tests[{index}] must take no required arguments"
                )
            outcome = value()
            if inspect.isawaitable(outcome):
                raise TypeError(
                    f"tests[{index}] returned an awaitable; use a synchronous assertion"
                )
            if outcome is False:
                raise AssertionError(f"tests[{index}] returned False")
            # The assertion has already run in the same trusted Python session.
            # Host validation and probation still gate activation.
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TypeError(
                    f"tests[{index}] must be a declarative case object, not Python "
                    "test source"
                ) from exc
        if not isinstance(value, Mapping):
            raise TypeError(
                f"tests[{index}] must be a declarative case object or a synchronous "
                "zero-argument assertion"
            )
        try:
            clean = json.loads(
                json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise TypeError(f"tests[{index}] must contain only JSON values") from exc
        normalized.append(clean)
    return normalized


def _promoted_helper_contract(
    helper: Any,
    tests: list[Any] | None = None,
    *,
    schema_override: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Project one ordinary Python function into the mutation contract."""

    if not inspect.isfunction(helper) or inspect.iscoroutinefunction(helper):
        raise TypeError("promote_helper expects one synchronous Python function")
    if helper.__closure__:
        raise ValueError(
            "promoted helpers cannot close over cell-local values; pass them as arguments"
        )
    signature = inspect.signature(helper)
    tests = list(tests or ())
    properties: dict[str, Any] = {}
    required: list[str] = []
    test_arguments = [
        dict(row.get("arguments") or {})
        for row in tests
        if isinstance(row, Mapping) and isinstance(row.get("arguments"), Mapping)
    ]
    for name, parameter in signature.parameters.items():
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            raise TypeError(
                "promoted helpers use named JSON parameters; *args, **kwargs, and "
                "positional-only parameters are unsupported"
            )
        spec = _helper_annotation_schema(parameter.annotation)
        if spec is None and parameter.default is not inspect.Parameter.empty:
            spec = _helper_value_schema(parameter.default)
        if spec is None:
            observed = [row[name] for row in test_arguments if name in row]
            inferred = {
                json.dumps(_helper_value_schema(value), sort_keys=True)
                for value in observed
                if _helper_value_schema(value) is not None
            }
            if len(inferred) == 1:
                spec = json.loads(inferred.pop())
        if spec is None:
            # The mounted bridge already validates that values are transportable.
            # Unknown annotations should not force ceremony merely to preserve a
            # working helper; keep the parameter provider-neutral instead.
            spec = {"type": "any"}
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        elif parameter.default is not None:
            try:
                json.dumps(parameter.default, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"parameter {name!r} has a non-JSON default"
                ) from exc
            spec = {**spec, "default": parameter.default}
        properties[name] = spec

    try:
        raw_source = textwrap.dedent(inspect.getsource(helper))
    except (OSError, TypeError) as exc:
        raise ValueError(
            "the helper source is unavailable; define it in a Python cell and retry"
        ) from exc
    tree = ast.parse(raw_source, mode="exec")
    candidates = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == helper.__name__
    ]
    if len(candidates) != 1:
        raise ValueError("could not isolate one helper function for promotion")
    function_node = candidates[0]
    function_node.decorator_list = []
    function_node.returns = None
    # Defaults have already been evaluated by CPython. Replaying their source
    # expressions can lose definition-only names or repeat side effects.
    # Materialize the values belonging to this function, including kw-only ones.
    def default_expression(name):
        return ast.parse(repr(signature.parameters[name].default), mode="eval").body

    positional_defaults = function_node.args.args[
        len(function_node.args.args) - len(function_node.args.defaults):
    ] if function_node.args.defaults else []
    function_node.args.defaults = [default_expression(arg.arg) for arg in positional_defaults]
    function_node.args.kw_defaults = [
        None if signature.parameters[arg.arg].default is inspect.Parameter.empty
        else default_expression(arg.arg)
        for arg in function_node.args.kwonlyargs
    ]
    for argument in (
        list(function_node.args.posonlyargs)
        + list(function_node.args.args)
        + list(function_node.args.kwonlyargs)
    ):
        argument.annotation = None
    prelude: list[ast.stmt] = []
    try:
        closure_variables = inspect.getclosurevars(helper)
    except TypeError:
        closure_variables = None
    if closure_variables is not None:
        for name, value in sorted(closure_variables.globals.items()):
            if isinstance(value, (CapabilityProxy, ReadOnlyTools, MountedPythonAPI)):
                # Mounted capabilities are resolved as mutation dependencies.
                continue
            statement = ""
            if isinstance(value, types.ModuleType):
                module_name = str(getattr(value, "__name__", "") or "")
                if module_name and all(part.isidentifier() for part in module_name.split(".")):
                    statement = f"import {module_name} as {name}"
            elif inspect.isclass(value) or inspect.isfunction(value):
                module_name = str(getattr(value, "__module__", "") or "")
                member_name = str(getattr(value, "__name__", "") or "")
                if (
                    module_name not in {"", "__main__"}
                    and all(part.isidentifier() for part in module_name.split("."))
                    and member_name.isidentifier()
                ):
                    statement = f"from {module_name} import {member_name} as {name}"
            else:
                try:
                    json.dumps(value, allow_nan=False)
                except (TypeError, ValueError):
                    pass
                else:
                    statement = f"{name} = {value!r}"
            if not statement:
                raise ValueError(
                    f"referenced global {name!r} cannot be packaged; import it inside "
                    "the helper or pass a JSON value as an argument"
                )
            prelude.extend(ast.parse(statement, mode="exec").body)
    module = ast.Module(body=[*prelude, function_node], type_ignores=[])
    ast.fix_missing_locations(module)
    packaged_source = ast.unparse(module)
    if helper.__name__ == "run":
        if schema_override is None:
            raise ValueError(
                "run(arguments) is the explicit-source form; pass schema=... or "
                "define an ordinary helper with named parameters"
            )
        candidate_source = packaged_source + "\n"
    else:
        candidate_source = (
            packaged_source
            + "\n\ndef run(arguments):\n"
            + f"    return {helper.__name__}(**arguments)\n"
        )
    schema = (
        json.loads(json.dumps(schema_override, ensure_ascii=False, allow_nan=False))
        if schema_override is not None
        else {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(required),
            "properties": properties,
        }
    )
    return helper.__name__, schema, candidate_source


class ToolbeltNamespace:
    """Local, non-authoritative discovery view over the pinned catalog."""

    def __init__(
        self,
        document: dict[str, Any],
        bridge: KernelBridgeClient | None = None,
        control_descriptor: dict[str, Any] | None = None,
    ):
        self._document = json.loads(json.dumps(document))
        self._index = list(self._document.get("catalog_index") or ())
        self._bridge = bridge
        self._control_methods: tuple[str, ...] = ()
        self._control_object: MountedSeedObject | None = None
        self._control_callables: dict[str, Any] = {}
        if bridge is not None and isinstance(control_descriptor, dict):
            control = MountedSeedObject(control_descriptor, bridge)
            self._control_object = control
            names = tuple(control.methods())
            for name in names:
                method = getattr(control, name)
                self._control_callables[name] = method
                if name in {"mutate", "synthesize"}:
                    continue
                if hasattr(self, name):
                    raise ValueError(f"toolbelt control method conflicts: {name}")
                setattr(self, name, method)
            self._control_methods = names

    def documentation(self, method: str | None = None) -> dict[str, Any]:
        if method == "last_failure":
            return {
                "api": "toolbelt",
                "method": "last_failure",
                "signature": "last_failure()",
                "description": (
                    "Return the latest failed or unsuccessful mounted capability call "
                    "as a typed mutation target, including its public arguments."
                ),
                "effect_class": "pure",
            }
        if method == "mutate" and "propose_activate" in self._control_methods:
            return {
                "api": "toolbelt",
                "method": "mutate",
                "signature": (
                    "mutate(target, using=helper, *, invoke=None, tests=None, "
                    "purpose=None)"
                ),
                "description": (
                    "Replace one mounted callable for this durable chat. Pass the "
                    "callable itself, such as computer.click, or its qualified name. "
                    "Define the replacement as an ordinary synchronous helper with "
                    "named parameters. VARIANT-1 infers its slot, schema, source, and "
                    "activation lifecycle. Optional tests may be declarative case "
                    "objects or zero-argument assertion functions."
                ),
                "effect_class": "write",
            }
        if method == "synthesize" and "propose_activate" in self._control_methods:
            return {
                "api": "toolbelt",
                "method": "synthesize",
                "signature": (
                    "synthesize(helper, *, tests=None, slot=None, alias=None, "
                    "purpose=None, invoke=None)"
                ),
                "description": (
                    "Turn an ordinary synchronous Python helper with named parameters "
                    "into a bounded session tool. The first vacancy in the current "
                    "category is used when slot is omitted. Optional tests may be "
                    "declarative case objects or zero-argument assertion functions."
                ),
                "effect_class": "write",
            }
        if method == "promote_helper" and "synthesize" in self._control_methods:
            return {
                "api": "toolbelt",
                "method": "promote_helper",
                "signature": (
                    "promote_helper(helper, *, tests=None, slot=None, alias=None, "
                    "purpose=None, invoke=None)"
                ),
                "description": (
                    "Turn a successful Python function into a tested "
                    "session-local tool. The current category's first available "
                    "vacancy is used when slot is omitted."
                ),
                "effect_class": "write",
            }
        if self._control_object is None:
            return {
                "api": "toolbelt",
                "summary": "Local catalog discovery; no host control is mounted.",
                "methods": {},
            }
        result = self._control_object.documentation(method)
        if method is None:
            result = dict(result)
            methods = dict(result.get("methods") or {})
            local_names = ["last_failure"]
            if "synthesize" in self._control_methods:
                local_names.extend(("mutate", "synthesize", "promote_helper"))
            for local_name in local_names:
                local = self.documentation(local_name)
                methods[local_name] = {
                    "signature": local["signature"],
                    "description": local["description"],
                    "effect_class": local["effect_class"],
                }
            result["methods"] = methods
        return result

    def methods(self) -> list[str]:
        names = list(self._control_methods)
        if self._bridge is not None:
            names.append("last_failure")
        if "synthesize" in self._control_methods:
            names.append("promote_helper")
        return sorted(set(names))

    def last_failure(self) -> Variant1CapabilityFailure | None:
        """Return the latest capability failure recorded by this kernel bridge."""

        return self._bridge.last_failure() if self._bridge is not None else None

    def _promotion_slot(self, slot: str | None) -> str:
        selected = str(self._document.get("selected_category_id") or "")
        requested = str(slot or "").strip()
        if requested:
            if requested.isdecimal() and int(requested) > 0:
                if not selected:
                    raise ValueError(
                        "numeric mutation slot requires a selected category"
                    )
                return f"{selected}/{int(requested)}"
            return requested
        for row in self._document.get("category_options") or ():
            if str(row.get("category_id") or "") != selected:
                continue
            vacancies = [
                str(item) for item in (row.get("vacant_slot_ids") or ())
                if str(item)
            ]
            if vacancies:
                return vacancies[0]
        raise ValueError(
            "the current category has no available vacancy; mount another category "
            "or pass an explicit vacant slot"
        )

    def _control(self, name: str) -> Any:
        method = self._control_callables.get(str(name))
        if not callable(method) and self._control_object is not None:
            method = getattr(self._control_object, str(name), None)
        if not callable(method):
            raise RuntimeError(f"toolbelt.{name} is not available")
        return method

    def _target_descriptor(self, target: Any) -> dict[str, Any]:
        if isinstance(target, Variant1CapabilityFailure):
            descriptor = dict(target.descriptor)
        elif isinstance(target, CapabilityProxy):
            descriptor = dict(target._descriptor)
        elif isinstance(target, str):
            requested = target.strip().replace("-", "_")
            if requested and "." not in requested:
                requested = f"tools.{requested}"
            matches: list[dict[str, Any]] = []
            for row in self._document.get("capabilities") or ():
                if not isinstance(row, Mapping):
                    continue
                qualified = f"tools.{row.get('alias')}"
                if requested == qualified:
                    matches.append(dict(row))
            for root, raw_object in dict(
                self._document.get("mounted_objects") or {}
            ).items():
                if not isinstance(raw_object, Mapping):
                    continue
                for row in raw_object.get("methods") or ():
                    if not isinstance(row, Mapping):
                        continue
                    if requested == f"{root}.{row.get('alias')}":
                        matches.append(dict(row))
            if len(matches) != 1:
                raise ValueError(
                    f"mutation target is not one mounted callable: {target!r}"
                )
            descriptor = matches[0]
        else:
            raise TypeError(
                "toolbelt.mutate target must be a mounted callable, last failure, "
                "or qualified name"
            )
        category = str(descriptor.get("category_id") or "")
        position = int(descriptor.get("position") or 0)
        alias = _identifier(str(descriptor.get("alias") or ""))
        if not category or position <= 0 or not alias:
            raise ValueError("mutation target is not owned by a mutable Toolbelt slot")
        return descriptor

    def mutate(
        self,
        target: Any = None,
        using: Any = None,
        *,
        invoke: dict[str, Any] | None = None,
        tests: Any = None,
        purpose: str | None = None,
        slot: str | None = None,
        source: str | None = None,
    ) -> Any:
        """Replace one mounted callable or retain the explicit source API."""

        if using is None:
            if target is not None:
                raise TypeError("toolbelt.mutate needs using=<Python helper>")
            if not slot or source is None:
                raise TypeError(
                    "toolbelt.mutate needs a target and helper, or slot/source"
                )
            payload = {
                "slot": slot,
                "source": source,
                "tests": _normalized_promotion_tests(
                    tests, allow_local_assertions=False
                ),
                "purpose": str(purpose or ""),
            }
            if invoke is not None:
                payload["invoke"] = dict(invoke)
            return self._control("mutate")(
                **payload,
            )
        if "propose_activate" not in self._control_methods:
            raise RuntimeError("session mutation authoring is not available")
        descriptor = self._target_descriptor(target)
        normalized_tests = _normalized_promotion_tests(
            tests, allow_local_assertions=True
        )
        _helper_name, schema, candidate_source = _promoted_helper_contract(
            using, normalized_tests
        )
        category = str(descriptor["category_id"])
        position = int(descriptor["position"])
        alias = _identifier(str(descriptor["alias"]))
        namespace = str(descriptor.get("namespace") or "tools")
        active_kind = str(descriptor.get("declared_kind") or "")
        if (
            bool(descriptor.get("session_local"))
            and namespace == "tools"
            and active_kind in {"create", "revise"}
        ):
            kind = "revise"
        elif (
            str(descriptor.get("kind") or "") == "mounted_object_method"
            or namespace != "tools"
        ):
            kind = "method"
        else:
            kind = "mutate"
        short_slot = f"{category}/{position}"
        payload = {
            "kind": kind,
            "slot": short_slot,
            "parent": short_slot,
            "alias": alias,
            "purpose": str(
                purpose
                or inspect.getdoc(using)
                or f"Session replacement for {namespace}.{alias}"
            ).strip(),
            "schema": schema,
            "source": candidate_source,
        }
        if normalized_tests:
            payload["tests"] = normalized_tests
        if invoke is not None:
            payload["invoke"] = dict(invoke)
        return self._control("propose_activate")(
            **payload,
        )

    def synthesize(
        self,
        helper: Any = None,
        *,
        tests: Any = None,
        slot: str | None = None,
        alias: str | None = None,
        purpose: str | None = None,
        invoke: dict[str, Any] | None = None,
        schema: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> Any:
        """Create one bounded session tool from a helper or explicit source."""

        if helper is None and source is not None:
            if not slot or not alias or not purpose or schema is None:
                raise TypeError(
                    "explicit synthesis needs slot, alias, purpose, schema, and source"
                )
            payload = {
                "slot": slot,
                "alias": alias,
                "purpose": purpose,
                "schema": schema,
                "source": source,
                "tests": _normalized_promotion_tests(
                    tests, allow_local_assertions=False
                ),
            }
            if invoke is not None:
                payload["invoke"] = dict(invoke)
            return self._control("synthesize")(**payload)
        if helper is None:
            raise TypeError("toolbelt.synthesize needs one Python helper")
        normalized_tests = _normalized_promotion_tests(
            tests, allow_local_assertions=True
        )
        helper_name, inferred_schema, candidate_source = _promoted_helper_contract(
            helper,
            normalized_tests,
            schema_override=schema,
        )
        clean_alias = _identifier(str(alias or helper_name))
        clean_purpose = str(
            purpose
            or inspect.getdoc(helper)
            or f"Synthesized session helper {clean_alias}"
        ).strip()
        selected_slot = self._promotion_slot(slot)
        payload = {
            "slot": selected_slot,
            "alias": clean_alias,
            "purpose": clean_purpose,
            "schema": inferred_schema,
            "source": candidate_source,
        }
        if normalized_tests:
            payload["tests"] = normalized_tests
        if invoke is not None:
            payload["invoke"] = dict(invoke)
        if "propose_activate" in self._control_methods:
            return self._control("propose_activate")(
                kind="create", parent=None, **payload
            )
        return self._control("synthesize")(**payload)

    def promote_helper(
        self,
        helper: Any,
        *,
        tests: Any = None,
        slot: str | None = None,
        alias: str | None = None,
        purpose: str | None = None,
        invoke: dict[str, Any] | None = None,
    ) -> Any:
        """Promote ordinary working Python through the tested atomic create path."""

        return self.synthesize(
            helper,
            tests=tests,
            slot=slot,
            alias=alias,
            purpose=purpose,
            invoke=invoke,
        )

    def inspect(self) -> dict[str, Any]:
        return {
            "catalog_release_id": self._document.get("catalog_release_id"),
            "mount_revision": int(self._document.get("mount_revision") or 0),
            "selected_category_id": self._document.get("selected_category_id"),
            "retained_category_ids": list(
                self._document.get("retained_category_ids") or ()
            ),
            "retained_grant_count": len(
                self._document.get("retained_capability_ref_ids") or ()
            ),
            "mount_card": self._document.get("mount_card"),
            "mutation": dict(self._document.get("mutation") or {}),
        }

    def missing_capability(self, alias: str, *, namespace: str = "tools") -> str:
        """Explain local disclosure state without mounting or invoking anything."""
        name = str(alias).removeprefix("tools.")
        matches = [row for row in self._index if name in {
            str(row.get("alias") or ""), str(row.get("qualified_alias") or ""),
        }]
        current = self._document.get("selected_category_id") or "base"
        prefix = f"{namespace}.{alias} is not available in this namespace (selected category: {current})."
        enabled = [row for row in matches if row.get("enabled", True)]
        categories = sorted({str(row['category_id']) for row in enabled if row.get('category_id')})
        if categories:
            choices = ", ".join(repr(category) for category in categories)
            return (f"{prefix} Catalog category: {choices}. "
                    f"Use toolbelt.describe({categories[0]!r}) for exact names; "
                    f"select it with ipython(category={categories[0]!r}, code=...).")
        if matches:
            return f"{prefix} The catalog entry is disabled; selecting a category will not enable it."
        return f"{prefix} No pinned catalog match. Use toolbelt.search({name!r}) or tools.methods()."

    def describe(self, alias: str) -> dict[str, Any]:
        clean = str(alias or "").replace("-", "_")
        if clean in self.methods():
            return self.documentation(clean)
        category = next((row for row in self._document.get("category_options", ())
                         if row.get("category_id") == clean), None)
        if category is not None:
            return {
                "category_id": clean,
                "selected": self._document.get("selected_category_id") == clean,
                "summary": category.get("summary", ""),
                "select": f"ipython(category={clean!r}, code=...)",
                "capabilities": [{key: row[key] for key in
                    ("alias", "namespace", "call", "signature", "enabled") if key in row}
                    for row in self._index if row.get("category_id") == clean],
            }
        matches = [
            dict(row) for row in self._index
            if clean in {
                str(row.get("alias") or ""),
                str(row.get("qualified_alias") or ""),
            }
        ]
        if len(matches) != 1:
            raise KeyError(self.missing_capability(alias, namespace="toolbelt"))
        return matches[0]

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._document.get("mount_history") or ())[-max(1, min(int(limit), 200)):]

    def top_k(self) -> list[dict[str, Any]]:
        return list(self._document.get("top_k") or ())

    def __repr__(self) -> str:
        state = self.inspect()
        return (
            "<VARIANT-1 toolbelt "
            f"catalog={state['catalog_release_id']} "
            f"category={state['selected_category_id'] or '-'} "
            f"mount={state['mount_revision']} "
            f"methods={','.join(self._control_methods) or '-'}>"
        )


def _install_import_shim(name: str, target: Any) -> None:
    """Make accidental imports resolve to the current protected namespace.

    Model-authored cells sometimes write ``import tools`` even though the
    runtime injects protected globals. This import shim prevents Python from
    resolving an unrelated host module under the same name.
    """

    module = types.ModuleType(name, getattr(target, "__doc__", None))
    module.__dict__["__variant1_preloaded_namespace__"] = True
    for attribute in dir(target):
        if attribute.startswith("_"):
            continue
        try:
            module.__dict__[attribute] = getattr(target, attribute)
        except (AttributeError, RuntimeError):
            continue
    sys.modules[name] = module


def install_document(
    context: Any,
    bridge: KernelBridgeClient,
    document: dict[str, Any],
) -> None:
    descriptors = document.get("capabilities") if isinstance(document, dict) else None
    if not isinstance(descriptors, list):
        raise RuntimeError("kernel capability descriptor document is invalid")
    namespace = context.namespace
    tools_namespace = ReadOnlyTools(descriptors, bridge, kernel_context=context)
    namespace["tools"] = tools_namespace
    _install_import_shim("tools", tools_namespace)
    if str(document.get("schema") or "").startswith("variant1.astb.namespace"):
        services = document.get("services") if isinstance(document.get("services"), dict) else {}
        if services:
            raise RuntimeError("retired service namespace descriptors are not supported")
        raw_python_apis = (
            document.get("python_apis")
            if isinstance(document.get("python_apis"), dict) else {}
        )
        python_apis: dict[str, MountedPythonAPI] = {}
        toolbelt_descriptor = None
        for api_name, api_descriptor in raw_python_apis.items():
            if not isinstance(api_descriptor, dict):
                continue
            clean_name = _identifier(str(api_name))
            if clean_name == "toolbelt":
                toolbelt_descriptor = api_descriptor
                continue
            if clean_name == "tools":
                raise ValueError(f"mounted Python API name conflicts: {clean_name}")
            python_apis[clean_name] = MountedPythonAPI(api_descriptor, bridge)
        raw_mounted_objects = (
            document.get("mounted_objects")
            if isinstance(document.get("mounted_objects"), dict) else {}
        )
        mounted_objects: dict[str, MountedSeedObject] = {}
        for object_name, object_descriptor in raw_mounted_objects.items():
            if not isinstance(object_descriptor, dict):
                continue
            clean_name = _identifier(str(object_name))
            if clean_name == "toolbelt":
                toolbelt_descriptor = object_descriptor
                continue
            if (
                clean_name in python_apis
                or clean_name == "tools"
            ):
                raise ValueError(f"mounted seed object name conflicts: {clean_name}")
            mounted_objects[clean_name] = MountedSeedObject(
                object_descriptor, bridge
            )
        previous_names = set(
            getattr(context, "mounted_namespace_names", ()) or ()
        )
        next_names = set(python_apis) | set(mounted_objects)
        for removed_name in sorted(previous_names - next_names):
            namespace.pop(removed_name, None)
            module = sys.modules.get(removed_name)
            if bool(getattr(module, "__variant1_preloaded_namespace__", False)):
                sys.modules.pop(removed_name, None)
        mounted_namespaces: dict[str, Any] = {
            **python_apis,
            **mounted_objects,
        }
        toolbelt_namespace = ToolbeltNamespace(
            document,
            bridge,
            control_descriptor=toolbelt_descriptor,
        )
        namespace["toolbelt"] = toolbelt_namespace
        _install_import_shim("toolbelt", toolbelt_namespace)
        namespace.update(mounted_namespaces)
        for mounted_name, mounted_namespace in mounted_namespaces.items():
            _install_import_shim(mounted_name, mounted_namespace)
        context.python_api_names = tuple(sorted(python_apis))
        context.mounted_object_names = tuple(sorted(mounted_objects))
        context.mounted_namespace_names = tuple(sorted(next_names))
    capsule_runtime = getattr(context, "capsule_runtime", None)
    if capsule_runtime is not None:
        capsule_runtime.update_document(document)
    context.document = json.loads(json.dumps(document))
    protected_names = {"tools"}
    if str(document.get("schema") or "").startswith("variant1.astb.namespace"):
        protected_names.add("toolbelt")
        protected_names.update(
            getattr(context, "mounted_namespace_names", ()) or ()
        )
    context.protected_globals = {
        name: namespace[name]
        for name in sorted(protected_names)
        if name in namespace
    }


def _refresh_worker_protected_globals(
    context: Any,
    bridge: KernelBridgeClient,
) -> None:
    protected = dict(
        getattr(context, "protected_globals", {}) or {}
    )
    protected.update({
        "Variant1CapabilityError": Variant1CapabilityError,
        "Variant1CapabilityFailure": Variant1CapabilityFailure,
        "_variant1_bridge": bridge,
    })
    context.protected_globals = protected


def repair_worker_namespace(
    context: Any,
    bridge: KernelBridgeClient,
) -> None:
    namespace = context.namespace
    protected = getattr(context, "protected_globals", {}) or {}
    if all(namespace.get(name) is value for name, value in protected.items()):
        return
    document = getattr(context, "document", None)
    if not isinstance(document, dict):
        raise RuntimeError("kernel namespace integrity document is absent")
    install_document(context, bridge, document)
    namespace["Variant1CapabilityError"] = Variant1CapabilityError
    namespace["Variant1CapabilityFailure"] = Variant1CapabilityFailure
    namespace["_variant1_bridge"] = bridge
    _refresh_worker_protected_globals(context, bridge)


def install_worker_namespace(context: Any) -> KernelBridgeClient:
    descriptor_path = os.environ["VARIANT1_KERNEL_DESCRIPTORS"]
    with open(descriptor_path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    from .runtime_profile import validate_profile_document

    try:
        profile_document = json.loads(
            base64.b64decode(
                os.environ["VARIANT1_KERNEL_RUNTIME_PROFILE"],
                validate=True,
            ).decode("utf-8", errors="strict")
        )
    except Exception as exc:
        raise RuntimeError("kernel runtime profile envelope is invalid") from exc
    profile = validate_profile_document(profile_document)
    if document.get("runtime_profile") != profile.to_dict():
        raise RuntimeError("kernel descriptor runtime profile is inconsistent")
    context.runtime_profile = profile.to_dict()
    secret = base64.urlsafe_b64decode(
        os.environ["VARIANT1_KERNEL_BRIDGE_SECRET"].encode("ascii")
    )
    bridge = KernelBridgeClient(
        host=os.environ.get("VARIANT1_KERNEL_BRIDGE_HOST", "127.0.0.1"),
        port=int(os.environ["VARIANT1_KERNEL_BRIDGE_PORT"]),
        secret=secret,
        nonce=os.environ["VARIANT1_KERNEL_NONCE"],
        generation=int(os.environ["VARIANT1_KERNEL_GENERATION"]),
        kernel=context,
        timeout_s=float(os.environ.get("VARIANT1_KERNEL_BRIDGE_TIMEOUT_S", "120")),
        max_frame_bytes=int(
            os.environ.get("VARIANT1_KERNEL_BRIDGE_MAX_BYTES", DEFAULT_MAX_FRAME_BYTES)
        ),
        async_concurrency=int(
            os.environ.get("VARIANT1_KERNEL_BRIDGE_ASYNC_CONCURRENCY", "8")
        ),
    )
    bridge.handshake()
    context.bridge = bridge
    namespace = context.namespace
    install_document(context, bridge, document)
    namespace["Variant1CapabilityError"] = Variant1CapabilityError
    namespace["Variant1CapabilityFailure"] = Variant1CapabilityFailure
    namespace["_variant1_bridge"] = bridge
    _refresh_worker_protected_globals(context, bridge)
    from .capsule_worker import KernelCapsuleWorker

    capsule_runtime = KernelCapsuleWorker(
        context,
        reinstall_namespace=lambda value: install_document(context, bridge, value),
        document=document,
    )
    context.capsule_runtime = capsule_runtime
    context._baseline_names = tuple(sorted(namespace))
    return bridge
