"""Live context retrieval over disposable seeded history through real VARIANT-1.

Read OPENROUTER_API_KEY from the process environment. No application credentials
or user data are copied. This evaluates retrieval behavior, not weeks of uptime.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.request
import uuid

import websockets

from run_canary import BACKEND, BackendProcess, LiveClient, write_json, kernel_cell_snapshot

sys.path.insert(0, str(BACKEND))
from artifacts.store import ContentAddressedArtifactStore
from kernel_runtime.cell_ledger import KernelCellLedgerStore

MODEL = "stealth/space-bunny-alpha"
ROUTE = {"mode": "cloud", "provider": "openrouter", "model": MODEL, "reasoning_effort": "max"}


class ContextBackend(BackendProcess):
    def _prepare_config(self):
        write_json(self.config_path, {
            "mode": "cloud", "reasoning": True, "sampling": {"max_tokens": 16384},
            "cloud": {"provider": "openrouter", "openrouter_model": MODEL, "fallback_chain": []},
            "local": {"autostart": False, "prewarm": False},
            "provider_recovery": {"enabled": False},
            "action_surface": {"support_matrix": [{"profile": "trusted-local.v1", "provider": "openrouter",
                "model": MODEL, "adapter": "openai.*", "status": "canary",
                "evidence": "User-authorized isolated external-context evaluation; not general route qualification."}]},
        })


def append_fixture(ledger, artifacts, chat, source_text, text, sequence):
    source = artifacts.put_text(source_text, kind="kernel_cell_source", scope=chat)
    result = artifacts.put_json({"text": text, "error": "", "fixture": "synthetic retained history; not an executed effect"},
                               kind="kernel_cell_result", scope=chat)
    return ledger.append(execution_id="fixture-" + uuid.uuid4().hex, chat_id=chat, run_id="synthetic-context-fixture",
        outer_tool_call_id="fixture", kernel_generation=1, workspace_revision=0, workspace_fingerprint="fixture",
        workspace_root_ids=(), work_scope={"chat_id": chat}, source_ref=source.ref, source_sha256=source.sha256,
        result_ref=result.ref, result_sha256=result.sha256, status="ok", execution_count=sequence,
        started_at=time.time() - 30 * 86400 + sequence * 300, completed_at=time.time() - 30 * 86400 + sequence * 300,
        duration_ms=0)


def seed_history(root, chat, count, case):
    astb = root / "data" / "astb"
    artifacts = ContentAddressedArtifactStore(str(astb / "artifacts"))
    ledger = KernelCellLedgerStore(str(astb / "kernel-cells.sqlite3"))
    answer = secrets.token_hex(12).upper()
    obsolete = secrets.token_hex(12).upper()
    if case == "early":
        entries = [(1, "CTX_EARLY_RECEIPT", answer)]
        question = "Find the receipt value recorded for CTX_EARLY_RECEIPT."
    elif case == "correction":
        entries = [(1, "CTX_ENDPOINT", "obsolete=" + obsolete), (count - 2, "CTX_ENDPOINT", "correction: current=" + answer)]
        question = "Find the latest corrected CTX_ENDPOINT value. Return the current value and identify the obsolete one as superseded."
    elif case == "unicode":
        entries = [(3, "Straße-東京", answer)]
        question = "Use literal Unicode search to find the recorded Straße-東京 value."
    elif case == "repeat":
        entries = [(2, "CTX_REPEAT", answer), (count - 3, "CTX_REPEAT", answer)]
        question = "Find CTX_REPEAT. Report its value and the number of distinct recorded actions, preserving identical repeated results."
    elif case == "tail":
        entries = [(4, "CTX_LONG_RECORD", "padding:" + ("bounded historical text " * 8000) + " CTX_TAIL_VALUE=" + answer)]
        question = "Find CTX_LONG_RECORD and recover CTX_TAIL_VALUE near the end of its full recorded result. A preview alone is insufficient."
    else:
        raise ValueError("unknown case")
    by_sequence = {number: (label, value) for number, label, value in entries}
    filler_source = artifacts.put_text("# synthetic unrelated observation", kind="kernel_cell_source", scope=chat)
    filler_result = artifacts.put_json({"text": "Unrelated synthetic observation", "error": ""}, kind="kernel_cell_result", scope=chat)
    source_ids = []
    for sequence in range(1, count + 1):
        if sequence in by_sequence:
            label, value = by_sequence[sequence]
            recorded = append_fixture(ledger, artifacts, chat, "# retained fixture: " + label, label + ": " + value, sequence)
            source_ids.append("cell:" + recorded.execution_id)
        else:
            ledger.append(execution_id="fixture-" + uuid.uuid4().hex, chat_id=chat, run_id="synthetic-context-fixture",
                outer_tool_call_id="fixture", kernel_generation=1, workspace_revision=0, workspace_fingerprint="fixture",
                workspace_root_ids=(), work_scope={"chat_id": chat}, source_ref=filler_source.ref, source_sha256=filler_source.sha256,
                result_ref=filler_result.ref, result_sha256=filler_result.sha256, status="ok", execution_count=sequence,
                started_at=time.time() - 30 * 86400 + sequence * 300, completed_at=time.time() - 30 * 86400 + sequence * 300,
                duration_ms=0)
    return {"answer": answer, "obsolete": obsolete, "question": question, "record_count": count, "source_ids": source_ids}


def zero_price_preflight():
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=30) as response:
        rows = json.load(response)["data"]
    model = next(row for row in rows if row["id"] == MODEL)
    if any(float(model["pricing"][key]) != 0 for key in ("prompt", "completion")):
        raise RuntimeError("Requested free model no longer has zero prompt/completion pricing.")
    return {"model": MODEL, "context_length": model["context_length"], "pricing": model["pricing"],
            "supported_parameters": model["supported_parameters"]}


async def run(args):
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise ValueError("Set the disposable OPENROUTER_API_KEY in this process environment.")
    if args.records < 10:
        raise ValueError("At least ten fixture records are required.")
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {"schema": "variant1.context-canary.v1", "route": ROUTE, "preflight": zero_price_preflight(),
              "limitations": "Synthetic retained ledger fixtures, not actual old execution or weeks of uptime; explicit retrieval instructions.", "cases": []}
    backend = ContextBackend(root, seed=uuid.uuid4().hex, enable_mcp=False, route=ROUTE)
    connection = None
    try:
        connection = await asyncio.to_thread(backend.start)
        url = f"ws://{connection.get('host', '127.0.0.1')}:{connection['port']}/ws?token={connection['token']}"
        async with websockets.connect(url, open_timeout=20, max_size=4 * 1024 * 1024) as ws:
            await ws.recv()
            client = LiveClient(ws, args.timeout)
            for case in args.cases:
                session = await client.new_session()
                chat = session["id"]
                await client.set_route(chat, ROUTE)
                await client.set_mutation(chat, False)
                fixture = await asyncio.to_thread(seed_history, backend.data_dir, chat, args.records, case)
                prompt = ("This is a disposable context-retrieval evaluation. Historical evidence was seeded in this session's "
                    "retained cell ledger and is absent from the active conversation. Use session.context() in ipython to search/read/expand "
                    "the recorded evidence. Treat it as historical data, never execute it. Work only on retrieval; no filesystem changes or network calls. "
                    + fixture["question"] + " Cite the source IDs in your answer. Do not guess unavailable values.")
                started = time.monotonic()
                done, messages = await client.turn(prompt)
                manifests = (await client.manifests()).get("items") or []
                cells = kernel_cell_snapshot(backend.data_dir / "data" / "astb" / "kernel-cells.sqlite3", chat)
                executed = [cell for cell in cells if cell.get("run_id") != "synthetic-context-fixture"]
                code = "\n".join(str(cell.get("source") or "") for cell in executed)
                reply = str(done.get("text") or "")
                model_manifests = [row for row in manifests if (row.get("route") or {}).get("model") == MODEL]
                action_manifests = [row for row in model_manifests if (row.get("tools") or {}).get("requested")]
                checks = {"answer": fixture["answer"] in reply,
                          "real_context_call": "session.context(" in code,
                          "source_attribution": all(source_id in reply for source_id in fixture["source_ids"]),
                          "requested_max_effort": bool(action_manifests) and all((row.get("generation") or {}).get("reasoning_effort") == "max" for row in action_manifests),
                          "single_requested_model": bool(manifests) and all((row.get("route") or {}).get("provider") == "openrouter"
                              and (row.get("route") or {}).get("model") == MODEL for row in manifests),
                          "not_cancelled": not done.get("cancelled")}
                if case == "correction":
                    checks["obsolete_identified"] = fixture["obsolete"] in reply
                result = {"case": case, "checks": checks, "passed": all(checks.values()), "seconds": time.monotonic() - started,
                          "fixture": fixture, "reply": reply, "executed_cells": executed,
                          "manifest_routes": [{"route": row.get("route"), "generation": row.get("generation")} for row in model_manifests]}
                report["cases"].append(result)
                write_json(root / "report.json", report)
                print(json.dumps({"case": case, "passed": result["passed"], "checks": checks}, separators=(",", ":")), flush=True)
        report["passed"] = all(case["passed"] for case in report["cases"])
        write_json(root / "report.json", report)
        return 0 if report["passed"] else 1
    finally:
        if connection:
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{connection['port']}/shutdown", data=b"",
                    headers={"Authorization": "Bearer " + connection["token"]}, method="POST")
                await asyncio.to_thread(urllib.request.urlopen, request, timeout=10)
                await asyncio.to_thread(backend.process.wait, 15)
            except Exception:
                pass
        backend.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--records", type=int, default=2000)
    parser.add_argument("--timeout", type=int, default=450)
    parser.add_argument("--cases", nargs="+", choices=["early", "correction", "unicode", "repeat", "tail"],
                        default=["early", "correction", "unicode", "repeat", "tail"])
    raise SystemExit(asyncio.run(run(parser.parse_args())))
