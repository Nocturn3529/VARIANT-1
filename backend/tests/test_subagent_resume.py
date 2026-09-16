"""Subagent durability: resume from durable checkpoint on same parent + sub-goal."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server
from agent_engine.snapshot_utils import subagent_thread_id
from run_context import Variant1RunContext, bind_run_context
from session_catalog.child_worker import run_child_worker
from session_catalog.profiles import ACTION_SURFACE, IPYTHON_SCHEMA_REVISION
from types import SimpleNamespace


async def _run_with_mocks(*, prior_run=None, graph_result=None):
    captured_calls: list = []

    async def fake_graph(**kwargs):
        captured_calls.append(kwargs)
        return graph_result or {
            "status": "completed",
            "output": {"mood": "neutral", "reply": "DONE: finished", "interrupted": False},
            "errors": [],
        }

    router = MagicMock()

    async def _stream(msgs, **kwargs):
        yield "DONE: finished"

    router.stream = _stream

    with (
        patch.object(server.APP, "router", router),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch("agent_engine.executor.execute_headless_worker", new=AsyncMock(side_effect=fake_graph)),
        patch("agent_engine.snapshot_utils.prior_incomplete_run_for_thread", return_value=prior_run),
    ):
        ctx = Variant1RunContext.create(
            source="subagent",
            chat_session=SimpleNamespace(interrupt=False, active=None),
            metadata={
                "_server_bound_kind": "subagent",
                "parent_thread_id": "",
                "runtime_identity": {
                    "action_surface": ACTION_SURFACE,
                    "provider_tool_schema_revision": IPYTHON_SCHEMA_REVISION,
                },
            },
        )
        with bind_run_context(ctx):
            result = await run_child_worker(
                server.APP.child_worker_ports(),
                "summarize logs",
                "paths in /tmp",
            )

    return result, captured_calls


@pytest.mark.asyncio
async def test_subagent_resumes_when_prior_checkpoint_exists():
    prior_run = {
        "run_id": "run_old",
        "goal": "summarize logs",
        "status": "running",
        "messages": [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}],
        "task": {"task_id": "run_old", "goal": "summarize logs", "status": "running"},
        "output": {},
        "step": 1,
    }
    _, calls = await _run_with_mocks(prior_run=prior_run)

    assert calls[0]["is_resume"] is True
    assert calls[0]["resume_snap"] is not None
    assert "INTERRUPTED PRIOR RUN" in calls[0]["resume_snap"]["messages"][0]["content"]
    assert calls[0]["thread_id"].startswith("subagent:")
    assert subagent_thread_id(
        parent_thread_id="",
        task="summarize logs",
        instructions="paths in /tmp",
    ) == calls[0]["thread_id"]


@pytest.mark.asyncio
async def test_subagent_starts_fresh_without_prior_checkpoint():
    _, calls = await _run_with_mocks(prior_run=None)

    assert calls[0]["is_resume"] is False
    assert calls[0]["resume_snap"] is None


@pytest.mark.asyncio
async def test_subagent_native_truncation_cannot_be_upgraded_by_done_text():
    result, _ = await _run_with_mocks(
        prior_run=None,
        graph_result={
            "status": "truncated",
            "output": {
                "mood": "concerned",
                "reply": "DONE: claimed success before the output limit",
                "interrupted": False,
                "completion_status": "truncated",
            },
            "errors": ["model output limit reached"],
        },
    )

    assert result.startswith("[child FAILED]")
    assert "partial output:" in result
    assert "claimed success before the output limit" in result
    assert "[child DONE]" not in result


def test_subagent_thread_id_is_stable_for_same_delegation():
    a = subagent_thread_id(parent_thread_id="chat-1", task="do thing", instructions="ctx")
    b = subagent_thread_id(parent_thread_id="chat-1", task="do thing", instructions="ctx")
    c = subagent_thread_id(parent_thread_id="chat-1", task="other", instructions="ctx")

    assert a == b
    assert a != c
    assert a.startswith("subagent:chat-1:")
