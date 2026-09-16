"""Construct the stable service graph for one :class:`AppHost`.

Constructs router, tool surface, durable stores, sidecars, and paths **onto**
a new ``AppHost``. Callers receive the host and do not build parallel module
service globals.

The process composition layer installs one named ``HostRuntime`` after the
stable services exist. No provisional callbacks or partial runtime are
installed here.
"""

from __future__ import annotations

import os
from typing import Any

from observability.activity import HUB
from agent_task import Task
from llm_router import LLMRouter
from llm_router_config import load_llm_config
from paths import APP_ROOT as DEFAULT_APP_ROOT

from app_host import AppHost
import server_stores
import server_tools_boot


def resolve_data_dirs(
    *,
    app_root: str | None = None,
    data_dir: str | None = None,
    config_dir: str | None = None,
) -> tuple[str, str, str]:
    root = app_root or DEFAULT_APP_ROOT
    data = (
        data_dir
        or os.environ.get("VARIANT1_DATA_DIR")
        or root
    )
    config = config_dir or os.environ.get("VARIANT1_CONFIG") or os.path.join(data, "config")
    return root, data, config


def bootstrap_app_host(
    *,
    app_root: str | None = None,
    data_dir: str | None = None,
    config_dir: str | None = None,
    version: str = "0.1.0",
    hub: Any = None,
    llm_config_path: str | None = None,
) -> AppHost:
    """Construct the process services; runtime installation is a separate step."""
    import desktop_control
    from desktop.runtime import DesktopRuntime

    root, data, config = resolve_data_dirs(
        app_root=app_root, data_dir=data_dir, config_dir=config_dir,
    )
    os.makedirs(config, exist_ok=True)

    h = AppHost()
    h.app_root = root
    h.data_dir = data
    h.config_dir = config
    h.version = version
    h.hub = hub if hub is not None else HUB
    h.Task = Task

    # LLM router
    cfg_path = (
        llm_config_path
        or os.environ.get("VARIANT1_LLM_CONFIG")
        or os.path.join(config, "llm_config.json")
    )
    router = LLMRouter(
        load_llm_config(cfg_path), root, config_path=cfg_path, data_dir=data,
    )
    from model_runtime.inference_observability import InferenceObservability
    h.inference_observability = InferenceObservability(data)

    async def _publish_inference_telemetry(snapshot):
        h.inference_observability.observe_inference(snapshot)
        await h.hub.broadcast(snapshot)

    router.set_inference_telemetry_sink(_publish_inference_telemetry)
    async def _publish_model_request_manifest(manifest):
        await h.hub.broadcast(manifest)
        try:
            from session_context import session_context_from_manifest
            snapshot = session_context_from_manifest(manifest)
            if snapshot is not None:
                await h.hub.broadcast(snapshot)
        except Exception:
            pass
    router.set_model_request_manifest_sink(_publish_model_request_manifest)
    h.router = router

    from security import secretstore
    h.secretstore = secretstore

    # Tool surface (registry, MCP, messaging credentials/gateway, desktop tools)
    surface = server_tools_boot.build_tool_surface(
        app_root=root,
        data_dir=data,
        config_dir=config,
        desktop_control=desktop_control.DesktopControl(DesktopRuntime()),
        router=router,
    )
    apply_tool_surface_to_host(h, surface)
    h.gateway.set_state_sink(h.hub.broadcast)

    # Managed SearXNG is a process service. Construct it beside the tools
    # configuration it consumes instead of adding a second server.py root.
    from web_search.searxng import SearxngServer

    searx_cfg = (h.tools_cfg.web_search or {}).get("searxng") or {}
    h.searxng = SearxngServer(
        searx_cfg if isinstance(searx_cfg, dict) else {},
        data_dir=h.data_dir,
        app_root=h.app_root,
    )

    # SQL chat sessions are the unconditional transcript authority.
    from chat_sessions import build_chat_sessions

    h._pending_sessions = build_chat_sessions(h)

    # Durable stores
    stores = server_stores.build_durable_stores(
        app_root=root,
        data_dir=data,
        config_dir=config,
        router_cfg=router.cfg,
        session_service=h._pending_sessions,
    )
    apply_stores_to_host(h, stores)

    # Local inference control plane. llama.cpp remains the bundled default;
    # optional stacks are discovered/installed into isolated targets and are
    # launched through persistent recipes owned by this graph.
    from model_runtime.benchmark import InferenceBenchmark
    from model_runtime.hardware import telemetry as hardware_telemetry
    from model_runtime.remote_nodes import RemoteNodeManager
    from model_runtime.runtime_installer import RuntimeInstaller
    from model_runtime.runtime_recipes import RuntimeRecipeManager

    event_sink = h.inference_observability.event
    h.runtime_installer = RuntimeInstaller(
        data, h.hub.broadcast, event_sink=event_sink, router=h.router)
    h.remote_nodes = RemoteNodeManager(data, event_sink=event_sink)
    h.runtime_recipes = RuntimeRecipeManager(
        data, h.runtime_installer, h.remote_nodes, h.hub.broadcast,
        event_sink=event_sink,
    )
    h.runtime_recipes.bind_host(h)
    from automation.scheduler import principal_for

    h.inference_benchmarks = InferenceBenchmark(
        data,
        h.hub.broadcast,
        hardware_telemetry,
        event_sink=event_sink,
        admission_gate=lambda: h.scheduler.slot(
            principal_for("benchmark", "Inference benchmark")
        ),
    )

    import background_tasks
    h.background_tasks = background_tasks
    return h


def apply_tool_surface_to_host(h: AppHost, surface) -> None:
    h._pending_registry = surface.registry
    h.tools_cfg = surface.tools_cfg
    h.messaging_credentials = surface.messaging_credentials
    h.gateway = surface.gateway
    h.desktop_control = surface.desktop_control


def apply_stores_to_host(h: AppHost, stores) -> None:
    h.scheduler = stores.scheduler
    h.automation_history = stores.automation_history
    h.automations = stores.automations
    h.voice = stores.voice

