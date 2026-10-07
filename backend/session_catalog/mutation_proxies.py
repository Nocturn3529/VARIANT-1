"""The ordinary Python proxy surface a session tool's worker may call.

Contracts come from the pinned catalog, the chat's active session tools and
the hidden remote-handle route. The mutation manager gathers those inputs;
these functions only compute.
"""

from __future__ import annotations

import ast
from typing import AbstractSet, Any, Iterable, Mapping

from capability_broker import CapabilityRef
from kernel_runtime.proxy_arguments import binding_param_specs, declared_parameter_order

from .catalog import LoadedCatalog
from .mutation_contracts import (
    MUTATION_REMOTE_HANDLE_PROXY,
    MUTATION_REMOTE_HANDLE_ROLE,
    MutationError,
)
from .mutation_schema import resolve_slot


def build_proxy_contracts(
    loaded: LoadedCatalog,
    *,
    enabled: AbstractSet[str],
    overlays: Iterable[Mapping[str, Any]] = (),
    hidden_dispatch: Any = None,
    exclude_slot_id: str = "",
    exclude_proxy: str = "",
) -> dict[str, dict[str, Any]]:
    """Build the ordinary Python proxy surface available to mutations.

    Category mounts remain a disclosure mechanism. Candidate code may use
    any currently installed catalog proxy by its normal Python name; the
    host derives dependencies from source and observed receipts.
    ``overlays`` are the chat's active session-tool descriptors and
    ``hidden_dispatch`` the enabled remote-handle tool, if any.
    """

    contracts: dict[str, dict[str, Any]] = {}

    def add(
        qualified_name: str,
        *,
        binding: Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        fixed_arguments: Mapping[str, Any] | None = None,
        argument_envelope: bool = False,
        slot_id: str = "",
        slot_version: int = 0,
        effect_class: str = "",
        signature: str = "",
    ) -> None:
        name = str(qualified_name or "").strip()
        if not name or name in contracts:
            return
        ref = CapabilityRef(
            capability_id=str(binding.get("capability_id") or ""),
            schema_revision=str(binding.get("schema_revision") or ""),
            handler_revision=str(binding.get("handler_revision") or ""),
            catalog_release_id=loaded.release_id,
            slot_id=str(slot_id or ""),
            slot_version=max(0, int(slot_version or 0)),
        )
        contract_params = dict(params or binding.get("params") or {})
        contracts[name] = {
            "proxy": name,
            "ref": ref.to_dict(),
            "effect_class": str(
                effect_class
                or binding.get("effect_class")
                or "external_side_effect"
            ),
            # Positional calls in the worker bind in the declared order,
            # exactly as the kernel proxy does.
            "parameters": declared_parameter_order(
                contract_params,
                str(signature or binding.get("signature") or ""),
            ),
            "param_specs": binding_param_specs(contract_params),
            "fixed_arguments": dict(fixed_arguments or {}),
            "argument_envelope": bool(argument_envelope),
        }

    for category in loaded.document.get("categories") or ():
        category_id = str(category.get("category_id") or "")
        for slot in category.get("slots") or ():
            position = int(slot.get("position") or 0)
            slot_id = f"{loaded.release_id}/{category_id}/{position}"
            bindings = [
                dict(row) for row in (slot.get("bindings") or ())
                if str(row.get("tool_name") or "") in enabled
            ]
            if not bindings:
                continue
            projection = str(slot.get("projection") or "seeds")
            if projection == "seeds":
                for binding in bindings:
                    add(
                        f"tools.{binding.get('alias')}",
                        binding=binding,
                        slot_id=slot_id,
                    )
                continue
            if projection != "object" or len(bindings) != 1:
                continue
            binding = bindings[0]
            root = str(slot.get("bundle") or "")
            for method in slot.get("methods") or ():
                if not isinstance(method, Mapping):
                    continue
                alias = str(method.get("alias") or "")
                add(
                    f"{root}.{alias}",
                    binding=binding,
                    params=dict(method.get("params") or {}),
                    fixed_arguments={
                        "operation": str(method.get("operation") or alias)
                    },
                    slot_id=slot_id,
                    effect_class=str(method.get("effect_class") or ""),
                    signature=str(method.get("signature") or ""),
                )

        for api in category.get("python_apis") or ():
            if not isinstance(api, Mapping):
                continue
            root = str(api.get("name") or "")
            if not root or root == "toolbelt":
                continue
            transport = dict(api.get("transport") or {})
            if str(api.get("tool_name") or transport.get("tool_name") or "") not in enabled:
                continue
            binding = {
                **transport,
                "capability_id": str(
                    transport.get("capability_id")
                    or api.get("tool_name")
                    or root
                ),
            }
            slot_id = f"{loaded.release_id}/{category_id}/api-{root}"
            for method in api.get("methods") or ():
                if not isinstance(method, Mapping):
                    continue
                alias = str(method.get("alias") or "")
                add(
                    f"{root}.{alias}",
                    binding=binding,
                    params=dict(method.get("params") or {}),
                    fixed_arguments={
                        "operation": str(method.get("operation") or alias)
                    },
                    slot_id=slot_id,
                    effect_class=str(method.get("effect_class") or ""),
                    signature=str(method.get("signature") or ""),
                )

    # Activated session tools are finite slot versions, not a growing
    # script list. They can be composed by later session mutations.
    for descriptor in overlays:
        same_slot = str(descriptor.get("slot_id") or "") == str(
            exclude_slot_id or ""
        )
        is_object = str(descriptor.get("kind") or "") == "mounted_object"
        if same_slot and not (is_object and exclude_proxy):
            continue
        if is_object:
            root = str(descriptor.get("name") or "")
            for method in descriptor.get("methods") or ():
                if not isinstance(method, Mapping):
                    continue
                qualified = f"{root}.{method.get('alias')}"
                if same_slot and qualified == str(exclude_proxy or ""):
                    continue
                contracts.pop(qualified, None)
                add(
                    qualified,
                    binding=method,
                    params=dict(method.get("params") or {}),
                    fixed_arguments=dict(method.get("fixed_arguments") or {}),
                    argument_envelope=bool(method.get("argument_envelope")),
                    slot_id=str(method.get("slot_id") or ""),
                    slot_version=int(method.get("slot_version") or 0),
                    effect_class=str(method.get("effect_class") or ""),
                )
        else:
            contracts.pop(f"tools.{descriptor.get('alias')}", None)
            add(
                f"tools.{descriptor.get('alias')}",
                binding=descriptor,
                params=dict(descriptor.get("params") or {}),
                fixed_arguments=dict(descriptor.get("fixed_arguments") or {}),
                argument_envelope=bool(descriptor.get("argument_envelope")),
                slot_id=str(descriptor.get("slot_id") or ""),
                slot_version=int(descriptor.get("slot_version") or 0),
                effect_class=str(descriptor.get("effect_class") or ""),
            )

    # Remote handles remain host-owned and opaque inside the disposable
    # mutation worker.  Give the worker one private broker route so a
    # handle returned by any normal proxy can dispatch its declared
    # methods without exposing connector credentials or live leases.
    if hidden_dispatch is not None:
        add(
            MUTATION_REMOTE_HANDLE_PROXY,
            binding=hidden_dispatch.broker_metadata(),
            params=dict(hidden_dispatch.params or {}),
            effect_class="external_side_effect",
        )
        contracts[MUTATION_REMOTE_HANDLE_PROXY]["internal_role"] = (
            MUTATION_REMOTE_HANDLE_ROLE
        )
    return contracts


def draft_target_proxy(
    loaded: LoadedCatalog, draft: Mapping[str, Any],
) -> str:
    if str(draft.get("declared_kind") or "") != "method":
        return ""
    _category, _position, slot = resolve_slot(
        loaded, str(draft.get("slot_id") or "")
    )
    return (
        f"{slot.get('bundle')}.{draft.get('alias')}"
        if slot.get("bundle") and draft.get("alias")
        else ""
    )


def worker_proxy_contracts(
    contracts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(name): {
            "parameters": list(dict(contract).get("parameters") or ()),
            "param_specs": dict(dict(contract).get("param_specs") or {}),
            **(
                {"internal_role": str(dict(contract).get("internal_role"))}
                if dict(contract).get("internal_role")
                else {}
            ),
        }
        for name, contract in contracts.items()
    }


def source_dependencies(
    source: str,
    contracts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(str(source), filename="<session-mutation>", mode="exec")
    except SyntaxError:
        return []
    roots = {name.split(".", 1)[0] for name in contracts}
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name):
            continue
        root = str(node.func.value.id)
        if root not in roots:
            continue
        qualified = f"{root}.{node.func.attr}"
        if qualified not in contracts:
            raise MutationError(
                "unknown_proxy",
                f"mutation source calls an unavailable proxy: {qualified}",
            )
        referenced.add(qualified)
    return [dict(contracts[name]) for name in sorted(referenced)]


__all__ = [
    "build_proxy_contracts",
    "draft_target_proxy",
    "source_dependencies",
    "worker_proxy_contracts",
]
