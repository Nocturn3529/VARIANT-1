"""Framework-neutral entry points for VARIANT-1 agent execution.

Product callers depend on this module, never on an orchestration framework.
Agent behavior belongs in the shared nodes and state contract; execution and
durability belong in VARIANT-1's native runner and snapshot store.
"""

from __future__ import annotations

from typing import Any

from .config import AgentRunConfig, validate_config
from .state import RunState


async def execute_main_chat(**kwargs: Any) -> Any:
    """Execute a main-chat turn on VARIANT-1's native state machine."""
    config = kwargs.get("config")
    if not isinstance(config, AgentRunConfig):
        raise TypeError("main-chat execution requires AgentRunConfig")
    validate_config(config)
    if config.source != "chat":
        raise ValueError(f"main-chat execution requires source='chat' (got {config.source!r})")

    from .runner import run_main_chat_task

    return await run_main_chat_task(**kwargs)


async def execute_headless_worker(**kwargs: Any) -> RunState:
    """Execute a worker without exposing its orchestration backend to callers."""
    config = kwargs.get("config")
    if not isinstance(config, AgentRunConfig):
        raise TypeError("headless execution requires AgentRunConfig")
    validate_config(config)
    if config.source == "chat":
        raise ValueError("headless execution cannot use source='chat'")

    from .runner import run_headless_worker

    return await run_headless_worker(**kwargs)
