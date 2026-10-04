"""Session-tool contracts derived from the catalog and candidate source.

Pure helpers: the public JSON schema of a candidate, catalog slot references,
the registration source check and declared positional parameter order. The
mutation manager owns state; these only compute.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Mapping, Sequence

from kernel_runtime.candidate_contract import CandidateContractError, validate_source
from kernel_runtime.proxy_arguments import (
    RESERVED_PARAMETER_NAMES,
    declared_parameter_order,
    proxy_name_problem,
)

from .catalog import LoadedCatalog, canonical_bytes
from .mutation_contracts import MutationError, MutationWorkerError


def public_schema(schema: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = json.loads(json.dumps(schema, ensure_ascii=False))
    if not isinstance(raw, dict) or raw.get("type") != "object":
        raise MutationError(
            "invalid_schema", "mutation schema must be a JSON object schema"
        )
    properties = raw.get("properties")
    if not isinstance(properties, dict):
        raise MutationError(
            "invalid_schema", "mutation schema properties must be an object"
        )
    if len(properties) > 32:
        raise MutationError("schema_quota", "mutation schema exceeds 32 properties")
    required = {str(item) for item in raw.get("required") or ()}
    if not required <= set(properties):
        raise MutationError("invalid_schema", "schema required names must exist")
    supported = {
        "any", "string", "integer", "number", "boolean", "array", "object",
    }
    params: dict[str, Any] = {}
    for name, value in properties.items():
        clean = str(name)
        problem = proxy_name_problem(clean, RESERVED_PARAMETER_NAMES)
        if problem:
            raise MutationError(
                "invalid_schema", f"parameter name {clean!r} {problem}"
            )
        spec = dict(value) if isinstance(value, dict) else {}
        if str(spec.get("type") or "") not in supported:
            raise MutationError(
                "invalid_schema", f"unsupported type for {clean!r}: {spec.get('type')!r}"
            )
        params[clean] = {**spec, "required": clean in required}
    raw["additionalProperties"] = False
    raw["required"] = sorted(required)
    raw["properties"] = properties
    if len(canonical_bytes(raw)) > 16 * 1024:
        raise MutationError("schema_quota", "mutation schema exceeds 16384 bytes")
    return raw, params


def atomic_schema_from_params(params: Any) -> dict[str, Any]:
    """Project one existing direct seed contract into mutation JSON Schema."""

    type_names = {
        "str": "string", "int": "integer", "float": "number",
        "bool": "boolean", "dict": "object", "list": "array",
    }

    def normalize(raw: Any) -> dict[str, Any]:
        spec = dict(raw) if isinstance(raw, dict) else {}
        typ = str(spec.get("type") or "string")
        spec["type"] = type_names.get(typ, typ)
        spec.pop("required", None)
        if isinstance(spec.get("items"), dict):
            spec["items"] = normalize(spec["items"])
        if isinstance(spec.get("properties"), dict):
            spec["properties"] = {
                str(name): normalize(child)
                for name, child in spec["properties"].items()
            }
        return spec

    rows = dict(params or {})
    required = sorted(
        str(name) for name, spec in rows.items()
        if isinstance(spec, dict) and bool(spec.get("required"))
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            str(name): normalize(spec) for name, spec in rows.items()
        },
    }


def resolve_slot(loaded: LoadedCatalog, reference: str) -> tuple[str, int, dict[str, Any]]:
    raw = str(reference or "").strip()
    prefix = loaded.release_id + "/"
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    match = re.fullmatch(r"([a-z][a-z0-9_]*)[/.:]([1-9][0-9]*)", raw.casefold())
    if match:
        category_id, position_text = match.groups()
        position = int(position_text)
        for category in loaded.document.get("categories") or ():
            if category.get("category_id") != category_id:
                continue
            for candidate in category.get("slots") or ():
                if int(candidate.get("position") or 0) == position:
                    return category_id, position, dict(candidate)
    matches: list[tuple[str, int, dict[str, Any]]] = []
    for category in loaded.document.get("categories") or ():
        for candidate in category.get("slots") or ():
            aliases = {
                str(binding.get("alias") or "").casefold()
                for binding in candidate.get("bindings") or ()
            }
            aliases.update({
                str(candidate.get("bundle") or "").casefold(),
            })
            if raw.casefold() in aliases:
                matches.append((
                    str(category.get("category_id") or ""),
                    int(candidate.get("position") or 0),
                    dict(candidate),
                ))
    if len(matches) == 1:
        return matches[0]
    raise MutationError("invalid_slot", f"unknown or ambiguous mutation slot: {reference!r}")


def selected_slot_reference(record: Any, reference: Any) -> str:
    """Resolve a bare numeric position against the chat's selected category."""

    raw = str(reference or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        return raw
    selected = str(
        getattr(getattr(record, "identity", None), "selected_category_id", "")
        or ""
    ).strip()
    if not selected:
        raise MutationError(
            "invalid_slot",
            f"numeric mutation slot {raw!r} requires a selected category",
        )
    return f"{selected}/{int(raw)}"


def source_check_report(source: str) -> dict[str, Any]:
    """Check syntax and the ``run(arguments)`` contract without running it."""

    try:
        _tree, digest = validate_source(source)
    except CandidateContractError as exc:
        raise MutationWorkerError("candidate_contract_error", str(exc)) from exc
    return {
        "ok": True,
        "source_sha256": digest,
        "language_policy": "full-python.same-user.v1",
        "execution_mode": "same_user",
    }


def wrapped_helper_order(source: str) -> list[str]:
    """Parameter order of the helper a promoted ``run(arguments)`` forwards to.

    Promotion packages ``def helper(...)`` plus ``run(arguments)`` returning
    ``helper(**arguments)``. Contracts travel as sorted JSON, so the helper's
    own signature is the only record of its declared positional order.
    """

    try:
        tree = ast.parse(str(source), filename="<session-mutation>", mode="exec")
    except SyntaxError:
        return []
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    run = functions.get("run")
    if run is None or len(run.body) != 1 or not isinstance(run.body[0], ast.Return):
        return []
    call = run.body[0].value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and not call.args
        and len(call.keywords) == 1
        and call.keywords[0].arg is None
        and isinstance(call.keywords[0].value, ast.Name)
        and call.keywords[0].value.id == "arguments"
    ):
        return []
    helper = functions.get(call.func.id)
    if helper is None or helper is run:
        return []
    arguments = helper.args
    return [
        item.arg
        for item in arguments.posonlyargs + arguments.args + arguments.kwonlyargs
    ]


def slot_parameter_order(slot: Mapping[str, Any], alias: str, kind: str) -> list[str]:
    """Declared order of the seed or method contract a mutation replaces."""

    rows = [
        dict(item) for item in (
            slot.get("methods") if kind == "method" else slot.get("bindings")
        ) or ()
        if isinstance(item, Mapping)
        and (kind != "method" or str(item.get("alias") or "") == alias)
    ]
    if not rows:
        return []
    return declared_parameter_order(
        dict(rows[0].get("params") or {}), str(rows[0].get("signature") or "")
    )


def parameter_order(params: Mapping[str, Any], declared: Sequence[str]) -> list[str]:
    known = list(dict.fromkeys(str(name) for name in declared if str(name) in params))
    return known + sorted(str(name) for name in params if str(name) not in known)


def ordered_params(params: Mapping[str, Any], declared: Any) -> dict[str, Any]:
    """Params in declared order; stored rows without an order stay as they are."""

    order = declared if isinstance(declared, (list, tuple)) else ()
    if not order:
        return dict(params)
    return {name: params[name] for name in parameter_order(params, order)}


def stored_order(value: Any) -> list[str]:
    try:
        decoded = json.loads(value) if value else []
    except (TypeError, ValueError):
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


__all__ = [
    "atomic_schema_from_params",
    "ordered_params",
    "parameter_order",
    "public_schema",
    "resolve_slot",
    "selected_slot_reference",
    "slot_parameter_order",
    "source_check_report",
    "stored_order",
    "wrapped_helper_order",
]
