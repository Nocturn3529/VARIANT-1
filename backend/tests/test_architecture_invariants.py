"""Static tripwires for the shared mounted-core backend invariants."""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _production_sources():
    result = []
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND)
        if any(
            part.startswith(".")
            or part in {"tests", "evals", "__pycache__", "build", "dist", "data"}
            for part in relative.parts
        ):
            continue
        result.append((path, path.read_text(encoding="utf-8-sig")))
    return tuple(result)


@lru_cache(maxsize=1)
def _production_trees():
    return tuple(
        (path, ast.parse(source, filename=str(path)))
        for path, source in _production_sources()
    )


def test_production_has_no_unowned_create_task_expression():
    offenders = []
    for path, tree in _production_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            function = node.value.func
            if isinstance(function, ast.Attribute) and function.attr == "create_task":
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert offenders == [], (
        "background tasks must be returned, awaited, stored by a domain owner, "
        f"or registered with background_tasks: {offenders}"
    )


def test_request_fingerprint_implementations_stay_in_core():
    allowed = {
        Path("core_invariants.py"),
        # Public broker vocabulary delegates directly to the core primitive.
        Path("capability_broker.py"),
    }
    offenders = []
    for path, tree in _production_trees():
        relative = path.relative_to(BACKEND)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "request_fingerprint" in node.name
                and relative not in allowed
            ):
                offenders.append(f"{relative}:{node.lineno}:{node.name}")
    assert offenders == [], (
        "durable callers must use core_invariants.request_fingerprint directly: "
        f"{offenders}"
    )


def test_canonical_json_helpers_do_not_reimplement_serialization():
    allowed = {
        "core_invariants.py",
        # Streaming size telemetry deliberately does not persist or identify a
        # request; it avoids materializing a second copy of a large manifest.
        "model_runtime/request_manifest_projection.py",
    }
    offenders = []
    for path, tree in _production_trees():
        relative = path.relative_to(BACKEND).as_posix()
        if relative in allowed:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "canonical" not in node.name and "stable" not in node.name:
                continue
            for nested in ast.walk(node):
                if not isinstance(nested, ast.Call) or not isinstance(
                    nested.func, ast.Attribute
                ):
                    continue
                owner = nested.func.value
                if (
                    isinstance(owner, ast.Name)
                    and owner.id == "json"
                    and nested.func.attr in {"dumps", "JSONEncoder"}
                ):
                    offenders.append(
                        f"{relative}:{node.lineno}:{node.name}"
                    )
                    break
    assert offenders == [], (
        "canonical serialization must delegate to core_invariants: "
        f"{offenders}"
    )


def test_websocket_and_bridge_transports_do_not_import_repositories():
    offenders = []
    for path, tree in _production_trees():
        relative = path.relative_to(BACKEND)
        is_transport = (
            path.name.startswith("ws_")
            or path.name.endswith("_ws.py")
            or relative.as_posix() in {
                "kernel_runtime/bridge.py",
                "kernel_runtime/bridge_protocol.py",
                "kernel_runtime/repl_protocol.py",
                "kernel_runtime/worker_bridge.py",
            }
        )
        if not is_transport:
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [str(node.module or "")]
            if any("repository" in name.split(".") for name in names):
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], (
        "transports must call mounted-core services instead of repositories: "
        f"{offenders}"
    )


def test_subprocess_launches_remain_in_explicit_drivers():
    allowed = {
        "builtin_tools.py",                    # visible user-open operations
        "execution_hosts/local.py",           # shared command/PTY driver
        "execution_hosts/windows_conpty.py",  # shared ConPTY driver
        "extensions/worker_host.py",           # shared-owner plugin worker
        "extensions/owned_stdio.py",           # suspended/owned MCP stdio driver
        "kernel_runtime/lease.py",             # one fenced IPykernel generation
        "model_runtime/hardware.py",           # bounded hardware probes
        "model_runtime/hermes_proxy.py",       # user-selected OAuth proxy service
        "model_runtime/llama_runtime.py",      # pinned runtime acquisition/verification driver
        "model_runtime/llama_server.py",       # shared-owner inference worker
        "model_runtime/ollama_cloud.py",       # user-selected desktop service
        "model_runtime/runtime_installer.py",  # managed runtime driver
        "model_runtime/runtime_recipes.py",    # shared-owner runtime worker
        "openai_codex_oauth.py",               # user-selected Codex auth refresh driver
        "web_search/searxng.py",               # shared-owner container CLI
        "session_catalog/mutation_worker_client.py",  # mutation worker transport
        "speech/local_stt.py",                 # shared-owner speech worker
        "speech/local_neutts.py",              # isolated heavyweight speech worker
    }
    offenders = []
    for path, tree in _production_trees():
        relative = path.relative_to(BACKEND).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            launched = False
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                if function.value.id == "subprocess":
                    launched = function.attr in {
                    "Popen", "run", "call", "check_call", "check_output",
                    }
                elif function.value.id == "asyncio":
                    launched = function.attr in {
                        "create_subprocess_exec", "create_subprocess_shell",
                    }
            if launched and relative not in allowed:
                offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], (
        "subprocess creation belongs in an approved shared-owner/driver module: "
        f"{offenders}"
    )


def test_production_catalog_publishes_only_after_complete_runtime_composition():
    services = (BACKEND / "host_services.py").read_text(encoding="utf-8-sig")
    surface = (BACKEND / "host_tool_surface.py").read_text(encoding="utf-8-sig")
    builder = (BACKEND / "host_runtime_builder.py").read_text(encoding="utf-8-sig")

    assert "defer_publication=True" in services
    assert "reconcile_registry(" not in surface
    assert builder.count("reconcile_registry(") == 1
    assert "reconcile_registry(require_complete=True)" in builder


def test_kernel_generation_owner_is_separate_from_chat_lifecycle_manager():
    from kernel_runtime import continuity, lease, manager

    manager_source = (BACKEND / "kernel_runtime/manager.py").read_text(
        encoding="utf-8-sig"
    )
    lease_source = (BACKEND / "kernel_runtime/lease.py").read_text(
        encoding="utf-8-sig"
    )
    continuity_source = (BACKEND / "kernel_runtime/continuity.py").read_text(
        encoding="utf-8-sig"
    )

    assert manager.KernelLease is lease.KernelLease
    assert "class KernelLease:" not in manager_source
    assert "class KernelRuntimeManager:" not in lease_source
    assert not (BACKEND / "kernel_runtime/debugger.py").exists()
    assert not (BACKEND / "kernel_runtime/debug_coordinator.py").exists()
    assert "debug_operation" not in manager_source
    assert "_debug_session" not in lease_source
    assert manager.KernelContinuityCoordinator is continuity.KernelContinuityCoordinator
    assert "async def _perform_auto_restore(" not in manager_source
    assert "def _continuity_policy_state(" not in manager_source
    assert "class KernelContinuityCoordinator:" in continuity_source


def test_capsule_host_contract_does_not_import_worker_codecs():
    continuity = ast.parse(
        (BACKEND / "kernel_runtime/continuity.py").read_text(encoding="utf-8-sig")
    )
    contracts = ast.parse(
        (BACKEND / "kernel_runtime/capsule_contracts.py").read_text(
            encoding="utf-8-sig"
        )
    )
    continuity_imports = {
        str(node.module or "")
        for node in ast.walk(continuity)
        if isinstance(node, ast.ImportFrom)
    }
    contract_imports = {
        alias.name
        for node in ast.walk(contracts)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        str(node.module or "")
        for node in ast.walk(contracts)
        if isinstance(node, ast.ImportFrom)
    }
    assert "capsule_worker" not in continuity_imports
    assert contract_imports <= {"__future__", "typing"}


def test_mutation_worker_transport_is_separate_from_mutation_state_machine():
    from session_catalog import mutation, mutation_worker_client

    lifecycle_source = (BACKEND / "session_catalog/mutation.py").read_text(
        encoding="utf-8-sig"
    )
    worker_source = (
        BACKEND / "session_catalog/mutation_worker_client.py"
    ).read_text(encoding="utf-8-sig")

    assert mutation.MutationWorkerClient is mutation_worker_client.MutationWorkerClient
    assert "class MutationWorkerClient:" not in lifecycle_source
    assert "create_subprocess_exec(" not in lifecycle_source
    assert "class MutationManager:" not in worker_source
