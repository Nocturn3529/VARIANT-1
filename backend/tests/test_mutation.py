"""Phase 7 session-local mutation lifecycle and execution contracts."""

from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

from artifacts.store import ContentAddressedArtifactStore
from session_catalog.catalog import (
    COVERED_BROKER_HANDLER_NAMES,
    MOUNTED_PYTHON_API_HANDLER_NAMES,
)
from session_catalog.releases import CatalogReleases, ReleaseError
from session_catalog.mutation import (
    MUTATION_HANDLER,
    MutationError,
    MutationWorkerClient,
    MutationWorkerError,
)
from session_catalog.mutation_contracts import MUTATION_REMOTE_HANDLE_PROXY
from session_catalog.service import CatalogService
from capability_broker import CapabilityBroker, current_capability_invocation
from kernel_runtime.manager import KernelLimits, KernelRuntimeManager
from kernel_runtime.output import OutputLimits
from kernel_runtime.worker_bridge import ToolbeltNamespace
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tools import Tool, ToolError, ToolRegistry


def _releases(service):
    return CatalogReleases(
        service.repository.path,
        artifact_store=service.artifact_store,
        mutation=service.mutation,
        catalog_repository=service.repository,
    )


async def _mock(name, arguments, request_id):
    return {
        "ok": True,
        "result": {"name": name, "arguments": arguments},
        "receipt_id": "mock-" + request_id,
    }


def _connector_handle_envelope() -> dict:
    return {
        "$variant1_handle": {
            "service": "connectors",
            "kind": "mcp",
            "id": "lease-test-1",
            "generation": 1,
            "revision": 3,
            "metadata": {
                "name": "inventory.lookup",
                "server_id": "inventory-test",
                "schema_included": True,
            },
            "_dispatch": {
                "schema": "variant1.remote-handle-dispatch.v1",
                "ref_id": "ref-test-remote-handle",
                "handler_revision": "variant1.remote-handle-dispatch-handler.v1",
            },
            "methods": {
                "schema": "variant1.remote-handle-methods.v1",
                "items": [
                    {
                        "name": "schema",
                        "description": "Return the leased MCP schema.",
                        "params": [],
                        "returns": "object",
                    },
                    {
                        "name": "invoke",
                        "description": "Invoke the leased MCP tool.",
                        "params": [
                            {
                                "name": "arguments",
                                "type": "object",
                                "required": False,
                                "default": {},
                            },
                            {
                                "name": "conclude",
                                "type": "boolean",
                                "required": False,
                                "default": False,
                            },
                        ],
                        "returns": "object",
                    },
                ],
            },
        }
    }


def _connector_search_result() -> dict:
    handle = _connector_handle_envelope()
    return {
        "mcp": [handle],
        "plugins": [],
        "top_match": {
            "handle": handle,
            "schema": {
                "descriptor": {
                    "name": "inventory.lookup",
                    "inputSchema": {
                        "type": "object",
                        "required": ["item_id"],
                        "properties": {"item_id": {"type": "string"}},
                    },
                },
                "lease": {"lease_id": "lease-test-1"},
            },
        },
    }


def _mcp_lookup_result(item_id: str) -> dict:
    return {
        "schema": "variant1.mcp-result.v2",
        "is_error": False,
        "structured_content": {
            "item_id": str(item_id),
            "value": "resolved-" + str(item_id),
        },
        "content": [],
    }


@pytest.mark.asyncio
async def test_disposable_worker_has_same_user_python_and_routes_normal_proxy(tmp_path):
    worker = MutationWorkerClient(
        str(tmp_path / "workers"),
        worker_executable=os.environ.get("VARIANT1_TEST_KERNEL_EXE", ""),
    )
    output_path = tmp_path / "same-user-output.txt"
    safe = await worker.run(
        {
            "mode": "execute",
            "source": (
                "import os\n"
                "import socket\n"
                "import subprocess\n"
                "from pathlib import Path\n"
                "def run(arguments):\n"
                "    Path(arguments['output']).write_text('same-user', encoding='utf-8')\n"
                "    with socket.socket() as probe:\n"
                "        probe.bind(('127.0.0.1', 0))\n"
                "        bound_port = probe.getsockname()[1]\n"
                "    if os.name == 'nt':\n"
                "        command = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', "
                "'echo same-user-process']\n"
                "    else:\n"
                "        command = ['/bin/sh', '-c', 'printf same-user-process']\n"
                "    child = subprocess.run(command, capture_output=True, text=True, check=True)\n"
                "    observed = tools.reader(path=arguments['path'])\n"
                "    clicked = computer.click(view={'id': 'view-1'}, x=3, y=4)\n"
                "    return {'upper': arguments['path'].upper(), 'observed': observed, "
                "'clicked': clicked, "
                "'pid': os.getpid(), 'bound_port': bound_port, "
                "'child': child.stdout.strip()}\n"
            ),
            "arguments": {"path": "alpha", "output": str(output_path)},
            "proxy_contracts": {
                "tools.reader": {"parameters": ["path"]},
                "computer.click": {"parameters": ["view", "target", "x", "y"]},
            },
        },
        proxy_call=_mock,
    )
    assert safe["result"]["upper"] == "ALPHA"
    assert safe["result"]["pid"] > 0
    assert safe["result"]["bound_port"] > 0
    assert safe["result"]["child"] == "same-user-process"
    assert safe["observed_calls"][0]["proxy"] == "tools.reader"
    assert safe["observed_calls"][1]["proxy"] == "computer.click"
    assert safe["execution_mode"] == "same_user"
    assert safe["filesystem_access"] == "user"
    assert output_path.read_text(encoding="utf-8") == "same-user"

    with pytest.raises(MutationWorkerError) as caught:
        await worker.run(
            {
                "mode": "validate",
                "source": "async def run(arguments):\n    return arguments\n",
            },
            proxy_call=_mock,
        )
    assert caught.value.code == "candidate_contract_error"


@pytest.mark.asyncio
async def test_mutation_worker_projects_connector_handles_and_dispatches_methods(
    tmp_path,
):
    worker = MutationWorkerClient(str(tmp_path / "workers"))
    calls = []

    async def proxy_call(name, arguments, request_id):
        calls.append((name, arguments, request_id))
        if name == "connectors.search":
            return {
                "ok": True,
                "result": _connector_search_result(),
                "receipt_id": "search-" + request_id,
            }
        assert name == MUTATION_REMOTE_HANDLE_PROXY
        assert arguments["handle"] == {
            "service": "connectors",
            "kind": "mcp",
            "id": "lease-test-1",
            "generation": 1,
            "revision": 3,
        }
        assert arguments["method"] == "invoke"
        supplied = arguments["arguments"]
        assert supplied["conclude"] is False
        return {
            "ok": True,
            "result": _mcp_lookup_result(supplied["arguments"]["item_id"]),
            "receipt_id": "dispatch-" + request_id,
        }

    report = await worker.run(
        {
            "mode": "execute",
            "source": (
                "def run(arguments):\n"
                "    found = connectors.search(query='inventory lookup', kind='tool')\n"
                "    result = found.top_match.invoke(\n"
                "        arguments={'item_id': arguments['item_id']}, conclude=False\n"
                "    )\n"
                "    return {\n"
                "        'value': result['value'],\n"
                "        'raw_schema': result.raw['schema'],\n"
                "        'handle': found.mcp[0],\n"
                "        'internal_visible': '__variant1_internal' in globals(),\n"
                "    }\n"
            ),
            "arguments": {"item_id": "item-7"},
            "proxy_contracts": {
                "connectors.search": {"parameters": ["query", "kind"]},
                MUTATION_REMOTE_HANDLE_PROXY: {
                    "parameters": ["handle", "method", "arguments"],
                    "internal_role": "remote_handle_dispatch",
                },
            },
        },
        proxy_call=proxy_call,
    )

    assert report["result"] == {
        "value": "resolved-item-7",
        "raw_schema": "variant1.mcp-result.v2",
        "handle": {
            "service": "connectors",
            "kind": "mcp",
            "id": "lease-test-1",
            "generation": 1,
            "revision": 3,
        },
        "internal_visible": False,
    }
    assert [row[0] for row in calls] == [
        "connectors.search",
        MUTATION_REMOTE_HANDLE_PROXY,
    ]
    assert [row["proxy"] for row in report["observed_calls"]] == [
        "connectors.search",
        MUTATION_REMOTE_HANDLE_PROXY,
    ]


@pytest.mark.asyncio
async def test_mutation_worker_honors_declared_frame_limit(tmp_path):
    worker = MutationWorkerClient(str(tmp_path / "workers"))
    large = await worker.run(
        {
            "mode": "execute",
            "source": "def run(arguments):\n    return 'x' * 70000\n",
            "arguments": {},
            "proxy_contracts": {},
        },
        proxy_call=_mock,
    )
    assert len(large["result"]) == 70_000

    with pytest.raises(MutationWorkerError) as caught:
        await worker.run(
            {
                "mode": "execute",
                "source": "def run(arguments):\n    return 'x' * 2100000\n",
                "arguments": {},
                "proxy_contracts": {},
            },
            proxy_call=_mock,
        )
    assert caught.value.code == "worker_frame_quota"


@pytest.fixture
def mutable_stack(tmp_path):
    registry = ToolRegistry()

    async def read_file(args):
        context = current_capability_invocation()
        if args["path"] == "remote-handle":
            return _connector_handle_envelope()
        return {
            "path": args["path"],
            "chat_id": context.chat_id if context else "",
        }

    async def apply_patch(args):
        if args.get("value") == "fail":
            raise ToolError("deterministic patch failure")
        return {"native": args}

    async def run_command(args):
        return {"native": args}

    async def computer(args):
        return {
            "operation": args["operation"],
            "view": args["view"],
            "x": args.get("x"),
            "y": args.get("y"),
        }

    async def remote_handle_dispatch(args):
        assert args["handle"] == {
            "service": "connectors",
            "kind": "mcp",
            "id": "lease-test-1",
            "generation": 1,
            "revision": 3,
        }
        assert args["method"] == "invoke"
        supplied = dict(args.get("arguments") or {})
        return _mcp_lookup_result(
            dict(supplied.get("arguments") or {}).get("item_id", "")
        )

    registry.register(Tool(
        "read_file", "Read a deterministic test path.", read_file,
        params={"path": {"type": "string", "required": True}},
        effect_class="read", schema_revision="schema.read.v1",
        handler_revision="handler.read.v1",
    ))
    registry.register(Tool(
        "apply_patch", "Apply a deterministic test patch.", apply_patch,
        params={"value": {"type": "string", "required": True}},
        effect_class="write", schema_revision="schema.patch.v1",
        handler_revision="handler.patch.v1",
    ))
    registry.register(Tool(
        "run_command", "Run a deterministic test command.", run_command,
        params={"value": {"type": "string", "required": False}},
        effect_class="external_side_effect", schema_revision="schema.run.v1",
        handler_revision="handler.run.v1",
    ))
    registry.register(Tool(
        "computer",
        "Exercise normal mounted-object syntax from a mutation.",
        computer,
        params={
            "operation": {"type": "string", "required": True},
            "view": {"type": "object", "required": True},
            "x": {"type": "integer", "required": False},
            "y": {"type": "integer", "required": False},
        },
        effect_class="external_side_effect",
        schema_revision="schema.computer.v1",
        handler_revision="handler.computer.v1",
        object_methods=(
            {
                "name": "click",
                "operation": "click",
                "description": "Click one point in a supplied view.",
                "effect_class": "external_side_effect",
                "params": {
                    "view": {"type": "object", "required": True},
                    "x": {"type": "integer", "required": False},
                    "y": {"type": "integer", "required": False},
                },
            },
            {
                "name": "observe",
                "operation": "observe",
                "description": "Return one supplied test view.",
                "effect_class": "read",
                "params": {
                    "view": {"type": "object", "required": True},
                },
            },
        ),
    ))
    registry.register(Tool(
        "remote_handle_dispatch",
        "Dispatch a versioned method against a test remote handle.",
        remote_handle_dispatch,
        params={
            "handle": {"type": "object", "required": True},
            "method": {"type": "string", "required": True},
            "arguments": {"type": "object", "required": False},
        },
        hidden=True,
        visibility="broker_only",
        effect_class="external_side_effect",
        schema_revision="variant1.remote-handle-dispatch.v1",
        handler_revision="variant1.remote-handle-dispatch-handler.v1",
    ))
    database = str(tmp_path / "astb.sqlite3")
    repository = SessionRuntimeRepository(database)
    holder = {}
    runtimes = SessionRuntimeRegistry(
        repository,
        identity_factory=lambda _chat_id, _is_new: holder["service"].identity(
            environment_digest="mutation-test"
        ),
    )
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    enabled = {
        "read_file", "apply_patch", "run_command", "computer",
        MUTATION_HANDLER, "remote_handle_dispatch",
    } | set(COVERED_BROKER_HANDLER_NAMES) | set(
        MOUNTED_PYTHON_API_HANDLER_NAMES
    )
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=runtimes,
        enabled_resolver=lambda: set(enabled),
        artifact_store=artifacts,
    )
    service = CatalogService(
        database_path=database,
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
        root=str(tmp_path / "kernels"),
        instance_id="mutation-test",
        app_root=str(tmp_path),
        worker_executable=os.environ.get("VARIANT1_TEST_KERNEL_EXE", ""),
        limits=KernelLimits(
            boot_timeout_s=30, cell_timeout_s=30, bridge_timeout_s=25,
            max_boot_concurrency=1, max_live_kernels=1,
            idle_lifetime_s=3600, absolute_lifetime_s=3600,
            output=OutputLimits(max_cell_bytes=32_768, max_events=128),
        ),
    )
    yield runtimes, broker, service, manager


def _enable_mutation(runtimes, chat_id: str):
    record = runtimes.ensure_runtime(chat_id, is_new=True)
    if not record.mutation_write_enabled:
        record = runtimes.set_mutation_write_enabled(
            chat_id, True, actor="test"
        )
    return record


def _object_method_aliases(document, name):
    mounted = (
        (document.get("mounted_objects") or {}).get(name)
        or (document.get("python_apis") or {}).get(name)
        or {}
    )
    return {row["alias"] for row in mounted.get("methods") or ()}


@pytest.mark.asyncio
async def test_active_mutation_cannot_be_demoted_by_persisted_retest(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "active-retest-fence"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposal = service.mutation.propose(
        chat_id,
        kind="create",
        slot="build/7",
        alias="echo_value",
        purpose="echo a value",
        schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        source="def run(arguments):\n    return arguments['value']\n",
        tests=[{"arguments": {"value": "ok"}, "expected": "ok"}],
    )
    await service.mutation.activate(chat_id, proposal["draft_id"])
    try:
        with pytest.raises(MutationError) as blocked:
            await service.mutation.test(chat_id, proposal["draft_id"])
        assert blocked.value.code == "draft_not_testable"
        assert service.mutation._draft(chat_id, proposal["draft_id"])[
            "status"
        ] in {"probation", "active"}
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_isolated_mutation_tests_use_live_workspace_root(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "mutation-workspace-parity"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposal = service.mutation.propose(
        chat_id,
        kind="create",
        slot="build/7",
        alias="cwd_value",
        purpose="return cwd",
        schema={"type": "object", "properties": {}},
        source="import os\ndef run(arguments):\n    return os.getcwd()\n",
    )
    await service.mutation.validate(chat_id, proposal["draft_id"])
    receipt = await service.mutation.test(
        chat_id,
        proposal["draft_id"],
        [{"arguments": {}, "expected": os.path.abspath(manager.app_root)}],
        workspace_roots=[manager.app_root],
    )
    try:
        assert receipt["ok"] is True
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mutation_replaces_grown_run_command_seed_directly(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "mutated-run-command"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    prompt = service.runtime_prompt(chat_id, "complete a complex command task")
    assert "adapt it as a normal recovery path" in prompt
    assert "toolbelt.mutate(toolbelt.last_failure(), using=helper" in prompt
    assert "toolbelt.synthesize(helper, invoke={...})" in prompt
    assert "Correct an ordinary argument mistake directly" in prompt
    proposal = service.mutation.propose(
        chat_id,
        kind="mutate",
        slot="build/5",
        parent="run_command",
        alias="run_command",
        purpose="Replace the grown command seed for this session.",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    return {'mutated_run': arguments['value'].upper()}\n"
        ),
        tests=[{
            "arguments": {"value": "candidate"},
            "expected": {"mutated_run": "CANDIDATE"},
        }],
    )
    activated = await service.mutation.activate(
        chat_id, proposal["draft_id"], expected_mount_revision=1
    )
    assert activated["slot_version"] == 1
    assert activated["invocation"] == {
        "qualified_name": "tools.run_command",
        "call": "tools.run_command(value)",
        "category_id": "build",
        "available": "next_cell",
    }
    document, _refs = service.namespace_document(chat_id, query="run_command")
    mutated = next(
        row for row in document["capabilities"]
        if row["alias"] == "run_command"
    )
    assert mutated["session_local"] is True
    assert "run_command" not in document["services"]
    indexed = [
        row for row in document["catalog_index"]
        if row["category_id"] == "build"
        and row["position"] == 5
    ]
    assert len(indexed) == 1
    assert indexed[0]["session_local"] is True
    assert document["top_k"][0]["call"] == "tools.run_command(value)"

    result = await manager.execute(
        chat_id=chat_id,
        code="print(tools.run_command(value='alpha'))",
        run_id="mut-run-command",
        outer_tool_call_id="mut-run-command-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "'mutated_run': 'ALPHA'" in result.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mutation_replaces_and_resets_the_artifacts_object_slot(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "mutated-artifacts-object"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposal = service.mutation.propose(
        chat_id,
        kind="mutate",
        slot="build/6",
        parent="artifacts",
        alias="summarize",
        purpose="Replace the artifacts object with one session method.",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    return {'artifact_summary': arguments['value'].upper()}\n"
        ),
        tests=[{
            "arguments": {"value": "candidate"},
            "expected": {"artifact_summary": "CANDIDATE"},
        }],
    )
    activated = await service.mutation.activate(
        chat_id, proposal["draft_id"], expected_mount_revision=1
    )
    assert activated["slot_version"] == 1
    assert activated["invocation"]["qualified_name"] == "artifacts.summarize"
    assert activated["invocation"]["call"] == "artifacts.summarize(value)"
    document, _refs = service.namespace_document(chat_id)
    assert "artifacts" not in document["services"]
    assert {
        row["alias"]
        for row in document["mounted_objects"]["artifacts"]["methods"]
    } == {"summarize"}

    result = await manager.execute(
        chat_id=chat_id,
        code="print(artifacts.summarize(value='alpha'))",
        run_id="mut-artifacts",
        outer_tool_call_id="mut-artifacts-outer",
    )
    reset = service.mutation.reset_slot(chat_id, "build/6")
    restored, _refs = service.namespace_document(chat_id)
    try:
        assert result.ok, result.to_dict()
        assert "'artifact_summary': 'ALPHA'" in result.output.text()
        assert reset["slot_version"] == 0
        assert {
            row["alias"]
            for row in restored["mounted_objects"]["artifacts"]["methods"]
        } == {
            "list", "read_text", "read_bytes", "save", "get", "create",
        }
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mutation_replaces_and_resets_the_children_object_slot(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "mutated-children-object"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "operate")
    proposal = service.mutation.propose(
        chat_id,
        kind="mutate",
        slot="operate/2",
        parent="children",
        alias="echo",
        purpose="Replace the children object with one session method.",
        schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments['value'].upper()\n",
        tests=[{"arguments": {"value": "child"}, "expected": "CHILD"}],
    )
    await service.mutation.activate(
        chat_id, proposal["draft_id"], expected_mount_revision=1
    )
    document, _refs = service.namespace_document(chat_id)
    assert {
        method["alias"]
        for method in document["mounted_objects"]["children"]["methods"]
    } == {"echo"}
    result = await manager.execute(
        chat_id=chat_id,
        code="print(children.echo(value='worker'))",
        run_id="mut-children",
        outer_tool_call_id="mut-children-outer",
    )
    service.mutation.reset_slot(chat_id, "operate/2")
    restored, _refs = service.namespace_document(chat_id)
    try:
        assert result.ok, result.to_dict()
        assert "WORKER" in result.output.text()
        assert {
            method["alias"]
            for method in restored["mounted_objects"]["children"]["methods"]
        } == {"spawn", "list", "tree"}
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_toolbelt_and_session_are_immutable_base_objects(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "immutable-base-objects"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    document, _refs = service.namespace_document(chat_id)
    assert set(document["python_apis"]) == {"toolbelt", "session"}
    with pytest.raises(MutationError) as caught:
        service.mutation.propose(
            chat_id,
            kind="mutate",
            slot="base/1",
            parent="toolbelt",
            alias="ping",
            purpose="Infrastructure roots cannot be mutated.",
            schema={"type": "object", "properties": {}},
            source="def run(arguments):\n    return arguments\n",
        )
    assert caught.value.code == "invalid_slot"
    result = await manager.execute(
        chat_id=chat_id,
        code="print(toolbelt.methods())\nprint(session.status()['chat_id'])",
        run_id="immutable-base",
        outer_tool_call_id="immutable-base-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "propose_activate" in result.output.text()
        assert chat_id in result.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_activation_rejects_cross_slot_projected_alias_collision(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "alias-collision"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposal = service.mutation.propose(
        chat_id,
        kind="create",
        slot="build/8",
        alias="apply_patch",
        purpose="Attempt to reuse an occupied direct-seed tools alias.",
        schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments['value']\n",
        tests=[{"arguments": {"value": "ok"}, "expected": "ok"}],
    )

    with pytest.raises(MutationError) as caught:
        await service.mutation.activate(
            chat_id, proposal["draft_id"], expected_mount_revision=1
        )
    try:
        assert caught.value.code == "alias_conflict"
        assert caught.value.details["alias"] == "tools.apply_patch"
        assert service.mutation.status(chat_id)["active"] == []
        document, _refs = service.namespace_document(chat_id)
        aliases = [row["alias"] for row in document["capabilities"]]
        assert aliases.count("apply_patch") == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_oversized_activated_result_is_accounted_and_rolled_back(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "oversized-invocation"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposal = service.mutation.propose(
        chat_id,
        kind="create",
        slot="build/8",
        alias="oversized_result",
        purpose="Exercise the admitted mutation response frame boundary.",
        schema={
            "type": "object",
            "required": ["large"],
            "properties": {"large": {"type": "boolean"}},
        },
        source=(
            "def run(arguments):\n"
            "    return 'x' * (2100000 if arguments['large'] else 1)\n"
        ),
        tests=[{"arguments": {"large": False}, "expected": "x"}],
    )
    await service.mutation.activate(
        chat_id, proposal["draft_id"], expected_mount_revision=1
    )

    result = await manager.execute(
        chat_id=chat_id,
        code="print(tools.oversized_result(large=True))",
        run_id="oversized-run",
        outer_tool_call_id="oversized-outer",
    )
    try:
        status = service.mutation.status(chat_id)
        assert not result.ok
        assert status["active"] == []
        assert any(
            row["stage"] == "invocation"
            and row["code"] == "worker_frame_quota"
            for row in status["failures"]
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mutate_live_remount_probation_and_reset(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "mutable-a")
    service.select("mutable-a", "build")
    proposed = service.mutation.propose(
        "mutable-a",
        kind="mutate",
        slot="build/4",
        parent="apply_patch",
        alias="apply_patch",
        purpose="Return an upper-case deterministic value.",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    return {'mutated': arguments['value'].upper()}\n"
        ),
        tests=[{
            "arguments": {"value": "candidate"},
            "expected": {"mutated": "CANDIDATE"},
        }],
    )
    activated = await service.mutation.activate(
        "mutable-a", proposed["draft_id"], expected_mount_revision=1
    )
    assert activated["slot_version"] == 1
    document, refs = service.namespace_document("mutable-a")
    mutated = next(
        row for row in document["capabilities"] if row["alias"] == "apply_patch"
    )
    assert mutated["session_local"] is True
    assert mutated["ref_id"] in refs

    first = await manager.execute(
        chat_id="mutable-a",
        code="old = tools.apply_patch\nprint(tools.apply_patch(value='alpha'))",
        run_id="mut-run-1", outer_tool_call_id="mut-outer-1",
    )
    second = await manager.execute(
        chat_id="mutable-a", code="print(tools.apply_patch(value='beta'))",
        run_id="mut-run-2", outer_tool_call_id="mut-outer-2",
    )
    status = service.mutation.status("mutable-a")
    reset = service.mutation.reset_slot("mutable-a", "build/4")
    stale = await manager.execute(
        chat_id="mutable-a",
        code=(
            "try:\n"
            "    old(value='gamma')\n"
            "except Variant1CapabilityError as exc:\n"
            "    print('STALE=' + exc.code)"
        ),
        run_id="mut-run-3", outer_tool_call_id="mut-outer-3",
    )
    try:
        assert first.ok and "ALPHA" in first.output.text()
        assert second.ok and "BETA" in second.output.text()
        assert status["probation"][0]["status"] == "passed"
        assert reset["slot_version"] == 0
        assert stale.ok and "STALE=" in stale.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_lkg_mutation_stays_mounted_after_later_semantic_failure(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "lkg-semantic-failure"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposed = service.mutation.propose(
        chat_id,
        kind="mutate",
        slot="build/4",
        parent="apply_patch",
        alias="apply_patch",
        purpose="Remain available after passing probation.",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    if arguments['value'] == 'bad-call':\n"
            "        raise ValueError('request is semantically invalid')\n"
            "    return {'mutated': arguments['value'].upper()}\n"
        ),
        tests=[{
            "arguments": {"value": "candidate"},
            "expected": {"mutated": "CANDIDATE"},
        }],
    )
    activated = await service.mutation.activate(
        chat_id, proposed["draft_id"], expected_mount_revision=1
    )

    first = await manager.execute(
        chat_id=chat_id,
        code="print(tools.apply_patch(value='alpha'))",
        run_id="lkg-pass-1",
        outer_tool_call_id="lkg-pass-outer-1",
    )
    second = await manager.execute(
        chat_id=chat_id,
        code="print(tools.apply_patch(value='beta'))",
        run_id="lkg-pass-2",
        outer_tool_call_id="lkg-pass-outer-2",
    )
    failed = await manager.execute(
        chat_id=chat_id,
        code="tools.apply_patch(value='bad-call')",
        run_id="lkg-fail",
        outer_tool_call_id="lkg-fail-outer",
    )
    recovered = await manager.execute(
        chat_id=chat_id,
        code="print(tools.apply_patch(value='gamma'))",
        run_id="lkg-recover",
        outer_tool_call_id="lkg-recover-outer",
    )
    status = service.mutation.status(chat_id)
    probation = status["probation"][0]
    try:
        assert first.ok and second.ok
        assert failed.ok is False
        assert recovered.ok and "GAMMA" in recovered.output.text()
        assert probation["status"] == "passed"
        assert probation["calls"] == 2
        assert probation["successful_calls"] == 2
        assert probation["semantic_errors"] == 0
        assert status["active"][0]["version"] == activated["slot_version"]
        assert any(
            row["stage"] == "invocation"
            and row["code"] == "candidate_execution_error"
            for row in status["failures"]
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_off_hides_authoring_keeps_overlay_live_and_pauses_probation(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "authority-off-overlay"
    enabled = _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposed = service.mutation.propose(
        chat_id,
        kind="mutate",
        slot="build/4",
        parent="apply_patch",
        alias="apply_patch",
        purpose="Keep one activated overlay callable while authoring is off.",
        schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    return {'off_overlay': arguments['value'].upper()}\n"
        ),
        tests=[{
            "arguments": {"value": "candidate"},
            "expected": {"off_overlay": "CANDIDATE"},
        }],
    )
    activated = await service.mutation.activate(
        chat_id, proposed["draft_id"], expected_mount_revision=1
    )
    disabled = runtimes.set_mutation_write_enabled(
        chat_id,
        False,
        actor="test",
        expected_revision=enabled.mutation_authority_revision,
    )

    document, refs = service.namespace_document(chat_id)
    toolbelt_aliases = _object_method_aliases(document, "toolbelt")
    overlay = next(
        row for row in document["capabilities"]
        if row["alias"] == "apply_patch"
    )
    assert document["mutation"]["availability"] == "disabled_by_chat"
    assert document["session"]["mutation_write_enabled"] is False
    assert document["session"]["mutation_authority_revision"] == (
        disabled.mutation_authority_revision
    )
    assert toolbelt_aliases == {
        "search", "mount", "mutation_status", "rollback", "reset", "reset_all",
    }
    assert overlay["session_local"] is True and overlay["ref_id"] in refs
    with pytest.raises(MutationError) as proposed_off:
        service.mutation.propose(
            chat_id,
            kind="mutate",
            slot="build/4",
            parent="apply_patch",
            alias="apply_patch",
            purpose="Must remain blocked while Off.",
            schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string"}},
            },
            source="def run(arguments):\n    return arguments\n",
        )
    assert proposed_off.value.code == "mutation_write_disabled"

    off_call = await manager.execute(
        chat_id=chat_id,
        code="print(tools.apply_patch(value='alpha'))",
        run_id="off-overlay-run",
        outer_tool_call_id="off-overlay-outer",
    )
    paused = service.mutation.status(chat_id)["probation"][0]
    await manager.close_chat(chat_id, reason="authority_test_toggle")
    runtimes.set_mutation_write_enabled(
        chat_id,
        True,
        actor="test",
        expected_revision=disabled.mutation_authority_revision,
    )
    resumed_call = await manager.execute(
        chat_id=chat_id,
        code="print(tools.apply_patch(value='beta'))",
        run_id="on-overlay-run-1",
        outer_tool_call_id="on-overlay-outer-1",
    )
    resumed = service.mutation.status(chat_id)["probation"][0]
    passed_call = await manager.execute(
        chat_id=chat_id,
        code="print(tools.apply_patch(value='gamma'))",
        run_id="on-overlay-run-2",
        outer_tool_call_id="on-overlay-outer-2",
    )
    passed = service.mutation.status(chat_id)["probation"][0]
    try:
        assert off_call.ok and "ALPHA" in off_call.output.text()
        assert paused["calls"] == 0 and paused["status"] == "probation"
        assert resumed_call.ok and "BETA" in resumed_call.output.text()
        assert resumed["calls"] == 1 and resumed["status"] == "probation"
        assert passed_call.ok and "GAMMA" in passed_call.output.text()
        assert passed["calls"] == 2 and passed["status"] == "passed"
        assert activated["slot_version"] == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_validate_commit_is_fenced_by_authority_revision(
    mutable_stack, monkeypatch,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "authority-validate-fence"
    enabled = _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposed = service.mutation.propose(
        chat_id,
        kind="mutate",
        slot="build/4",
        parent="apply_patch",
        alias="apply_patch",
        purpose="Fence an async validation result after authority changes.",
        schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments['value']\n",
    )

    async def revoke_during_worker(_request, *, proxy_call):
        del proxy_call
        runtimes.set_mutation_write_enabled(
            chat_id,
            False,
            actor="test-race",
            expected_revision=enabled.mutation_authority_revision,
        )
        return {"ok": True}

    monkeypatch.setattr(service.mutation.worker, "run", revoke_during_worker)
    with pytest.raises(MutationError) as caught:
        await service.mutation.validate(chat_id, proposed["draft_id"])
    try:
        assert caught.value.code == "mutation_authority_changed"
        draft = service.mutation._draft(chat_id, proposed["draft_id"])
        assert draft["status"] == "draft"
        assert draft["validation"] is None
        status = service.mutation.status(chat_id)
        assert status["authority"]["write_enabled"] is False
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_probation_rollback_failure_is_visible(mutable_stack, monkeypatch):
    runtimes, _broker, service, _manager = mutable_stack
    _enable_mutation(runtimes, "mutable-rollback-failure")
    service.select("mutable-rollback-failure", "build")
    proposed = service.mutation.propose(
        "mutable-rollback-failure",
        kind="mutate",
        slot="build/4",
        parent="apply_patch",
        alias="apply_patch",
        purpose="Candidate whose probation rollback is exercised.",
        schema={
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return {'ok': True}\n",
        tests=[{"arguments": {"value": "test"}, "expected": {"ok": True}}],
    )
    activated = await service.mutation.activate(
        "mutable-rollback-failure",
        proposed["draft_id"],
        expected_mount_revision=1,
    )

    def fail_rollback(*_args, **_kwargs):
        raise RuntimeError("simulated transition failure")

    monkeypatch.setattr(service.mutation, "rollback", fail_rollback)
    with pytest.raises(MutationError, match="could not be rolled back") as caught:
        await service.mutation._probation_result(
            "mutable-rollback-failure",
            activated["slot_id"],
            activated["slot_version"],
            ok=False,
            mechanical=True,
        )

    assert caught.value.code == "probation_rollback_failed"
    status = service.mutation.status("mutable-rollback-failure")
    assert status["probation"][0]["status"] == "rollback_failed"


@pytest.mark.asyncio
async def test_vacancy_candidate_uses_normal_seed_proxy_with_inferred_dependency(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "mutable-b")
    service.select("mutable-b", "explore")
    proposal = service.mutation.propose(
        "mutable-b",
        kind="create",
        slot="explore/4",
        alias="read_upper",
        purpose="Read through the admitted seed and return an upper-case path.",
        schema={
            "type": "object", "required": ["path"],
            "properties": {"path": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    value = tools.read_file(path=arguments['path'])\n"
            "    return {'path': value['path'].upper(), 'chat_id': value['chat_id']}\n"
        ),
        tests=[{
            "arguments": {"path": "delta"},
            "mocks": {"read_file": {"path": "delta", "chat_id": "test-chat"}},
            "expected": {"path": "DELTA", "chat_id": "test-chat"},
        }],
    )
    draft = service.mutation._draft("mutable-b", proposal["draft_id"])
    assert [row["proxy"] for row in draft["dependencies"]] == [
        "tools.read_file"
    ]
    assert "capabilities" not in draft
    assert "effects" not in draft
    await service.mutation.activate(
        "mutable-b", proposal["draft_id"], expected_mount_revision=1
    )
    result = await manager.execute(
        chat_id="mutable-b",
        code="print(tools.read_upper('delta'))",
        run_id="vacancy-run", outer_tool_call_id="vacancy-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "DELTA" in result.output.text()
        assert "mutable-b" in result.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_active_mutation_invokes_host_owned_remote_handle(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "mutation-remote-handle"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    proposal = service.mutation.propose(
        chat_id,
        kind="create",
        slot="build/8",
        alias="lookup_via_handle",
        purpose="Use a host-owned handle returned by a normal proxy.",
        schema={
            "type": "object",
            "required": ["item_id"],
            "properties": {"item_id": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    handle = tools.read_file(path='remote-handle')\n"
            "    result = handle.invoke(\n"
            "        arguments={'item_id': arguments['item_id']}, conclude=False\n"
            "    )\n"
            "    return {'value': result['value'], 'handle_id': handle.id}\n"
        ),
        tests=[{
            "arguments": {"item_id": "mock-item"},
            "mocks": {
                "tools.read_file": _connector_handle_envelope(),
                MUTATION_REMOTE_HANDLE_PROXY: _mcp_lookup_result("mock-item"),
            },
            "expected": {
                "value": "resolved-mock-item",
                "handle_id": "lease-test-1",
            },
        }],
    )
    activated = await service.mutation.activate(
        chat_id, proposal["draft_id"], expected_mount_revision=1
    )
    assert activated["slot_version"] == 1
    draft = service.mutation._draft(chat_id, proposal["draft_id"])
    assert {row["proxy"] for row in draft["dependencies"]} == {
        "tools.read_file",
        MUTATION_REMOTE_HANDLE_PROXY,
    }

    result = await manager.execute(
        chat_id=chat_id,
        code="print(tools.lookup_via_handle(item_id='live-item'))",
        run_id="mutation-remote-handle-run",
        outer_tool_call_id="mutation-remote-handle-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "resolved-live-item" in result.output.text()
        assert "lease-test-1" in result.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_candidate_mocks_support_repeated_proxy_result_sequences(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "mock-sequence")
    service.select("mock-sequence", "build")
    proposal = service.mutation.propose(
        "mock-sequence",
        kind="create",
        slot="build/8",
        alias="join_pair",
        purpose="Join two independently mocked reads.",
        schema={
            "type": "object",
            "required": ["left", "right"],
            "properties": {
                "left": {"type": "string"},
                "right": {"type": "string"},
            },
        },
        source=(
            "def run(arguments):\n"
            "    left = tools.read_file(path=arguments['left'])\n"
            "    right = tools.read_file(path=arguments['right'])\n"
            "    return left + '::' + right\n"
        ),
        tests=[{
            "arguments": {"left": "left.txt", "right": "right.txt"},
            "mocks": {
                "read_file": {"$sequence": ["LEFT", "RIGHT"]},
            },
            "expected": "LEFT::RIGHT",
        }],
    )

    activated = await service.mutation.activate(
        "mock-sequence", proposal["draft_id"], expected_mount_revision=1,
    )
    try:
        assert activated["slot_version"] == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_vacancy_candidate_uses_normal_mounted_object_syntax(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "object-proxy-mutation"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "explore")
    proposal = service.mutation.propose(
        chat_id,
        kind="create",
        slot="explore/4",
        alias="click_known_point",
        purpose="Reuse one normal computer click composition.",
        schema={
            "type": "object",
            "required": ["view"],
            "properties": {"view": {"type": "object"}},
        },
        source=(
            "def run(arguments):\n"
            "    return computer.click(view=arguments['view'], x=3, y=4)\n"
        ),
        tests=[{
            "arguments": {"view": {"id": "mock-view"}},
            "mocks": {
                "computer.click": {
                    "operation": "click",
                    "view": {"id": "mock-view"},
                    "x": 3,
                    "y": 4,
                },
            },
            "expected": {
                "operation": "click",
                "view": {"id": "mock-view"},
                "x": 3,
                "y": 4,
            },
        }],
    )
    draft = service.mutation._draft(chat_id, proposal["draft_id"])
    assert [row["proxy"] for row in draft["dependencies"]] == [
        "computer.click"
    ]
    await service.mutation.activate(
        chat_id, proposal["draft_id"], expected_mount_revision=1
    )
    result = await manager.execute(
        chat_id=chat_id,
        code="print(tools.click_known_point(view={'id': 'live-view'}))",
        run_id="object-proxy-run",
        outer_tool_call_id="object-proxy-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "live-view" in result.output.text()
        assert "'operation': 'click'" in result.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_callable_mutation_preserves_and_accumulates_object_methods(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "callable-object-mutation"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "explore")
    code = (
        "def fixed_click(view, x=0, y=0):\n"
        "    return {'replacement': computer.click(view=view, x=x + 10, y=y + 20)}\n"
        "toolbelt.mutate(computer.click, using=fixed_click)\n"
        "def fixed_observe(view):\n"
        "    return {'replacement': computer.observe(view=view), "
        "'prior': computer.click(view=view, x=5, y=6)}\n"
        "toolbelt.mutate(computer.observe, using=fixed_observe)\n"
        "print(computer.methods())\n"
        "print(computer.click(view={'id': 'live'}, x=1, y=2))\n"
        "print(computer.observe(view={'id': 'state'}))\n"
    )
    result = await manager.execute(
        chat_id=chat_id,
        code=code,
        run_id="callable-object-run",
        outer_tool_call_id="callable-object-outer",
    )
    try:
        assert result.ok, result.to_dict()
        text = result.output.text()
        assert "['click', 'observe']" in text
        assert "'x': 11" in text
        assert "'y': 22" in text
        assert "'operation': 'observe'" in text
        assert "'x': 15" in text
        assert "'y': 26" in text
        status = service.mutation.status(chat_id)
        assert status["active"][0]["version"] == 2
        assert [row["declared_kind"] for row in status["drafts"][:2]] == [
            "method", "method",
        ]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_callable_mutation_accepts_one_assertion_without_list_wrapper(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "single-callable-mutation-test"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    code = (
        "def repaired_patch(value: str):\n"
        "    return {'repaired': value.upper()}\n"
        "def repaired_patch_test():\n"
        "    assert repaired_patch('ok') == {'repaired': 'OK'}\n"
        "activated = toolbelt.mutate(\n"
        "    tools.apply_patch, using=repaired_patch,\n"
        "    tests=repaired_patch_test, purpose='Repair patch for this chat.'\n"
        ")\n"
        "print(activated['invocation']['call'])\n"
        "print(tools.apply_patch(value='ready'))\n"
    )
    result = await manager.execute(
        chat_id=chat_id,
        code=code,
        run_id="single-callable-test-run",
        outer_tool_call_id="single-callable-test-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "tools.apply_patch(value)" in result.output.text()
        assert "'repaired': 'READY'" in result.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_callable_synthesis_can_enter_probation_without_declared_examples(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "callable-synthesis"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    result = await manager.execute(
        chat_id=chat_id,
        code=(
            "def uppercase(value):\n"
            "    return value.upper()\n"
            "toolbelt.synthesize(uppercase)\n"
            "print(tools.uppercase(value='ready'))\n"
        ),
        run_id="callable-synthesis-run",
        outer_tool_call_id="callable-synthesis-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "READY" in result.output.text()
        assert service.mutation.status(chat_id)["active"][0]["version"] == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_callable_revises_active_synthesized_tool_in_workspace_lineage(
    mutable_stack,
    tmp_path,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "callable-synthesis-revision"
    workspace = tmp_path / "bound-workspace"
    workspace.mkdir()
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    result = await manager.execute(
        chat_id=chat_id,
        code=(
            "def workspace_writer(value):\n"
            "    from pathlib import Path\n"
            "    Path('mutation-relative.txt').write_text('v1:' + value, encoding='utf-8')\n"
            "    return {'version': 1, 'cwd': str(Path.cwd())}\n"
            "first = toolbelt.synthesize(\n"
            "    workspace_writer, slot=8, invoke={'value': 'alpha'}\n"
            ")\n"
            "def repaired_writer(value):\n"
            "    from pathlib import Path\n"
            "    Path('mutation-relative.txt').write_text('v2:' + value, encoding='utf-8')\n"
            "    return {'version': 2, 'cwd': str(Path.cwd())}\n"
            "second = toolbelt.mutate(\n"
            "    tools.workspace_writer, using=repaired_writer, invoke={'value': 'beta'}\n"
            ")\n"
            "print(first['slot_version'], second['slot_version'])\n"
            "print(second['result'])\n"
        ),
        run_id="callable-synthesis-revision-run",
        outer_tool_call_id="callable-synthesis-revision-outer",
        workspace_roots=(str(workspace),),
    )
    status = service.mutation.status(chat_id)
    with service.mutation._lock, service.mutation._connect() as conn:
        versions = [dict(row) for row in conn.execute(
            "SELECT version, draft_id, previous_version, status "
            "FROM astb_slot_version WHERE chat_id=? ORDER BY version",
            (chat_id,),
        ).fetchall()]
    with pytest.raises(MutationError) as duplicate_create:
        service.mutation.propose(
            chat_id,
            kind="create",
            slot="8",
            alias="another_tool",
            purpose="Do not replace an active synthesized slot with a new lineage.",
            schema={
                "type": "object",
                "properties": {},
            },
            source="def run(arguments):\n    return arguments\n",
        )
    rolled_back = service.mutation.rollback(
        chat_id, "8", to_version=1
    )
    original = await manager.execute(
        chat_id=chat_id,
        code="print(tools.workspace_writer(value='gamma'))",
        run_id="callable-synthesis-revision-rollback-run",
        outer_tool_call_id="callable-synthesis-revision-rollback-outer",
        workspace_roots=(str(workspace),),
    )
    try:
        assert result.ok, result.to_dict()
        assert "1 2" in result.output.text()
        assert "'version': 2" in result.output.text()
        assert (workspace / "mutation-relative.txt").read_text(
            encoding="utf-8"
        ) == "v1:gamma"
        assert [row["previous_version"] for row in versions] == [0, 1]
        assert duplicate_create.value.code == "slot_already_active"
        assert [row["declared_kind"] for row in status["drafts"][:2]] == [
            "revise", "create",
        ]
        assert status["active"][0]["version"] == 2
        assert rolled_back["slot_version"] == 1
        assert original.ok and "'version': 1" in original.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_activation_executes_and_blocks_failing_candidate_tests(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "candidate-gate")
    service.select("candidate-gate", "build")
    proposal = service.mutation.propose(
        "candidate-gate",
        kind="mutate", slot="build/4", parent="apply_patch",
        alias="apply_patch", purpose="Candidate must satisfy its declared oracle.",
        schema={
            "type": "object", "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments['value'].upper()\n",
        tests=[{"arguments": {"value": "alpha"}, "expected": "WRONG"}],
    )

    with pytest.raises(MutationError) as caught:
        await service.mutation.activate(
            "candidate-gate", proposal["draft_id"], expected_mount_revision=1
        )
    try:
        assert caught.value.code == "candidate_tests_failed"
        assert "expected_mismatch" in str(caught.value)
        assert service.mutation.status("candidate-gate")["active"] == []
        assert runtimes.ensure_runtime("candidate-gate").identity.mount_revision == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_validate_is_one_shot_and_validated_draft_can_activate(mutable_stack):
    runtimes, _broker, service, _manager = mutable_stack
    _enable_mutation(runtimes, "validate-once")
    service.select("validate-once", "build")
    proposal = service.mutation.propose(
        "validate-once",
        kind="mutate", slot="build/4", parent="apply_patch",
        alias="apply_patch", purpose="Validate this candidate exactly once.",
        schema={
            "type": "object", "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments\n",
        tests=[{
            "arguments": {"value": "alpha"},
            "expected": {"value": "alpha"},
        }],
    )

    receipt = await service.mutation.validate(
        "validate-once", proposal["draft_id"]
    )
    assert receipt["ok"] is True

    with pytest.raises(MutationError) as caught:
        await service.mutation.validate("validate-once", proposal["draft_id"])

    assert caught.value.code == "draft_not_validatable"
    assert caught.value.details == {"status": "validated"}

    activated = await service.mutation.activate(
        "validate-once", proposal["draft_id"], expected_mount_revision=1
    )
    assert activated["slot_version"] == 1


@pytest.mark.asyncio
async def test_garbage_collected_slot_version_releases_retained_quota(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "gc-releases-version-quota"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "operate")
    proposal = service.mutation.propose(
        chat_id,
        kind="mutate",
        slot="build/4",
        parent="apply_patch",
        alias="apply_patch",
        purpose="Verify retained mutation quota ignores collected history.",
        schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments\n",
        tests=[{"arguments": {"value": "ok"}, "expected": {"value": "ok"}}],
    )
    await service.mutation.validate(chat_id, proposal["draft_id"])
    draft = service.mutation._draft(chat_id, proposal["draft_id"])
    with sqlite3.connect(service.mutation.path) as conn:
        for version in range(1, 9):
            conn.execute(
                "INSERT INTO astb_slot_version(chat_id,slot_id,version,draft_id,"
                "content_sha256,previous_version,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (
                    chat_id,
                    draft["slot_id"],
                    version,
                    f"historical-draft-{version}",
                    f"digest-{version}",
                    version - 1,
                    "garbage_collected" if version == 1 else "probation",
                ),
            )
    mount_revision = int(
        runtimes.ensure_runtime(chat_id).identity.mount_revision or 0
    )
    activated = await service.mutation.activate(
        chat_id,
        proposal["draft_id"],
        expected_mount_revision=mount_revision,
    )
    try:
        assert activated["slot_version"] == 9
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_validate_cas_does_not_overwrite_a_newer_draft_state(
    mutable_stack, monkeypatch,
):
    runtimes, _broker, service, _manager = mutable_stack
    _enable_mutation(runtimes, "validate-cas")
    service.select("validate-cas", "build")
    proposal = service.mutation.propose(
        "validate-cas",
        kind="mutate", slot="build/4", parent="apply_patch",
        alias="apply_patch", purpose="Exercise validation status CAS.",
        schema={
            "type": "object", "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments\n",
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_validation(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr(service.mutation.worker, "run", paused_validation)
    task = asyncio.create_task(service.mutation.validate(
        "validate-cas", proposal["draft_id"]
    ))
    await entered.wait()
    with service.mutation._lock, service.mutation._connect() as conn:
        conn.execute(
            "UPDATE mutation_draft SET status='tested' WHERE chat_id=? AND draft_id=?",
            ("validate-cas", proposal["draft_id"]),
        )
    release.set()

    with pytest.raises(MutationError) as caught:
        await task

    assert caught.value.code == "draft_not_validatable"
    assert caught.value.details == {"status": "tested"}
    assert service.mutation._draft(
        "validate-cas", proposal["draft_id"]
    )["status"] == "tested"


@pytest.mark.asyncio
async def test_foundry_replay_e4_capsule_and_revocation_never_move_catalog(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "foundry-a")
    service.select("foundry-a", "build")
    proposal = service.mutation.propose(
        "foundry-a",
        kind="mutate", slot="build/4", parent="apply_patch",
        alias="apply_patch", purpose="Upper-case a deterministic value.",
        schema={
            "type": "object", "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    return {'value': arguments['value'].upper()}\n"
        ),
        tests=[{
            "arguments": {"value": "candidate"},
            "expected": {"value": "CANDIDATE"},
        }],
    )
    await service.mutation.activate(
        "foundry-a", proposal["draft_id"], expected_mount_revision=1
    )
    for index in range(2):
        result = await manager.execute(
            chat_id="foundry-a",
            code=f"print(tools.apply_patch(value='v{index}'))",
            run_id=f"foundry-run-{index}", outer_tool_call_id=f"foundry-outer-{index}",
        )
        assert result.ok, result.to_dict()
    before_catalog = service.repository.current().release_id
    extracted = _releases(service).extract_episode("foundry-a", proposal["draft_id"])
    replay = await _releases(service).replay(
        "foundry-a", proposal["draft_id"],
        cases=[
            {"arguments": {"value": "held-a"}, "expected": {"value": "HELD-A"}},
            {"arguments": {"value": "held-b"}, "expected": {"value": "HELD-B"}},
        ],
        baseline_passes=0,
        held_out=True,
        oracle_digest="oracle-sha256-test",
        evaluator_version="test.replay.v1",
    )
    assert service.mutation._draft("foundry-a", proposal["draft_id"])["status"] == "active"
    approval = _releases(service).approve_e4(
        "foundry-a", proposal["draft_id"],
        security_report={"status": "passed", "suite": "test-security-v1"},
        canary_report={"status": "passed", "suite": "test-canary-v1"},
        compatibility={"models": ["test-model"], "profile": "disclosure.topk.v1"},
        approved_by="test-operator",
        approval_note="explicit test approval",
        evaluator_version="test.release-review.v1",
    )
    capsule = _releases(service).publish_capsule(
        "foundry-a", proposal["draft_id"],
        approved_by="test-operator",
        compatibility={"models": ["test-model"], "profile": "disclosure.topk.v1"},
    )
    selected = _releases(service).select_release(capsule["release_id"], channel="candidate")
    selected_again = _releases(service).select_release(
        capsule["release_id"], channel="candidate"
    )
    assert selected_again["already_selected"] is True
    assert selected_again["revision"] == selected["revision"]
    assert selected_again["previous_release_id"] == selected["previous_release_id"]
    # Exercise a full two-release pointer window independently from publication.
    with _releases(service)._connect() as conn:
        base = conn.execute(
            "SELECT * FROM catalog_release_release WHERE release_id=?",
            (capsule["release_id"],),
        ).fetchone()
        conn.execute(
            "INSERT INTO catalog_release_release(release_id, draft_id, "
            "base_catalog_release_id, content_sha256, artifact_ref, evidence_digest, "
            "compatibility_json, rollback_release_id, status, approved_by, created_at) "
            "VALUES ('astb.capsule.rollback-drill.v1', ?, ?, ?, ?, ?, ?, ?, "
            "'approved_unselected', ?, ?)",
            (
                base["draft_id"], base["base_catalog_release_id"], "f" * 64,
                base["artifact_ref"], base["evidence_digest"],
                base["compatibility_json"], capsule["release_id"],
                base["approved_by"], float(base["created_at"]) + 1,
            ),
        )
    _releases(service).select_release("astb.capsule.rollback-drill.v1", channel="candidate")
    drill = _releases(service).revoke(
        "astb.capsule.rollback-drill.v1", reason="two-release drill",
        actor="test-operator",
    )
    status = _releases(service).status(chat_id="foundry-a")
    revoked = _releases(service).revoke(
        capsule["release_id"], reason="rollback drill", actor="test-operator"
    )
    try:
        assert extracted["levels"] == ["E0", "E1", "E2"]
        assert replay["status"] == "passed" and replay["uplift"] == 2
        assert replay["promotion_eligible"] is True
        assert replay["execution_mode"] == "same_user"
        assert approval["level"] == "E4"
        assert capsule["live_catalog_pointer_changed"] is False
        assert selected["live_catalog_pointer_changed"] is False
        assert service.repository.current().release_id == before_catalog
        assert status["live_catalog_pointer_mutable_from_foundry"] is False
        assert drill["restored_channels"]["candidate"] == capsule["release_id"]
        assert status["pointers"][0]["release_id"] == capsule["release_id"]
        assert all("source" not in row for row in status["evidence"])
        assert revoked["status"] == "revoked"
        assert service.repository.current().release_id == before_catalog
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_foundry_failed_replay_cannot_authorize_e4(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "foundry-unqualified")
    service.select("foundry-unqualified", "build")
    proposal = service.mutation.propose(
        "foundry-unqualified",
        kind="mutate", slot="build/4", parent="apply_patch",
        alias="apply_patch", purpose="Upper-case a deterministic value.",
        schema={
            "type": "object", "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    return {'value': arguments['value'].upper()}\n"
        ),
    )
    try:
        replay = await _releases(service).replay(
            "foundry-unqualified", proposal["draft_id"],
            cases=[{"arguments": {"value": "held"}, "expected": {"value": "WRONG"}}],
            baseline_passes=0,
            held_out=True,
            oracle_digest="oracle-sha256-unqualified",
            evaluator_version="test.replay.v1",
        )
        assert replay["status"] == "failed"
        assert replay["promotion_eligible"] is False
        with pytest.raises(ReleaseError, match="passing paired held-out replay"):
            _releases(service).approve_e4(
                "foundry-unqualified", proposal["draft_id"],
                security_report={"status": "passed"},
                canary_report={"status": "passed"},
                compatibility={"models": ["test-model"], "profile": "test-profile"},
                approved_by="test-operator",
                approval_note="failed replay must remain blocked",
                evaluator_version="test.release-review.v1",
            )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_identical_validation_failures_trip_session_breaker(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "breaker-a")
    service.select("breaker-a", "build")
    kwargs = dict(
        kind="mutate", slot="build/4", parent="apply_patch",
        alias="apply_patch", purpose="Invalid callable contract must stay closed.",
        schema={
            "type": "object", "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def helper(arguments):\n    return arguments\n",
    )
    for _ in range(2):
        proposal = service.mutation.propose("breaker-a", **kwargs)
        with pytest.raises(MutationError, match="validation did not pass"):
            await service.mutation.activate(
                "breaker-a", proposal["draft_id"], expected_mount_revision=1
            )
    with pytest.raises(MutationError) as caught:
        service.mutation.propose("breaker-a", **kwargs)
    try:
        assert caught.value.code == "identical_failure_breaker"
        status = service.mutation.status("breaker-a")
        assert len(status["failures"]) >= 3
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mutation_freeze_blocks_all_manual_authoring_until_released(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "freeze-a")
    service.select("freeze-a", "build")
    proposal = service.mutation.propose(
        "freeze-a", kind="mutate", slot="build/4", parent="apply_patch",
        alias="apply_patch", purpose="Simple candidate.",
        schema={
            "type": "object", "required": ["value"],
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return arguments['value']\n",
        tests=[{"arguments": {"value": "candidate"}, "expected": "candidate"}],
    )
    await service.mutation.activate(
        "freeze-a", proposal["draft_id"], expected_mount_revision=1
    )
    service.mutation.mutation_allowed = lambda: False
    frozen_document, _refs = service.namespace_document("freeze-a")
    assert frozen_document["mutation"]["availability"] == "frozen_by_host"
    assert _object_method_aliases(frozen_document, "toolbelt") == {
        "search", "mount", "mutation_status", "rollback", "reset", "reset_all",
    }
    frozen_patch = next(
        row for row in frozen_document["capabilities"]
        if row["alias"] == "apply_patch"
    )
    assert frozen_patch.get("session_local") is True
    with pytest.raises(MutationError) as caught:
        service.mutation.propose(
            "freeze-a", kind="mutate", slot="build/4", parent="apply_patch",
            alias="apply_patch", purpose="blocked",
            schema={
                "type": "object", "required": ["value"],
                "properties": {"value": {"type": "string"}},
            },
            source="def run(arguments):\n    return arguments\n",
        )
    reset = service.mutation.reset_slot("freeze-a", "build/4")
    try:
        assert caught.value.code == "mutation_frozen"
        assert reset["slot_version"] == 0
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_atomic_model_api_remounts_inside_the_same_cell(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    _enable_mutation(runtimes, "atomic-a")
    service.select("atomic-a", "build")
    code = (
        "source = \"def run(arguments):\\n"
        "    return {'atomic': arguments['value'][::-1]}\\n\"\n"
        "activated = toolbelt.mutate(\n"
        "    slot='build/4', purpose='Reverse one exact value.',\n"
        "    source=source,\n"
        "    tests=[{'arguments': {'value': 'drawer'}, "
        "'expected': {'atomic': 'reward'}}]\n"
        ")\n"
        "print(activated['slot_version'])\n"
        "print(tools.apply_patch(value='drawer'))\n"
    )
    result = await manager.execute(
        chat_id="atomic-a", code=code,
        run_id="atomic-run", outer_tool_call_id="atomic-outer",
    )
    try:
        assert result.ok, result.to_dict()
        assert "reward" in result.output.text()
        assert service.mutation.status("atomic-a")["active"][0]["version"] == 1
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_last_failure_can_be_mutated_and_invoked_in_one_call(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "failure-to-adaptation"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    code = (
        "try:\n"
        "    tools.apply_patch(value='fail')\n"
        "except Exception as error:\n"
        "    print(type(error).__name__, error.code)\n"
        "failure = toolbelt.last_failure()\n"
        "print(failure.target, failure.arguments)\n"
        "def fixed_patch(value: str):\n"
        "    return {'fixed': value}\n"
        "adapted = toolbelt.mutate(\n"
        "    failure, using=fixed_patch, invoke=failure.arguments\n"
        ")\n"
        "print(adapted)\n"
        "print(tools.apply_patch(value='again'))\n"
    )
    result = await manager.execute(
        chat_id=chat_id,
        code=code,
        run_id="failure-to-adaptation-run",
        outer_tool_call_id="failure-to-adaptation-outer",
    )
    try:
        assert result.ok, result.to_dict()
        text = result.output.text()
        assert "Variant1CapabilityError" in text
        assert "tools.apply_patch {'value': 'fail'}" in text
        assert "'adapted': 'tools.apply_patch'" in text
        assert "'fixed': 'fail'" in text
        assert "'fixed': 'again'" in text
        with service.mutation._lock, service.mutation._connect() as conn:
            invocations = [dict(row) for row in conn.execute(
                "SELECT run_id,outer_tool_call_id,cell_execution_id," 
                "nested_call_id,kernel_generation,status "
                "FROM mutation_invocation WHERE chat_id=? ORDER BY created_at",
                (chat_id,),
            ).fetchall()]
        assert invocations[0]["run_id"] == "failure-to-adaptation-run"
        assert invocations[0]["outer_tool_call_id"] == "failure-to-adaptation-outer"
        assert invocations[0]["cell_execution_id"].startswith("cell_")
        assert invocations[0]["nested_call_id"].startswith("mutation-first-use-")
        assert invocations[0]["kernel_generation"] == "1"
        assert invocations[0]["status"] == "ok"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_atomic_create_requires_tests_and_activates_one_vacancy(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "atomic-create"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    source = "def run(arguments):\n    return arguments['value'].upper()\n"
    schema = {
        "type": "object", "required": ["value"],
        "properties": {"value": {"type": "string"}},
    }
    with pytest.raises(MutationError) as missing:
        await service.mutation.synthesize(
            chat_id, slot="build/8", alias="uppercase", purpose="Uppercase text.",
            schema=schema, source=source, tests=[],
        )
    assert missing.value.code == "tests_required"

    activated = await service.mutation.synthesize(
        chat_id, slot="build/8", alias="uppercase", purpose="Uppercase text.",
        schema=schema, source=source,
        tests=[{"arguments": {"value": "ok"}, "expected": "OK"}],
    )
    result = await manager.execute(
        chat_id=chat_id, code="print(tools.uppercase(value='ready'))",
        run_id="atomic-create-run", outer_tool_call_id="atomic-create-outer",
    )
    revised = await service.mutation.mutate(
        chat_id,
        slot="8",
        source="def run(arguments):\n    return arguments['value'].lower()\n",
        tests=[{"arguments": {"value": "OK"}, "expected": "ok"}],
        purpose="Revise the active synthesized direct tool.",
    )
    revised_result = await manager.execute(
        chat_id=chat_id,
        code="print(tools.uppercase(value='READY'))",
        run_id="atomic-revise-run",
        outer_tool_call_id="atomic-revise-outer",
    )
    document, _refs = service.namespace_document(chat_id)
    try:
        assert activated["slot_version"] == 1
        assert result.ok and "READY" in result.output.text()
        assert revised["slot_version"] == 2
        assert revised_result.ok and "ready" in revised_result.output.text()
        assert service.mutation.status(chat_id)["drafts"][0][
            "declared_kind"
        ] == "revise"
        assert "- slot 8: tools.uppercase(value)" in document["mount_card"]
        assert "- slot 8: VACANT" not in document["mount_card"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["rollback", "reset"])
async def test_active_synthesized_tool_is_searchable_across_mounts_and_recovery(
    mutable_stack, recovery,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = f"effective-overlay-search-{recovery}"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    activated = await service.mutation.synthesize(
        chat_id,
        slot="build/7",
        alias="reconcile_jsonl",
        purpose="Reconcile JSONL records by stable identity.",
        schema={
            "type": "object",
            "required": ["jsonl"],
            "properties": {"jsonl": {"type": "string"}},
        },
        source=(
            "def run(arguments):\n"
            "    return arguments['jsonl'].strip()\n"
        ),
        tests=[{
            "arguments": {"jsonl": "alpha\n"},
            "expected": "alpha",
        }],
    )
    service.select(chat_id, "explore")
    document, _refs = service.namespace_document(
        chat_id, query="reconcile_jsonl",
    )

    try:
        assert activated["invocation"] == {
            "qualified_name": "tools.reconcile_jsonl",
            "call": "tools.reconcile_jsonl(jsonl)",
            "category_id": "build",
            "available": "next_cell",
        }
        assert document["top_k"][0]["alias"] == "reconcile_jsonl"
        assert document["top_k"][0]["qualified_alias"] == "tools.reconcile_jsonl"
        assert document["top_k"][0]["call"] == "tools.reconcile_jsonl(jsonl)"
        assert document["top_k"][0]["category_id"] == "build"
        assert document["top_k"][0]["session_local"] is True
        diagnostic = ToolbeltNamespace(document).missing_capability(
            "reconcile_jsonl"
        )
        assert "Catalog category: 'build'" in diagnostic
        assert "No pinned catalog match" not in diagnostic

        mount_revision = int(document["mount_revision"])
        if recovery == "rollback":
            service.mutation.rollback(
                chat_id,
                "build/7",
                expected_mount_revision=mount_revision,
            )
        else:
            service.mutation.reset_slot(
                chat_id,
                "build/7",
                expected_mount_revision=mount_revision,
            )
        recovered, _refs = service.namespace_document(
            chat_id, query="reconcile_jsonl",
        )
        assert not any(
            row.get("alias") == "reconcile_jsonl"
            for row in recovered["catalog_index"]
        )
        assert not any(
            row.get("alias") == "reconcile_jsonl"
            for row in recovered["top_k"]
        )
        assert "No pinned catalog match" in ToolbeltNamespace(
            recovered
        ).missing_capability("reconcile_jsonl")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_working_python_helper_promotes_through_atomic_create(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "promote-helper"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    code = (
        "def normalize_ticket(ticket_id: str, prefix: str = 'TK-'):\n"
        "    return prefix + ticket_id.strip().upper()\n"
        "activated = toolbelt.promote_helper(\n"
        "    normalize_ticket,\n"
        "    tests=[{'arguments': {'ticket_id': ' 17 '}, "
        "'expected': 'TK-17'}],\n"
        ")\n"
        "print(activated['slot_id'])\n"
        "print(tools.normalize_ticket(ticket_id=' 42 '))\n"
    )

    result = await manager.execute(
        chat_id=chat_id,
        code=code,
        run_id="promote-helper-run",
        outer_tool_call_id="promote-helper-outer",
    )
    document, _refs = service.namespace_document(chat_id)
    try:
        assert result.ok, result.to_dict()
        assert "build/7" in result.output.text()
        assert "TK-42" in result.output.text()
        assert document["category_options"][0]["vacant_slots"] == 1
        assert document["category_options"][0]["vacant_slot_ids"] == ["build/8"]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_working_proxy_helper_promotes_without_dependency_declarations(
    mutable_stack,
):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "promote-proxy-helper"
    _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    code = (
        "def read_path_upper(path: str):\n"
        "    return tools.read_file(path=path)['path'].upper()\n"
        "activated = toolbelt.promote_helper(\n"
        "    read_path_upper,\n"
        "    tests=[{\n"
        "        'arguments': {'path': 'mock.txt'},\n"
        "        'mocks': {'tools.read_file': {'path': 'mock.txt'}},\n"
        "        'expected': 'MOCK.TXT',\n"
        "    }],\n"
        ")\n"
        "print(activated['slot_id'])\n"
        "print(tools.read_path_upper(path='live.txt'))\n"
    )

    result = await manager.execute(
        chat_id=chat_id,
        code=code,
        run_id="promote-proxy-helper-run",
        outer_tool_call_id="promote-proxy-helper-outer",
    )
    status = service.mutation.status(chat_id)
    try:
        assert result.ok, result.to_dict()
        assert "build/7" in result.output.text()
        assert "LIVE.TXT" in result.output.text()
        draft = service.mutation._draft(
            chat_id, status["active"][0]["draft_id"]
        )
        assert [row["proxy"] for row in draft["dependencies"]] == [
            "tools.read_file"
        ]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_reset_remains_available_when_chat_mutation_is_off(mutable_stack):
    runtimes, _broker, service, manager = mutable_stack
    chat_id = "off-reset"
    enabled = _enable_mutation(runtimes, chat_id)
    service.select(chat_id, "build")
    await service.mutation.mutate(
        chat_id,
        slot="build/4",
        source="def run(arguments):\n    return arguments['value']\n",
        tests=[{"arguments": {"value": "ok"}, "expected": "ok"}],
    )
    runtimes.set_mutation_write_enabled(
        chat_id, False, actor="test",
        expected_revision=enabled.mutation_authority_revision,
    )
    reset = service.mutation.reset_slot(chat_id, "build/4")
    try:
        assert reset["slot_version"] == 0
        assert service.mutation.status(chat_id)["active"] == []
    finally:
        await manager.shutdown()
