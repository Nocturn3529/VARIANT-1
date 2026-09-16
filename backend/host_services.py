"""Construction of the single ASTB execution spine installed on HostRuntime."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable


@dataclass(frozen=True)
class ToolActionRuntime:
    """Run every provider action through explicit, mandatory ASTB dependencies."""

    registry: Any
    broker: Any
    session_runtimes: Any
    tool_runner_ports: Callable[[Any], Any]
    headless_tool_runner_ports: Callable[[], Any]
    desktop_action_lock: Any

    def _executor_deps(self):
        import action_executor
        from desktop.catalog import uses_desktop_surface

        return action_executor.ActionExecutorDeps(
            registry=self.registry,
            tool_runner_ports=self.tool_runner_ports,
            headless_tool_runner_ports=self.headless_tool_runner_ports,
            uses_desktop_surface=uses_desktop_surface,
            desktop_action_lock=self.desktop_action_lock,
            capability_broker=self.broker,
        )

    async def run_interactive(self, websocket, actions, should_stop=None):
        import action_executor

        return await action_executor.run_actions_interactive(
            self._executor_deps(), websocket, actions, should_stop=should_stop,
        )

    async def run_headless(self, actions, should_stop=None):
        import action_executor

        return await action_executor.run_actions_headless(
            self._executor_deps(), actions, should_stop=should_stop,
        )


@dataclass(frozen=True)
class AstbServices:
    """Complete construction value copied directly onto HostRuntime."""

    registry: Any
    broker: Any
    catalog: Any
    catalog_releases: Any
    session_control: Any
    session_runtimes: Any
    kernel: Any
    actions: ToolActionRuntime
    session_artifacts: Any
    memory_store: Any


def build_astb_services(host, *, registry: Any, chat_sessions: Any) -> AstbServices:
    """Build the ASTB spine without publishing a partial live service graph."""

    catalog_root = os.path.join(host.data_dir, "data", "astb")
    catalog_database = os.path.join(catalog_root, "astb.sqlite3")
    cfg = getattr(host.router, "cfg", None) or {}
    surface_cfg = dict(cfg.get("action_surface") or cfg.get("astb") or {})

    from session_catalog.control import ControlPlane
    from runtime_profiles import identity_for_profile
    from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
    from session_catalog.profiles import ACTION_SURFACE

    session_control = ControlPlane(catalog_database, surface_cfg)
    session_runtime_repository = SessionRuntimeRepository(catalog_database)
    catalog_holder: dict[str, Any] = {}

    def identity_factory(_chat_id: str, _is_new: bool):
        del _chat_id, _is_new
        catalog = catalog_holder.get("catalog")
        if catalog is None:
            raise RuntimeError("ASTB catalog is not ready")
        return identity_for_profile(host, catalog, ACTION_SURFACE)

    session_runtimes = SessionRuntimeRegistry(
        session_runtime_repository,
        identity_factory=identity_factory,
    )
    chat_sessions.bind_runtime_lifecycle(session_runtimes.ensure_runtime)

    from kernel_runtime.integration import (
        broker_enabled_names,
        register_ipython_tool,
    )

    # The handler resolves the installed runtime only when invoked. Registration
    # is safe before HostRuntime publication; execution is not.
    register_ipython_tool(registry, host.require_runtime)
    from peers.capabilities import register_peers_tool

    register_peers_tool(registry, host.require_runtime, host)

    from artifacts.store import ContentAddressedArtifactStore
    from capability_broker import CapabilityBroker
    from desktop.catalog import uses_desktop_surface

    session_artifacts = ContentAddressedArtifactStore(
        os.path.join(catalog_root, "artifacts")
    )
    from memory_store import MemoryStore

    memory_store = MemoryStore(catalog_database)
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=session_runtimes,
        enabled_resolver=lambda: broker_enabled_names(registry),
        artifact_store=session_artifacts,
        outer_call_repository=session_runtime_repository,
        uses_desktop_surface=uses_desktop_surface,
        desktop_action_lock=host.desktop_action_lock,
    )

    # Cloud-model content is sanitized only after each adapter has assembled
    # its final wire payload. Resolve secrets from the installed or pending
    # extension service without publishing that service twice.
    def host_managed_model_secrets():
        rows = []
        messaging = getattr(host, "messaging_credentials", None)
        if messaging is not None:
            for adapter in ("discord", "telegram"):
                try:
                    value = messaging.token(adapter)
                except Exception:
                    value = ""
                if value:
                    rows.append((f"messaging.{adapter}", value))
        installed = getattr(host, "runtime", None)
        extension_runtime = (
            getattr(installed, "extensions", None)
            or getattr(host, "_pending_extensions", None)
        )
        mcp = getattr(extension_runtime, "mcp", None)
        configured = mcp.configured() if mcp is not None else ()
        for server in configured:
            server_id = str(server.get("server_id") or "")
            spec = dict(server.get("spec") or {})
            env = spec.get("env") if isinstance(spec.get("env"), dict) else None
            for name, value in (env or {}).items():
                if value:
                    rows.append((f"mcp.{server_id}.{name}", str(value)))
            headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else None
            for name, value in (headers or {}).items():
                if value:
                    rows.append((f"mcp.{server_id}.header.{name}", str(value)))
        return rows

    from model_runtime.secret_egress import SecretEgressFirewall

    host.router.register_model_secret_resolver(host_managed_model_secrets)
    host.router.set_secret_egress_firewall(SecretEgressFirewall(
        artifact_store=session_artifacts,
        known_secret_resolver=host.router._known_model_secrets,
    ))

    if host._pending_extensions is None:
        from extensions import create_extension_v2_runtime

        bundled_plugins = os.path.join(host.app_root, "config", "plugins")
        user_plugins = os.path.join(host.config_dir, "plugins")
        os.makedirs(user_plugins, exist_ok=True)
        plugin_sources = [bundled_plugins]
        if os.path.normcase(os.path.realpath(user_plugins)) != os.path.normcase(
            os.path.realpath(bundled_plugins)
        ):
            plugin_sources.append(user_plugins)
        host._pending_extensions = create_extension_v2_runtime(
            os.path.join(os.path.abspath(host.data_dir), "data"),
            variant1_version=str(host.version or "0.1.0"),
            plugin_sources=plugin_sources,
        )

    from session_catalog.service import CatalogService

    catalog = CatalogService(
        database_path=catalog_database,
        artifact_store=session_artifacts,
        registry=registry,
        broker=broker,
        runtime_registry=session_runtimes,
        enabled_resolver=lambda: broker_enabled_names(registry),
        mutation_allowed=session_control.mutation_allowed,
        host=host,
        defer_publication=True,
    )
    catalog_holder["catalog"] = catalog
    session_runtimes.register_chat_cleanup(catalog.repository.delete_chat_state)
    session_runtimes.register_chat_cleanup(memory_store.delete_chat)
    session_runtimes.register_chat_cleanup(catalog.mutation.delete_chat)
    if catalog.children is not None:
        session_runtimes.register_chat_cleanup(catalog.children.delete_chat)

    from session_catalog.releases import CatalogReleases

    catalog_releases = CatalogReleases(
        catalog_database,
        artifact_store=session_artifacts,
        mutation=catalog.mutation,
        catalog_repository=catalog.repository,
    )

    from kernel_runtime.capsules import KernelCheckpointPolicy
    from kernel_runtime.contracts import KernelLimits
    from kernel_runtime.manager import KernelRuntimeManager
    from model_runtime.context import session_model_route

    kernel_cfg = dict(surface_cfg.get("kernel") or {})
    defaults = KernelLimits()
    checkpoint_cfg = dict(kernel_cfg.get("checkpoint") or {})
    checkpoint_defaults = KernelCheckpointPolicy()
    configured_reasons = checkpoint_cfg.get("reasons")
    checkpoint_reasons = (
        tuple(
            str(item).strip()
            for item in configured_reasons
            if str(item or "").strip()
        )
        if isinstance(configured_reasons, list)
        else checkpoint_defaults.reasons
    )

    def fanout_limit(chat_id: str) -> int:
        route = session_model_route(chat_sessions, chat_id, host.router)
        if str(route.get("mode") or "") == "local":
            return int(kernel_cfg.get("local_fanout", 1) or 1)
        return int(kernel_cfg.get("cloud_fanout", 8) or 8)

    kernel = KernelRuntimeManager(
        registry=session_runtimes,
        broker=broker,
        artifact_store=session_artifacts,
        root=os.path.join(catalog_root, "kernels"),
        instance_id=host.instance_id,
        app_root=host.app_root,
        limits=KernelLimits(
            boot_timeout_s=float(kernel_cfg.get("boot_timeout_s", defaults.boot_timeout_s)),
            cell_timeout_s=float(kernel_cfg.get("cell_timeout_s", defaults.cell_timeout_s)),
            interrupt_grace_s=float(kernel_cfg.get("interrupt_grace_s", defaults.interrupt_grace_s)),
            shutdown_grace_s=float(kernel_cfg.get("shutdown_grace_s", defaults.shutdown_grace_s)),
            bridge_timeout_s=float(kernel_cfg.get("bridge_timeout_s", defaults.bridge_timeout_s)),
            bridge_max_frame_bytes=int(kernel_cfg.get("bridge_max_frame_bytes", defaults.bridge_max_frame_bytes)),
            max_live_kernels=int(kernel_cfg.get("max_live_kernels", defaults.max_live_kernels)),
            max_boot_concurrency=int(kernel_cfg.get("max_boot_concurrency", defaults.max_boot_concurrency)),
            idle_lifetime_s=float(kernel_cfg.get("idle_lifetime_s", defaults.idle_lifetime_s)),
            absolute_lifetime_s=float(kernel_cfg.get("absolute_lifetime_s", defaults.absolute_lifetime_s)),
            max_processes=int(kernel_cfg.get("max_processes", defaults.max_processes)),
            process_memory_bytes=int(kernel_cfg.get("process_memory_bytes", defaults.process_memory_bytes)),
            job_memory_bytes=int(kernel_cfg.get("job_memory_bytes", defaults.job_memory_bytes)),
            cpu_percent=int(kernel_cfg.get("cpu_percent", defaults.cpu_percent)),
            worker_stream_chunk_bytes=int(kernel_cfg.get("worker_stream_chunk_bytes", defaults.worker_stream_chunk_bytes)),
            worker_stream_cell_bytes=int(kernel_cfg.get("worker_stream_cell_bytes", defaults.worker_stream_cell_bytes)),
            worker_rich_message_bytes=int(kernel_cfg.get("worker_rich_message_bytes", defaults.worker_rich_message_bytes)),
            output=defaults.output,
        ),
        worker_executable=str(kernel_cfg.get("worker_executable") or ""),
        catalog_service=catalog,
        new_kernel_allowed=session_control.new_kernel_allowed,
        fanout_limit_resolver=fanout_limit,
        checkpoint_policy=KernelCheckpointPolicy(
            enabled=bool(checkpoint_cfg.get("enabled", False)),
            restore_on_boot=bool(checkpoint_cfg.get("restore_on_boot", False)),
            reasons=checkpoint_reasons,
        ),
        runtime_profile_id=str(kernel_cfg.get("runtime_profile") or "core.v1"),
    )
    catalog.mutation.configure_worker(worker_executable=kernel.worker_executable)

    actions = ToolActionRuntime(
        registry=registry,
        broker=broker,
        session_runtimes=session_runtimes,
        tool_runner_ports=host.tool_runner_ports,
        headless_tool_runner_ports=host.headless_tool_runner_ports,
        desktop_action_lock=host.desktop_action_lock,
    )
    return AstbServices(
        registry=registry,
        broker=broker,
        catalog=catalog,
        catalog_releases=catalog_releases,
        session_control=session_control,
        session_runtimes=session_runtimes,
        kernel=kernel,
        actions=actions,
        session_artifacts=session_artifacts,
        memory_store=memory_store,
    )


__all__ = ["AstbServices", "ToolActionRuntime", "build_astb_services"]
