from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

import pytest


EVAL_ROOT = Path(__file__).resolve().parents[2] / "experiments" / "live-canary"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from eval_plan import (  # noqa: E402
    CLOUD_MODEL_ORDER,
    MODEL_BY_KEY,
    MODEL_ORDER,
    MODEL_ROUTES,
    MUTATION_GAP_CASES,
    frozen_kernel_path,
    plan_document,
    stages_for_model,
)
from run_canary import (  # noqa: E402
    BackendProcess,
    LiveClient,
    PreparedCase,
    async_fanout_evidence,
    capability_receipt_snapshot,
    continuation_with_provider_retry,
    grade_case,
    kernel_cell_snapshot,
    mutation_snapshot,
    pin_frozen_local_model_paths,
    prepare_case,
    provider_turn_failed,
    raw_mutation_gap_bypass_evidence,
    request_manifest_matches_surface,
    resolve_source_path,
)
from run_phase12 import (  # noqa: E402
    _backend_source_paths,
    _cloud_gate_complete,
    _kernel_source_paths,
    _stage_units,
)
from run_provider_manifest_canary import CHAT_SUBAGENT_PROMPT  # noqa: E402


def test_phase12_model_order_and_exact_routes_are_product_gates():
    assert MODEL_ORDER == ("x-preview", "grok", "solar", "gemma", "qwen")
    assert CLOUD_MODEL_ORDER == MODEL_ORDER[:-1]
    assert MODEL_ROUTES == {
        "x-preview": {
            "mode": "cloud", "provider": "opencode-zen",
            "model": "x-preview-f-free",
        },
        "grok": {"mode": "cloud", "provider": "xai", "model": "grok-4.6"},
        "solar": {
            "mode": "cloud", "provider": "hermes",
            "model": "upstage/solar-pro4:free",
        },
        "gemma": {
            "mode": "cloud", "provider": "ollama",
            "model": "gemma4:31b-cloud",
        },
        "qwen": {
            "mode": "local", "provider": "local",
            "model": "Qwen3.5-4B-BF16.gguf",
        },
    }
    assert MODEL_BY_KEY["qwen"].desktop_route_only_allowed is True
    assert "Coordinate category" in CHAT_SUBAGENT_PROMPT
    assert "children.spawn" in CHAT_SUBAGENT_PROMPT


def test_frozen_kernel_lives_in_its_isolated_onedir_runtime(tmp_path):
    backend = tmp_path / "backend" / "Variant1Backend.exe"

    assert frozen_kernel_path(backend) == (
        backend.parent / "kernel" / "Variant1Kernel.exe"
    ).resolve()


def test_frozen_freshness_ignores_generated_python_copies(tmp_path):
    backend = tmp_path / "backend"
    source = backend / "runtime.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    generated = [
        backend / "build" / "copied.py",
        backend / "dist" / "Variant1Kernel" / "_internal" / "copied.py",
        backend / ".venv" / "Lib" / "site-packages" / "copied.py",
        backend / "tests" / "test_copied.py",
        backend / "pkg" / "__pycache__" / "copied.py",
    ]
    for path in generated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 2\n", encoding="utf-8")

    assert _backend_source_paths(backend) == [source]


def test_kernel_freshness_tracks_only_the_kernel_runtime_boundary(tmp_path):
    backend = tmp_path / "backend"
    kernel_source = backend / "kernel_runtime" / "worker_main.py"
    host_source = backend / "session_catalog" / "mutation.py"
    for path in (kernel_source, host_source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")

    assert _kernel_source_paths(backend) == [kernel_source]


def test_stage_plan_requires_cloud_gates_before_qwen_and_both_mutation_shapes():
    assert MUTATION_GAP_CASES == ("GAPC", "GAPM")
    for key in CLOUD_MODEL_ORDER:
        ids = [stage.stage_id for stage in stages_for_model(key)]
        assert ids[:5] == [
            "provider-manifests",
            "static-core-off",
            "static-domains-off",
            "mutation-available-static-control",
            "mutation-off-gap-control",
        ]
    assert stages_for_model("x-preview")[-1].stage_id == "mutation-on-held-out"
    assert stages_for_model("grok")[-1].stage_id == "mutation-on-held-out"
    assert stages_for_model("gemma")[-1].stage_id == "mutation-on-held-out"
    qwen_stages = stages_for_model("qwen", include_optional_mutation=True)
    assert [stage.stage_id for stage in qwen_stages] == [
        "provider-manifests", "static-core-off", "static-domains-off",
    ]
    assert all(stage.mutation is False for stage in qwen_stages)
    static = next(
        stage for stage in stages_for_model("x-preview")
        if stage.stage_id == "static-core-off"
    )
    units = _stage_units(static)
    assert [unit_id for unit_id, _unit in units] == [
        "static-core-off/S1",
        "static-core-off/D4",
        "static-core-off/F8",
        "static-core-off/MIX",
    ]
    assert all(len(unit.cases) == 1 for _unit_id, unit in units)

    state = {"stages": []}
    assert _cloud_gate_complete(state, include_optional_mutation=False) is False
    state["stages"] = [
        {
            "model_key": key,
            "stage_id": stage.stage_id,
            "unit_id": unit_id,
            "status": "passed",
        }
        for key in CLOUD_MODEL_ORDER
        for stage in stages_for_model(key)
        if stage.required
        for unit_id, _unit in _stage_units(stage)
    ]
    assert _cloud_gate_complete(state, include_optional_mutation=False) is True


def test_plan_digest_is_stable_and_covers_optional_mutation_choice():
    first = plan_document()
    second = plan_document()
    optional = plan_document(include_optional_mutation=True)

    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["plan_sha256"] != optional["plan_sha256"]
    assert first["cloud_gate"]["qwen_runs_only_after_all_cloud_required_stages_pass"]


def test_only_cloud_fallback_terminal_text_is_a_provider_turn_failure():
    assert provider_turn_failed({
        "text": "Model error: cloud fallback chain exhausted: transient",
    }) is True
    assert provider_turn_failed({"text": "I could not complete the artifact."}) is False


def test_source_relative_model_path_is_resolved_from_project_root(monkeypatch, tmp_path):
    project = tmp_path / "VARIANT-1"
    model = project / "models" / "user" / "Qwen3.5-4B-BF16.gguf"
    model.parent.mkdir(parents=True)
    model.touch()
    monkeypatch.setattr("run_canary.ROOT", project)
    monkeypatch.chdir(project / "models")

    selected = resolve_source_path("models/user/Qwen3.5-4B-BF16.gguf")

    assert selected == model
    assert selected.is_file()


def test_frozen_eval_pins_user_owned_model_and_projector_without_copying(
    monkeypatch, tmp_path,
):
    project = tmp_path / "VARIANT-1"
    model = project / "models" / "user" / "Qwen3.5-4B-BF16.gguf"
    projector = project / "models" / "user" / "mmproj-Qwen3.5-4B-BF16.gguf"
    model.parent.mkdir(parents=True)
    model.touch()
    projector.touch()
    monkeypatch.setattr("run_canary.ROOT", project)

    pinned = pin_frozen_local_model_paths({
        "model": "models/user/Qwen3.5-4B-BF16.gguf",
        "mmproj": "models/user/mmproj-Qwen3.5-4B-BF16.gguf",
        "binary": "bin/llama-server.exe",
    })

    assert pinned["model"] == str(model)
    assert pinned["mmproj"] == str(projector)
    assert pinned["binary"] == "bin/llama-server.exe"


@pytest.mark.asyncio
async def test_cloud_continuation_retries_once_after_transient_cooldown():
    class FakeClient:
        def __init__(self):
            self.turns = 0

        async def turn(self, prompt):
            self.turns += 1
            if self.turns == 1:
                return (
                    {"text": "Model error: cloud fallback chain exhausted: timeout"},
                    [{"attempt": 1, "prompt": prompt}],
                )
            return {"text": "complete"}, [{"attempt": 2, "prompt": prompt}]

    sleeps = []

    async def sleeper(seconds):
        sleeps.append(seconds)

    outcome, messages, retries, attempts = await continuation_with_provider_retry(
        FakeClient(),
        "reuse active mutation",
        route_mode="cloud",
        cooldown_s=31,
        sleeper=sleeper,
    )

    assert outcome["text"] == "complete"
    assert [row["attempt"] for row in messages] == [1, 2]
    assert (retries, attempts, sleeps) == (1, 2, [31.0])


def test_current_case_contracts_use_native_async_and_correct_mutation_slots(tmp_path):
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    common = {
        "seed": "phase12-test-seed",
        "fixture_root": fixture_root,
        "http_base_url": "http://127.0.0.1:1",
        "desktop_title": "VARIANT-1 Eval Desktop",
    }
    fanout = prepare_case("F8", tmp_path / "f8", **common)
    depth = prepare_case("D4", tmp_path / "d4", **common)
    create = prepare_case("GAPC", tmp_path / "gapc", **common)
    mutate = prepare_case("GAPM", tmp_path / "gapm", **common)
    kernel = prepare_case("KRN", tmp_path / "kernel", **common)
    child = prepare_case("CHILD", tmp_path / "child", **common)

    assert "toolbelt.read_many" not in fanout.prompt
    assert "async" in fanout.prompt.lower()
    assert "tools.read_file.async_(path=p)" in fanout.prompt
    assert "does not qualify" in fanout.prompt
    assert "no spaces, newlines, or other separators" in fanout.prompt
    assert "Read that exact answer path" in fanout.prompt
    assert fanout.batch_calls == 8
    assert "characters after the exact fragment= prefix" in depth.prompt
    assert "no spaces, newlines, or other separators" in depth.prompt
    artifact = prepare_case("ART", tmp_path / "art", **common)
    browser = prepare_case("BRW", tmp_path / "brw-contract", **common)
    assert "artifact = artifacts.create(...)" in artifact.prompt
    assert "artifact.inspect()" in artifact.prompt
    assert "artifact.history()" in artifact.prompt
    assert "Browser work ends there" in browser.prompt
    assert "In the next Python call, switch to Build" in browser.prompt
    assert "previously absent" in browser.prompt
    assert "direct tools.browser_* seeds" in browser.prompt
    assert "browser_read again to obtain fresh references" in browser.prompt
    assert "exactly once" in browser.prompt
    assert create.mutation_shape == "create" and create.mutation_slot == "build/8"
    assert mutate.mutation_shape == "mutate" and mutate.mutation_slot == "build/1"
    assert "select Build first" in mutate.prompt
    assert "before reading either fixture" in mutate.prompt
    assert {Path(path).name for path in create.protected_parent_paths} == {
        "answer.txt", "answer-heldout.txt",
    }
    assert {Path(path).name for path in mutate.protected_parent_paths} == {
        "first.txt", "held.txt",
    }
    assert create.reset_prompt and mutate.reset_prompt
    assert kernel.followup_prompt and kernel.followup_requires_mutation is False
    assert "eval_checkpoint" in kernel.prompt
    assert "session.status()" in kernel.prompt
    assert "Use tools.read_file" in prepare_case(
        "MIX", tmp_path / "mix-route", "mix-route-seed",
        fixture_root=fixture_root,
        http_base_url="http://127.0.0.1:1",
        desktop_title="VARIANT-1 Desktop Fixture",
    ).prompt
    assert "embedding its literal again" in kernel.followup_prompt
    assert child.required_manifest_sources == ("subagent",)
    assert "that quoted sentence is the entire child task" in child.prompt
    assert "children.spawn(task=child_task)" in child.prompt
    assert "child = child.wait()" in child.prompt
    assert "result = child.inspect()" in child.prompt
    assert "reported_text" in child.prompt
    mcp = prepare_case("MCP", tmp_path / "mcp", **common)
    assert "connectors.search(query='compose_record', kind='tool')" in mcp.prompt
    assert "connector = found['mcp'][0]" in mcp.prompt
    assert "connector.schema()" in mcp.prompt
    assert "connector.invoke(arguments={'record_id':" in mcp.prompt


def test_mutation_gap_raw_python_detection_stays_in_the_external_evaluator(tmp_path):
    protected = tmp_path / "held.txt"
    prepared = PreparedCase(
        case_id="GAPM",
        family="mutation-mutate",
        workspace=tmp_path,
        prompt="mutate",
        expected=b"",
        required={},
        before={},
        protected_parent_paths=(str(protected),),
    )

    assert raw_mutation_gap_bypass_evidence(prepared, [{
        "sequence": 1,
        "source": "tools.read_file(path='held.txt')",
    }]) == []
    assert raw_mutation_gap_bypass_evidence(prepared, [{
        "sequence": 2,
        "execution_id": "cell-raw",
        "source": "open('held.txt', 'rb').read()",
    }]) == [{"sequence": 2, "execution_id": "cell-raw"}]


def test_fanout_evidence_requires_explicit_async_calls_in_one_cell():
    traces = [
        {
            "event": "kernel:capability_invocation_policy",
            "attributes": {
                "invocation_mode": "async",
                "capability_id": "read_file",
                "cell_execution_id": "cell-fanout",
                "host_concurrency_limit": 8,
            },
        }
        for _ in range(8)
    ]
    traces.append({
        "event": "kernel:capability_invocation_policy",
        "attributes": {
            "invocation_mode": "sync",
            "capability_id": "read_file",
            "cell_execution_id": "cell-other",
            "host_concurrency_limit": 8,
        },
    })

    evidence = async_fanout_evidence(
        traces, capability_id="read_file", minimum_calls=8,
    )

    assert evidence["passed"] is True
    assert evidence["peak_async_calls_in_one_cell"] == 8
    assert evidence["host_concurrency_limits"] == [8]


def test_mutation_snapshot_reads_authoritative_lifecycle_without_source(tmp_path):
    database = tmp_path / "astb.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE mutation_receipt(
                receipt_id TEXT, chat_id TEXT, draft_id TEXT, kind TEXT,
                receipt_digest TEXT, created_at REAL
            );
            CREATE TABLE mutation_draft(
                draft_id TEXT, chat_id TEXT, declared_kind TEXT, slot_id TEXT,
                alias TEXT, status TEXT, created_at REAL
            );
            CREATE TABLE astb_activation(
                chat_id TEXT, slot_id TEXT, version INTEGER, draft_id TEXT,
                active INTEGER
            );
            CREATE TABLE mutation_invocation(
                invocation_id TEXT, chat_id TEXT, slot_id TEXT, version INTEGER,
                status TEXT, created_at REAL
            );
            CREATE TABLE astb_mount_history(
                chat_id TEXT, mount_revision INTEGER, reason TEXT
            );
        """)
        conn.execute(
            "INSERT INTO mutation_receipt VALUES (?,?,?,?,?,?)",
            ("r1", "chat", "d1", "activated", "digest", 1.0),
        )
        conn.execute(
            "INSERT INTO mutation_draft VALUES (?,?,?,?,?,?,?)",
            ("d1", "chat", "create", "release/build/8", "join_pair", "probation", 1.0),
        )
        conn.execute(
            "INSERT INTO mutation_invocation VALUES (?,?,?,?,?,?)",
            ("i1", "chat", "release/build/8", 1, "ok", 2.0),
        )
        conn.execute(
            "INSERT INTO astb_mount_history VALUES (?,?,?)",
            ("chat", 2, "mutation_activate"),
        )

    snapshot = mutation_snapshot(database, "chat")

    assert snapshot["receipts"][0]["kind"] == "activated"
    assert snapshot["drafts"][0]["slot_id"].endswith("/build/8")
    assert snapshot["invocations"] == [{
        "slot_id": "release/build/8", "version": 1, "status": "ok",
    }]
    assert "source" not in json.dumps(snapshot)


def test_capability_receipts_are_read_from_authoritative_work_fabric(tmp_path):
    database = tmp_path / "work.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("""
            CREATE TABLE work_operation(
                operation_id TEXT, response_json TEXT, updated_at REAL
            )
        """)
        for index, chat_id in enumerate(("chat-a", "chat-b", "chat-a")):
            receipt = {
                "receipt_id": f"receipt-{index}",
                "status": "ok",
                "capability": {"capability_id": "read_file"},
                "attribution": {"chat_id": chat_id, "run_id": "run"},
            }
            conn.execute(
                "INSERT INTO work_operation VALUES (?,?,?)",
                (receipt["receipt_id"], json.dumps(receipt), float(index)),
            )

    receipts = capability_receipt_snapshot(database, "chat-a")

    assert [row["receipt_id"] for row in receipts] == ["receipt-0", "receipt-2"]
    assert all(row["attribution"]["chat_id"] == "chat-a" for row in receipts)


def test_canary_seeds_current_extension_mcp_database_not_retired_tools_key(tmp_path):
    process = BackendProcess(
        tmp_path,
        seed="mcp-current-path",
        enable_mcp=True,
        route=MODEL_ROUTES["x-preview"],
    )
    process.data_dir.mkdir(parents=True)

    process._prepare_tools_config()
    process._prepare_mcp_runtime()

    tools = json.loads(process.tools_config_path.read_text(encoding="utf-8"))
    database = process.data_dir / "data" / "extensions" / "extensions.sqlite3"
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            "SELECT server_id,spec_json,enabled,status FROM mcp_server_v2"
        ).fetchone()
    assert row[0] == "astb_fixture"
    assert json.loads(row[1])["transport"] == "stdio"
    assert row[2] == 1
    assert row[3] == "disconnected"


def test_live_browser_fixture_does_not_import_retired_browser_backend():
    source = (EVAL_ROOT / "run_canary.py").read_text(encoding="utf-8")
    assert "browser.backends" not in source
    assert "browser_fabric.adapters" in source


@pytest.mark.asyncio
async def test_live_mutation_toggle_uses_current_cas_revision():
    class FakeWebSocket:
        def __init__(self):
            self.sent = []
            self.responses = [
                {
                    "type": "chat:session",
                    "session": {
                        "id": "chat",
                        "runtime": {"mutation_authority_revision": 4},
                    },
                },
                {
                    "type": "chat:runtime:mutation:set:done",
                    "request_id": "",
                    "enabled": True,
                    "effective_enabled": True,
                    "authority_revision": 5,
                },
            ]

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def recv(self):
            return json.dumps(self.responses.pop(0))

    websocket = FakeWebSocket()
    client = LiveClient(websocket, timeout_s=30)

    result = await client.set_mutation("chat", True)

    toggle = websocket.sent[1]
    assert toggle["type"] == "chat:runtime:mutation:set"
    assert toggle["expected_revision"] == 4
    assert toggle["request_id"].startswith("mutation-set-")
    assert result["authority_revision"] == 5


def _manifest(route: dict[str, str], session_id: str, adapter: str) -> dict:
    return {
        "run": {"session_id": session_id, "source": "chat"},
        "surface": {
            "action_surface": "trusted-local.v1",
            "provider_tool_schema_revision": "ipython.portable.v6",
        },
        "route": {**route, "adapter": adapter},
        "tools": {
            "requested_count": 1,
            "rendered_count": 1,
            "requested": [{"name": "ipython"}],
            "rendered": [{"name": "ipython"}],
            "schema_loss": False,
        },
        "tool_protocol": {"valid": True},
        "request": {"wire_body_bytes": 100},
        "budget": {"estimated_message_tokens": 10, "estimated_schema_tokens": 5},
    }


def test_internal_observation_projection_is_toolless_on_same_astb_route():
    route = MODEL_ROUTES["grok"]
    action = _manifest(route, "chat", MODEL_BY_KEY["grok"].adapter)
    internal = json.loads(json.dumps(action))
    internal["generation"] = {"mode": "json", "response_format": "json_object"}
    internal["provenance"] = {"observation_projection_available": True}
    internal["tools"].update({
        "requested_count": 0,
        "rendered_count": 0,
        "requested": [],
        "rendered": [],
    })

    assert request_manifest_matches_surface(
        action, route=route, adapter=MODEL_BY_KEY["grok"].adapter,
    ) is True
    assert request_manifest_matches_surface(
        internal, route=route, adapter=MODEL_BY_KEY["grok"].adapter,
    ) is True
    internal["tools"]["rendered"] = [{"name": "ipython"}]
    assert request_manifest_matches_surface(
        internal, route=route, adapter=MODEL_BY_KEY["grok"].adapter,
    ) is False


def test_internal_compression_manifest_is_toolless_on_same_astb_route():
    route = MODEL_ROUTES["qwen"]
    compression = _manifest(
        route, "chat", MODEL_BY_KEY["qwen"].adapter,
    )
    compression["generation"] = {"mode": "text", "reasoning_budget": 0}
    compression["provenance"] = {"compression_receipt_available": True}
    compression["tools"].update({
        "requested_count": 0,
        "rendered_count": 0,
        "requested": [],
        "rendered": [],
    })

    assert request_manifest_matches_surface(
        compression, route=route, adapter=MODEL_BY_KEY["qwen"].adapter,
    ) is True
    compression["route"]["model"] = "wrong.gguf"
    assert request_manifest_matches_surface(
        compression, route=route, adapter=MODEL_BY_KEY["qwen"].adapter,
    ) is False


def _routing_grade(
    tmp_path: Path, model_key: str, *, connector_checks: int = 1,
) -> dict:
    workspace = tmp_path / model_key
    workspace.mkdir(parents=True)
    prepared = PreparedCase(
        case_id="DESK",
        family="desktop-route",
        workspace=workspace,
        prompt="desktop",
        expected=b"unproduced\n",
        required={},
        before={},
    )
    route = MODEL_ROUTES[model_key]
    receipts = [
        {
            "status": "ok",
            "capability": {"capability_id": capability},
            "attribution": {"chat_id": "chat", "run_id": "run"},
        }
        for capability in (
            *("connectors" for _ in range(connector_checks)),
            "computer",
        )
    ]
    return grade_case(
        prepared,
        model_key=model_key,
        route=route,
        mutation=False,
        mutation_state={
            "receipts": [], "drafts": [], "active": [], "invocations": [],
            "mount_reasons": [],
        },
        initial_session={"id": "chat"},
        final_session={"runtime": {"action_surface": "trusted-local.v1"}},
        done={"text": "routed", "cancelled": False},
        messages=[{"type": "activity", "event": "tool:start", "tool": "ipython"}],
        manifests=[_manifest(route, "chat", MODEL_BY_KEY[model_key].adapter)],
        receipts=receipts,
        traces=[
            *(
                {"event": "broker:admitted", "run_id": "run"}
                for _ in receipts
            ),
            *(
                {"event": "broker:result", "run_id": "run"}
                for _ in receipts
            ),
        ],
    )


def test_qwen_can_pass_desktop_routing_signal_but_cloud_models_need_artifact(tmp_path):
    qwen = _routing_grade(tmp_path, "qwen")
    cloud = _routing_grade(tmp_path, "x-preview")

    assert qwen["passed"] is True
    assert qwen["full_passed"] is False
    assert qwen["acceptance"] == "desktop_route_only"
    assert cloud["passed"] is False
    assert cloud["acceptance"] == "failed"

    qwen_rechecked = _routing_grade(
        tmp_path / "rechecked", "qwen", connector_checks=3,
    )
    assert qwen_rechecked["passed"] is True
    assert qwen_rechecked["acceptance"] == "desktop_route_only"


def test_kernel_grade_requires_persistent_variable_across_turns(tmp_path):
    workspace = tmp_path / "kernel"
    workspace.mkdir()
    checkpoint = "KRN-EXACT-CHECKPOINT"
    expected = (checkpoint + "\n").encode("utf-8")
    (workspace / "answer.txt").write_bytes(expected)
    prepared = PreparedCase(
        case_id="KRN",
        family="persistent-kernel-continuity",
        workspace=workspace,
        prompt="checkpoint",
        expected=expected,
        required={},
        before={},
    )
    route = MODEL_ROUTES["solar"]
    common = dict(
        model_key="solar",
        route=route,
        mutation=False,
        mutation_state={
            "receipts": [], "drafts": [], "active": [], "invocations": [],
            "mount_reasons": [],
        },
        initial_session={"id": "chat"},
        final_session={"runtime": {"action_surface": "trusted-local.v1"}},
        done={"text": "done", "cancelled": False},
        messages=[{"type": "activity", "event": "tool:start", "tool": "ipython"}],
        manifests=[_manifest(route, "chat", MODEL_BY_KEY["solar"].adapter)],
        receipts=[],
        traces=[
            {"event": "broker:admitted", "run_id": "turn-2"},
            {"event": "broker:result", "run_id": "turn-2"},
        ],
        kernel_cells=[
            {
                "sequence": 1,
                "run_id": "turn-1",
                "status": "ok",
                "source": f"eval_checkpoint = {checkpoint!r}",
                "result_text": "",
            },
            {
                "sequence": 2,
                "run_id": "turn-2",
                "status": "ok",
                "source": "print(eval_checkpoint)",
                "result_text": checkpoint,
            },
        ],
    )
    valid = grade_case(prepared, **common)
    checks = {row["name"]: row["passed"] for row in valid["checks"]}
    assert checks["persistent_kernel_variable_across_turns"] is True

    shallow_cells = [dict(common["kernel_cells"][0])]
    shallow = grade_case(prepared, **{**common, "kernel_cells": shallow_cells})
    shallow_checks = {row["name"]: row["passed"] for row in shallow["checks"]}
    assert shallow_checks["persistent_kernel_variable_across_turns"] is False


def test_kernel_cell_snapshot_is_scoped_to_the_graded_chat(tmp_path):
    database = tmp_path / "kernel-cells.sqlite3"
    with sqlite3.connect(str(database)) as conn:
        conn.execute(
            "CREATE TABLE kernel_cell_ledger ("
            "sequence INTEGER, chat_id TEXT, run_id TEXT, status TEXT, "
            "execution_count INTEGER, source_ref TEXT, result_ref TEXT)"
        )
        conn.executemany(
            "INSERT INTO kernel_cell_ledger VALUES (?,?,?,?,?,?,?)",
            [
                (1, "other-chat", "unrelated-turn", "ok", 1, "", ""),
                (2, "graded-chat", "first-turn", "ok", 1, "", ""),
                (3, "graded-chat", "second-turn", "ok", 2, "", ""),
            ],
        )

    cells = kernel_cell_snapshot(database, "graded-chat")

    assert [row["run_id"] for row in cells] == ["first-turn", "second-turn"]


def test_browser_retries_are_efficiency_evidence_when_one_real_click_succeeds(tmp_path):
    workspace = tmp_path / "browser-retry"
    workspace.mkdir()
    expected = b"BRW-EXACT\n"
    (workspace / "answer.txt").write_bytes(expected)
    audit = tmp_path / "browser-audit.jsonl"
    actions = [
        "navigate", "read", "navigate", "read", "fill", "read",
        "fill", "read", "click", "read",
    ]
    audit.write_text(
        "".join(json.dumps({"event": "browser_command", "action": action}) + "\n"
                for action in actions),
        encoding="utf-8",
    )
    prepared = PreparedCase(
        case_id="BRW",
        family="live-visible-browser-host",
        workspace=workspace,
        prompt="browser",
        expected=expected,
        required={},
        before={},
        fixture_audit_path=audit,
        fixture_event="browser_command",
        fixture_match=(("action", "click"),),
        fixture_exact_calls=1,
    )
    route = MODEL_ROUTES["gemma"]
    receipt = {
        "status": "ok",
        "capability": {"capability_id": "browser_click"},
        "attribution": {"chat_id": "chat", "run_id": "run"},
    }
    grade = grade_case(
        prepared,
        model_key="gemma",
        route=route,
        mutation=False,
        mutation_state={
            "receipts": [], "drafts": [], "active": [], "invocations": [],
            "mount_reasons": [],
        },
        initial_session={"id": "chat"},
        final_session={"runtime": {"action_surface": "trusted-local.v1"}},
        done={"text": "done", "cancelled": False},
        messages=[{"type": "activity", "event": "tool:start", "tool": "ipython"}],
        manifests=[_manifest(route, "chat", MODEL_BY_KEY["gemma"].adapter)],
        receipts=[receipt],
        traces=[
            {"event": "broker:admitted", "run_id": "run"},
            {"event": "broker:result", "run_id": "run"},
        ],
    )
    checks = {row["name"]: row["passed"] for row in grade["checks"]}
    assert checks["interactive_browser_host_protocol_exercised"] is True
    assert checks["external_fixture_called_exactly"] is True


def test_browser_case_accepts_seed_or_handle_actions_but_requires_seed_entry(tmp_path):
    prepared = prepare_case(
        "BRW",
        tmp_path / "brw",
        "browser-handle-route",
        fixture_root=tmp_path / "fixture",
        http_base_url="http://127.0.0.1:12345",
        desktop_title="unused",
    )

    assert prepared.required == {
        "browser_navigate": 1,
        "browser_read": 1,
        "apply_patch": 1,
        "read_file": 1,
    }


def test_held_out_create_grade_uses_sql_lifecycle_and_broker_invocations(tmp_path):
    workspace = tmp_path / "gapc-grade"
    workspace.mkdir()
    expected = b"FIRST\n"
    held = b"SECOND\n"
    (workspace / "answer.txt").write_bytes(expected)
    (workspace / "answer-heldout.txt").write_bytes(held)
    prepared = PreparedCase(
        case_id="GAPC",
        family="mutation-create",
        workspace=workspace,
        prompt="create",
        expected=expected,
        required={"read_file": 6, "apply_patch": 2, "mutation_invoke": 2},
        before={},
        followup_artifact="answer-heldout.txt",
        followup_expected=held,
        followup_requires_mutation=True,
        reset_prompt="reset",
        mutation_required=True,
        mutation_shape="create",
        mutation_slot="build/8",
        mutation_alias="join_pair",
    )
    route = MODEL_ROUTES["x-preview"]
    receipts = []
    for index in range(6):
        receipts.append({
            "status": "ok",
            "capability": {"capability_id": "read_file"},
            "attribution": {
                "chat_id": "chat", "run_id": f"run-{index // 3}",
                "nested_call_id": f"mcall_read_{index}",
            },
        })
    for index in range(2):
        receipts.extend((
            {
                "status": "ok",
                "capability": {"capability_id": "apply_patch"},
                "attribution": {
                    "chat_id": "chat", "run_id": f"run-{index}",
                    "nested_call_id": f"mcall_patch_{index}",
                },
            },
            {
                "status": "ok",
                "capability": {
                    "capability_id": "mutation_invoke",
                    "slot_id": "release/build/8",
                    "slot_version": 1,
                },
                "attribution": {
                    "chat_id": "chat", "run_id": f"run-{index}",
                    "nested_call_id": f"invoke-{index}",
                },
            },
        ))
    receipts.extend((
        {
            "status": "ok",
            "capability": {"capability_id": "read_file"},
            "attribution": {
                "chat_id": "chat", "run_id": "run-1",
                "nested_call_id": "mcall_read_verify",
            },
        },
        {
            "status": "error",
            "capability": {
                "capability_id": "mutation_invoke",
                "slot_id": "release/build/8",
                "slot_version": 1,
            },
            "attribution": {
                "chat_id": "chat", "run_id": "run-1",
                "nested_call_id": "invoke-verify",
            },
        },
    ))
    traces = [
        {"event": event, "run_id": "run-0"}
        for _ in receipts
        for event in ("broker:admitted", "broker:result")
    ]
    mutation_state = {
        "receipts": [
            {
                "kind": kind,
                "draft_id": (
                    "draft-rejected" if index < 4 else "draft-active"
                ),
            }
            for index, kind in enumerate((
                "proposed", "validated", "worker_contract_tested", "tested",
                "proposed", "validated", "worker_contract_tested", "tested",
                "activated", "reset",
            ))
        ],
        "drafts": [
            {
                "draft_id": "draft-rejected", "declared_kind": "create",
                "slot_id": "release/build/8", "alias": "join_pair",
                "status": "rejected",
            },
            {
                "draft_id": "draft-active", "declared_kind": "create",
                "slot_id": "release/build/8", "alias": "join_pair",
                "status": "probation",
            },
        ],
        "active": [],
        "invocations": [
            {"slot_id": "release/build/8", "version": 1, "status": "ok"},
            {"slot_id": "release/build/8", "version": 1, "status": "ok"},
            {"slot_id": "release/build/8", "version": 1, "status": "error"},
        ],
        "mount_reasons": ["mutation_activate", "mutation_reset"],
    }

    grade = grade_case(
        prepared,
        model_key="x-preview",
        route=route,
        mutation=True,
        mutation_state=mutation_state,
        initial_session={"id": "chat"},
        final_session={"runtime": {"action_surface": "trusted-local.v1"}},
        done={"text": "RESET", "cancelled": False},
        messages=[
            {"type": "activity", "event": "tool:start", "tool": "ipython"},
            {"type": "activity", "event": "tool:start", "tool": "ipython"},
            {"type": "activity", "event": "tool:start", "tool": "ipython"},
        ],
        manifests=[_manifest(route, "chat", MODEL_BY_KEY["x-preview"].adapter)],
        receipts=receipts,
        traces=traces,
    )

    assert grade["passed"] is True
    assert grade["full_passed"] is True
    assert grade["mutation_lifecycle_counts"] == {
        "proposal": 2,
        "rejected_repair_drafts": 1,
        "activation": 1,
        "invoke": 2,
        "invoke_errors": 1,
        "reset": 1,
    }
