"""One broker handler per mounted Python object.

Thin seeds (artifacts, children, toolbelt) already dispatch methods through a
single hidden tool. Category APIs use the same contract: the kernel proxy
sends ``operation`` via ``fixed_arguments``; the host validates per-method
params and runs the matching implementation.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from tools import Tool, ToolError, validate_arguments


def dispatcher_params(
    methods: Sequence[Mapping[str, Any]],
    *,
    operation_param: str = "operation",
    operation_required: bool = True,
    base_params: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    params: dict[str, dict[str, Any]] = {
        operation_param: {
            "type": "string",
            "required": bool(operation_required),
            "enum": [str(row["name"]) for row in methods],
        }
    }
    param_groups = [dict(base_params or {})]
    param_groups.extend(dict(method.get("params") or {}) for method in methods)
    for group in param_groups:
        for name, raw_spec in group.items():
            spec = dict(raw_spec)
            spec["required"] = False
            current = params.get(str(name))
            params[str(name)] = (
                spec if current is None else _merge_param_spec(current, spec, str(name))
            )
    return params


def _merge_param_spec(
    current: Mapping[str, Any], incoming: Mapping[str, Any], name: str
) -> dict[str, Any]:
    merged = dict(current)
    for key, value in incoming.items():
        if key == "required":
            merged["required"] = False
            continue
        if key not in merged:
            merged[key] = value
            continue
        if merged[key] == value:
            continue
        if key in {"maximum", "maxLength", "maxProperties", "maxItems"}:
            merged[key] = max(int(merged[key]), int(value))
            continue
        if key == "minimum":
            merged[key] = min(int(merged[key]), int(value))
            continue
        if key == "enum" and isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = sorted({str(item) for item in [*merged[key], *value]})
            continue
        if key in {"desc", "description"}:
            continue
        if key == "type" and merged[key] != value:
            raise RuntimeError(f"conflicting object-API parameter schema: {name}")
    return merged


def method_arguments(
    methods: Sequence[Mapping[str, Any]],
    operation: str,
    args: Mapping[str, Any],
    *,
    api_name: str,
) -> dict[str, Any]:
    method = next((row for row in methods if row["name"] == operation), None)
    if method is None:
        raise ToolError(f"unknown {api_name} operation: {operation!r}")
    specs = dict(method.get("params") or {})
    payload = {key: value for key, value in args.items() if key != "operation"}
    unknown = sorted(set(payload) - set(specs))
    if unknown:
        raise ToolError(
            f"{api_name}.{operation}: unknown argument(s): {', '.join(unknown)}"
        )
    return validate_arguments(f"{api_name}.{operation}", payload, specs)


async def dispatch_object(
    methods: Sequence[Mapping[str, Any]],
    args: Mapping[str, Any],
    *,
    api_name: str,
    handlers: Mapping[str, Callable[[dict[str, Any]], Any]],
) -> Any:
    operation = str(args.get("operation") or "")
    payload = method_arguments(methods, operation, args, api_name=api_name)
    handler = handlers.get(operation)
    if handler is None:
        raise ToolError(f"unknown {api_name} operation: {operation!r}")
    result = handler(payload)
    if inspect.isawaitable(result) or isinstance(result, Awaitable):
        return await result
    return result


def register_object_tool(
    registry: Any,
    *,
    name: str,
    description: str,
    methods: Sequence[Mapping[str, Any]],
    handler: Callable[[dict[str, Any]], Any],
    category: str,
    schema_revision: str,
    handler_revision: str,
    effect_class: str = "write",
    idempotency: str = "caller_key",
    may_return_secrets: bool = True,
    default_deadline_ms: int = 0,
) -> None:
    """Install one canonical mounted-object dispatcher."""

    if registry.get(name) is not None:
        return
    registry.register(Tool(
        name,
        description,
        handler,
        category=category,
        hidden=True,
        visibility="broker_only",
        effect_class=effect_class,
        parallel_safe=False,
        idempotency=idempotency,
        may_return_secrets=may_return_secrets,
        default_deadline_ms=default_deadline_ms,
        params=dispatcher_params(methods),
        schema_revision=schema_revision,
        handler_revision=handler_revision,
        object_methods=tuple(methods),
    ))
