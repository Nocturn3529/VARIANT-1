"""Configuration model for native agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from session_catalog.profiles import (
    CHAT_GRAPH_REVISION,
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
    WORKER_GRAPH_REVISION,
    canonical_action_surface,
    canonical_graph_revision,
    is_action_surface,
)


MAIN_REASONING_MAX_OUTPUT_TOKENS = 16_000
MAIN_PLAIN_MAX_OUTPUT_TOKENS = 1_536
DEFAULT_MAX_CONSECUTIVE_LENGTH_RECOVERIES = 2


@dataclass(frozen=True)
class AgentRunConfig:
    """Configuration surface for one VARIANT-1 agent graph run.

    Presets preserve the current behavioral differences between main chat tasks,
    subagents and automations while sharing one execution substrate.
    """

    name: str
    source: str
    # Every run carries an explicit provider/action contract. Headless sources
    # receive a separately persisted worker identity; they never infer a chat's
    # profile from whichever request happens to invoke them.
    action_surface: str = ACTION_SURFACE
    provider_tool_schema_revision: str = IPYTHON_SCHEMA_REVISION
    checkpoints: bool = False

    # User-facing chat uses one unified persistent-Python route. Its first prompt stays
    # conversational and a text-only first response exits directly.
    # Specialized graph callers and restored/manual tasks keep structured mode.
    unified_conversation: bool = False

    max_output_tokens: int = 1536
    max_consecutive_length_recoveries: int = (
        DEFAULT_MAX_CONSECUTIVE_LENGTH_RECOVERIES
    )

    # Pinned topology/action-surface revision.
    graph_revision: str = ""

    def with_overrides(self, **kwargs: Any) -> "AgentRunConfig":
        data = self.__dict__.copy()
        data.update(kwargs)
        return AgentRunConfig(**data)


# Run sources accepted by the shared agent graph. Keep in sync with
# agent_engine.presets and automation.scheduler.KIND_PRIORITY.
_VALID_SOURCES = frozenset({
    "chat",
    "subagent",
    "automation",
})


def validate_config(config: AgentRunConfig) -> None:
    if config.source not in _VALID_SOURCES:
        raise ValueError(f"unknown agent source: {config.source}")
    surface = canonical_action_surface(config.action_surface)
    schema_revision = str(config.provider_tool_schema_revision or "").strip()
    graph_revision = canonical_graph_revision(config.graph_revision) if config.graph_revision else ""
    if not surface:
        raise ValueError("agent action_surface must be explicit")
    if not schema_revision:
        raise ValueError("agent provider_tool_schema_revision must be explicit")
    if not is_action_surface(surface):
        raise ValueError(
            f"unsupported action surface: {surface!r}; "
            f"expected {ACTION_SURFACE!r}"
        )
    if schema_revision != IPYTHON_SCHEMA_REVISION:
        raise ValueError(
            "runs require the pinned persistent-Python provider schema"
        )
    if int(config.max_output_tokens or 0) <= 0:
        raise ValueError("agent max_output_tokens must be positive")
    if int(config.max_consecutive_length_recoveries or 0) < 0:
        raise ValueError(
            "max_consecutive_length_recoveries must be non-negative"
        )
    expected = CHAT_GRAPH_REVISION if config.source == "chat" else WORKER_GRAPH_REVISION
    if graph_revision and graph_revision != expected:
        raise ValueError(
            "runs require a pinned chat or worker Python graph"
        )
