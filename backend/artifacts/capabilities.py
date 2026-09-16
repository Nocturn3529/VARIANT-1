"""Scoped IPython capabilities for versioned artifacts and publishing jobs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from capability_broker import current_capability_invocation
from tools import Tool, ToolError
from work_fabric.handles import job_handle_envelope, remote_handle_envelope
from work_fabric.scope import WorkScope, coerce_work_scope, effective_work_scope


def _context():
    context = current_capability_invocation()
    if context is None:
        raise ToolError("artifact capabilities require an admitted Python cell")
    return context


def _scope() -> WorkScope:
    return effective_work_scope(_context())


def _runtime(host: Any):
    if host is None:
        raise ToolError("Artifact runtime is unavailable")
    require_runtime = getattr(host, "require_runtime", None)
    runtime = getattr(require_runtime(), "artifacts", None) if callable(require_runtime) else None
    if runtime is None:
        raise ToolError("Artifact runtime is unavailable")
    return runtime


def _artifact(host: Any, artifact_id: Any):
    return _runtime(host).require_visible(
        str(artifact_id or ""), scope=_scope(),
    )


ARTIFACT_OPERATIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "read_bytes",
        "description": "Read a chat-owned screenshot or artifact ref as Python bytes. Large files can be saved with artifacts.save(ref, path).",
        "effect_class": "read",
        "params": {"ref": {"type": "string", "required": True},
                   "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16_777_216}},
    },
    {
        "name": "save",
        "description": "Save a chat-owned screenshot/download/artifact ref to a local path; returns verified byte count and SHA-256. Existing paths require overwrite=True or a different destination. No CAS path knowledge needed.",
        "effect_class": "write",
        "params": {"ref": {"type": "string", "required": True},
                   "path": {"type": "string", "required": True},
                   "overwrite": {"type": "boolean", "required": False}},
    },
    {
        "name": "list",
        "description": "List content-addressed artifacts granted to this durable chat.",
        "effect_class": "read",
        "params": {
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 200},
        },
    },
    {
        "name": "read_text",
        "description": "Read one bounded UTF-8 projection of a chat-scoped CAS artifact.",
        "effect_class": "read",
        "params": {
            "ref": {"type": "string", "required": True},
            "max_chars": {"type": "integer", "required": False, "minimum": 1, "maximum": 100_000},
        },
    },
    {
        "name": "objects",
        "description": "List versioned artifacts visible to this WorkScope.",
        "effect_class": "read",
        "params": {
            "kind": {"type": "string", "required": False},
            "include_tombstoned": {"type": "boolean", "required": False},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 200},
        },
    },
    {
        "name": "get",
        "description": "Inspect one immutable artifact revision and its evidence.",
        "effect_class": "read",
        "params": {
            "artifact_id": {"type": "string", "required": True},
            "revision": {"type": "integer", "required": False, "minimum": 1},
        },
    },
    {
        "name": "history",
        "description": "List immutable source revisions for an artifact.",
        "effect_class": "read",
        "params": {
            "artifact_id": {"type": "string", "required": True},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 200},
        },
    },
    {
        "name": "create",
        "description": "Create an immutable source-spec artifact in the current scope.",
        "effect_class": "write",
        "params": {
            "title": {"type": "string", "required": True},
            "kind": {"type": "string", "required": False},
            "specification": {"type": "object", "required": True},
            "metadata": {"type": "object", "required": False},
            "alias": {"type": "string", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
    {
        "name": "revise",
        "description": "Append one CAS-fenced immutable source revision.",
        "effect_class": "write",
        "params": {
            "artifact_id": {"type": "string", "required": True},
            "expected_version": {"type": "integer", "required": True, "minimum": 1},
            "specification": {"type": "object", "required": True},
            "metadata": {"type": "object", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
    {
        "name": "publish",
        "description": "Durably render, validate, and optionally alias artifact formats.",
        "effect_class": "write",
        "params": {
            "artifact_id": {"type": "string", "required": True},
            "revision": {"type": "integer", "required": False, "minimum": 1},
            "formats": {"type": "array", "required": True},
            "validate": {"type": "boolean", "required": False},
            "require_valid": {"type": "boolean", "required": False},
            "alias": {"type": "string", "required": False},
            "expected_alias_version": {"type": "integer", "required": False, "minimum": 0},
            "priority": {"type": "integer", "required": False},
            "idempotency_key": {"type": "string", "required": False},
        },
    },
    {
        "name": "set_alias",
        "description": "CAS-fence a scope-local alias to one exact revision.",
        "effect_class": "write",
        "params": {
            "artifact_id": {"type": "string", "required": True},
            "alias": {"type": "string", "required": True},
            "revision": {"type": "integer", "required": False, "minimum": 1},
            "expected_version": {"type": "integer", "required": True, "minimum": 1},
            "expected_alias_version": {"type": "integer", "required": False, "minimum": 0},
        },
    },
    {
        "name": "export",
        "description": "Export one verified rendered artifact to an explicit local path.",
        "effect_class": "write",
        "params": {
            "artifact_id": {"type": "string", "required": True},
            "destination": {"type": "string", "required": True},
            "format": {"type": "string", "required": False},
            "revision": {"type": "integer", "required": False, "minimum": 1},
            "overwrite": {"type": "boolean", "required": False},
        },
    },
)

_ARTIFACT_OPERATION_BY_NAME = {
    str(item["name"]): item for item in ARTIFACT_OPERATIONS
}

# The mounted object is a small factory/index. Versioned continuation belongs
# to the bound artifact handle returned by list/get/create.
ARTIFACT_OBJECT_METHODS: tuple[dict[str, Any], ...] = (
    {
        **_ARTIFACT_OPERATION_BY_NAME["objects"],
        "name": "list",
        "description": "List versioned artifacts as bound handles in this WorkScope.",
    },
    _ARTIFACT_OPERATION_BY_NAME["read_text"],
    _ARTIFACT_OPERATION_BY_NAME["read_bytes"],
    _ARTIFACT_OPERATION_BY_NAME["save"],
    {
        **_ARTIFACT_OPERATION_BY_NAME["get"],
        "description": "Acquire a bound handle for one versioned artifact.",
        "params": {
            "artifact_id": {"type": "string", "required": True},
        },
    },
    {
        **_ARTIFACT_OPERATION_BY_NAME["create"],
        "description": "Create a versioned artifact and return its bound handle.",
    },
)


def _handle_method(operation: str, *, name: str | None = None) -> dict[str, Any]:
    source = _ARTIFACT_OPERATION_BY_NAME[operation]
    params = []
    for param_name, raw in dict(source.get("params") or {}).items():
        if param_name in {"artifact_id", "expected_version"}:
            continue
        item = {
            "name": str(param_name),
            "type": str(raw.get("type") or "any"),
            "required": bool(raw.get("required")),
        }
        if not item["required"] and "default" in raw:
            item["default"] = raw.get("default")
        params.append(item)
    return {
        "name": str(name or operation),
        "description": str(source.get("description") or ""),
        "params": params,
        "returns": "artifact" if operation == "revise" else "any",
    }


ARTIFACT_HANDLE_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "refresh",
        "description": "Reconnect this artifact to its current durable version.",
        "params": [],
        "returns": "artifact",
    },
    {
        "name": "inspect",
        "description": "Inspect this artifact's current or selected immutable revision.",
        "params": [{
            "name": "revision", "type": "integer", "required": False,
        }],
        "returns": "dict",
    },
    _handle_method("history"),
    _handle_method("revise"),
    _handle_method("publish"),
    _handle_method("set_alias"),
    _handle_method("export"),
)


def _seed_params() -> dict[str, dict[str, Any]]:
    params: dict[str, dict[str, Any]] = {
        "operation": {
            "type": "string",
            "required": True,
            "enum": [str(row["name"]) for row in ARTIFACT_OBJECT_METHODS],
        }
    }
    for method in ARTIFACT_OBJECT_METHODS:
        for name, raw_spec in dict(method.get("params") or {}).items():
            spec = dict(raw_spec)
            spec["required"] = False
            current = params.get(str(name))
            if current is not None and current != spec:
                raise RuntimeError(f"conflicting artifacts parameter schema: {name}")
            params[str(name)] = spec
    return params


def _arguments_for(
    methods: tuple[dict[str, Any], ...],
    operation: str,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    method = next(
        (row for row in methods if row["name"] == operation),
        None,
    )
    if method is None:
        raise ToolError(f"unknown artifacts operation: {operation!r}")
    specs = dict(method.get("params") or {})
    payload = {key: value for key, value in args.items() if key != "operation"}
    unknown = sorted(set(payload) - set(specs))
    if unknown:
        raise ToolError(
            f"artifacts.{operation}: unknown argument(s): {', '.join(unknown)}"
        )
    for name, spec in specs.items():
        if not bool(spec.get("required")):
            continue
        if name not in payload or payload[name] in (None, "", [], {}):
            raise ToolError(f"artifacts.{operation} needs '{name}'")
    return payload


def _method_arguments(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    return _arguments_for(ARTIFACT_OBJECT_METHODS, operation, args)


def _artifact_handle(host: Any, context: Any, record: Any) -> dict[str, Any]:
    return remote_handle_envelope(
        service="artifacts",
        kind="artifact",
        handle_id=str(record.artifact_id),
        generation=1,
        revision=int(record.version),
        metadata={
            "title": str(record.title),
            "kind": str(record.kind),
            "current_revision": int(record.current_revision),
            "tombstoned": bool(record.tombstoned_at),
        },
        methods=ARTIFACT_HANDLE_METHODS,
        broker=host.require_runtime().broker,
        context=context,
    )


def _export_receipt_view(host: Any, context: Any, result: dict[str, Any]) -> dict[str, Any]:
    from .blob_handles import blob_handle_envelope
    if result.get("receipt_ref"):
        try:
            result = {**result, "receipt": blob_handle_envelope(host, context, result["receipt_ref"])}
        except Exception as exc:
            result = {**result, "receipt_error": f"{type(exc).__name__}: {exc}"[:500]}
    return result


async def _artifact_handle_router(
    host: Any,
    context: Any,
    identity: Mapping[str, Any],
    method: str,
    arguments: dict[str, Any],
) -> Any:
    if str(identity.get("kind") or "") == "blob":
        if int(identity.get("generation") or -1) != 1 or int(identity.get("revision") or -1) != 1:
            raise ToolError("artifact blob handle generation or revision is unsupported")
        blobs = _runtime(host).blobs
        scope = effective_work_scope(context)
        ref = str(identity.get("id") or "")
        # Validate through the same scoped service as artifacts.save/read_bytes.
        blobs.stat(ref, scope=scope)
        if method not in {"save", "read_bytes"}:
            raise ToolError(f"unsupported artifacts.blob method: {method}")
        payload = _arguments_for(ARTIFACT_OPERATIONS, method, {
            "operation": method, **arguments, "ref": ref,
        })
        if method == "save":
            return _export_receipt_view(host, context, blobs.save(
                **payload, scope=scope, cancellation=context.cancellation))
        return blobs.read_bytes(**payload, scope=scope)
    if (
        str(identity.get("kind") or "") != "artifact"
        or int(identity.get("generation") or -1) != 1
    ):
        raise ToolError("artifact handle kind or generation is unsupported")
    runtime = _runtime(host)
    record = runtime.require_visible(
        str(identity.get("id") or ""),
        scope=coerce_work_scope(context.work_scope),
    )
    if method == "refresh":
        if arguments:
            raise ToolError("artifacts.artifact.refresh takes no arguments")
        return _artifact_handle(host, context, record)
    supplied_version = int(identity.get("revision") or -1)
    if supplied_version != int(record.version):
        raise ToolError(
            f"stale artifacts.artifact handle: expected version {record.version}, "
            f"received {supplied_version}; call refresh()"
        )
    if method == "inspect":
        unknown = sorted(set(arguments) - {"revision"})
        if unknown:
            raise ToolError(
                "artifacts.artifact.inspect: unknown argument(s): "
                + ", ".join(unknown)
            )
        revision = int(arguments.get("revision") or 0) or None
        return runtime.snapshot(record.artifact_id, revision).to_dict()
    if method not in {
        "history", "revise", "publish", "set_alias", "export",
    }:
        raise ToolError(f"unsupported artifacts.artifact method: {method}")
    bound_arguments = {
        "operation": method,
        "artifact_id": record.artifact_id,
        **dict(arguments),
    }
    if "expected_version" in dict(
        _ARTIFACT_OPERATION_BY_NAME[method].get("params") or {}
    ):
        bound_arguments["expected_version"] = int(record.version)
    validated = _arguments_for(
        ARTIFACT_OPERATIONS, method, bound_arguments,
    )
    arguments = {
        key: value
        for key, value in validated.items()
        if key not in {"artifact_id", "expected_version"}
    }
    if method == "history":
        return runtime.history(
            record.artifact_id,
            limit=max(1, min(int(arguments.get("limit") or 50), 200)),
        )
    if method == "revise":
        specification = arguments.get("specification")
        if not isinstance(specification, Mapping):
            raise ToolError("specification must be an object")
        result = runtime.revise(
            record.artifact_id,
            dict(specification),
            expected_version=int(record.version),
            metadata=dict(arguments.get("metadata") or {}),
            producer={"source": "ipython", "run_id": context.run_id},
            correlation_id=str(
                context.nested_call_id or context.outer_tool_call_id or ""
            ),
            idempotency_key=str(
                arguments.get("idempotency_key")
                or context.idempotency_key
                or ""
            ),
        )
        return _artifact_handle(host, context, result.artifact)
    if method == "publish":
        formats = arguments.get("formats") or []
        job = runtime.publish(
            record.artifact_id,
            tuple(str(item) for item in formats),
            revision=int(arguments.get("revision") or 0) or None,
            validate=bool(arguments.get("validate", True)),
            require_valid=bool(arguments.get("require_valid", True)),
            alias=str(arguments.get("alias") or ""),
            expected_alias_version=int(
                arguments.get("expected_alias_version") or 0
            ),
            idempotency_key=str(
                arguments.get("idempotency_key")
                or context.idempotency_key
                or context.outer_tool_call_id
                or ""
            ),
            priority=int(arguments.get("priority") or 0),
        )
        return job_handle_envelope(
            job, broker=host.require_runtime().broker, context=context
        )
    if method == "set_alias":
        return runtime.set_alias(
            record.artifact_id,
            str(arguments.get("alias") or ""),
            revision=int(arguments.get("revision") or 0) or None,
            expected_version=int(record.version),
            expected_alias_version=int(
                arguments.get("expected_alias_version") or 0
            ),
        )
    if method == "export":
        return runtime.export(
            record.artifact_id,
            str(arguments.get("destination") or ""),
            format=str(arguments.get("format") or ""),
            revision=int(arguments.get("revision") or 0) or None,
            overwrite=bool(arguments.get("overwrite")),
        )
    raise ToolError(f"unsupported artifacts.artifact method: {method}")


def register_artifact_tools(
    host: Any = None,
    *,
    registry: Any = None,
) -> None:
    """Install the one slot-owned ``artifacts`` seed and no method handlers."""

    registry = registry or host.require_runtime().registry
    if registry is None:
        raise RuntimeError("artifacts seed requires a registry")

    def blobs():
        return _runtime(host).blobs
    if registry.get("artifacts") is not None:
        return

    async def artifacts(args: dict[str, Any]):
        operation = str(args.get("operation") or "")
        payload = _method_arguments(operation, args)
        if operation == "list":
            context = _context()
            rows = _runtime(host).list(
                scope=_scope(),
                kind=str(payload.get("kind") or ""),
                include_tombstoned=bool(payload.get("include_tombstoned")),
                limit=max(1, min(int(payload.get("limit") or 50), 200)),
            )
            return [
                _artifact_handle(host, context, row.artifact) for row in rows
            ]
        if operation == "read_text":
            return blobs().read_text(
                str(payload.get("ref") or ""),
                scope=_scope(),
                max_chars=int(payload.get("max_chars") or 20_000),
            )
        if operation == "read_bytes":
            return blobs().read_bytes(str(payload['ref']), scope=_scope(),
                                      max_bytes=int(payload.get('max_bytes', 4_194_304)))
        if operation == "save":
            return _export_receipt_view(host, _context(), blobs().save(str(payload['ref']), str(payload['path']), scope=_scope(),
                                overwrite=bool(payload.get('overwrite', False)),
                                cancellation=_context().cancellation))
        if operation == "get":
            context = _context()
            record = _artifact(host, payload.get("artifact_id"))
            return _artifact_handle(host, context, record)
        if operation == "create":
            specification = payload.get("specification")
            if not isinstance(specification, Mapping):
                raise ToolError("specification must be an object")
            context = _context()
            result = _runtime(host).create(
                title=str(payload.get("title") or ""),
                kind=str(payload.get("kind") or "document"),
                specification=dict(specification),
                scope=_scope(),
                metadata=dict(payload.get("metadata") or {}),
                producer={"source": "ipython", "run_id": context.run_id},
                alias=str(payload.get("alias") or ""),
                correlation_id=str(
                    context.nested_call_id or context.outer_tool_call_id or ""
                ),
                idempotency_key=str(
                    payload.get("idempotency_key") or context.idempotency_key or ""
                ),
            )
            return _artifact_handle(host, context, result.artifact)
        raise ToolError(f"unsupported artifacts root operation: {operation}")

    registry.register(Tool(
        "artifacts",
        "Read/save scoped artifact bytes or text and acquire bound versioned-artifact handles.",
        artifacts,
        category="artifact_infrastructure",
        hidden=True,
        visibility="broker_only",
        effect_class="write",
        parallel_safe=False,
        idempotency="caller_key",
        may_return_secrets=True,
        params=_seed_params(),
        schema_revision="variant1.artifacts-seed.v3",
        handler_revision="variant1.artifacts-seed-handler.v3",
        object_methods=ARTIFACT_OBJECT_METHODS,
    ))

    if host is not None:
        routers = getattr(host, "remote_handle_routers", None)
        if routers is None:
            routers = {}
            setattr(host, "remote_handle_routers", routers)
        if not isinstance(routers, dict):
            raise TypeError("host.remote_handle_routers must be a dictionary")
        routers["artifacts"] = (
            lambda context, identity, method, arguments:
            _artifact_handle_router(
                host, context, identity, method, dict(arguments or {})
            )
        )


__all__ = [
    "ARTIFACT_HANDLE_METHODS",
    "ARTIFACT_OBJECT_METHODS",
    "ARTIFACT_OPERATIONS",
    "register_artifact_tools",
]
