"""Small constructors for shared agent ports used by unit-test fakes."""

from __future__ import annotations

from agent_engine.shared_ports import (
    AgentContextPorts,
    HeadlessAgentPorts,
    ToolSurfacePorts,
)


def headless_agent_ports(
    *,
    tools_prompt_block,
    run_actions_headless,
    compress_messages,
    approx_tokens,
    ctx_compress_threshold,
    make_run_context,
    prepare_worker_surface=None,
) -> HeadlessAgentPorts:
    return HeadlessAgentPorts(
        context=AgentContextPorts(
            approx_tokens=approx_tokens,
            ctx_compress_threshold=ctx_compress_threshold,
            compress_messages=compress_messages,
        ),
        tools=ToolSurfacePorts(
            tools_prompt_block=tools_prompt_block,
            run_actions_headless=run_actions_headless,
        ),
        make_run_context=make_run_context,
        prepare_worker_surface=prepare_worker_surface,
    )
