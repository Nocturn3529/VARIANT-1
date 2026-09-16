"""Provider-boundary tests for headless worker projections."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from session_catalog.profiles import ACTION_SURFACE, IPYTHON_SCHEMA_REVISION
from session_catalog.service import IPYTHON_PROVIDER_SPEC
from automation.runner import AutomationPorts, execute_automation
from run_context import Variant1RunContext, current_run_context
from tests.support.agent_ports import headless_agent_ports
from tests.support.model_routes import RouteAwareRouter


def _agent(worker_source: str, *, observed_should_stop=None):
    async def compress(messages, *args, **kwargs):
        return messages

    async def run_actions(_actions, should_stop=None):
        if observed_should_stop is not None:
            observed_should_stop.append(should_stop)
        return SimpleNamespace(text="", outcomes=[], executed=False, had_error=False)

    def make_context(source, title, **kwargs):
        return Variant1RunContext.create(
            source=source,
            title=title,
            metadata=kwargs.get("metadata"),
        )

    agent = headless_agent_ports(
        tools_prompt_block=lambda names, specs: (
            "\nTOOLS: " + ", ".join(sorted(names)) if specs else ""
        ),
        run_actions_headless=run_actions,
        compress_messages=compress,
        approx_tokens=lambda messages: 100,
        ctx_compress_threshold=lambda: 10_000,
        make_run_context=make_context,
    )
    runtime_id = f"worker:{worker_source}:test"
    agent.prepare_worker_surface = MagicMock(return_value={
        "source": worker_source,
        "runtime_id": runtime_id,
        "action_surface": ACTION_SURFACE,
        "provider_tool_schema_revision": IPYTHON_SCHEMA_REVISION,
        "graph_revision": "worker.ipython.v2",
        "runtime_identity": {
            "chat_id": runtime_id,
            "action_surface": ACTION_SURFACE,
            "provider_tool_schema_revision": IPYTHON_SCHEMA_REVISION,
            "graph_revision": "worker.ipython.v2",
        },
        "runtime_prompt": "Use the admitted IPython namespace.",
        "provider_specs": [deepcopy(IPYTHON_PROVIDER_SPEC)],
        "mutation_enabled": False,
    })
    agent.begin_worker_run = AsyncMock(return_value="admit-worker")
    agent.finish_worker_run = MagicMock()
    return agent


class _History:
    def __init__(self):
        self.rows = []

    def add(self, *args):
        self.rows.append(args)

    def list(self, **kwargs):
        return []

    def count(self):
        return len(self.rows)


@pytest.mark.asyncio
async def test_automation_provider_request_exposes_exactly_one_ipython_action():
    observed_should_stop = []
    agent = _agent("automation", observed_should_stop=observed_should_stop)
    complete = AsyncMock(return_value={"text": "done"})
    captured = {}

    async def graph(**kwargs):
        captured.update(kwargs)
        await kwargs["stream_tools"](
            kwargs["messages"], 200, kwargs["full_tspec"]
        )
        await kwargs["run_actions"]([])
        return {
            "status": "completed",
            "output": {"mood": "neutral", "reply": "", "completion_status": "ok"},
            "errors": [],
        }

    async def no_op(*args, **kwargs):
        return None

    ports = AutomationPorts(
        agent=agent,
        router=RouteAwareRouter(mode="local"),
        hub=SimpleNamespace(broadcast=no_op),
        history=_History(),
        emit=no_op,
        new_run=lambda source, title: {"id": "run-1"},
        mem_query=AsyncMock(return_value=[]),
        build_system_prompt=lambda memories, note: "automation system",
        require_bound_run_context=lambda source, operation="": current_run_context(),
        prior_incomplete_run=lambda thread_id: None,
        notify_proactive=no_op,
    )
    task = {
        "id": "auto-1",
        "name": "Digest",
        "prompt": "Summarize the fixture",
        "durable_checkpoints": True,
        "trigger": {"type": "daily", "time": "09:00"},
    }

    with (
        patch("agent_engine.executor.execute_headless_worker", new=graph),
        patch("llm_router.complete_turn", new=complete),
    ):
        await execute_automation(ports, task)

    assert [row["name"] for row in captured["full_tspec"]] == ["ipython"]
    assert captured["config"].action_surface == ACTION_SURFACE
    assert captured["config"].graph_revision == "worker.ipython.v2"
    assert complete.await_args.kwargs["tools"] == [IPYTHON_PROVIDER_SPEC]
    assert len(observed_should_stop) == 1
    assert callable(observed_should_stop[0])
    assert observed_should_stop[0]() is False
    agent.begin_worker_run.assert_called_once()
    agent.finish_worker_run.assert_called_once_with(
        "admit-worker", status="completed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "native_status",
        "completion_status",
        "interrupted",
        "expected_status",
        "expected_surface_status",
    ),
    [
        ("truncated", "truncated", False, "truncated", "error"),
        ("failed", "error", False, "error", "error"),
        ("cancelled", "cancelled", True, "cancelled", "cancelled"),
    ],
)
async def test_automation_native_non_success_never_emits_or_delivers_success(
    native_status,
    completion_status,
    interrupted,
    expected_status,
    expected_surface_status,
):
    agent = _agent("automation")
    history = _History()
    events = []
    notify = AsyncMock()

    async def emit(event, **fields):
        events.append((event, fields))

    async def graph(**_kwargs):
        return {
            "status": native_status,
            "output": {
                "mood": "concerned",
                "reply": "DONE: claimed success before native completion",
                "completion_status": completion_status,
                "interrupted": interrupted,
            },
            "errors": ["native worker did not complete"],
        }

    async def no_op(*_args, **_kwargs):
        return None

    ports = AutomationPorts(
        agent=agent,
        router=RouteAwareRouter(mode="local"),
        hub=SimpleNamespace(broadcast=no_op),
        history=history,
        emit=emit,
        new_run=lambda source, title: {"id": "run-1"},
        mem_query=AsyncMock(return_value=[]),
        build_system_prompt=lambda memories, note: "automation system",
        require_bound_run_context=lambda source, operation="": current_run_context(),
        prior_incomplete_run=lambda thread_id: None,
        notify_proactive=notify,
    )
    task = {
        "id": "auto-outcome",
        "name": "Outcome probe",
        "prompt": "Complete the fixture",
        "durable_checkpoints": True,
        "trigger": {"type": "daily", "time": "09:00"},
    }

    with patch("agent_engine.executor.execute_headless_worker", new=graph):
        result = await execute_automation(ports, task)

    assert result["status"] == expected_status
    assert result["native_status"] == native_status
    assert result["reply"] == "DONE: claimed success before native completion"
    assert result["diagnostic"]
    assert result.get("partial") is (True if native_status == "truncated" else None)
    terminal_events = [fields for event, fields in events if event == "task:done"]
    assert terminal_events[-1]["status"] == expected_surface_status
    assert all(fields["status"] != "ok" for fields in terminal_events)
    assert history.rows[-1][3] == expected_surface_status
    notify.assert_not_awaited()
    agent.finish_worker_run.assert_called_once_with(
        "admit-worker", status=native_status
    )
