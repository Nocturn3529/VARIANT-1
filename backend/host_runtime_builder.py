"""Composition of VARIANT-1's typed late-bound host runtime."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from host_chat_service import ChatService
from host_lifecycle_service import LifecycleService
from host_memory_service import MemoryService
from host_model_service import ModelService
from host_runtime import HostRuntime
from host_tool_service import ToolSurfaceService
from host_workflow_service import WorkflowService
from speech.service import SpeechService
from transcript_service import TranscriptService

if TYPE_CHECKING:
    from app_host import AppHost


def _configure_runtime_adapters(host: "AppHost") -> None:
    """Bind process mechanics to their one installed runtime owner."""
    from automation import scheduler as agent_scheduler
    from run_context import current_run_context

    desktop_cfg = (host.tools_cfg.data.get("desktop", {}) or {})
    host.desktop_control.set_recovery_config(desktop_cfg.get("recovery"))
    host.desktop_control.set_perception_config(desktop_cfg.get("perception"))
    host.desktop_control.set_observability_config(desktop_cfg.get("observability"))

    async def desktop_perception_activity(event: str, **fields):
        await host.emit_activity(event, **fields)

    host.desktop_control.set_activity_emitter(desktop_perception_activity)
    host.desktop_control.set_ctx_getter(
        lambda: int(host.router.projection_budget_tokens() or 0)
    )
    def local_gate():
        principal = agent_scheduler.principal_for_context(current_run_context())
        return host.scheduler.slot(principal)

    host.router.set_local_gate(local_gate)


def install_host_runtime(
    host: "AppHost",
) -> HostRuntime:
    """Build, install, and activate the complete service graph exactly once."""
    # Import at composition time rather than module import time.  Work Fabric
    # may depend on host-owned stores, while its factory derives all concrete
    # paths and worker identity from the already-bootstrapped AppHost.
    from work_fabric.service import build_work_runtime
    from chat_sessions import build_chat_sessions
    from execution_hosts import create_execution_runtime
    from coding import create_coding_runtime
    from goals import create_goal_service, recover_goal_supervisors
    from goals.host_handlers import build_goal_host_handlers
    from artifacts import create_artifact_runtime
    from browser_fabric import create_browser_fabric, install_browser_fabric
    from desktop_fabric import create_desktop_fabric, install_desktop_fabric
    from extensions import create_extension_v2_runtime

    registry = host._pending_registry
    if registry is None:
        raise RuntimeError("HostRuntime installation requires the bootstrap registry")
    sessions = host._pending_sessions
    if sessions is None:
        sessions = build_chat_sessions(host)
    from host_services import build_astb_services

    astb = build_astb_services(
        host,
        registry=registry,
        chat_sessions=sessions,
    )

    transcript = TranscriptService(host)
    models = ModelService(host)
    from model_runtime.local_models import LocalModelLibrary
    host.local_models = LocalModelLibrary(host)
    chat = ChatService(host, transcript, models)
    workflows = WorkflowService(host)
    work = build_work_runtime(host)
    workflows.bind_work(work)
    host.runtime_installer.bind_work(work)
    host.runtime_recipes.bind_work(work)
    children = getattr(astb.catalog, "children", None)
    if children is not None:
        children.bind_work(work)
        if getattr(host, "hub", None) is not None:
            # Composition owns the websocket sink. Lifespan attaches its
            # running loop before Work starts, allowing sync/thread child
            # commits to publish through this one canonical path.
            children.bind_change_publisher(host.hub.broadcast)
    data_root = os.path.join(os.path.abspath(host.data_dir), "data")
    artifacts = create_artifact_runtime(work, astb.session_artifacts)
    async def browser_credential(provider, default_base_url):
        from service_credentials import resolve
        return await resolve(host.router, 'browser', provider, default_base_url=default_base_url)
    browser = create_browser_fabric(
        data_dir=data_root,
        artifact_store=astb.session_artifacts,
        cloud_credential_resolver=browser_credential,
    )
    browser.startup()
    if getattr(host, 'hub', None) is not None:
        browser.preferences.publisher = host.hub.broadcast
    desktop = create_desktop_fabric(
        data_dir=data_root,
        artifact_store=astb.session_artifacts,
        desktop_control=host.desktop_control,
        backend_instance_id=host.instance_id,
    )
    extensions = host._pending_extensions
    if extensions is None:
        os.makedirs(os.path.join(host.config_dir, "plugins"), exist_ok=True)
        extensions = create_extension_v2_runtime(
            data_root,
            variant1_version=str(host.version or "0.1.0"),
            plugin_sources=(os.path.join(host.config_dir, "plugins"),),
        )
    execution = create_execution_runtime(
        data_dir=data_root,
        artifact_store=astb.session_artifacts,
        backend_instance_id=host.instance_id,
    )
    astb.session_runtimes.register_chat_cleanup(execution.delete_chat)

    def worktree_process_owned(worktree_id: str) -> bool:
        return execution.worktree_has_live_owner(worktree_id)

    coding = create_coding_runtime(
        data_dir=data_root,
        artifact_store=astb.session_artifacts,
        process_owned=worktree_process_owned,
        process_service=execution.processes,
    )
    coding.startup()
    goals = create_goal_service(work)
    goal_adapters = build_goal_host_handlers(host, goals)
    goals.register_cancellation_handler(goal_adapters.cancel_goal_resources)
    for kind, handler in goal_adapters.handlers().items():
        goals.executor.register(kind, handler)
    for source, resolver in goal_adapters.wait_resolvers().items():
        goals.register_wait_resolver(source, resolver)
    recover_goal_supervisors(goals)
    from peers import PeerCommunicationService, PeerRepository

    peers = PeerCommunicationService(
        host,
        PeerRepository(astb.session_runtimes.repository.path),
        sessions=sessions,
        session_runtimes=astb.session_runtimes,
        chat_service=chat,
    )
    if getattr(host, "hub", None) is not None:
        peers.bind_publisher(host.hub.broadcast)
    runtime = HostRuntime(
        chat=chat,
        memory=MemoryService(host, astb.memory_store),
        models=models,
        voice=SpeechService(host),
        workflows=workflows,
        tool_settings=ToolSurfaceService(host),
        registry=astb.registry,
        broker=astb.broker,
        catalog=astb.catalog,
        catalog_releases=astb.catalog_releases,
        session_control=astb.session_control,
        session_runtimes=astb.session_runtimes,
        kernel=astb.kernel,
        actions=astb.actions,
        session_artifacts=astb.session_artifacts,
        work=work,
        sessions=sessions,
        execution=execution,
        coding=coding,
        goals=goals,
        peers=peers,
        artifacts=artifacts,
        browser=browser,
        desktop=desktop,
        extensions=extensions,
        lifecycle=LifecycleService(host),
    )
    host.install_runtime(runtime)
    runtime.session_runtimes.register_chat_cleanup(work.delete_chat)
    runtime.session_runtimes.register_chat_cleanup(goals.delete_chat)
    runtime.session_runtimes.register_chat_cleanup(peers.delete_chat)
    runtime.session_runtimes.register_chat_cleanup(coding.delete_chat)
    runtime.session_runtimes.register_chat_tombstone_cleanup(
        runtime.kernel.close_chat
    )
    runtime.session_runtimes.register_chat_tombstone_cleanup(
        execution.delete_chat
    )
    runtime.session_runtimes.register_chat_tombstone_cleanup(coding.stop_chat)
    runtime.session_runtimes.register_chat_tombstone_cleanup(
        browser.delete_chat
    )
    runtime.session_runtimes.register_chat_tombstone_cleanup(work.delete_chat)
    runtime.session_runtimes.register_chat_tombstone_cleanup(goals.delete_chat)
    runtime.session_runtimes.register_chat_tombstone_cleanup(peers.delete_chat)
    if runtime.catalog.children is not None:
        runtime.session_runtimes.register_chat_tombstone_cleanup(
            runtime.catalog.children.cancel_chat
        )
    _configure_runtime_adapters(host)
    from host_tool_surface import register_all

    register_all(host)
    install_browser_fabric(browser, host)
    runtime.session_runtimes.register_chat_cleanup(browser.delete_chat)
    install_desktop_fabric(desktop, host)
    if getattr(host, "gateway", None) is not None:
        host.gateway.bind_extension_runtime(extensions)
    from work_fabric.capabilities import register_work_fabric_tools
    from kernel_runtime.capabilities import (
        register_kernel_control_job_handlers,
    )
    from execution_hosts.capabilities import register_execution_tools
    from coding.job_handlers import register_coding_job_handlers
    from artifacts.capabilities import register_artifact_tools
    from extensions.capabilities_v2 import (
        register_extension_job_handlers,
        register_extension_v2_tools,
    )
    from browser_fabric.capabilities import register_browser_fabric_tools

    register_work_fabric_tools(host)
    # All durable kinds are installed before server_lifespan starts Work Fabric.
    register_kernel_control_job_handlers(host)
    register_execution_tools(host)
    register_coding_job_handlers(host)
    register_artifact_tools(host)
    register_extension_job_handlers(host)
    register_extension_v2_tools(host)
    register_browser_fabric_tools(host)
    if getattr(host, "hub", None) is not None:
        async def broadcast_work_event(event):
            try:
                from observability.operational_log import mirror_work_terminal

                mirror_work_terminal(
                    event,
                    work.jobs.get(str(event.aggregate_id or ""))
                    if event.aggregate_kind == "job" else None,
                )
            except Exception:
                pass
            await host.hub.broadcast({
                "type": "work:event",
                "schema": "variant1.work-event.v1",
                "event": event.to_dict(),
            })

        work.events.subscribe(broadcast_work_event)
    register_receipt_sink = getattr(
        runtime.broker, "register_receipt_sink", None
    )
    if callable(register_receipt_sink):
        register_receipt_sink(work.record_capability_receipt)
    # Durable domain capabilities are registered by this composition stage,
    # after CatalogService is constructed during bootstrap. Publish the fully
    # composed seed catalog now so new runtimes pin every mounted namespace and
    # handler revision rather than the bootstrap-only subset.
    runtime.catalog.reconcile_registry(require_complete=True)
    runtime_chat_ids = [
        str(row.get("id") or "")
        for row in (runtime.sessions.list_sessions() or ())
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    ]
    runtime.session_runtimes.reconcile_runtime_owners(runtime_chat_ids)
    host._pending_registry = None
    host._pending_sessions = None
    host._pending_extensions = None
    return runtime
