"""Inventory short condition waits for review without weakening timing assertions."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


def audit(path: Path) -> list[dict]:
    rows = []
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or not function.name.startswith("test_"):
            continue
        for call in ast.walk(function):
            if not isinstance(call, ast.Call): continue
            name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else ""
            if name not in {"wait_for", "_until", "until"}: continue
            timeout = next((arg.value for arg in call.keywords if arg.arg in {"timeout", "timeout_s"}), None)
            if timeout is None and name == "wait_for" and len(call.args) > 1: timeout = call.args[1]
            numeric = timeout.value if isinstance(timeout, ast.Constant) and isinstance(timeout.value, (int, float)) else None
            if name == "wait_for" and (numeric is None or numeric > 5): continue
            rows.append({"file": path.as_posix(), "test": function.name, "line": call.lineno,
                "call": name, "literal_timeout_s": numeric,
                "review": "Determine whether this asserts timing or waits for a condition; do not increase it mechanically."})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    rows = [row for path in sorted((root / "backend" / "tests").glob("test_*.py")) for row in audit(path)]
    for row in rows: row["file"] = Path(row["file"]).relative_to(root).as_posix()
    report = {"schema": "variant1.test-wait-review.v1", "short_wait_sites": len(rows),
              "policy": "Condition waits use explicit budgets; latency assertions retain their intended bound.", "sites": rows}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
