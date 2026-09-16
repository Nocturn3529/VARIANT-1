"""Versioned native run-state contract tests."""

from __future__ import annotations

from agent_engine.snapshot_utils import validate_run_state_for_resume
from agent_engine.run_contract import (
    CHAT_GRAPH_REVISION,
    RUN_STATE_SCHEMA_VERSION,
    WORKER_GRAPH_REVISION,
    validate_checkpoint_contract,
)
from agent_engine.state import new_run_state


def test_new_state_is_stamped_with_topology_and_schema_revision():
    chat = new_run_state(source="chat", title="chat", goal="g")
    worker = new_run_state(source="automation", title="worker", goal="g")
    assert chat["graph_revision"] == CHAT_GRAPH_REVISION
    assert worker["graph_revision"] == WORKER_GRAPH_REVISION
    assert chat["state_schema_version"] == RUN_STATE_SCHEMA_VERSION


def test_unversioned_native_checkpoint_is_rejected():
    state, error = validate_checkpoint_contract({
        "source": "chat",
        "tools": {"schema_mode": "searchable", "schema_specs": [{"name": "x"}]},
        "main": {"loop": {"route": "model_step"}},
    })
    assert state is None
    assert "no recognizable graph revision" in error


def test_retired_action_surface_is_rejected_without_compatibility_upgrade():
    raw = new_run_state(source="chat", title="chat", goal="g")
    raw["action_surface"] = "astb-static.trusted-local.v1"
    state, error = validate_checkpoint_contract(raw)
    assert state is None
    assert "action surface is unsupported" in error


def test_unknown_unversioned_checkpoint_is_rejected():
    state, error = validate_checkpoint_contract({"source": "chat"})
    assert state is None
    assert "no recognizable graph revision" in error


def test_wrong_graph_revision_is_rejected_before_resume():
    state = new_run_state(source="chat", title="chat", goal="g")
    state["graph_revision"] = "chat.unknown.v0"
    state["status"] = "running"
    state["task"] = {
        "task_id": state["run_id"],
        "goal": "g",
        "status": "in_progress",
    }
    restored, error = validate_run_state_for_resume(state)
    assert restored is None
    assert "graph revision changed" in error


def test_invalid_or_future_state_schema_is_rejected_not_relabelled():
    base = new_run_state(source="chat", title="chat", goal="g")

    invalid, invalid_error = validate_checkpoint_contract({
        **base,
        "state_schema_version": "not-a-version",
    })
    future, future_error = validate_checkpoint_contract({
        **base,
        "state_schema_version": RUN_STATE_SCHEMA_VERSION + 1,
    })

    assert invalid is None
    assert "invalid state schema version" in invalid_error
    assert future is None
    assert "newer than this runtime" in future_error


def test_native_state_contract_preserves_work_scope_projection():
    raw = new_run_state(
        source="chat",
        title="chat",
        goal="g",
        work_scope={"chat_id": "chat-1", "goal_id": "goal-1", "attempt": 2},
    )

    state, error = validate_checkpoint_contract(raw)

    assert error == ""
    assert state is not None
    assert state["work_scope"] == {
        "chat_id": "chat-1",
        "goal_id": "goal-1",
        "attempt": 2,
    }
