"""The registered-tool source contract, checked without running candidate code.

Both the host (registration) and the disposable mutation worker (execution)
use this so a source that registers is exactly a source the worker accepts.
"""

from __future__ import annotations

import ast
import hashlib

MAX_SOURCE_BYTES = 64 * 1024
# Registration bounds the host enforces and the kernel's helper promotion
# respects when it derives values for the model.
MAX_PURPOSE_CHARS = 2000
MAX_EXAMPLE_CASES = 20
MAX_EXAMPLE_BYTES = 64 * 1024


class CandidateContractError(ValueError):
    pass


def validate_source(source: str) -> tuple[ast.Module, str]:
    """Validate only the mutation callable contract, not Python capability."""
    raw = str(source or "")
    if not raw.strip():
        raise CandidateContractError("candidate source is empty")
    if len(raw.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise CandidateContractError(
            f"candidate source exceeds {MAX_SOURCE_BYTES} bytes"
        )
    try:
        tree = ast.parse(raw, filename="<session-mutation>", mode="exec")
    except SyntaxError as exc:
        raise CandidateContractError(
            f"candidate syntax error at line {exc.lineno}: {exc.msg}"
        ) from exc
    run_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run"
    ]
    if len(run_nodes) != 1 or isinstance(run_nodes[0], ast.AsyncFunctionDef):
        raise CandidateContractError(
            "candidate must define exactly one synchronous run(arguments)"
        )
    run_node = run_nodes[0]
    positional = list(run_node.args.posonlyargs) + list(run_node.args.args)
    if [node.arg for node in positional] != ["arguments"]:
        raise CandidateContractError("run must have the exact signature run(arguments)")
    if (
        run_node.args.vararg is not None
        or run_node.args.kwarg is not None
        or run_node.args.kwonlyargs
        or run_node.args.defaults
    ):
        raise CandidateContractError("run must have the exact signature run(arguments)")
    compile(tree, "<session-mutation>", "exec")
    return tree, hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "CandidateContractError",
    "MAX_EXAMPLE_BYTES",
    "MAX_EXAMPLE_CASES",
    "MAX_PURPOSE_CHARS",
    "MAX_SOURCE_BYTES",
    "validate_source",
]
