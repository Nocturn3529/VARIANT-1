"""Declared parameter order shared by kernel and mutation-worker proxies."""

from __future__ import annotations

import ast
from typing import Any, Mapping


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


__all__ = ["declared_parameter_order"]
