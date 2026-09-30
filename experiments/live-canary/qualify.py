"""Repeat receipt-audited task qualification on one explicit model route.

Outputs are local evidence, never a claim that all providers/hardware qualify.
An optional user-specified budget is enforced in the source backend before
provider dispatch; no cap is imposed when the user has not specified one.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import time

import run_canary


def assess(results: list[dict], repetitions: int, cases: list[str], maximum_ms: float) -> dict:
    required = {case.upper() for case in cases}
    rounds = []
    for summary in results:
        rows = summary.get("results") or []
        complete = len(rows) == len(required) and {row.get("case_id") for row in rows} == required
        full = complete and all(row.get("full_passed") is True for row in rows)
        latency = complete and all(isinstance(row.get("duration_ms"), (int, float))
                                  and not isinstance(row["duration_ms"], bool)
                                  and math.isfinite(row["duration_ms"])
                                  and 0 <= row["duration_ms"] <= maximum_ms for row in rows)
        rounds.append({"full_completion": full, "latency_passed": latency})
    return {"required_repetitions": repetitions, "completed_repetitions": len(rounds),
            "cases": sorted(required), "max_case_ms": maximum_ms, "rounds": rounds,
            "qualified": len(rounds) == repetitions and all(row["full_completion"] and row["latency_passed"] for row in rounds)}


async def run(args) -> int:
    root = run_canary.RUNS / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-qualification-" + secrets.token_hex(3))
    root.mkdir(parents=True)
    budget = root / "budget.json"
    changes = {"VARIANT1_QUALIFICATION_OAUTH_ONLY": "1" if args.oauth_only else None,
               "VARIANT1_QUALIFICATION_CANDIDATE_ROUTE": "1",
               "VARIANT1_QUALIFICATION_BUDGET_FILE": str(budget) if args.max_cost_usd is not None else None}
    if args.max_cost_usd is not None:
        if args.model_id != "grok-4.7":
            raise ValueError("Specify verified conservative rates before using a budget with another model")
        budget.write_text(json.dumps({"model": args.model_id, "reasoning_effort": args.reasoning_effort,
            "max_cost_usd": args.max_cost_usd, "context_tokens": 500000,
            "input_per_million": 4, "output_per_million": 12, "upper_spend_usd": 0,
            "pricing_source": "https://docs.x.ai/developers/grok-4-7", "pricing_verified": "2026-09-30",
            "requests": []}, indent=2), encoding="utf-8")
    previous = {key: os.environ.get(key) for key in changes}
    for key, value in changes.items():
        if value is None: os.environ.pop(key, None)
        else: os.environ[key] = value
    results = []
    started = time.time()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=run_canary.ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=run_canary.ROOT, text=True).strip())
    try:
        for index in range(args.repetitions):
            result_path = root / f"round-{index + 1}.json"
            namespace = argparse.Namespace(model=args.model, model_id=args.model_id,
                reasoning_effort=args.reasoning_effort, mutation=args.mutation,
                cases=args.cases, timeout=args.timeout, target="source", frozen_backend=None,
                preflight_only=False, seed=f"{args.seed}:{index}", stop_on_failure=True,
                result_path=str(result_path))
            code = await run_canary.run(namespace)
            results.append(json.loads(result_path.read_text(encoding="utf-8")))
            if code: break
    finally:
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        gate = assess(results, args.repetitions, args.cases, args.timeout * 1000)
        try:
            completed_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=run_canary.ROOT, text=True).strip()
            completed_dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=run_canary.ROOT, text=True).strip())
        except subprocess.CalledProcessError:
            completed_commit, completed_dirty = "unknown", True
        report = {"schema": "variant1.task-qualification.v1", "source_commit": commit,
            "working_tree_dirty": dirty, "completed_source_commit": completed_commit,
            "working_tree_dirty_at_completion": completed_dirty,
            "route": {"provider": run_canary.MODEL_ROUTES[args.model]["provider"], "model": args.model_id,
            "reasoning_effort": args.reasoning_effort, "oauth_only": args.oauth_only},
            "started_at": started, "completed_at": time.time(), "seed": args.seed,
            "gate": gate, "budget": json.loads(budget.read_text()) if budget.exists() else None,
            "run_roots": [row["run_root"] for row in results]}
        (root / "qualification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Qualification evidence: {root / 'qualification.json'}", flush=True)
        print(f"Qualified: {gate['qualified']}; repetitions: {len(results)}/{args.repetitions}", flush=True)
    return 0 if gate["qualified"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=sorted(run_canary.MODEL_ROUTES), default="grok")
    parser.add_argument("--model-id")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"), default="low")
    parser.add_argument("--cases", nargs="+", default=["S1", "F8", "KRN", "ART", "MCP", "CHILD"])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--seed", default="variant1-qualification-v1")
    parser.add_argument("--mutation", action="store_true")
    parser.add_argument("--oauth-only", action="store_true")
    parser.add_argument("--max-cost-usd", type=float)
    args = parser.parse_args()
    if not args.model_id:
        args.model_id = "grok-4.7" if args.model == "grok" else run_canary.MODEL_ROUTES[args.model]["model"]
    if args.repetitions < 1 or args.timeout < 1 or (args.max_cost_usd is not None and (not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0)):
        parser.error("Repetitions, timeout, and any specified budget must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
