"""run_automation()'s interrupted-prior-run detection and resume.

Automations run with should_stop=lambda: False (no graceful-cancel path), so
an incomplete snapshot for an automation's thread always means the process
restarted mid-run. Since headless_worker_node was split into discrete state
machine nodes, that snapshot
can now carry real conversation progress -- not just the empty init-stage
state from before the split -- so a resumed automation continues its prior
messages/step count instead of only getting a "this was interrupted" note on
a fresh restart. This module covers the detection helper
(_prior_incomplete_automation_run) against a real durable snapshot DB, and
run_automation()'s wiring of that detection into automation history (which
otherwise silently drops crashed runs) and into an actual resume_snap passed
to the native headless worker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server
from host_runtime_testkit import patch_host_runtime

from agent_engine.errors import DurableCheckpointUnavailable
from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore


def _commit_snapshot(store, thread_id, state):
    state = {
        "graph_revision": "worker.ipython.v2",
        "action_surface": "trusted-local.v1",
        "provider_tool_schema_revision": "ipython.portable.v6",
        "state_schema_version": 1,
        "thread_id": thread_id,
        **state,
    }
    return store.commit_boundary_sync(
        state,
        completed_node="init",
        next_node="prepare",
        expected_head_sequence=None,
    )


# ---------------------------------------------------------------------------
# _prior_incomplete_automation_run: real SQLite snapshot DB, no mocking of
# the lookup path itself.
# ---------------------------------------------------------------------------


def test_prior_incomplete_automation_run_finds_a_dangling_running_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.sqlite3"
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))
    store = SQLiteRunSnapshotStore(str(db_path))
    _commit_snapshot(
        store,
        "automation:auto-1",
        {"source": "automation", "status": "running", "run_id": "run_1", "goal": "check inbox"},
    )

    prior = server.APP.require_runtime().workflows.prior_incomplete_automation_run(
        "automation:auto-1")

    assert prior is not None
    assert prior["status"] == "running"


def test_prior_incomplete_automation_run_ignores_a_completed_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.sqlite3"
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))
    store = SQLiteRunSnapshotStore(str(db_path))
    _commit_snapshot(
        store,
        "automation:auto-1",
        {"source": "automation", "status": "completed", "run_id": "run_1", "goal": "check inbox"},
    )

    assert server.APP.require_runtime().workflows.prior_incomplete_automation_run(
        "automation:auto-1") is None


def test_prior_incomplete_automation_run_returns_none_for_unknown_thread(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.sqlite3"
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))

    assert server.APP.require_runtime().workflows.prior_incomplete_automation_run(
        "automation:never-run") is None


def test_prior_incomplete_automation_run_returns_none_for_empty_thread_id():
    assert server.APP.require_runtime().workflows.prior_incomplete_automation_run("") is None
    assert server.APP.require_runtime().workflows.prior_incomplete_automation_run(None) is None


def test_prior_incomplete_automation_run_fails_closed_on_backend_failures(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("agent_engine.snapshot_utils.SQLiteRunSnapshotStore", _boom)

    with pytest.raises(DurableCheckpointUnavailable, match="automation:auto-1"):
        server.APP.require_runtime().workflows.prior_incomplete_automation_run(
            "automation:auto-1")


# ---------------------------------------------------------------------------
# run_automation(): wiring the detection into history + the next run's prompt.
# ---------------------------------------------------------------------------


def _task(**overrides):
    task = {
        "id": "auto-1",
        "name": "Morning digest",
        "prompt": "Summarize my inbox",
        "durable_checkpoints": True,
        "trigger": {"type": "daily", "time": "09:00"},
    }
    task.update(overrides)
    return task


def _mock_router():
    router = MagicMock()
    router.engine_ready = True
    router.mode = "local"
    return router


async def _run_automation_with_mocks(task, *, prior_run, run_result=None, run_side_effect=None):
    captured_calls: list = []

    async def fake_run(**kwargs):
        captured_calls.append(kwargs)
        if run_side_effect is not None:
            return await run_side_effect(**kwargs)
        return run_result or {
            "status": "completed",
            "output": {"mood": "neutral", "reply": "Done.", "completion_status": "ok"},
            "errors": [],
        }

    deliver = AsyncMock()
    prior_lookup = MagicMock(return_value=prior_run)
    with (
        patch_host_runtime(server.APP, workflows={
            "notify_proactive": deliver,
            "prior_incomplete_automation_run": prior_lookup,
        }),
        patch.object(server.APP, "router", _mock_router()),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch.object(
            server.APP.automations, "get",
            return_value={**task, "enabled": True},
        ),
        patch("agent_engine.executor.execute_headless_worker", new=AsyncMock(side_effect=fake_run)),
    ):
        runtime = server.APP.require_runtime()
        job_id = await runtime.workflows.run_automation(task)
        await runtime.work.jobs.wait(job_id, timeout_s=10)

    return captured_calls


@pytest.mark.asyncio
async def test_logs_interrupted_history_entry_for_a_crashed_prior_run():
    task = _task()
    prior_run = {
        "run_id": "run_old",
        "goal": "Summarize my inbox",
        "status": "running",
        "created_at": 1000.0,
        "updated_at": 1005.0,
        "task": {},
    }

    calls = await _run_automation_with_mocks(task, prior_run=prior_run)

    rows = server.APP.automation_history.list(automation_id="auto-1", limit=10)
    interrupted = [r for r in rows if r["status"] == "interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["started_at"] == 1000.0
    assert interrupted[0]["finished_at"] == 1005.0

    # The fresh attempt still ran normally afterward.
    assert len(calls) == 1
    ok_rows = [r for r in rows if r["status"] == "ok"]
    assert len(ok_rows) == 1


@pytest.mark.asyncio
async def test_resume_snap_notes_the_interruption_in_its_refreshed_system_prompt():
    # The note lives in resume_snap's messages (what headless_worker_prepare_node
    # actually uses when is_resume=True), not the fresh `messages` param -- that
    # fresh copy only serves as a fallback for a non-resumed run.
    task = _task()
    prior_run = {
        "run_id": "run_old",
        "goal": "Summarize my inbox",
        "status": "running",
        "created_at": 1000.0,
        "updated_at": 1005.0,
        "task": {},
    }

    calls = await _run_automation_with_mocks(task, prior_run=prior_run)

    assert calls[0]["is_resume"] is True
    resume_messages = calls[0]["resume_snap"]["messages"]
    assert resume_messages[0]["role"] == "system"
    assert "interrupted before finishing" in resume_messages[0]["content"]
    assert "continuing that run" in resume_messages[0]["content"]


@pytest.mark.asyncio
async def test_resume_snap_preserves_the_prior_runs_actual_conversation_and_step_count():
    # A crash between headless_worker_step_node and headless_worker_tool_node
    # (now real thanks to the node split) leaves a checkpoint with genuine
    # tool-call history, not just the empty init state. That history must
    # survive into resume_snap untouched -- only the system message (index 0)
    # gets refreshed.
    task = _task()
    prior_run = {
        "run_id": "run_old",
        "goal": "Summarize my inbox",
        "status": "running",
        "step": 2,
        "created_at": 1000.0,
        "updated_at": 1005.0,
        "task": {},
        "messages": [
            {"role": "system", "content": "stale system prompt from the interrupted run"},
            {"role": "user", "content": "Summarize my inbox"},
            {"role": "assistant", "content": '{"actions": [{"tool": "read_email", "args": {}}]}'},
            {"role": "user", "content": "Tool results:\n[read_email] 3 unread messages"},
        ],
    }

    calls = await _run_automation_with_mocks(task, prior_run=prior_run)

    resume_snap = calls[0]["resume_snap"]
    assert resume_snap["step"] == 2
    resume_messages = resume_snap["messages"]
    assert len(resume_messages) == 4
    # message 0 is refreshed (today's system prompt + note), not the stale one.
    assert "stale system prompt" not in resume_messages[0]["content"]
    # the rest of the actual prior conversation survives untouched.
    assert resume_messages[1] == {"role": "user", "content": "Summarize my inbox"}
    assert resume_messages[2]["content"] == '{"actions": [{"tool": "read_email", "args": {}}]}'
    assert "3 unread messages" in resume_messages[3]["content"]


@pytest.mark.asyncio
async def test_no_interrupted_entry_or_note_when_prior_run_is_absent():
    task = _task()

    calls = await _run_automation_with_mocks(task, prior_run=None)

    rows = server.APP.automation_history.list(automation_id="auto-1", limit=10)
    assert not [r for r in rows if r["status"] == "interrupted"]
    assert calls[0]["is_resume"] is False
    assert calls[0]["resume_snap"] is None
    messages = calls[0]["messages"]
    assert "interrupted before finishing" not in messages[0]["content"]


@pytest.mark.asyncio
async def test_skips_interruption_check_when_automation_is_not_durable():
    task = _task(durable_checkpoints=False)

    async def fake_run(**kwargs):
        return {
            "status": "completed",
            "output": {"mood": "neutral", "reply": "Done.", "completion_status": "ok"},
            "errors": [],
        }

    lookup = MagicMock()
    with (
        patch_host_runtime(server.APP, workflows={
            "notify_proactive": AsyncMock(),
            "prior_incomplete_automation_run": lookup,
        }),
        patch.object(server.APP, "router", _mock_router()),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch("agent_engine.executor.execute_headless_worker", new=AsyncMock(side_effect=fake_run)),
    ):
        runtime = server.APP.require_runtime()
        job_id = await runtime.workflows.run_automation(task)
        await runtime.work.jobs.wait(job_id, timeout_s=10)

    lookup.assert_not_called()
