"""Plan or execute the ordered, gated VARIANT-1 Phase 12 evaluation.

The default operation is read-only: print the exact plan. Live inference starts
only with ``--execute``. Required stages stop the sequence immediately, and the
local Qwen model is unreachable until every required cloud stage has passed in
the same plan state.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import uuid
from typing import Any

from eval_plan import (
    CLOUD_MODEL_ORDER,
    MODEL_BY_KEY,
    MODEL_ORDER,
    frozen_kernel_path,
    plan_document,
    stages_for_model,
)
from run_canary import preflight_model, read_json, utc_now, write_json


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
EVAL_ROOT = Path(__file__).resolve().parent
RUNS = EVAL_ROOT / "runs"
PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"
DEFAULT_FROZEN_BACKEND = (
    ROOT / "dist" / "win-unpacked" / "resources" / "backend" / "Variant1Backend.exe"
)
STATE_SCHEMA = "variant1.astb.phase12-sequence.v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_seed(master: str, model_key: str, stage_id: str) -> str:
    return hashlib.sha256(
        f"{master}\0{model_key}\0{stage_id}".encode("utf-8")
    ).hexdigest()


def _backend_source_paths(root: Path) -> list[Path]:
    """Return authored backend Python, never generated/frozen copies."""

    backend = root.resolve()
    excluded = tuple(
        (backend / name).resolve()
        for name in (".venv", "build", "dist", "tests")
    )
    return [
        item
        for item in backend.rglob("*.py")
        if "__pycache__" not in item.parts
        and not any(item.is_relative_to(parent) for parent in excluded)
    ]


def _kernel_source_paths(root: Path) -> list[Path]:
    """Return authored sources that can enter the separately frozen kernel."""

    backend = root.resolve()
    return [
        item
        for item in (backend / "kernel_runtime").rglob("*.py")
        if "__pycache__" not in item.parts
    ]


def _models_through(last: str) -> tuple[str, ...]:
    index = MODEL_ORDER.index(last)
    return MODEL_ORDER[: index + 1]


def _frozen_readiness(path: Path) -> dict[str, Any]:
    backend = path.resolve()
    kernel = frozen_kernel_path(backend)
    missing = [str(item) for item in (backend, kernel) if not item.is_file()]
    if missing:
        return {"ready": False, "missing": missing}
    source_paths = _backend_source_paths(ROOT / "backend")
    kernel_source_paths = _kernel_source_paths(ROOT / "backend")
    latest_source_ns = max((item.stat().st_mtime_ns for item in source_paths), default=0)
    latest_kernel_source_ns = max(
        (item.stat().st_mtime_ns for item in kernel_source_paths),
        default=0,
    )
    stale = {
        str(backend): backend.stat().st_mtime_ns < latest_source_ns,
        str(kernel): kernel.stat().st_mtime_ns < latest_kernel_source_ns,
    }
    return {
        "ready": not any(stale.values()),
        "backend": str(backend),
        "kernel": str(kernel),
        "backend_sha256": _sha256(backend),
        "kernel_sha256": _sha256(kernel),
        "latest_source_mtime_ns": latest_source_ns,
        "latest_kernel_source_mtime_ns": latest_kernel_source_ns,
        "stale": stale,
    }


def _command_for(
    *,
    model_key: str,
    stage,
    target: str,
    frozen_backend: Path,
    timeout: int,
    seed: str,
) -> list[str]:
    if stage.runner == "manifests":
        script = EVAL_ROOT / "run_provider_manifest_canary.py"
        command = [
            str(PYTHON), str(script), "--model", model_key,
            "--sources", *stage.sources,
        ]
    elif stage.runner == "canary":
        script = EVAL_ROOT / "run_canary.py"
        command = [
            str(PYTHON), str(script), "--model", model_key,
            "--cases", *stage.cases,
        ]
        if stage.mutation:
            command.append("--mutation")
    else:
        raise ValueError(f"unknown stage runner: {stage.runner}")
    command.extend((
        "--target", target,
        "--timeout", str(timeout),
        "--seed", seed,
    ))
    if target == "frozen":
        command.extend(("--frozen-backend", str(frozen_backend)))
    return command


def _stage_units(stage) -> tuple[tuple[str, Any], ...]:
    """Checkpoint each task case independently for focused repair/resume."""

    if stage.runner == "canary" and stage.cases:
        return tuple(
            (
                f"{stage.stage_id}/{case_id}",
                replace(stage, cases=(case_id,)),
            )
            for case_id in stage.cases
        )
    return ((stage.stage_id, stage),)


def _display_command(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def printable_plan(args: argparse.Namespace) -> dict[str, Any]:
    document = plan_document(
        include_optional_mutation=args.include_optional_mutation,
    )
    document["execution"] = {
        "target": args.target,
        "through": args.through,
        "live_inference": bool(args.execute),
        "default_live_inference": False,
        "timeout_s": args.timeout,
        "frozen_backend": (
            str(Path(args.frozen_backend).resolve()) if args.target == "frozen" else None
        ),
    }
    return document


async def _preflight_selected(model_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for key in model_keys:
        try:
            rows.append(await preflight_model(key))
        except Exception as exc:
            rows.append({
                "model_key": key,
                "display_name": MODEL_BY_KEY[key].display_name,
                "route": MODEL_BY_KEY[key].route(),
                "ready": False,
                "inference_sent": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return rows


def _new_state(
    *, target: str, plan: dict[str, Any], master_seed: str, run_root: Path,
) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "target": target,
        "status": "running",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "experiment_seed": master_seed,
        "run_root": str(run_root),
        "preflight": [],
        "stages": [],
    }


def _load_or_create_state(
    args: argparse.Namespace,
    plan: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if args.resume:
        state_path = Path(args.resume).expanduser().resolve()
        state = read_json(state_path)
        if state.get("schema") != STATE_SCHEMA:
            raise RuntimeError("resume state has the wrong schema")
        if state.get("plan_sha256") != plan["plan_sha256"]:
            raise RuntimeError("resume state belongs to a different evaluation plan")
        if state.get("target") != args.target:
            raise RuntimeError("resume state target does not match --target")
        return state_path, state
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = RUNS / f"phase12-{stamp}-{args.target}-{uuid.uuid4().hex[:6]}"
    run_root.mkdir(parents=True, exist_ok=False)
    state_path = run_root / "sequence.json"
    master = str(args.experiment_seed or secrets.token_hex(32))
    state = _new_state(
        target=args.target, plan=plan, master_seed=master, run_root=run_root,
    )
    write_json(state_path, state)
    return state_path, state


def _recorded_passes(state: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (
            str(row.get("model_key") or ""),
            str(row.get("unit_id") or row.get("stage_id") or ""),
        )
        for row in state.get("stages") or ()
        if row.get("status") == "passed"
    }


def _cloud_gate_complete(
    state: dict[str, Any], *, include_optional_mutation: bool,
) -> bool:
    passed = _recorded_passes(state)
    for model_key in CLOUD_MODEL_ORDER:
        for stage in stages_for_model(
            model_key,
            include_optional_mutation=include_optional_mutation,
        ):
            if stage.required:
                for unit_id, _unit in _stage_units(stage):
                    if (model_key, unit_id) not in passed:
                        return False
    return True


def execute(args: argparse.Namespace, plan: dict[str, Any]) -> int:
    if not PYTHON.is_file():
        raise FileNotFoundError(f"backend interpreter is missing: {PYTHON}")
    frozen_backend = Path(args.frozen_backend).expanduser().resolve()
    target_readiness: dict[str, Any]
    if args.target == "frozen":
        target_readiness = _frozen_readiness(frozen_backend)
        if not target_readiness.get("ready"):
            raise RuntimeError(
                "frozen backend/kernel are missing or older than current source; "
                f"rebuild before packaged evaluation: {json.dumps(target_readiness)}"
            )
    else:
        authority = ROOT / "docs" / "ASTB_CAPABILITY_AUTHORITY_MANIFEST.json"
        target_readiness = {
            "target": "source",
            "ready": authority.is_file(),
            "authority_manifest_sha256": _sha256(authority) if authority.is_file() else "",
        }

    state_path, state = _load_or_create_state(args, plan)
    state["status"] = "running"
    state.pop("completed_at", None)
    state["target_readiness"] = target_readiness
    model_keys = _models_through(args.through)
    preflight = asyncio.run(_preflight_selected(model_keys))
    state["preflight"] = preflight
    state["updated_at"] = utc_now()
    write_json(state_path, state)
    blocked = [row for row in preflight if not row.get("ready")]
    if blocked:
        state["status"] = "blocked_preflight"
        state["updated_at"] = utc_now()
        write_json(state_path, state)
        print(json.dumps({"preflight_failures": blocked}, indent=2), file=sys.stderr)
        return 2

    passed = _recorded_passes(state)
    for model_key in model_keys:
        if model_key == "qwen" and not _cloud_gate_complete(
            state,
            include_optional_mutation=args.include_optional_mutation,
        ):
            state["status"] = "blocked_before_qwen"
            state["updated_at"] = utc_now()
            write_json(state_path, state)
            print(
                "Qwen remains gated until all required cloud stages pass.",
                file=sys.stderr,
            )
            return 3
        for stage in stages_for_model(
            model_key,
            include_optional_mutation=args.include_optional_mutation,
        ):
            for unit_id, unit in _stage_units(stage):
                identity = (model_key, unit_id)
                if identity in passed:
                    print(f"[{model_key}] {unit_id}: already passed; skipping")
                    continue
                seed = _stage_seed(
                    str(state["experiment_seed"]), model_key, unit_id,
                )
                command = _command_for(
                    model_key=model_key,
                    stage=unit,
                    target=args.target,
                    frozen_backend=frozen_backend,
                    timeout=args.timeout,
                    seed=seed,
                )
                record = {
                    "model_key": model_key,
                    "stage_id": stage.stage_id,
                    "unit_id": unit_id,
                    "runner": unit.runner,
                    "required": unit.required,
                    "mutation": unit.mutation,
                    "cases": list(unit.cases),
                    "sources": list(unit.sources),
                    "command": _display_command(command),
                    "started_at": utc_now(),
                    "status": "running",
                }
                state.setdefault("stages", []).append(record)
                state["updated_at"] = utc_now()
                write_json(state_path, state)
                print(f"\n[{model_key}] {unit_id}: starting", flush=True)
                completed = subprocess.run(
                    command,
                    cwd=str(BACKEND),
                    stdin=subprocess.DEVNULL,
                    check=False,
                )
                record["return_code"] = int(completed.returncode)
                record["completed_at"] = utc_now()
                record["status"] = (
                    "passed" if completed.returncode == 0 else "failed"
                )
                state["updated_at"] = utc_now()
                write_json(state_path, state)
                if completed.returncode == 0:
                    passed.add(identity)
                    continue
                if unit.required:
                    state["status"] = "failed"
                    state["failed_at"] = {
                        "model_key": model_key,
                        "stage_id": stage.stage_id,
                        "unit_id": unit_id,
                    }
                    state["updated_at"] = utc_now()
                    write_json(state_path, state)
                    print(
                        "Required case failed; resume will retry only this unit: "
                        f"{state_path}",
                        file=sys.stderr,
                    )
                    return int(completed.returncode or 1)

    state["status"] = "passed"
    state.pop("failed_at", None)
    state["completed_at"] = utc_now()
    state["updated_at"] = utc_now()
    write_json(state_path, state)
    print(f"Phase 12 sequence passed through {args.through}: {state_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually send live model requests. Omit to print the plan only.",
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--target", choices=("source", "frozen"), default="source")
    parser.add_argument("--through", choices=MODEL_ORDER, default="qwen")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--frozen-backend", default=str(DEFAULT_FROZEN_BACKEND),
    )
    parser.add_argument("--resume", default=None, help="Resume a sequence.json state")
    parser.add_argument(
        "--experiment-seed",
        default=None,
        help="Reuse this seed for paired source/frozen fixture generation.",
    )
    parser.add_argument(
        "--include-optional-mutation",
        action="store_true",
        help="Also run non-gating mutation authoring diagnostics on Solar/Gemma.",
    )
    parser.add_argument("--output-plan", default=None)
    args = parser.parse_args()
    plan = printable_plan(args)
    if args.output_plan:
        write_json(args.output_plan, plan)
    if args.preflight and not args.execute:
        rows = asyncio.run(_preflight_selected(_models_through(args.through)))
        if args.target == "frozen":
            rows.append({
                "target": "frozen",
                **_frozen_readiness(Path(args.frozen_backend)),
            })
        print(json.dumps({
            "plan_sha256": plan["plan_sha256"],
            "inference_sent": False,
            "preflight": rows,
        }, indent=2))
        return 0 if all(row.get("ready") for row in rows) else 2
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return 0
    return execute(args, plan)


if __name__ == "__main__":
    raise SystemExit(main())
