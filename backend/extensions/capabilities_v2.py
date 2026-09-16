"""IPython capabilities and Work jobs for executable extensions and MCP v2."""

from __future__ import annotations

import asyncio
import base64
import copy
from collections.abc import Mapping
import hashlib
import json
from typing import Any

from capability_broker import current_capability_invocation
from object_api import dispatch_object, register_object_tool
from tools import ToolError
from work_fabric.handles import remote_handle_envelope
from work_fabric.jobs import JobExecutionContext, JobResult
from work_fabric.scope import WorkScope, effective_work_scope
from tool_core import ToolProjectionResult

from .mcp_v2 import McpLease, StaleMcpLease
from .packages_v2 import ExtensionPackageError


EXTENSION_PACKAGE_JOB = "extension.package.v2"


def _trace(event: str, **fields: Any) -> None:
    try:
        from observability.trace_events import record_trace_event

        record_trace_event(event, **fields)
    except Exception:
        pass


def _context():
    context = current_capability_invocation()
    if context is None:
        raise ToolError("extension capabilities require an admitted Python cell")
    return context


def _scope() -> WorkScope:
    return effective_work_scope(_context())


def _runtime(host: Any):
    runtime = getattr(host.require_runtime(), "extensions", None)
    if runtime is None:
        raise ToolError("Extension runtime v2 is unavailable")
    return runtime


def _work(host: Any):
    runtime = getattr(host.require_runtime(), "work", None)
    if runtime is None:
        raise ToolError("Work Fabric is unavailable")
    return runtime


def _lease(value: Any) -> McpLease:
    if not isinstance(value, Mapping):
        raise ToolError("lease must be an object returned by mcp.search/describe")
    try:
        return McpLease(
            server_id=str(value.get("server_id") or ""),
            generation=int(value.get("generation") or 0),
            capability_revision=int(value.get("capability_revision") or 0),
            kind=str(value.get("kind") or ""),
            name=str(value.get("name") or ""),
            schema_digest=str(value.get("schema_digest") or ""),
        )
    except Exception as exc:
        raise ToolError("MCP lease is malformed") from exc


CONNECTOR_OBJECT_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "search",
        "description": "Search live MCP capabilities and callable immutable plugin contributions.",
        "effect_class": "read",
        "params": {
            "query": {"type": "string", "required": False},
            "kind": {"type": "string", "required": False},
            "server_id": {"type": "string", "required": False},
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 500},
        },
    },
)

MCP_CONNECTOR_HANDLE_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "schema",
        "description": (
            "Inspect this exact live MCP capability lease when its search result "
            "did not already include the schema, or after it becomes stale."
        ),
        "params": [],
        "returns": "dict",
    },
    {
        "name": "invoke",
        "description": (
            "Invoke this exact leased MCP capability using the schema included "
            "by connector search or returned by schema(). Set conclude=True only "
            "for the final authoritative effect."
        ),
        "params": [
            {"name": "arguments", "type": "object", "required": False},
            {
                "name": "operation", "type": "string", "required": False,
                "default": "call",
            },
            {"name": "request_id", "type": "string", "required": False},
            {
                "name": "raise_on_error", "type": "bool", "required": False,
                "default": True,
            },
            {
                "name": "conclude", "type": "bool", "required": False,
                "default": False,
                "description": (
                    "Set true only when this successful invocation is the final "
                    "authoritative effect requested by the user."
                ),
            },
        ],
        "returns": "any",
    },
    {
        "name": "cancel",
        "control": True,
        "description": "Cancel this cell's in-flight request on this connector lease using its request_id. Returns false if already settled.",
        "params": [
            {"name": "request_id", "type": "string", "required": True},
        ],
        "returns": "bool",
    },
)


_MCP_DEFAULT_OPERATIONS = {
    "tool": "call", "resource": "read_resource",
    "resource_template": "read_resource", "prompt": "get_prompt",
}


def mcp_connector_methods(kind: str) -> list[dict[str, Any]]:
    """Disclose the same lease-specific default used by canonical dispatch."""
    methods = copy.deepcopy(list(MCP_CONNECTOR_HANDLE_METHODS))
    default = _MCP_DEFAULT_OPERATIONS.get(kind, "call")
    invoke = next(method for method in methods if method["name"] == "invoke")
    invoke["description"] += (
        f" Omit operation to use {default!r} for this {kind} lease. "
        "Operation spellings are call, read_resource, get_prompt, subscribe, unsubscribe."
    )
    next(param for param in invoke["params"] if param["name"] == "operation")["default"] = default
    return methods

PLUGIN_CONNECTOR_HANDLE_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "inspect",
        "description": "Inspect this immutable plugin package and contributions.",
        "params": [],
        "returns": "dict",
    },
    {
        "name": "invoke",
        "description": (
            "Invoke one contribution from this immutable package. Set "
            "conclude=True only for the final authoritative effect."
        ),
        "params": [
            {
                "name": "contribution_id", "type": "string", "required": True,
            },
            {"name": "arguments", "type": "object", "required": False},
            {"name": "request_id", "type": "string", "required": False},
            {"name": "idempotency_key", "type": "string", "required": False},
            {"name": "deadline_ms", "type": "integer", "required": False},
            {
                "name": "conclude", "type": "bool", "required": False,
                "default": False,
            },
        ],
        "returns": "any",
    },
    {
        "name": "read_resource",
        "description": "Read one exact immutable resource from this package.",
        "params": [
            {
                "name": "contribution_id", "type": "string", "required": True,
            },
            {"name": "resource", "type": "string", "required": True},
        ],
        "returns": "dict",
    },
)


def _connector_identity(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_connector_identity(value: Any) -> dict[str, Any]:
    text = str(value or "")
    if not text or len(text) > 16_384:
        raise ToolError("connector handle identity is invalid")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ToolError("connector handle identity is invalid") from exc
    if not isinstance(payload, dict):
        raise ToolError("connector handle identity is invalid")
    return payload


def _mcp_input_schema(host: Any, lease: McpLease) -> Mapping[str, Any] | None:
    """Return one locally cached MCP input schema without invoking the server."""

    described = _runtime(host).mcp.describe(lease)
    descriptor = described.get("descriptor")
    if not isinstance(descriptor, Mapping):
        return None
    schema = descriptor.get("inputSchema", descriptor.get("input_schema"))
    return schema if isinstance(schema, Mapping) else None


def _validate_mcp_tool_arguments(
    host: Any,
    lease: McpLease,
    arguments: Mapping[str, Any],
) -> None:
    """Fail locally on clear schema-name errors before an MCP side effect."""

    schema = _mcp_input_schema(host, lease)
    if schema is None:
        return
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return
    allowed = {str(name) for name in properties if str(name)}
    supplied = {str(name) for name in arguments}
    required_raw = schema.get("required")
    required = (
        {str(name) for name in required_raw if str(name)}
        if isinstance(required_raw, (list, tuple))
        else set()
    )
    missing = sorted(required - supplied)
    additional = schema.get("additionalProperties")
    allows_additional = additional is True or isinstance(additional, Mapping)
    unknown = sorted(supplied - allowed) if allowed and not allows_additional else []
    if not missing and not unknown:
        return
    problems = []
    if unknown:
        problems.append("unknown argument(s): " + ", ".join(unknown))
    if missing:
        problems.append("missing required argument(s): " + ", ".join(missing))
    allowed_text = ", ".join(sorted(allowed)) or "no named arguments"
    compact_contract: dict[str, Any] = {}
    for name in sorted(allowed):
        raw_spec = properties.get(name)
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        item: dict[str, Any] = {
            "type": str(spec.get("type") or "any"),
            "required": name in required,
        }
        if isinstance(spec.get("enum"), list):
            item["enum"] = list(spec.get("enum") or ())
        if "default" in spec and name not in required:
            item["default"] = spec.get("default")
        compact_contract[name] = item
    contract_text = json.dumps(
        compact_contract,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )[:2_000]
    raise ToolError(
        f"MCP tool {lease.name!r} arguments do not match its input schema "
        f"({'; '.join(problems)}). Allowed names: {allowed_text}. "
        f"Exact input contract: {contract_text}."
    )


def _mcp_error_text(result: Mapping[str, Any]) -> str:
    blocks = result.get("content")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, Mapping) and str(block.get("text") or "").strip():
                return str(block.get("text") or "").strip()
    return "The MCP capability reported an error."


def _mcp_result_data(result: Mapping[str, Any]) -> Any:
    structured = result.get("structured_content")
    if isinstance(structured, Mapping) and set(structured) == {"result"}:
        structured = structured.get("result")
    if structured is not None:
        if isinstance(structured, str):
            try:
                return json.loads(structured)
            except (TypeError, ValueError, json.JSONDecodeError):
                return structured
        return structured
    blocks = result.get("content")
    texts = [
        str(block.get("text") or "")
        for block in blocks
        if isinstance(block, Mapping) and block.get("text") is not None
    ] if isinstance(blocks, list) else []
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return texts[0]
    return texts or None


def _terminal_mcp_observation(result: Mapping[str, Any]) -> str:
    """Bounded user/model confirmation while the full result stays in Python."""

    projection = json.dumps(
        {
            "ok": not bool(result.get("is_error")),
            "result": _mcp_result_data(result),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    if len(projection) <= 4_000:
        return projection
    return json.dumps(
        {
            "ok": not bool(result.get("is_error")),
            "result": "MCP invocation completed; full result remains in Python and its receipt.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _terminal_plugin_observation(result: Any) -> str:
    projection = json.dumps(
        {"ok": True, "result": result},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    if len(projection) <= 4_000:
        return projection
    return '{"ok":true,"result":"Plugin invocation completed; full result remains in Python and its receipt."}'

def register_extension_job_handlers(host: Any) -> None:
    def package_job(execution: JobExecutionContext) -> JobResult:
        request = dict(execution.job.input_manifest or {})
        action = str(request.get("action") or "")
        packages = _runtime(host).packages
        execution.progress({"phase": action, "message": f"Extension {action} in progress"})
        try:
            if action == "install":
                result = packages.install(
                    str(request.get("source") or ""),
                    activate=bool(request.get("activate", True)),
                    cancellation_requested=execution.cancellation_requested,
                )
            elif action == "update":
                result = packages.update(
                    str(request.get("source") or ""),
                    cancellation_requested=execution.cancellation_requested,
                )
            elif action == "rollback":
                result = packages.rollback(
                    str(request.get("package_id") or ""),
                    version=str(request.get("version") or ""),
                    cancellation_requested=execution.cancellation_requested,
                )
            elif action == "promote_dev_mount":
                result = packages.promote_dev_mount(
                    str(request.get("chat_id") or execution.job.scope.chat_id or ""),
                    str(request.get("package_id") or ""),
                    cancellation_requested=execution.cancellation_requested,
                )
            else:
                raise ValueError(f"unsupported extension package action: {action}")
        except ExtensionPackageError:
            if execution.cancellation_requested():
                raise asyncio.CancelledError from None
            raise
        artifact = host.require_runtime().session_artifacts.put_json(
            {"schema": "variant1.extension-package-result.v2", "action": action,
             "result": result},
            kind="extension_package_result",
            scope=str(execution.job.scope.chat_id or f"extension:{execution.job.job_id}"),
        )
        return JobResult(
            result_ref=artifact.ref,
            progress={"phase": "complete", "current": 1, "total": 1,
                      "message": f"Extension {action} completed"},
        )

    _work(host).register_job_handler(EXTENSION_PACKAGE_JOB, package_job)


def register_extension_v2_tools(host: Any) -> None:
    async def plugins_search(args):
        return _runtime(host).packages.search(
            str(args.get("query") or ""),
            # PluginWorkerHost can invoke only capability contributions. Skills
            # have their own progressive-disclosure object and must not reappear
            # here as unusable connector handles.
            kind="capabilities",
            limit=max(1, min(int(args.get("limit") or 100), 500)),
        )

    async def plugins_inspect(args):
        package_id = str(args.get("package_id") or "")
        result = _runtime(host).packages.inspect(
            package_id,
            version=str(args.get("version") or ""),
            digest=str(args.get("digest") or ""),
        )
        result["contributions"] = _runtime(host).packages.list_contributions(
            package_id, chat_id=str(_scope().chat_id or ""),
        )
        return result

    async def plugins_read_resource(args):
        return _runtime(host).packages.read_resource(
            str(args.get("package_id") or ""),
            str(args.get("contribution_id") or ""),
            str(args.get("resource") or ""),
            chat_id=str(_scope().chat_id or ""),
        )

    async def plugins_invoke(args):
        invocation = _context()
        scope = _scope()
        request_id = str(
            args.get("request_id") or invocation.nested_call_id
            or invocation.outer_tool_call_id or ""
        )
        plugin_context = {
            "schema": "variant1.plugin-context.v1",
            "chat_id": str(scope.chat_id or invocation.chat_id or ""),
            "run_id": str(invocation.run_id or ""),
            "outer_tool_call_id": str(invocation.outer_tool_call_id or ""),
            "cell_execution_id": str(invocation.cell_execution_id or ""),
            "nested_call_id": str(invocation.nested_call_id or ""),
            "principal_actor_id": str(invocation.principal_actor_id or ""),
            "workspace_root_ids": list(invocation.workspace_root_ids),
            "kernel_generation": str(invocation.kernel_generation or ""),
            "catalog_release_id": str(invocation.catalog_release_id or ""),
            "work_scope": scope.to_dict(),
        }
        deadline_ms = int(args.get("deadline_ms") or invocation.deadline_ms or 30_000)
        return await _runtime(host).workers.invoke(
            str(args.get("package_id") or ""),
            str(args.get("contribution_id") or ""),
            dict(args.get("arguments") or {}),
            context=plugin_context,
            chat_id=str(scope.chat_id or ""),
            idempotency_key=str(
                args.get("idempotency_key") or invocation.idempotency_key
                or invocation.outer_tool_call_id or ""
            ),
            request_id=request_id,
            deadline_s=max(100, min(deadline_ms, 300_000)) / 1000.0,
        )

    async def mcp_v2_search(args):
        return _runtime(host).mcp.search(
            str(args.get("query") or ""),
            kind=str(args.get("kind") or ""),
            server_id=str(args.get("server_id") or ""),
        )[:max(1, min(int(args.get("limit") or 100), 500))]

    async def mcp_v2_describe(args):
        return _runtime(host).mcp.describe(_lease(args.get("lease")))

    async def mcp_v2_invoke(args):
        context = _context()
        lease = _lease(args.get("lease"))
        operation = str(args.get("kind") or args.get("operation") or "call")
        service = _runtime(host).mcp
        if operation == "call":
            result = await service.call_tool(
                lease, dict(args.get("arguments") or {}),
                request_id=str(args.get("request_id") or context.nested_call_id or context.outer_tool_call_id or ""),
            )
        elif operation == "read_resource":
            result = await service.read_resource(lease)
        elif operation == "get_prompt":
            result = await service.get_prompt(lease, dict(args.get("arguments") or {}))
        elif operation == "subscribe":
            return await service.subscribe(lease)
        elif operation == "unsubscribe":
            return await service.unsubscribe(lease)
        else:
            raise ToolError("operation must be call, read_resource, get_prompt, subscribe, or unsubscribe")
        return result.to_dict()

    def mcp_handle(
        row: Mapping[str, Any],
        *,
        schema_included: bool = False,
    ) -> dict[str, Any]:
        context = _context()
        lease = _lease(row.get("lease"))
        descriptor = row.get("descriptor")
        description = (
            str(descriptor.get("description") or "")
            if isinstance(descriptor, Mapping) else ""
        )
        return remote_handle_envelope(
            service="connectors",
            kind="mcp",
            handle_id=_connector_identity(lease.to_dict()),
            generation=int(lease.generation),
            revision=int(lease.capability_revision),
            metadata={
                "server_id": lease.server_id,
                "capability_kind": lease.kind,
                "name": lease.name,
                "description": description[:2_000],
                "schema_included": bool(schema_included),
                "usage_hint": (
                    "Use the exact schema included beside this handle."
                    if schema_included else
                    "Call this handle's schema() once before its first invoke(), "
                    "then use only the schema's exact argument names."
                ),
            },
            methods=mcp_connector_methods(lease.kind),
            broker=host.require_runtime().broker,
            context=context,
        )

    def mcp_schema(row: Mapping[str, Any]) -> dict[str, Any]:
        lease = _lease(row.get("lease"))
        descriptor = (
            dict(row.get("descriptor"))
            if isinstance(row.get("descriptor"), Mapping)
            else {}
        )
        input_schema = descriptor.get(
            "inputSchema", descriptor.get("input_schema")
        )
        compact_descriptor = {
            "name": lease.name,
            "description": str(descriptor.get("description") or "")[:2_000],
        }
        if isinstance(input_schema, Mapping):
            compact_descriptor["inputSchema"] = dict(input_schema)
        return {
            "kind": lease.kind,
            "name": lease.name,
            "server_id": lease.server_id,
            "descriptor": compact_descriptor,
            "lease": lease.to_dict(),
        }

    def plugin_handle(row: Mapping[str, Any]) -> dict[str, Any]:
        context = _context()
        package_id = str(row.get("package_id") or "")
        digest = str(row.get("package_digest") or row.get("active_digest") or "")
        if not package_id or not digest:
            raise ToolError("plugin search result has no immutable package identity")
        revision = int(hashlib.sha256(digest.encode("utf-8")).hexdigest()[:8], 16)
        return remote_handle_envelope(
            service="connectors",
            kind="plugin",
            handle_id=_connector_identity({
                "package_id": package_id,
                "package_digest": digest,
            }),
            generation=1,
            revision=revision,
            metadata={
                "package_id": package_id,
                "name": str(row.get("name") or package_id),
                "version": str(row.get("version") or ""),
                "contribution_count": int(row.get("contribution_count") or 0),
            },
            methods=PLUGIN_CONNECTOR_HANDLE_METHODS,
            broker=host.require_runtime().broker,
            context=context,
        )

    async def connectors_search(args: dict[str, Any]):
        limit = max(1, min(int(args.get("limit") or 100), 500))
        mcp_rows = list(await mcp_v2_search({**args, "limit": limit}))
        # The primary match and its exact schema are one disclosure event.  The
        # model can use the familiar result['mcp'][0] path without a redundant
        # schema round trip; secondary candidates remain summary-only.
        mcp_handles = [
            mcp_handle(row, schema_included=(index == 0))
            for index, row in enumerate(mcp_rows)
        ]
        top_match = (
            {
                "handle": mcp_handles[0],
                "schema": mcp_schema(mcp_rows[0]),
            }
            if mcp_rows else None
        )
        return {
            "mcp": mcp_handles,
            "plugins": [
                plugin_handle(row)
                for row in await plugins_search({**args, "limit": limit})
            ],
            "top_match": top_match,
            "guidance": (
                "The highest-confidence MCP match includes its exact compact "
                "schema and is ready to invoke. Reuse that bound handle; inspect secondary handles "
                "with handle.schema() before their first invoke."
            ),
        }

    async def connectors(args: dict[str, Any]):
        return await dispatch_object(
            CONNECTOR_OBJECT_METHODS, args, api_name="connectors", handlers={
                "search": connectors_search,
            },
        )

    register_object_tool(
        host.require_runtime().registry,
        name="connectors",
        description=(
            "Search MCP/API/plugin capabilities and return bound callable handles."
        ),
        methods=CONNECTOR_OBJECT_METHODS,
        handler=connectors,
        category="extension_infrastructure",
        schema_revision="variant1.connectors.v2",
        handler_revision="variant1.connectors-handler.v4",
        default_deadline_ms=300_000,
    )

    async def refreshed_mcp_replacement(lease: McpLease):
        try:
            service = _runtime(host).mcp
            await service.refresh(lease.server_id)
            matches = [
                row for row in service.search(
                    lease.name,
                    kind=lease.kind,
                    server_id=lease.server_id,
                )
                if str(row.get("name") or "") == lease.name
            ]
            if len(matches) == 1:
                replacement = {
                    "handle": mcp_handle(matches[0], schema_included=True),
                    "schema": mcp_schema(matches[0]),
                }
                _trace(
                    "connector:stale_reacquired",
                    status="recovered",
                    server_id=lease.server_id,
                    capability_kind=lease.kind,
                    capability_name=lease.name,
                )
                return replacement
        except Exception:
            return None
        return None

    def stale_mcp_message(replacement: Any) -> str:
        if replacement is None:
            return "stale MCP connector handle; search again"
        return json.dumps(
            {
                "code": "stale_mcp_handle",
                "message": "Use the replacement handle and schema.",
                "replacement": replacement,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    async def connector_router(
        context: Any,
        identity: Mapping[str, Any],
        method: str,
        arguments: dict[str, Any],
        *, control_only: bool = False,
    ) -> Any:
        kind = str(identity.get("kind") or "")
        payload = _decode_connector_identity(identity.get("id"))
        if kind == "mcp":
            lease = _lease(payload)
            stale_lease = (
                int(identity.get("generation") or -1) != lease.generation
                or int(identity.get("revision") or -1)
                != lease.capability_revision
            )
            if control_only:
                if stale_lease or method != "cancel" or set(arguments) != {"request_id"}:
                    return False
                _runtime(host).mcp.validate_cancel(
                    lease, str(arguments.get("request_id") or ""), origin=context.cell_origin,
                )
                return True
            if stale_lease:
                replacement = await refreshed_mcp_replacement(lease)
                if method == "schema" and replacement is not None:
                    return {
                        **dict(replacement["schema"]),
                        "refreshed": True,
                        "replacement_handle": replacement["handle"],
                    }
                raise ToolError(
                    stale_mcp_message(replacement),
                    code="stale_mcp_handle",
                )
            if method == "schema":
                if arguments:
                    raise ToolError("connectors.mcp.schema takes no arguments")
                try:
                    return await mcp_v2_describe({"lease": lease.to_dict()})
                except StaleMcpLease:
                    replacement = await refreshed_mcp_replacement(lease)
                    if replacement is None:
                        raise ToolError(
                            stale_mcp_message(None), code="stale_mcp_handle"
                        )
                    return {
                        **dict(replacement["schema"]),
                        "refreshed": True,
                        "replacement_handle": replacement["handle"],
                    }
            if method == "invoke":
                unknown = sorted(
                    set(arguments) - {
                        "arguments", "operation", "request_id", "raise_on_error",
                        "conclude",
                    }
                )
                if unknown:
                    raise ToolError(
                        "connectors.mcp.invoke: unknown argument(s): "
                        + ", ".join(unknown)
                    )
                raise_on_error = arguments.get("raise_on_error", True)
                if not isinstance(raise_on_error, bool):
                    raise ToolError("connectors.mcp.invoke raise_on_error must be boolean")
                conclude = arguments.get("conclude", False)
                if not isinstance(conclude, bool):
                    raise ToolError("connectors.mcp.invoke conclude must be boolean")
                default_operation = _MCP_DEFAULT_OPERATIONS.get(lease.kind, "call")
                operation = str(arguments.get("operation") or default_operation)
                tool_arguments = dict(arguments.get("arguments") or {})
                if operation == "call":
                    _validate_mcp_tool_arguments(host, lease, tool_arguments)
                try:
                    result = dict(await mcp_v2_invoke({
                        "lease": lease.to_dict(),
                        "kind": operation,
                        "arguments": tool_arguments,
                        "request_id": str(arguments.get("request_id") or ""),
                    }))
                except StaleMcpLease:
                    replacement = await refreshed_mcp_replacement(lease)
                    raise ToolError(
                        stale_mcp_message(replacement),
                        code="stale_mcp_handle",
                    )
                if (
                    raise_on_error
                    and isinstance(result, Mapping)
                    and bool(result.get("is_error"))
                ):
                    raise ToolError(
                        f"MCP tool {lease.name!r} returned an error: "
                        f"{_mcp_error_text(result)}",
                        code="mcp_tool_error",
                        cause_class="capability",
                    )
                result_data = _mcp_result_data(result)
                next_tool = (
                    str(result_data.get("next_tool") or "").strip()
                    if isinstance(result_data, Mapping)
                    else ""
                )
                if next_tool:
                    try:
                        service = _runtime(host).mcp
                        await service.refresh(lease.server_id)
                        matches = [
                            row for row in service.search(
                                next_tool,
                                kind="tool",
                                server_id=lease.server_id,
                            )
                            if str(row.get("name") or "") == next_tool
                        ]
                        if len(matches) == 1:
                            result["next_capability"] = {
                                "handle": mcp_handle(
                                    matches[0], schema_included=True
                                ),
                                "schema": mcp_schema(matches[0]),
                            }
                    except Exception as exc:
                        # The completed invocation remains authoritative. The
                        # next capability can still be found with a fresh search.
                        result["next_capability_status"] = (
                            "refresh_failed:" + type(exc).__name__
                        )
                if conclude:
                    return ToolProjectionResult(
                        json.dumps(
                            result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        programmatic_value=result,
                        receipt_metadata={
                            "projection": "mcp-result-v2",
                            "concluded": True,
                            "terminal_observation": _terminal_mcp_observation(
                                result
                            ),
                        },
                        terminate=True,
                    )
                return result
            if method == "cancel":
                if set(arguments) != {"request_id"}:
                    raise ToolError("connectors.mcp.cancel needs only request_id")
                return await _runtime(host).mcp.cancel(
                    lease.server_id, str(arguments.get("request_id") or ""),
                    lease=lease, origin=context.cell_origin,
                )
            raise ToolError(f"unsupported connectors.mcp method: {method}")
        if kind == "plugin":
            if control_only:
                return False
            package_id = str(payload.get("package_id") or "")
            digest = str(payload.get("package_digest") or "")
            expected_revision = int(
                hashlib.sha256(digest.encode("utf-8")).hexdigest()[:8], 16
            )
            if (
                int(identity.get("generation") or -1) != 1
                or int(identity.get("revision") or -1) != expected_revision
            ):
                raise ToolError("stale plugin connector handle; search again")
            if method == "inspect":
                if arguments:
                    raise ToolError("connectors.plugin.inspect takes no arguments")
                return await plugins_inspect({
                    "package_id": package_id, "digest": digest,
                })
            if method == "invoke":
                allowed = {
                    "contribution_id", "arguments", "request_id",
                    "idempotency_key", "deadline_ms", "conclude",
                }
                unknown = sorted(set(arguments) - allowed)
                if unknown or not str(arguments.get("contribution_id") or ""):
                    raise ToolError(
                        "connectors.plugin.invoke needs contribution_id and only "
                        "its documented optional arguments"
                    )
                conclude = arguments.get("conclude", False)
                if not isinstance(conclude, bool):
                    raise ToolError(
                        "connectors.plugin.invoke conclude must be boolean"
                    )
                invocation_arguments = {
                    key: value for key, value in arguments.items()
                    if key != "conclude"
                }
                result = await plugins_invoke({
                    **invocation_arguments,
                    "package_id": package_id,
                })
                if conclude:
                    terminal_observation = _terminal_plugin_observation(result)
                    return ToolProjectionResult(
                        terminal_observation,
                        programmatic_value=result,
                        receipt_metadata={
                            "projection": "plugin-result-v2",
                            "concluded": True,
                            "terminal_observation": terminal_observation,
                        },
                        terminate=True,
                    )
                return result
            if method == "read_resource":
                if set(arguments) != {"contribution_id", "resource"}:
                    raise ToolError(
                        "connectors.plugin.read_resource needs contribution_id "
                        "and resource"
                    )
                return await plugins_read_resource({
                    **arguments,
                    "package_id": package_id,
                })
            raise ToolError(f"unsupported connectors.plugin method: {method}")
        raise ToolError("connector handle kind is unsupported")

    routers = getattr(host, "remote_handle_routers", None)
    if routers is None:
        routers = {}
        setattr(host, "remote_handle_routers", routers)
    if not isinstance(routers, dict):
        raise TypeError("host.remote_handle_routers must be a dictionary")
    routers["connectors"] = connector_router
    connector_router.control_admission = (
        lambda context, identity, method, arguments:
        connector_router(context, identity, method, arguments, control_only=True)
    )


__all__ = [
    "EXTENSION_PACKAGE_JOB",
    "CONNECTOR_OBJECT_METHODS",
    "MCP_CONNECTOR_HANDLE_METHODS",
    "PLUGIN_CONNECTOR_HANDLE_METHODS",
    "register_extension_job_handlers",
    "register_extension_v2_tools",
]
