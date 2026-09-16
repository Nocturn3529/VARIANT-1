from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_engine.shared_ports import AgentContextPorts, HeadlessAgentPorts, ToolSurfacePorts
from session_catalog.profiles import ACTION_SURFACE, IPYTHON_SCHEMA_REVISION
from session_catalog.child_worker import ChildWorkerPorts, run_child_worker
from run_context import Variant1RunContext, bind_run_context


@pytest.mark.asyncio
async def test_admitted_astb_child_uses_one_ipython_surface_and_worker_revision():
    captured = {}

    async def graph(**kwargs):
        captured.update(kwargs)
        return {
            "status": "completed",
            "output": {"reply": "DONE: complete", "interrupted": False},
            "errors": [],
        }

    async def emit(*_args, **_kwargs):
        return None

    async def compress(messages, **_kwargs):
        return messages

    registry = MagicMock()
    registry.specs.side_effect = AssertionError(
        "child must not project the native registry"
    )
    agent = HeadlessAgentPorts(
        context=AgentContextPorts(
            approx_tokens=lambda _messages: 1,
            ctx_compress_threshold=lambda: 1000,
            compress_messages=compress,
        ),
        tools=ToolSurfacePorts(
            tools_prompt_block=lambda _enabled, specs: ",".join(
                row["name"] for row in specs
            ),
            run_actions_headless=AsyncMock(return_value=[]),
        ),
        make_run_context=MagicMock(),
    )
    session = SimpleNamespace(interrupt=False, active=SimpleNamespace(task=None))
    ports = ChildWorkerPorts(
        agent=agent, router=MagicMock(), emit=emit,
        new_run=lambda *_args: None,
        active_session=lambda: session,
        template_dirs=(".",),
    )
    ctx = Variant1RunContext.create(
        source="subagent",
        metadata={
            "_server_bound_kind": "subagent",
            "parent_thread_id": "parent-thread",
            "chat_id": "child-chat",
            "runtime_identity": {
                "action_surface": ACTION_SURFACE,
                "provider_tool_schema_revision": IPYTHON_SCHEMA_REVISION,
            },
            "runtime_prompt": "CHILD RUNTIME CARD",
        },
    )
    with (
        bind_run_context(ctx),
        patch("agent_engine.executor.execute_headless_worker", new=AsyncMock(side_effect=graph)),
        patch("agent_engine.snapshot_utils.prior_incomplete_run_for_thread", return_value=None),
    ):
        result = await run_child_worker(ports, "complete task", "context")

    assert "DONE" in result
    assert captured["config"].action_surface == ACTION_SURFACE
    assert captured["config"].graph_revision == "worker.ipython.v2"
    assert [row["name"] for row in captured["full_tspec"]] == ["ipython"]
    assert "CHILD RUNTIME CARD" in captured["messages"][0]["content"]
    assert ctx.run_config is captured["config"]

