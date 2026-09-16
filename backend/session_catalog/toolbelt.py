"""One mounted control seed for category selection and session mutation."""

from __future__ import annotations

from typing import Any

from capability_broker import current_capability_invocation
from tools import Tool, ToolError

from .catalog import CatalogError
from .mutation import MutationError


def _tests_param(*, required: bool) -> dict[str, Any]:
    return {
        "type": "array",
        "required": bool(required),
        "minItems": 1,
        "maxItems": 20,
        "desc": (
            "Candidate cases shaped as {'arguments': {...}, 'mocks': "
            "{'capability_alias': mocked_result}, 'expected': exact_return_value}. "
            "For repeated calls to one alias, use "
            "{'$sequence': [first_result, second_result]} as that alias's mocked result."
        ),
        "items": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "arguments": {
                    "type": "object",
                    "required": True,
                    "desc": "Candidate run(arguments) input.",
                },
                "mocks": {
                    "type": "object",
                    "required": False,
                    "desc": "Object keyed by ordinary Python proxy name, such as tools.read_file.",
                },
            },
        },
    }


_SLOT_PARAM = {
    "type": "string",
    "required": True,
    "desc": (
        "Category/position slot such as build/8, or a numeric position such as "
        "8 resolved against the currently selected category."
    ),
}
_SOURCE_PARAM = {
    "type": "string",
    "required": True,
    "desc": (
        "Synchronous Python source defining run(arguments). Use the ordinary mounted "
        "Python surface inside it, for example tools.read_file(path=...) or "
        "computer.click(...). VARIANT-1 derives dependencies from the source and observed "
        "receipts. When embedding candidate source in a Python string, use chr(10) or "
        "double-escape backslashes for candidate newlines."
    ),
}
_INVOKE_PARAM = {
    "type": "object",
    "required": False,
    "desc": (
        "Optional first real call arguments. VARIANT-1 activates the adaptation and "
        "runs it before this toolbelt call returns."
    ),
}


_PROPOSAL_PARAMS = {
    "kind": {
        "type": "string",
        "required": True,
        "enum": ["mutate", "method", "create", "revise"],
    },
    "slot": _SLOT_PARAM,
    "alias": {"type": "string", "required": True},
    "purpose": {"type": "string", "required": True},
    "schema": {
        "type": "object", "required": True,
        "desc": "Public JSON object schema for candidate arguments.",
    },
    "source": _SOURCE_PARAM,
    "tests": _tests_param(required=False),
    "parent": {"type": "string", "required": False},
}
_ACTIVATE_PROPOSAL_PARAMS = {**_PROPOSAL_PARAMS, "invoke": _INVOKE_PARAM}


TOOLBELT_OBJECT_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "search",
        "description": "Rank capability roots with the canonical disclosure index.",
        "effect_class": "read",
        "params": {
            "query": {"type": "string", "required": True},
            "width": {"type": "integer", "required": False, "minimum": 1, "maximum": 5},
        },
    },
    {
        "name": "mount",
        "description": "Select one capability category for the next Python cell.",
        "effect_class": "write",
        "params": {"category": {"type": "string", "required": True}},
    },
    {
        "name": "mutation_status",
        "description": "Inspect drafts, active versions, and probation state.",
        "effect_class": "read",
        "params": {
            "limit": {"type": "integer", "required": False, "minimum": 1, "maximum": 100},
        },
    },
    {
        "name": "mutate",
        "description": (
            "Atomically replace one direct seed or revise an active synthesized tool. "
            "Retain the newly returned proxy; prior proxy versions remain fenced."
        ),
        "effect_class": "write",
        "condition": "mutation_write",
        "params": {
            "slot": _SLOT_PARAM,
            "source": _SOURCE_PARAM,
            "tests": _tests_param(required=True),
            "purpose": {"type": "string", "required": False},
            "invoke": _INVOKE_PARAM,
        },
    },
    {
        "name": "synthesize",
        "description": "Atomically test and synthesize one tool in an explicit vacancy.",
        "effect_class": "write",
        "condition": "mutation_write",
        "params": {
            "slot": _SLOT_PARAM,
            "alias": {"type": "string", "required": True},
            "purpose": {"type": "string", "required": True},
            "schema": {
                "type": "object", "required": True,
                "desc": "Public JSON object schema for candidate arguments.",
            },
            "source": _SOURCE_PARAM,
            "tests": _tests_param(required=True),
            "invoke": _INVOKE_PARAM,
        },
    },
    {
        "name": "propose",
        "description": "Propose a session-local slot without activating it.",
        "effect_class": "write",
        "condition": "mutation_write",
        "params": _PROPOSAL_PARAMS,
    },
    {
        "name": "validate",
        "description": "Validate a draft in the disposable same-user worker.",
        "effect_class": "write",
        "condition": "mutation_write",
        "params": {"draft_id": {"type": "string", "required": True}},
    },
    {
        "name": "test",
        "description": "Test a draft with mocked mounted-proxy results.",
        "effect_class": "write",
        "condition": "mutation_write",
        "params": {
            "draft_id": {"type": "string", "required": True},
            "cases": _tests_param(required=False),
        },
    },
    {
        "name": "activate",
        "description": "CAS-activate a validated draft in probation.",
        "effect_class": "write",
        "condition": "mutation_write",
        "params": {
            "draft_id": {"type": "string", "required": True},
            "expected_mount_revision": {"type": "integer", "required": False},
        },
    },
    {
        "name": "propose_activate",
        "description": "Propose, validate, host-test, and CAS-activate one draft.",
        "effect_class": "write",
        "condition": "mutation_write",
        "params": _ACTIVATE_PROPOSAL_PARAMS,
    },
    {
        "name": "rollback",
        "description": "CAS-select a predecessor or explicit slot version.",
        "effect_class": "write",
        "params": {
            "slot": _SLOT_PARAM,
            "to_version": {"type": "integer", "required": False},
            "expected_mount_revision": {"type": "integer", "required": False},
        },
    },
    {
        "name": "reset",
        "description": "Reset one slot to its immutable baseline seed or vacancy.",
        "effect_class": "write",
        "params": {
            "slot": _SLOT_PARAM,
            "expected_mount_revision": {"type": "integer", "required": False},
        },
    },
    {
        "name": "reset_all",
        "description": "Reset every active session overlay to its immutable baseline.",
        "effect_class": "write",
        "params": {},
    },
)


def _seed_params() -> dict[str, dict[str, Any]]:
    params: dict[str, dict[str, Any]] = {
        "operation": {
            "type": "string",
            "required": True,
            "enum": [str(row["name"]) for row in TOOLBELT_OBJECT_METHODS],
        }
    }
    for method in TOOLBELT_OBJECT_METHODS:
        for name, raw_spec in dict(method.get("params") or {}).items():
            spec = dict(raw_spec)
            spec["required"] = False
            current = params.get(str(name))
            if current is not None and current != spec:
                raise RuntimeError(f"conflicting toolbelt parameter schema: {name}")
            params[str(name)] = spec
    return params


def _method_arguments(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    method = next(
        (row for row in TOOLBELT_OBJECT_METHODS if row["name"] == operation),
        None,
    )
    if method is None:
        raise ToolError(f"unknown toolbelt operation: {operation!r}")
    specs = dict(method.get("params") or {})
    payload = {key: value for key, value in args.items() if key != "operation"}
    unknown = sorted(set(payload) - set(specs))
    if unknown:
        raise ToolError(
            f"toolbelt.{operation}: unknown argument(s): {', '.join(unknown)}"
        )
    for name, spec in specs.items():
        if bool(spec.get("required")) and (
            name not in payload or payload[name] in (None, "", [], {})
        ):
            raise ToolError(f"toolbelt.{operation} needs '{name}'")
    return payload


def register_toolbelt_tool(registry: Any, service: Any) -> None:
    """Install the one Control seed while retaining mutation's hidden worker."""

    if registry.get("toolbelt") is not None:
        return

    async def toolbelt(args: dict[str, Any]) -> Any:
        invocation = current_capability_invocation()
        if invocation is None or not invocation.chat_id:
            raise ToolError("toolbelt requires an active admitted Python cell")
        chat_id = invocation.chat_id
        operation = str(args.get("operation") or "")
        payload = _method_arguments(operation, args)

        async def finish_activation(activation: Any, invoke: Any) -> Any:
            if invoke is None:
                return activation
            return await mutation.invoke_activation(
                invocation,
                dict(activation or {}),
                dict(invoke or {}),
            )

        try:
            if operation == "search":
                document, _refs = service.namespace_document(
                    chat_id, query=str(payload.get("query") or "")
                )
                return list(document.get("top_k") or ())[
                    :max(1, min(int(payload.get("width") or 5), 5))
                ]
            if operation == "mount":
                selected = service.select(
                    chat_id, str(payload.get("category") or "")
                )
                return {
                    **selected,
                    "message": (
                        f"Mounted {selected.get('category_id') or payload.get('category')} "
                        "for the next Python cell. End this cell, then call its "
                        "capabilities in a new cell."
                    ),
                }
            mutation = service.mutation
            if operation == "mutation_status":
                return mutation.status(
                    chat_id, limit=int(payload.get("limit") or 20)
                )
            if operation == "mutate":
                invoke = payload.pop("invoke", None)
                activation = await mutation.mutate(chat_id, **payload)
                return await finish_activation(activation, invoke)
            if operation == "synthesize":
                invoke = payload.pop("invoke", None)
                activation = await mutation.synthesize(chat_id, **payload)
                return await finish_activation(activation, invoke)
            if operation == "propose":
                return mutation.propose(chat_id, **payload)
            if operation == "validate":
                return await mutation.validate(chat_id, str(payload["draft_id"]))
            if operation == "test":
                return await mutation.test(
                    chat_id,
                    str(payload["draft_id"]),
                    list(payload.get("cases") or ()),
                    authority="model",
                )
            if operation == "activate":
                return await mutation.activate(
                    chat_id,
                    str(payload["draft_id"]),
                    expected_mount_revision=payload.get("expected_mount_revision"),
                )
            if operation == "propose_activate":
                invoke = payload.pop("invoke", None)
                activation = await mutation.propose_activate(chat_id, **payload)
                return await finish_activation(activation, invoke)
            if operation == "rollback":
                return mutation.rollback(
                    chat_id,
                    str(payload["slot"]),
                    to_version=payload.get("to_version"),
                    expected_mount_revision=payload.get("expected_mount_revision"),
                )
            if operation == "reset":
                return mutation.reset_slot(
                    chat_id,
                    str(payload["slot"]),
                    expected_mount_revision=payload.get("expected_mount_revision"),
                )
            return mutation.reset_all(chat_id)
        except (CatalogError, MutationError) as exc:
            code = getattr(exc, "code", "toolbelt_error")
            raise ToolError(f"[{code}] {exc}") from exc

    registry.register(Tool(
        "toolbelt",
        "Category mounting and session-local mutation control.",
        toolbelt,
        category="session_infrastructure",
        params=_seed_params(),
        hidden=True,
        visibility="broker_only",
        effect_class="write",
        parallel_safe=False,
        may_return_secrets=True,
        schema_revision="variant1.toolbelt-seed.v2",
        handler_revision="variant1.toolbelt-seed-handler.v3",
        object_methods=TOOLBELT_OBJECT_METHODS,
    ))


__all__ = [
    "TOOLBELT_OBJECT_METHODS",
    "register_toolbelt_tool",
]
