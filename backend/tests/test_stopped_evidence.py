from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import json

import pytest

from agent_engine.state import new_run_state
from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
from agent_engine.snapshot_utils import is_incomplete_run_state, load_main_chat_follow_up_state, load_main_chat_resume_state
from artifacts.store import ContentAddressedArtifactStore
from capability_broker import capability_request_fingerprint
from core_invariants import canonical_digest
from kernel_runtime.cell_ledger import KernelCellLedgerStore
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
from session_projection import current_projection
from stopped_evidence import SCHEMA, accept_stopped_evidence, reconcile_call
from tests.support.conversation_sessions import open_sessions
from work_fabric.repository import WorkRepository
from work_fabric.scope import WorkScope


def fixture(tmp_path, status="in_progress"):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    snapshots = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")), snapshot_store=snapshots)
    registry.ensure_runtime(sid)
    state = new_run_state(source="chat", title="inspect", goal="inspect", thread_id="stopped-thread", run_id="stopped-run")
    action = {"id": "call-1", "tool": "ipython", "args": {"code": "print(3)"}}
    state.update(chat_id=sid, status=status)
    suffix = "\n\n---\n<variant1_current_context>\nCURRENT ONLY\n</variant1_current_context>"
    state["messages"] = [
        {"role": "system", "content": "OLD SYSTEM"},
        {
            "role": "user",
            "content": (
                "[Host kernel continuation]\ninspect runtime before claiming continuity\n\n"
                "inspect" + suffix
            ),
            "variant1_host_context_suffix_chars": len(suffix),
        },
        {"role": "assistant", "content": "visible progress", "thinking": "PRIVATE REASONING",
         "tool_calls": [{"id": "call-1", "type": "function", "function": {
             "name": "ipython", "arguments": json.dumps(action["args"])}}]},
    ]
    state["main"] = {"loop": {"route": "tools", "actions": [action]}}
    cursor = snapshots.commit_boundary_sync(state, completed_node="model_step", next_node="tool", expected_head_sequence=None)
    sessions.append_messages(sid, [{"role": "user", "text": "inspect"}, {"role": "assistant", "text": "Task stopped."}])
    sessions.set_last_run_receipt(sid, {"run_id": state["run_id"], "status": "cancelled", "settled": True})
    runtime = SimpleNamespace(sessions=sessions, session_runtimes=registry,
        kernel=SimpleNamespace(cell_ledger=KernelCellLedgerStore(str(tmp_path / "cells.sqlite3")),
                               artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts"))),
        work=SimpleNamespace(repository=WorkRepository(str(tmp_path / "work.sqlite3"))))
    return runtime, sid, snapshots.load_cursor_sync(cursor), action


def reserve(runtime, sid, action):
    fingerprint = capability_request_fingerprint(action["tool"], action["args"])
    runtime.session_runtimes.repository.reserve_outer_tool_call(chat_id=sid, run_id="stopped-run",
        call_id=action["id"], tool_name=action["tool"], request_fingerprint=fingerprint)
    return fingerprint


def cell(runtime, sid, *, corrupt=False):
    artifacts = runtime.kernel.artifact_store
    source = artifacts.put_text("print(3)", kind="source", scope=sid)
    result = artifacts.put_json({"execution_id": "cell-1", "chat_id": sid, "status": "ok", "text": "3"}, kind="result", scope=sid)
    return runtime.kernel.cell_ledger.append(execution_id="cell-1", chat_id=sid, run_id="stopped-run",
        outer_tool_call_id="call-1", kernel_generation=1, workspace_revision=0, workspace_fingerprint="",
        workspace_root_ids=(), work_scope={"chat_id": sid}, source_ref=source.ref, source_sha256=source.sha256,
        result_ref=result.ref, result_sha256="bad" if corrupt else result.sha256,
        status="ok", execution_count=1, started_at=1, completed_at=2, duration_ms=1)


@pytest.mark.parametrize("kind", ["none", "terminal", "dispatched", "cell", "corrupt", "orphan_cell", "unknown_work"])
def test_pending_call_truth_is_reconciled_without_dispatch(tmp_path, kind):
    runtime, sid, candidate, action = fixture(tmp_path)
    repo = runtime.session_runtimes.repository
    if kind not in {"none", "orphan_cell"}:
        fingerprint = reserve(runtime, sid, action)
    if kind == "terminal":
        outcome = {"tool": "ipython", "call_id": "call-1", "ok": True, "executed": True,
                   "result": "exact durable result", "model_result": "exact durable result", "status": "ok"}
        repo.finish_outer_tool_call(chat_id=sid, run_id="stopped-run", call_id="call-1",
            request_fingerprint=fingerprint, state="succeeded", outcome=outcome)
    if kind in {"cell", "corrupt", "orphan_cell", "unknown_work"}:
        cell(runtime, sid, corrupt=kind == "corrupt")
    if kind == "unknown_work":
        runtime.work.repository.record_operation_receipt(operation_id="operation-1", kind="fixture",
            status="unknown_effect", scope=WorkScope(chat_id=sid), request={"attribution": {
                "chat_id": sid, "run_id": "stopped-run", "outer_tool_call_id": "call-1"}})
    actual = reconcile_call(runtime, sid, candidate.run_id, action)
    if kind == "none":
        assert actual["executed"] is False and actual["status"] == "cancelled_before_start"
    elif kind == "terminal":
        assert actual == outcome
    elif kind == "cell":
        assert actual["executed"] and actual["ok"] and actual["model_result"] == "3"
        assert repo.get_outer_tool_call(sid, candidate.run_id, "call-1")["state"] == "succeeded"
    else:
        assert actual["executed"] and actual["status"] == "needs_reconciliation"
        assert "Not executed" not in actual["model_result"]


def test_accepted_stop_survives_reopen_route_change_and_preserves_suffix_once(tmp_path, monkeypatch):
    runtime, sid, candidate, action = fixture(tmp_path)
    reserve(runtime, sid, action)
    cell(runtime, sid)
    reference = accept_stopped_evidence(runtime, sid, candidate)
    assert reference and reference["sequence"] == 2
    assert reference["host_context_extents_revision"] == 1
    snapshots = runtime.session_runtimes.snapshot_store
    accepted = snapshots.load_head_sync(candidate.cursor.thread_id)
    assert accepted.status == "cancelled" and not is_incomplete_run_state(accepted.state)
    assert accepted.state["host_context_extents_revision"] == 1
    assert [
        row["content"] for row in accepted.state["messages"]
        if row.get("role") == "user"
    ] == ["inspect"]
    assert accept_stopped_evidence(runtime, sid, accepted) == reference
    assert snapshots.load_head_sync(candidate.cursor.thread_id).cursor.sequence == 2
    runtime.sessions.append_messages(sid, [{"role": "user", "text": "later"}, {"role": "assistant", "text": "later answer"}])
    reopened = open_sessions(tmp_path / "chats")
    projected, _ = current_projection(reopened, sid, snapshot_store=SQLiteRunSnapshotStore(snapshots.path),
                                     model_route={"provider": "different", "model": "different"})
    encoded = json.dumps(projected)
    assert "PRIVATE REASONING" not in encoded and "OLD SYSTEM" not in encoded
    assert [r["content"] for r in projected if r["role"] == "tool"] == ["3"]
    assert sum(r.get("content") == "inspect" for r in projected) == 1
    assert projected[-2:] == [{"role": "user", "content": "later"}, {"role": "assistant", "content": "later answer"}]
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", snapshots.path)
    assert load_main_chat_resume_state(current_chat_id=sid)[0] is None


def test_stopped_acceptance_rejects_invalid_extent_without_rewriting_snapshot(tmp_path):
    runtime, sid, candidate, action = fixture(tmp_path)
    reserve(runtime, sid, action)
    cell(runtime, sid)
    invalid = deepcopy(candidate.state)
    invalid["host_context_extents_revision"] = 1
    invalid["messages"][1]["variant1_host_context_prefix_chars"] = None
    cursor = runtime.session_runtimes.snapshot_store.commit_boundary_sync(
        invalid,
        completed_node="model_step",
        next_node="tool",
        expected_head_sequence=candidate.cursor.sequence,
    )
    invalid_candidate = runtime.session_runtimes.snapshot_store.load_cursor_sync(cursor)
    assert accept_stopped_evidence(runtime, sid, invalid_candidate) is None
    assert not runtime.sessions.get_context_projection(sid)
    head = runtime.session_runtimes.snapshot_store.load_head_sync(cursor.thread_id)
    assert head.cursor == cursor
    assert head.state["messages"][1]["variant1_host_context_prefix_chars"] is None


def test_already_accepted_legacy_stop_repairs_user_and_preserves_tool_receipt(tmp_path):
    runtime, sid, candidate, action = fixture(tmp_path)
    sessions = runtime.sessions
    store = runtime.session_runtimes.snapshot_store
    covered = sessions.get_stopped_context_coverage(sid, candidate.run_id)
    original = sessions.canonical_context(
        sid, head_node_id=covered["head_node_id"],
    )
    assert original["cursor"] == covered
    stop_reply = original["messages"][-1]
    source_user = candidate.state["messages"][1]
    suffix = source_user["variant1_host_context_suffix_chars"]
    legacy_user = source_user["content"][:-suffix]
    assistant_call = deepcopy(candidate.state["messages"][2])
    assistant_call.pop("thinking", None)
    messages = [
        {"role": "user", "content": legacy_user},
        assistant_call,
        {"role": "tool", "tool_call_id": action["id"], "content": "3"},
        stop_reply,
    ]
    marker = {
        "schema": SCHEMA,
        "source_snapshot": {
            **asdict(candidate.cursor),
            "run_id": candidate.run_id,
        },
        "canonical_coverage": covered,
        "settlement_sha256": canonical_digest(messages),
        "prior_projection_sha256": canonical_digest({}),
        "call_ids": [action["id"]],
    }
    legacy_state = deepcopy(candidate.state)
    legacy_state.update({
        "messages": messages,
        "status": "cancelled",
        "evidence_acceptance": marker,
        "updated_at": candidate.updated_at + 1,
    })
    legacy_state.pop("host_context_extents_revision", None)
    legacy_state["task"] = {
        **legacy_state.get("task", {}),
        "status": "cancelled",
    }
    legacy_state["output"] = {
        **legacy_state.get("output", {}),
        "snapshot_terminal_status": "cancelled",
        "completion_status": "cancelled",
        "transcript_committed": True,
    }
    cursor = store.commit_boundary_sync(
        legacy_state,
        completed_node="finalize",
        next_node="end",
        expected_head_sequence=candidate.cursor.sequence,
    )
    accepted = store.load_cursor_sync(cursor)
    immutable_digest = canonical_digest(accepted.state)
    reference = {**asdict(cursor), "run_id": accepted.run_id}
    assert sessions.set_native_context_projection(
        sid,
        reference,
        source_cursor=covered,
        model_route={},
        evidence=marker,
    )

    projected, _ = current_projection(
        sessions,
        sid,
        snapshot_store=store,
        model_route={"provider": "different", "model": "different"},
    )
    assert [row["content"] for row in projected if row["role"] == "user"] == [
        "inspect"
    ]
    assert [row["content"] for row in projected if row["role"] == "tool"] == [
        "3"
    ]
    assert "[Host kernel continuation]" not in json.dumps(projected)
    assert canonical_digest(store.load_cursor_sync(cursor).state) == immutable_digest


def test_preterminal_stopped_head_is_evidence_only(tmp_path, monkeypatch):
    runtime, sid, candidate, _ = fixture(tmp_path)
    receipt = runtime.sessions.get_last_run_receipt(sid)
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", runtime.session_runtimes.snapshot_store.path)
    assert load_main_chat_resume_state(current_chat_id=sid, last_run_receipt=receipt)[0] is None
    found, error = load_main_chat_follow_up_state(current_chat_id=sid, snapshot_store=runtime.session_runtimes.snapshot_store,
                                                last_run_receipt=receipt)
    assert found.cursor == candidate.cursor and not error


def test_retry_after_projection_failure_reuses_derived_snapshot(tmp_path, monkeypatch):
    runtime, sid, candidate, _ = fixture(tmp_path)
    setter = runtime.sessions.set_native_context_projection
    monkeypatch.setattr(runtime.sessions, "set_native_context_projection", lambda *a, **kw: False)
    assert accept_stopped_evidence(runtime, sid, candidate) is None
    head = runtime.session_runtimes.snapshot_store.load_head_sync(candidate.cursor.thread_id)
    assert head.cursor.sequence == 2
    monkeypatch.setattr(runtime.sessions, "set_native_context_projection", setter)
    assert accept_stopped_evidence(runtime, sid, head)["sequence"] == 2


def test_projection_race_does_not_overwrite_later_projection(tmp_path, monkeypatch):
    runtime, sid, candidate, _ = fixture(tmp_path)
    setter = runtime.sessions.set_native_context_projection
    def race(*args, **kwargs):
        context = runtime.sessions.canonical_context(sid)
        runtime.sessions.set_context_projection(sid, [{"role": "assistant", "content": "newer projection"}],
            source_message_count=len(context["messages"]), source_cursor=context["cursor"])
        return setter(*args, **kwargs)
    monkeypatch.setattr(runtime.sessions, "set_native_context_projection", race)
    assert accept_stopped_evidence(runtime, sid, candidate) is None
    head = runtime.session_runtimes.snapshot_store.load_head_sync(candidate.cursor.thread_id)
    monkeypatch.setattr(runtime.sessions, "set_native_context_projection", setter)
    assert accept_stopped_evidence(runtime, sid, head) is None
    assert runtime.sessions.get_context_projection(sid)["messages"][0]["content"] == "newer projection"


def test_mismatched_call_aborts_acceptance_without_changing_canonical_history(tmp_path):
    runtime, sid, candidate, action = fixture(tmp_path)
    reserve(runtime, sid, {**action, "args": {"code": "different()"}})
    before = runtime.sessions.recent_convo(sid, None)
    assert accept_stopped_evidence(runtime, sid, candidate) is None
    assert runtime.sessions.recent_convo(sid, None) == before
    assert runtime.sessions.get_context_projection(sid) == {}


def test_failed_evidence_read_keeps_canonical_history(tmp_path, monkeypatch):
    runtime, sid, candidate, _ = fixture(tmp_path)
    before = runtime.sessions.recent_convo(sid, None)
    def unavailable(*args):
        raise OSError("ledger unavailable")
    monkeypatch.setattr(runtime.kernel.cell_ledger, "for_outer_call", unavailable)
    assert accept_stopped_evidence(runtime, sid, candidate) is None
    assert runtime.sessions.get_context_projection(sid) == {}
    assert runtime.sessions.recent_convo(sid, None) == before
    assert runtime.session_runtimes.snapshot_store.load_head_sync(candidate.cursor.thread_id).cursor == candidate.cursor
