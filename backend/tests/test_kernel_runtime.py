from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import inspect
import json
import os
import re
import threading
import time
from types import SimpleNamespace

import pytest
import builtin_tools

from artifacts.store import ContentAddressedArtifactStore
from chat_sessions import build_chat_sessions
from host_run_context import make_run_context
from session_catalog.catalog import COVERED_BROKER_HANDLER_NAMES
from session_catalog.mutation import MUTATION_HANDLER
from session_catalog.profiles import (
    CHAT_GRAPH_REVISION,
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
)
from session_catalog.service import IPYTHON_PROVIDER_SPEC, CatalogService
from capability_broker import CapabilityBroker, current_capability_invocation
from artifacts.blob_service import ArtifactBlobService
from kernel_runtime.manager import (
    ExecutionAdmission,
    KernelAutoRestoreError,
    KernelContinuityError,
    KernelExecutionError,
    KernelLease,
    KernelLimits,
    KernelRuntimeManager,
    KernelUnavailable,
)
from kernel_runtime.integration import (
    _mount_selection_observation,
    bind_outer_tool_call_id,
    preserve_persistent_kernel_workspace,
    register_ipython_tool,
)
from kernel_runtime.capsules import (
    KERNEL_CONTINUITY_POLICY_SCHEMA,
    KernelCapsuleError,
    KernelCapsuleLimits,
    KernelCapsuleValue,
    KernelCheckpointPolicy,
)
from kernel_runtime.capsule_contracts import serializer_registry_document
from kernel_runtime.capabilities import (
    KERNEL_CHECKPOINT_JOB,
    KERNEL_RESTART_JOB,
    register_kernel_control_job_handlers,
)
import kernel_runtime.capsule_worker as capsule_worker_module
from kernel_runtime.capsule_worker import KernelCapsuleWorker, WORKER_CAPSULE_SCHEMA
from kernel_runtime.output import (
    OUTPUT_EVENT_SCHEMA,
    CellOutputCollector,
    OutputLimits,
)
from kernel_runtime.contracts import KernelExecutionResult
from kernel_runtime.cell_ledger import KernelCellLedgerStore
from kernel_runtime.worker_bridge import (
    CapabilityProxy,
    KernelBridgeClient,
    Variant1ConnectorMatch,
    Variant1ConnectorSearchResult,
    ToolbeltNamespace,
)
from kernel_runtime.runtime_profile import (
    DATA_RUNTIME_PROFILE,
    installed_profile_state,
    runtime_profile,
)
from kernel_runtime.repl_worker import ReplWorker, _EventTextStream
from session_runtime import (
    RuntimeIdentity,
    SessionRuntimeRegistry,
    SessionRuntimeRepository,
)
from run_context import Variant1RunContext, bind_run_context
from tools import Tool, ToolError, ToolRegistry
from tool_core import ToolProjectionResult
from work_fabric.jobs import RetryJob


def test_repl_output_chunk_boundary_preserves_utf8_codepoint():
    raw = ("a" * 65_535 + "🙂tail").encode("utf-8")
    size = _EventTextStream._utf8_prefix_size(raw, 65_536)
    assert size == 65_535
    assert raw[:size].decode("utf-8") == "a" * 65_535


@pytest.mark.asyncio
async def test_repl_control_timeout_hard_closes_generation(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    record = runtimes.ensure_runtime("chat-control-timeout", is_new=True)
    lease = KernelLease(
        manager=manager,
        chat_id="chat-control-timeout",
        generation=1,
        identity=record.identity,
        generation_root=os.path.join(manager.root, "control-timeout"),
        workspace_root=manager.app_root,
        workspace_fingerprint="test",
        workspace_roots=(manager.app_root,),
        workspace_revision=0,
    )

    class Transport:
        def send(self, *_args, **_kwargs):
            pass

        async def receive(self, *, timeout_s=None):
            await asyncio.sleep(float(timeout_s or 0))
            raise asyncio.TimeoutError

    class Process:
        def poll(self):
            return None

    closed = []

    async def close(*, reason="", hard=False):
        closed.append((reason, hard))
        lease._closed = True
        lease.state = "absent"

    lease.transport = Transport()
    lease.process = Process()
    lease.state = "ready"
    lease.close = close

    with pytest.raises(KernelUnavailable, match="timed out"):
        await lease._repl_control("capsule_restore", timeout_s=0.02)

    assert closed == [("repl_capsule_restore_timeout", True)]
    assert lease._closed is True


def test_direct_seed_describe_aliases_local_documentation():
    bridge = SimpleNamespace(invoke=lambda descriptor, arguments: {
        "descriptor": descriptor,
        "arguments": arguments,
    })
    proxy = CapabilityProxy({
        "alias": "read_file",
        "signature": "read_file(path)",
        "description": "Read one file.",
        "effect_class": "read",
        "params": {"path": {"type": "string", "required": True}},
    }, bridge)

    assert proxy.describe() == proxy.documentation()
    assert proxy.describe()["name"] == "tools.read_file"


def test_mutation_mount_card_uses_current_recovery_api():
    text = _mount_selection_observation(
        {
            "category_id": "operate",
            "mount_card": "Operate mounted.",
            "mutation": {
                "authority": {"effective_write_enabled": True},
            },
        },
        fallback_category="operate",
    )

    assert "Mutation: on." in text
    assert "toolbelt.last_failure()" in text
    assert "toolbelt.mutate(" in text
    assert "toolbelt.synthesize(helper, invoke={...})" in text
    assert "Optional authoring paths" not in text


def test_bridge_retains_unsuccessful_action_result_as_typed_failure():
    bridge = KernelBridgeClient(
        host="127.0.0.1",
        port=1,
        secret=b"test-secret",
        nonce="test-nonce",
        generation=1,
        kernel=SimpleNamespace(shell=SimpleNamespace(events=None)),
    )
    bridge._remember_unsuccessful_result(
        {
            "alias": "click",
            "qualified_alias": "computer.click",
            "namespace": "computer",
            "fixed_arguments": {"operation": "click"},
        },
        {"operation": "click", "x": 10, "y": 20},
        {
            "action_status": "unknown_effect",
            "action_error": "the target did not expose a verifiable state change",
        },
    )

    failure = bridge.last_failure()
    assert failure is not None
    assert failure.target == "computer.click"
    assert failure.code == "unknown_effect"
    assert failure.arguments == {"x": 10, "y": 20}


def test_connector_top_match_is_mapping_and_direct_bound_proxy():
    calls = []

    class Handle:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    handle = Handle()
    schema = {"name": "environment_exec", "required": ["command"]}
    found = Variant1ConnectorSearchResult({
        "mcp": [handle],
        "plugins": [],
        "top_match": {"handle": handle, "schema": schema},
    })

    assert isinstance(found.top_match, Variant1ConnectorMatch)
    assert found["top_match"]["handle"] is handle
    assert found.top_match.handle is handle
    assert found.top_match.schema() == schema
    assert found.top_match.invoke({"command": "pwd"}, conclude=True) == {
        "ok": True,
    }
    assert calls == [{
        "arguments": {"command": "pwd"},
        "conclude": True,
    }]


def test_toolbelt_promotes_working_python_into_atomic_session_tool():
    calls = []

    class Control:
        def synthesize(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "slot_id": kwargs["slot"]}

    namespace = ToolbeltNamespace({
        "selected_category_id": "build",
        "category_options": [{
            "category_id": "build",
            "vacant_slot_ids": ["build/8", "build/9"],
        }],
    })
    namespace._control_object = Control()
    namespace._control_methods = ("synthesize",)

    def repeat_text(value: str, count: int = 1):
        """Repeat text without a broker dependency."""

        return value * count

    result = namespace.promote_helper(
        repeat_text,
        tests=[{
            "arguments": {"value": "ab", "count": 2},
            "expected": "abab",
        }],
    )

    assert result == {"ok": True, "slot_id": "build/8"}
    assert "promote_helper" in namespace.methods()
    assert namespace.documentation("promote_helper")["effect_class"] == "write"
    payload = calls[0]
    assert payload["slot"] == "build/8"
    assert payload["alias"] == "repeat_text"
    assert "capabilities" not in payload
    assert "effects" not in payload
    assert payload["schema"] == {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {
            "value": {"type": "string"},
            "count": {"type": "integer", "default": 1},
        },
    }
    assert "def repeat_text(value, count=1):" in payload["source"]
    assert "def run(arguments):" in payload["source"]


def test_toolbelt_runs_callable_assertions_before_json_control_boundary():
    calls = []
    assertions = []

    class Control:
        def propose_activate(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    namespace = ToolbeltNamespace({
        "selected_category_id": "build",
        "category_options": [{
            "category_id": "build",
            "vacant_slot_ids": ["build/8"],
        }],
    })
    namespace._control_object = Control()
    namespace._control_methods = ("propose_activate", "synthesize")

    def uppercase(value: str):
        return value.upper()

    def test_uppercase():
        assert uppercase("ready") == "READY"
        assertions.append("ran")

    namespace.synthesize(uppercase, tests=[test_uppercase])

    assert assertions == ["ran"]
    assert "tests" not in calls[0]
    json.dumps(calls[0], allow_nan=False)


def test_toolbelt_accepts_explicit_run_helper_with_supplied_schema():
    calls = []

    class Control:
        def propose_activate(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    namespace = ToolbeltNamespace({
        "selected_category_id": "build",
        "category_options": [{
            "category_id": "build",
            "vacant_slot_ids": ["build/8"],
        }],
    })
    namespace._control_object = Control()
    namespace._control_methods = ("propose_activate", "synthesize")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    }

    def run(arguments):
        return arguments["value"].upper()

    namespace.synthesize(
        run,
        alias="uppercase",
        schema=schema,
        tests=[{"arguments": {"value": "ok"}, "expected": "OK"}],
    )

    assert calls[0]["schema"] == schema
    assert calls[0]["alias"] == "uppercase"
    assert calls[0]["source"].count("def run(") == 1
    json.dumps(calls[0], allow_nan=False)


def test_toolbelt_describe_covers_advertised_local_mutation_methods():
    namespace = ToolbeltNamespace({
        "catalog_index": [{
            "alias": "read_file",
            "qualified_alias": "tools.read_file",
            "description": "Read one file.",
        }],
    })
    namespace._control_methods = ("propose_activate", "synthesize")

    synthesize = namespace.describe("synthesize")
    compatibility = namespace.describe("promote-helper")
    seed = namespace.describe("read-file")

    assert synthesize["method"] == "synthesize"
    assert synthesize["signature"].startswith("synthesize(helper")
    assert compatibility["method"] == "promote_helper"
    assert seed["qualified_alias"] == "tools.read_file"
    with pytest.raises(KeyError):
        namespace.describe("not_available")


def test_toolbelt_helper_promotion_requires_current_vacancy():
    namespace = ToolbeltNamespace({
        "selected_category_id": "build",
        "category_options": [{
            "category_id": "build",
            "vacant_slot_ids": [],
        }],
    })
    namespace._control_object = SimpleNamespace(synthesize=lambda **_kwargs: {})
    namespace._control_methods = ("synthesize",)

    def identity(value: str):
        return value

    with pytest.raises(ValueError, match="no available vacancy"):
        namespace.promote_helper(
            identity,
            tests=[{"arguments": {"value": "x"}, "expected": "x"}],
        )


def test_toolbelt_packages_referenced_module_imports_with_promoted_helper():
    calls = []

    class Control:
        def synthesize(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    namespace = ToolbeltNamespace({
        "selected_category_id": "build",
        "category_options": [{
            "category_id": "build",
            "vacant_slot_ids": ["build/8"],
        }],
    })
    namespace._control_object = Control()
    namespace._control_methods = ("synthesize",)

    def filename(path: str):
        return os.path.basename(path)

    namespace.synthesize(filename, invoke={"path": "folder/file.txt"})

    assert "import os as os" in calls[0]["source"]
    assert calls[0]["invoke"] == {"path": "folder/file.txt"}


def test_terminal_execution_control_projects_bounded_confirmation():
    collector = CellOutputCollector(limits=OutputLimits())

    collector.accept_execution_control({
        "schema": "variant1.kernel-execution-control.v1",
        "terminate": True,
        "observation": '{"ok":true,"result":"saved"}',
    })

    assert collector.result.terminate_requested is True
    assert collector.result.text() == '{"ok":true,"result":"saved"}'


def test_namespace_footer_is_exception_only():
    collector = CellOutputCollector(limits=OutputLimits())
    collector.accept_namespace_delta({
        "schema": "variant1.kernel-namespace-delta.v1",
        "updated": ["ordinary_value"],
        "retained": ["prior_value"],
        "updated_omitted": 0,
        "retained_omitted": 0,
    })
    assert collector.result.namespace_footer() == ""

    collector.accept_namespace_delta({
        "schema": "variant1.kernel-namespace-delta.v1",
        "updated": ["visible_value"],
        "retained": [],
        "updated_omitted": 3,
        "retained_omitted": 0,
    })
    assert collector.result.namespace_footer() == (
        "[Session state] Updated: visible_value; 3 updated name(s) omitted"
    )

    successful = KernelExecutionResult(
        execution_id="cell-success",
        chat_id="chat-success",
        generation=1,
        status="ok",
        output=collector.result,
    )
    assert "[Session state]" not in successful.render()

    restarted = KernelExecutionResult(
        execution_id="cell-restarted",
        chat_id="chat-restarted",
        generation=2,
        status="ok",
        output=collector.result,
        hard_restarted=True,
    )
    assert "[Kernel state] Runtime restarted" in restarted.render()
    assert "[Session state] Updated: visible_value" in restarted.render()


def test_default_model_cell_has_no_elapsed_wall_clock_deadline():
    limits = KernelLimits()
    assert limits.cell_timeout_s == 0.0


def test_serializer_registry_projects_the_validated_kernel_profile():
    core = serializer_registry_document(runtime_profile().packages)
    data = serializer_registry_document(
        runtime_profile(DATA_RUNTIME_PROFILE).packages
    )
    core_by_id = {row["id"]: row for row in core["serializers"]}
    data_by_id = {row["id"]: row for row in data["serializers"]}
    assert core_by_id["json.strict.v1"]["available"] is True
    assert core_by_id["pandas.arrow.v1"]["available"] is False
    assert data_by_id["pandas.arrow.v1"]["available"] is True
    assert data_by_id["pandas.arrow.v1"]["packages"] == {
        "pandas": "3.0.5",
        "pyarrow": "25.0.0",
    }


def test_namespace_key_fences_mutation_authority_revisions():
    document = {
        "schema": "variant1.astb.namespace.v1",
        "catalog_release_id": "catalog-a",
        "mount_revision": 3,
        "session": {
            "overlay_revision": 7,
            "mutation_authority_revision": 1,
        },
    }
    first = KernelLease._document_key(document)
    document["session"]["mutation_authority_revision"] = 2
    second = KernelLease._document_key(document)
    assert first == ("catalog-a", 3, 7, 1)
    assert second == ("catalog-a", 3, 7, 2)
    assert first != second


@pytest.mark.asyncio
async def test_bridge_origin_is_immutable_for_tasks_and_unbound_threads_reject():
    admission = {
        "schema": "variant1.kernel-execution-admission.v1",
        "execution_id": "cell-a",
        "outer_tool_call_id": "outer-a",
        "generation": 7,
    }
    kernel = SimpleNamespace(current_admission=lambda: dict(admission))
    bridge = KernelBridgeClient(
        host="127.0.0.1",
        port=1,
        secret=b"test-secret",
        nonce="test-nonce",
        generation=7,
        kernel=kernel,
    )

    bridge.bind_execution_origin()
    gate = asyncio.Event()

    async def delayed_origin():
        await gate.wait()
        return bridge._execution()

    delayed = asyncio.create_task(delayed_origin())
    await asyncio.sleep(0)
    admission.update({
        "execution_id": "cell-b",
        "outer_tool_call_id": "outer-b",
    })
    assert bridge._execution() == ("cell-a", "outer-a")
    bridge.reset_execution_origin()
    assert bridge._execution() == ("", "")
    gate.set()
    assert await delayed == ("cell-a", "outer-a")

    from_thread = []
    thread = threading.Thread(target=lambda: from_thread.append(bridge._execution()))
    thread.start()
    thread.join(timeout=2)
    assert from_thread == [("", "")]


def test_typed_output_event_count_is_bounded_before_projection():
    collector = CellOutputCollector(
        limits=OutputLimits(
            max_message_bytes=128,
            max_cell_bytes=256,
            max_events=2,
            max_mime_bytes=128,
        )
    )
    for index in range(3):
        collector.accept_event({
            "type": "stdout",
            "text": f"event-{index}\n",
        })

    evidence = collector.result.evidence()
    assert evidence["schema"] == OUTPUT_EVENT_SCHEMA
    assert [event["sequence"] for event in evidence["events"]] == [1, 2]
    assert evidence["bounds"]["admitted_events"] == 2
    assert evidence["bounds"]["dropped_events"] == 1
    assert "event-2" not in collector.result.text()


def test_pinned_data_runtime_profile_has_one_exact_dependency_digest():
    core = runtime_profile("core.v1")
    data = runtime_profile(DATA_RUNTIME_PROFILE)

    assert installed_profile_state(core)["compatible"] is True
    assert data.packages == {
        **core.packages,
        "duckdb": "1.5.5",
        "matplotlib": "3.11.1",
        "numpy": "2.5.1",
        "pandas": "3.0.5",
        "plotly": "6.9.0",
        "pyarrow": "25.0.0",
        "safetensors": "0.8.0",
    }
    assert data.python_major_minor == "3.13"
    assert len(data.digest) == 64
    assert data.digest == runtime_profile(DATA_RUNTIME_PROFILE).digest


@pytest.mark.asyncio
async def test_data_runtime_profile_is_enforced_before_model_code(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    profile = runtime_profile(DATA_RUNTIME_PROFILE)
    state = installed_profile_state(profile)
    manager.runtime_profile = profile
    runtimes.ensure_runtime("chat-data-profile", is_new=True)
    try:
        if not state["compatible"]:
            with pytest.raises(KernelUnavailable) as unavailable:
                await manager.execute(
                    chat_id="chat-data-profile",
                    code="raise AssertionError('model code must not run')",
                    run_id="run-data-profile-mismatch",
                    outer_tool_call_id="outer-data-profile-mismatch",
                )
            assert "runtime profile dependency mismatch" in str(unavailable.value)
        else:
            result = await manager.execute(
                chat_id="chat-data-profile",
                code=(
                    "import duckdb, matplotlib, numpy, pandas, plotly, pyarrow\n"
                    "print('data-profile-ready')"
                ),
                run_id="run-data-profile-ready",
                outer_tool_call_id="outer-data-profile-ready",
                timeout_s=15,
            )
            assert result.ok, result.to_dict()
            assert "data-profile-ready" in result.output.text()
    finally:
        await manager.shutdown()


@pytest.fixture
def kernel_stack(tmp_path):
    registry = ToolRegistry()

    async def read_file(args):
        return str(args["path"])

    registry.register(Tool(
        "read_file",
        "Return a deterministic test value.",
        read_file,
        category="read",
        params={"path": {"type": "string", "required": True}},
        effect_class="read",
        may_return_secrets=False,
    ))
    repository = SessionRuntimeRepository(str(tmp_path / "astb.sqlite3"))
    holder = {}
    runtimes = SessionRuntimeRegistry(
        repository,
        identity_factory=lambda _chat_id, _is_new: holder["service"].identity(
            environment_digest="kernel-test"
        ),
    )
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=runtimes,
        enabled_resolver=lambda: {
            "read_file", MUTATION_HANDLER, "remote_handle_dispatch",
            *COVERED_BROKER_HANDLER_NAMES,
        },
        artifact_store=artifacts,
    )
    service = CatalogService(
        database_path=str(tmp_path / "astb.sqlite3"),
        artifact_store=artifacts,
        registry=registry,
        broker=broker,
        runtime_registry=runtimes,
        enabled_resolver=broker.enabled_resolver,
        mutation_allowed=lambda: True,
    )
    holder["service"] = service
    manager = KernelRuntimeManager(
        registry=runtimes,
        broker=broker,
        artifact_store=artifacts,
        catalog_service=service,
        root=str(tmp_path / "kernels with space Ω"),
        instance_id="test-instance",
        app_root=str(tmp_path),
        worker_executable=os.environ.get("VARIANT1_TEST_KERNEL_EXE", ""),
        limits=KernelLimits(
            boot_timeout_s=30,
            cell_timeout_s=15,
            interrupt_grace_s=1.0,
            shutdown_grace_s=2.0,
            max_live_kernels=3,
            max_boot_concurrency=1,
            idle_lifetime_s=3600,
            absolute_lifetime_s=3600,
            process_memory_bytes=1_000_000_000,
            job_memory_bytes=1_250_000_000,
            output=OutputLimits(
                max_message_bytes=4096,
                max_cell_bytes=8192,
                max_events=64,
                max_mime_bytes=4096,
                max_artifact_bytes=1024 * 1024,
            ),
        ),
    )
    yield manager, runtimes, artifacts


@pytest.mark.asyncio
async def test_large_capability_response_remains_in_the_same_persistent_worker(kernel_stack):
    manager, runtimes, _ = kernel_stack
    calls = []
    text = 'Zażółć 東京 🙂' * 150_000

    async def large_result(args):
        calls.append(args['path'])
        return {'text': text, 'tail': {'value': 41}}

    manager.broker.registry.get('read_file').handler = large_result
    runtimes.ensure_runtime('chat-large-response', is_new=True)
    manager.catalog_service.select('chat-large-response', 'build')
    try:
        first = await manager.execute(chat_id='chat-large-response', code="retained = tools.read_file(path='large')\nprint(len(retained['text']))", run_id='large-1', outer_tool_call_id='large-call-1')
        second = await manager.execute(chat_id='chat-large-response', code="retained['tail']['value'] += 1\nprint(retained['tail']['value'], retained['text'][-1])", run_id='large-2', outer_tool_call_id='large-call-2')
        assert first.ok and second.ok, (first.to_dict(), second.to_dict())
        assert str(len(text)) in first.output.text()
        assert '42 🙂' in second.output.text()
        assert first.generation == second.generation
        assert calls == ['large']
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_read_file_complete_value_survives_bounded_display_and_compiles(kernel_stack):
    manager, runtimes, _ = kernel_stack
    source_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "r3-phase2-client-reconstructed.txt"
    )
    with open(source_path, "rb") as source_handle:
        source_bytes = source_handle.read()
    source = source_bytes.decode("utf-8")
    assert len(source_bytes) == 12397
    assert len(source.splitlines()) == 292
    assert hashlib.sha256(source_bytes).hexdigest() == "a1cfc490305fdff6149e7e1eec793c2cfd568dbda6d145dc848879f4356a8a53"
    read_tool = manager.broker.registry.get("read_file")
    read_definition = next(row for row in builtin_tools._defs() if row[0] == "read_file")
    read_tool.handler = builtin_tools.read_file
    read_tool.params = read_definition[3]
    read_tool.schema_revision = "variant1.read-file.v2"
    manager.catalog_service.reconcile_registry()
    runtimes.ensure_runtime("chat-complete-read", is_new=True)
    manager.catalog_service.select("chat-complete-read", "build")
    try:
        first = await manager.execute(
            chat_id="chat-complete-read",
            code=(
                f"path={source_path!r}\n"
                "x=tools.read_file(path=path)\n"
                "y=tools.read_file(path=path, offset=None, limit=None)\n"
                "print(x)\n"
                "compile(x,path,'exec')\n"
                "compile(y,path,'exec')\n"
            ),
            run_id="complete-read-1",
            outer_tool_call_id="complete-read-call-1",
        )
        second = await manager.execute(
            chat_id="chat-complete-read",
            code="import hashlib\nprint(tools.read_file.documentation()['signature'], type(x).__name__, type(y).__name__, x==y, len(x.encode()), len(x.splitlines()), hashlib.sha256(x.encode()).hexdigest())",
            run_id="complete-read-2",
            outer_tool_call_id="complete-read-call-2",
        )
        assert first.ok and second.ok, (first.to_dict(), second.to_dict())
        assert str(len(source_bytes)) in second.output.text()
        assert "292" in second.output.text()
        assert hashlib.sha256(source_bytes).hexdigest() in second.output.text()
        assert "offset=None" in second.output.text() and "limit=None" in second.output.text()
        assert "str str True" in second.output.text()
        assert first.output.truncated is True
        window = await manager.execute(
            chat_id="chat-complete-read",
            code="w=tools.read_file(path=path, offset=2, limit=2)\nprint(type(w).__name__, w.offset, w.end, w.complete_file, w.next_offset)",
            run_id="complete-read-window", outer_tool_call_id="complete-read-window-call",
        )
        assert window.ok, window.to_dict()
        assert "Variant1FileReadResult 2 3 False 4" in window.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_read_file_cas_result_is_resolvable_through_public_artifacts_facade(kernel_stack, tmp_path, monkeypatch):
    manager, runtimes, store = kernel_stack
    path = tmp_path / "cas-source.txt"
    content = ("complete via public facade\n" * 20).encode()
    path.write_bytes(content)
    manager.broker.registry.get("read_file").handler = builtin_tools.read_file
    blob = ArtifactBlobService(store)
    async def artifact_handler(args):
        ctx = current_capability_invocation()
        assert args.get("operation") == "read_bytes"
        return blob.read_bytes(args["ref"], scope=ctx.chat_id, max_bytes=int(args.get("max_bytes") or 4096))
    manager.broker.registry.get("artifacts").handler = artifact_handler
    monkeypatch.setattr(builtin_tools, "MAX_COMPLETE_PROGRAMMATIC_BYTES", 32)
    chat = "chat-cas-read"
    runtimes.ensure_runtime(chat, is_new=True)
    manager.catalog_service.select(chat, "build")
    try:
        result = await manager.execute(
            chat_id=chat,
            code=(f"p={str(path)!r}\n"
                  "r=tools.read_file(path=p)\n"
                  "payload=artifacts.read_bytes(ref=r.artifact_ref, max_bytes=4096)\n"
                  "import hashlib\nprint(type(r).__name__, len(payload), hashlib.sha256(payload).hexdigest())"),
            run_id="cas-read", outer_tool_call_id="cas-read-call",
        )
        assert result.ok, result.to_dict()
        assert "Variant1FileReadResult" in result.output.text()
        assert str(len(content)) in result.output.text()
        assert hashlib.sha256(content).hexdigest() in result.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_read_file_production_caps_cross_stream_frame_and_cas_boundaries(kernel_stack, tmp_path):
    manager, runtimes, store = kernel_stack
    inline_path = tmp_path / "escaping-heavy.txt"
    inline_bytes = ((b'"\\\\payload\\n' * 70_000)[:900_000])
    cas_path = tmp_path / "over-inline.txt"
    cas_bytes = (b"cas-complete-payload\n" * 60_000)[:1_100_000]
    inline_path.write_bytes(inline_bytes)
    cas_path.write_bytes(cas_bytes)
    assert len(inline_bytes) < builtin_tools.MAX_COMPLETE_PROGRAMMATIC_BYTES
    assert len(json.dumps(inline_bytes.decode()).encode()) > 1_048_576
    assert len(cas_bytes) > builtin_tools.MAX_COMPLETE_PROGRAMMATIC_BYTES
    read_tool = manager.broker.registry.get("read_file")
    read_definition = next(row for row in builtin_tools._defs() if row[0] == "read_file")
    read_tool.handler = builtin_tools.read_file
    read_tool.params = read_definition[3]
    read_tool.schema_revision = "variant1.read-file.v2"
    blob = ArtifactBlobService(store)
    async def artifact_handler(args):
        ctx = current_capability_invocation()
        assert args.get("operation") == "read_bytes"
        return blob.read_bytes(args["ref"], scope=ctx.chat_id, max_bytes=int(args.get("max_bytes") or 4_194_304))
    manager.broker.registry.get("artifacts").handler = artifact_handler
    manager.catalog_service.reconcile_registry()
    chat = "chat-production-read-boundaries"
    runtimes.ensure_runtime(chat, is_new=True)
    manager.catalog_service.select(chat, "build")
    try:
        first = await manager.execute(
            chat_id=chat,
            code=(f"inline_path={str(inline_path)!r}\n"
                  "inline_value=tools.read_file(path=inline_path)\n"
                  "import hashlib\nprint(type(inline_value).__name__, len(inline_value.encode()), hashlib.sha256(inline_value.encode()).hexdigest())"),
            run_id="production-inline", outer_tool_call_id="production-inline-call", timeout_s=15,
        )
        second = await manager.execute(
            chat_id=chat,
            code=(f"cas_path={str(cas_path)!r}\n"
                  "cas_result=tools.read_file(path=cas_path)\n"
                  "cas_value=artifacts.read_bytes(ref=cas_result.artifact_ref, max_bytes=2000000)\n"
                  "print(type(cas_result).__name__, len(cas_value), hashlib.sha256(cas_value).hexdigest())"),
            run_id="production-cas", outer_tool_call_id="production-cas-call", timeout_s=15,
        )
        third = await manager.execute(
            chat_id=chat,
            code="print(len(inline_value.encode()), len(cas_value), hashlib.sha256(inline_value.encode()).hexdigest(), hashlib.sha256(cas_value).hexdigest())",
            run_id="production-retained", outer_tool_call_id="production-retained-call", timeout_s=15,
        )
        assert first.ok and second.ok and third.ok, (first.to_dict(), second.to_dict(), third.to_dict())
        assert first.generation == second.generation == third.generation == 1
        assert str(len(inline_bytes)) in first.output.text()
        assert hashlib.sha256(inline_bytes).hexdigest() in first.output.text()
        assert "Variant1FileReadResult" in second.output.text()
        assert str(len(cas_bytes)) in second.output.text()
        assert hashlib.sha256(cas_bytes).hexdigest() in second.output.text()
        assert str(len(inline_bytes)) in third.output.text() and str(len(cas_bytes)) in third.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object process ceiling")
@pytest.mark.parametrize("max_processes", [None, 8])
@pytest.mark.asyncio
async def test_kernel_child_process_ceiling_is_opt_in_and_shutdown_owns_children(kernel_stack, max_processes):
    import psutil

    manager, runtimes, _ = kernel_stack
    defaults = KernelLimits()
    manager.limits = replace(manager.limits, max_processes=defaults.max_processes if max_processes is None else max_processes,
                            process_memory_bytes=defaults.process_memory_bytes,
                            job_memory_bytes=defaults.job_memory_bytes, cpu_percent=defaults.cpu_percent)
    runtimes.ensure_runtime('chat-process-ceiling', is_new=True)
    try:
        result = await manager.execute(chat_id='chat-process-ceiling', code='''
import subprocess, sys, json
children = []
failure = None
for _ in range(12):
    try:
        children.append(subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', 'import time; time.sleep(60)'],
                                         creationflags=subprocess.CREATE_NO_WINDOW))
    except OSError as exc:
        failure = getattr(exc, 'winerror', None)
        break
print(json.dumps({'pids': [p.pid for p in children], 'failure': failure}))
''', run_id='process-ceiling', outer_tool_call_id='process-ceiling-call')
        assert result.ok, result.to_dict()
        receipt = json.loads(result.output.text().strip())
        if max_processes is None:
            assert len(receipt['pids']) == 12 and receipt['failure'] is None
        else:
            # Successful CreateProcess returns include launchers that may
            # already have exited; the limit counts concurrently live members.
            assert 0 < len(receipt['pids']) < 12 and receipt['failure'] == 1816
        children = [psutil.Process(pid) for pid in receipt['pids'] if psutil.pid_exists(pid)]
    finally:
        await manager.shutdown()
    _, alive = psutil.wait_procs(children, timeout=3)
    assert not alive


@pytest.mark.asyncio
async def test_same_headless_chrome_launch_inside_and_outside_persistent_kernel(kernel_stack, tmp_path, capsys):
    chrome = os.environ.get("VARIANT1_TEST_CHROME_EXE", "")
    if not chrome or not os.path.isfile(chrome):
        pytest.skip("set VARIANT1_TEST_CHROME_EXE for installed Chrome acceptance")
    import ast
    manager, runtimes, _ = kernel_stack
    defaults = KernelLimits()
    manager.limits = replace(manager.limits, cell_timeout_s=0,
        max_processes=defaults.max_processes, process_memory_bytes=defaults.process_memory_bytes,
        job_memory_bytes=defaults.job_memory_bytes, cpu_percent=defaults.cpu_percent)
    source = f'''
import json, psutil
from playwright.async_api import async_playwright
async with async_playwright() as pw:
    browser = await pw.chromium.launch(executable_path={chrome!r}, headless=True)
    try:
        contexts = [await browser.new_context() for _ in range(3)]
        for index, context in enumerate(contexts):
            page = await context.new_page()
            await page.set_content('<title>kernel-child-check</title><p>owned browser</p>')
            assert await page.title() == 'kernel-child-check'
        print(json.dumps({{'ok':True,'contexts':len(contexts),'child_processes':len(psutil.Process().children(recursive=True))}}))
    finally:
        await browser.close()
'''
    await eval(compile(source, "<outside-kernel-chrome>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), {})
    outside = json.loads(capsys.readouterr().out.strip())
    runtimes.ensure_runtime("chrome-acceptance", is_new=True)
    try:
        result = await manager.execute(chat_id="chrome-acceptance", code=source,
            run_id="chrome-acceptance", outer_tool_call_id="chrome-acceptance")
        assert result.ok, result.to_dict()
        inside = json.loads(result.output.text().strip())
        assert outside["ok"] and inside["ok"]
        assert outside["contexts"] == inside["contexts"] == 3
        assert inside["child_processes"] >= 4
        print(json.dumps({"executable":chrome,"outside":outside,"inside":inside}))
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_binary_capability_result_reaches_python_losslessly(kernel_stack):
    from kernel_runtime.wire_values import pack_value, unpack_value
    manager, runtimes, _ = kernel_stack
    raw = bytes(range(256)) * 5000
    async def binary(args):
        return raw
    manager.broker.registry.get('read_file').handler = binary
    runtimes.ensure_runtime('chat-binary', is_new=True)
    manager.catalog_service.select('chat-binary', 'build')
    literal = {'$variant1_bytes': 'not encoded bytes'}
    assert unpack_value(pack_value(literal)) == literal
    try:
        result = await manager.execute(chat_id='chat-binary', code="payload=tools.read_file(path='bytes')\nimport hashlib\nprint(type(payload).__name__, len(payload), hashlib.sha256(payload).hexdigest())",
                                       run_id='binary-run', outer_tool_call_id='binary-call')
        assert result.ok, result.to_dict()
        assert f'bytes {len(raw)} {hashlib.sha256(raw).hexdigest()}' in result.output.text()
        note = manager.continuation_context('chat-binary')
        assert 'Live CPython generation' in note and 'payload' in note
        assert 'Prior assistant prose is not execution evidence' in note
    finally:
        await manager.shutdown()
    assert 'No live CPython worker' in manager.continuation_context('chat-binary')


@pytest.mark.asyncio
async def test_continuation_prioritizes_requested_names_without_exposing_inventory(kernel_stack):
    manager, runtimes, _ = kernel_stack
    chat_id = 'chat-relevant-namespace'
    runtimes.ensure_runtime(chat_id, is_new=True)
    try:
        created = await manager.execute(
            chat_id=chat_id, run_id='create', outer_tool_call_id='create-call',
            code="for n in range(40): globals()[f'aaa_{n}'] = n\npb10_state = {'value': 'private-value'}",
        )
        assert created.ok, created.to_dict()
        assert 'pb10_state' not in created.output.namespace_delta['updated']
        assert 'pb10_state' in created.output.namespace_inventory
        assert 'inventory' not in json.dumps(created.output.evidence())
        retained = await manager.execute(chat_id=chat_id, code='other_state = 1',
                                         run_id='retained', outer_tool_call_id='retained-call')
        assert retained.ok
        note = manager.continuation_context(chat_id, current_user_text='Continue PB10 using pb10_state.')
        names = note.split('Recorded namespace names after the last cell (bounded, values omitted): ')[1].split('.\n')[0]
        assert names.split(', ')[0] == 'pb10_state'
        assert len(names.split(', ')) <= 24
        assert 'private-value' not in note
        assert 'promote_helper' not in note
        assert 'Mutation authoring is OFF' in note
        fresh = manager.continuation_context(chat_id, current_user_text='PB99 create a new document.')
        assert 'Recorded namespace names' not in fresh
        assert 'Existing state may belong to earlier tasks' in fresh
        assert 'perform the requested follow-up' not in fresh
        enabled = manager.continuation_context(chat_id, current_user_text='Continue PB10', mutation_enabled=True)
        assert 'promote_helper' in enabled
        deleted = await manager.execute(chat_id=chat_id, code='del pb10_state',
                                        run_id='deleted', outer_tool_call_id='deleted-call')
        assert deleted.ok
        after = manager.continuation_context(chat_id, current_user_text='Continue with pb10_state.')
        assert 'pb10_state' not in after
    finally:
        await manager.shutdown()


def _append_cell_outcome(
    ledger: KernelCellLedgerStore,
    *,
    execution_id: str,
    chat_id: str,
    status: str,
    generation: int = 1,
    error_code: str = "",
):
    return ledger.append(
        execution_id=execution_id,
        chat_id=chat_id,
        run_id=f"run-{execution_id}",
        outer_tool_call_id=f"call-{execution_id}",
        kernel_generation=generation,
        workspace_revision=1,
        workspace_fingerprint="workspace",
        workspace_root_ids=("root",),
        work_scope={"chat_id": chat_id},
        source_ref="artifact://source",
        source_sha256="source-sha",
        result_ref="artifact://result",
        result_sha256="result-sha",
        status=status,
        execution_count=1,
        started_at=1.0,
        completed_at=2.0,
        duration_ms=1.0,
        error_code=error_code,
    )


def test_cell_ledger_outcome_snapshot_is_exact_beyond_tail_and_chat_scoped(tmp_path):
    ledger = KernelCellLedgerStore(str(tmp_path / "cells.sqlite3"))
    first_error = _append_cell_outcome(
        ledger,
        execution_id="old-error",
        chat_id="chat-a",
        status="error",
        generation=3,
        error_code="python_exception",
    )
    for index in range(501):
        _append_cell_outcome(
            ledger,
            execution_id=f"later-ok-{index}",
            chat_id="chat-a",
            status="ok",
            generation=3,
        )
    assert all(record.status == "ok" for record in ledger.tail("chat-a", limit=500))
    snapshot = ledger.outcome_snapshot("chat-a")
    assert snapshot.to_dict() == {
        "chat_id": "chat-a",
        "cell_count": 502,
        "non_ok_count": 1,
        "latest_non_ok_execution_id": "old-error",
        "latest_non_ok_sequence": first_error.sequence,
        "latest_non_ok_kernel_generation": 3,
        "latest_non_ok_status": "error",
        "latest_non_ok_error_code": "python_exception",
        "later_cell_count": 501,
    }

    cancelled = _append_cell_outcome(
        ledger,
        execution_id="cancelled-cell",
        chat_id="chat-b",
        status="cancelled",
        generation=4,
        error_code="kernel_host_interrupted",
    )
    _append_cell_outcome(
        ledger,
        execution_id="clean-after-cancel",
        chat_id="chat-b",
        status="ok",
        generation=4,
    )
    running = _append_cell_outcome(
        ledger,
        execution_id="nonterminal-cell",
        chat_id="chat-b",
        status="running",
        generation=4,
    )
    other = ledger.outcome_snapshot("chat-b")
    assert other.non_ok_count == 2
    assert other.latest_non_ok_execution_id == running.execution_id
    assert other.latest_non_ok_sequence == running.sequence
    assert other.latest_non_ok_status == "running"
    assert other.later_cell_count == 0
    assert cancelled.sequence < running.sequence

    clean = _append_cell_outcome(
        ledger,
        execution_id="other-chat-clean",
        chat_id="chat-c",
        status="ok",
    )
    isolated = ledger.outcome_snapshot("chat-c")
    assert isolated.cell_count == 1
    assert isolated.non_ok_count == 0
    assert isolated.latest_non_ok_execution_id == ""
    assert isolated.latest_non_ok_sequence == 0
    assert isolated.later_cell_count == 0
    assert clean.chat_id == "chat-c"
    assert ledger.outcome_snapshot("missing-chat").cell_count == 0


@pytest.mark.asyncio
async def test_continuation_context_retains_non_ok_facts_after_clean_turns_and_restart(kernel_stack):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-durable-outcomes"
    clean_chat = "chat-clean-outcomes"
    runtimes.ensure_runtime(chat_id, is_new=True)
    runtimes.ensure_runtime(clean_chat, is_new=True)
    try:
        failed = await manager.execute(
            chat_id=chat_id,
            run_id="failed-run",
            outer_tool_call_id="failed-call",
            code="raise ValueError('expected test failure')",
        )
        assert not failed.ok
        for index in range(3):
            completed = await manager.execute(
                chat_id=chat_id,
                run_id=f"clean-run-{index}",
                outer_tool_call_id=f"clean-call-{index}",
                code=f"clean_value_{index} = {index}",
            )
            assert completed.ok
        clean_result = await manager.execute(
            chat_id=clean_chat,
            run_id="only-clean-run",
            outer_tool_call_id="only-clean-call",
            code="clean_value = 1",
        )
        assert clean_result.ok

        snapshot = manager.cell_ledger.outcome_snapshot(chat_id)
        note = manager.continuation_context(chat_id)
        expected_line = (
            "Durable-chat-scoped cell ledger: non-OK cells=1; "
            f"latest non-OK={snapshot.latest_non_ok_execution_id} "
            f"(sequence={snapshot.latest_non_ok_sequence}, generation=1, status=error); "
            "later cells=3. These execution facts do not establish state loss, "
            "unresolved work, or recovery."
        )
        assert expected_line in note
        assert "python_exception" not in note
        clean_note = manager.continuation_context(clean_chat)
        assert "Durable-chat-scoped cell ledger" not in clean_note
    finally:
        await manager.shutdown()

    restarted_note = manager.continuation_context(chat_id)
    assert "No live CPython worker" in restarted_note
    assert expected_line in restarted_note
    assert "Durable-chat-scoped cell ledger" not in manager.continuation_context(clean_chat)


@pytest.mark.asyncio
async def test_task_cancellation_records_partial_cell_before_reraising(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    manager.limits = replace(manager.limits, cell_timeout_s=0)
    chat_id = 'chat-cancel-evidence'
    runtimes.ensure_runtime(chat_id, is_new=True)
    entered = asyncio.Event()
    def output(chunk):
        if 'entered-evidence-loop' in chunk.get('text', ''): entered.set()
    code = "print('entered-evidence-loop', flush=True)\nwhile True: pass"
    task = asyncio.create_task(manager.execute(chat_id=chat_id, code=code, run_id='cancel-evidence',
                                               outer_tool_call_id='cancel-evidence-call', on_chunk=output))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        pending = manager.cell_ledger.unsettled()
        assert len(pending) == 1 and pending[0]['run_id'] == 'cancel-evidence'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        record = manager.cell_ledger.latest(chat_id)
        assert record.status == 'cancelled' and record.outer_tool_call_id == 'cancel-evidence-call'
        assert artifacts.read_bytes_scoped(record.source_ref, chat_id).decode() == code
        saved = json.loads(artifacts.read_bytes_scoped(record.result_ref, chat_id))
        assert 'entered-evidence-loop' in saved['text']
        assert manager.cell_ledger.unsettled() == ()
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


def test_prior_host_admission_is_reconciled_without_replay_or_invented_timing(kernel_stack, tmp_path):
    import subprocess
    import sys
    manager, runtimes, artifacts = kernel_stack
    source = artifacts.put_text("raise AssertionError('must never replay')", kind='kernel_cell_source', scope='crashed-chat')
    evidence = dict(execution_id='crashed-cell', chat_id='crashed-chat', run_id='crashed-run', outer_tool_call_id='crashed-call',
        kernel_generation=1, workspace_revision=0, workspace_fingerprint='test', workspace_root_ids=(),
        work_scope={'chat_id':'crashed-chat'}, source_ref=source.ref, source_sha256=source.sha256, started_at=time.time())
    # A separate host exits without a completion row; SQLite must retain the
    # admission even on a non-clean process exit.
    script = ('import os\nfrom kernel_runtime.cell_ledger import KernelCellLedgerStore\n'
              f'KernelCellLedgerStore({manager.cell_ledger.path!r}).admit("dead-host", {evidence!r})\n'
              'os._exit(7)\n')
    exited = subprocess.run([sys.executable, '-c', script], cwd=os.path.dirname(os.path.dirname(__file__)),
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
    assert exited.returncode == 7
    replacement = KernelRuntimeManager(registry=runtimes, broker=manager.broker, artifact_store=artifacts,
        root=manager.root, instance_id='replacement-host', app_root=manager.app_root,
        catalog_service=manager.catalog_service, cell_ledger_path=manager.cell_ledger.path)
    record = replacement.cell_ledger.latest('crashed-chat')
    assert record.source_ref == source.ref and record.status == 'unknown_effect'
    assert record.to_dict()['duration_ms'] is None
    assert replacement.cell_ledger.unsettled() == ()
    replacement._recover_interrupted_cell_evidence()
    assert len(replacement.cell_ledger.list('crashed-chat')) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_user_wait_survives_default_capability_and_transport_deadlines(kernel_stack, asynchronous):
    from capability_broker import current_capability_invocation
    manager, runtimes, _ = kernel_stack
    manager.limits = replace(manager.limits, bridge_timeout_s=.15)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_selection(args):
        context = current_capability_invocation()
        context.user_wait.set()
        waiting.set()
        try:
            await release.wait()
        finally:
            context.user_wait.clear()
        return 'selected-profile'

    manager.broker.registry.get('read_file').handler = wait_for_selection
    runtimes.ensure_runtime('chat-user-wait', is_new=True)
    manager.catalog_service.select('chat-user-wait', 'build')
    code = "print(await tools.read_file.async_(path='wait'))" if asynchronous else "print(tools.read_file(path='wait'))"
    task = asyncio.create_task(manager.execute(chat_id='chat-user-wait', code=code, run_id='wait-1', outer_tool_call_id='wait-call-1'))
    try:
        await asyncio.wait_for(waiting.wait(), 20)
        await asyncio.sleep(1.4)  # exceeds both the .15s host limit and 1s client idle limit
        assert not task.done()
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.ok and 'selected-profile' in result.output.text(), result.to_dict()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_waiting_for_profile_remains_cancellable_in_a_real_kernel(kernel_stack):
    from capability_broker import current_capability_invocation
    manager, runtimes, _ = kernel_stack
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def wait_for_selection(args):
        context = current_capability_invocation()
        context.user_wait.set()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            context.user_wait.clear()
            stopped.set()

    manager.broker.registry.get('read_file').handler = wait_for_selection
    chat_id = 'chat-stop-profile-wait'
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.catalog_service.select(chat_id, 'build')
    task = asyncio.create_task(manager.execute(chat_id=chat_id, code="tools.read_file(path='wait')", run_id='wait-stop', outer_tool_call_id='wait-stop-call'))
    try:
        await asyncio.wait_for(entered.wait(), 20)
        await manager.interrupt(chat_id)
        result = await asyncio.wait_for(task, 8)
        await asyncio.wait_for(stopped.wait(), 2)
        assert result.status == 'cancelled', result.to_dict()
        assert KernelExecutionError(result).cause_class == 'user'
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_real_ask_user_wait_uses_bridge_heartbeat_and_retires_cancelled_question(kernel_stack, tmp_path, cancel):
    from clarification import tool_ask_user, resolve_response
    from work_fabric.scope import WorkScope
    from work_fabric.service import WorkService

    manager, runtimes, _ = kernel_stack
    wait_s = float(os.environ.get('VARIANT1_LONG_USER_WAIT_S', '1.4'))
    manager.limits = replace(manager.limits, cell_timeout_s=0,
                            bridge_timeout_s=120 if wait_s > 120 else .15)
    work = WorkService.open(str(tmp_path/'questions.sqlite3'))
    messages = []
    requested = asyncio.Event()

    class Transport:
        async def send_json(self, value):
            messages.append(value)
            if value['type'] == 'clarification:request':
                requested.set()

    async def ask(args):
        return await tool_ask_user(work.interactions, {'questions': [{
            'question': 'Choose a format', 'header': 'Format',
            'options': [{'label': 'Markdown', 'description': 'A document'},
                        {'label': 'Text', 'description': 'Plain text'}]}]})

    manager.broker.registry.get('read_file').handler = ask
    chat_id = 'chat-actual-ask-user'
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.catalog_service.select(chat_id, 'build')
    ctx = Variant1RunContext.create(source='chat', chat_session=SimpleNamespace(),
                                   chat_transport=Transport(), work_scope=WorkScope(chat_id=chat_id))
    with bind_run_context(ctx):
        task = asyncio.create_task(manager.execute(chat_id=chat_id, code=(
            "answer = tools.read_file(path='ask')\n"
            "assert answer['status'] == 'answered'\n"
            "print('chosen=' + answer['answers']['q1'])"),
                                                   run_id='ask-run', outer_tool_call_id='ask-call'))
    try:
        await asyncio.wait_for(requested.wait(), 20)
        request = next(m for m in messages if m['type'] == 'clarification:request')
        await asyncio.sleep(wait_s)
        assert not task.done()
        if cancel:
            await manager.interrupt(chat_id)
        else:
            assert resolve_response(work.interactions, request['id'], {'q1': 'Markdown'}, skipped=False)
        result = await asyncio.wait_for(task, 8)
        assert result.status == ('cancelled' if cancel else 'ok'), result.to_dict()
        record = work.interactions.get(request['id'])
        assert record.status == ('cancelled' if cancel else 'answered')
        if not cancel:
            assert 'chosen=Markdown' in result.output.text()
            assert 'The user answered:' not in result.output.text()
        assert messages[-1]['type'] == 'clarification:closed'
    finally:
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [
    "print(tools.read_file(path='slow'))",
    "print(await tools.read_file.async_(path='slow'))",
    "print(await tools.read_file.async_(path='slow', _deadline_ms=800))",
])
async def test_capability_deadline_is_not_bridge_idle_timeout(kernel_stack, code):
    manager, runtimes, _ = kernel_stack
    manager.limits = replace(manager.limits, cell_timeout_s=0, bridge_timeout_s=.1)
    calls = []

    async def slow_read(args):
        calls.append(args['path'])
        await asyncio.sleep(.3)
        return 'finished with state intact'

    manager.broker.registry.get('read_file').handler = slow_read
    chat_id = 'chat-slow-capability'
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.catalog_service.select(chat_id, 'build')
    try:
        result = await manager.execute(chat_id=chat_id, code='retained = 41\n' + code,
                                       run_id='slow-run', outer_tool_call_id='slow-call')
        assert result.status == 'ok', result.to_dict()
        assert 'finished with state intact' in result.output.text()
        assert calls == ['slow']
        follow = await manager.execute(chat_id=chat_id, code='print(retained + 1)',
                                       run_id='follow-run', outer_tool_call_id='follow-call')
        assert follow.status == 'ok', follow.to_dict()
        assert '42' in follow.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows launcher ownership")
async def test_kernel_launcher_is_owned_before_interpreter_starts(kernel_stack, monkeypatch):
    import psutil
    from kernel_runtime.job_object import KernelJobObject

    manager, runtimes, _ = kernel_stack
    assign = KernelJobObject.assign_pid
    early_children = []

    def delayed_assign(job, pid):
        time.sleep(0.2)
        early_children.extend(psutil.Process(pid).children(recursive=True))
        return assign(job, pid)

    monkeypatch.setattr(KernelJobObject, "assign_pid", delayed_assign)
    runtimes.ensure_runtime("owned-launch", is_new=True)
    try:
        result = await manager.execute(
            chat_id="owned-launch", code="print(7)", run_id="owned-launch",
            outer_tool_call_id="owned-launch",
        )
        assert result.ok, result.to_dict()
        assert early_children == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_persistent_kernel_executes_state_and_typed_read_proxy(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-a", is_new=True)
    manager.catalog_service.select("chat-a", "build")
    first = await manager.execute(
        chat_id="chat-a",
        code="counter = 41\nprint(tools.read_file(path='bridge-ok'))",
        run_id="run-1",
        outer_tool_call_id="outer-1",
    )
    second = await manager.execute(
        chat_id="chat-a",
        code="counter += 1\nprint(counter)",
        run_id="run-2",
        outer_tool_call_id="outer-2",
    )
    try:
        assert first.ok, first.to_dict()
        assert "bridge-ok" in first.output.text()
        assert second.ok, second.to_dict()
        assert "42" in second.output.text()
        # A transient pre-admission boot failure may consume a fenced
        # generation before the first usable kernel is admitted.  Persistence
        # means both successful cells share the same admitted generation, not
        # that its durable counter must always be one.
        assert first.generation == second.generation
        assert first.generation >= 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_repl_top_level_await_trailing_result_and_raw_output_attribution(
    kernel_stack,
):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-repl-semantics"
    runtimes.ensure_runtime(chat_id, is_new=True)
    result = await manager.execute(
        chat_id=chat_id,
        code=(
            "import asyncio, os\n"
            "await asyncio.sleep(0.01)\n"
            "os.write(1, b'RAW-UNATTRIBUTED\\n')\n"
            "answer = 42\n"
            "answer"
        ),
        run_id="run-repl-semantics",
        outer_tool_call_id="outer-repl-semantics",
    )
    follow = await manager.execute(
        chat_id=chat_id,
        code="print('FOLLOW=' + str(answer))",
        run_id="run-repl-semantics-follow",
        outer_tool_call_id="outer-repl-semantics-follow",
    )
    try:
        assert result.ok and follow.ok
        assert "42" in result.output.text()
        assert "RAW-UNATTRIBUTED" not in result.output.text()
        assert "FOLLOW=42" in follow.output.text()
        assert manager.status(chat_id)["background_output"]["events"] >= 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_repl_preserves_future_imports_and_repairs_protected_roots(kernel_stack):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-repl-protected"
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.catalog_service.select(chat_id, "build")
    overwritten = await manager.execute(
        chat_id=chat_id,
        code=(
            "from __future__ import annotations\n"
            "tools = 'model-overwrite'\n"
            "toolbelt = 'model-overwrite'"
        ),
        run_id="run-repl-overwrite",
        outer_tool_call_id="outer-repl-overwrite",
    )
    repaired = await manager.execute(
        chat_id=chat_id,
        code=(
            "print(tools.read_file(path='protected-ready'))\n"
            "print(type(toolbelt).__name__)"
        ),
        run_id="run-repl-repaired",
        outer_tool_call_id="outer-repl-repaired",
    )
    broken_repr = await manager.execute(
        chat_id=chat_id,
        code=(
            "class BrokenRepr:\n"
            "    def __repr__(self):\n"
            "        raise RuntimeError('repr-boom')\n"
            "BrokenRepr()"
        ),
        run_id="run-repl-broken-repr",
        outer_tool_call_id="outer-repl-broken-repr",
    )
    try:
        assert overwritten.ok and repaired.ok and broken_repr.ok
        assert "protected-ready" in repaired.output.text()
        assert "ToolbeltNamespace" in repaired.output.text()
        assert "repr failed" in broken_repr.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_repl_native_imports_do_not_wait_for_another_control_frame(kernel_stack):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-import-debug"
    runtimes.ensure_runtime(chat_id, is_new=True)
    statuses = []
    for module in ("numpy", "pandas", "pyarrow", "duckdb", "matplotlib", "plotly"):
        result = await manager.execute(
            chat_id=chat_id,
            code=f"import {module}\nprint('IMPORTED={module}')",
            run_id=f"run-import-{module}",
            outer_tool_call_id=f"outer-import-{module}",
            timeout_s=10,
        )
        statuses.append((module, result.status, result.error_message))
    try:
        assert all(status == "ok" for _, status, _ in statuses), statuses
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_awaitable_capabilities_are_ordered_bounded_deadlined_and_sync_safe(
    kernel_stack,
):
    manager, runtimes, _ = kernel_stack
    invocation_events = []
    manager.emit = lambda event, **fields: invocation_events.append({
        "event": event, **fields,
    })
    runtimes.ensure_runtime("chat-async", is_new=True)
    manager.catalog_service.select("chat-async", "build")
    tool = manager.broker.registry.get("read_file")
    assert tool is not None
    active = 0
    max_active = 0

    async def async_probe(args):
        nonlocal active, max_active
        path = str(args["path"])
        active += 1
        max_active = max(max_active, active)
        try:
            if path == "deadline":
                await asyncio.sleep(1.0)
            elif "|" in path:
                await asyncio.sleep(float(path.split("|", 1)[1]))
            return path.split("|", 1)[0]
        finally:
            active -= 1

    tool.handler = async_probe
    ordered = await manager.execute(
        chat_id="chat-async",
        code=(
            "import asyncio, inspect\n"
            "paths = [f'{i}|{(12-i)*0.01}' for i in range(12)]\n"
            "values = await asyncio.gather(*(tools.read_file.async_(path=p) for p in paths))\n"
            "print('ASYNC_ORDER=' + repr(values))\n"
            "print('ASYNC_SIGNATURE=' + str(inspect.signature(tools.read_file.async_)))"
        ),
        run_id="run-async-order",
        outer_tool_call_id="outer-async-order",
    )
    parallel_peak = max_active
    tool.effect_class = "external_side_effect"
    tool.parallel_safe = False
    max_active = 0
    serialized = await manager.execute(
        chat_id="chat-async",
        code=(
            "serial_paths = [f'serial-{i}|0.03' for i in range(4)]\n"
            "serial_values = await asyncio.gather(*(tools.read_file.async_(path=p) "
            "for p in serial_paths))\n"
            "print('SERIAL_ORDER=' + repr(serial_values))"
        ),
        run_id="run-async-serialized",
        outer_tool_call_id="outer-async-serialized",
    )
    serialized_peak = max_active
    tool.effect_class = "read"
    tool.parallel_safe = True
    max_active = 0
    deadline = await manager.execute(
        chat_id="chat-async",
        code=(
            "try:\n"
            "    await tools.read_file.async_(path='deadline', _deadline_ms=25)\n"
            "except Variant1CapabilityError as exc:\n"
            "    print('ASYNC_DEADLINE=' + exc.code)\n"
            "    print('ASYNC_RECEIPT=' + str(bool(exc.receipt)))"
        ),
        run_id="run-async-deadline",
        outer_tool_call_id="outer-async-deadline",
    )
    synchronous = await manager.execute(
        chat_id="chat-async",
        code="print('SYNC_RESULT=' + tools.read_file(path='sync-still-works'))",
        run_id="run-sync-after-async",
        outer_tool_call_id="outer-sync-after-async",
    )
    try:
        assert ordered.ok, ordered.to_dict()
        assert (
            "ASYNC_ORDER=['0', '1', '2', '3', '4', '5', '6', '7', "
            "'8', '9', '10', '11']" in ordered.output.text()
        )
        assert "_deadline_ms" in ordered.output.text()
        assert 1 < parallel_peak <= manager.fanout_limit("chat-async")
        assert serialized.ok, serialized.to_dict()
        assert "SERIAL_ORDER=['serial-0', 'serial-1', 'serial-2', 'serial-3']" in serialized.output.text()
        assert serialized_peak == 1
        assert deadline.ok, deadline.to_dict()
        assert "ASYNC_DEADLINE=broker_deadline_exceeded" in deadline.output.text()
        assert "ASYNC_RECEIPT=True" in deadline.output.text()
        assert synchronous.ok, synchronous.to_dict()
        assert "SYNC_RESULT=sync-still-works" in synchronous.output.text()
        async_reads = [
            row for row in invocation_events
            if row.get("event") == "kernel:capability_invocation_policy"
            and row.get("capability_id") == "read_file"
            and row.get("invocation_mode") == "async"
        ]
        sync_reads = [
            row for row in invocation_events
            if row.get("event") == "kernel:capability_invocation_policy"
            and row.get("capability_id") == "read_file"
            and row.get("invocation_mode") == "sync"
        ]
        assert len(async_reads) >= 17
        assert sync_reads
    finally:
        await manager.close_chat("chat-async")


@pytest.mark.asyncio
async def test_awaitable_origin_is_fenced_but_retained_mount_proxy_runs(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-async-fence", is_new=True)
    manager.catalog_service.select("chat-async-fence", "build")
    captured = await manager.execute(
        chat_id="chat-async-fence",
        code=(
            "pending_from_a = tools.read_file.async_(path='late-origin')\n"
            "old_read_proxy = tools.read_file\n"
            "print('ASYNC_CAPTURED')"
        ),
        run_id="run-async-capture",
        outer_tool_call_id="outer-async-capture",
    )
    manager.catalog_service.select("chat-async-fence", "explore")
    fenced = await manager.execute(
        chat_id="chat-async-fence",
        code=(
            "try:\n"
            "    await pending_from_a\n"
            "except Variant1CapabilityError as exc:\n"
            "    print('ASYNC_ORIGIN=' + exc.code)\n"
            "retained = await old_read_proxy.async_(path='old-mount')\n"
            "print('ASYNC_RETAINED=' + str(retained))"
        ),
        run_id="run-async-fenced",
        outer_tool_call_id="outer-async-fenced",
    )
    try:
        assert captured.ok, captured.to_dict()
        assert fenced.ok, fenced.to_dict()
        assert "ASYNC_ORIGIN=stale_execution_admission" in fenced.output.text()
        assert "ASYNC_RETAINED=old-mount" in fenced.output.text()
    finally:
        await manager.close_chat("chat-async-fence")


@pytest.mark.asyncio
async def test_awaitable_cancellation_before_and_after_dispatch(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-async-cancel", is_new=True)
    manager.catalog_service.select("chat-async-cancel", "build")
    tool = manager.broker.registry.get("read_file")
    assert tool is not None
    called = []
    after_started = asyncio.Event()
    after_finished = asyncio.Event()

    async def cancellable(args):
        path = str(args["path"])
        called.append(path)
        if path == "after":
            after_started.set()
            try:
                await asyncio.sleep(10)
            finally:
                after_finished.set()
        return path

    tool.handler = cancellable
    result = await manager.execute(
        chat_id="chat-async-cancel",
        code=(
            "import asyncio\n"
            "before = asyncio.create_task(tools.read_file.async_(path='before'))\n"
            "before.cancel()\n"
            "try:\n"
            "    await before\n"
            "except asyncio.CancelledError:\n"
            "    print('CANCEL_BEFORE=True')\n"
            "after = asyncio.create_task(tools.read_file.async_(path='after'))\n"
            "await asyncio.sleep(0.3)\n"
            "after.cancel()\n"
            "try:\n"
            "    await after\n"
            "except asyncio.CancelledError:\n"
            "    print('CANCEL_AFTER=True')\n"
            "await asyncio.sleep(0.2)"
        ),
        run_id="run-async-cancel",
        outer_tool_call_id="outer-async-cancel",
    )
    try:
        assert result.ok, result.to_dict()
        assert "CANCEL_BEFORE=True" in result.output.text()
        assert "CANCEL_AFTER=True" in result.output.text()
        assert "before" not in called
        await asyncio.wait_for(after_started.wait(), timeout=2)
        await asyncio.wait_for(after_finished.wait(), timeout=2)
        error_codes = {
            receipt.error.code
            for receipt in manager.broker.receipts(50)
            if receipt.error is not None
        }
        assert "broker_cancelled_after_dispatch" in error_codes
        assert all(receipt.error.cause_class == 'unknown'
                   for receipt in manager.broker.receipts(50)
                   if receipt.error and 'broker_cancelled' in receipt.error.code)
    finally:
        await manager.close_chat("chat-async-cancel")


@pytest.mark.asyncio
async def test_kernel_shutdown_cancels_inflight_awaitable_capability(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-async-shutdown", is_new=True)
    manager.catalog_service.select("chat-async-shutdown", "build")
    tool = manager.broker.registry.get("read_file")
    assert tool is not None
    started = asyncio.Event()
    finished = asyncio.Event()

    async def until_shutdown(args):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            finished.set()
        return str(args["path"])

    tool.handler = until_shutdown
    execution = asyncio.create_task(manager.execute(
        chat_id="chat-async-shutdown",
        code="await tools.read_file.async_(path='shutdown')",
        run_id="run-async-shutdown",
        outer_tool_call_id="outer-async-shutdown",
    ))
    await asyncio.wait_for(started.wait(), timeout=30)
    assert await manager.close_chat(
        "chat-async-shutdown", reason="test_async_shutdown"
    )
    await asyncio.wait_for(finished.wait(), timeout=3)
    await asyncio.wait_for(
        asyncio.gather(execution, return_exceptions=True),
        timeout=5,
    )
    assert manager.status("chat-async-shutdown")["state"] == "absent"


@pytest.mark.asyncio
async def test_concurrent_same_chat_cells_share_one_serialized_lease(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-concurrent", is_new=True)
    first_task = asyncio.create_task(manager.execute(
        chat_id="chat-concurrent",
        code=(
            "import time\n"
            "shared_counter = 41\n"
            "time.sleep(0.5)\n"
            "print('first-finished')"
        ),
        run_id="run-concurrent-1",
        outer_tool_call_id="outer-concurrent-1",
    ))
    # A frozen worker can spend noticeably longer in Windows process launch /
    # antivirus inspection before the lease reaches ``busy``.  The manager's
    # admitted boot window, not an unrelated ten-second test constant, is the
    # contract this synchronization should honor.
    deadline = time.monotonic() + max(10.0, manager.limits.boot_timeout_s + 5.0)
    while manager.status("chat-concurrent")["state"] != "busy":
        if time.monotonic() >= deadline:
            raise AssertionError("first kernel cell never became busy")
        await asyncio.sleep(0.01)
    second_task = asyncio.create_task(manager.execute(
        chat_id="chat-concurrent",
        code="shared_counter += 1\nprint(shared_counter)",
        run_id="run-concurrent-2",
        outer_tool_call_id="outer-concurrent-2",
    ))

    first, second = await asyncio.gather(first_task, second_task)
    try:
        assert first.ok and second.ok
        assert "first-finished" in first.output.text()
        assert "42" in second.output.text()
        assert first.generation == second.generation
        assert len(manager._leases) == 1
        lease = manager._leases["chat-concurrent"]
        assert runtimes.kernel_lease("chat-concurrent") is lease
        assert lease.state == "ready"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("VARIANT1_TEST_KERNEL_EXE"),
    reason="requires the separately frozen Variant1Kernel executable",
)
async def test_frozen_worker_has_no_system_python_dependency(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-frozen-path", is_new=True)
    result = await manager.execute(
        chat_id="chat-frozen-path",
        code=(
            "import os, shutil, sys\n"
            "print('PYTHON=' + str(shutil.which('python') or 'NONE'))\n"
            "print('EXECUTABLE=' + os.path.basename(sys.executable))\n"
            "print('VENV=' + str(os.environ.get('VIRTUAL_ENV') or 'NONE'))"
        ),
        run_id="run-frozen-path",
        outer_tool_call_id="outer-frozen-path",
    )
    try:
        assert result.ok, result.to_dict()
        text = result.output.text()
        assert "PYTHON=NONE" in text
        assert "EXECUTABLE=Variant1Kernel.exe" in text
        assert "VENV=NONE" in text
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_repeated_task_cancellation_does_not_publish_lease_ready_early(
    kernel_stack,
):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-double-cancel"
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="seed = 1",
        run_id="run-double-cancel-seed",
        outer_tool_call_id="outer-double-cancel-seed",
    )
    assert seeded.ok
    lease = manager._leases[chat_id]
    task = asyncio.create_task(manager.execute(
        chat_id=chat_id,
        code="import asyncio\nawait asyncio.sleep(30)",
        run_id="run-double-cancel",
        outer_tool_call_id="outer-double-cancel",
    ))
    for _ in range(200):
        if lease._admissions:
            break
        await asyncio.sleep(0.01)
    assert lease._admissions
    execution_id = next(iter(lease._admissions))
    task.cancel()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
        assert execution_id not in lease._admissions
        assert lease.state == "ready"
        follow = await manager.execute(
            chat_id=chat_id,
            code="print('ready-after-double-cancel')",
            run_id="run-double-cancel-follow",
            outer_tool_call_id="outer-double-cancel-follow",
        )
        assert follow.ok
    finally:
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()


def test_kernel_execute_has_no_retired_ptr_per_call_switch():
    assert "persistent" not in inspect.signature(KernelRuntimeManager.execute).parameters


@pytest.mark.asyncio
async def test_interrupt_finishes_and_kernel_remains_controlled(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-c", is_new=True)
    cancelled = asyncio.Event()
    task = asyncio.create_task(manager.execute(
        chat_id="chat-c",
        code="import time\ntime.sleep(30)",
        run_id="run-cancel",
        outer_tool_call_id="outer-cancel",
        cancellation=cancelled,
    ))
    await asyncio.sleep(0.5)
    cancelled.set()
    result = await asyncio.wait_for(task, timeout=8)
    follow = await manager.execute(
        chat_id="chat-c",
        code="print('ready-again')",
        run_id="run-follow",
        outer_tool_call_id="outer-follow",
    )
    try:
        assert result.status == "cancelled"
        assert follow.ok, follow.to_dict()
        assert "ready-again" in follow.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_direct_kernel_steer_interrupts_sync_python_and_preserves_generation(
    kernel_stack,
):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-direct-interrupt"
    runtimes.ensure_runtime(chat_id, is_new=True)
    task = asyncio.create_task(manager.execute(
        chat_id=chat_id,
        code=(
            "steer_marker = 'before'\n"
            "while True:\n"
            "    steer_spin = 1\n"
        ),
        run_id="run-direct-interrupt",
        outer_tool_call_id="outer-direct-interrupt",
    ))
    for _ in range(200):
        lease = manager._leases.get(chat_id)
        if lease is not None and lease._admissions:
            break
        await asyncio.sleep(0.01)
    control = await manager.interrupt(chat_id, intent="steer")
    result = await asyncio.wait_for(task, timeout=8)
    follow = await manager.execute(
        chat_id=chat_id,
        code="print('direct-interrupt-recovered', steer_marker)",
        run_id="run-direct-interrupt-follow",
        outer_tool_call_id="outer-direct-interrupt-follow",
    )
    try:
        assert control["status"] == "requested"
        assert control["intent"] == "steer"
        assert control["hard_kill_on_grace"] is False
        assert result.status == "cancelled"
        assert result.hard_restarted is False
        assert follow.ok
        assert "direct-interrupt-recovered before" in follow.output.text()
        assert follow.generation == result.generation
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_pb12_style_sleep_steer_retains_partial_namespace(
    kernel_stack, tmp_path,
):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-pb12-style-steer"
    runtimes.ensure_runtime(chat_id, is_new=True)
    ready = tmp_path / "pb12-style-ready"
    task = asyncio.create_task(manager.execute(
        chat_id=chat_id,
        code=(
            "import time\n"
            "from pathlib import Path\n"
            "pb12_partial = ['before-sleep']\n"
            f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            "time.sleep(3)\n"
            "pb12_partial.append('after-sleep')\n"
        ),
        run_id="run-pb12-style-steer",
        outer_tool_call_id="outer-pb12-style-steer",
    ))
    for _ in range(400):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()
    control = await manager.interrupt(chat_id, intent="steer")
    result = await asyncio.wait_for(task, timeout=8)
    follow = await manager.execute(
        chat_id=chat_id,
        code="print(pb12_partial)",
        run_id="run-pb12-style-follow",
        outer_tool_call_id="outer-pb12-style-follow",
    )
    try:
        assert control["status"] == "requested"
        assert result.status == "cancelled"
        assert not result.hard_restarted
        assert follow.ok, follow.to_dict()
        assert follow.generation == result.generation
        assert "['before-sleep']" in follow.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_steer_caught_interrupt_waits_for_safe_boundary_without_replacement(
    kernel_stack,
):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-caught-steer"
    runtimes.ensure_runtime(chat_id, is_new=True)
    task = asyncio.create_task(manager.execute(
        chat_id=chat_id,
        code=(
            "import time\n"
            "caught_steer = 'waiting'\n"
            "try:\n"
            "    while True:\n"
            "        caught_spin = 1\n"
            "except KeyboardInterrupt:\n"
            "    caught_steer = 'caught'\n"
            "    time.sleep(1.25)\n"
            "caught_steer = 'finished'\n"
        ),
        run_id="run-caught-steer",
        outer_tool_call_id="outer-caught-steer",
    ))
    for _ in range(200):
        lease = manager._leases.get(chat_id)
        if lease is not None and lease._admissions:
            break
        await asyncio.sleep(0.01)
    control = await manager.interrupt(chat_id, intent="steer")
    result = await asyncio.wait_for(task, timeout=8)
    follow = await manager.execute(
        chat_id=chat_id,
        code="print('CAUGHT', caught_steer)",
        run_id="run-caught-steer-follow",
        outer_tool_call_id="outer-caught-steer-follow",
    )
    try:
        assert control["status"] == "requested"
        assert result.status == "cancelled"
        assert not result.hard_restarted
        assert result.duration_ms >= 1_000
        assert follow.ok, follow.to_dict()
        assert follow.generation == result.generation
        assert "CAUGHT finished" in follow.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_interrupt_escalates_to_job_kill_and_fences_generation(
    kernel_stack, tmp_path,
):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-hard", is_new=True)
    cancelled = asyncio.Event()
    ready = tmp_path / "uninterruptible-stop-ready"
    task = asyncio.create_task(manager.execute(
        chat_id="chat-hard",
        code=(
            "import signal, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGINT, lambda *_args: None)\n"
            f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            "while True:\n"
            "    time.sleep(0.1)\n"
        ),
        run_id="run-hard",
        outer_tool_call_id="outer-hard",
        cancellation=cancelled,
    ))
    for _ in range(400):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()
    cancelled.set()
    result = await asyncio.wait_for(task, timeout=8)
    follow = await manager.execute(
        chat_id="chat-hard",
        code="print('new-generation')",
        run_id="run-after-hard",
        outer_tool_call_id="outer-after-hard",
    )
    try:
        assert result.status == "cancelled"
        assert result.hard_restarted
        assert follow.ok
        assert follow.generation == result.generation + 1
        assert "new-generation" in follow.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_late_output_cannot_cross_into_next_cell(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-late", is_new=True)
    first = await manager.execute(
        chat_id="chat-late",
        code=(
            "import threading, time\n"
            "def late():\n"
            "    time.sleep(0.4)\n"
            "    print('LATE-OLD')\n"
            "threading.Thread(target=late, daemon=True).start()\n"
            "print('first-cell')\n"
        ),
        run_id="run-late-1",
        outer_tool_call_id="outer-late-1",
    )
    await asyncio.sleep(0.7)
    second = await manager.execute(
        chat_id="chat-late",
        code="print('second-cell')",
        run_id="run-late-2",
        outer_tool_call_id="outer-late-2",
    )
    try:
        assert first.ok and second.ok
        assert "first-cell" in first.output.text()
        assert "second-cell" in second.output.text()
        assert "LATE-OLD" not in second.output.text()
        assert second.output.stale_events >= 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stdin_is_deterministic_error_and_output_limit_preserves_control(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-bounds", is_new=True)
    blocked_input = await manager.execute(
        chat_id="chat-bounds",
        code="input('not-admitted: ')",
        run_id="run-input",
        outer_tool_call_id="outer-input",
    )
    bounded = await manager.execute(
        chat_id="chat-bounds",
        code="print('X' * 20000)",
        run_id="run-output",
        outer_tool_call_id="outer-output",
    )
    follow = await manager.execute(
        chat_id="chat-bounds",
        code="print('still-controlled')",
        run_id="run-controlled",
        outer_tool_call_id="outer-controlled",
    )
    try:
        assert blocked_input.status == "error"
        assert "EOFError" in blocked_input.output.text()
        assert bounded.ok
        assert bounded.output.truncated
        assert bounded.output.admitted_bytes <= 8192
        assert follow.ok
        assert "still-controlled" in follow.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("reader", ["raw_fd", "inherited_child"])
def test_private_repl_protocol_is_not_native_or_inherited_stdin(reader):
    import subprocess
    import sys

    # Run startup in a disposable interpreter: mutating standard descriptors
    # in the pytest process would damage its own capture/transport.
    script = r'''
import json, os, subprocess, sys
from kernel_runtime.repl_worker import _prepare_private_protocol_streams
control, protocol, stdout_read, stderr_read = _prepare_private_protocol_streams()
if sys.argv[1] == 'raw_fd':
    observed = os.read(0, 1).decode()
else:
    child = subprocess.run(
        [sys.executable, '-c', 'import os; print(repr(os.read(0, 1)))'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    observed = child.stdout.decode().strip()
frame = control.readline().decode()
protocol.write((json.dumps({'observed': observed, 'frame': frame}) + '\n').encode())
protocol.flush()
'''
    probe = subprocess.run(
        [sys.executable, "-c", script, reader], input=b"PRIVATE-FRAME\n",
        capture_output=True, timeout=10,
        cwd=os.path.dirname(os.path.dirname(__file__)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert probe.returncode == 0, probe.stderr
    receipt = json.loads(probe.stdout)
    assert receipt == {
        "observed": "" if reader == "raw_fd" else "b''",
        "frame": "PRIVATE-FRAME\n",
    }


@pytest.mark.asyncio
async def test_subprocess_explicit_stdin_and_later_cells_survive_native_stdin_isolation(kernel_stack):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-native-stdin", is_new=True)
    try:
        first = await manager.execute(
            chat_id="chat-native-stdin",
            code="""
import os, subprocess, sys
assert os.read(0, 1) == b''
flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
inherited = subprocess.run([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'],
                          capture_output=True, text=True, timeout=5, creationflags=flags)
assert inherited.returncode == 0 and inherited.stdout.strip() == "''"
explicit = subprocess.run([sys.executable, '-c', 'import sys; print(sys.stdin.read().upper())'],
                         input='explicit input', capture_output=True, text=True,
                         timeout=5, creationflags=flags)
assert explicit.returncode == 0 and explicit.stdout.strip() == 'EXPLICIT INPUT'
retained_after_children = 41
print('child-input-contract-verified')
""",
            run_id="native-stdin-1", outer_tool_call_id="native-stdin-1",
        )
        follow = await manager.execute(
            chat_id="chat-native-stdin", code="print(retained_after_children + 1)",
            run_id="native-stdin-2", outer_tool_call_id="native-stdin-2",
        )
        assert first.ok, first.output.text()
        assert "child-input-contract-verified" in first.output.text()
        assert follow.ok and "42" in follow.output.text()
        assert first.generation == follow.generation
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_artifact_disk_failure_drops_rich_output_but_preserves_control(
    kernel_stack,
):
    manager, runtimes, _ = kernel_stack

    class FullDiskArtifacts:
        def put_bytes(self, *_args, **_kwargs):
            raise OSError(28, "simulated disk full")

    manager.artifact_store = FullDiskArtifacts()
    runtimes.ensure_runtime("chat-low-disk", is_new=True)
    rich = await manager.execute(
        chat_id="chat-low-disk",
        code=(
            "display({'image/png': "
            "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='}, raw=True)\n"
            "print('cell-finished')"
        ),
        run_id="run-low-disk",
        outer_tool_call_id="outer-low-disk",
    )
    follow = await manager.execute(
        chat_id="chat-low-disk",
        code="print('control-preserved')",
        run_id="run-low-disk-follow",
        outer_tool_call_id="outer-low-disk-follow",
    )
    try:
        assert rich.ok, rich.to_dict()
        assert rich.output.artifact_errors == ["OSError"]
        assert rich.output.dropped_events >= 1
        assert "cell-finished" in rich.output.text()
        assert follow.ok, follow.to_dict()
        assert "control-preserved" in follow.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_chat_namespaces_are_isolated_and_rich_output_becomes_artifact(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    runtimes.ensure_runtime("chat-one", is_new=True)
    runtimes.ensure_runtime("chat-two", is_new=True)
    one = await manager.execute(
        chat_id="chat-one",
        code="private_value = 'only-one'\nprint(private_value)",
        run_id="run-one",
        outer_tool_call_id="outer-one",
    )
    two = await manager.execute(
        chat_id="chat-two",
        code=(
            "display({'text/plain': '<Figure fallback-only>', 'image/png': "
            "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='}, raw=True)\n"
            "print('absent' if 'private_value' not in globals() else private_value)\n"
        ),
        run_id="run-two",
        outer_tool_call_id="outer-two",
    )
    try:
        assert one.ok and two.ok
        assert one.generation == 1 and two.generation == 1
        assert "absent" in two.output.text()
        assert "<Figure fallback-only>" not in two.output.text()
        assert len(two.output.artifacts) == 1
        assert artifacts.exists(two.output.artifacts[0]["ref"])
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
async def test_shutdown_kills_kernel_grandchild(kernel_stack):
    import psutil

    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-tree", is_new=True)
    result = await manager.execute(
        chat_id="chat-tree",
        code=(
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(child.pid)\n"
        ),
        run_id="run-tree",
        outer_tool_call_id="outer-tree",
    )
    child_pid = int(result.output.text().splitlines()[-1])
    assert psutil.pid_exists(child_pid)
    await manager.close_chat("chat-tree", reason="tree-test")
    deadline = time.monotonic() + 4
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    try:
        assert not psutil.pid_exists(child_pid)
    finally:
        await manager.shutdown()


def test_provider_contract_is_exactly_one_ipython_tool():
    assert IPYTHON_PROVIDER_SPEC["name"] == "ipython"
    assert set(IPYTHON_PROVIDER_SPEC["params"]) == {"category", "code"}
    assert IPYTHON_SCHEMA_REVISION == "ipython.portable.v6"


@pytest.mark.asyncio
async def test_delayed_capability_task_cannot_borrow_next_cell_admission(
    kernel_stack,
):
    manager, runtimes, _artifacts = kernel_stack
    runtimes.ensure_runtime("chat-origin-fence", is_new=True)
    manager.catalog_service.select("chat-origin-fence", "build")
    read_calls = []
    read_tool = manager.broker.registry.get("read_file")
    original_read = read_tool.handler

    async def tracked_read(arguments):
        read_calls.append(dict(arguments))
        return await original_read(arguments)

    read_tool.handler = tracked_read
    scheduled = await manager.execute(
        chat_id="chat-origin-fence",
        code=(
            "import asyncio\n"
            "async def delayed_capability():\n"
            "    await asyncio.sleep(0.35)\n"
            "    try:\n"
            "        value = tools.read_file(path='wrong-cell-effect')\n"
            "        print('DELAYED_RESULT', value)\n"
            "    except Exception as exc:\n"
            "        print('DELAYED_ERROR', getattr(exc, 'code', ''), str(exc))\n"
            "asyncio.create_task(delayed_capability())\n"
            "print('scheduled-origin-task')"
        ),
        run_id="run-origin-a",
        outer_tool_call_id="outer-origin-a",
    )
    next_cell = await manager.execute(
        chat_id="chat-origin-fence",
        code="import asyncio\nawait asyncio.sleep(0.7)\nprint('next-cell-finished')",
        run_id="run-origin-b",
        outer_tool_call_id="outer-origin-b",
    )
    try:
        assert scheduled.ok, scheduled.to_dict()
        assert next_cell.ok, next_cell.to_dict()
        assert "scheduled-origin-task" in scheduled.output.text()
        assert "next-cell-finished" in next_cell.output.text()
        assert "DELAYED_RESULT" not in next_cell.output.text()
        assert "wrong-cell-effect" not in next_cell.output.text()
        assert next_cell.output.stale_events >= 1
        assert read_calls == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_ipython_front_door_returns_structured_execution(
    kernel_stack,
):
    manager, runtimes, _artifacts = kernel_stack
    runtimes.ensure_runtime("chat-front-door", is_new=True)
    registry = manager.broker.registry
    runtime = SimpleNamespace(
        session_runtimes=runtimes,
        catalog=manager.catalog_service,
        kernel=manager,
    )
    register_ipython_tool(registry, lambda: runtime)
    tool = registry.get("ipython")
    context = Variant1RunContext.create(
        source="test",
        run_id="run-front-door",
        metadata={"chat_id": "chat-front-door"},
    )

    try:
        with bind_run_context(context):
            first_projection = await tool.run({
                "category": "build",
            })
            repeated_projection = await tool.run({
                "category": "build",
            })
            execution_projection = await tool.run({
                "category": "explore",
                "code": "front_door_value = 42\nprint('structured-ok')",
            })
            with pytest.raises(KernelExecutionError) as failed:
                await tool.run({"code": "raise ValueError('structured-failure')"})
            with pytest.raises(KernelExecutionError) as failed_after_mount:
                await tool.run({
                    "category": "build",
                    "code": "surviving_value = 73\nraise ValueError('failed-after-mount')",
                })
            recovered = await tool.run({"code": "print(surviving_value)"})

        first_selection = first_projection.programmatic_value
        repeated_selection = repeated_projection.programmatic_value
        execution = execution_projection.programmatic_value
        failure = failed.value.result.to_dict()
        assert execution["schema"] == "variant1.kernel-execution.v1"
        assert first_selection["unchanged"] is False
        assert "mount_card" in first_selection
        assert "Build mounted" in str(first_projection)
        assert "Python calls below are available in the `code` field of `ipython`" in str(
            first_projection
        )
        assert "tools.read_file(" in str(first_projection)
        assert "Mutation: off." in str(first_projection)
        assert "catalog_release_id" not in str(first_projection)
        assert "chat_id" not in str(first_projection)
        assert repeated_selection["unchanged"] is True
        assert repeated_selection["mount_revision"] == first_selection["mount_revision"]
        assert "already mounted" in str(repeated_projection)
        assert "Execute Python code now" in str(repeated_projection)
        assert "mount_card" not in str(repeated_projection)
        assert execution["status"] == "ok"
        assert "structured-ok" in execution["text"]
        assert execution["ledger"]["sequence"] > 0
        assert "Explore mounted" in str(execution_projection)
        assert "structured-ok" in str(execution_projection)
        assert "[Session state]" not in str(execution_projection)
        assert execution_projection.receipt_metadata["category_id"] == "explore"
        assert execution_projection.receipt_metadata["mount_disclosed"] is True
        assert "execution_id" not in str(execution_projection)
        assert "ledger" not in str(execution_projection)
        assert failure["status"] == "error"
        assert failure["error"]["code"] == "python_exception"
        assert "structured-failure" in failure["text"]
        assert str(failed.value).startswith(
            "ERROR python_exception: ValueError: structured-failure"
        )
        assert failed.value.code == "python_exception"
        assert failed.value.cause_class == "model"
        assert "Traceback tail:" in str(failed.value)
        assert "structured-failure" in str(failed.value)
        assert "execution_id" not in str(failed.value)
        # Mounting and prior Python effects are already committed when code
        # fails. The next model turn needs the new contract to recover.
        mounted_error = failed_after_mount.value
        assert str(mounted_error).startswith("Build mounted")
        assert "tools.read_file(" in str(mounted_error)
        assert "ERROR python_exception: ValueError: failed-after-mount" in str(mounted_error)
        assert mounted_error.code == "python_exception"
        assert mounted_error.cause_class == "model"
        assert mounted_error.result.to_dict()["status"] == "error"
        assert "73" in str(recovered)
        assert "Build mounted" not in str(recovered)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_uncaught_nested_capability_error_has_compact_model_projection(
    kernel_stack,
):
    manager, runtimes, _artifacts = kernel_stack
    runtimes.ensure_runtime("chat-capability-error", is_new=True)

    result = await manager.execute(
        chat_id="chat-capability-error",
        code=(
            "from kernel_runtime.worker_bridge import Variant1CapabilityError\n"
            "raise Variant1CapabilityError("
            "'mcp_tool_error', 'MCP tool returned an exact synthetic failure')"
        ),
        run_id="run-capability-error",
        outer_tool_call_id="outer-capability-error",
    )

    try:
        assert result.ok is False
        assert result.error_code == "capability_error"
        assert result.model_observation() == (
            "ERROR capability_error: MCP tool returned an exact synthetic failure"
        )
        assert "Traceback" not in result.model_observation()
    finally:
        await manager.close_chat("chat-capability-error")


@pytest.mark.asyncio
async def test_nested_capability_conclusion_reaches_outer_ipython_tool(
    kernel_stack,
):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-nested-conclusion"
    runtimes.ensure_runtime(chat_id, is_new=True)
    registry = manager.broker.registry
    read_file = registry.get("read_file")
    original_handler = read_file.handler

    async def concluding_read(arguments):
        return ToolProjectionResult(
            "authoritative result",
            programmatic_value={"path": arguments["path"], "ok": True},
            receipt_metadata={
                "terminal_observation": "authoritative result",
            },
            terminate=True,
        )

    read_file.handler = concluding_read
    runtime = SimpleNamespace(
        session_runtimes=runtimes,
        catalog=manager.catalog_service,
        kernel=manager,
    )
    register_ipython_tool(registry, lambda: runtime)
    context = Variant1RunContext.create(
        source="test",
        run_id="run-nested-conclusion",
        metadata={"chat_id": chat_id},
    )

    try:
        with bind_run_context(context):
            await registry.get("ipython").run({"category": "build"})
            projection = await registry.get("ipython").run({
                "code": "result = tools.read_file(path='done.txt')",
            })
        assert projection.terminate is True
        assert projection.programmatic_value["terminate"] is True
        assert str(projection) == "authoritative result"
    finally:
        read_file.handler = original_handler
        await manager.shutdown()


@pytest.mark.asyncio
async def test_nested_ipython_screenshot_rejoins_outer_model_image_sink(
    kernel_stack,
):
    from desktop import service as desktop_service

    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-nested-screenshot"
    registry = manager.broker.registry
    read_file_tool = registry.get("read_file")
    original_handler = read_file_tool.handler
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nmock-image").decode("ascii")

    async def capture_screenshot(_arguments):
        desktop_service.deliver_image(
            png,
            media_type="image/png",
            artifact_ref="artifact://sha256/test-screenshot",
            producer="browser_screenshot",
        )
        return ToolProjectionResult(
            "screenshot captured",
            programmatic_value={"ok": True},
        )

    read_file_tool.handler = capture_screenshot
    runtimes.ensure_runtime(chat_id, is_new=True)
    runtime = SimpleNamespace(
        session_runtimes=runtimes,
        catalog=manager.catalog_service,
        kernel=manager,
    )
    register_ipython_tool(registry, lambda: runtime)
    holder = {"image": None}
    context = Variant1RunContext.create(
        source="test",
        run_id="run-nested-screenshot",
        metadata={"chat_id": chat_id},
        image_sink=holder,
    )
    outer_call_id = "call-ipython-screenshot-1"

    try:
        with bind_run_context(context), bind_outer_tool_call_id(outer_call_id):
            provenance = desktop_service.bind_image_provenance(
                outer_call_id, "ipython",
            )
            try:
                projection = await registry.get("ipython").run({
                    "category": "build",
                    "code": "shot = tools.read_file(path='capture')\nprint(shot)",
                })
            finally:
                desktop_service.reset_image_provenance(provenance)
        assert "{'ok': True}" in str(projection)
        assert holder["image"]["origin"] == "tool_result"
        assert holder["image"]["tool_call_ids"] == [outer_call_id]
        assert holder["image"]["tool_name"] == "browser_screenshot"
        assert holder["image"]["media_type"] == "image/png"
        assert holder["image"]["artifact_ref"].startswith("artifact://")
        assert holder["image"]["capture"]["status"] == "captured"
    finally:
        read_file_tool.handler = original_handler
        await manager.shutdown()


@pytest.mark.asyncio
async def test_ipython_front_door_projects_structured_auto_restore_failure(
    kernel_stack, monkeypatch,
):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-front-door-auto-restore-error"
    runtimes.ensure_runtime(chat_id, is_new=True)
    registry = manager.broker.registry
    runtime = SimpleNamespace(
        session_runtimes=runtimes,
        catalog=manager.catalog_service,
        kernel=manager,
    )
    register_ipython_tool(registry, lambda: runtime)
    tool = registry.get("ipython")
    outcome = {
        "schema": "variant1.kernel-auto-restore-outcome.v1",
        "runtime_chat_id": chat_id,
        "status": "failed",
        "reason": "capsule_value_unavailable",
    }

    async def fail_restore(**_kwargs):
        raise KernelAutoRestoreError(
            "capsule_value_unavailable",
            "Configured checkpoint restoration failed before model code.",
            outcome=outcome,
        )

    monkeypatch.setattr(manager, "execute", fail_restore)
    context = Variant1RunContext.create(
        source="test",
        run_id="run-front-door-auto-restore-error",
        metadata={"chat_id": chat_id},
    )
    with bind_run_context(context):
        with pytest.raises(ToolError) as failed:
            await tool.run({"code": "print('must-not-run')"})
    projected = json.loads(str(failed.value))
    assert projected["schema"] == "variant1.kernel-auto-restore-error.v1"
    assert projected["error"]["code"] == "capsule_value_unavailable"
    assert projected["outcome"] == outcome
    await manager.shutdown()


def test_capsule_worker_admits_portable_builtins_and_discloses_exclusions():
    namespace = {
        "tools": object(),
        "text_value": "hello Ω",
        "bytes_value": b"\x00\xff",
        "json_value": {"items": [1, True, None, 3.5]},
        "tuple_value": (1, 2),
        "callable_value": lambda: None,
        "module_value": json,
    }
    kernel = SimpleNamespace(namespace=namespace, runtime_profile={})
    worker = KernelCapsuleWorker(
        kernel,
        reinstall_namespace=lambda _document: None,
        document={"schema": "variant1.astb.namespace.v1", "services": {}},
    )

    captured = worker.capture()

    admitted = {item["name"]: item["serializer"] for item in captured["values"]}
    excluded = {item["name"]: item["reason"] for item in captured["excluded"]}
    assert captured["schema"] == WORKER_CAPSULE_SCHEMA
    assert admitted == {
        "bytes_value": "bytes.v1",
        "json_value": "json.strict.v1",
        "text_value": "text.utf8.v1",
        "tuple_value": "tuple.tree.v1",
    }
    assert excluded["tools"] == "host_owned_namespace"
    assert excluded["callable_value"] == "callable"
    assert excluded["module_value"] == "module"


def test_namespace_worker_inspection_never_invokes_a_serializer(monkeypatch):
    namespace = {
        "large_text": "x" * 100_000,
        "portable": {"items": [1, 2, 3]},
        "tools": object(),
    }
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace=namespace, runtime_profile={}),
        reinstall_namespace=lambda _document: None,
        document={"schema": "variant1.astb.namespace.v1"},
    )

    def serialization_is_a_bug(*_args, **_kwargs):
        raise AssertionError("metadata inspection serialized a value")

    monkeypatch.setattr(
        capsule_worker_module,
        "_encoded_chunks",
        serialization_is_a_bug,
    )
    inspected = worker.inspect({"limit": 20})

    assert inspected["schema"] == "variant1.kernel-namespace-worker.v1"
    values = {item["name"]: item for item in inspected["values"]}
    assert values["large_text"]["serializer"] == "text.utf8.v1"
    assert values["large_text"]["bytes"] is None
    assert values["portable"]["serializer"] == "json.strict.v1"


def test_capsule_worker_enforces_body_bound_before_hashing(monkeypatch):
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace={"too_large": "four"}, runtime_profile={}),
        reinstall_namespace=lambda _document: None,
        document={},
    )

    def hashing_is_too_late(*_args, **_kwargs):
        raise AssertionError("oversized value reached hashing")

    monkeypatch.setattr(capsule_worker_module, "_digest", hashing_is_too_late)
    captured = worker.capture({
        "schema": "variant1.kernel-capsule-capture-request.v1",
        "limits": {"max_value_bytes": 3},
    })

    assert captured["error"]["code"] == "capsule_value_too_large"


def test_capsule_worker_enforces_count_depth_total_and_response_bounds():
    def capture(namespace, **limits):
        worker = KernelCapsuleWorker(
            SimpleNamespace(namespace=namespace, runtime_profile={}),
            reinstall_namespace=lambda _document: None,
            document={},
        )
        return worker.capture({
            "schema": "variant1.kernel-capsule-capture-request.v1",
            "limits": limits,
        })

    count = capture({"a": 1, "b": 2}, max_values=1)
    assert count["error"]["code"] == "capsule_value_limit"

    total = capture(
        {"a": "abc", "b": "def"},
        max_value_bytes=10,
        max_total_value_bytes=5,
    )
    assert total["error"]["code"] == "capsule_total_too_large"

    depth = capture({"nested": [[[1]]]}, max_depth=1)
    excluded = {item["name"]: item["reason"] for item in depth["excluded"]}
    assert excluded["nested"] == "max_depth_exceeded"

    response = capture(
        {"large": "x" * 1_000},
        max_value_bytes=2_000,
        max_total_value_bytes=2_000,
        max_response_bytes=1_024,
    )
    assert response["error"]["code"] == "capsule_response_too_large"


def test_versioned_tuple_dataclass_numpy_and_safetensors_codecs_round_trip():
    numpy = pytest.importorskip("numpy")
    pytest.importorskip("safetensors")
    namespace = {
        "tuple_value": (1, [2], {"nested": (3,)}),
        "dataclass_value": KernelCapsuleLimits(max_values=7, max_depth=12),
        "array_value": numpy.arange(6, dtype=numpy.int32).reshape(2, 3),
        "tensor_map": {"weights": numpy.arange(3, dtype=numpy.float32)},
    }
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace=namespace, runtime_profile={}),
        reinstall_namespace=lambda _document: None,
        document={},
    )

    captured = worker.capture()
    serializers = {
        item["name"]: item["serializer"] for item in captured["values"]
    }
    assert serializers == {
        "array_value": "numpy.npy.v1",
        "dataclass_value": "dataclass.fields.v1",
        "tensor_map": "safetensors.numpy.v1",
        "tuple_value": "tuple.tree.v1",
    }
    assert all(item["requirements"] for item in captured["values"])

    namespace["stale"] = True
    restored = worker.restore({
        "schema": WORKER_CAPSULE_SCHEMA,
        "values": captured["values"],
    })
    assert set(restored["restored_names"]) == set(serializers)
    assert namespace["tuple_value"] == (1, [2], {"nested": (3,)})
    assert isinstance(namespace["dataclass_value"], KernelCapsuleLimits)
    assert namespace["dataclass_value"].max_values == 7
    numpy.testing.assert_array_equal(
        namespace["array_value"], numpy.arange(6, dtype=numpy.int32).reshape(2, 3)
    )
    numpy.testing.assert_array_equal(
        namespace["tensor_map"]["weights"],
        numpy.arange(3, dtype=numpy.float32),
    )
    assert "stale" not in namespace


def test_pandas_arrow_codec_round_trips_frame_series_and_indexes():
    pandas = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    frame = pandas.DataFrame(
        {
            "count": pandas.array([1, None], dtype="Int64"),
            "label": pandas.Categorical(["x", "y"]),
        },
        index=pandas.Index(["row-a", "row-b"], name="row"),
    )
    series = pandas.Series(
        pandas.array([1.5, None], dtype="Float64"),
        index=pandas.Index([10, 20], name="sample"),
        name="metric",
    )
    index = pandas.DatetimeIndex(
        pandas.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
        name="when",
    )
    range_index = pandas.RangeIndex(2, 10, 2, name="step")
    multi_index = pandas.MultiIndex.from_tuples(
        [("a", 1), ("b", 2)], names=["group", "number"]
    )
    expected = {
        "frame": frame.copy(),
        "series": series.copy(),
        "index": index.copy(),
        "range_index": range_index.copy(),
        "multi_index": multi_index.copy(),
    }
    namespace = dict(expected)
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace=namespace, runtime_profile={}),
        reinstall_namespace=lambda _document: None,
        document={},
    )

    contributors = {
        item["name"]: item
        for item in worker.resource_snapshot(limit=20)["namespace"]["contributors"]
    }
    assert contributors["frame"]["basis"] == "pandas_memory_usage"
    assert contributors["frame"]["estimated_bytes"] > 0

    captured = worker.capture()
    serializers = {
        item["name"]: item["serializer"] for item in captured["values"]
    }
    assert serializers == {
        name: "pandas.arrow.v1" for name in sorted(expected)
    }
    assert all(
        item["requirements"]["packages"]["pandas"] == "3.0.5"
        and item["requirements"]["packages"]["pyarrow"] == "25.0.0"
        for item in captured["values"]
    )

    namespace["stale"] = True
    restored = worker.restore({
        "schema": WORKER_CAPSULE_SCHEMA,
        "values": captured["values"],
    })
    assert set(restored["restored_names"]) == set(expected)
    pandas.testing.assert_frame_equal(namespace["frame"], expected["frame"])
    pandas.testing.assert_series_equal(namespace["series"], expected["series"])
    pandas.testing.assert_index_equal(namespace["index"], expected["index"])
    pandas.testing.assert_index_equal(
        namespace["range_index"], expected["range_index"], exact=True
    )
    pandas.testing.assert_index_equal(
        namespace["multi_index"], expected["multi_index"], exact=True
    )
    assert "stale" not in namespace


def test_pandas_capsule_bound_rejects_before_arrow_materialization(monkeypatch):
    pandas = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    frame = pandas.DataFrame({"value": ["x" * 100] * 100})
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace={"frame": frame}, runtime_profile={}),
        reinstall_namespace=lambda _document: None,
        document={},
    )

    def arrow_is_too_late(_value):
        raise AssertionError("oversized pandas value reached Arrow encoding")

    monkeypatch.setattr(
        capsule_worker_module, "_pandas_arrow_table", arrow_is_too_late
    )
    captured = worker.capture({
        "schema": "variant1.kernel-capsule-capture-request.v1",
        "limits": {"max_value_bytes": 1},
    })

    assert captured["error"]["code"] == "capsule_value_too_large"


def test_pandas_codec_discloses_unavailable_package_pair(monkeypatch):
    original = capsule_worker_module._package_version

    def without_pandas_stack(name):
        return "" if name in {"pandas", "pyarrow"} else original(name)

    capsule_worker_module.serializer_registry.cache_clear()
    monkeypatch.setattr(
        capsule_worker_module, "_package_version", without_pandas_stack
    )
    try:
        registry = capsule_worker_module.serializer_registry()
        descriptor = next(
            item for item in registry["serializers"]
            if item["id"] == "pandas.arrow.v1"
        )
        assert descriptor["available"] is False
        assert descriptor["packages"] == {"pandas": "", "pyarrow": ""}
    finally:
        capsule_worker_module.serializer_registry.cache_clear()


def test_capsule_worker_rolls_back_user_namespace_if_reinstall_fails():
    namespace = {"old_value": "still here", "tools": object()}
    kernel = SimpleNamespace(namespace=namespace, runtime_profile={})
    calls = []

    def reinstall(_document):
        calls.append("called")
        if len(calls) == 1:
            raise RuntimeError("simulated namespace reinstall failure")

    worker = KernelCapsuleWorker(
        kernel,
        reinstall_namespace=reinstall,
        document={"schema": "variant1.astb.namespace.v1", "services": {}},
    )
    raw = b"new"
    with pytest.raises(RuntimeError, match="capsule_restore_rolled_back"):
        worker.restore({
            "schema": WORKER_CAPSULE_SCHEMA,
            "values": [{
                "name": "new_value",
                "serializer": "text.utf8.v1",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "data_b64": base64.b64encode(raw).decode("ascii"),
            }],
        })

    assert namespace["old_value"] == "still here"
    assert "new_value" not in namespace
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_kernel_capsule_round_trip_and_namespace_reinstallation(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    runtimes.ensure_runtime("chat-capsule", is_new=True)
    manager.catalog_service.select("chat-capsule", "build")
    initial = await manager.execute(
        chat_id="chat-capsule",
        code=(
            "import json as user_module\n"
            "text_value = 'hello Ω'\n"
            "bytes_value = b'\\x00\\xff'\n"
            "json_value = {'items': [1, True, None]}\n"
            "same_text_a = 'deduplicated'\n"
            "same_text_b = 'deduplicated'\n"
            "tuple_value = (1, 2)"
        ),
        run_id="run-capsule-initial",
        outer_tool_call_id="outer-capsule-initial",
    )
    assert initial.ok, initial.to_dict()

    manifest = await manager.create_capsule(runtime_chat_id="chat-capsule")
    assert artifacts.stat(
        manifest.artifact_ref, scope="chat-capsule", verify=True
    ).kind == "kernel_capsule_manifest"
    assert {item.name for item in manifest.values} == {
        "bytes_value",
        "json_value",
        "same_text_a",
        "same_text_b",
        "text_value",
        "tuple_value",
    }
    value_refs = {item.name: item.artifact_ref for item in manifest.values}
    assert value_refs["same_text_a"] == value_refs["same_text_b"]
    excluded = {item.name: item.reason for item in manifest.excluded_values}
    assert excluded["user_module"] == "module"
    assert len(manifest.app_digest) == 64
    assert len(manifest.python_digest) == 64
    assert len(manifest.platform_digest) == 64
    assert manifest.runtime_profile_id == "core.v1"
    assert len(manifest.runtime_profile_digest) == 64
    assert manifest.runtime_profile["digest"] == manifest.runtime_profile_digest
    assert manifest.to_dict()["compatibility"]["verdict"] == "compatible"
    assert manifest.cell_sequence == initial.ledger_sequence
    assert manifest.cell_execution_id == initial.execution_id
    assert "cloudpickle" in manifest.unsupported_serializers
    pandas_codec = next(
        item for item in manifest.serializer_registry["serializers"]
        if item["id"] == "pandas.arrow.v1"
    )
    assert (
        "pandas.arrow.v1" not in manifest.unsupported_serializers
        if pandas_codec["available"]
        else "pandas.arrow.v1" in manifest.unsupported_serializers
    )
    assert "numpy.npy.v1" not in manifest.unsupported_serializers

    changed = await manager.execute(
        chat_id="chat-capsule",
        code="text_value = 'changed'\nstale_value = 99",
        run_id="run-capsule-change",
        outer_tool_call_id="outer-capsule-change",
    )
    assert changed.ok, changed.to_dict()
    restore = await manager.restore_capsule(
        runtime_chat_id="chat-capsule",
        capsule_ref=manifest.artifact_ref,
    )
    assert restore.namespace_reinstalled
    assert restore.compatibility.compatible
    assert set(restore.restored_names) == {
        "bytes_value",
        "json_value",
        "same_text_a",
        "same_text_b",
        "text_value",
        "tuple_value",
    }

    verified = await manager.execute(
        chat_id="chat-capsule",
        code=(
            "print(text_value)\n"
            "print(bytes_value.hex())\n"
            "print(json_value['items'])\n"
            "print(tuple_value)\n"
            "print('stale' if 'stale_value' in globals() else 'cleared')\n"
            "print(tools.read_file(path='namespace-restored'))"
        ),
        run_id="run-capsule-verify",
        outer_tool_call_id="outer-capsule-verify",
    )
    try:
        assert verified.ok, verified.to_dict()
        text = verified.output.text()
        assert "hello Ω" in text
        assert "00ff" in text
        assert "[1, True, None]" in text
        assert "(1, 2)" in text
        assert "cleared" in text
        assert "namespace-restored" in text
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_host_capsule_round_trips_pandas_through_scoped_cas(kernel_stack):
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    manager, runtimes, artifacts = kernel_stack
    chat_id = "chat-pandas-capsule"
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code=(
            "import pandas as pd\n"
            "frame = pd.DataFrame({'count': pd.array([1, None], dtype='Int64')}, "
            "index=pd.Index(['a', 'b'], name='row'))\n"
            "series = pd.Series([1.5, 2.5], name='metric')\n"
            "portable_label = 'kept'"
        ),
        run_id="run-pandas-capsule",
        outer_tool_call_id="outer-pandas-capsule",
        timeout_s=15,
    )
    assert seeded.ok, seeded.to_dict()
    manifest = await manager.create_capsule(runtime_chat_id=chat_id)
    codecs = {item.name: item.serializer for item in manifest.values}
    assert codecs["frame"] == "pandas.arrow.v1"
    assert codecs["series"] == "pandas.arrow.v1"
    assert artifacts.stat(
        manifest.artifact_ref, scope=chat_id, verify=True
    ).kind == "kernel_capsule_manifest"
    frame_value = next(item for item in manifest.values if item.name == "frame")
    portable_value = next(
        item for item in manifest.values if item.name == "portable_label"
    )
    incompatible_frame = replace(
        frame_value,
        requirements={
            **dict(frame_value.requirements),
            "packages": {
                **dict(frame_value.requirements["packages"]),
                "pandas": "999.0.0",
            },
        },
    )
    partial = manager._capsule_compatibility(
        replace(manifest, values=(portable_value, incompatible_frame)),
        identity=runtimes.ensure_runtime(chat_id).identity,
        workspace_digest=manifest.workspace_digest,
    )
    assert partial.verdict == "partial"
    assert partial.restorable_names == ("portable_label",)
    assert partial.skipped_names == ("frame",)
    repeated = await manager.create_capsule(runtime_chat_id=chat_id)
    assert repeated.parent_capsule_ref == manifest.artifact_ref
    assert repeated.incremental["reused_values"] == len(manifest.values)
    assert repeated.incremental["materialized_values"] == 0
    assert {
        item.name: item.artifact_ref for item in repeated.values
    } == {
        item.name: item.artifact_ref for item in manifest.values
    }

    changed = await manager.execute(
        chat_id=chat_id,
        code="frame = pd.DataFrame({'count': [99]})\nseries = pd.Series([99])",
        run_id="run-pandas-capsule-change",
        outer_tool_call_id="outer-pandas-capsule-change",
    )
    assert changed.ok
    restored = await manager.restore_capsule(
        runtime_chat_id=chat_id,
        capsule_ref=repeated.artifact_ref,
    )
    verified = await manager.execute(
        chat_id=chat_id,
        code=(
            "import pandas as pd\n"
            "assert str(frame['count'].dtype) == 'Int64'\n"
            "assert frame.index.name == 'row'\n"
            "assert frame.loc['a', 'count'] == 1\n"
            "assert pd.isna(frame.loc['b', 'count'])\n"
            "assert series.name == 'metric' and series.tolist() == [1.5, 2.5]\n"
            "print('pandas-capsule-restored')"
        ),
        run_id="run-pandas-capsule-verify",
        outer_tool_call_id="outer-pandas-capsule-verify",
    )
    try:
        assert restored.lineage_persisted is True
        assert restored.lineage_ref.startswith("artifact://sha256/")
        assert verified.ok, verified.to_dict()
        assert "pandas-capsule-restored" in verified.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_incremental_capsules_reuse_cas_without_copying_cell_history(
    kernel_stack,
):
    manager, runtimes, artifacts = kernel_stack
    record = runtimes.ensure_runtime("chat-incremental-capsule", is_new=True)
    created = await manager.execute(
        chat_id="chat-incremental-capsule",
        code="alpha = 'one'\nbeta = {'value': 2}",
        run_id="run-incremental-initial",
        outer_tool_call_id="outer-incremental-initial",
    )
    assert created.ok
    first = await manager.create_capsule(
        runtime_chat_id="chat-incremental-capsule"
    )
    second = await manager.create_capsule(
        runtime_chat_id="chat-incremental-capsule"
    )
    changed = await manager.execute(
        chat_id="chat-incremental-capsule",
        code="alpha = 'changed'\ndel beta\ngamma = (3, 4)",
        run_id="run-incremental-changed",
        outer_tool_call_id="outer-incremental-changed",
    )
    third = await manager.create_capsule(
        runtime_chat_id="chat-incremental-capsule"
    )
    try:
        first_refs = {item.name: item.artifact_ref for item in first.values}
        second_refs = {item.name: item.artifact_ref for item in second.values}
        assert second.parent_capsule_ref == first.artifact_ref
        assert second_refs == first_refs
        assert second.incremental == {
            "known_values": 2,
            "materialized_values": 0,
            "reused_values": 2,
        }
        assert changed.ok
        assert third.cell_sequence == changed.ledger_sequence
        assert third.cell_execution_id == changed.execution_id
        assert "cell_ledger" not in third.to_payload()
        assert "namespace_changes" not in third.to_payload()
        third_refs = {item.name: item.artifact_ref for item in third.values}
        assert third_refs["alpha"] != second_refs["alpha"]
        assert "beta" not in third_refs
        assert third.incremental["reused_values"] == 0

        portable = third.values[0]
        typed = KernelCapsuleValue(
            name="workspace_type",
            type_name="demo.WorkspaceType",
            serializer="dataclass.fields.v1",
            sha256=portable.sha256,
            bytes=portable.bytes,
            artifact_ref=portable.artifact_ref,
            requirements={
                "serializer_revision": 1,
                "portable": False,
                "packages": {},
                "python_major_minor": (
                    f"{os.sys.version_info.major}.{os.sys.version_info.minor}"
                ),
                "workspace_sensitive": True,
            },
        )
        partial_manifest = replace(
            third,
            values=(portable, typed),
            workspace_digest="captured-workspace",
            artifact_refs=(portable.artifact_ref, typed.artifact_ref),
        )
        compatibility = manager._capsule_compatibility(
            partial_manifest,
            identity=record.identity,
            workspace_digest="different-workspace",
        )
        assert compatibility.verdict == "partial"
        assert compatibility.restorable_names == (portable.name,)
        assert compatibility.skipped_names == ("workspace_type",)

        partial_ref = artifacts.put_json(
            partial_manifest.to_payload(),
            kind="kernel_capsule_manifest",
            scope="chat-incremental-capsule",
        )
        partial_restore = await manager.restore_capsule(
            runtime_chat_id="chat-incremental-capsule",
            capsule_ref=partial_ref.ref,
            workspace_digest="different-workspace",
        )
        assert partial_restore.restored_names == (portable.name,)
        assert partial_restore.skipped_names == ("workspace_type",)
        assert partial_restore.compatibility.verdict == "partial"

        incompatible_manifest = replace(
            partial_manifest,
            values=(typed,),
            artifact_refs=(typed.artifact_ref,),
        )
        incompatible_ref = artifacts.put_json(
            incompatible_manifest.to_payload(),
            kind="kernel_capsule_manifest",
            scope="chat-incremental-capsule",
        )
        with pytest.raises(KernelCapsuleError) as incompatible:
            await manager.restore_capsule(
                runtime_chat_id="chat-incremental-capsule",
                capsule_ref=incompatible_ref.ref,
                workspace_digest="different-workspace",
            )
        assert incompatible.value.code == "capsule_incompatible"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_capsule_lineage_and_checkpoint_survive_manager_restart(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    chat_id = "chat-durable-capsule-lineage"
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="durable_value = {'answer': 42}",
        run_id="run-durable-lineage",
        outer_tool_call_id="outer-durable-lineage",
    )
    assert seeded.ok
    first = await manager.create_capsule(runtime_chat_id=chat_id)
    manager.checkpoint_policy = KernelCheckpointPolicy(enabled=True)
    restarted = await manager.restart(chat_id)
    checkpoint = restarted["checkpoint"]
    assert checkpoint["status"] == "captured"
    assert checkpoint["evidence_ref"].startswith("artifact://sha256/")
    checkpoint_ref = checkpoint["capsule_ref"]
    assert checkpoint_ref != first.artifact_ref
    await manager.shutdown()

    replacement = KernelRuntimeManager(
        registry=runtimes,
        broker=manager.broker,
        artifact_store=artifacts,
        catalog_service=manager.catalog_service,
        root=manager.root,
        instance_id="test-instance-restarted",
        app_root=manager.app_root,
        worker_executable=manager.worker_executable,
        limits=manager.limits,
        capsule_limits=manager.capsule_limits,
        checkpoint_policy=KernelCheckpointPolicy(enabled=False),
        runtime_profile_id=manager.runtime_profile.profile_id,
        app_version=manager.app_version,
        cell_ledger_path=manager.cell_ledger.path,
    )
    try:
        durable_status = replacement.status(chat_id)
        assert durable_status["latest_checkpoint"]["status"] == "captured"
        assert durable_status["latest_checkpoint"]["capsule_ref"] == checkpoint_ref
        assert durable_status["latest_checkpoint"]["evidence_ref"] == (
            checkpoint["evidence_ref"]
        )
        assert durable_status["latest_capsule"]["capsule_ref"] == checkpoint_ref
        assert durable_status["latest_capsule"]["lineage_ref"].startswith(
            "artifact://sha256/"
        )

        recreated = await replacement.execute(
            chat_id=chat_id,
            code="durable_value = {'answer': 42}",
            run_id="run-durable-lineage-recreated",
            outer_tool_call_id="outer-durable-lineage-recreated",
        )
        assert recreated.ok
        next_capsule = await replacement.create_capsule(runtime_chat_id=chat_id)
        assert next_capsule.parent_capsule_ref == checkpoint_ref
        assert next_capsule.incremental["reused_values"] == 1
        assert next_capsule.incremental["materialized_values"] == 0
    finally:
        await replacement.shutdown()


@pytest.mark.asyncio
async def test_restore_discloses_capsule_lineage_persistence_failure(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    chat_id = "chat-capsule-lineage-failure"
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="lineage_value = 'original'",
        run_id="run-lineage-failure",
        outer_tool_call_id="outer-lineage-failure",
    )
    assert seeded.ok
    manifest = await manager.create_capsule(runtime_chat_id=chat_id)
    changed = await manager.execute(
        chat_id=chat_id,
        code="lineage_value = 'changed'",
        run_id="run-lineage-failure-change",
        outer_tool_call_id="outer-lineage-failure-change",
    )
    assert changed.ok

    class FailingLineageArtifacts:
        def __getattr__(self, name):
            return getattr(artifacts, name)

        def put_json(self, value, *, kind, scope=""):
            if kind == "kernel_capsule_pointer":
                raise OSError(28, "simulated lineage pointer disk full")
            return artifacts.put_json(value, kind=kind, scope=scope)

    manager.artifact_store = FailingLineageArtifacts()
    restored = await manager.restore_capsule(
        runtime_chat_id=chat_id,
        capsule_ref=manifest.artifact_ref,
    )
    manager.artifact_store = artifacts
    verified = await manager.execute(
        chat_id=chat_id,
        code="print(lineage_value)",
        run_id="run-lineage-failure-verify",
        outer_tool_call_id="outer-lineage-failure-verify",
    )
    try:
        assert restored.lineage_persisted is False
        assert restored.lineage_ref == ""
        assert "simulated lineage pointer disk full" in restored.lineage_error
        assert verified.ok
        assert "original" in verified.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_opt_in_auto_restore_runs_before_first_cell(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-auto-restore"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="auto_restore_value = {'answer': 42}",
        run_id="run-auto-restore-seed",
        outer_tool_call_id="outer-auto-restore-seed",
    )
    previous_generation = seeded.generation
    restarted = await manager.restart(chat_id)
    assert restarted["checkpoint"]["status"] == "captured"

    resumed = await manager.execute(
        chat_id=chat_id,
        code="print(auto_restore_value['answer'])",
        run_id="run-auto-restore-resumed",
        outer_tool_call_id="outer-auto-restore-resumed",
    )
    status = manager.status(chat_id)
    try:
        assert resumed.ok, resumed.to_dict()
        assert resumed.generation > previous_generation
        assert "42" in resumed.output.text()
        assert status["latest_auto_restore"]["status"] == "restored"
        assert status["latest_auto_restore"]["capsule_ref"] == (
            restarted["checkpoint"]["capsule_ref"]
        )
        assert status["latest_auto_restore"]["restored_names"] == [
            "auto_restore_value"
        ]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_auto_restore_reuses_exact_versioned_multi_root_lease(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-auto-restore-versioned-roots"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    first_root = os.path.join(manager.app_root, "workspace-a")
    second_root = os.path.join(manager.app_root, "workspace-b")
    os.makedirs(first_root, exist_ok=True)
    os.makedirs(second_root, exist_ok=True)
    roots = (first_root, second_root)
    scope = {"chat_id": chat_id, "workspace_revision": 7}

    seeded = await manager.execute(
        chat_id=chat_id,
        code="versioned_restore_value = {'answer': 84}",
        run_id="run-versioned-restore-seed",
        outer_tool_call_id="outer-versioned-restore-seed",
        workspace_roots=roots,
        work_scope=scope,
    )
    restarted = await manager.restart(chat_id)
    assert restarted["checkpoint"]["status"] == "captured"

    try:
        resumed = await asyncio.wait_for(
            manager.execute(
                chat_id=chat_id,
                code="print(versioned_restore_value['answer'])",
                run_id="run-versioned-restore-resumed",
                outer_tool_call_id="outer-versioned-restore-resumed",
                workspace_roots=roots,
                work_scope=scope,
            ),
            timeout=15.0,
        )
        status = manager.status(chat_id)
        assert resumed.ok, resumed.to_dict()
        assert "84" in resumed.output.text()
        assert status["workspace_roots"] == list(roots)
        assert status["workspace_revision"] == 7
        assert status["latest_auto_restore"]["status"] == "restored"
        assert resumed.generation > seeded.generation
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_kernel_waiter_cannot_boot_after_shutdown_starts(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-shutdown-boot-waiter"
    runtimes.ensure_runtime(chat_id, is_new=True)
    lock = manager._boot_locks.setdefault(chat_id, asyncio.Lock())
    await lock.acquire()
    waiter = asyncio.create_task(manager.execute(
        chat_id=chat_id,
        code="print('must not run')",
        run_id="run-shutdown-waiter",
        outer_tool_call_id="outer-shutdown-waiter",
    ))
    await asyncio.sleep(0)
    assert not waiter.done()

    await manager.shutdown()
    lock.release()
    with pytest.raises(KernelUnavailable, match="shutting down"):
        await waiter
    assert manager._leases == {}


@pytest.mark.asyncio
async def test_opt_in_auto_restore_records_absent_checkpoint_and_runs(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-auto-restore-absent"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    result = await manager.execute(
        chat_id=chat_id,
        code="print('fresh-without-checkpoint')",
        run_id="run-auto-restore-absent",
        outer_tool_call_id="outer-auto-restore-absent",
    )
    status = manager.status(chat_id)
    try:
        assert result.ok
        assert "fresh-without-checkpoint" in result.output.text()
        assert status["latest_auto_restore"]["status"] == "skipped"
        assert status["latest_auto_restore"]["reason"] == "checkpoint_absent"
        assert status["latest_auto_restore"]["evidence_ref"].startswith(
            "artifact://sha256/"
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_auto_restore_read_failure_blocks_first_cell(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    chat_id = "chat-auto-restore-read-failure"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="blocked_restore_value = 'must-survive'",
        run_id="run-auto-restore-failure-seed",
        outer_tool_call_id="outer-auto-restore-failure-seed",
    )
    assert seeded.ok
    restarted = await manager.restart(chat_id)
    manifest = manager._load_capsule_manifest(
        runtime_chat_id=chat_id,
        capsule_ref=restarted["checkpoint"]["capsule_ref"],
        verify_values=False,
    )
    value_refs = {item.artifact_ref for item in manifest.values}
    before = len(manager.execution_history(chat_id)["items"])

    class FailingRestoreReads:
        def __getattr__(self, name):
            return getattr(artifacts, name)

        def read_bytes_scoped(self, ref, scope):
            if str(ref) in value_refs:
                raise OSError(5, "simulated checkpoint value read failure")
            return artifacts.read_bytes_scoped(ref, scope)

    manager.artifact_store = FailingRestoreReads()
    with pytest.raises(KernelAutoRestoreError) as failed:
        await manager.execute(
            chat_id=chat_id,
            code="print('must-not-run')",
            run_id="run-auto-restore-failure",
            outer_tool_call_id="outer-auto-restore-failure",
        )
    manager.artifact_store = artifacts
    status = manager.status(chat_id)
    try:
        assert failed.value.code == "capsule_value_unavailable"
        assert status["state"] == "absent"
        assert status["latest_auto_restore"]["status"] == "failed"
        assert status["latest_auto_restore"]["reason"] == "capsule_value_unavailable"
        assert len(manager.execution_history(chat_id)["items"]) == before
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_auto_restore_rejects_partial_value_compatibility(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    chat_id = "chat-auto-restore-incompatible"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="compatible_value = 'portable'",
        run_id="run-auto-restore-incompatible-seed",
        outer_tool_call_id="outer-auto-restore-incompatible-seed",
    )
    assert seeded.ok
    restarted = await manager.restart(chat_id)
    manifest = manager._load_capsule_manifest(
        runtime_chat_id=chat_id,
        capsule_ref=restarted["checkpoint"]["capsule_ref"],
        verify_values=True,
    )
    original = manifest.values[0]
    incompatible = replace(
        original,
        requirements={
            **dict(original.requirements),
            "packages": {"pandas": "999.0.0"},
        },
    )
    incompatible_manifest = replace(
        manifest,
        capsule_id="capsule_auto_restore_incompatible",
        values=(incompatible,),
        artifact_refs=(incompatible.artifact_ref,),
        artifact_ref="",
    )
    incompatible_ref = artifacts.put_json(
        incompatible_manifest.to_payload(),
        kind="kernel_capsule_manifest",
        scope=chat_id,
    )
    incompatible_manifest = incompatible_manifest.with_artifact_ref(
        incompatible_ref.ref
    )
    manager._record_capsule_pointer(
        incompatible_manifest, reason="test_incompatible"
    )
    manager._persist_checkpoint_outcome({
        "schema": "variant1.kernel-checkpoint-outcome.v1",
        "runtime_chat_id": chat_id,
        "kernel_generation": int(manifest.kernel_generation),
        "boundary": "operator_restart",
        "status": "captured",
        "reason": "",
        "capsule_ref": incompatible_ref.ref,
    })
    with pytest.raises(KernelAutoRestoreError) as failed:
        await manager.execute(
            chat_id=chat_id,
            code="print('must-not-run')",
            run_id="run-auto-restore-incompatible",
            outer_tool_call_id="outer-auto-restore-incompatible",
        )
    status = manager.status(chat_id)
    try:
        assert failed.value.code == "auto_restore_exact_compatibility_required"
        assert status["state"] == "absent"
        assert status["latest_auto_restore"]["reason"] == (
            "auto_restore_exact_compatibility_required"
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_auto_restore_fences_checkpoint_generation(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-auto-restore-generation-fence"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="generation_value = 'must-not-cross-fence'",
        run_id="run-auto-restore-generation-seed",
        outer_tool_call_id="outer-auto-restore-generation-seed",
    )
    assert seeded.ok
    restarted = await manager.restart(chat_id)
    checkpoint = dict(restarted["checkpoint"])
    checkpoint["kernel_generation"] = int(checkpoint["kernel_generation"]) + 1
    manager._persist_checkpoint_outcome(checkpoint)
    before = len(manager.execution_history(chat_id)["items"])

    with pytest.raises(KernelAutoRestoreError) as failed:
        await manager.execute(
            chat_id=chat_id,
            code="print('must-not-run')",
            run_id="run-auto-restore-generation-fence",
            outer_tool_call_id="outer-auto-restore-generation-fence",
        )
    status = manager.status(chat_id)
    try:
        assert failed.value.code == "auto_restore_generation_fence"
        assert status["state"] == "absent"
        assert status["latest_auto_restore"]["reason"] == (
            "auto_restore_generation_fence"
        )
        details = status["latest_auto_restore"]["details"]
        assert details["checkpoint_generation"] == seeded.generation + 1
        assert details["capsule_generation"] == seeded.generation
        assert details["target_generation"] == seeded.generation + 1
        assert len(manager.execution_history(chat_id)["items"]) == before
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelled_auto_restore_closes_partial_generation(
    kernel_stack, monkeypatch
):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-auto-restore-cancelled"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="cancelled_restore_value = 'must-not-be-partial'",
        run_id="run-auto-restore-cancelled-seed",
        outer_tool_call_id="outer-auto-restore-cancelled-seed",
    )
    assert seeded.ok
    restarted = await manager.restart(chat_id)
    assert restarted["checkpoint"]["status"] == "captured"
    before = len(manager.execution_history(chat_id)["items"])

    async def cancel_restore(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "restore_capsule", cancel_restore)
    with pytest.raises(asyncio.CancelledError):
        await manager.execute(
            chat_id=chat_id,
            code="print('must-not-run')",
            run_id="run-auto-restore-cancelled",
            outer_tool_call_id="outer-auto-restore-cancelled",
        )
    status = manager.status(chat_id)
    try:
        assert status["state"] == "absent"
        assert status["latest_auto_restore"]["status"] == "failed"
        assert status["latest_auto_restore"]["reason"] == "auto_restore_cancelled"
        assert len(manager.execution_history(chat_id)["items"]) == before
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_explicit_restore_suppresses_auto_restore_of_latest_checkpoint(
    kernel_stack,
):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-manual-restore-suppression"
    manager.checkpoint_policy = KernelCheckpointPolicy(
        enabled=True, restore_on_boot=True
    )
    runtimes.ensure_runtime(chat_id, is_new=True)
    older_seed = await manager.execute(
        chat_id=chat_id,
        code="selected_value = 'older-explicit'",
        run_id="run-manual-restore-older",
        outer_tool_call_id="outer-manual-restore-older",
    )
    assert older_seed.ok
    initial_auto_restore = manager.status(chat_id)["latest_auto_restore"]
    assert initial_auto_restore["reason"] == "checkpoint_absent"
    older = await manager.create_capsule(runtime_chat_id=chat_id)
    latest_seed = await manager.execute(
        chat_id=chat_id,
        code="selected_value = 'latest-checkpoint'",
        run_id="run-manual-restore-latest",
        outer_tool_call_id="outer-manual-restore-latest",
    )
    assert latest_seed.ok
    restarted = await manager.restart(chat_id)
    assert restarted["checkpoint"]["capsule_ref"] != older.artifact_ref

    restored = await manager.restore_capsule(
        runtime_chat_id=chat_id,
        capsule_ref=older.artifact_ref,
    )
    verified = await manager.execute(
        chat_id=chat_id,
        code="print(selected_value)",
        run_id="run-manual-restore-verify",
        outer_tool_call_id="outer-manual-restore-verify",
    )
    status = manager.status(chat_id)
    try:
        assert restored.restored_names == ("selected_value",)
        assert verified.ok
        assert "older-explicit" in verified.output.text()
        assert status["latest_auto_restore"]["evidence_ref"] == (
            initial_auto_restore["evidence_ref"]
        )
        assert status["latest_auto_restore"]["kernel_generation"] == (
            older_seed.generation
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_per_chat_continuity_override_is_durable_isolated_and_resettable(
    kernel_stack,
):
    manager, _runtimes, artifacts = kernel_stack
    chat_id = "chat-continuity-policy"
    other_chat_id = "chat-continuity-other"

    initial = manager.continuity_status(chat_id)
    assert initial["policy"]["source"] == "global_default"
    assert initial["policy"]["effective"]["checkpoint_enabled"] is False
    assert initial["policy"]["effective"]["restore_on_boot"] is False

    configured = manager.configure_continuity(
        chat_id,
        restore_on_boot=True,
        configured_by={"principal_actor_id": "model", "run_id": "run-policy"},
    )
    override = configured["policy"]["override"]
    assert configured["policy"]["source"] == "chat_override"
    assert configured["policy"]["effective"]["checkpoint_enabled"] is True
    assert configured["policy"]["effective"]["restore_on_boot"] is True
    assert override["schema"] == KERNEL_CONTINUITY_POLICY_SCHEMA
    assert override["revision"] == 1
    assert override["configured_by"]["run_id"] == "run-policy"
    assert artifacts.stat(
        override["evidence_ref"], scope=chat_id, verify=True
    ).kind == "kernel_continuity_policy"
    assert manager.continuity_status(other_chat_id)["policy"]["source"] == (
        "global_default"
    )

    with pytest.raises(KernelContinuityError) as conflict:
        manager.configure_continuity(
            chat_id,
            checkpoint_enabled=False,
            restore_on_boot=True,
        )
    assert conflict.value.code == "continuity_policy_conflict"

    repeated = manager.configure_continuity(chat_id, restore_on_boot=True)
    assert repeated["policy"]["override"]["revision"] == 2
    assert repeated["policy"]["override"]["evidence_ref"] != (
        override["evidence_ref"]
    )

    disabled = manager.configure_continuity(
        chat_id,
        checkpoint_enabled=False,
    )
    assert disabled["policy"]["effective"]["checkpoint_enabled"] is False
    assert disabled["policy"]["effective"]["restore_on_boot"] is False
    assert disabled["policy"]["override"]["revision"] == 3

    inherited = manager.configure_continuity(chat_id, inherit_defaults=True)
    try:
        assert inherited["policy"]["source"] == "global_default"
        assert inherited["policy"]["override"]["mode"] == "inherit"
        assert inherited["policy"]["override"]["revision"] == 4
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_continuity_policy_write_failure_does_not_create_runtime_override(
    kernel_stack,
):
    manager, _runtimes, artifacts = kernel_stack
    chat_id = "chat-continuity-policy-write-failure"

    class FailingPolicyArtifacts:
        def __getattr__(self, name):
            return getattr(artifacts, name)

        def put_json(self, value, *, kind, scope=""):
            if kind == "kernel_continuity_policy":
                raise OSError(28, "simulated continuity policy disk full")
            return artifacts.put_json(value, kind=kind, scope=scope)

    manager.artifact_store = FailingPolicyArtifacts()
    with pytest.raises(KernelContinuityError) as failed:
        manager.configure_continuity(chat_id, restore_on_boot=True)
    manager.artifact_store = artifacts
    try:
        assert failed.value.code == "continuity_policy_persistence_failed"
        assert "simulated continuity policy disk full" in (
            failed.value.details["error"]
        )
        assert manager.continuity_status(chat_id)["policy"]["source"] == (
            "global_default"
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_invalid_continuity_evidence_falls_back_off_and_can_be_replaced(
    kernel_stack,
):
    manager, _runtimes, artifacts = kernel_stack
    chat_id = "chat-invalid-continuity-evidence"
    invalid = artifacts.put_json(
        {
            "schema": KERNEL_CONTINUITY_POLICY_SCHEMA,
            "runtime_chat_id": chat_id,
            "mode": "override",
            "checkpoint_enabled": False,
            "restore_on_boot": True,
        },
        kind="kernel_continuity_policy",
        scope=chat_id,
    )
    fallback = manager.continuity_status(chat_id)
    assert fallback["policy"]["source"] == "global_fallback"
    assert fallback["policy"]["effective"]["checkpoint_enabled"] is False
    assert fallback["policy"]["effective"]["restore_on_boot"] is False
    assert fallback["policy"]["override"]["mode"] == "unavailable"
    assert fallback["policy"]["override"]["evidence_ref"] == invalid.ref

    repaired = manager.configure_continuity(chat_id, checkpoint_enabled=True)
    try:
        assert repaired["policy"]["source"] == "chat_override"
        assert repaired["policy"]["override"]["revision"] == 1
        assert repaired["policy"]["effective"]["checkpoint_enabled"] is True
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_per_chat_continuity_policy_survives_manager_restart_and_restores(
    kernel_stack,
):
    manager, runtimes, artifacts = kernel_stack
    chat_id = "chat-durable-continuity-policy"
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.configure_continuity(
        chat_id,
        checkpoint_enabled=True,
        restore_on_boot=True,
    )
    seeded = await manager.execute(
        chat_id=chat_id,
        code="durable_continuity_value = {'answer': 42}",
        run_id="run-durable-continuity-seed",
        outer_tool_call_id="outer-durable-continuity-seed",
    )
    assert seeded.ok
    restarted = await manager.restart(chat_id)
    assert restarted["checkpoint"]["status"] == "captured"
    checkpoint_ref = restarted["checkpoint"]["capsule_ref"]
    await manager.shutdown()

    replacement = KernelRuntimeManager(
        registry=runtimes,
        broker=manager.broker,
        artifact_store=artifacts,
        catalog_service=manager.catalog_service,
        root=manager.root,
        instance_id="test-instance-continuity-replacement",
        app_root=manager.app_root,
        worker_executable=manager.worker_executable,
        limits=manager.limits,
        capsule_limits=manager.capsule_limits,
        checkpoint_policy=KernelCheckpointPolicy(enabled=False),
        runtime_profile_id=manager.runtime_profile.profile_id,
        app_version=manager.app_version,
        cell_ledger_path=manager.cell_ledger.path,
    )
    try:
        recovered = replacement.continuity_status(chat_id)
        assert recovered["policy"]["source"] == "chat_override"
        assert recovered["policy"]["effective"] == {
            "checkpoint_enabled": True,
            "restore_on_boot": True,
            "reasons": list(manager.checkpoint_policy.reasons),
        }
        assert recovered["policy"]["override"]["evidence_ref"].startswith(
            "artifact://sha256/"
        )
        resumed = await replacement.execute(
            chat_id=chat_id,
            code="print(durable_continuity_value['answer'])",
            run_id="run-durable-continuity-resume",
            outer_tool_call_id="outer-durable-continuity-resume",
        )
        assert resumed.ok, resumed.to_dict()
        assert "42" in resumed.output.text()
        status = replacement.status(chat_id)
        assert status["latest_auto_restore"]["status"] == "restored"
        assert status["latest_auto_restore"]["capsule_ref"] == checkpoint_ref
    finally:
        await replacement.shutdown()


@pytest.mark.asyncio
async def test_explicit_checkpoint_recovers_last_requested_namespace(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    chat_id = "chat-explicit-continuity-checkpoint"
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.configure_continuity(
        chat_id,
        checkpoint_enabled=True,
        restore_on_boot=True,
    )
    seeded = await manager.execute(
        chat_id=chat_id,
        code="explicit_checkpoint_value = 'captured'",
        run_id="run-explicit-checkpoint-seed",
        outer_tool_call_id="outer-explicit-checkpoint-seed",
    )
    assert seeded.ok
    checkpoint = await manager.checkpoint(
        chat_id, reason="model_requested_checkpoint"
    )
    assert checkpoint["status"] == "captured"
    assert checkpoint["boundary"] == "model_requested_checkpoint"
    changed = await manager.execute(
        chat_id=chat_id,
        code="explicit_checkpoint_value = 'not-captured'",
        run_id="run-explicit-checkpoint-change",
        outer_tool_call_id="outer-explicit-checkpoint-change",
    )
    assert changed.ok
    await manager._leases[chat_id].close(reason="simulated_crash", hard=True)

    resumed = await manager.execute(
        chat_id=chat_id,
        code="print(explicit_checkpoint_value)",
        run_id="run-explicit-checkpoint-resume",
        outer_tool_call_id="outer-explicit-checkpoint-resume",
    )
    try:
        assert resumed.ok, resumed.to_dict()
        assert "captured" in resumed.output.text()
        assert "not-captured" not in resumed.output.text()
        assert manager.status(chat_id)["latest_auto_restore"]["capsule_ref"] == (
            checkpoint["capsule_ref"]
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_kernel_checkpoint_work_handler_waits_for_idle_owner(
    kernel_stack, monkeypatch
):
    manager, runtimes, artifacts = kernel_stack
    chat_id = "chat-checkpoint-job-handler"
    runtimes.ensure_runtime(chat_id, is_new=True)
    seeded = await manager.execute(
        chat_id=chat_id,
        code="checkpoint_job_value = 7",
        run_id="run-checkpoint-job-seed",
        outer_tool_call_id="outer-checkpoint-job-seed",
    )
    assert seeded.ok

    class FakeWork:
        def __init__(self):
            self.handlers = {}

        def register_job_handler(self, kind, handler):
            self.handlers[kind] = handler

    work = FakeWork()
    composed = SimpleNamespace(
        work=work,
        kernel=manager,
        session_artifacts=artifacts,
    )
    host = SimpleNamespace(
        kernel_runtime=manager,
        require_runtime=lambda: composed,
        session_artifacts=artifacts,
    )
    register_kernel_control_job_handlers(host)
    assert set(work.handlers) == {
        KERNEL_CHECKPOINT_JOB,
        KERNEL_RESTART_JOB,
    }
    execution = SimpleNamespace(job=SimpleNamespace(input_manifest={
        "runtime_chat_id": chat_id,
        "reason": "model_job_checkpoint",
    }))
    original_status = manager.status
    monkeypatch.setattr(manager, "status", lambda _chat_id: {"state": "busy"})
    with pytest.raises(RetryJob):
        await work.handlers[KERNEL_CHECKPOINT_JOB](execution)
    monkeypatch.setattr(manager, "status", original_status)
    result = await work.handlers[KERNEL_CHECKPOINT_JOB](execution)
    try:
        assert result.progress["status"] == "captured"
        assert result.progress["boundary"] == "model_job_checkpoint"
        assert artifacts.stat(
            result.result_ref, scope=chat_id, verify=True
        ).kind == "kernel_control_result"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_opt_in_restart_checkpoint_is_captured_and_restorable(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    manager.checkpoint_policy = KernelCheckpointPolicy(enabled=True)
    runtimes.ensure_runtime("chat-checkpoint", is_new=True)
    seeded = await manager.execute(
        chat_id="chat-checkpoint",
        code="checkpoint_value = {'answer': 42}",
        run_id="run-checkpoint",
        outer_tool_call_id="outer-checkpoint",
    )
    assert seeded.ok

    restarted = await manager.restart("chat-checkpoint")
    checkpoint = restarted["checkpoint"]
    assert checkpoint["status"] == "captured"
    assert checkpoint["capsule_ref"].startswith("artifact://sha256/")
    absent = manager.status("chat-checkpoint")
    assert absent["state"] == "absent"
    assert absent["latest_checkpoint"] == checkpoint

    restored = await manager.restore_capsule(
        runtime_chat_id="chat-checkpoint",
        capsule_ref=checkpoint["capsule_ref"],
    )
    verified = await manager.execute(
        chat_id="chat-checkpoint",
        code="print(checkpoint_value['answer'])",
        run_id="run-checkpoint-verify",
        outer_tool_call_id="outer-checkpoint-verify",
    )
    try:
        assert restored.restored_names == ("checkpoint_value",)
        assert verified.ok
        assert "42" in verified.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_checkpoint_outcomes_disclose_skipped_failed_and_hard_failure(
    kernel_stack,
):
    manager, runtimes, artifacts = kernel_stack

    runtimes.ensure_runtime("chat-checkpoint-skipped", is_new=True)
    skipped_seed = await manager.execute(
        chat_id="chat-checkpoint-skipped",
        code="value = 'skip'",
        run_id="run-checkpoint-skip",
        outer_tool_call_id="outer-checkpoint-skip",
    )
    assert skipped_seed.ok
    skipped = await manager.restart("chat-checkpoint-skipped")
    assert skipped["checkpoint"]["status"] == "skipped"
    assert skipped["checkpoint"]["reason"] == "policy_disabled"

    manager.checkpoint_policy = KernelCheckpointPolicy(enabled=True)
    runtimes.ensure_runtime("chat-checkpoint-failed", is_new=True)
    failed_seed = await manager.execute(
        chat_id="chat-checkpoint-failed",
        code="value = 'cannot-persist'",
        run_id="run-checkpoint-failed",
        outer_tool_call_id="outer-checkpoint-failed",
    )
    assert failed_seed.ok

    class FailingCheckpointArtifacts:
        def __getattr__(self, name):
            return getattr(artifacts, name)

        def put_bytes(self, *_args, **_kwargs):
            raise OSError(28, "simulated checkpoint disk full")

    manager.artifact_store = FailingCheckpointArtifacts()
    failed = await manager.restart("chat-checkpoint-failed")
    manager.artifact_store = artifacts
    assert failed["checkpoint"]["status"] == "failed"
    assert failed["checkpoint"]["reason"] == "capsule_artifact_write_failed"
    assert failed["checkpoint"]["capsule_ref"] is None

    runtimes.ensure_runtime("chat-checkpoint-hard", is_new=True)
    hard_seed = await manager.execute(
        chat_id="chat-checkpoint-hard",
        code="value = 'lost-with-worker'",
        run_id="run-checkpoint-hard",
        outer_tool_call_id="outer-checkpoint-hard",
    )
    assert hard_seed.ok
    lease = manager._leases["chat-checkpoint-hard"]
    assert lease.process is not None
    lease.process.kill()
    await asyncio.to_thread(lease.process.wait)
    assert await manager.close_chat("chat-checkpoint-hard")
    hard = manager.status("chat-checkpoint-hard")["latest_checkpoint"]
    try:
        assert hard["status"] == "failed"
        assert hard["reason"] == "kernel_process_died"
        assert hard["capsule_ref"] is None
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cell_ledger_namespace_notebook_and_restart_controls(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    runtimes.ensure_runtime("chat-ledger", is_new=True)
    result = await manager.execute(
        chat_id="chat-ledger",
        code="ledger_value = {'answer': 42}\nprint('ledger-ok')",
        run_id="run-ledger",
        outer_tool_call_id="outer-ledger",
        work_scope={
            "chat_id": "chat-ledger",
            "workspace_id": "workspace-ledger",
            "workspace_revision": 7,
        },
    )
    try:
        assert result.ok, result.to_dict()
        assert result.ledger_sequence > 0
        assert result.source_ref.startswith("artifact://sha256/")
        assert result.result_ref.startswith("artifact://sha256/")

        history = manager.execution_history("chat-ledger", limit=10)
        assert history["items"][0]["execution_id"] == result.execution_id
        assert history["items"][0]["workspace_revision"] == 7
        assert history["items"][0]["work_scope"]["workspace_id"] == "workspace-ledger"

        namespace = await manager.bounded_namespace_view("chat-ledger", limit=100)
        assert any(item["name"] == "ledger_value" for item in namespace["values"])
        assert namespace["inspection_mode"] == "metadata_only"
        ledger_entry = next(
            item for item in namespace["values"] if item["name"] == "ledger_value"
        )
        assert ledger_entry["bytes"] is None
        live_status = manager.status("chat-ledger")
        assert live_status["runtime_profile"]["profile_id"] == "core.v1"
        assert len(live_status["runtime_profile"]["digest"]) == 64
        assert live_status["namespace_sync"]["performed"] == 0
        assert live_status["namespace_sync"]["skipped"] >= 1
        resources = live_status["resources"]
        assert resources["process"]["rss_bytes"] > 0
        assert resources["process"]["cpu_user_s"] >= 0
        assert resources["pressure"]["process_memory_ratio"] > 0
        assert any(
            item["name"] == "ledger_value"
            for item in resources["namespace"]["contributors"]
        ), resources

        notebook = manager.export_notebook("chat-ledger")
        exported = json.loads(
            artifacts.read_bytes_scoped(
                notebook["artifact_ref"], "chat-ledger"
            ).decode("utf-8")
        )
        assert exported["nbformat"] == 4
        assert "ledger-ok" in "".join(
            exported["cells"][0]["outputs"][0]["text"]
        )

        control = await manager.restart("chat-ledger")
        assert control["status"] == "closed"
        assert manager.status("chat-ledger")["state"] == "absent"
        follow = await manager.execute(
            chat_id="chat-ledger",
            code="print('fresh-generation')",
            run_id="run-ledger-next",
            outer_tool_call_id="outer-ledger-next",
            work_scope={"workspace_revision": 7},
        )
        assert follow.ok
        assert follow.generation > result.generation
        assert len(manager.execution_history("chat-ledger", limit=10)["items"]) == 2
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_typed_output_evidence_preserves_order_mime_updates_and_native_errors(
    kernel_stack,
):
    manager, runtimes, artifacts = kernel_stack
    runtimes.ensure_runtime("chat-output-events", is_new=True)
    updated = await manager.execute(
        chat_id="chat-output-events",
        code=(
            "import sys\n"
            "sys.stdout.write('stdout-before\\n'); sys.stdout.flush()\n"
            "sys.stderr.write('stderr-before\\n'); sys.stderr.flush()\n"
            "handle = display({\n"
            "    'text/plain': 'old-plain',\n"
            "    'text/html': '<b>old-html</b>',\n"
            "    'application/json': {'version': 1},\n"
            "}, raw=True, metadata={'phase': 'old'}, display_id='phase3-display')\n"
            "handle.update({\n"
            "    'text/plain': 'new-plain',\n"
            "    'text/html': '<b>new-html</b>',\n"
            "    'application/json': {'version': 2},\n"
            "}, raw=True, metadata={'phase': 'new'})\n"
        ),
        run_id="run-output-update",
        outer_tool_call_id="outer-output-update",
    )
    failed = await manager.execute(
        chat_id="chat-output-events",
        code="print('before-error')\nraise ValueError('typed-boom')",
        run_id="run-output-error",
        outer_tool_call_id="outer-output-error",
    )
    try:
        assert updated.ok, updated.to_dict()
        assert failed.status == "error"
        assert updated.output_evidence_ref.startswith("artifact://sha256/")
        evidence = json.loads(
            artifacts.read_bytes_scoped(
                updated.output_evidence_ref, "chat-output-events"
            ).decode("utf-8")
        )
        assert evidence["schema"] == OUTPUT_EVENT_SCHEMA
        assert [event["sequence"] for event in evidence["events"]] == list(
            range(1, len(evidence["events"]) + 1)
        )
        event_types = [event["type"] for event in evidence["events"]]
        assert event_types == [
            "stream",
            "stream",
            "display_data",
            "update_display_data",
        ]
        initial_display = evidence["events"][2]
        updated_display = evidence["events"][3]
        assert set(initial_display["data"]) == {
            "application/json",
            "text/html",
            "text/plain",
        }
        assert initial_display["display_id"] == "phase3-display"
        assert updated_display["display_id"] == "phase3-display"
        assert '"version": 2' in updated.output.text()
        assert '"version": 1' not in updated.output.text()

        failed_evidence = json.loads(
            artifacts.read_bytes_scoped(
                failed.output_evidence_ref, "chat-output-events"
            ).decode("utf-8")
        )
        failed_types = [event["type"] for event in failed_evidence["events"]]
        assert failed_types[-1] == "error"
        assert set(failed_types[:-1]) == {"stream"}

        notebook = manager.export_notebook("chat-output-events")
        exported = json.loads(
            artifacts.read_bytes_scoped(
                notebook["artifact_ref"], "chat-output-events"
            ).decode("utf-8")
        )
        first_outputs = exported["cells"][0]["outputs"]
        assert [output["output_type"] for output in first_outputs] == [
            "stream",
            "stream",
            "display_data",
        ]
        assert [output["name"] for output in first_outputs[:2]] == [
            "stdout",
            "stderr",
        ]
        display_output = first_outputs[2]
        assert display_output["data"] == {
            "application/json": {"version": 2},
            "text/html": "<b>new-html</b>",
            "text/plain": "new-plain",
        }
        assert display_output["metadata"]["phase"] == "new"
        assert display_output["metadata"]["variant1"]["display_id"] == (
            "phase3-display"
        )
        assert exported["cells"][0]["metadata"]["variant1"][
            "output_evidence_ref"
        ] == updated.output_evidence_ref
        error_output = exported["cells"][1]["outputs"][-1]
        assert error_output["output_type"] == "error"
        assert error_output["ename"] == "ValueError"
        assert error_output["evalue"] == "typed-boom"
        assert any("ValueError" in row for row in error_output["traceback"])
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_clear_output_and_artifact_bodies_round_trip_to_notebook(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    runtimes.ensure_runtime("chat-output-roundtrip", is_new=True)
    cleared = await manager.execute(
        chat_id="chat-output-roundtrip",
        code=(
            "import sys\n"
            "print('removed-output', flush=True)\n"
            "clear_output(wait=True)\n"
            "sys.stderr.write('surviving-output\\n'); sys.stderr.flush()\n"
        ),
        run_id="run-output-clear",
        outer_tool_call_id="outer-output-clear",
    )
    large = await manager.execute(
        chat_id="chat-output-roundtrip",
        code="print('Z' * 20000)",
        run_id="run-output-large",
        outer_tool_call_id="outer-output-large",
    )
    image_payload = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A"
        "AQUBAScY42YAAAAASUVORK5CYII="
    )
    rich = await manager.execute(
        chat_id="chat-output-roundtrip",
        code=(
            f"display({{'text/plain': 'pixel-fallback', 'image/png': "
            f"'{image_payload}'}}, raw=True)\n"
        ),
        run_id="run-output-image",
        outer_tool_call_id="outer-output-image",
    )
    expression = await manager.execute(
        chat_id="chat-output-roundtrip",
        code="{'answer': 42}",
        run_id="run-output-expression",
        outer_tool_call_id="outer-output-expression",
    )
    try:
        assert cleared.ok and large.ok and rich.ok and expression.ok
        assert "surviving-output" in cleared.output.text()
        assert "removed-output" not in cleared.output.text()

        clear_evidence = json.loads(
            artifacts.read_bytes_scoped(
                cleared.output_evidence_ref, "chat-output-roundtrip"
            ).decode("utf-8")
        )
        clear_types = [event["type"] for event in clear_evidence["events"]]
        assert clear_types[-2:] == ["clear_output", "stream"]
        assert set(clear_types[:-2]) == {"stream"}
        clear_event = next(
            event for event in clear_evidence["events"]
            if event["type"] == "clear_output"
        )
        assert clear_event["wait"] is True

        large_evidence = json.loads(
            artifacts.read_bytes_scoped(
                large.output_evidence_ref, "chat-output-roundtrip"
            ).decode("utf-8")
        )
        large_streams = [
            event for event in large_evidence["events"]
            if event["type"] == "stream"
        ]
        assert any(
            event["body"]["storage"] == "artifact" for event in large_streams
        )
        reconstructed_stream = []
        for event in large_streams:
            body = event["body"]
            if body["storage"] == "artifact":
                reconstructed_stream.append(
                    artifacts.read_bytes_scoped(
                        body["artifact"]["ref"], "chat-output-roundtrip"
                    ).decode("utf-8")
                )
            else:
                reconstructed_stream.append(str(body.get("data") or ""))
        assert "".join(reconstructed_stream) == ("Z" * 20000) + "\n"
        assert large.output.truncated

        rich_evidence = json.loads(
            artifacts.read_bytes_scoped(
                rich.output_evidence_ref, "chat-output-roundtrip"
            ).decode("utf-8")
        )
        rich_bundle = rich_evidence["events"][0]["data"]
        assert set(rich_bundle) == {"image/png", "text/plain"}
        assert rich_bundle["image/png"]["storage"] == "artifact"
        expression_evidence = json.loads(
            artifacts.read_bytes_scoped(
                expression.output_evidence_ref, "chat-output-roundtrip"
            ).decode("utf-8")
        )
        assert expression_evidence["events"][0]["type"] == "execute_result"
        assert expression_evidence["events"][0]["execution_count"] == (
            expression.execution_count
        )

        notebook = manager.export_notebook("chat-output-roundtrip")
        exported = json.loads(
            artifacts.read_bytes_scoped(
                notebook["artifact_ref"], "chat-output-roundtrip"
            ).decode("utf-8")
        )
        assert exported["cells"][0]["outputs"] == [{
            "name": "stderr",
            "output_type": "stream",
            "text": ["surviving-output\n"],
        }]
        assert "".join(
            piece
            for output in exported["cells"][1]["outputs"]
            for piece in output["text"]
        ) == (("Z" * 20000) + "\n")
        display_output = exported["cells"][2]["outputs"][0]
        assert display_output["data"]["text/plain"] == "pixel-fallback"
        assert display_output["data"]["image/png"] == image_payload
        execute_output = exported["cells"][3]["outputs"][0]
        assert execute_output["output_type"] == "execute_result"
        assert execute_output["execution_count"] == expression.execution_count
        assert execute_output["data"]["text/plain"] == "{'answer': 42}"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_worker_bounds_stream_frames_and_oversized_rich_bundle(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    manager.limits = replace(
        manager.limits,
        worker_stream_chunk_bytes=32,
        worker_stream_cell_bytes=100,
        worker_rich_message_bytes=100,
    )
    runtimes.ensure_runtime("chat-worker-output-bounds", is_new=True)
    result = await manager.execute(
        chat_id="chat-worker-output-bounds",
        code=(
            "print('S' * 200)\n"
            "display({'text/plain': 'T' * 200, "
            "'application/json': {'payload': 'J' * 200}}, raw=True)\n"
            "'E' * 200\n"
        ),
        run_id="run-worker-output-bounds",
        outer_tool_call_id="outer-worker-output-bounds",
    )
    try:
        assert result.ok, result.to_dict()
        evidence = json.loads(
            artifacts.read_bytes_scoped(
                result.output_evidence_ref, "chat-worker-output-bounds"
            ).decode("utf-8")
        )
        stream_events = [
            event for event in evidence["events"] if event["type"] == "stream"
        ]
        assert len(stream_events) > 1
        assert all(event["body"]["bytes"] <= 32 for event in stream_events[:-1])
        stream_parts = []
        for event in stream_events:
            body = event["body"]
            if body.get("storage") == "artifact":
                stream_parts.append(
                    artifacts.read_bytes_scoped(
                        body["artifact"]["ref"], "chat-worker-output-bounds"
                    ).decode("utf-8")
                )
            else:
                stream_parts.append(str(body.get("data") or ""))
        stream_text = "".join(stream_parts)
        assert "worker stream output capped before REPL transport" in stream_text
        assert "S" * 150 not in stream_text

        display_event = next(
            event for event in evidence["events"] if event["type"] == "display_data"
        )
        assert set(display_event["data"]) == {"text/plain"}
        marker = display_event["data"]["text/plain"]["data"]
        assert "rich output omitted before REPL transport" in marker
        metadata = display_event["metadata"]["data"]
        assert len(metadata["variant1"]["worker_omitted_mime"]) == 2
        execute_event = next(
            event for event in evidence["events"]
            if event["type"] == "execute_result"
        )
        assert "rich output omitted before REPL transport" in (
            execute_event["data"]["text/plain"]["data"]
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_worker_coalesces_bursty_small_prints_without_stalling(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    runtimes.ensure_runtime("chat-worker-print-burst", is_new=True)

    result = await asyncio.wait_for(
        manager.execute(
            chat_id="chat-worker-print-burst",
            code="for index in range(500): print(f'line-{index}')",
            run_id="run-worker-print-burst",
            outer_tool_call_id="outer-worker-print-burst",
        ),
        timeout=20,
    )
    try:
        assert result.ok, result.to_dict()
        evidence = json.loads(
            artifacts.read_bytes_scoped(
                result.output_evidence_ref, "chat-worker-print-burst"
            ).decode("utf-8")
        )
        stream_events = [
            event for event in evidence["events"] if event["type"] == "stream"
        ]
        stream_parts = []
        for event in stream_events:
            body = event["body"]
            if body.get("storage") == "artifact":
                stream_parts.append(
                    artifacts.read_bytes_scoped(
                        body["artifact"]["ref"], "chat-worker-print-burst"
                    ).decode("utf-8")
                )
            else:
                stream_parts.append(str(body.get("data") or ""))
        stream_text = "".join(stream_parts)
        assert "line-0\n" in stream_text
        assert "line-499\n" in stream_text
        assert len(stream_events) < 100
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_workspace_fingerprint_change_fences_kernel_generation(
    kernel_stack, tmp_path
):
    manager, runtimes, _ = kernel_stack
    runtimes.ensure_runtime("chat-workspace-fence", is_new=True)
    first_root = tmp_path / "workspace-a"
    second_root = tmp_path / "workspace-b"
    first_root.mkdir()
    second_root.mkdir()
    first = await manager.execute(
        chat_id="chat-workspace-fence",
        code="workspace_marker = 'first'",
        run_id="run-workspace-a",
        outer_tool_call_id="outer-workspace-a",
        workspace_roots=(str(first_root),),
        work_scope={"workspace_revision": 1},
    )
    second = await manager.execute(
        chat_id="chat-workspace-fence",
        code="print('workspace_marker' in globals())",
        run_id="run-workspace-b",
        outer_tool_call_id="outer-workspace-b",
        workspace_roots=(str(second_root),),
        work_scope={"workspace_revision": 2},
    )
    try:
        assert first.ok and second.ok
        assert second.generation > first.generation
        assert "False" in second.output.text()
        status = manager.status("chat-workspace-fence")
        assert status["workspace_root"] == os.path.abspath(str(second_root))
        expected = manager._workspace_fingerprint((str(second_root),), 2)
        assert status["workspace_fingerprint"] == expected
    finally:
        await manager.shutdown()


def test_project_default_reuses_live_workspace_but_explicit_revision_still_fences(
    tmp_path,
):
    first_root = str((tmp_path / "project-a").resolve())
    second_root = str((tmp_path / "project-b").resolve())
    kernel = SimpleNamespace(status=lambda _chat_id: {
        "state": "ready",
        "workspace_roots": [first_root],
        "workspace_revision": 3,
    })

    roots, scope = preserve_persistent_kernel_workspace(
        kernel, "chat-a", (second_root,), {"chat_id": "chat-a"},
    )
    assert roots == (first_root,)
    assert scope["workspace_revision"] == 3

    explicit_roots, explicit_scope = preserve_persistent_kernel_workspace(
        kernel,
        "chat-a",
        (second_root,),
        {"chat_id": "chat-a", "workspace_revision": 4},
    )
    assert explicit_roots == (second_root,)
    assert explicit_scope["workspace_revision"] == 4


@pytest.mark.asyncio
async def test_chat_project_switch_preserves_live_python_namespace(
    kernel_stack, tmp_path,
):
    manager, runtimes, _ = kernel_stack
    first_root = tmp_path / "project-a"
    second_root = tmp_path / "project-b"
    first_root.mkdir()
    second_root.mkdir()
    sessions = build_chat_sessions(path=str(tmp_path / "conversations.sqlite3"))
    chat_id = sessions.create_session("Project continuity")
    runtime = SimpleNamespace(
        sessions=sessions,
        session_runtimes=runtimes,
        catalog=manager.catalog_service,
        kernel=manager,
    )
    host = SimpleNamespace(
        app_root=str(tmp_path), data_dir=str(tmp_path),
        require_runtime=lambda: runtime,
        emit_activity=lambda *_args, **_kwargs: None,
    )
    session = SimpleNamespace(viewed_session_id=chat_id, interrupt=False)
    register_ipython_tool(manager.broker.registry, lambda: runtime)
    tool = manager.broker.registry.get("ipython")

    try:
        sessions.set_project(chat_id, str(first_root))
        first_context = make_run_context(
            host, "chat", "first", session=session,
            metadata={"chat_id": chat_id},
        )
        with bind_run_context(first_context):
            first = await tool.run({
                "category": "build",
                "code": (
                    "import asyncio, os\n"
                    "project_sentinel = object()\n"
                    "project_sentinel_id = id(project_sentinel)\n"
                    "project_task = asyncio.create_task(asyncio.Event().wait())\n"
                    "project_task_id = id(project_task)\n"
                    "print(f'SENTINEL_ID={project_sentinel_id} TASK_ID={project_task_id} '"
                    "      f'PID={os.getpid()}')"
                ),
            })
        first_status = manager.status(chat_id)
        first_ids = re.search(
            r"SENTINEL_ID=(\d+) TASK_ID=(\d+)", str(first),
        )
        assert first_ids is not None
        sentinel_id, task_id = first_ids.groups()

        sessions.set_project(chat_id, str(second_root))
        second_context = make_run_context(
            host, "chat", "second", session=session,
            metadata={"chat_id": chat_id},
        )
        assert second_context.metadata["working_directory"] == str(
            second_root.resolve()
        )
        with bind_run_context(second_context):
            second = await tool.run({
                "code": (
                    "import asyncio, os\n"
                    "print(f'SENTINEL_ID={id(project_sentinel)} TASK_ID={id(project_task)} '"
                    "      f'TASK_DONE={project_task.done()} PID={os.getpid()}')\n"
                    "project_task.cancel()\n"
                    "try:\n"
                    "    await project_task\n"
                    "except asyncio.CancelledError:\n"
                    "    pass\n"
                    "print('TASK_CANCELLED=' + str(project_task.cancelled()))"
                ),
            })
        second_status = manager.status(chat_id)

        assert first.programmatic_value["status"] == "ok"
        assert second.programmatic_value["status"] == "ok"
        assert f"SENTINEL_ID={sentinel_id} TASK_ID={task_id}" in str(second)
        assert "TASK_DONE=False" in str(second)
        assert "TASK_CANCELLED=True" in str(second)
        assert second_status["pid"] == first_status["pid"]
        assert second_status["generation"] == first_status["generation"]
        assert second_status["workspace_roots"] == [str(first_root.resolve())]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_kernel_capsule_fork_grants_shared_cas_and_restores_target(kernel_stack):
    manager, runtimes, artifacts = kernel_stack
    runtimes.ensure_runtime("chat-capsule-source", is_new=True)
    runtimes.ensure_runtime("chat-capsule-target", is_new=True)
    source = await manager.execute(
        chat_id="chat-capsule-source",
        code="portable = {'answer': 42}\nlabel = 'forked'",
        run_id="run-capsule-source",
        outer_tool_call_id="outer-capsule-source",
    )
    assert source.ok, source.to_dict()

    forked = await manager.fork_capsule(
        source_runtime_chat_id="chat-capsule-source",
        target_runtime_chat_id="chat-capsule-target",
    )
    assert forked.created_capsule
    assert forked.restore.compatibility.compatible
    assert artifacts.stat(
        forked.capsule_ref, scope="chat-capsule-target", verify=True
    ).kind == "kernel_capsule_manifest"
    source_pointer = json.loads(artifacts.read_bytes_scoped(
        manager.continuity._capsule_pointer_refs["chat-capsule-source"],
        "chat-capsule-source",
    ))
    target_pointer = json.loads(artifacts.read_bytes_scoped(
        manager.continuity._capsule_pointer_refs["chat-capsule-target"],
        "chat-capsule-target",
    ))
    assert source_pointer["runtime_chat_id"] == "chat-capsule-source"
    assert source_pointer["reason"] == "capture"
    assert target_pointer["runtime_chat_id"] == "chat-capsule-target"
    assert target_pointer["origin_runtime_chat_id"] == "chat-capsule-source"
    assert target_pointer["reason"] == "restore"
    check = await manager.execute(
        chat_id="chat-capsule-target",
        code="print(label, portable['answer'])",
        run_id="run-capsule-target",
        outer_tool_call_id="outer-capsule-target",
    )
    try:
        assert check.ok, check.to_dict()
        assert "forked 42" in check.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_create_capsule_requires_live_state_and_enforces_host_bounds(kernel_stack):
    manager, runtimes, _artifacts = kernel_stack
    runtimes.ensure_runtime("chat-capsule-absent", is_new=True)
    with pytest.raises(KernelCapsuleError) as absent:
        await manager.create_capsule(runtime_chat_id="chat-capsule-absent")
    assert absent.value.code == "kernel_not_live"

    runtimes.ensure_runtime("chat-capsule-bounded", is_new=True)
    created = await manager.execute(
        chat_id="chat-capsule-bounded",
        code="too_large = 'four'",
        run_id="run-capsule-bounded",
        outer_tool_call_id="outer-capsule-bounded",
    )
    assert created.ok, created.to_dict()
    manager.capsule_limits = KernelCapsuleLimits(max_value_bytes=3)
    try:
        with pytest.raises(KernelCapsuleError) as bounded:
            await manager.create_capsule(runtime_chat_id="chat-capsule-bounded")
        assert bounded.value.code == "capsule_value_too_large"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelled_kernel_close_finishes_teardown_before_propagating(tmp_path):
    reply_entered = asyncio.Event()
    lease_closed: list[tuple[str, KernelLease]] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    class FakeTransport:
        def __init__(self) -> None:
            self.request_id = ""
            self.closed = False

        def send(self, request_id, operation, **_fields):
            assert operation == "shutdown"
            self.request_id = request_id

        async def receive(self, *, timeout_s=None):
            del timeout_s
            reply_entered.set()
            return {
                "type": "done",
                "id": self.request_id,
                "status": "ok",
            }

        def close(self):
            self.closed = True

    class FakeJob:
        def __init__(self) -> None:
            self.closed = False

        def terminate_and_close(self):
            self.closed = True

    class FakeBridge:
        def __init__(self) -> None:
            self.closed = False

        async def close(self):
            self.closed = True

    manager = SimpleNamespace(
        runtime_profile=runtime_profile("core.v1"),
        limits=KernelLimits(shutdown_grace_s=30.0),
        lease_closed=lambda chat_id, lease: lease_closed.append((chat_id, lease)),
        emit=lambda *_args, **_kwargs: None,
    )
    lease = KernelLease(
        manager=manager,
        chat_id="chat-close-retry",
        generation=1,
        identity=SimpleNamespace(),
        generation_root=str(tmp_path / "generation"),
        workspace_root=str(tmp_path / "workspace"),
        workspace_fingerprint="workspace-fingerprint",
        workspace_roots=(str(tmp_path / "workspace"),),
        workspace_revision=1,
    )
    process = FakeProcess()
    transport = FakeTransport()
    job = FakeJob()
    bridge = FakeBridge()
    lease.process = process
    lease.transport = transport
    lease.job = job
    lease.bridge = bridge
    lease.state = "ready"

    first_close = asyncio.create_task(lease.close(reason="cancelled-close"))
    await asyncio.wait_for(reply_entered.wait(), timeout=1)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first_close, timeout=1)

    assert lease._closed is True
    assert lease.state == "absent"
    assert lease.process is lease.transport is lease.job is lease.bridge is None
    assert process.killed is True
    assert transport.closed is True
    assert job.closed is True
    assert bridge.closed is True
    assert lease_closed == [("chat-close-retry", lease)]


@pytest.mark.asyncio
async def test_kernel_reports_broken_exception_and_preserves_next_cell(kernel_stack):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-broken-exception"
    runtimes.ensure_runtime(chat_id, is_new=True)
    try:
        failed = await manager.execute(
            chat_id=chat_id,
            code=(
                "class Broken(Exception):\n"
                "    def __str__(self):\n"
                "        raise RuntimeError('formatting failed')\n"
                "raise Broken()"
            ),
            run_id="broken-error",
            outer_tool_call_id="broken-error-call",
            timeout_s=2,
        )
        assert failed.status == "error", failed.to_dict()
        assert failed.error_code == "python_exception"
        assert "<exception str() failed>" in failed.error_message
        follow = await manager.execute(
            chat_id=chat_id, code="1 + 1",
            run_id="after-broken-error", outer_tool_call_id="after-broken-call",
        )
        assert follow.ok, follow.to_dict()
        assert follow.generation == failed.generation
        assert follow.output.text().strip() == "2"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_start", [False, True])
async def test_kernel_request_backstop_sends_terminal_response(cancel_before_start):
    class Broken(Exception):
        def __str__(self):
            raise RuntimeError("formatting failed")

    async def broken_handler():
        raise Broken()

    events = []
    worker = ReplWorker.__new__(ReplWorker)
    worker.emitter = SimpleNamespace(
        emit=lambda kind, request_id, **fields: events.append(
            {"type": kind, "id": request_id, **fields}
        )
    )
    worker.control_read_allowed = threading.Event()
    worker._interrupt_lock = threading.Lock()
    worker._queued_execution_ids = set()
    worker._pending_interrupts = set()
    worker._signal_target = ""
    worker._active_phase = "executing"
    worker.active_id = "request-backstop"
    worker.in_flight = {worker.active_id}
    worker.active_task = asyncio.create_task(broken_handler())
    if cancel_before_start:
        worker.active_task.cancel()

    await worker._settle_active()

    assert len(events) == 1
    assert events[0]["type"] == "done"
    assert events[0]["id"] == "request-backstop"
    assert events[0]["status"] == ("cancelled" if cancel_before_start else "error")
    assert events[0]["error"]["code"] == (
        "kernel_cell_cancelled" if cancel_before_start else "kernel_request_error"
    )
    if not cancel_before_start:
        assert events[0]["error"]["message"] == "<exception str() failed>"
    assert worker.active_task is None
    assert not worker.in_flight
    assert worker.control_read_allowed.is_set()


@pytest.mark.asyncio
async def test_kernel_request_backstop_exits_on_unwritable_protocol():
    async def broken_handler():
        raise RuntimeError("request failed")

    def broken_protocol(*_args, **_kwargs):
        raise BrokenPipeError("host pipe closed")

    worker = ReplWorker.__new__(ReplWorker)
    worker.emitter = SimpleNamespace(emit=broken_protocol)
    worker.control_read_allowed = threading.Event()
    worker._interrupt_lock = threading.Lock()
    worker._queued_execution_ids = set()
    worker._pending_interrupts = set()
    worker._signal_target = ""
    worker._active_phase = "executing"
    worker.active_id = "unwritable-terminal"
    worker.in_flight = {worker.active_id}
    worker.active_task = asyncio.create_task(broken_handler())
    with pytest.raises(BrokenPipeError):
        await worker._settle_active()
    assert worker.active_task is None
    assert not worker.in_flight


@pytest.mark.asyncio
async def test_reader_interrupt_is_exactly_fenced_and_does_not_signal_handoff():
    events = []
    release = asyncio.Event()
    worker = ReplWorker.__new__(ReplWorker)
    worker.emitter = SimpleNamespace(
        emit=lambda kind, request_id, **fields: events.append(
            {"type": kind, "id": request_id, **fields}
        )
    )
    worker._interrupt_lock = threading.Lock()
    worker._queued_execution_ids = set()
    worker._pending_interrupts = set()
    worker._signal_target = ""
    worker._active_phase = "executing"
    worker.active_id = "cell-current"
    worker.active_task = asyncio.create_task(release.wait())
    worker.in_flight = {"cell-current"}
    delivered = []
    worker._deliver_main_thread_interrupt = (
        lambda target_id, task: delivered.append((target_id, task))
    )
    try:
        worker._reader_interrupt({
            "id": "interrupt-current",
            "target_id": "cell-current",
        })
        assert delivered == [("cell-current", worker.active_task)]
        assert events[-1]["result"] == {
            "matched": True, "pending": False,
            "target_id": "cell-current",
        }

        worker._active_phase = "finishing"
        worker._reader_interrupt({
            "id": "interrupt-handoff",
            "target_id": "cell-current",
        })
        assert len(delivered) == 1
        assert events[-1]["result"]["matched"] is True

        worker.active_id = "cell-next"
        worker.in_flight = {"cell-next"}
        worker._reader_interrupt({
            "id": "interrupt-late",
            "target_id": "cell-current",
        })
        assert len(delivered) == 1
        assert events[-1]["result"]["matched"] is False

        worker.active_id = ""
        worker.active_task = None
        worker.in_flight.clear()
        worker._queued_execution_ids.add("cell-queued")
        worker._reader_interrupt({
            "id": "interrupt-pending",
            "target_id": "cell-queued",
        })
        assert "cell-queued" in worker._pending_interrupts
        assert events[-1]["result"] == {
            "matched": True, "pending": True,
            "target_id": "cell-queued",
        }
    finally:
        release.set()
        task = delivered[0][1]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("shield_wait", [False, True])
async def test_kernel_interrupt_settles_host_calls_and_keeps_python_background_tasks(
    kernel_stack, shield_wait,
):
    manager, runtimes, _ = kernel_stack
    chat_id = "chat-interrupt-host-calls"
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.catalog_service.select(chat_id, "build")
    started = asyncio.Event()
    finished = asyncio.Event()
    completed = []

    async def pending_call(args):
        started.set()
        try:
            await asyncio.sleep(30)
            completed.append(args["path"])
            return args["path"]
        finally:
            finished.set()

    manager.broker.registry.get("read_file").handler = pending_call
    execution = asyncio.create_task(manager.execute(
        chat_id=chat_id,
        code=(
            "import asyncio\n"
            "survivor_gate = asyncio.Event()\n"
            "async def ordinary_background():\n"
            "    await survivor_gate.wait()\n"
            "    return 42\n"
            "async def pending_host_call():\n"
            "    try:\n"
            "        return await tools.read_file.async_(path='pending')\n"
            "    except Exception as exc:\n"
            "        return getattr(exc, 'code', type(exc).__name__)\n"
            "survivor = asyncio.create_task(ordinary_background())\n"
            "host_call = asyncio.create_task(pending_host_call())\n"
            + ("await asyncio.shield(survivor)" if shield_wait else "await asyncio.sleep(30)")
        ),
        run_id="interrupt-host", outer_tool_call_id="interrupt-host-call",
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        control = await manager.interrupt(chat_id, intent="steer")
        assert control["status"] == "requested"
        result = await asyncio.wait_for(execution, timeout=5)
        assert result.status == "cancelled", result.to_dict()
        assert not result.hard_restarted
        assert finished.is_set()
        assert not completed
        follow = await manager.execute(
            chat_id=chat_id,
            code=("print('PENDING', not survivor.done())\nsurvivor_gate.set()\n"
                  "print('SURVIVOR', await survivor)\nprint('HOST', await host_call)"),
            run_id="after-host-interrupt", outer_tool_call_id="after-host-interrupt-call",
        )
        assert follow.ok, follow.to_dict()
        assert follow.generation == result.generation
        assert "PENDING True" in follow.output.text()
        assert "SURVIVOR 42" in follow.output.text()
        assert "broker_cancelled_after_dispatch" in follow.output.text()
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_chat_steering_waits_for_live_cell_completion(
    kernel_stack, tmp_path,
):
    from unittest.mock import AsyncMock
    from tests.test_ws_dispatch_chat import _chat_stack
    import ws_dispatch

    manager, runtimes, _ = kernel_stack
    srv, queue, _repository, connection, sid = _chat_stack(tmp_path / "chat", AsyncMock())
    runtimes.ensure_runtime(sid, is_new=True)
    manager.catalog_service.select(sid, "build")
    srv.require_runtime().kernel = manager
    connection.busy = True
    started_path, release_path = tmp_path / "entered", tmp_path / "release"
    execution = asyncio.create_task(manager.execute(
        chat_id=sid,
        code=("import asyncio\nfrom pathlib import Path\n"
              f"Path({str(started_path)!r}).write_text('entered')\n"
              f"while not Path({str(release_path)!r}).exists():\n"
              "    await asyncio.sleep(0.01)\n"
              "completed_before_steer = 42\nprint('CELL COMPLETED')"),
        run_id="boundary-proof", outer_tool_call_id="boundary-proof-cell",
    ))
    async def wait_started():
        while not started_path.exists():
            await asyncio.sleep(.01)
    try:
        await asyncio.wait_for(wait_started(), 20)
        socket = AsyncMock()
        await ws_dispatch.HANDLERS["chat"](srv, socket, connection, {
            "text":"Change the final summary", "session_id":sid,
            "delivery":"steer",
        })
        assert socket.send_json.await_args.args[0]["type"] == "chat:queued"
        assert not execution.done()
        release_path.write_text("release")
        result = await asyncio.wait_for(execution, 5)
        assert result.status == "ok"
        assert not result.hard_restarted
        assert "CELL COMPLETED" in result.output.text()
        ticket = queue.claim_input(sid, "steer", run_id="boundary-proof")
        assert ticket["text"] == "Change the final summary"
        assert queue.claim_input(sid, "steer", run_id="boundary-proof") is None
        follow = await manager.execute(
            chat_id=sid, code="print(globals().get('completed_before_steer', 'interrupted'))",
            run_id="boundary-follow", outer_tool_call_id="boundary-follow-cell",
        )
        assert follow.ok and follow.generation == result.generation
        assert "42" in follow.output.text()
    finally:
        release_path.write_text("cleanup")
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_paused_admission_pins_live_python_state_against_automatic_eviction(kernel_stack):
    manager, runtimes, _ = kernel_stack
    chat_id = 'pause-keeps-python'
    runtimes.ensure_runtime(chat_id, is_new=True)
    manager.catalog_service.select(chat_id, 'build')
    original_limits = manager.limits
    first = await manager.execute(chat_id=chat_id, code="marker = object()\nprint(id(marker))",
                                  run_id='pin-first', outer_tool_call_id='pin-first')
    assert first.ok
    admission = runtimes.try_reserve_run(chat_id)
    runtimes.set_run_paused(chat_id, True)
    try:
        manager.limits = replace(original_limits, idle_lifetime_s=1, absolute_lifetime_s=1, max_live_kernels=1)
        manager._leases[chat_id].last_used_at -= 10
        manager._leases[chat_id].created_at -= 10
        assert await manager.reap_idle() == 0
        with pytest.raises(KernelUnavailable, match='pinned'):
            await manager._evict_for_capacity()
        lease = manager._leases[chat_id]
        lock = manager._boot_locks.setdefault(chat_id, asyncio.Lock())
        runtimes.set_run_paused(chat_id, False)
        runtimes.finish_run(admission, status='first-pause-complete')
        admission = runtimes.try_reserve_run(chat_id)
        await lock.acquire()
        closing = asyncio.create_task(manager._close_lease_serialized(lease, reason='capacity_eviction'))
        await asyncio.sleep(0)
        runtimes.set_run_paused(chat_id, True)
        lock.release()
        assert await closing == {} and not lease._closed
        runtimes.set_run_paused(chat_id, False)
        assert await manager.reap_idle() == 0  # Resume must not immediately expire the retained worker.
        manager.limits = original_limits
        follow = await manager.execute(chat_id=chat_id, code="print(id(marker))",
                                       run_id='pin-follow', outer_tool_call_id='pin-follow')
        assert follow.ok and follow.generation == first.generation
        assert follow.output.text().strip() == first.output.text().strip()
    finally:
        manager.limits = original_limits
        runtimes.finish_run(admission, status='complete')
        await manager.shutdown()


@pytest.mark.asyncio
async def test_kernel_capacity_rechecks_after_shared_eviction_victim():
    manager = KernelRuntimeManager.__new__(KernelRuntimeManager)
    manager.limits = KernelLimits(max_live_kernels=1)
    manager._boot_locks = {}
    victim = SimpleNamespace(chat_id="victim", state="ready", _closed=False, last_used_at=0)
    manager._leases = {"victim": victim}
    checkpoint_entered = asyncio.Event()
    release_checkpoint = asyncio.Event()
    second_victim_selected = asyncio.Event()

    async def reap_idle():
        return 0

    async def close_with_checkpoint(lease, **_kwargs):
        checkpoint_entered.set()
        await release_checkpoint.wait()
        lease._closed = True
        manager._leases.pop(lease.chat_id)
        return {}

    close_serialized = manager._close_lease_serialized
    selections = 0

    async def observed_close(lease, **kwargs):
        nonlocal selections
        selections += 1
        if selections == 2:
            second_victim_selected.set()
        return await close_serialized(lease, **kwargs)

    manager.reap_idle = reap_idle
    manager._close_lease_serialized = observed_close
    manager.continuity = SimpleNamespace(close_lease_with_checkpoint=close_with_checkpoint)

    async def admit(name):
        await manager._evict_for_capacity()
        manager._leases[name] = SimpleNamespace(
            chat_id=name, state="starting", _closed=False, last_used_at=1
        )

    first = asyncio.create_task(admit("first"))
    await asyncio.wait_for(checkpoint_entered.wait(), timeout=1)
    second = asyncio.create_task(admit("second"))
    await asyncio.wait_for(second_victim_selected.wait(), timeout=1)
    release_checkpoint.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert outcomes[0] is None
    assert isinstance(outcomes[1], KernelUnavailable)
    assert list(manager._leases) == ["first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("calls_settle", [True, False])
async def test_kernel_interrupt_racing_done_still_settles_host_calls(kernel_stack, calls_settle):
    manager, runtimes, _ = kernel_stack
    record = runtimes.ensure_runtime("interrupt-done-race", is_new=True)
    lease = KernelLease(
        manager=manager, chat_id=record.chat_id, generation=1, identity=record.identity,
        generation_root=os.path.join(manager.root, "race"), workspace_root=manager.app_root,
        workspace_fingerprint="race", workspace_roots=(manager.app_root,), workspace_revision=0,
    )
    admission = ExecutionAdmission(
        execution_id="race-cell", chat_id=record.chat_id, run_id="race-run",
        outer_tool_call_id="race-call", generation=1,
        catalog_release_id=record.identity.catalog_release_id, mount_revision=0,
        selected_category_id="", overlay_revision=0,
        environment_digest=record.identity.environment_digest, workspace_root_ids=(manager.app_root,),
    )
    settlements = []
    hard_closes = []

    class Transport:
        def send(self, *_args, **_kwargs):
            pass

        async def receive(self, **_kwargs):
            lease._send_interrupt()
            return {"type": "done", "id": admission.execution_id, "status": "ok"}

    class Bridge:
        async def cancel_execution(self, execution_id, **_kwargs):
            settlements.append(execution_id)
            assert lease._admissions[execution_id].cancelled()
            return calls_settle

        async def forget_execution(self, execution_id):
            assert execution_id not in lease._admissions

    async def close(*, reason, hard):
        hard_closes.append((reason, hard))
        lease._closed = True
        lease.state = "absent"

    lease.transport = Transport()
    lease.process = SimpleNamespace(poll=lambda: None)
    lease.bridge = Bridge()
    lease.close = close
    lease.state = "ready"
    result = await lease.execute("pass", admission)
    assert result.status == "cancelled"
    assert settlements == ["race-cell"]
    assert result.hard_restarted is (not calls_settle)
    assert lease.state == ("ready" if calls_settle else "absent")
    assert hard_closes == ([] if calls_settle else [("cancelled", True)])


@pytest.mark.parametrize("bridge_settles", [False, True])
@pytest.mark.asyncio
async def test_steer_waits_past_grace_but_terminal_stop_can_replace_generation(
    tmp_path,
    bridge_settles,
):
    emitted = []
    manager = SimpleNamespace(
        runtime_profile=runtime_profile("core.v1"),
        limits=KernelLimits(interrupt_grace_s=0.05),
        emit=lambda event, **fields: emitted.append((event, fields)),
    )
    lease = KernelLease(
        manager=manager,
        chat_id="steer-safe-boundary",
        generation=1,
        identity=SimpleNamespace(),
        generation_root=str(tmp_path / "generation"),
        workspace_root=str(tmp_path),
        workspace_fingerprint="safe-boundary",
        workspace_roots=(str(tmp_path),),
        workspace_revision=0,
    )
    execution_id = "safe-boundary-cell"
    lease._cell_cancellations[execution_id] = asyncio.Event()
    closes = []

    class Bridge:
        async def cancel_execution(self, *_args, **_kwargs):
            await asyncio.sleep(0.005)
            return bridge_settles

    class Transport:
        def send(self, *_args, **_kwargs):
            pass

        async def receive(self, **_kwargs):
            await asyncio.sleep(0)
            raise asyncio.TimeoutError

    async def close(*, reason, hard):
        closes.append((reason, hard))
        lease._closed = True
        lease.state = "absent"

    lease.bridge = Bridge()
    lease.transport = Transport()
    lease.process = SimpleNamespace(poll=lambda: None)
    lease.close = close
    lease.state = "busy"
    lease._admissions[execution_id] = SimpleNamespace()
    assert lease._request_interrupt(intent="steer", send=False) == execution_id
    steering = asyncio.create_task(lease._interrupt_repl_or_kill(
        execution_id=execution_id,
        collector=SimpleNamespace(),
        reason="steered",
        intent="steer",
    ))
    await asyncio.sleep(0.3)
    assert not steering.done()
    assert closes == []
    assert [event for event, _fields in emitted] == [
        "kernel:steer_waiting_safe_boundary"
    ]
    assert lease._request_interrupt(intent="stop", send=False) == execution_id
    stopped, hard_restarted = await asyncio.wait_for(
        steering,
        timeout=1.0,
    )
    assert stopped is None and hard_restarted is True
    assert closes == [("terminal_interrupt", True)]


def test_capsule_reassigned_proxy_alias_is_user_state():
    namespace = {"reader": CapabilityProxy({"alias": "read_file"}, SimpleNamespace())}
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace=namespace),
        reinstall_namespace=lambda _document: None,
        document={},
    )
    assert worker.capture()["excluded"][0]["reason"] == "service_proxy"
    namespace["reader"] = {"answer": 42}
    captured = worker.capture()
    assert [item["name"] for item in captured["values"]] == ["reader"]
    namespace["reader"] = "changed"
    worker.restore({"schema": WORKER_CAPSULE_SCHEMA, "values": captured["values"]})
    assert namespace["reader"] == {"answer": 42}


def test_capsule_retired_mount_name_can_be_restored_as_user_state():
    namespace = {"former_root": object()}
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace=namespace),
        reinstall_namespace=lambda _document: None,
        document={"mounted_objects": {"former_root": {}}},
    )
    worker.update_document({"mounted_objects": {}})
    namespace["former_root"] = [1, 2, 3]
    captured = worker.capture()
    assert [item["name"] for item in captured["values"]] == ["former_root"]
    namespace["former_root"] = []
    worker.restore({"schema": WORKER_CAPSULE_SCHEMA, "values": captured["values"]})
    assert namespace["former_root"] == [1, 2, 3]


@pytest.mark.parametrize("rows", [[], [1, 2, 3]])
def test_capsule_arrow_record_batch_preserves_empty_schema_and_values(rows):
    arrow = pytest.importorskip("pyarrow")
    schema = arrow.schema(
        [arrow.field("id", arrow.int64(), nullable=False)],
        metadata={b"source": b"kernel-regression"},
    )
    original = arrow.RecordBatch.from_arrays([arrow.array(rows, type=arrow.int64())], schema=schema)
    namespace = {"batch": original, "other": "retained"}
    worker = KernelCapsuleWorker(
        SimpleNamespace(namespace=namespace),
        reinstall_namespace=lambda _document: None, document={},
    )
    captured = worker.capture()
    namespace.clear()
    worker.restore({"schema": WORKER_CAPSULE_SCHEMA, "values": captured["values"]})
    assert namespace["batch"].equals(original, check_metadata=True)
    assert namespace["other"] == "retained"


@pytest.mark.asyncio
async def test_kernel_close_survives_cancellation_at_reply_and_bridge_cleanup(tmp_path):
    reply_entered = asyncio.Event()
    process_killed = asyncio.Event()
    bridge_entered = asyncio.Event()
    release_bridge = asyncio.Event()
    removed = []

    class Process:
        def poll(self):
            return -9 if process_killed.is_set() else None

        def kill(self):
            process_killed.set()

        def wait(self):
            return self.poll()

    class Transport:
        closed = False

        def send(self, *_args, **_kwargs):
            pass

        async def receive(self, **_kwargs):
            reply_entered.set()
            await process_killed.wait()
            raise EOFError("worker exited")

        def close(self):
            self.closed = True

    class Bridge:
        closed = False

        async def close(self):
            bridge_entered.set()
            await release_bridge.wait()
            self.closed = True

    manager = SimpleNamespace(
        runtime_profile=runtime_profile("core.v1"), limits=KernelLimits(),
        lease_closed=lambda *args: removed.append(args), emit=lambda *_args, **_kwargs: None,
    )
    lease = KernelLease(
        manager=manager, chat_id="cancel-close", generation=1, identity=SimpleNamespace(),
        generation_root=str(tmp_path / "generation"), workspace_root=str(tmp_path),
        workspace_fingerprint="test", workspace_roots=(str(tmp_path),), workspace_revision=0,
    )
    transport, bridge = Transport(), Bridge()
    lease.process, lease.transport, lease.bridge = Process(), transport, bridge
    lease.state = "ready"
    closing = asyncio.create_task(lease.close())
    try:
        await asyncio.wait_for(reply_entered.wait(), timeout=1)
        closing.cancel()
        await asyncio.wait_for(bridge_entered.wait(), timeout=1)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert not removed
    finally:
        process_killed.set()
        release_bridge.set()
        await asyncio.gather(closing, return_exceptions=True)
    assert closing.cancelled()
    assert lease._closed and lease.state == "absent"
    assert lease.process is lease.transport is lease.bridge is None
    assert transport.closed and bridge.closed
    assert removed == [("cancel-close", lease)]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_disabled_automatic_retirement_preserves_old_leases_beyond_four(limit):
    manager = KernelRuntimeManager.__new__(KernelRuntimeManager)
    manager.limits = KernelLimits(max_live_kernels=limit, idle_lifetime_s=limit, absolute_lifetime_s=limit)
    manager._leases = {str(n): SimpleNamespace(chat_id=str(n), state="ready", _closed=False,
                                             last_used_at=0, created_at=0) for n in range(6)}
    assert await manager.reap_idle() == 0
    await manager._evict_for_capacity()
    assert len(manager._leases) == 6 and all(not lease._closed for lease in manager._leases.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("idle,absolute,old_idle,old_absolute", [
    (1, 0, True, False), (0, 1, False, True),
])
async def test_positive_retirement_limits_expire_independently(idle, absolute, old_idle, old_absolute):
    manager = KernelRuntimeManager.__new__(KernelRuntimeManager)
    manager.limits = KernelLimits(idle_lifetime_s=idle, absolute_lifetime_s=absolute)
    now = time.monotonic()
    lease = SimpleNamespace(chat_id="old", state="ready", _closed=False,
                            last_used_at=now - 10 if old_idle else now,
                            created_at=now - 10 if old_absolute else now)
    manager._leases = {"old": lease}
    async def close(target, *, reason):
        assert target is lease and reason == "idle_or_absolute_eviction"
        target._closed = True
    manager._close_lease_serialized = close
    assert await manager.reap_idle() == 1 and lease._closed


@pytest.mark.asyncio
async def test_default_policy_retains_five_real_workers_and_explicit_close_still_works(kernel_stack):
    manager, runtimes, _ = kernel_stack
    defaults = KernelLimits()
    assert defaults.max_live_kernels == defaults.idle_lifetime_s == defaults.absolute_lifetime_s == 0
    manager.limits = replace(manager.limits, max_live_kernels=defaults.max_live_kernels,
                            idle_lifetime_s=defaults.idle_lifetime_s,
                            absolute_lifetime_s=defaults.absolute_lifetime_s)
    first = None
    try:
        for index in range(5):
            chat = f"retained-{index}"
            runtimes.ensure_runtime(chat, is_new=True)
            result = await manager.execute(chat_id=chat, code="marker = object(); print(id(marker))",
                                           run_id=chat, outer_tool_call_id=chat)
            assert result.ok
            if first is None:
                first = result
            manager._leases[chat].created_at -= 100_000
            manager._leases[chat].last_used_at -= 100_000
        assert len(manager._leases) == 5 and await manager.reap_idle() == 0
        following = await manager.execute(chat_id="retained-0", code="print(id(marker))",
                                          run_id="return", outer_tool_call_id="return")
        assert following.ok and following.generation == first.generation
        assert following.output.text() == first.output.text()
        lease = manager._leases["retained-0"]
        assert await manager.close_chat("retained-0") and lease._closed
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_ordinary_active_admission_pins_ready_kernel_between_cells(kernel_stack):
    manager, runtimes, _ = kernel_stack
    chat = "thinking-retention"
    runtimes.ensure_runtime(chat, is_new=True)
    result = await manager.execute(chat_id=chat, code="marker = object()",
                                   run_id="first", outer_tool_call_id="first")
    assert result.ok
    lease = manager._leases[chat]
    lease.last_used_at -= 10
    lease.created_at -= 10
    manager.limits = replace(manager.limits, max_live_kernels=1, idle_lifetime_s=1, absolute_lifetime_s=1)
    admission = runtimes.try_reserve_run(chat)
    try:
        assert lease.state == "ready" and runtimes.pause_snapshot(chat)["state"] == "running"
        assert await manager.reap_idle() == 0
        with pytest.raises(KernelUnavailable, match="busy or pinned"):
            await manager._evict_for_capacity()
        assert await manager._close_lease_serialized(lease, reason="capacity_eviction") == {}
        assert not lease._closed
    finally:
        runtimes.finish_run(admission, status="complete")
        await manager.shutdown()
