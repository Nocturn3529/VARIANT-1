"""run_subagent() wires isolated-but-related child desktop/browser sessions
into the delegated graph run."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server
from run_context import Variant1RunContext, bind_run_context
from session_catalog.child_worker import run_child_worker
from session_catalog.profiles import ACTION_SURFACE, IPYTHON_SCHEMA_REVISION
from types import SimpleNamespace


async def _run_subagent_with_mocks(*, graph_result=None):
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
                "summarize the logs",
                "paths in /tmp",
            )

    return result, captured_calls


@pytest.mark.asyncio
async def test_run_subagent_passes_child_desktop_and_browser_snapshots():
    _, calls = await _run_subagent_with_mocks()

    assert len(calls) == 1
    desktop_snapshot = calls[0]["desktop"]
    browser_snapshot = calls[0]["browser"]
    assert desktop_snapshot is not None
    assert browser_snapshot is not None
    assert desktop_snapshot["schema"] == "variant1.desktop-binding.v1"
    assert desktop_snapshot["owner_kind"] == "run"
    assert desktop_snapshot["active_window_id"] is None
    assert browser_snapshot["owner_kind"] == "run"
    assert browser_snapshot["fabric_session_id"] is None


@pytest.mark.asyncio
async def test_run_subagent_child_browser_inherits_parent_url():
    from browser_fabric import BrowserBinding, bind_browser_binding

    parent = BrowserBinding(
        binding_id="browser_binding_host",
        fabric_session_id="browser_fabric_host",
        owner_kind="chat",
        resume_url="https://example.com/docs",
    )

    with bind_browser_binding(parent):
        _, calls = await _run_subagent_with_mocks()

    browser_snapshot = calls[0]["browser"]
    assert browser_snapshot["resume_url"] == "https://example.com/docs"
    assert browser_snapshot["fabric_session_id"] is None
    assert browser_snapshot["binding_id"] != parent.binding_id
    assert browser_snapshot["parent_binding_id"] == parent.binding_id
