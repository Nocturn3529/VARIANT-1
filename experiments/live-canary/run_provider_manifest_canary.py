"""Audit live post-adapter request manifests across run sources.

This is a routing/receipt canary, not a task-quality, repeatability, latency, or
SLO benchmark.  It starts one isolated VARIANT-1 backend on an explicitly selected
real provider route, triggers chat/subagent/automation/curator through their
normal product entry points, and stores only privacy-safe model-request
manifests plus structural grades.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
from typing import Any
import urllib.request
import uuid

import websockets

from run_canary import (
    BACKEND,
    MODEL_ROUTES,
    ROOT,
    RUNS,
    BackendProcess,
    LiveClient,
    discover_compatible_qwen_endpoint,
    preflight_model,
    recover_latest_xai_oauth,
    read_json,
    sha256_bytes,
    sync_newer_xai_oauth,
    utc_now,
    write_json,
)
from eval_plan import (
    EXPECTED_ADAPTER,
    MANIFEST_SOURCES,
    MODEL_BY_KEY,
    plan_document,
)


SCHEMA = "variant1.astb.provider-manifest-canary.v1"
SOURCES = MANIFEST_SOURCES
EXPECTED_PROFILE = "trusted-local.v1"
EXPECTED_SCHEMA_REVISION = "ipython.portable.v6"
EXPECTED_GRAPH = {
    "chat": "chat.ipython.v2",
    "subagent": "worker.ipython.v2",
    "automation": "worker.ipython.v2",
    "curator": "worker.ipython.v2",
}
EXPECTED_RUN_CONFIG = {
    "chat": "chat_task_default",
    "subagent": "subagent_v1",
    "automation": "automation_v1",
    "curator": "curator_v1",
}
CHAT_SUBAGENT_PROMPT = (
    "This is a provider-manifest routing canary. Use exactly one Python call "
    "in the Coordinate category with this code:\n"
    "handle = children.spawn(task='Reply exactly DONE: provider manifest "
    "canary. Do not call tools.', name='provider-manifest-canary')\n"
    "handle\n"
    "After that call succeeds, reply exactly SPAWNED."
)
class ManifestBackendProcess(BackendProcess):
    """Existing isolated canary backend pinned globally to one real route."""

    def __init__(
        self,
        run_root: Path,
        *,
        seed: str,
        route: dict[str, str],
        attach_local_port: int | None = None,
        target: str = "source",
        frozen_backend: str | Path | None = None,
    ):
        super().__init__(
            run_root,
            seed=seed,
            enable_mcp=False,
            route=route,
            local_autostart=route.get("mode") == "local",
            attach_local_port=attach_local_port,
            target=target,
            frozen_backend=frozen_backend,
        )
        self.route = dict(route)
        self.attach_local_port = int(attach_local_port or 0)

    def _prepare_config(self) -> None:
        config = read_json(ROOT / "config" / "llm_config.json")
        mode = str(self.route["mode"])
        provider = str(self.route["provider"])
        model = str(self.route["model"])
        config["mode"] = mode
        config["subagent_enabled"] = True

        cloud = config.setdefault("cloud", {})
        cloud["fallback_chain"] = []
        if mode == "cloud":
            cloud["provider"] = provider
            cloud[f"{provider}_model"] = model

        local = config.setdefault("local", {})
        local["autostart"] = mode == "local" and not self.attach_local_port
        local["prewarm"] = False
        # This machine and its llama.cpp route are deliberately single-slot.
        local["parallel"] = 1
        if self.attach_local_port:
            local["host"] = "127.0.0.1"
            local["port"] = self.attach_local_port

        surface = config.setdefault("action_surface", {})
        surface.update({
            "stop_new_kernels": False,
            "freeze_mutation": False,
        })
        config.setdefault("curator", {})["consolidate"] = True
        write_json(self.config_path, config)
        self._prepare_curator_fixture_skills()

    def _prepare_curator_fixture_skills(self) -> None:
        """Supply six unrelated isolated skills so the real curator calls a model."""
        root = self.data_dir / "config" / "skills"
        rows = (
            ("manifest-alpha", "Summarize an alpha-only fixture."),
            ("manifest-bravo", "Format a bravo-only checklist."),
            ("manifest-charlie", "Inspect a charlie-only ledger."),
            ("manifest-delta", "Normalize a delta-only label."),
            ("manifest-echo", "Describe an echo-only record."),
            ("manifest-foxtrot", "Validate a foxtrot-only marker."),
        )
        for name, description in rows:
            folder = root / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_text(
                "---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                "---\n"
                "This isolated canary skill is intentionally unrelated to every "
                "other fixture skill. Do not merge it.\n",
                encoding="utf-8",
                newline="",
            )


def _model_matches(actual: Any, expected: str) -> bool:
    value = str(actual or "").replace("\\", "/").split("/")[-1].casefold()
    wanted = str(expected or "").replace("\\", "/").split("/")[-1].casefold()
    return value == wanted


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def grade_source_manifests(
    source: str,
    manifests: list[dict[str, Any]],
    *,
    model_key: str,
    route: dict[str, str],
) -> dict[str, Any]:
    rows = [
        row for row in manifests
        if str((row.get("run") or {}).get("source") or "") == source
    ]
    row_checks: list[dict[str, Any]] = []
    for row in rows:
        run = row.get("run") or {}
        surface = row.get("surface") or {}
        physical = row.get("route") or {}
        tools = row.get("tools") or {}
        protocol = row.get("tool_protocol") or {}
        privacy = row.get("privacy") or {}
        requested = tools.get("requested") or []
        rendered = tools.get("rendered") or []
        requested_names = [str(item.get("name") or "") for item in requested]
        rendered_names = [str(item.get("name") or "") for item in rendered]
        identity_fields = (
            physical.get("provider_returned_model_id"),
            physical.get("model_revision"),
            physical.get("system_fingerprint"),
        )
        passed = all((
            row.get("schema") == "variant1.model-request-manifest.v1",
            bool(row.get("manifest_id")),
            bool(run.get("run_id")),
            bool(run.get("session_id")),
            surface.get("action_surface") == EXPECTED_PROFILE,
            surface.get("provider_tool_schema_revision") == EXPECTED_SCHEMA_REVISION,
            surface.get("graph_revision") == EXPECTED_GRAPH[source],
            surface.get("run_config_revision") == EXPECTED_RUN_CONFIG[source],
            physical.get("selected_mode") == route["mode"],
            physical.get("physical_mode") == route["mode"],
            physical.get("provider") == route["provider"],
            _model_matches(physical.get("model"), route["model"]),
            physical.get("adapter") == EXPECTED_ADAPTER[model_key],
            all(isinstance(value, str) and bool(value) for value in identity_fields),
            tools.get("requested_count") == 1,
            tools.get("rendered_count") == 1,
            requested_names == ["ipython"],
            rendered_names == ["ipython"],
            not tools.get("schema_loss"),
            protocol.get("valid") is True,
            isinstance(row.get("usage"), dict),
            privacy.get("policy") == "metadata_only.v1",
            privacy.get("prompt_text_stored") is False,
            privacy.get("tool_values_stored") is False,
            privacy.get("headers_stored") is False,
            privacy.get("url_stored") is False,
            privacy.get("exact_payload_ref") is None,
        ))
        row_checks.append({
            "manifest_id": row.get("manifest_id"),
            "passed": passed,
            "run_id": run.get("run_id"),
            "runtime_id": run.get("session_id"),
            "provider": physical.get("provider"),
            "model": physical.get("model"),
            "adapter": physical.get("adapter"),
            "action_surface": surface.get("action_surface"),
            "schema_revision": surface.get("provider_tool_schema_revision"),
            "graph_revision": surface.get("graph_revision"),
            "run_config_revision": surface.get("run_config_revision"),
            "requested_tools": requested_names,
            "rendered_tools": rendered_names,
            "schema_loss": bool(tools.get("schema_loss")),
            "protocol_valid": protocol.get("valid"),
            "usage_available": isinstance(row.get("usage"), dict),
            "provider_returned_model_id": physical.get(
                "provider_returned_model_id"
            ),
            "model_revision": physical.get("model_revision"),
            "system_fingerprint": physical.get("system_fingerprint"),
        })
    checks = [
        _check("source_manifest_present", bool(rows), len(rows)),
        _check(
            "every_request_completed_on_exact_astb_surface",
            bool(row_checks) and all(item["passed"] for item in row_checks),
            row_checks,
        ),
    ]
    return {
        "source": source,
        "passed": all(item["passed"] for item in checks),
        "manifest_count": len(rows),
        "checks": checks,
        "manifest_ids": [row.get("manifest_id") for row in rows],
    }


async def _wait_for_source(
    client: LiveClient,
    source: str,
    baseline_ids: set[str],
    *,
    timeout_s: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    latest: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        snapshot = await client.manifests()
        latest = [
            row for row in (snapshot.get("items") or [])
            if str(row.get("manifest_id") or "") not in baseline_ids
            and str((row.get("run") or {}).get("source") or "") == source
        ]
        if latest and any(isinstance(row.get("usage"), dict) for row in latest):
            return latest
        await asyncio.sleep(0.25)
    raise TimeoutError(
        f"no completed {source} provider manifest within {timeout_s:.0f}s; "
        f"observed={len(latest)}"
    )


async def _trigger_chat_and_subagent(
    client: LiveClient,
    route: dict[str, str],
    baseline_ids: set[str],
    timeout_s: float,
) -> dict[str, Any]:
    initial = await client.new_session()
    session_id = str(initial.get("id") or "")
    if not session_id:
        raise RuntimeError("new chat did not return an ID")
    await client.set_route(session_id, route)
    done, _messages = await client.turn(CHAT_SUBAGENT_PROMPT)
    if done.get("cancelled"):
        raise RuntimeError("chat/subagent trigger was cancelled")
    await _wait_for_source(
        client, "subagent", baseline_ids, timeout_s=timeout_s,
    )
    return {"session_id": session_id, "done": True}


async def _trigger_automation(
    client: LiveClient,
    timeout_s: float,
) -> dict[str, Any]:
    name = "Provider manifest canary " + uuid.uuid4().hex[:8]
    await client.send({
        "type": "automation:add",
        "name": name,
        "prompt": "Reply exactly AUTOMATION-MANIFEST-CANARY. Do not call tools.",
        "trigger": {"type": "webhook"},
        "enabled": True,
    })
    response, _ = await client.receive_until(
        lambda row: row.get("type") == "automations"
        and any(item.get("name") == name for item in row.get("items") or []),
        timeout_s=30,
    )
    item = next(
        item for item in response.get("items") or [] if item.get("name") == name
    )
    automation_id = str(item.get("id") or "")
    await client.send({"type": "automation:run", "id": automation_id})
    await client.receive_until(
        lambda row: row.get("type") == "automations",
        timeout_s=30,
    )

    deadline = time.monotonic() + timeout_s
    terminal: dict[str, Any] = {}
    while time.monotonic() < deadline:
        await client.send({
            "type": "automations:history",
            "id": automation_id,
            "limit": 10,
        })
        history, _ = await client.receive_until(
            lambda row: row.get("type") == "automations:history",
            timeout_s=20,
        )
        rows = history.get("items") or []
        if rows:
            terminal = dict(rows[0])
            break
        await asyncio.sleep(0.25)
    if not terminal:
        raise TimeoutError("automation did not reach durable history")
    if terminal.get("status") != "ok":
        raise RuntimeError(
            f"automation finished with status {terminal.get('status')!r}"
        )
    return {"automation_id": automation_id, "status": terminal.get("status")}


async def _curator_state(client: LiveClient) -> dict[str, Any]:
    await client.send({"type": "curator:get"})
    response, _ = await client.receive_until(
        lambda row: row.get("type") == "curator",
        timeout_s=20,
    )
    return dict(response.get("state") or {})


async def _trigger_curator(
    client: LiveClient,
    timeout_s: float,
) -> dict[str, Any]:
    before = await _curator_state(client)
    before_count = int(before.get("run_count") or 0)
    await client.send({"type": "curator:run", "consolidate": True})
    await client.receive_until(
        lambda row: row.get("type") == "curator:started",
        timeout_s=20,
    )
    deadline = time.monotonic() + timeout_s
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = await _curator_state(client)
        if int(state.get("run_count") or 0) > before_count:
            return {
                "run_count_before": before_count,
                "run_count_after": int(state.get("run_count") or 0),
            }
        await asyncio.sleep(0.25)
    raise TimeoutError("curator did not complete its manual consolidation pass")


async def run(args: argparse.Namespace) -> int:
    route = dict(MODEL_ROUTES[args.model])
    preflight = await preflight_model(args.model)
    if args.preflight_only:
        print(json.dumps(preflight, indent=2), flush=True)
        return 0
    oauth_handoff = {
        "recovered_before_start": False,
        "persisted_after_run": False,
    }
    if args.model == "grok":
        oauth_handoff["recovered_before_start"] = await asyncio.to_thread(
            recover_latest_xai_oauth
        )
    attached_local_port = 0
    if args.model == "qwen":
        attached_local_port = await asyncio.to_thread(
            discover_compatible_qwen_endpoint
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = RUNS / (
        f"{stamp}-{args.target}-{args.model}-provider-manifests-{uuid.uuid4().hex[:6]}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    seed = str(args.seed or secrets.token_hex(32))
    backend = ManifestBackendProcess(
        run_root,
        seed=seed,
        route=route,
        attach_local_port=attached_local_port,
        target=args.target,
        frozen_backend=args.frozen_backend,
    )
    started = utc_now()
    trigger_evidence: dict[str, Any] = {}
    manifests: list[dict[str, Any]] = []
    snapshot_meta: dict[str, Any] = {}
    error = ""
    try:
        connection = await asyncio.to_thread(backend.start)
        host = str(connection.get("host") or "127.0.0.1")
        port = int(connection["port"])
        url = f"ws://{host}:{port}/ws?token={connection['token']}"
        async with websockets.connect(
            url, open_timeout=20, max_size=4 * 1024 * 1024,
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=20)
            client = LiveClient(ws, args.timeout)
            baseline = await client.manifests()
            baseline_ids = {
                str(row.get("manifest_id") or "")
                for row in baseline.get("items") or []
            }

            requested = set(args.sources)
            if requested & {"chat", "subagent"}:
                print(f"[{args.model}] chat + subagent manifest trigger", flush=True)
                trigger_evidence["chat_subagent"] = (
                    await _trigger_chat_and_subagent(
                        client, route, baseline_ids, args.timeout,
                    )
                )
            if "automation" in requested:
                print(f"[{args.model}] automation manifest trigger", flush=True)
                trigger_evidence["automation"] = await _trigger_automation(
                    client, args.timeout,
                )
                await _wait_for_source(
                    client, "automation", baseline_ids, timeout_s=args.timeout,
                )
            if "curator" in requested:
                print(f"[{args.model}] curator manifest trigger", flush=True)
                trigger_evidence["curator"] = await _trigger_curator(
                    client, args.timeout,
                )
                await _wait_for_source(
                    client, "curator", baseline_ids, timeout_s=args.timeout,
                )

            snapshot = await client.manifests()
            manifests = [
                row for row in snapshot.get("items") or []
                if str(row.get("manifest_id") or "") not in baseline_ids
                and str((row.get("run") or {}).get("source") or "") in requested
            ]
            snapshot_meta = {
                "dropped_events": int(snapshot.get("dropped_events") or 0),
                "publish_failures": int(snapshot.get("publish_failures") or 0),
            }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"RUNNER ERROR: {error}", file=sys.stderr, flush=True)
    finally:
        await asyncio.to_thread(backend.stop)
        if args.model == "grok":
            oauth_handoff["persisted_after_run"] = await asyncio.to_thread(
                sync_newer_xai_oauth, backend.config_path,
            )

    source_results = [
        grade_source_manifests(
            source,
            manifests,
            model_key=args.model,
            route=route,
        )
        for source in args.sources
    ]
    common_checks = [
        _check("runner_completed", not error, error),
        _check(
            "manifest_bus_no_drops",
            snapshot_meta.get("dropped_events") == 0,
            snapshot_meta,
        ),
        _check(
            "manifest_bus_no_publish_failures",
            snapshot_meta.get("publish_failures") == 0,
            snapshot_meta,
        ),
    ]
    all_passed = (
        all(item["passed"] for item in common_checks)
        and len(source_results) == len(args.sources)
        and all(item["passed"] for item in source_results)
    )
    summary = {
        "schema": SCHEMA,
        "started_at": started,
        "completed_at": utc_now(),
        "scope": "single_live_call_per_source_no_repeatability_or_slo_scoring",
        "model_key": args.model,
        "target": args.target,
        "route": route,
        "preflight": preflight,
        "eval_plan_sha256": plan_document()["plan_sha256"],
        "local_provider_attachment": {
            "attached": bool(attached_local_port),
            "host": "127.0.0.1" if attached_local_port else "",
            "port": attached_local_port or None,
            "ownership": "external_existing_process" if attached_local_port else "canary_owned",
        },
        "sources_requested": list(args.sources),
        "passed": all_passed,
        "common_checks": common_checks,
        "source_results": source_results,
        "trigger_evidence": trigger_evidence,
        "xai_oauth_handoff": oauth_handoff,
        "manifest_count": len(manifests),
        "run_root": str(run_root),
        "backend_log": str(backend.log_path),
        "target_identity": backend.target_identity(),
        "fixture_seed_sha256": sha256_bytes(seed.encode("utf-8")),
    }
    write_json(run_root / "summary.json", summary)
    write_json(run_root / "manifests.json", {
        "type": "model:request_manifests",
        "privacy": "metadata_only.v1",
        "items": manifests,
        **snapshot_meta,
    })
    print(
        f"[{args.model}] provider manifests: "
        f"{'PASS' if all_passed else 'FAIL'} at {run_root}",
        flush=True,
    )
    return 0 if all_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=sorted(MODEL_ROUTES), default="grok")
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=SOURCES,
        default=list(SOURCES),
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--target", choices=("source", "frozen"), default="source")
    parser.add_argument("--frozen-backend", default=None)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--seed", default=None)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
