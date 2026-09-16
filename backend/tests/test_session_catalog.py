"""Phase 4 immutable catalog, disclosure, mount, and routing gates."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import inspect
import json
import logging
import os
from types import SimpleNamespace

import pytest

from artifacts.store import ContentAddressedArtifactStore
from artifacts.blob_service import ArtifactBlobService
from artifacts.capabilities import register_artifact_tools
from session_catalog.catalog import (
    MOUNTED_OBJECT_BINDINGS,
    MOUNTED_OBJECT_HANDLER_NAMES,
    CatalogError,
    COVERED_BROKER_HANDLER_NAMES,
    MOUNTED_PYTHON_API_BINDINGS,
    MOUNTED_PYTHON_API_HANDLER_NAMES,
    ALL_MOUNTED_PYTHON_API_BINDINGS,
    CONDITIONAL_PYTHON_API_BINDINGS,
    MountConflict,
    SEED_SLOT_BLUEPRINT,
    binding_signature,
    build_catalog_document,
    category_ids,
    handler_only_catalog_follow_allowed,
)
from session_catalog.profiles import ACTION_SURFACE
from session_catalog.mutation import MUTATION_HANDLER
from session_catalog.service import (
    BROWSER_SELECTION_GUIDANCE,
    IPYTHON_HOST_CAPABILITY_CONTRACT,
    IPYTHON_PROVIDER_SPEC,
    CatalogService,
    _query_tokens,
)
from capability_broker import (
    CapabilityBroker,
    InvocationContext,
    current_capability_invocation,
)
from kernel_runtime.manager import KernelLimits, KernelRuntimeManager
from kernel_runtime.output import OutputLimits
from kernel_runtime.worker_bridge import CapabilityProxy, MountedPythonAPI, ReadOnlyTools
from session_catalog.toolbelt import TOOLBELT_OBJECT_METHODS
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from tools import BROWSER_OBJECT_METHODS, Tool, ToolRegistry
from work_fabric.capabilities import register_work_fabric_tools
from work_fabric.service import WorkService
from desktop.registry import COMPUTER_OBJECT_METHODS, _root_params as computer_root_params
from object_api import dispatcher_params


def test_binding_signature_orders_required_fields_before_canonical_optional_fields():
    params = {
        "limit": {"type": "integer", "required": False},
        "offset": {"type": "integer", "required": False},
        "path": {"type": "string", "required": True},
    }
    signature = binding_signature("read_file", params)
    proxy = CapabilityProxy(
        {"alias": "read_file", "params": params, "signature": signature},
        SimpleNamespace(),
    )
    assert signature == "read_file(path, limit=None, offset=None)"
    assert list(inspect.signature(proxy).parameters) == ["path", "limit", "offset"]


def test_declared_defaults_survive_signature_rendering_and_canonical_key_order():
    params = {'path': {'type': 'string', 'required': True},
              'z': {'type': 'string', 'default': 'a,b'},
              'a': {'type': 'object', 'default': {'values': [1, 2]}}}
    signature = binding_signature('example', params)
    proxy = CapabilityProxy({'alias': 'example', 'signature': signature,
                             'params': json.loads(json.dumps(params, sort_keys=True))}, SimpleNamespace())
    actual = inspect.signature(proxy).parameters
    assert list(actual) == ['path', 'z', 'a']
    assert actual['z'].default == 'a,b'
    assert actual['a'].default == {'values': [1, 2]}
    assert "z='a,b'" in signature
    assert "a={'values': [1, 2]}" in signature


def test_mount_card_exposes_actual_process_modes_defaults_and_lifecycle(catalog_stack):
    from shell_tool import _RUN_COMMAND_PARAMS
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    registry.get('run_command').params = json.loads(json.dumps(_RUN_COMMAND_PARAMS))
    service.reconcile_registry()
    runtimes.ensure_runtime('chat-mode-contract', is_new=True)
    selected = service.select('chat-mode-contract', 'build')
    document, _ = service.namespace_document('chat-mode-contract')
    card = selected['mount_card']
    assert "mode='once'" in card and 'timeout=120' in card
    for mode in _RUN_COMMAND_PARAMS['mode']['enum']:
        assert repr(mode) in card
    assert 'waits for command exit' in card and 'Owned children can outlive their launcher' in card
    descriptor = next(row for row in document['capabilities'] if row['alias'] == 'run_command')
    proxy = CapabilityProxy(descriptor, SimpleNamespace())
    assert inspect.signature(proxy).parameters['mode'].default == 'once'
    assert inspect.signature(proxy).parameters['timeout'].default == 120


def test_mount_contract_hints_are_bounded_and_follow_the_effective_schema():
    huge = {'mode': {'enum': ['x' * 300] * 1000, 'default': 'd' * 20000,
                     'desc': 'long ' * 10000}}
    row = {'alias': 'run_command', 'params': huge,
           'signature': binding_signature('run_command', huge)}
    rendered = CatalogService._mount_callable_contract(row, 'tools')
    assert len(rendered) < 1600
    assert '.describe()' in rendered and '1000 choices' in rendered
    assert rendered == CatalogService._mount_callable_contract(row, 'tools')
    replacement = {'alias': 'run_command', 'signature': 'run_command(mode=None)',
                   'params': {'mode': {'enum': ['custom'], 'default': 'custom'}}}
    replaced = CatalogService._mount_callable_contract(replacement, 'tools')
    assert "'custom'" in replaced and "'process'" not in replaced


def test_handler_only_follow_accepts_python_api_transport_revision_change():
    pinned = {
        "source_digest": "old",
        "categories": [{
            "slots": [],
            "python_apis": [{
                "name": "toolbelt",
                "transport": {
                    "capability_id": "toolbelt",
                    "handler_revision": "handler-old",
                },
                "methods": [],
            }],
        }],
    }
    current = {
        "source_digest": "new",
        "categories": [{
            "slots": [],
            "python_apis": [{
                "name": "toolbelt",
                "transport": {
                    "capability_id": "toolbelt",
                    "handler_revision": "handler-new",
                },
                "methods": [],
            }],
        }],
    }

    assert handler_only_catalog_follow_allowed(pinned, current) is True

SEED_NAMES = {
    "read_file", "glob", "grep", "apply_patch", "run_command",
    "web_search", "browser", "computer", "ask_user",
}


def test_opaque_result_label_does_not_select_a_capability_family():
    terms = _query_tokens("Reply with exactly DONE: CHILD-4BC0AB9D9DF647134385.")

    assert "child" not in terms
    assert {"reply", "exactly", "done"}.issubset(terms)

EXECUTION_ABSORBED_HANDLER_NAMES = {
    "terminal_open", "terminal_list", "terminal_get", "terminal_read",
    "terminal_write", "terminal_resize", "terminal_signal", "terminal_wait",
    "terminal_detach", "terminal_close",
    "process_start", "process_list", "process_get", "process_logs",
    "process_write", "process_signal", "process_wait", "process_stop",
}
CODING_ABSORBED_HANDLER_NAMES = {
    "git_discover", "git_status", "git_diff", "git_branches", "git_log",
    "git_show", "git_stage", "git_unstage", "git_commit",
    "worktree_create", "worktree_get", "worktree_list",
    "worktree_remove", "worktree_reconcile",
}
DESKTOP_ACTION_ABSORBED_HANDLER_NAMES = {
    "desktop_act", "desktop_operations", "desktop_events",
}
DESKTOP_OBSERVATION_ABSORBED_HANDLER_NAMES = {
    "desktop_observe", "desktop_capture",
}
DESKTOP_CATALOG_ABSORBED_HANDLER_NAMES = {
    "desktop_catalog", "desktop_apps", "desktop_windows",
}


def _api_method_aliases(document, name):
    api = (
        (document.get("mounted_objects") or {}).get(name)
        or (document.get("python_apis") or {}).get(name)
        or {}
    )
    return {row["alias"] for row in api.get("methods") or ()}


def _object_tool(name, methods, *, effect="read"):
    async def no_op(arguments):
        return arguments

    rows = []
    for method in methods:
        if isinstance(method, dict):
            rows.append(method)
            continue
        rows.append({
            "name": str(method),
            "description": f"{name}.{method}",
            "effect_class": effect,
            "params": {},
        })
    return Tool(
        name,
        f"Test {name}.",
        no_op,
        params={},
        hidden=True,
        visibility="broker_only",
        effect_class=effect,
        object_methods=tuple(rows),
    )


def _mounted_object_method_aliases(document, name):
    mounted = (document.get("mounted_objects") or {}).get(name) or {}
    return {row["alias"] for row in mounted.get("methods") or ()}


def test_capability_proxy_accepts_argument_envelopes_and_single_batch_items():
    class Bridge:
        def invoke(self, _descriptor, arguments):
            return arguments

    proxy = CapabilityProxy({
        "alias": "apply_patch",
        "params": {
            "changes": {
                "type": "array",
                "required": True,
                "items": {"type": "object"},
            },
        },
    }, Bridge())
    change = {"path": "answer.txt", "content": "ready"}

    assert proxy({"changes": [change]}) == {"changes": [change]}
    assert proxy(change) == {"changes": [change]}
    assert proxy(changes=[change]) == {"changes": [change]}
    assert proxy.documentation()["signature"] == "(changes: list)"
    assert proxy.documentation()["params"]["changes"]["required"] is True
    with pytest.raises(TypeError, match=r"tools\.apply_patch\(changes: list\).+documentation"):
        proxy([], [])
    with pytest.raises(TypeError, match=r"tools\.apply_patch\(changes: list\).+documentation"):
        proxy(content="ready")

    namespace = ReadOnlyTools([{
        "alias": "apply_patch",
        "description": "Recoverable patch.",
        "params": {"changes": {"type": "array", "required": True}},
    }], Bridge())
    assert namespace.documentation("apply_patch")["description"] == "Recoverable patch."
    assert set(namespace.documentation()) == {"apply_patch"}
    assert "params" not in namespace.documentation()["apply_patch"]
    assert namespace.methods() == namespace.aliases() == ["apply_patch"]


def test_capability_proxy_restores_signature_order_after_canonical_json_sorting():
    class Bridge:
        def invoke(self, _descriptor, arguments):
            return arguments

    proxy = CapabilityProxy({
        "alias": "focus",
        "signature": "focus(name=None, window=None)",
        # Canonical catalog JSON sorts this object alphabetically.
        "params": {
            "name": {"type": "string", "required": False},
            "window": {"type": "any", "required": False},
        },
    }, Bridge())

    assert list(inspect.signature(proxy).parameters) == [
        "name", "window",
    ]
    assert proxy("VARIANT-1 Desktop") == {
        "name": "VARIANT-1 Desktop",
    }


def test_toolbelt_atomic_mutation_schema_uses_normal_proxy_contract():
    create = next(
        row for row in TOOLBELT_OBJECT_METHODS
        if row["name"] == "synthesize"
    )
    params = create["params"]

    assert "build/8" in params["slot"]["desc"]
    assert "selected category" in params["slot"]["desc"]
    assert "capabilities" not in params
    assert "effects" not in params
    assert params["tests"]["items"]["properties"]["mocks"]["type"] == "object"
    assert "'$sequence'" in params["tests"]["desc"]
    assert "tools.read_file(path=...)" in params["source"]["desc"]
    assert "derives dependencies" in params["source"]["desc"]
    assert "chr(10)" in params["source"]["desc"]


def test_capability_proxy_composes_one_search_selection():
    class Bridge:
        def invoke(self, _descriptor, arguments):
            return arguments

    proxy = CapabilityProxy({
        "alias": "schema",
        "params": {
            "selection": {"type": "object", "required": False},
            "server_id": {"type": "string", "required": False},
            "tool_name": {"type": "string", "required": False},
        },
    }, Bridge(), namespace="plugins")
    match = {"server_id": "srv", "tool_name": "echo", "schema_digest": "abc"}

    assert proxy([match]) == {"selection": match}
    assert proxy("echo") == {"tool_name": "echo"}


def test_run_command_proxy_treats_a_positional_string_list_as_exact_argv():
    class Bridge:
        def invoke(self, _descriptor, arguments):
            return arguments

    proxy = CapabilityProxy({
        "alias": "run_command",
        "signature": "run_command(command=None, cwd=None, argv=None)",
        "params": {
            "command": {"type": "string", "required": False},
            "cwd": {"type": "string", "required": False},
            "argv": {
                "type": "array", "required": False,
                "items": {"type": "string"},
            },
        },
    }, Bridge())

    assert proxy(["git", "diff", "--", "ledger.txt"], cwd="workspace") == {
        "argv": ["git", "diff", "--", "ledger.txt"],
        "cwd": "workspace",
    }


def test_mounted_python_api_exposes_methods_with_lazy_local_documentation():
    class Bridge:
        def invoke(self, descriptor, arguments):
            return descriptor["alias"], arguments

    api = MountedPythonAPI({
        "name": "session",
        "summary": "Session controls.",
        "methods": [{
            "alias": "state",
            "description": "Read session state.",
            "signature": "state(limit=None)",
            "effect_class": "read",
            "params": {"limit": {"type": "integer", "required": False}},
        }],
    }, Bridge())

    assert api.state(limit=2) == ("state", {"limit": 2})
    assert api.methods() == ["state"]
    assert api.documentation("state")["signature"] == "state(limit=None)"
    assert ".async_(" in api.documentation("state")["awaitable"]
    assert set(api.documentation()["methods"]) == {"state"}
    assert "methods()" in repr(api)
    assert "describe(name)" in repr(api)
    assert "params" not in api.documentation()["methods"]["state"]
    with pytest.raises(AttributeError, match="read-only"):
        api.state = None


def test_public_jobs_root_is_absent_while_returned_handle_dispatch_remains(
    catalog_stack, tmp_path,
):
    registry, enabled, _runtimes, _artifacts, broker, service, _manager = catalog_stack
    work = WorkService.open(str(tmp_path / "work.sqlite3"), worker_id="catalog-test")
    runtime = SimpleNamespace(work=work, registry=registry, broker=broker)
    host = SimpleNamespace(
        registry=registry,
        capability_broker=broker,
        require_runtime=lambda: runtime,
    )
    register_work_fabric_tools(host)
    enabled.update(tool.name for tool in registry.all())
    service.reconcile_registry()
    service.select("chat-work", "operate")

    document, refs = service.namespace_document("chat-work")

    assert "work" not in document["services"]
    assert "jobs" not in document["services"]
    assert _api_method_aliases(document, "work") == set()
    assert _api_method_aliases(document, "jobs") == set()
    assert _api_method_aliases(document, "goals") == set()
    assert _api_method_aliases(document, "session") == {
        "status", "continuity", "configure_continuity", "checkpoint",
        "restart", "report_outcome",
    }
    prompt = service.runtime_prompt("chat-work", "check the current jobs")
    assert "jobs.list/get" not in prompt
    assert registry.get("jobs") is None
    hidden_ref = broker.ref_for_name(
        "remote_handle_dispatch",
        catalog_release_id=document["catalog_release_id"],
    )
    assert hidden_ref.opaque_id in refs
    assert all(
        operation.get("capability_id") != "remote_handle_dispatch"
        for api in [
            *document["python_apis"].values(),
            *document["mounted_objects"].values(),
        ]
        for operation in api.get("methods") or ()
    )


@pytest.mark.asyncio
async def test_kernel_continuity_is_absorbed_by_immutable_session_control(
    catalog_stack,
):
    registry, enabled, _runtimes, artifacts, broker, service, manager = catalog_stack
    host = SimpleNamespace(
        registry=registry,
        capability_broker=broker,
        session_artifacts=artifacts,
        kernel_runtime=manager,
        require_runtime=lambda: SimpleNamespace(kernel=manager),
    )
    service.host = host
    enabled.add("session")
    service.reconcile_registry()
    chat_id = "chat-kernel-continuity-dispatch"
    service.select(chat_id, "build")
    context = InvocationContext(
        chat_id=chat_id,
        run_id="run-kernel-continuity",
        outer_tool_call_id="outer-kernel-continuity",
        cell_execution_id="cell-kernel-continuity",
        nested_call_id="nested-kernel-continuity",
        catalog_release_id=service.current_release_id,
        kernel_generation="1",
        surface="ipython",
    )

    configured = await broker.invoke_name(
        "session",
        {
            "operation": "configure_continuity",
            "restore_on_boot": True,
        },
        context,
    )
    assert configured.ok, configured.to_dict()
    assert configured.result_value["policy"]["source"] == "chat_override"
    assert configured.result_value["policy"]["effective"][
        "checkpoint_enabled"
    ] is True
    assert configured.result_value["policy"]["effective"]["restore_on_boot"] is True

    inspected = await broker.invoke_name(
        "session",
        {"operation": "continuity"},
        replace(context, nested_call_id="nested-kernel-continuity-inspect"),
    )
    assert inspected.ok, inspected.to_dict()
    assert inspected.result_value["policy"]["override"]["configured_by"] == {
        "cell_execution_id": "cell-kernel-continuity",
            "kernel_generation": "1",
        "nested_call_id": "nested-kernel-continuity",
        "outer_tool_call_id": "outer-kernel-continuity",
        "principal_actor_id": "model",
        "run_id": "run-kernel-continuity",
    }
    assert registry.get("session").schema_revision == "variant1.session.v3"
    assert registry.get("kernel") is None
    assert not any(
        registry.get(name) is not None
        for name in (
            "kernel_continuity",
            "kernel_configure_continuity",
            "kernel_checkpoint",
        )
    )


def _effect(name: str) -> str:
    if name in {"read_file", "glob", "grep", "web_search", "browser_read",
                "browser_screenshot"}:
        return "read"
    if name == "apply_patch":
        return "write"
    if name == "ask_user":
        return "interactive"
    return "external_side_effect"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in sorted(SEED_NAMES):
        async def handler(args, _name=name):
            context = current_capability_invocation()
            return {
                "tool": _name,
                "args": args,
                "chat_id": context.chat_id if context else "",
                "mount_revision": context.mount_revision if context else -1,
            }

        params = (
            computer_root_params()
            if name == "computer"
            else dispatcher_params(BROWSER_OBJECT_METHODS)
            if name == "browser"
            else {"path": {"type": "string", "required": True}}
            if name == "read_file"
            else {"value": {"type": "string", "required": False}}
        )
        registry.register(Tool(
            name,
            f"Deterministic {name} test capability.",
            handler,
            category="test",
            params=params,
            effect_class=_effect(name),
            schema_revision=f"schema.{name}.v1",
            handler_revision=f"handler.{name}.v1",
            may_return_secrets=False,
            object_methods=(
                COMPUTER_OBJECT_METHODS
                if name == "computer"
                else BROWSER_OBJECT_METHODS
                if name == "browser"
                else None
            ),
        ))
    registry.register(_object_tool("peers", [
        "list", "get", "send", "inbox", "inspect_message",
        "inspect_request", "reply",
    ], effect="external_side_effect"))
    return registry


@pytest.fixture
def catalog_stack(tmp_path):
    registry = _registry()
    enabled = (
        set(SEED_NAMES)
        | set(COVERED_BROKER_HANDLER_NAMES)
        | set(MOUNTED_PYTHON_API_HANDLER_NAMES)
        | {
        MUTATION_HANDLER, "remote_handle_dispatch",
        }
    )
    database = str(tmp_path / "astb.sqlite3")
    repository = SessionRuntimeRepository(database)
    holder = {}
    runtimes = SessionRuntimeRegistry(
        repository,
        identity_factory=lambda _chat_id, _is_new: holder["service"].identity(
            environment_digest="test-environment"
        ),
    )
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    artifact_graph = SimpleNamespace(
        artifacts=SimpleNamespace(blobs=ArtifactBlobService(artifacts))
    )
    register_artifact_tools(SimpleNamespace(
        registry=registry,
        require_runtime=lambda: artifact_graph,
    ), registry=registry)
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=runtimes,
        enabled_resolver=lambda: enabled,
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
        root=str(tmp_path / "kernels Ω"),
        instance_id="astb-test",
        app_root=str(tmp_path),
        worker_executable=os.environ.get("VARIANT1_TEST_KERNEL_EXE", ""),
        limits=KernelLimits(
            boot_timeout_s=30,
            cell_timeout_s=15,
            interrupt_grace_s=1,
            max_live_kernels=3,
            max_boot_concurrency=1,
            idle_lifetime_s=3600,
            absolute_lifetime_s=3600,
            output=OutputLimits(max_cell_bytes=32_768, max_events=128),
        ),
    )
    yield registry, enabled, runtimes, artifacts, broker, service, manager


def test_catalog_is_content_addressed_sparse_and_immutable(catalog_stack):
    registry, _enabled, _runtimes, artifacts, _broker, service, _manager = catalog_stack
    loaded = service.repository.current()
    assert loaded.release_id == service.current_release_id
    assert loaded.content_sha256 == hashlib.sha256(
        artifacts.read_bytes(loaded.artifact_ref)
    ).hexdigest()
    assert category_ids(loaded.document) == (
        "build", "explore", "operate", "base",
    )
    assert loaded.document["schema_revision"] == "6"
    expected_slot_counts = {
        "build": 8, "explore": 4, "operate": 6, "base": 0,
    }
    expected_vacancies = {
        "build": 2, "explore": 1, "operate": 1, "base": 0,
    }
    for category in loaded.document["categories"]:
        category_id = category["category_id"]
        assert [slot["position"] for slot in category["slots"]] == list(
            range(1, expected_slot_counts[category_id] + 1)
        )
        assert sum(
            slot["status"] == "vacant" for slot in category["slots"]
        ) == expected_vacancies[category_id]
        assert category["mount_mode"] in {"selected", "base"}
    assert service.repository.publish(build_catalog_document(registry)) == loaded.release_id


def test_catalog_covers_the_remaining_service_baseline_during_absorption():
    category_counts = Counter(
        category_id
        for category_id, _position, _bundle, _binding
        in MOUNTED_OBJECT_BINDINGS
    )
    namespaces = {
        binding.namespace
        for _category, _position, _bundle, binding
        in MOUNTED_OBJECT_BINDINGS
    }
    occupied_counts = {
        category_id: sum(bool(slot.bindings) for slot in slots)
        for category_id, slots in SEED_SLOT_BLUEPRINT.items()
    }
    covered_operation_counts = {
        category_id: sum(len(slot.bindings) for slot in slots)
        for category_id, slots in SEED_SLOT_BLUEPRINT.items()
    }

    assert len(MOUNTED_OBJECT_BINDINGS) == 7
    assert len(MOUNTED_OBJECT_HANDLER_NAMES) == 7
    assert len(COVERED_BROKER_HANDLER_NAMES) == 14
    assert len(MOUNTED_PYTHON_API_BINDINGS) == 2
    assert len(ALL_MOUNTED_PYTHON_API_BINDINGS) == 2
    assert len(CONDITIONAL_PYTHON_API_BINDINGS) == 0
    assert len(MOUNTED_PYTHON_API_HANDLER_NAMES) == 2
    assert MOUNTED_PYTHON_API_HANDLER_NAMES == frozenset({
        "toolbelt", "session",
    })
    assert sum(occupied_counts.values()) == 14
    assert occupied_counts == {
        "build": 6,
        "explore": 3,
        "operate": 5,
        "base": 0,
    }
    assert covered_operation_counts == {
        "build": 6,
        "explore": 3,
        "operate": 5,
        "base": 0,
    }
    assert len(namespaces) == 7
    assert category_counts == {
        "build": 1,
        "explore": 2,
        "operate": 4,
    }
    assert EXECUTION_ABSORBED_HANDLER_NAMES.isdisjoint(
        MOUNTED_OBJECT_HANDLER_NAMES
    )
    assert EXECUTION_ABSORBED_HANDLER_NAMES.isdisjoint(
        COVERED_BROKER_HANDLER_NAMES
    )
    assert CODING_ABSORBED_HANDLER_NAMES.isdisjoint(
        MOUNTED_OBJECT_HANDLER_NAMES
    )
    assert CODING_ABSORBED_HANDLER_NAMES.isdisjoint(
        COVERED_BROKER_HANDLER_NAMES
    )
    assert DESKTOP_ACTION_ABSORBED_HANDLER_NAMES.isdisjoint(
        MOUNTED_OBJECT_HANDLER_NAMES
    )
    assert DESKTOP_ACTION_ABSORBED_HANDLER_NAMES.isdisjoint(
        COVERED_BROKER_HANDLER_NAMES
    )
    assert DESKTOP_OBSERVATION_ABSORBED_HANDLER_NAMES.isdisjoint(
        MOUNTED_OBJECT_HANDLER_NAMES
    )
    assert DESKTOP_OBSERVATION_ABSORBED_HANDLER_NAMES.isdisjoint(
        COVERED_BROKER_HANDLER_NAMES
    )
    assert DESKTOP_CATALOG_ABSORBED_HANDLER_NAMES.isdisjoint(
        MOUNTED_OBJECT_HANDLER_NAMES
    )
    assert DESKTOP_CATALOG_ABSORBED_HANDLER_NAMES.isdisjoint(
        COVERED_BROKER_HANDLER_NAMES
    )
    assert MOUNTED_PYTHON_API_HANDLER_NAMES.isdisjoint(
        COVERED_BROKER_HANDLER_NAMES
    )
    run_slot = SEED_SLOT_BLUEPRINT["build"][4]
    assert run_slot.projection == "seeds"
    assert [(row.namespace, row.alias, row.tool_name) for row in run_slot.bindings] == [
        ("tools", "run_command", "run_command")
    ]
    assert all(not slot.bindings for slot in SEED_SLOT_BLUEPRINT["build"][6:])
    assert [
        slot.bindings[0].tool_name for slot in SEED_SLOT_BLUEPRINT["operate"][:4]
    ] == ["ask_user", "children", "skills", "connectors"]
    assert SEED_SLOT_BLUEPRINT["operate"][0].projection == "seeds"
    assert all(
        slot.projection == "object"
        for slot in SEED_SLOT_BLUEPRINT["operate"][1:5]
    )
    assert all(not slot.bindings for slot in SEED_SLOT_BLUEPRINT["operate"][5:])
    assert all(slot.bindings for slot in SEED_SLOT_BLUEPRINT["explore"][:3])
    assert all(
        slot.projection == "object"
        for slot in SEED_SLOT_BLUEPRINT["explore"][1:3]
    )
    assert all(not slot.bindings for slot in SEED_SLOT_BLUEPRINT["explore"][3:])
    artifact_slot = SEED_SLOT_BLUEPRINT["build"][5]
    assert artifact_slot.projection == "object"
    assert [row.tool_name for row in artifact_slot.bindings] == ["artifacts"]
    children_slot = SEED_SLOT_BLUEPRINT["operate"][1]
    assert children_slot.projection == "object"
    assert [row.tool_name for row in children_slot.bindings] == ["children"]
    assert SEED_SLOT_BLUEPRINT["base"] == ()


def test_explore_mount_advertises_one_compact_browser_object(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    chat_id = "chat-grown-browser-seeds"
    runtimes.ensure_runtime(chat_id, is_new=True)
    service.select(chat_id, "explore")

    document, refs = service.namespace_document(chat_id)
    aliases = {row["alias"] for row in document["capabilities"]}
    assert aliases == {"web_search"}
    assert "result.session.pages()" in document["mount_card"]
    assert "navigates the current tab" in document['mount_card']
    assert "result.session.new_page(url) opens another tab" in document['mount_card']
    assert "result.session.history('downloads'" in document["mount_card"]
    assert "This is a mount receipt" in document["mount_card"]
    assert document["mount_card"].splitlines()[1] == (
        "Direct calls: tools.web_search. Top-level objects: browser, computer."
    )
    assert "kind='embedded' explicitly selects" in document['mount_card']
    assert "built-in browser as the default" in document['mount_card']
    assert "explicit user browser/tab request takes precedence" in document['mount_card']
    assert "kind='visible'" not in document['mount_card']
    assert set(document["mounted_objects"]) == {"browser", "computer"}
    assert _api_method_aliases(document, "browser") == {
        "navigate", "read", "screenshot", "click", "fill",
    }
    assert "browser" in {ref.capability_id for ref in refs.values()}
    assert "computer" in {ref.capability_id for ref in refs.values()}
    assert _api_method_aliases(document, "computer") == {
        "list_windows", "focus", "observe", "click", "type_text",
        "press_key", "set_value", "scroll", "drag",
    }
    assert "secondary_action" not in document["mount_card"]
    assert "action: choices 'invoke', 'select', 'toggle'" in document["mount_card"]
    assert all(
        row["projection"] == "object"
        and row["call"].startswith("browser.")
        for row in document["catalog_index"]
        if row.get("namespace") == "browser"
    )
    prompt = service.runtime_prompt(chat_id, "open a browser tab")
    assert "`browser`" in prompt
    assert "browser.navigate(" in prompt
    assert "browser('open'" not in prompt
    desktop_prompt = service.runtime_prompt(chat_id, "inspect the desktop window")
    assert "computer.focus(" in desktop_prompt


def test_operate_mount_combines_interaction_and_children(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    chat_id = "chat-grown-desktop-seeds"
    runtimes.ensure_runtime(chat_id, is_new=True)
    service.select(chat_id, "operate")

    document, _refs = service.namespace_document(chat_id)
    assert {row["alias"] for row in document["capabilities"]} == {"ask_user"}
    assert set(document["mounted_objects"]) == {"children", "peers"}
    assert _api_method_aliases(document, "children") == {
        "spawn", "list", "tree",
    }
    assert all(
        row["projection"] == "seeds" and row["call"].startswith("tools.")
        for row in document["catalog_index"]
        if row["alias"] == "ask_user"
    )
    prompt = service.runtime_prompt(chat_id, "ask the user then delegate work")
    assert "children.spawn(" in prompt
    assert "computer.focus(" not in prompt
    assert "focus_window" not in prompt
    assert "see_ui" not in prompt
    assert "ground_ui" not in prompt
    assert "open_app" not in prompt
    assert "open_path" not in prompt


def test_review_is_not_projected_and_selected_categories_retain_vacancies(
    catalog_stack,
):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    registry.register(_object_tool("review", [
        "start", "get", "list", "check_recipes",
    ]))
    service.reconcile_registry()
    chat_id = "chat-review-explore-vacancies"
    runtimes.ensure_runtime(chat_id, is_new=True)

    service.select(chat_id, "build")
    build, _refs = service.namespace_document(chat_id)
    assert "review" not in build["services"]
    assert "review" not in build["mounted_objects"]
    assert _api_method_aliases(build, "review") == set()
    option = next(
        row for row in build["category_options"]
        if row["category_id"] == "build"
    )
    assert [
        value.rsplit("/", 2)[-2:]
        for value in option["vacant_slot_ids"]
    ] == [["build", "7"], ["build", "8"]]
    assert "review.start/list/get" not in service.runtime_prompt(
        chat_id, "review changes"
    )

    service.select(chat_id, "explore")
    explore, _refs = service.namespace_document(chat_id)
    assert "research" not in explore["services"]
    assert "research" not in explore["mounted_objects"]
    option = next(
        row for row in explore["category_options"]
        if row["category_id"] == "explore"
    )
    assert [
        value.rsplit("/", 2)[-2:]
        for value in option["vacant_slot_ids"]
    ] == [["explore", "4"]]
    prompt = service.runtime_prompt(chat_id, "web sources and citations")
    assert "research.methods()" not in prompt
    assert "research.describe(name)" not in prompt


def test_complete_catalog_discovers_every_baseline_method_at_its_mount(
    catalog_stack,
):
    registry, _enabled, _runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )

    async def no_op(arguments):
        return arguments

    for name in sorted(
        COVERED_BROKER_HANDLER_NAMES | MOUNTED_PYTHON_API_HANDLER_NAMES
    ):
        if registry.get(name) is not None:
            continue
        object_methods = ()
        if name in MOUNTED_PYTHON_API_HANDLER_NAMES | MOUNTED_OBJECT_HANDLER_NAMES:
            object_methods = ({"name": "ping", "params": {}, "effect_class": "read"},)
        registry.register(Tool(
            name,
            f"Catalog coverage fixture for {name}.",
            no_op,
            params={},
            hidden=True,
            visibility="broker_only",
            effect_class="read",
            object_methods=object_methods,
        ))

    service.reconcile_registry(require_complete=True)
    loaded = service.repository.current()
    assert all(
        slot["status"] != "unavailable"
        for category in loaded.document["categories"]
        for slot in category["slots"]
    )
    assert all(
        api["status"] == "available"
        for category in loaded.document["categories"]
        for api in category.get("python_apis") or ()
    )
    flags = {
        "children_enabled": True,
        "mutation_write": True,
    }
    for category_id, position, _bundle, binding in MOUNTED_OBJECT_BINDINGS:
        category = next(
            row for row in loaded.document["categories"]
            if row["category_id"] == category_id
        )
        slot = next(
            row for row in category["slots"] if row["position"] == position
        )
        qualified_alias = (
            binding.alias
            if slot.get("projection") == "seeds"
            and binding.namespace == "tools"
            else str(slot["bundle"])
            if slot.get("projection") == "object"
            else f"{binding.namespace}.{binding.alias}"
        )
        hits = service.top_k(
            loaded,
            qualified_alias,
            width=1,
            condition_flags=flags,
        )
        assert hits and hits[0]["qualified_alias"] == qualified_alias
        assert hits[0]["category_id"] == category_id
        assert hits[0]["position"] == position

    registry.register(Tool(
        "unmounted_duplicate",
        "A regression fixture that bypasses slot ownership.",
        no_op,
        params={},
        effect_class="read",
    ))
    with pytest.raises(CatalogError, match="registered methods exist outside"):
        service.reconcile_registry(require_complete=True)


def test_root_index_includes_slot_objects_and_immutable_base_objects(catalog_stack):
    registry, _enabled, _runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    for name in sorted(MOUNTED_OBJECT_HANDLER_NAMES):
        if registry.get(name) is None:
            registry.register(_object_tool(name, ["ping"]))
    service.reconcile_registry()
    loaded = service.repository.current()
    flags = {
        "children_enabled": True,
        "mutation_write": False,
    }
    for query, expected, category in (
        ("connectors", "connectors", "operate"),
        ("children", "children", "operate"),
        ("session", "session", "base"),
        ("toolbelt", "toolbelt", "base"),
    ):
        hits = service.top_k(
            loaded, query, width=1, condition_flags=flags
        )
        assert hits and hits[0]["qualified_alias"] == expected
        assert hits[0]["category_id"] == category
    for query in ("agent", "subagent", "child agent"):
        hits = service.top_k(
            loaded, query, width=1, condition_flags=flags
        )
        assert hits and hits[0]["qualified_alias"] == "children"
        assert hits[0]["category_id"] == "operate"


def test_real_handler_or_schema_change_publishes_a_new_release(catalog_stack):
    registry, _enabled, _runtimes, _artifacts, _broker, service, _manager = catalog_stack
    before = service.current_release_id
    registry.remove("read_file")

    async def replacement(args):
        return args

    registry.register(Tool(
        "read_file", "Changed handler.", replacement,
        params={"path": {"type": "string", "required": True}},
        effect_class="read",
        schema_revision="schema.read_file.v2",
        handler_revision="handler.read_file.v2",
    ))
    after = service.reconcile_registry()
    assert after != before
    assert service.repository.load(before).release_id == before
    assert service.repository.load(after).release_id == after


def test_static_chat_follows_handler_only_catalog_upgrade(catalog_stack):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    before = service.current_release_id
    runtimes.ensure_runtime("chat-handler-follow", is_new=True)
    selected = service.select("chat-handler-follow", "build")
    assert selected["mount_revision"] == 1

    registry.get("read_file").handler_revision = "handler.read_file.v2"
    after = service.reconcile_registry()
    assert after != before

    document, refs = service.namespace_document("chat-handler-follow")
    record = runtimes.ensure_runtime("chat-handler-follow")
    read_ref = next(
        ref for ref in refs.values() if ref.capability_id == "read_file"
    )

    assert document["catalog_release_id"] == after
    assert record.identity.catalog_release_id == after
    assert record.identity.selected_category_id == "build"
    assert record.identity.mount_revision == 2
    assert read_ref.handler_revision == "handler.read_file.v2"
    assert service.repository.history("chat-handler-follow")[-1]["reason"] == (
        "catalog_handler_follow"
    )


def test_mutation_authority_pins_handler_only_catalog(catalog_stack):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    chat_id = "chat-authority-pin"
    before = service.current_release_id
    runtimes.ensure_runtime(chat_id, is_new=True)
    runtimes.set_mutation_write_enabled(chat_id, True, actor="test")
    registry.get("read_file").handler_revision = "handler.read_file.v2"
    after = service.reconcile_registry()

    document, _refs = service.namespace_document(chat_id)

    assert after != before
    assert document["catalog_release_id"] == before
    assert service.repository.catalog_follow_eligible(chat_id) is False


def test_mutation_artifact_keeps_catalog_pinned_after_toggle_off(catalog_stack):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    chat_id = "chat-artifact-pin"
    before = service.current_release_id
    runtimes.ensure_runtime(chat_id, is_new=True)
    runtimes.set_mutation_write_enabled(chat_id, True, actor="test")
    service.mutation.propose(
        chat_id,
        kind="create",
        slot="explore/4",
        alias="session_probe",
        purpose="Return one deterministic session-local value.",
        schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        source="def run(arguments):\n    return {'ok': True}\n",
        tests=[],
    )
    runtimes.set_mutation_write_enabled(chat_id, False, actor="test")
    registry.get("read_file").handler_revision = "handler.read_file.v2"
    after = service.reconcile_registry()

    document, _refs = service.namespace_document(chat_id)

    assert after != before
    assert document["catalog_release_id"] == before
    assert service.repository.catalog_follow_eligible(chat_id) is False


def test_handler_only_catalog_follow_failure_is_logged(
    catalog_stack, monkeypatch, caplog,
):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    before = service.current_release_id
    runtimes.ensure_runtime("chat-handler-follow-failure", is_new=True)
    registry.get("read_file").handler_revision = "handler.read_file.v2"
    after = service.reconcile_registry()

    def fail_follow(*_args, **_kwargs):
        raise CatalogError("simulated catalog follow failure")

    monkeypatch.setattr(
        service.repository, "follow_handler_only_release", fail_follow
    )
    caplog.set_level(logging.WARNING, logger="astb.service")

    document, _refs = service.namespace_document("chat-handler-follow-failure")

    assert document["catalog_release_id"] == before
    assert "handler-only catalog follow failed" in caplog.text
    assert "chat_id=chat-handler-follow-failure" in caplog.text
    assert f"from_release={before}" in caplog.text
    assert f"to_release={after}" in caplog.text
    assert "simulated catalog follow failure" in caplog.text


def test_static_chat_does_not_follow_schema_changing_catalog(catalog_stack):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    before = service.current_release_id
    runtimes.ensure_runtime("chat-schema-pin", is_new=True)
    registry.get("read_file").schema_revision = "schema.read_file.v2"
    after = service.reconcile_registry()
    assert after != before

    document, _refs = service.namespace_document("chat-schema-pin")
    record = runtimes.ensure_runtime("chat-schema-pin")

    assert document["catalog_release_id"] == before
    assert record.identity.catalog_release_id == before
    assert record.identity.mount_revision == 0


def test_top_k_and_provider_contract_are_model_independent(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    runtimes.ensure_runtime("chat-rank", is_new=True)
    loaded = service.repository.current()
    first = service.top_k(loaded, "run_command")
    second = service.top_k(loaded, "run_command")
    assert first == second
    assert 1 <= len(first) <= 5
    assert first[0]["score"] >= 100
    assert set(IPYTHON_PROVIDER_SPEC["params"]) == {"category", "code"}
    assert "combined with execution in one call" in (
        IPYTHON_PROVIDER_SPEC["description"]
    )
    assert IPYTHON_HOST_CAPABILITY_CONTRACT in IPYTHON_PROVIDER_SPEC["description"]
    preferred_process = (
        "Use Python to compose workflows. For launching and managing applications, "
        "prefer the provided process capability."
    )
    prompt = service.runtime_prompt("chat-rank", "launch OBS")
    assert prompt.count(preferred_process) == 1
    assert IPYTHON_PROVIDER_SPEC["description"].count(preferred_process) == 1
    assert prompt.count(BROWSER_SELECTION_GUIDANCE) == 1
    assert "prefer browser and its built-in default" in IPYTHON_PROVIDER_SPEC["description"]
    assert "explicit user browser choice" in IPYTHON_PROVIDER_SPEC["description"]
    for value in (prompt, IPYTHON_PROVIDER_SPEC["description"]):
        assert "subprocess" not in value and "Popen" not in value
    assert "Later mount receipts supersede these lists" in prompt
    assert IPYTHON_PROVIDER_SPEC == IPYTHON_PROVIDER_SPEC.copy()


def test_runtime_prompt_leads_with_outcome_and_one_confident_category(
    catalog_stack,
):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    if registry.get("connectors") is None:
        registry.register(_object_tool("connectors", ["search"]))
        service.reconcile_registry()
    runtimes.ensure_runtime("chat-prompt-focus", is_new=True)

    prompt = service.runtime_prompt(
        "chat-prompt-focus", "find an MCP connector capability"
    )
    assert prompt.startswith(
        "Complete the user’s task fully. Treat successful tool results and "
        "assertions as evidence. Verify any requested condition that is not "
        "already established, then stop calling tools and give the final answer."
    )
    assert (
        "Keep useful read/search results in named variables and reuse them "
        "while inputs remain unchanged."
    ) in prompt
    assert "Likely capability category: `operate`" in prompt
    assert "Likely useful operations" not in prompt
    assert "tools.browser_fill" not in prompt
    assert "tools.computer" not in prompt

    service.select("chat-prompt-focus", "build")
    build_prompt = service.runtime_prompt(
        "chat-prompt-focus", "fix code with run_command and run tests"
    )
    assert "verify the requested behavior" in build_prompt
    assert "project's relevant checks" in build_prompt
    assert "Potentially useful operations in the current category" in build_prompt


def test_runtime_prompt_projection_keeps_live_mount_and_query_out_of_stable_tier(
    catalog_stack,
):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    chat_id = "chat-prompt-projection"
    runtimes.ensure_runtime(chat_id, is_new=True)

    before = service.runtime_prompt_projection(
        chat_id,
        "help me think this through",
        allow_auto_mount=False,
    )
    service.select(chat_id, "build")
    after = service.runtime_prompt_projection(
        chat_id,
        "start a background service and retain its durable handle",
        allow_auto_mount=False,
    )

    assert before.stable == after.stable
    assert before.current != after.current
    assert "Current capability mount:" not in before.stable
    assert "Current capability mount:" in after.current
    assert "Two execution cells" not in before.current
    assert "Two execution cells" in after.current
    assert service.runtime_prompt(
        chat_id,
        "start a background service and retain its durable handle",
        allow_auto_mount=False,
    ) == after.combined()


@pytest.mark.parametrize("query", [
    "Start a background service and retain its handle",
    "Restart the running application",
    "Start the local service and check its output",
    "Start the Python HTTP server and inspect its output",
    "Launch OBS in the background and retain its handle",
    "Restart Notepad",
    "Keep the application running while I inspect its window",
])
def test_application_tasks_disclose_process_workflow_from_explore(catalog_stack, query):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    runtimes.ensure_runtime("chat-process-disclosure", is_new=True)
    service.select("chat-process-disclosure", "explore")
    before, _ = service.namespace_document("chat-process-disclosure")

    prompt = service.runtime_prompt("chat-process-disclosure", query)
    after, _ = service.namespace_document("chat-process-disclosure")

    assert "ipython(category='build'" in prompt
    assert "argv=[exe_path], cwd=exe_dir, mode='process'" in prompt
    assert "ipython(category='explore'" in prompt
    assert "app_proc" in prompt and "remains usable across the category switch" in prompt
    assert after["selected_category_id"] == before["selected_category_id"] == "explore"
    assert after["mount_revision"] == before["mount_revision"]
    assert "run_command" not in {row["alias"] for row in after["capabilities"]}
    assert "Popen" not in prompt and "subprocess" not in prompt


def test_computer_mount_discloses_resulting_view_reuse_only_when_present(catalog_stack):
    _registry, enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    runtimes.ensure_runtime('chat-action-view-disclosure', is_new=True)
    service.select('chat-action-view-disclosure', 'explore')
    document, _ = service.namespace_document('chat-action-view-disclosure')
    card = document['mount_card']
    assert 'Computer actions return the resulting DesktopView; assign and reuse it.' in card
    enabled.remove('computer')
    disabled, _ = service.namespace_document('chat-action-view-disclosure')
    assert 'Computer actions return the resulting DesktopView' not in disabled['mount_card']


def test_process_workflow_is_not_disclosed_for_unrelated_or_disabled_work(catalog_stack):
    _registry, enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    runtimes.ensure_runtime("chat-process-unrelated", is_new=True)
    assert "Two execution cells" not in service.runtime_prompt(
        "chat-process-unrelated", "help me think this through"
    )
    for query in ('Open this file in a suitable viewer', 'Use Notepad on the desktop',
                  'Process these invoices into a summary', 'Inspect the application window'):
        assert 'Two execution cells' not in service.runtime_prompt('chat-process-unrelated', query)
    enabled.remove("run_command")
    assert "Two execution cells" not in service.runtime_prompt(
        "chat-process-unrelated", "launch the installed application"
    )


@pytest.mark.parametrize("query", [
    "Start one background asyncio task that increments this live dictionary every 0.1 seconds",
    "Restart the background asyncio task and keep its dictionary",
    "Keep running this coroutine across later Python cells",
    "Process the incoming records in the background",
    "Terminate the background coroutine after its final verification",
])
def test_python_background_work_does_not_disclose_application_launch_workflow(catalog_stack, query):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    runtimes.ensure_runtime("chat-python-background", is_new=True)
    service.select("chat-python-background", "build")
    before, _ = service.namespace_document("chat-python-background")

    prompt = service.runtime_prompt("chat-python-background", query)

    after, _ = service.namespace_document("chat-python-background")
    assert "For this application/process workflow" not in prompt
    assert "Two execution cells" not in prompt
    # The user's conditional general preference and actual capabilities remain.
    assert "For launching and managing applications, prefer the provided process capability" in prompt
    assert after["selected_category_id"] == before["selected_category_id"] == "build"
    assert after["mount_revision"] == before["mount_revision"]
    assert "run_command" in {row["alias"] for row in after["capabilities"]}


def test_confident_first_prompt_auto_mounts_with_explicit_provenance(
    catalog_stack,
):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    runtimes.ensure_runtime("chat-auto-mount", is_new=True)

    prompt = service.runtime_prompt(
        "chat-auto-mount", "use run_command to inspect the project"
    )
    document, _refs = service.namespace_document("chat-auto-mount")

    assert document["selected_category_id"] == "build"
    assert document["mount_revision"] == 1
    assert "Current capability mount:" in prompt
    assert "Likely capability category:" not in prompt
    assert document["mount_history"][-1]["reason"] == "host_ranked_auto_select"


def test_first_prompt_auto_mount_cas_follows_handler_revision(catalog_stack):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    chat_id = "chat-handler-follow-auto-mount"
    runtimes.ensure_runtime(chat_id, is_new=True)
    registry.get("read_file").handler_revision = "handler.read_file.v2"
    service.reconcile_registry()

    service.runtime_prompt(chat_id, "use run_command to inspect the project")
    document, _refs = service.namespace_document(chat_id)

    assert document["selected_category_id"] == "build"
    assert document["mount_revision"] == 2
    assert [row["reason"] for row in document["mount_history"][-2:]] == [
        "catalog_handler_follow",
        "host_ranked_auto_select",
    ]


def test_weak_first_prompt_does_not_auto_mount(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    runtimes.ensure_runtime("chat-no-auto-mount", is_new=True)

    service.runtime_prompt("chat-no-auto-mount", "help me think this through")
    document, _refs = service.namespace_document("chat-no-auto-mount")

    assert document["selected_category_id"] is None
    assert document["mount_revision"] == 0
    assert document["mount_history"] == []


def test_query_only_runtime_prompt_cannot_consume_first_auto_mount(
    catalog_stack,
):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    chat_id = "chat-query-only"
    runtimes.ensure_runtime(chat_id, is_new=True)

    service.runtime_prompt(
        chat_id,
        "use run_command to inspect the project",
        allow_auto_mount=False,
    )
    document, _refs = service.namespace_document(chat_id)

    assert document["selected_category_id"] is None
    assert document["mount_revision"] == 0
    assert document["mount_history"] == []


def test_mount_persists_alias_projection_and_cas_allows_one_winner(
    catalog_stack, monkeypatch,
):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    record = runtimes.ensure_runtime("chat-mount", is_new=True)
    chosen = service.select("chat-mount", "Build")
    document, _ = service.namespace_document("chat-mount")
    assert chosen["category_id"] == "build"
    assert document["mount_revision"] == 1
    assert {row["alias"] for row in document["capabilities"]} == {
        "read_file", "glob", "grep", "apply_patch", "run_command",
    }
    assert document["services"] == {}
    assert set(document["mounted_objects"]) == {"artifacts"}
    assert set(document["python_apis"]) == {"toolbelt", "session"}
    assert _mounted_object_method_aliases(document, "artifacts") == {
        "list", "read_text", "read_bytes", "save", "get", "create",
    }
    assert "terminal" not in document["services"]
    assert "processes" not in document["services"]
    assert "web" not in document["services"]
    assert "read_file" in document["mount_card"]
    assert "Python calls below are available in the `code` field of `ipython`" in document[
        "mount_card"
    ]
    assert "tools.read_file(path" in document["mount_card"]
    assert "artifacts.list(" in document["mount_card"]
    assert "artifacts.methods()" not in document["mount_card"]
    prompt = service.runtime_prompt("chat-mount", "edit this file")
    assert IPYTHON_HOST_CAPABILITY_CONTRACT in prompt
    assert (
        "Only the listed VARIANT-1 host proxies are mounted; standard Python "
        "remains available independently."
    ) in prompt
    assert "Only the listed direct seeds, slot-owned objects" not in prompt
    assert "one callable seed bundle" not in prompt
    assert "Turn-start globals:" in prompt
    assert "Currently preloaded globals:" not in prompt
    assert "await object.method.async_" not in prompt
    assert "asyncio.gather" not in prompt
    assert "Use each call exactly as printed." in prompt
    assert "object-prefixed calls use top-level globals." in prompt
    assert "available in the persistent session through `ipython(code=...)`" in prompt
    assert "callable signatures are listed in the current mount card" in prompt
    assert "use `object.methods()` for names, then" not in prompt
    assert "use `connectors.search(...)`" not in prompt
    assert "`toolbelt` provides category search" in prompt
    async_prompt = service.runtime_prompt(
        "chat-mount", "read independent files in parallel with async fanout",
    )
    assert "await object.method.async_" in async_prompt
    assert "asyncio.gather" in async_prompt
    assert "op=debug" not in prompt
    immutable_prompt = service.runtime_prompt(
        "chat-mount", "inspect an immutable schema lease",
    )
    assert "Session-local mutation authoring is off" not in immutable_prompt
    mutation_prompt = service.runtime_prompt("chat-mount", "mutate this seed")
    assert "Session-local mutation authoring is off" in mutation_prompt
    debug_prompt = service.runtime_prompt("chat-mount", "debug a breakpoint")
    assert "op=debug" not in debug_prompt
    assert "tools.run_command(" in prompt
    assert {"terminal", "processes", "git"}.isdisjoint(document["python_apis"])
    assert "`terminal`" not in prompt
    assert "`processes`" not in prompt
    assert "`git`" not in prompt
    assert "toolbelt.mount(category=...)" in prompt
    assert "preserves exact line endings" in prompt
    assert _api_method_aliases(document, "toolbelt") == {
        "search", "mount", "mutation_status", "rollback", "reset", "reset_all",
    }
    assert _api_method_aliases(document, "session") == {
        "status", "continuity", "configure_continuity", "checkpoint",
        "restart", "report_outcome",
    }
    restarted = SessionRuntimeRegistry(runtimes.repository)
    restored = restarted.ensure_runtime("chat-mount")
    assert restored.identity.selected_category_id == "build"
    assert restored.identity.mount_revision == 1

    def select_once():
        return service.repository.select_mount(
            "chat-mount",
            catalog_release_id=record.identity.catalog_release_id,
            category_id="explore",
            expected_mount_revision=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(select_once) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except MountConflict:
            outcomes.append("conflict")
    assert sum(item == "conflict" for item in outcomes) == 1


@pytest.mark.asyncio
async def test_kernel_mount_sync_retains_acquired_proxies_and_cross_chat_routing(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, _broker, service, manager = catalog_stack
    for chat_id in ("chat-a", "chat-b"):
        runtimes.ensure_runtime(chat_id, is_new=True)
        service.select(chat_id, "build")
    first = await manager.execute(
        chat_id="chat-a",
        code=(
            "old = tools.read_file\n"
            "print(tools.aliases())\n"
            "print('HAS_FILES=' + str('files' in globals()))"
        ),
        run_id="run-a1", outer_tool_call_id="outer-a1",
    )
    same_selection = service.select("chat-a", "build")
    same_mount = await manager.execute(
        chat_id="chat-a",
        code="print(old(path='same-category')['chat_id'])",
        run_id="run-a-same", outer_tool_call_id="outer-a-same",
    )
    routed = await manager.execute(
        chat_id="chat-b", code="print(tools.read_file(path='x')['chat_id'])",
        run_id="run-b1", outer_tool_call_id="outer-b1",
    )
    service.select("chat-a", "explore")
    retained = await manager.execute(
        chat_id="chat-a",
        code=(
            "print('RETAINED=' + old(path='x')['chat_id'])\n"
            "print('ARTIFACTS_GLOBAL=' + str('artifacts' in globals()))"
        ),
        run_id="run-a2", outer_tool_call_id="outer-a2",
    )
    before_generation = retained.generation
    await manager.close_chat("chat-a", reason="restart-test")
    restored = await manager.execute(
        chat_id="chat-a", code="print(tools.aliases())\nprint(toolbelt.inspect())",
        run_id="run-a3", outer_tool_call_id="outer-a3",
    )
    try:
        assert first.ok, first.to_dict()
        assert same_selection["unchanged"] is True
        assert same_selection["mount_revision"] == 1
        assert same_mount.ok and "chat-a" in same_mount.output.text()
        assert len(service.repository.history("chat-a")) == 2
        assert [row["category_id"] for row in service.repository.history("chat-a")] == [
            "build", "explore",
        ]
        assert "HAS_FILES=False" in first.output.text()
        assert routed.ok and "chat-b" in routed.output.text()
        assert retained.ok, retained.to_dict()
        assert "RETAINED=chat-a" in retained.output.text()
        assert "ARTIFACTS_GLOBAL=False" in retained.output.text()
        assert restored.ok, restored.to_dict()
        assert restored.generation > before_generation
        assert "search" in restored.output.text()
        assert "'selected_category_id': 'explore'" in restored.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mounted_object_is_protected_retained_then_revoked_by_reset(
    catalog_stack,
):
    _registry, _enabled, runtimes, _artifacts, _broker, service, manager = (
        catalog_stack
    )
    chat_id = "chat-mounted-object-stale"
    runtimes.ensure_runtime(chat_id, is_new=True)
    service.select(chat_id, "explore")
    mounted = await manager.execute(
        chat_id=chat_id,
        code=(
            "saved_browser = browser\n"
            "print(browser.methods())\n"
            "print(browser.documentation('read')['signature'])\n"
            "print(session.status()['chat_id'])"
        ),
        run_id="run-api-1", outer_tool_call_id="outer-api-1",
    )
    service.select(chat_id, "build")
    retained = await manager.execute(
        chat_id=chat_id,
        code=(
            "print('BROWSER_GLOBAL=' + str('browser' in globals()))\n"
            "print('BROWSER_RETAINED=' + str(saved_browser.read() is not None))"
        ),
        run_id="run-api-2", outer_tool_call_id="outer-api-2",
    )
    service.reset(chat_id)
    revoked = await manager.execute(
        chat_id=chat_id,
        code=(
            "try:\n"
            "    saved_browser.read()\n"
            "except Variant1CapabilityError as exc:\n"
            "    print('API_REVOKED=' + exc.code)"
        ),
        run_id="run-api-3", outer_tool_call_id="outer-api-3",
    )
    try:
        assert mounted.ok, mounted.to_dict()
        assert "read" in mounted.output.text()
        assert "read(" in mounted.output.text()
        assert chat_id in mounted.output.text()
        assert retained.ok, retained.to_dict()
        assert "BROWSER_GLOBAL=False" in retained.output.text()
        assert "BROWSER_RETAINED=True" in retained.output.text()
        assert revoked.ok, revoked.to_dict()
        assert "API_REVOKED=stale_category_mount" in revoked.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_kernel_restores_protected_globals_and_imports_use_mount_shims(
    catalog_stack,
):
    _registry, _enabled, runtimes, _artifacts, _broker, service, manager = catalog_stack
    runtimes.ensure_runtime("chat-protected", is_new=True)
    service.select("chat-protected", "build")
    imported = await manager.execute(
        chat_id="chat-protected",
        code=(
            "import tools\n"
            "import toolbelt\n"
            "print(tools.aliases())\n"
            "print(toolbelt.inspect()['selected_category_id'])\n"
            "print('TOOLBELT_CALLABLE=' + str(callable(toolbelt)))"
        ),
        run_id="run-protected-1", outer_tool_call_id="outer-protected-1",
    )
    shadowed = await manager.execute(
        chat_id="chat-protected",
        code="tools = 'shadowed'\ntoolbelt = 'shadowed'\nprint('shadowed')",
        run_id="run-protected-2", outer_tool_call_id="outer-protected-2",
    )
    restored = await manager.execute(
        chat_id="chat-protected",
        code="print(tools.aliases())\nprint(toolbelt.inspect()['selected_category_id'])",
        run_id="run-protected-3", outer_tool_call_id="outer-protected-3",
    )
    try:
        assert imported.ok, imported.to_dict()
        assert "read" in imported.output.text()
        assert "build" in imported.output.text()
        assert "TOOLBELT_CALLABLE=False" in imported.output.text()
        assert shadowed.ok, shadowed.to_dict()
        assert restored.ok, restored.to_dict()
        assert "read" in restored.output.text()
        assert "build" in restored.output.text()
        lease = manager._leases["chat-protected"]
        assert lease._namespace_sync_count == 0
        assert lease._namespace_sync_skipped >= 3
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_ordinary_python_composes_repeated_seed_calls(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, broker, service, manager = catalog_stack
    runtimes.ensure_runtime("chat-batch", is_new=True)
    service.select("chat-batch", "build")
    result = await manager.execute(
        chat_id="chat-batch",
        code=(
            "values = [tools.read_file(path=path) for path in "
            "['first.txt', 'second.txt']]\n"
            "print(values)"
        ),
        run_id="run-batch",
        outer_tool_call_id="outer-batch",
    )
    try:
        assert result.ok, result.to_dict()
        output = result.output.text()
        assert output.index("first.txt") < output.index("second.txt")
        receipts = broker.receipts(limit=10)
        assert [row.result_value["args"]["path"] for row in receipts[-2:]] == [
            "first.txt", "second.txt",
        ]
        assert all(not row.attribution["batch_id"] for row in receipts[-2:])
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_session_and_artifacts_objects_are_chat_scoped(catalog_stack):
    _registry, _enabled, runtimes, artifacts, _broker, service, manager = catalog_stack
    for chat_id in ("scope-a", "scope-b"):
        runtimes.ensure_runtime(chat_id, is_new=True)
        service.select(chat_id, "build")
    secret = artifacts.put_text("only-a", kind="test", scope="scope-a")
    own = await manager.execute(
        chat_id="scope-a",
        code=f"print(artifacts.read_text(ref={secret.ref!r})['text'])",
        run_id="scope-run-a", outer_tool_call_id="scope-outer-a",
    )
    denied = await manager.execute(
        chat_id="scope-b",
        code=(
            "try:\n"
            f"    artifacts.read_text(ref={secret.ref!r})\n"
            "except Variant1CapabilityError as exc:\n"
            "    print('DENIED=' + exc.code)"
        ),
        run_id="scope-run-b", outer_tool_call_id="scope-outer-b",
    )
    session_state = await manager.execute(
        chat_id="scope-a",
        code="print(session.status()['chat_id'])",
        run_id="scope-run-session", outer_tool_call_id="scope-outer-session",
    )
    try:
        assert own.ok and "only-a" in own.output.text()
        assert denied.ok and "DENIED=" in denied.output.text()
        assert session_state.ok and "scope-a" in session_state.output.text()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_binary_artifact_proxy_reads_and_exports_complete_payload(catalog_stack, tmp_path):
    _registry, _enabled, runtimes, artifacts, _broker, service, manager = catalog_stack
    for chat_id in ("binary-owner", "binary-other"):
        runtimes.ensure_runtime(chat_id, is_new=True)
        service.select(chat_id, "build")
    payload = bytes(range(256)) * 8192
    ref = artifacts.put_bytes(payload, kind="download", scope="binary-owner")
    destination = tmp_path / "export Ω.bin"
    try:
        result = await manager.execute(
            chat_id="binary-owner", run_id="binary-read", outer_tool_call_id="binary-read",
            code=("import hashlib\n"
                  f"data = artifacts.read_bytes(ref={ref.ref!r})\n"
                  f"assert isinstance(data, bytes) and hashlib.sha256(data).hexdigest() == {ref.sha256!r}\n"
                  f"artifacts.save(ref={ref.ref!r}, path={str(destination)!r})\n"
                  "print(len(data))"),
        )
        assert result.ok, result.to_dict()
        assert str(len(payload)) in result.output.text()
        assert destination.read_bytes() == payload
        denied = await manager.execute(
            chat_id="binary-other", run_id="binary-denied", outer_tool_call_id="binary-denied",
            code=f"artifacts.read_bytes(ref={ref.ref!r})",
        )
        assert not denied.ok
        assert "scope" in denied.output.text().lower() or "grant" in denied.output.text().lower()
    finally:
        await manager.shutdown()


def test_corrupt_source_falls_back_to_lkg_and_recovers_chat(catalog_stack):
    registry, _enabled, runtimes, artifacts, _broker, service, _manager = catalog_stack
    lkg = service.current_release_id
    registry.remove("read_file")

    async def changed(args):
        return args

    registry.register(Tool(
        "read_file", "Changed source", changed,
        params={"path": {"type": "string", "required": True}},
        effect_class="read", schema_revision="schema.changed",
        handler_revision="handler.changed",
    ))
    corrupt = service.reconcile_registry()
    record = runtimes.ensure_runtime("chat-corrupt", is_new=True)
    assert record.identity.catalog_release_id == corrupt
    loaded = service.repository.load(corrupt)
    with open(artifacts._path(loaded.content_sha256), "wb") as handle:
        handle.write(b"not canonical JSON")

    document, _ = service.namespace_document("chat-corrupt")
    recovered = runtimes.ensure_runtime("chat-corrupt")
    assert document["catalog_release_id"] == lkg
    assert recovered.identity.catalog_release_id == lkg
    assert recovered.identity.selected_category_id == ""
    assert recovered.identity.mount_revision == 1


def test_delete_removes_mount_history_and_chat_authority_disables_mutation(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    record = runtimes.ensure_runtime("chat-delete", is_new=True)
    assert record.identity.action_surface == ACTION_SURFACE
    service.select("chat-delete", "build")
    document, _ = service.namespace_document("chat-delete")
    assert document["mutation"]["availability"] == "disabled_by_chat"
    assert document["mutation"]["authority"]["write_enabled"] is False
    assert _api_method_aliases(document, "toolbelt") == {
        "search", "mount", "mutation_status", "rollback", "reset", "reset_all",
    }
    assert _api_method_aliases(document, "session") == {
        "status", "continuity", "configure_continuity", "checkpoint",
        "restart", "report_outcome",
    }
    assert service.repository.history("chat-delete")
    service.repository.delete_chat_state("chat-delete")
    assert service.repository.history("chat-delete") == []


def test_ordinary_namespace_exposes_only_the_selected_category(catalog_stack):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack
    runtimes.ensure_runtime("chat-surface", is_new=True)
    service.select("chat-surface", "build")
    document, _refs = service.namespace_document("chat-surface")
    services = document["services"]
    seed_names = {
        str(binding.get("tool_name") or "")
        for category in service.repository.current().document["categories"]
        for slot in category["slots"]
        for binding in (slot.get("bindings") or ())
    }
    assert "foundry" not in services
    assert "retrieve_memory" not in seed_names
    assert "skill" not in seed_names
    assert "delegate_task" not in seed_names
    assert "memory" not in services
    assert "context" not in services
    assert _api_method_aliases(document, "context") == set()
    assert "skills" not in services
    assert "package_status" not in {
        operation["alias"]
        for rows in services.values()
        for row in rows
        for operation in row.get("operations") or [row]
    }
    prompt = service.runtime_prompt("chat-surface", "edit a project file")
    assert "tools.read_file(" in prompt
    assert "context." not in prompt
    assert "skills('search'" not in prompt
    assert "object.describe('method')" in prompt
    assert "foundry" not in prompt.lower()

    service.select("chat-surface", "operate")
    operated, _refs = service.namespace_document("chat-surface")
    assert "memory" not in operated["services"]
    assert operated["selected_category_id"] == "operate"


def test_operate_objects_are_atomic_slots_with_immutable_base_controls(
    catalog_stack,
):
    registry, _enabled, runtimes, _artifacts, _broker, service, _manager = catalog_stack

    if registry.get("skills") is None:
        registry.register(_object_tool("skills", [
            "search", "inspect",
        ]))
        if registry.get("connectors") is None:
            registry.register(_object_tool("connectors", [
                "search",
            ]))
    service.reconcile_registry()
    runtimes.ensure_runtime("chat-operate-apis", is_new=True)
    service.select("chat-operate-apis", "operate")
    document, refs = service.namespace_document("chat-operate-apis")

    assert document["services"] == {}
    assert set(document["python_apis"]) == {"toolbelt", "session"}
    assert set(document["mounted_objects"]) == {
        "children", "skills", "connectors", "peers",
    }
    assert {row["alias"] for row in document["capabilities"]} == {"ask_user"}
    assert _api_method_aliases(document, "skills") == {"search", "inspect"}
    assert _api_method_aliases(document, "connectors") == {
        "search",
    }
    assert [bool(slot.bindings) for slot in SEED_SLOT_BLUEPRINT["operate"]] == [
        True, True, True, True, True, False,
    ]
    api_capability_ids = {
        method["capability_id"]
        for api in document["mounted_objects"].values()
        for method in api["methods"]
    }
    assert api_capability_ids <= {ref.capability_id for ref in refs.values()}
    prompt = service.runtime_prompt("chat-operate-apis", "use a plugin")
    assert "connectors.search(" in prompt
    assert "connectors.methods()" not in prompt
    assert "package installation" not in prompt
    assert "skills.propose(" not in prompt
    connector_prompt = service.runtime_prompt(
        "chat-operate-apis", "use an MCP connector"
    )
    assert "`match = found.top_match`" in connector_prompt
    assert "`conclude=True` only on the final authoritative" in connector_prompt
    assert "`match.schema()`" in connector_prompt
    assert "only if that lease later reports stale" in connector_prompt
    assert "Inspect a secondary handle with `handle.schema()`" in connector_prompt
    assert "already includes its exact input schema" in connector_prompt


def test_live_mcp_manifests_participate_in_astb_disclosure_ranking(
    catalog_stack,
):
    _registry, _enabled, runtimes, _artifacts, _broker, service, _manager = (
        catalog_stack
    )
    mcp = SimpleNamespace(catalog=lambda: [{
        "kind": "tool",
        "name": "environment_exec",
        "server_id": "benchmark-environment",
        "descriptor": {
            "name": "environment_exec",
            "description": (
                "Run shell commands and inspect files inside the authoritative "
                "isolated Docker task environment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to execute in the container.",
                    },
                },
            },
        },
    }])
    service.host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(
            extensions=SimpleNamespace(mcp=mcp)
        )
    )

    ranked = service.top_k(
        service.repository.current(),
        "inspect files and execute commands in the isolated Docker environment",
    )

    assert ranked[0]["category_id"] == "operate"
    assert ranked[0]["alias"] == "environment_exec"
    assert ranked[0]["live_connector"] is True
    assert ranked[0]["call"].startswith("connectors.search(")

    runtimes.ensure_runtime("chat-live-connector", is_new=True)
    prompt = service.runtime_prompt(
        "chat-live-connector",
        "inspect files and execute commands in the isolated Docker environment",
    )
    document, _refs = service.namespace_document("chat-live-connector")
    assert document["selected_category_id"] == "operate"
    assert document["mount_history"][-1]["reason"] == "host_ranked_auto_select"
    assert "Current capability mount:" in prompt
    assert "connectors.search(query='environment_exec').top_match" in prompt


def test_skills_search_and_inspect_use_the_unified_extension_catalog():
    from session_catalog.service import (
        _inspect_available_skill,
        _search_available_skills,
    )

    class _Store:
        def search(self, query, _limit, *, chat_id=""):
            del chat_id
            if "timer" in str(query).lower():
                return [{"name": "Pomodoro", "description": "Set a timer",
                         "package_id": "focus-coach"}]
            if "tidy" in str(query).lower():
                return [{"name": "tidy-folder", "description": "Tidy a folder",
                         "package_id": "local.skill.tidy-folder"}]
            return []

        def inspect(self, name, *, chat_id=""):
            del chat_id
            if str(name) == "Pomodoro":
                return {
                    "name": "Pomodoro", "instructions": "Set a 25m timer",
                    "package_id": "focus-coach", "body_sha256": "def",
                    "body_chars": 15,
                }
            if str(name) == "tidy-folder":
                return {
                    "name": "tidy-folder",
                    "instructions": "Tidy the folder",
                    "body_sha256": "abc",
                    "body_chars": 15,
                    "package_id": "local.skill.tidy-folder",
                }
            return None

    extensions = SimpleNamespace(skills=_Store())
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(extensions=extensions)
    )
    mixed = _search_available_skills(host, "timer", 8)
    assert mixed[0]["name"] == "Pomodoro"
    assert mixed[0]["package_id"] == "focus-coach"
    store_hit = _search_available_skills(host, "tidy", 8)
    assert store_hit[0]["name"] == "tidy-folder"
    app_body = _inspect_available_skill(host, "Pomodoro")
    assert app_body["package_id"] == "focus-coach"
    assert app_body["instructions"] == "Set a 25m timer"
    store_body = _inspect_available_skill(host, "tidy-folder")
    assert store_body["package_id"] == "local.skill.tidy-folder"


def test_skills_search_preserves_catalog_ranking():
    from session_catalog.service import _search_available_skills

    class _Store:
        def search(self, _query, _limit, *, chat_id=""):
            del chat_id
            return [
                {"name": "z-target", "description": "Exact timer helper"},
                {"name": "a-unrelated", "description": "Unrelated utility"},
            ]

    extensions = SimpleNamespace(skills=_Store())
    host = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(extensions=extensions),
    )

    assert [
        row["name"] for row in _search_available_skills(host, "timer", 2)
    ] == ["z-target", "a-unrelated"]
