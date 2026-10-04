"""Call-argument rules shared by kernel proxies and mutation-worker proxies.

A promoted helper must call mounted capabilities exactly as it did in the
persistent kernel, so both proxy kinds normalize Python call arguments here.
Fixed dispatcher arguments, argument envelopes and remote-handle encoding stay
with the transport that applies them today.
"""

from __future__ import annotations

import ast
import inspect
import keyword
from typing import Any, Mapping, Sequence


# Mounted aliases and parameters become Python attributes and inspect.Parameter
# names. Registration applies this rule, so an active session tool is always
# one the kernel can mount.
MAX_PROXY_NAME_LENGTH = 64
# ``tools`` keeps these for its own discovery API.
TOOLS_RESERVED_NAMES = frozenset({"aliases", "methods", "describe", "documentation"})
# ``<proxy>.async_(...)`` adds this keyword to every capability signature.
RESERVED_PARAMETER_NAMES = frozenset({"_deadline_ms"})

_ANNOTATIONS = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def proxy_name_problem(name: str, reserved: frozenset[str] = frozenset()) -> str:
    """Why ``name`` cannot be a mounted alias or parameter name; ``""`` if it can."""

    if not (
        name.isascii()
        and name.isidentifier()
        and len(name) <= MAX_PROXY_NAME_LENGTH
    ):
        return (
            "must be an ASCII Python identifier of at most "
            f"{MAX_PROXY_NAME_LENGTH} characters"
        )
    if keyword.iskeyword(name):
        return "is a Python keyword"
    if name in reserved:
        return "is reserved by the mounted namespace"
    return ""


def declared_parameter_order(params: Mapping[str, Any], signature: str = "") -> list[str]:
    """Return the order positional arguments bind to.

    Catalog artifacts are canonical JSON, so ``params`` keys arrive sorted.
    The catalog's ``signature`` keeps the author's order; use it when it names
    exactly the same parameters, otherwise fall back to required-then-optional.
    """

    names = [str(name) for name in params]
    required = [
        name for name in names
        if isinstance(params[name], Mapping) and params[name].get("required")
    ]
    ordered = required + [name for name in names if name not in required]
    raw = str(signature or "").strip()
    left, right = raw.find("("), raw.rfind(")")
    if 0 <= left < right:
        # Defaults may contain commas, quoted text or nested containers.
        # Parse only the syntax; no default expression is ever evaluated.
        try:
            parsed = ast.parse("def _contract" + raw[left:right + 1] + ": pass")
            arguments = parsed.body[0].args
            declared = [item.arg for item in
                        arguments.posonlyargs + arguments.args + arguments.kwonlyargs]
        except (SyntaxError, ValueError):
            declared = []
        if (
            len(declared) == len(ordered)
            and len(set(declared)) == len(declared)
            and set(declared) == set(ordered)
        ):
            return declared
    return ordered


def proxy_signature(ordered: Sequence[tuple[str, Mapping[str, Any]]]) -> inspect.Signature:
    """Python signature of a proxy; required parameters have no default."""

    return inspect.Signature([
        inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=(inspect.Parameter.empty if spec.get("required") else spec.get("default")),
            annotation=_ANNOTATIONS.get(
                str(spec.get("type") or "").lower(), inspect.Parameter.empty
            ),
        )
        for name, spec in ordered
    ])


def binding_param_specs(params: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The parameter facts the argument rules read, small enough for worker frames."""

    specs: dict[str, dict[str, Any]] = {}
    for name, raw in params.items():
        spec = raw if isinstance(raw, Mapping) else {}
        items = spec.get("items") if isinstance(spec.get("items"), Mapping) else {}
        clean: dict[str, Any] = {
            "required": bool(spec.get("required")),
            "type": str(spec.get("type") or ""),
        }
        if items.get("type"):
            clean["items"] = {"type": str(items.get("type"))}
        if "default" in spec and isinstance(spec["default"], (str, int, float, bool)):
            clean["default"] = spec["default"]
        specs[str(name)] = clean
    return specs


def normalize_call_arguments(
    qualified_name: str,
    signature: inspect.Signature,
    param_specs: Mapping[str, Mapping[str, Any]],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one Python call to capability arguments.

    Keeps the conveniences models rely on: an argv list for ``run_command``,
    a provider-style argument dictionary, a one-item array for a single object,
    and selection/tool-name shorthands. ``None`` values are omitted.
    """

    ordered_names = list(signature.parameters)
    if len(args) > len(ordered_names):
        raise TypeError(
            f"Invalid call to {qualified_name}{signature}: "
            f"received {len(args)} positional arguments, accepts at most "
            f"{len(ordered_names)}. Inspect "
            f"{qualified_name}.documentation() for the exact contract."
        )
    values = dict(kwargs)
    positional = list(args)
    if (
        len(positional) == 1
        and isinstance(positional[0], list)
        and "argv" in ordered_names
        and "command" in ordered_names
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
        if set(candidate).issubset(set(ordered_names)):
            # Accept the conventional provider-style argument envelope in
            # addition to normal Python keyword arguments.
            values = candidate
            positional = []
        elif len(ordered_names) == 1:
            name = ordered_names[0]
            spec = param_specs.get(name) or {}
            items = spec.get("items") if isinstance(spec.get("items"), Mapping) else {}
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
            "selection" in ordered_names
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
            "selection" in ordered_names
            and "tool_name" in ordered_names
            and isinstance(candidate, str)
        ):
            # A lone name is the natural unique-tool shorthand; the host
            # still rejects ambiguity before any connector call.
            values = {"tool_name": candidate}
            positional = []
    for index, value in enumerate(positional):
        name = ordered_names[index]
        if name in values:
            raise TypeError(f"{qualified_name} got multiple values for {name!r}")
        values[name] = value
    try:
        bound = signature.bind(**values)
    except TypeError as exc:
        raise TypeError(
            f"Invalid call to {qualified_name}{signature}: {exc}. "
            f"Inspect {qualified_name}.documentation() for the exact contract."
        ) from None
    return {
        key: value
        for key, value in bound.arguments.items()
        if value is not None
    }


__all__ = [
    "MAX_PROXY_NAME_LENGTH",
    "RESERVED_PARAMETER_NAMES",
    "TOOLS_RESERVED_NAMES",
    "binding_param_specs",
    "declared_parameter_order",
    "normalize_call_arguments",
    "proxy_name_problem",
    "proxy_signature",
]
