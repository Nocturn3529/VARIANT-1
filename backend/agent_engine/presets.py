"""Run presets for VARIANT-1's chat and headless native execution paths."""

from __future__ import annotations

from session_catalog.profiles import (
    CHAT_GRAPH_REVISION,
    ACTION_SURFACE,
    IPYTHON_SCHEMA_REVISION,
    WORKER_GRAPH_REVISION,
)

from .config import AgentRunConfig


def chat_task_default() -> AgentRunConfig:
    # Interactive host work (files, coding, shell, web, desktop actions): tool results
    # return to the model and plain text ends the turn.
    return AgentRunConfig(
        name="chat_task_default",
        source="chat",
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
        graph_revision=CHAT_GRAPH_REVISION,
        checkpoints=True,
    )


def subagent_v1() -> AgentRunConfig:
    # Delegated workers inherit enough context to finish a small sub-goal and
    # report back. Durable per-step checkpoints let an interrupted subagent
    # resume on the same parent thread + sub-goal (see child_worker).
    return AgentRunConfig(
        name="subagent_v1",
        source="subagent",
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
        graph_revision=WORKER_GRAPH_REVISION,
        checkpoints=True,
        max_output_tokens=1024,
    )


def automation_v1() -> AgentRunConfig:
    # The base preset is neutral/ephemeral for generic callers. server.py enables
    # checkpoints by default for saved automations and only disables them when an
    # automation explicitly sets durable_checkpoints=false.
    return AgentRunConfig(
        name="automation_v1",
        source="automation",
        action_surface=ACTION_SURFACE,
        provider_tool_schema_revision=IPYTHON_SCHEMA_REVISION,
        graph_revision=WORKER_GRAPH_REVISION,
        checkpoints=False,
        max_output_tokens=1024,
    )

