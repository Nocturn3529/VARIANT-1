"""Shared dependency contracts for VARIANT-1's agent-backed product surfaces.

Interactive chat, subagents, and automations all use
the same transcript-economy and headless tool capabilities.  These contracts
make that common surface explicit so adding a context or tool-loop dependency
does not require editing every product-specific ports dataclass independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass
class AgentContextPorts:
    """Prompt measurement and transcript compression."""

    approx_tokens: Callable[[list], int]
    ctx_compress_threshold: Callable[[], int]
    compress_messages: Callable[..., Awaitable[list]]
    count_tokens: Optional[Callable[..., Awaitable[Optional[int]]]] = None
    context_limit: Optional[Callable[[], int]] = None


@dataclass
class ToolSurfacePorts:
    """The single provider projection and execution surface for workers."""

    tools_prompt_block: Callable[[set, list], str]
    run_actions_headless: Callable[..., Awaitable[Any]]


@dataclass
class HeadlessAgentPorts:
    """Capabilities shared by every non-interactive agent-backed product."""

    context: AgentContextPorts
    tools: ToolSurfacePorts
    make_run_context: Callable[..., Any]
    prepare_worker_surface: Optional[Callable[..., dict]] = None
    begin_worker_run: Optional[Callable[..., Awaitable[str]]] = None
    finish_worker_run: Optional[Callable[..., None]] = None
    delete_worker_runtime: Optional[Callable[..., Awaitable[bool]]] = None
    snapshot_store: Any = None
