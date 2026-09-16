"""VARIANT-1 application host — explicit service and runtime composition.

Stable process services live directly on ``AppHost``. A graph of concrete,
typed runtime services is installed once as ``HostRuntime`` after bootstrap.
Port factories close over those two explicit dependency sets.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from host_protocols import (
    ActivityHubPort,
    RouterPort,
    ToolRegistryPort,
)
from host_runtime import ChatSessionRuntime, ExtensionRuntime, HostRuntime

@dataclass
class AppHost:
    """Process-wide stable services and the installed runtime graph."""

    # Core inference / tools
    router: Optional[RouterPort] = None
    tools_cfg: Any = None

    # Stores
    automations: Any = None
    messaging_credentials: Any = None

    # Sidecars / services
    hub: Optional[ActivityHubPort] = None
    voice: Any = None
    gateway: Any = None
    scheduler: Any = None
    automation_history: Any = None
    desktop_control: Any = None
    Task: Any = None
    searxng: Any = None
    hardware: Any = None
    inference_observability: Any = None
    runtime_installer: Any = None
    local_models: Any = None
    runtime_recipes: Any = None
    remote_nodes: Any = None
    inference_benchmarks: Any = None
    background_tasks: Any = None
    desktop_action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    secretstore: Any = None
    # Bootstrap-only construction values. They are deliberately private and
    # cleared immediately after the one complete HostRuntime is installed.
    # Live code must use require_runtime(); these are not alternate authorities.
    _pending_registry: Optional[ToolRegistryPort] = None
    _pending_sessions: Optional[ChatSessionRuntime] = None
    _pending_extensions: Optional[ExtensionRuntime] = None
    remote_handle_routers: dict[str, Any] = field(default_factory=dict)
    runtime: Optional[HostRuntime] = None
    startup_ready: bool = False
    startup_error: str = ""
    startup_failures: list[dict] = field(default_factory=list)
    _local_model_switching: bool = False
    _local_model_switch_generation: int = 0
    _local_model_switch_target: str = ""

    # Paths / constants
    data_dir: str = ""
    config_dir: str = ""
    app_root: str = ""
    version: str = ""
    instance_id: str = ""
    start_time: float = 0.0
    last_user_text: str = ""

    def install_runtime(self, runtime: HostRuntime) -> None:
        """Install the complete named runtime exactly once."""
        if self.runtime is not None:
            raise RuntimeError("AppHost runtime is already installed")
        self.runtime = runtime

    def require_runtime(self) -> HostRuntime:
        runtime = self.runtime
        if runtime is None:
            raise RuntimeError("AppHost runtime has not been installed")
        return runtime

    def set_last_user_text(self, text: str) -> None:
        self.last_user_text = text or ""

    async def emit_activity(self, event: str, **fields) -> None:
        from observability.activity import emit_activity
        await emit_activity(event, **fields)

    @staticmethod
    def new_run(kind: str, title: str):
        from observability.activity import new_run
        return new_run(kind, title)

    @staticmethod
    def clip(text: str, limit: int) -> str:
        from observability.activity import clip
        return clip(text, limit)

    def agent_mode(self) -> str:
        import host_flags
        return host_flags.agent_mode(self.router.cfg or {})

    def vision_cfg(self) -> dict:
        import host_flags
        return host_flags.vision_cfg_from_router(self.router)

    def tts_enabled(self) -> bool:
        import host_voice
        return host_voice.tts_enabled(self.router)

    def tts_speed(self) -> float:
        import host_voice
        return host_voice.tts_speed(self.router)

    def active_model_name(self) -> str:
        if getattr(self.router, "mode", "local") == "cloud":
            model = self.router.get_cloud_model()
            return model if isinstance(model, str) else ""
        model = getattr(self.router, "model_name", "")
        return model if isinstance(model, str) else ""

    def engine_label(self) -> str:
        import host_status
        return host_status.engine_label(self)

    def engine_status_message(self) -> dict:
        import host_status
        return host_status.engine_status_msg(self)

    def tools_prompt_block(self, enabled: set, specs: list) -> str:
        import host_prompt
        return host_prompt.tools_prompt_block(self, enabled, specs)

    def tool_lines(self, specs: list) -> str:
        import host_prompt
        return host_prompt.tool_lines(self, specs)

    def state_block(self, task) -> str:
        import host_prompt
        return host_prompt.state_block(self, task)

    def make_run_context(self, *args, **kwargs):
        import host_run_context
        return host_run_context.make_run_context(self, *args, **kwargs)

    def require_bound_run_context(self, source: str, *, operation: str):
        import host_run_context
        return host_run_context.require_bound_run_context(source, operation=operation)

    async def mem_query(self, text, n=5):
        import host_memory_ops
        return await host_memory_ops.mem_query(self, text, n)

    async def mem_prefetch(self, text, n=2):
        import host_memory_ops
        return await host_memory_ops.mem_prefetch(self, text, n)

    async def mem_add(self, *args, **kwargs):
        import host_memory_ops
        return await host_memory_ops.mem_add(self, *args, **kwargs)

    async def mem_add_record(self, *args, **kwargs):
        import host_memory_ops
        return await host_memory_ops.mem_add_record(self, *args, **kwargs)

    async def mem_delete(self, *args, **kwargs):
        import host_memory_ops
        return await host_memory_ops.mem_delete(self, *args, **kwargs)

    async def mem_list(self, *, limit=200, offset=0):
        import host_memory_ops
        return await host_memory_ops.mem_list(self, limit=limit, offset=offset)

    # ------------------------------------------------------------------
    # Ports factories — public composition API.
    # ------------------------------------------------------------------

    def child_worker_ports(self):
        import host_ports
        return host_ports.child_worker_ports(self)

    def memory_ports(self):
        import host_ports
        return host_ports.memory_ports(self)

    def tool_runner_ports(self, websocket, session=None):
        import host_ports
        return host_ports.tool_runner_ports(self, websocket, session=session)

    def headless_tool_runner_ports(self):
        import host_ports
        return host_ports.headless_tool_runner_ports(self)

    def automation_ports(self):
        import host_ports
        return host_ports.automation_ports(self)

    def chat_ports(self):
        import host_ports
        return host_ports.chat_ports(self)

    def task_turn_ports(self, websocket, session):
        import host_ports
        return host_ports.build_task_turn_ports(self, websocket, session)

