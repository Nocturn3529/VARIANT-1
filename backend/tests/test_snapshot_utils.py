"""latest_run_state_for_thread: thread-scoped snapshot lookup used by
automation resume detection (see server._prior_incomplete_automation_run).

This looks up one already-known thread_id, such as an automation's stable
``automation:{id}`` snapshot thread."""

from __future__ import annotations

import time

import pytest

from agent_engine.snapshot_utils import (
    latest_run_state_for_thread,
    load_main_chat_resume_state,
    mutation_elevation_blocked_by_threads,
    summarize_run_state_for_log,
    validate_run_state_for_resume,
)
from agent_engine.errors import DurableCheckpointUnavailable
from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
from session_catalog.profiles import IPYTHON_SCHEMA_REVISION


def _commit_snapshot(store, thread_id, state):
    source = str(state.get("source") or "chat")
    state = {
        "state_schema_version": 1,
        "graph_revision": "chat.ipython.v2" if source == "chat" else "worker.ipython.v2",
        "action_surface": "trusted-local.v1",
        "provider_tool_schema_revision": IPYTHON_SCHEMA_REVISION,
        "thread_id": thread_id,
        **state,
    }
    head = store.load_head_sync(thread_id)
    expected = head.cursor.sequence if head is not None else None
    return store.commit_boundary_sync(
        state,
        completed_node="init",
        next_node="prepare",
        expected_head_sequence=expected,
    )


def _store(tmp_path):
    return SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))


def test_log_summary_reads_main_loop_step():
    summary = summarize_run_state_for_log({
        "run_id": "run-1",
        "main": {"loop": {"step": 7}},
    })

    assert summary["step"] == 7


def test_returns_none_when_thread_has_no_snapshot(tmp_path):
    store = _store(tmp_path)
    assert latest_run_state_for_thread(store, "automation:missing") is None


def test_returns_none_for_empty_thread_id(tmp_path):
    store = _store(tmp_path)
    assert latest_run_state_for_thread(store, "") is None


def test_main_chat_scope_uses_thread_index_when_snapshot_has_no_chat_id(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "snapshots.sqlite3"
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))
    store = SQLiteRunSnapshotStore(str(db_path))
    now = time.time()

    def state(thread_id, task_id, goal, updated_at):
        return {
            "source": "chat",
            "state_schema_version": 1,
            "graph_revision": "chat.ipython.v2",
            "thread_id": thread_id,
            "run_id": task_id,
            "goal": goal,
            "status": "in_progress",
            "updated_at": updated_at,
            "task": {
                "task_id": task_id,
                "goal": goal,
                "status": "in_progress",
            },
        }

    _commit_snapshot(
        store,
        "owned-thread",
        state("owned-thread", "owned-task", "resume mine", now - 20),
    )
    _commit_snapshot(
        store,
        "foreign-thread",
        state("foreign-thread", "foreign-task", "not mine", now - 1),
    )

    restored, reason = load_main_chat_resume_state(
        current_chat_id="active-chat",
        thread_ids=("owned-thread",),
    )

    assert reason == ""
    assert restored is not None
    assert restored["thread_id"] == "owned-thread"
    assert restored["task"]["task_id"] == "owned-task"


def test_returns_the_newest_snapshot_for_the_thread(tmp_path):
    store = _store(tmp_path)
    thread_id = "automation:abc"
    _commit_snapshot(
        store, thread_id,
        {"source": "automation", "status": "running", "run_id": "run_1", "goal": "first"},
    )
    _commit_snapshot(
        store, thread_id,
        {"source": "automation", "status": "completed", "run_id": "run_1", "goal": "second"},
    )

    state = latest_run_state_for_thread(store, thread_id)

    assert state is not None
    assert state["goal"] == "second"
    assert state["status"] == "completed"


def test_is_scoped_to_its_own_thread_and_ignores_others(tmp_path):
    store = _store(tmp_path)
    _commit_snapshot(
        store, "automation:one",
        {"source": "automation", "status": "running", "run_id": "run_1", "goal": "thread one"},
    )
    _commit_snapshot(
        store, "automation:two",
        {"source": "automation", "status": "running", "run_id": "run_2", "goal": "thread two"},
    )

    state = latest_run_state_for_thread(store, "automation:one")

    assert state["goal"] == "thread one"


def test_snapshot_store_errors_fail_closed():
    class _BrokenSnapshotStore:
        def load_head_sync(self, thread_id):
            raise RuntimeError("db is locked")

    with pytest.raises(DurableCheckpointUnavailable, match="automation:x"):
        latest_run_state_for_thread(_BrokenSnapshotStore(), "automation:x")


def test_returns_none_for_empty_snapshot_head():
    class _EmptySnapshotStore:
        def load_head_sync(self, thread_id):
            return None

    assert latest_run_state_for_thread(_EmptySnapshotStore(), "automation:x") is None


def _resumable_state(*, model_name="", updated_at=None):
    return {
        "source": "chat",
        "graph_revision": "chat.ipython.v2",
        "action_surface": "trusted-local.v1",
        "provider_tool_schema_revision": IPYTHON_SCHEMA_REVISION,
        "state_schema_version": 1,
        "status": "running",
        "run_id": "run-1",
        "updated_at": time.time() if updated_at is None else updated_at,
        "task": {
            "task_id": "run-1",
            "goal": "finish the task",
            "status": "running",
            "model_name": model_name,
        },
    }


def test_resume_validation_rejects_model_change():
    state, reason = validate_run_state_for_resume(
        _resumable_state(model_name="old-model"),
        current_model="new-model",
    )
    assert state is None
    assert "model changed" in reason


def test_resume_validation_rejects_checkpoint_older_than_24_hours():
    state, reason = validate_run_state_for_resume(
        _resumable_state(updated_at=time.time() - 25 * 60 * 60),
    )
    assert state is None
    assert "older than 24h" in reason


def _pending_ipython_state(*, surface, mutation_enabled=None):
    state = _resumable_state()
    state["graph_revision"] = "chat.ipython.v2"
    state["action_surface"] = surface
    if mutation_enabled is not None:
        state["session_capabilities"] = {
            "mutation_write_enabled": mutation_enabled,
            "mutation_authority_revision": 3,
        }
    state["messages"] = [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "ipython",
                "arguments": '{"code":"toolbelt.propose_activate(...)"}',
            },
        }],
    }]
    state["main"] = {"loop": {
        "route": "tools",
        "actions": [{
            "id": "call_1",
            "tool": "ipython",
            "args": {"code": "toolbelt.propose_activate(...)"},
        }],
    }}
    return state


def test_resume_rejects_pending_tool_call_after_mutation_authority_elevation():
    state, reason = validate_run_state_for_resume(
        _pending_ipython_state(
            surface="trusted-local.v1",
            mutation_enabled=False,
        ),
        current_graph_revision="chat.ipython.v2",
        current_session_capabilities={
            "mutation_write_enabled": True,
            "mutation_authority_revision": 4,
        },
    )

    assert state is None
    assert "authority increased" in reason


def test_resume_allows_pending_tool_call_after_authority_downgrade():
    state, reason = validate_run_state_for_resume(
        _pending_ipython_state(
            surface="trusted-local.v1",
            mutation_enabled=True,
        ),
        current_graph_revision="chat.ipython.v2",
        current_session_capabilities={
            "mutation_write_enabled": False,
            "mutation_authority_revision": 4,
        },
    )

    assert reason == ""
    assert state["session_capabilities"]["mutation_write_enabled"] is True


def test_retired_checkpoint_identity_is_rejected():
    original = _pending_ipython_state(
        surface="astb-mutable.trusted-local.v1",
        mutation_enabled=None,
    )
    state, reason = validate_run_state_for_resume(
        original,
        current_graph_revision="chat.ipython.v2",
    )

    assert state is None
    assert "action surface is unsupported" in reason


def test_mutation_elevation_preflight_blocks_only_latest_incomplete_off_boundary(
    tmp_path,
):
    store = _store(tmp_path)
    blocked = _pending_ipython_state(
        surface="trusted-local.v1",
        mutation_enabled=False,
    )
    _commit_snapshot(
        store,
        "chat:blocked",
        blocked,
    )

    is_blocked, reason = mutation_elevation_blocked_by_threads(
        ["chat:blocked"], store
    )

    assert is_blocked is True
    assert "pending tool call saved with mutation writes off" in reason


def test_mutation_elevation_preflight_allows_completed_or_saved_on_boundaries(
    tmp_path,
):
    store = _store(tmp_path)
    completed = _pending_ipython_state(
        surface="trusted-local.v1",
        mutation_enabled=False,
    )
    completed["status"] = "completed"
    completed["task"]["status"] = "completed"
    saved_on = _pending_ipython_state(
        surface="trusted-local.v1",
        mutation_enabled=True,
    )
    _commit_snapshot(
        store,
        "chat:completed",
        completed,
    )
    _commit_snapshot(
        store,
        "chat:saved-on",
        saved_on,
    )

    assert mutation_elevation_blocked_by_threads(
        ["chat:completed", "chat:saved-on"], store
    ) == (False, "")


def test_mutation_elevation_preflight_fails_closed_on_snapshot_read_error():
    class BrokenSnapshotStore:
        def load_head_sync(self, _thread_id):
            raise OSError("snapshot database unavailable")

    is_blocked, reason = mutation_elevation_blocked_by_threads(
        ["chat:broken"], BrokenSnapshotStore()
    )

    assert is_blocked is True
    assert "snapshot database unavailable" in reason

