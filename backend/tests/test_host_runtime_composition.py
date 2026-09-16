"""Typed AppHost runtime composition and status projection invariants."""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints
from unittest.mock import MagicMock, patch

import pytest

from app_host import AppHost
from host_runtime import (
    ArtifactAuthoringRuntime,
    ActionRuntime,
    BrowserFabricRuntime,
    CapabilityBrokerRuntime,
    CatalogRuntime,
    ChatRuntime,
    CodingRuntime,
    ChatSessionRuntime,
    ExecutionHostRuntime,
    GoalRuntime,
    DesktopFabricRuntime,
    ExtensionRuntime,
    SessionRuntimeRegistryRuntime,
    HostRuntime,
    KernelRuntime,
    LifecycleRuntime,
    MemoryRuntime,
    ModelRuntime,
    PeerRuntime,
    ToolRuntime,
    ToolRegistryRuntime,
    VoiceRuntime,
    WorkRuntime,
    WorkflowRuntime,
)
import host_status


def test_host_modules_never_import_the_server_composition_root():
    backend = Path(__file__).resolve().parents[1]
    offenders = []
    for path in backend.glob("host_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "server" or name.startswith("server.") for name in names):
                offenders.append(path.name)
                break
    assert offenders == []


def test_runtime_manifest_contains_only_named_service_protocols():
    assert get_type_hints(HostRuntime) == {
        "chat": ChatRuntime,
        "memory": MemoryRuntime,
        "models": ModelRuntime,
        "voice": VoiceRuntime,
        "workflows": WorkflowRuntime,
        "tool_settings": ToolRuntime,
        "registry": ToolRegistryRuntime,
        "broker": CapabilityBrokerRuntime,
        "catalog": CatalogRuntime,
        "catalog_releases": Any,
        "session_control": Any,
        "session_runtimes": SessionRuntimeRegistryRuntime,
        "kernel": KernelRuntime,
        "actions": ActionRuntime,
        "session_artifacts": Any,
        "work": WorkRuntime,
        "sessions": ChatSessionRuntime,
        "execution": ExecutionHostRuntime,
        "coding": CodingRuntime,
        "goals": GoalRuntime,
        "peers": PeerRuntime,
        "artifacts": ArtifactAuthoringRuntime,
        "browser": BrowserFabricRuntime,
        "desktop": DesktopFabricRuntime,
        "extensions": ExtensionRuntime,
        "lifecycle": LifecycleRuntime,
    }
    assert HostRuntime.__dataclass_params__.frozen is True


def test_runtime_installs_once_without_dynamic_attach_api():
    host = AppHost()
    runtime = MagicMock(spec=HostRuntime)

    host.install_runtime(runtime)

    assert host.require_runtime() is runtime
    assert not hasattr(host, "attach_op")
    assert not hasattr(host, "attach_ops")
    with pytest.raises(RuntimeError, match="already installed"):
        host.install_runtime(MagicMock(spec=HostRuntime))


def test_production_runtime_is_concrete_and_signature_complete():
    import server

    runtime = server.APP.require_runtime()

    assert isinstance(runtime.chat, ChatRuntime)
    assert isinstance(runtime.memory, MemoryRuntime)
    assert isinstance(runtime.models, ModelRuntime)
    assert isinstance(runtime.voice, VoiceRuntime)
    assert isinstance(runtime.workflows, WorkflowRuntime)
    assert isinstance(runtime.artifacts, ArtifactAuthoringRuntime)
    assert isinstance(runtime.browser, BrowserFabricRuntime)
    assert isinstance(runtime.desktop, DesktopFabricRuntime)
    assert isinstance(runtime.extensions, ExtensionRuntime)
    assert isinstance(runtime.tool_settings, ToolRuntime)
    assert isinstance(runtime.registry, ToolRegistryRuntime)
    assert isinstance(runtime.broker, CapabilityBrokerRuntime)
    assert isinstance(runtime.catalog, CatalogRuntime)
    assert isinstance(runtime.session_runtimes, SessionRuntimeRegistryRuntime)
    assert isinstance(runtime.kernel, KernelRuntime)
    assert isinstance(runtime.actions, ActionRuntime)
    assert isinstance(runtime.work, WorkRuntime)
    assert isinstance(runtime.sessions, ChatSessionRuntime)
    assert isinstance(runtime.execution, ExecutionHostRuntime)
    assert isinstance(runtime.coding, CodingRuntime)
    assert isinstance(runtime.goals, GoalRuntime)
    assert isinstance(runtime.lifecycle, LifecycleRuntime)
    assert type(runtime.chat).__name__ == "ChatService"
    assert type(runtime.voice).__name__ == "SpeechService"

    # The ASTB spine is one object graph, not mirrored aliases on AppHost.
    assert runtime.actions.registry is runtime.registry
    assert runtime.actions.broker is runtime.broker
    assert runtime.catalog.registry is runtime.registry
    assert runtime.catalog.broker is runtime.broker
    assert runtime.catalog.runtime_registry is runtime.session_runtimes
    assert runtime.kernel.registry is runtime.session_runtimes
    assert runtime.kernel.broker is runtime.broker
    assert runtime.kernel.catalog_service is runtime.catalog
    assert server.APP._pending_registry is None
    assert server.APP._pending_sessions is None
    assert server.APP._pending_extensions is None
    retired_aliases = {
        "registry", "tool_runtime", "capability_broker",
        "session_artifacts", "session_catalog", "catalog_releases",
        "session_control", "session_runtime_repository",
        "session_runtimes", "kernel_runtime", "actions",
    }
    assert not any(hasattr(server.APP, name) for name in retired_aliases)


def test_engine_status_reads_the_lowercase_scheduler_service():
    router = MagicMock()
    router.engine_ready = True
    router.model_name = "test-model"
    router.mode = "local"
    router.local_prewarm = False
    router.cloud_provider = "openai"
    router.reasoning = False
    router.sampling = {}
    router.cfg = {"voice": {}}
    router.wants_local_engine.return_value = True
    router.get_cloud_model.return_value = "cloud-model"
    router.usage_snapshot.return_value = {}
    router.oauth_status.return_value = {"connected": False}
    router.has_cloud_key.return_value = False
    router.engine.runtime_status.return_value = {}
    scheduler = MagicMock()
    scheduler.status.return_value = {"queue_depth": 3}
    host = AppHost(
        router=router,
        scheduler=scheduler,
        secretstore=SimpleNamespace(is_available=lambda: True),
        hardware={},
    )

    with patch.object(host_status, "voice_state", return_value={}):
        payload = host_status.engine_status_msg(host)

    assert payload["scheduler"] == {"queue_depth": 3}
    assert "contract" not in payload
    scheduler.status.assert_called_once_with()


def test_voice_status_has_only_structured_stt_and_tts_contracts():
    router = MagicMock()
    router.mode = "local"
    router.cfg = {"voice": {}}
    host = AppHost(
        router=router,
        voice=SimpleNamespace(installed=lambda: True, ready=True),
    )

    with patch("host_status.tts.asset_status", return_value={
        "available": True, "drop_path": "", "model_path": "",
        "voices_path": "", "required_files": [],
    }):
        payload = host_status.voice_state(host)

    assert set(payload) == {"stt", "tts"}
    assert payload["stt"]["local_available"] is True
    assert payload["tts"]["local_available"] is True
    SessionRuntimeRegistryRuntime,
    ToolRegistryRuntime,
