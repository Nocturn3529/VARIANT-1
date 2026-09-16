"""Compacted checkpoints survive turn/restart boundaries without duplicating input."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sqlite3

import pytest

from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
from agent_engine.snapshot_store import SnapshotCursor
from agent_engine.state import new_run_state
from chat_context_stage import append_dynamic_memory_context
from message_context_extents import (
    HOST_CONTEXT_EXTENTS_REVISION,
    HOST_CONTEXT_PREFIX_KEY,
    HOST_CONTEXT_SUFFIX_KEY,
)
from model_runtime.message_graph import build_message_graph, render_openai_responses
from session_projection import (
    current_projection,
    project_session_conversation,
    promote_native_projection,
    repair_run_state_user_context,
)
from session_projection import projection_model_route
from tests.support.conversation_sessions import open_sessions
from transcript_economy import COMPACT_SUMMARY_MARKER

ROUTE = {"mode": "cloud", "provider": "openai-codex", "model": "gpt-5.6-luna"}
ROUTER = SimpleNamespace(bound_model_route=lambda: ROUTE)


@pytest.mark.asyncio
async def test_recursive_compaction_keeps_delivery_contract_after_snapshot_restart(tmp_path):
    from transcript_economy import compress_messages, COMPACT_SUMMARY_SENTINEL
    from model_runtime.message_graph import render_openai_chat, render_anthropic_messages

    requests = [
        'Write the deliverables to C:/requested/work, not the application directory.',
        'Change the output name to final.json and include only the east region.',
        'Finalize the retained result and verify the delivered files.',
    ]
    messages = [{"role": "system", "content": "host contract"}]

    async def lossy_recap(*_args, **_kwargs):
        return ("## Goal\nFinish work.\n## Confirmed Results\nPython state is live.\n"
                "## Pending Work\nWrite output.\n## Failed Attempts (not blockers)\nNone observed.\n"
                "## Exact Context\nCurrent working directory is C:/app.\n"
                + COMPACT_SUMMARY_SENTINEL)

    for turn, request in enumerate(requests):
        messages.append({"role": "user", "content": request})
        for step in range(8):
            call_id = f"call_{turn}_{step}"
            messages.extend([
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id, "type": "function", "function": {
                        "name": "ipython", "arguments": '{"code":"print(state)"}'}}]},
                {"role": "tool", "tool_call_id": call_id, "content": "state retained; cwd=C:/app"},
            ])
        before = deepcopy(messages)
        messages = await compress_messages(messages, complete=lossy_recap)
        assert messages != before
        assert [m['content'] for m in messages if m['role'] == 'user'] == requests[:turn + 1]

    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    run_id = "delivery-contract-run"
    receipt = sessions.append_messages(sid, [
        *[{"role": "user", "text": text, "transcript_id": run_id} for text in requests],
        {"role": "assistant", "text": "turn settled"},
    ])
    snapshots = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    state = new_run_state(source="chat", title="delivery", goal=requests[-1],
                          thread_id="delivery-thread", run_id=run_id)
    state.update(chat_id=sid, status="completed", messages=messages,
                 output={"transcript_committed": True, "reply": "turn settled"})
    cursor = snapshots.commit_boundary_sync(state, completed_node="finalize", next_node="end",
                                             expected_head_sequence=None)
    ref = {"thread_id": cursor.thread_id, "sequence": cursor.sequence,
           "snapshot_id": cursor.snapshot_id, "run_id": run_id}
    assert promote_native_projection(sessions, sid, ref, canonical_head=receipt["_canonical_head"],
                                      router=ROUTER, transcript_id=run_id)
    sessions = open_sessions(tmp_path / "chats")
    snapshots = SQLiteRunSnapshotStore(snapshots.path)
    restored, _ = current_projection(sessions, sid, snapshot_store=snapshots, model_route=ROUTE)
    assert [m['content'] for m in restored if m['role'] == 'user'] == requests
    graph = build_message_graph(restored)
    for wire in (render_openai_chat(graph), render_openai_responses(graph),
                 render_anthropic_messages(graph)):
        rendered = json.dumps(wire)
        for request in requests:
            assert request in rendered


def _fixture(tmp_path, *, promote=True, snapshot_status="completed", committed=True,
             unmatched=False, tagged=True, steering_text="steering message"):
    sessions = open_sessions(tmp_path / "chats", max_messages=2)
    sid = sessions.create_session()
    sessions.append_messages(sid, [{"role": "user", "text": "original task"},
                                   {"role": "assistant", "text": "earlier result"}])
    run_id = "completed-fixture-run"
    receipt = sessions.append_messages(sid, [
        {"role": "user", "text": "current task", "transcript_id": run_id},
        {"role": "user", "text": steering_text, "ticket_id": "steer-1"},
        {"role": "assistant", "text": "verified final result"},
    ])
    snapshots = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    state = new_run_state(source="chat", title="fixture", goal="current task",
                          thread_id="context-thread", run_id=run_id)
    state["chat_id"] = sid
    state["status"] = snapshot_status
    state["output"] = {"transcript_committed": committed, "reply": "verified final result"}
    augmented = append_dynamic_memory_context("current task", current_context="STALE_HOST_CONTEXT")
    legacy_prefix = (
        "[Host kernel continuation]\n"
        "Live CPython generation 1; state ready."
    )
    state["messages"] = [
        {"role": "system", "content": "OLD SYSTEM CONTRACT"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": COMPACT_SUMMARY_MARKER + "\nVerified recap",
         "variant1_compaction": tagged, "variant1_compaction_revision": 2},
        {"role": "user", "content": legacy_prefix + "\n\n" + augmented,
         "variant1_host_context_suffix_chars": len(augmented) - len("current task")},
        {"role": "user", "content": steering_text},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function",
         "function": {"name": "ipython", "arguments": '{"code":"print(persistent_total)"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "persistent_total=780"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]},
        {"role": "assistant", "content": "verified final result"},
    ]
    if unmatched:
        state["messages"] = [r for r in state["messages"] if r.get("role") != "tool"]
    cursor = snapshots.commit_boundary_sync(state, completed_node="finalize", next_node="end", expected_head_sequence=None)
    reference = {"thread_id": cursor.thread_id, "sequence": cursor.sequence,
                 "snapshot_id": cursor.snapshot_id, "run_id": run_id}
    if promote:
        assert promote_native_projection(sessions, sid, reference, canonical_head=receipt["_canonical_head"],
                                         router=ROUTER, transcript_id=run_id)
    return sessions, sid, snapshots, reference, receipt, state


@pytest.mark.asyncio
async def test_restart_reuses_exact_compacted_checkpoint_and_appends_only_canonical_suffix(tmp_path):
    sessions, sid, snapshots, ref, _, original_state = _fixture(tmp_path)
    # A subsequent canonical exchange is a suffix, not part of this checkpoint.
    sessions.append_messages(sid, [{"role": "user", "text": "later task"},
                                   {"role": "assistant", "text": "later answer"}])
    original_canonical = sessions.recent_convo(sid, None)
    assert len(original_canonical) == 7  # view is deliberately limited to two rows
    assert len(sessions.get_session(sid)["messages"]) == 2
    sessions = open_sessions(tmp_path / "chats", max_messages=2)
    snapshots = SQLiteRunSnapshotStore(snapshots.path)
    attempts = []

    async def must_not_summarize(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("the accepted checkpoint should be reused")

    result = await project_session_conversation(
        sessions, sid, mode="cloud", context_limit_tokens=272_000,
        snapshot_store=snapshots, model_route=ROUTE, compress_messages=must_not_summarize,
    )
    assert not attempts
    assert result[-2:] == original_canonical[-2:]
    assert sum(r.get("content") == "current task" for r in result) == 1
    assert sum(r.get("content") == "steering message" for r in result) == 1
    assert "persistent_total=780" in json.dumps(result)
    assert "[Host kernel continuation]" not in json.dumps(result)
    assert "STALE_HOST_CONTEXT" not in json.dumps(result)
    assert "OLD SYSTEM CONTRACT" not in json.dumps(result)
    assert "image_url" not in json.dumps(result)
    assert all(r.get("content") or r.get("tool_calls") or r.get("role") == "tool" for r in result)
    graph = build_message_graph(result)
    _, wire, images = render_openai_responses(graph)
    assert not images and any(r["type"] == "function_call_output" for r in wire)
    assert sessions.recent_convo(sid, None) == original_canonical
    stored = await snapshots.load_cursor(SnapshotCursor(
        ref["thread_id"], ref["sequence"], ref["snapshot_id"]))
    assert "STALE_HOST_CONTEXT" in json.dumps(stored.state)  # only disposable projection was stripped


def test_reference_does_not_follow_a_newer_mutable_snapshot_head(tmp_path):
    sessions, sid, snapshots, ref, _, state = _fixture(tmp_path)
    later = deepcopy(state)
    later["run_id"] = "a-newer-run"
    later["messages"] = [{"role": "user", "content": "SHOULD NOT REPLACE OLD CURSOR"}]
    snapshots.commit_boundary_sync(later, completed_node="finalize", next_node="end", expected_head_sequence=ref["sequence"])
    result, count = current_projection(sessions, sid, snapshot_store=snapshots, model_route=ROUTE)
    assert "persistent_total=780" in json.dumps(result)
    assert "SHOULD NOT REPLACE" not in json.dumps(result)
    assert count == 5


@pytest.mark.parametrize("invalid", ["missing", "changed_prefix", "rewind", "corrupt_snapshot", "wrong_model", "wrong_chat", "uncommitted", "unmatched", "untagged"])
def test_invalid_reference_falls_back_to_unmodified_canonical_history(tmp_path, invalid):
    sessions, sid, snapshots, _, _, _ = _fixture(
        tmp_path, committed=invalid != "uncommitted", unmatched=invalid == "unmatched", tagged=invalid != "untagged")
    route = ROUTE
    if invalid == "missing":
        snapshots.delete_thread_sync("context-thread")
    elif invalid == "changed_prefix":
        with sessions.repository._write() as conn:
            # Fault injection bypasses the normal immutable-node trigger in
            # this isolated fixture to test independent coverage verification.
            conn.execute("DROP TRIGGER trg_conversation_node_no_update")
            conn.execute("UPDATE conversation_node SET content_json=? WHERE role='user' AND content_json=?",
                         (json.dumps("edited original task"), json.dumps("original task")))
    elif invalid == "rewind":
        with sessions.repository._write() as conn:
            first = conn.execute("SELECT node_id FROM conversation_node ORDER BY created_at LIMIT 1").fetchone()[0]
            conn.execute("UPDATE conversation_branch SET head_node_id=? WHERE runtime_chat_id=?", (first, sid))
    elif invalid == "corrupt_snapshot":
        with sqlite3.connect(snapshots.path) as conn:
            conn.execute("UPDATE agent_snapshots SET payload_sha256=?", ("wrong-checksum",))
    elif invalid == "wrong_model":
        route = {**ROUTE, "model": "different-model"}
    elif invalid == "wrong_chat":
        other = sessions.create_session()
        old = sessions.get_context_projection(sid)
        with sessions.repository._write() as conn:
            conn.execute("UPDATE conversation_session_state SET state_json=? WHERE runtime_chat_id=?",
                         (json.dumps({"context_projection": old}), other))
        sid = other
    result, _ = current_projection(sessions, sid, snapshot_store=snapshots, model_route=route)
    assert result == sessions.recent_convo(sid, None)


@pytest.mark.parametrize("native", [True, False])
def test_legacy_compaction_rebuilds_missing_user_turns_from_canonical(tmp_path, native):
    sessions, sid, snapshots, reference, _, state = _fixture(tmp_path)
    legacy = [deepcopy(m) for m in state["messages"] if m.get("content") != "steering message"]
    for message in legacy:
        message.pop("variant1_compaction_revision", None)
    if native:
        state["messages"] = legacy
        cursor = snapshots.commit_boundary_sync(state, completed_node="finalize", next_node="end",
                                                  expected_head_sequence=reference["sequence"])
        reference.update(sequence=cursor.sequence, snapshot_id=cursor.snapshot_id)
        assert sessions.set_native_context_projection(sid, reference,
            source_cursor=sessions.canonical_context(sid)["cursor"], model_route=ROUTE)
    else:
        assert sessions.set_context_projection(sid,
            [m for m in legacy if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)],
            source_message_count=5, source_cursor=sessions.canonical_context(sid)["cursor"])
    expected = sessions.recent_convo(sid, None)
    restored, count = current_projection(sessions, sid, snapshot_store=snapshots, model_route=ROUTE)
    assert restored == expected and count == 5
    assert {"role": "user", "content": "steering message"} in restored


def test_text_projection_roundtrip_retains_user_preservation_revision(tmp_path):
    sessions, sid, _, _, _, _ = _fixture(tmp_path)
    messages = [{"role": "user", "content": "original task"},
                {"role": "assistant", "content": COMPACT_SUMMARY_MARKER + "\nrecap",
                 "variant1_compaction": True, "variant1_compaction_revision": 2},
                {"role": "user", "content": "current task"},
                {"role": "user", "content": "steering message"}]
    assert sessions.set_context_projection(sid, messages, source_message_count=5,
                                          source_cursor=sessions.canonical_context(sid)["cursor"])
    sessions = open_sessions(tmp_path / "chats")
    assert sessions.get_context_projection(sid)["messages"] == messages
    assert current_projection(sessions, sid)[0] == messages


@pytest.mark.asyncio
async def test_legacy_native_like_text_cache_repairs_users_after_restart_and_appends_error_once(
    tmp_path,
):
    from transcript_economy import COMPACT_SUMMARY_SENTINEL, compress_messages

    store_path = tmp_path / "chats"
    sessions = open_sessions(store_path)
    sid = sessions.create_session()
    canonical_rows = []
    for index in range(9):
        canonical_rows.extend([
            {"role": "user", "text": f"task {index}"},
            {"role": "assistant", "text": f"result {index}"},
        ])
    sessions.append_messages(sid, canonical_rows)
    canonical_before = sessions.recent_convo(sid, None)

    legacy_input = deepcopy(canonical_before)
    legacy_input[0]["content"] = (
        "[Host kernel continuation]\nSTALE_GENERATION_AND_POLICY\n\n"
        + legacy_input[0]["content"]
    )

    async def complete_recap(*_args, **_kwargs):
        return (
            "## Goal\nComplete the retained tasks.\n"
            "## Confirmed Results\nEarlier results were recorded.\n"
            "## Pending Work\nContinue from canonical user requests.\n"
            "## Failed Attempts (not blockers)\nNone observed.\n"
            "## Exact Context\nThe durable conversation store is authoritative.\n"
            + COMPACT_SUMMARY_SENTINEL
        )

    compacted = await compress_messages(
        legacy_input,
        complete=complete_recap,
        protect_first=0,
        protect_last=8,
    )
    assert len(compacted) < len(legacy_input)
    assert "STALE_GENERATION_AND_POLICY" in json.dumps(compacted)
    recap = next(row for row in compacted if row.get("variant1_compaction") is True)
    recap_identity = {
        "role": recap["role"],
        "content": recap["content"],
        "variant1_compaction": recap["variant1_compaction"],
        "variant1_compaction_revision": recap["variant1_compaction_revision"],
    }
    canonical_context = sessions.canonical_context(sid)
    assert sessions.set_context_projection(
        sid,
        compacted,
        source_message_count=len(canonical_before),
        source_cursor=canonical_context["cursor"],
    )
    stored_before = sessions.get_context_projection(sid)
    assert "STALE_GENERATION_AND_POLICY" in json.dumps(stored_before)

    sessions = open_sessions(store_path)
    sessions.append_messages(sid, [
        {"role": "user", "text": "retry after provider error"},
        {"role": "assistant", "text": "Provider request failed: rate limited"},
    ])
    canonical_with_error = sessions.recent_convo(sid, None)
    result, count = current_projection(sessions, sid)

    assert count == len(canonical_with_error)
    assert "[Host kernel continuation]" not in json.dumps(result)
    assert "STALE_GENERATION_AND_POLICY" not in json.dumps(result)
    assert [row["content"] for row in result if row.get("role") == "user"] == [
        *(f"task {index}" for index in range(9)),
        "retry after provider error",
    ]
    assert sum(
        row.get("role") == "user" and row.get("content") == "retry after provider error"
        for row in result
    ) == 1
    assert sum(
        row.get("role") == "assistant"
        and row.get("content") == "Provider request failed: rate limited"
        for row in result
    ) == 1
    repaired_recap = next(
        row for row in result if row.get("variant1_compaction") is True
    )
    assert {key: repaired_recap[key] for key in recap_identity} == recap_identity
    assert sessions.get_context_projection(sid) == stored_before
    assert sessions.recent_convo(sid, None) == canonical_with_error
    assert canonical_with_error[:len(canonical_before)] == canonical_before
    build_message_graph(result)


def test_clean_text_cache_preserves_true_user_marker_and_cache_bytes(tmp_path):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    marker = "[Host kernel continuation]\nThis is literal user-authored text."
    sessions.append_messages(sid, [
        {"role": "user", "text": marker},
        {"role": "assistant", "text": "recorded"},
    ])
    projection = [
        {"role": "user", "content": marker},
        {
            "role": "assistant",
            "content": COMPACT_SUMMARY_MARKER + "\nexact recap",
            "variant1_compaction": True,
            "variant1_compaction_revision": 2,
        },
    ]
    assert sessions.set_context_projection(
        sid,
        projection,
        source_message_count=2,
        source_cursor=sessions.canonical_context(sid)["cursor"],
    )
    stored_before = sessions.get_context_projection(sid)
    result, _ = current_projection(sessions, sid)
    assert result == projection
    assert result[0]["content"] == marker
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == projection[1]["content"]
    assert sessions.get_context_projection(sid) == stored_before


@pytest.mark.parametrize("projected_users", [[], ["wrong user binding"]])
def test_text_cache_user_mismatch_falls_back_and_cas_clears(
    tmp_path, projected_users,
):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    sessions.append_messages(sid, [
        {"role": "user", "text": "canonical request"},
        {"role": "assistant", "text": "canonical answer"},
    ])
    projection = [
        *({"role": "user", "content": content} for content in projected_users),
        {
            "role": "assistant",
            "content": COMPACT_SUMMARY_MARKER + "\nrecap",
            "variant1_compaction": True,
            "variant1_compaction_revision": 2,
        },
    ]
    assert sessions.set_context_projection(
        sid,
        projection,
        source_message_count=2,
        source_cursor=sessions.canonical_context(sid)["cursor"],
    )
    expected = sessions.recent_convo(sid, None)
    result, _ = current_projection(sessions, sid)
    assert result == expected
    assert not sessions.get_context_projection(sid)


def test_invalid_text_cache_clear_is_compare_and_swap_safe(tmp_path, monkeypatch):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    sessions.append_messages(sid, [
        {"role": "user", "text": "canonical request"},
        {"role": "assistant", "text": "canonical answer"},
    ])
    cursor = sessions.canonical_context(sid)["cursor"]
    invalid = [
        {"role": "user", "content": "wrong user binding"},
        {"role": "assistant", "content": COMPACT_SUMMARY_MARKER + "\nold recap",
         "variant1_compaction": True, "variant1_compaction_revision": 2},
    ]
    replacement = [
        {"role": "user", "content": "canonical request"},
        {"role": "assistant", "content": COMPACT_SUMMARY_MARKER + "\nnew recap",
         "variant1_compaction": True, "variant1_compaction_revision": 2},
    ]
    assert sessions.set_context_projection(
        sid, invalid, source_message_count=2, source_cursor=cursor,
    )
    original_clear = sessions.clear_context_projection
    raced = []

    def race_clear(chat_id, *, expected):
        raced.append(expected)
        assert sessions.set_context_projection(
            sid, replacement, source_message_count=2, source_cursor=cursor,
        )
        return original_clear(chat_id, expected=expected)

    monkeypatch.setattr(sessions, "clear_context_projection", race_clear)
    expected = sessions.recent_convo(sid, None)
    result, _ = current_projection(sessions, sid)
    assert result == expected
    assert len(raced) == 1
    assert sessions.get_context_projection(sid)["messages"] == replacement


def test_canonical_advance_during_commit_handshake_refuses_stale_promotion(tmp_path):
    sessions, sid, _, reference, receipt, _ = _fixture(tmp_path, promote=False)
    sessions.append_messages(sid, [{"role": "user", "text": "unseen later input"}])
    assert not promote_native_projection(sessions, sid, reference,
        canonical_head=receipt["_canonical_head"], router=ROUTER, transcript_id=reference["run_id"])
    assert not sessions.get_context_projection(sid)
    assert sessions.recent_convo(sid, None)[-1]["content"] == "unseen later input"


def test_transcript_idempotency_does_not_advertise_a_later_head_as_old_coverage(tmp_path):
    sessions, sid, _, reference, _, _ = _fixture(tmp_path, promote=False)
    sessions.append_messages(sid, [{"role": "user", "text": "new task"}, {"role": "assistant", "text": "new answer"}])
    before = sessions.recent_convo(sid, None)
    repeated = sessions.append_messages(sid, [{"role": "user", "text": "current task", "transcript_id": reference["run_id"]},
                                            {"role": "assistant", "text": "verified final result"}])
    assert "_canonical_head" not in repeated
    assert sessions.recent_convo(sid, None) == before


def test_plain_projection_validates_prefix_instead_of_only_message_count(tmp_path):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    sessions.append_messages(sid, [{"role": "user", "text": "first"}, {"role": "assistant", "text": "second"}])
    assert sessions.set_context_projection(sid, [{"role": "assistant", "content": "recap"}], source_message_count=2)
    with sessions.repository._write() as conn:
        conn.execute("DROP TRIGGER trg_conversation_node_no_update")
        conn.execute("UPDATE conversation_node SET content_json=? WHERE role='user'", (json.dumps("edited"),))
    result, _ = current_projection(sessions, sid)
    assert result == sessions.recent_convo(sid, None)


@pytest.mark.asyncio
async def test_healthy_projection_scans_history_once_and_does_not_decode_old_canonical_text(tmp_path, monkeypatch):
    import chat_sessions.service as service_module

    sessions, sid, snapshots, _, _, _ = _fixture(tmp_path)
    visits, decoded = [], []
    original_rows = service_module.history_rows
    original_decode = sessions._projection_messages
    def rows(*args):
        visits.append(True)
        return original_rows(*args)
    def decode(data):
        decoded.append(len(data))
        return original_decode(data)
    monkeypatch.setattr(service_module, "history_rows", rows)
    monkeypatch.setattr(sessions, "_projection_messages", decode)
    result = await project_session_conversation(sessions, sid, mode="cloud", context_limit_tokens=272_000,
                                               snapshot_store=snapshots, model_route=ROUTE)
    assert any(r.get("variant1_compaction") for r in result)
    assert len(visits) == 1 and decoded == [0]


def test_missing_reference_is_cleared_but_model_mismatch_preserves_it(tmp_path):
    sessions, sid, snapshots, _, _, _ = _fixture(tmp_path)
    current_projection(sessions, sid, snapshot_store=snapshots, model_route={**ROUTE, "model": "other"})
    assert sessions.get_context_projection(sid)["kind"] == "native_snapshot"
    snapshots.delete_thread_sync("context-thread")
    current_projection(sessions, sid, snapshot_store=snapshots, model_route=ROUTE)
    assert not sessions.get_context_projection(sid)


def test_endpoint_change_invalidates_replay_without_destroying_reference(tmp_path):
    from model_providers.base import ProviderProfile

    sessions, sid, snapshots, reference, receipt, _ = _fixture(tmp_path, promote=False)
    endpoint = ["https://first.example/v1"]
    router = SimpleNamespace(bound_model_route=lambda: ROUTE, cfg={"cloud": {}},
        provider_profile=lambda _name: ProviderProfile("openai-codex", "test"),
        provider_base_url=lambda _name: endpoint[0], oauth_account_id=lambda _name: "account")
    assert promote_native_projection(sessions, sid, reference, canonical_head=receipt["_canonical_head"],
                                     router=router, transcript_id=reference["run_id"])
    route = projection_model_route(router, sessions, sid)
    result, _ = current_projection(sessions, sid, snapshot_store=snapshots, model_route=route)
    assert any(r.get("variant1_compaction") for r in result)
    endpoint[0] = "https://second.example/v1"
    other = projection_model_route(router, sessions, sid)
    assert route["wire_identity"] != other["wire_identity"]
    result, _ = current_projection(sessions, sid, snapshot_store=snapshots, model_route=other)
    assert result == sessions.recent_convo(sid, None)
    assert sessions.get_context_projection(sid)["kind"] == "native_snapshot"


def test_user_authored_context_lookalike_is_preserved_without_host_extent(tmp_path):
    quoted = "User-authored example\n\n---\nDifferent preface\n<variant1_current_context>\nKEEP ME\n</variant1_current_context>"
    sessions, sid, snapshots, _, _, _ = _fixture(
        tmp_path, steering_text=quoted,
    )
    result, _ = current_projection(sessions, sid, snapshot_store=snapshots, model_route=ROUTE)
    assert {"role": "user", "content": quoted} in result


def test_legacy_equal_user_count_with_wrong_content_falls_back_to_canonical(tmp_path):
    sessions, sid, snapshots, reference, _, state = _fixture(tmp_path)
    state["messages"][4] = {"role": "user", "content": "wrong user binding"}
    later = snapshots.commit_boundary_sync(
        state,
        completed_node="finalize",
        next_node="end",
        expected_head_sequence=reference["sequence"],
    )
    reference.update(sequence=later.sequence, snapshot_id=later.snapshot_id)
    assert sessions.set_native_context_projection(
        sid,
        reference,
        source_cursor=sessions.canonical_context(sid)["cursor"],
        model_route=ROUTE,
    )
    expected = sessions.recent_convo(sid, None)
    result, _ = current_projection(
        sessions, sid, snapshot_store=snapshots, model_route=ROUTE,
    )
    assert result == expected
    assert not sessions.get_context_projection(sid)


def test_extent_aware_projection_strips_exact_prefix_and_suffix(tmp_path):
    sessions, sid, snapshots, reference, receipt, state = _fixture(
        tmp_path, promote=False,
    )
    current = state["messages"][3]
    current[HOST_CONTEXT_PREFIX_KEY] = current["content"].index("current task")
    state["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
    later = snapshots.commit_boundary_sync(
        state,
        completed_node="finalize",
        next_node="end",
        expected_head_sequence=reference["sequence"],
    )
    reference.update(
        sequence=later.sequence,
        snapshot_id=later.snapshot_id,
        host_context_extents_revision=HOST_CONTEXT_EXTENTS_REVISION,
    )
    assert promote_native_projection(
        sessions,
        sid,
        reference,
        canonical_head=receipt["_canonical_head"],
        router=ROUTER,
        transcript_id=reference["run_id"],
    )
    stored = sessions.get_context_projection(sid)
    assert stored["host_context_extents_revision"] == HOST_CONTEXT_EXTENTS_REVISION
    assert stored["snapshot"]["host_context_extents_revision"] == HOST_CONTEXT_EXTENTS_REVISION
    result, _ = current_projection(
        sessions, sid, snapshot_store=snapshots, model_route=ROUTE,
    )
    assert sum(row.get("content") == "current task" for row in result) == 1
    assert "[Host kernel continuation]" not in json.dumps(result)
    assert "STALE_HOST_CONTEXT" not in json.dumps(result)
    assert [row["content"] for row in result if row.get("role") == "tool"] == [
        "persistent_total=780"
    ]
    build_message_graph(result)


def test_extent_revision_cannot_be_fabricated_for_a_legacy_snapshot(tmp_path):
    sessions, sid, snapshots, reference, receipt, _state = _fixture(
        tmp_path, promote=False,
    )
    reference["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
    assert promote_native_projection(
        sessions,
        sid,
        reference,
        canonical_head=receipt["_canonical_head"],
        router=ROUTER,
        transcript_id=reference["run_id"],
    )
    expected = sessions.recent_convo(sid, None)
    result, _ = current_projection(
        sessions, sid, snapshot_store=snapshots, model_route=ROUTE,
    )
    assert result == expected
    assert not sessions.get_context_projection(sid)


def test_two_terminal_turns_and_restart_do_not_accumulate_host_context(tmp_path):
    sessions, sid, snapshots, reference, receipt, state = _fixture(
        tmp_path, promote=False,
    )
    first_current = state["messages"][3]
    first_current[HOST_CONTEXT_PREFIX_KEY] = first_current["content"].index(
        "current task"
    )
    state["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
    first_cursor = snapshots.commit_boundary_sync(
        state,
        completed_node="finalize",
        next_node="end",
        expected_head_sequence=reference["sequence"],
    )
    reference.update(
        sequence=first_cursor.sequence,
        snapshot_id=first_cursor.snapshot_id,
        host_context_extents_revision=HOST_CONTEXT_EXTENTS_REVISION,
    )
    assert promote_native_projection(
        sessions,
        sid,
        reference,
        canonical_head=receipt["_canonical_head"],
        router=ROUTER,
        transcript_id=reference["run_id"],
    )

    sessions = open_sessions(tmp_path / "chats")
    snapshots = SQLiteRunSnapshotStore(snapshots.path)
    first_history, _ = current_projection(
        sessions, sid, snapshot_store=snapshots, model_route=ROUTE,
    )
    assert "[Host kernel continuation]" not in json.dumps(first_history)

    second_run = "second-terminal-run"
    second_receipt = sessions.append_messages(sid, [
        {"role": "user", "text": "second task", "transcript_id": second_run},
        {"role": "assistant", "text": "second result"},
    ])
    second_prefix = "[Host kernel continuation]\nsecond-turn facts\n\n"
    second_suffix = (
        "\n\n---\n<variant1_current_context>\n"
        "SECOND TURN ONLY\n</variant1_current_context>"
    )
    second_state = new_run_state(
        source="chat",
        title="second task",
        goal="second task",
        thread_id="second-context-thread",
        run_id=second_run,
    )
    second_state.update({
        "chat_id": sid,
        "status": "completed",
        "host_context_extents_revision": HOST_CONTEXT_EXTENTS_REVISION,
        "messages": [
            {"role": "system", "content": "old system"},
            *first_history,
            {
                "role": "user",
                "content": second_prefix + "second task" + second_suffix,
                HOST_CONTEXT_PREFIX_KEY: len(second_prefix),
                HOST_CONTEXT_SUFFIX_KEY: len(second_suffix),
            },
            {"role": "assistant", "content": "second result"},
        ],
        "output": {"transcript_committed": True, "reply": "second result"},
    })
    second_cursor = snapshots.commit_boundary_sync(
        second_state,
        completed_node="finalize",
        next_node="end",
        expected_head_sequence=None,
    )
    second_reference = {
        "thread_id": second_cursor.thread_id,
        "sequence": second_cursor.sequence,
        "snapshot_id": second_cursor.snapshot_id,
        "run_id": second_run,
        "host_context_extents_revision": HOST_CONTEXT_EXTENTS_REVISION,
    }
    assert promote_native_projection(
        sessions,
        sid,
        second_reference,
        canonical_head=second_receipt["_canonical_head"],
        router=ROUTER,
        transcript_id=second_run,
    )

    sessions = open_sessions(tmp_path / "chats")
    snapshots = SQLiteRunSnapshotStore(snapshots.path)
    second_history, _ = current_projection(
        sessions, sid, snapshot_store=snapshots, model_route=ROUTE,
    )
    encoded = json.dumps(second_history)
    assert "[Host kernel continuation]" not in encoded
    assert "STALE_HOST_CONTEXT" not in encoded
    assert "SECOND TURN ONLY" not in encoded
    assert sum(row.get("content") == "current task" for row in second_history) == 1
    assert sum(row.get("content") == "second task" for row in second_history) == 1
    assert [row["content"] for row in second_history if row.get("role") == "tool"] == [
        "persistent_total=780"
    ]
    assert sessions.recent_convo(sid, None)[-2:] == [
        {"role": "user", "content": "second task"},
        {"role": "assistant", "content": "second result"},
    ]
    build_message_graph(second_history)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        (True, 0),
        ("1", 0),
        (None, 0),
        (-1, 0),
        (10_000, 1),
        (1, 10_000),
    ],
)
def test_invalid_or_overlapping_host_extents_fall_back_to_canonical(
    tmp_path, prefix, suffix,
):
    sessions, sid, snapshots, reference, receipt, state = _fixture(
        tmp_path, promote=False,
    )
    current = state["messages"][3]
    current[HOST_CONTEXT_PREFIX_KEY] = prefix
    current[HOST_CONTEXT_SUFFIX_KEY] = suffix
    state["host_context_extents_revision"] = HOST_CONTEXT_EXTENTS_REVISION
    later = snapshots.commit_boundary_sync(
        state,
        completed_node="finalize",
        next_node="end",
        expected_head_sequence=reference["sequence"],
    )
    reference.update(
        sequence=later.sequence,
        snapshot_id=later.snapshot_id,
        host_context_extents_revision=HOST_CONTEXT_EXTENTS_REVISION,
    )
    assert promote_native_projection(
        sessions,
        sid,
        reference,
        canonical_head=receipt["_canonical_head"],
        router=ROUTER,
        transcript_id=reference["run_id"],
    )
    expected = sessions.recent_convo(sid, None)
    result, _ = current_projection(
        sessions, sid, snapshot_store=snapshots, model_route=ROUTE,
    )
    assert result == expected
    assert not sessions.get_context_projection(sid)


def test_legacy_resume_overlay_uses_canonical_user_order_without_forging_revision(
    tmp_path,
):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    receipt = sessions.append_messages(sid, [
        {"role": "user", "text": "literal user request"},
        {"role": "assistant", "text": "Task stopped."},
    ])
    covered = sessions.canonical_context(
        sid, head_node_id=receipt["_canonical_head"],
    )["cursor"]
    legacy = {
        "run_id": "legacy-resume",
        "messages": [
            {"role": "system", "content": "old system"},
            {
                "role": "user",
                "content": "[Host kernel continuation]\nold facts\n\nliteral user request",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-legacy",
                    "type": "function",
                    "function": {
                        "name": "ipython",
                        "arguments": '{"code":"print(7)"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-legacy",
                "content": "7",
            },
        ],
    }
    repaired = repair_run_state_user_context(
        sessions, sid, legacy, covered=covered,
    )
    assert repaired["messages"][1]["content"] == "literal user request"
    assert repaired["messages"][2]["tool_calls"][0]["id"] == "call-legacy"
    assert repaired["messages"][3]["tool_call_id"] == "call-legacy"
    assert "host_context_extents_revision" not in repaired
    assert legacy["messages"][1]["content"].startswith("[Host kernel continuation]")
    build_message_graph(repaired["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "transcript", "terminal_snapshot", "reference"])
async def test_real_runner_finalizer_handshake_and_restart(tmp_path, monkeypatch, failure):
    from agent_engine.runner import run_main_chat_task
    from agent_engine.presets import chat_task_default
    from assistant_turn import AssistantTurn
    from agent_types import ToolBatchResult
    from chat_finalize import finish_chat_turn
    from chat_session import ActiveTurn, ConnectionSession
    from run_context import Variant1RunContext, bind_run_context
    from work_fabric.scope import WorkScope
    from tests.test_agent_engine import FakePorts, SPEC
    from tests.test_transcript_economy import _complete_recap
    import transcript_economy as te

    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    for i in range(8):
        sessions.append_messages(sid, [{"role": "user", "text": f"old task {i}"},
                                       {"role": "assistant", "text": f"old result {i}"}])
    before = sessions.recent_convo(sid, None)
    snapshots = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    action = {"tool": "ipython", "args": {"code": "print(persistent_total)"}, "id": "call_live"}
    fake = FakePorts(
        [AssistantTurn(tool_calls=(action,), stop_reason="tool_use"), AssistantTurn(text="finished")],
        [ToolBatchResult(text="persistent_total=780")],
        prompt_token_counts=[1000, 97_842, 1000], compress_threshold=96_000,
    )
    ports = fake.build()
    ports.setup.continuation_context = lambda _text: (
        "[Host kernel continuation]\nCURRENT TURN ONLY"
    )
    compressions = []

    async def compress(messages):
        compressions.append(True)
        async def summary(*_args, **_kwargs):
            return _complete_recap(confirmed="Prior tasks completed.")
        return await te.compress_messages(messages, complete=summary)

    ports.context.compress_messages = compress
    context = Variant1RunContext.create(source="chat", work_scope=WorkScope(chat_id=sid), metadata={"chat_id": sid})
    with bind_run_context(context):
        turn = await run_main_chat_task(
            config=chat_task_default().with_overrides(checkpoints=True), text="current task",
            base_system="fresh system", full_tspec=[SPEC], convo_tail=before, images=[],
            is_resume=False, resume_snap=None, ports=ports, snapshot_store=snapshots,
        )
        assert len(compressions) == 1
        assert len(fake.action_batches) == 1
        assert turn.commit_transcript_terminal is not None
        awaiting = await snapshots.load_latest_for_chat(sid)
        assert awaiting.status == "awaiting_transcript"
        assert awaiting.state["host_context_extents_revision"] == (
            HOST_CONTEXT_EXTENTS_REVISION
        )
        assert not sessions.get_context_projection(sid)
        session = ConnectionSession(viewed_session_id=sid)
        session.active = ActiveTurn(turn_session_id=sid, turn_display_user_text="current task")
        finish_ports = SimpleNamespace(
            io=SimpleNamespace(sessions=sessions, router=ROUTER, runtime_registry=None,
                               hub=SimpleNamespace(broadcast=AsyncMock()), emit=AsyncMock()),
            session=SimpleNamespace(set_last_user_text=lambda _text: None),
            tts=SimpleNamespace(tts_enabled=lambda: False),
        )
        callback = turn.commit_transcript_terminal
        if failure == "transcript":
            monkeypatch.setattr(sessions, "_append_segment_atomic", lambda *_: (_ for _ in ()).throw(OSError("disk unavailable")))
        elif failure == "terminal_snapshot":
            async def unavailable():
                raise OSError("snapshot unavailable")
            callback = unavailable
        elif failure == "reference":
            monkeypatch.setattr(sessions, "set_native_context_projection", lambda *_args, **_kwargs: False)
        await finish_chat_turn(finish_ports, SimpleNamespace(send_json=AsyncMock()), session,
            "current task", "neutral", "finished", extract_memory=False,
            transcript_id=turn.transcript_id, commit_transcript_terminal=callback)

    canonical = sessions.recent_convo(sid, None)
    assert canonical == (before if failure == "transcript" else before + [
        {"role": "user", "content": "current task"}, {"role": "assistant", "content": "finished"}])
    reference = sessions.get_context_projection(sid)
    if failure:
        assert not reference
        return
    assert reference["kind"] == "native_snapshot"
    assert reference["host_context_extents_revision"] == (
        HOST_CONTEXT_EXTENTS_REVISION
    )
    cursor = reference["snapshot"]
    assert cursor["host_context_extents_revision"] == (
        HOST_CONTEXT_EXTENTS_REVISION
    )
    loaded = await snapshots.load_cursor(SnapshotCursor(cursor["thread_id"], cursor["sequence"], cursor["snapshot_id"]))
    assert loaded.state["output"]["transcript_committed"] is True
    assert loaded.state["host_context_extents_revision"] == (
        HOST_CONTEXT_EXTENTS_REVISION
    )
    assert await turn.commit_transcript_terminal() == cursor  # idempotent exact cursor

    sessions = open_sessions(tmp_path / "chats")
    snapshots = SQLiteRunSnapshotStore(snapshots.path)
    next_history = await project_session_conversation(sessions, sid, mode="cloud", context_limit_tokens=272_000,
        snapshot_store=snapshots, model_route=ROUTE, compress_messages=compress)
    assert len(compressions) == 1
    assert sum(r.get("content") == "current task" for r in next_history) == 1
    assert "[Host kernel continuation]" not in json.dumps(next_history)
    assert "CURRENT TURN ONLY" not in json.dumps(next_history)
    assert any(r.get("variant1_compaction") for r in next_history)
    assert any(r.get("role") == "tool" for r in next_history)
    build_message_graph(next_history)
    assert sessions.recent_convo(sid, None) == canonical
