"""Native SQLite snapshot-store contracts."""

from __future__ import annotations

import json
import sqlite3
import zlib

import pytest

from agent_engine.errors import DurableCheckpointUnavailable
from agent_engine.presets import subagent_v1
from agent_engine.runner import _managed_boundary_committer, _validate_resume_boundary
from agent_engine.snapshot_store import SnapshotCursor, SnapshotHeadFilter
from agent_engine.snapshot_utils import is_incomplete_run_state
from agent_engine.sqlite_snapshot_store import (
    SQLiteRunSnapshotStore,
    SnapshotStoreConflict,
    SnapshotTombstonedError,
)
from agent_engine.state import RunState, new_run_state
from observability.trace_events import RECORDER


def _run_state(
    thread_id: str,
    *,
    source: str = "chat",
    chat_id: str = "chat-a",
    status: str = "running",
    updated_at: float = 10.0,
) -> RunState:
    state = new_run_state(
        source=source,
        title=f"Task {thread_id}",
        goal=f"Finish {thread_id}",
        thread_id=thread_id,
        run_id=f"run-{thread_id}",
    )
    state["chat_id"] = chat_id
    state["status"] = status
    state["created_at"] = 1.0
    state["updated_at"] = updated_at
    return state


def _commit(
    store: SQLiteRunSnapshotStore,
    state: RunState,
    *,
    completed_node: str = "init",
    next_node: str = "prepare",
    expected: int | None = None,
):
    return store.commit_boundary_sync(
        state,
        completed_node=completed_node,
        next_node=next_node,
        expected_head_sequence=expected,
    )


def test_full_state_snapshot_survives_reopen(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    state = _run_state("thread-a")
    state["messages"] = [{"role": "user", "content": "persist me"}]

    cursor = _commit(store, state)
    reopened = SQLiteRunSnapshotStore(str(db))
    loaded = reopened.load_head_sync("thread-a")

    assert cursor.sequence == 1
    assert loaded is not None
    assert loaded.cursor == cursor
    assert loaded.parent_snapshot_id == ""
    assert loaded.completed_node == "init"
    assert loaded.next_node == "prepare"
    assert loaded.state == state
    assert loaded.state["messages"] == [{"role": "user", "content": "persist me"}]
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        codec, storage_type = conn.execute(
            "SELECT codec_version, typeof(state_blob) FROM agent_snapshots"
        ).fetchone()
    assert {"agent_threads", "agent_snapshots", "snapshot_migrations"} <= tables
    assert codec == "json.zlib.v1"
    assert storage_type == "blob"


def test_snapshot_commit_trace_separates_durable_phases(tmp_path):
    trace = tmp_path / "trace.jsonl"
    RECORDER.configure_for_tests(path=str(trace), enabled=True)
    try:
        store = SQLiteRunSnapshotStore(str(tmp_path / "timed.sqlite3"))
        _commit(store, _run_state("timed-thread"))
        assert RECORDER.flush()
        rows = [
            json.loads(line)
            for line in trace.read_text(encoding="utf-8").splitlines()
        ]
        commits = [row for row in rows if row["event"] == "snapshot:commit"]
        assert len(commits) == 1
        attributes = commits[0]["attributes"]
        assert commits[0]["status"] == "ok"
        assert attributes["completed_node"] == "init"
        assert attributes["next_node"] == "prepare"
        for key in (
            "encode_ms", "lock_wait_ms", "connect_ms", "begin_ms",
            "write_ms", "commit_ms", "total_ms",
        ):
            assert attributes[key] >= 0
        assert attributes["total_ms"] >= attributes["commit_ms"]
    finally:
        RECORDER.reset_configuration()


def test_append_requires_exact_head_sequence_and_preserves_parent_chain(tmp_path):
    store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    state = _run_state("cas-thread")
    first = _commit(store, state)

    with pytest.raises(SnapshotStoreConflict, match="already has head sequence"):
        _commit(store, state)

    state["step"] = 1
    state["updated_at"] = 20.0
    second = _commit(
        store,
        state,
        completed_node="prepare",
        next_node="loop_init",
        expected=first.sequence,
    )

    with pytest.raises(SnapshotStoreConflict, match="expected 1, found 2"):
        _commit(
            store,
            state,
            completed_node="loop_init",
            next_node="model_step",
            expected=first.sequence,
        )

    loaded = store.load_head_sync("cas-thread")
    assert loaded is not None
    assert loaded.cursor == second
    assert loaded.parent_snapshot_id == first.snapshot_id
    assert loaded.state["step"] == 1


def test_historical_cursor_survives_a_new_run_on_the_same_stable_thread(tmp_path):
    store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    original = _run_state("stable-thread", updated_at=10.0)
    original["run_id"] = "run-original"
    first = _commit(store, original)

    replacement = _run_state("stable-thread", updated_at=20.0)
    replacement["run_id"] = "run-replacement"
    second = _commit(
        store,
        replacement,
        completed_node="prepare",
        next_node="loop_init",
        expected=first.sequence,
    )

    historical = store.load_cursor_sync(first)
    head = store.load_cursor_sync(second)

    assert historical is not None
    assert historical.run_id == "run-original"
    assert historical.state["run_id"] == "run-original"
    assert head is not None
    assert head.run_id == "run-replacement"


def test_snapshot_scrubs_image_bytes_recursively_without_mutating_input(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    state = _run_state("private")
    state["vision"] = {
        "initial_image_b64": "vision-secret",
        "initial_image_count": 1,
        "initial_image_media_types": ["image/png"],
    }
    state["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,url-secret"},
                },
                {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": "content-secret",
                    "metadata": {"data": "keep-this-text"},
                },
            ],
        }
    ]

    _commit(store, state)
    loaded = store.load_head_sync("private")

    assert state["vision"]["initial_image_b64"] == "vision-secret"
    assert loaded is not None
    assert loaded.state["vision"] == {
        "initial_image_b64": "",
        "initial_image_count": 1,
        "initial_image_media_types": ["image/png"],
    }
    content = loaded.state["messages"][0]["content"]
    assert content[0]["image_url"]["url"] == ""
    assert content[1]["data"] == ""
    assert content[1]["metadata"]["data"] == "keep-this-text"
    with sqlite3.connect(db) as conn:
        blob = conn.execute("SELECT state_blob FROM agent_snapshots").fetchone()[0]
        scrubbed = conn.execute(
            "SELECT values_scrubbed FROM snapshot_migrations "
            "WHERE name='native_full_state_v1'"
        ).fetchone()[0]
    raw = zlib.decompress(blob)
    assert b"vision-secret" not in raw
    assert b"url-secret" not in raw
    assert b"content-secret" not in raw
    assert scrubbed == 3


def test_delete_tombstones_thread_and_refuses_resurrection(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    state = _run_state("deleted-thread")
    _commit(store, state)

    store.delete_thread_sync("deleted-thread")

    assert store.load_head_sync("deleted-thread") is None
    with sqlite3.connect(db) as conn:
        tombstone = conn.execute(
            "SELECT tombstoned_at, head_seq, head_snapshot_id "
            "FROM agent_threads WHERE thread_id='deleted-thread'"
        ).fetchone()
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM agent_snapshots WHERE thread_id='deleted-thread'"
        ).fetchone()[0]
    assert tombstone[0] is not None
    assert tombstone[1:] == (None, None)
    assert snapshot_count == 0

    with pytest.raises(SnapshotTombstonedError, match="deleted"):
        _commit(store, state)


def test_deleting_unknown_thread_also_prevents_late_creation(tmp_path):
    store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    store.delete_thread_sync("late-thread")

    with pytest.raises(SnapshotTombstonedError, match="deleted"):
        _commit(store, _run_state("late-thread"))


def test_copy_thread_rewrites_identity_and_preserves_complete_history(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    state = _run_state("source", chat_id="copy-chat")
    first = _commit(store, state)
    state["step"] = 1
    state["updated_at"] = 20.0
    source_head = _commit(
        store,
        state,
        completed_node="prepare",
        next_node="loop_init",
        expected=first.sequence,
    )

    store.copy_thread_sync("source", "fork")

    original = store.load_head_sync("source")
    copied = store.load_head_sync("fork")
    assert original is not None and original.cursor == source_head
    assert copied is not None
    assert copied.cursor.thread_id == "fork"
    assert copied.cursor.sequence == 2
    assert copied.cursor.snapshot_id != source_head.snapshot_id
    assert copied.parent_snapshot_id not in {"", first.snapshot_id}
    assert copied.state["thread_id"] == "fork"
    assert copied.state["run_id"] == state["run_id"]
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT seq, snapshot_id, parent_snapshot_id FROM agent_snapshots "
            "WHERE thread_id='fork' ORDER BY seq"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][2] == ""
    assert rows[1][2] == rows[0][1]

    with pytest.raises(ValueError, match="already exists"):
        store.copy_thread_sync("source", "fork")


def test_copy_refuses_corrupt_source_and_rolls_back_target(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    _commit(store, _run_state("source"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE agent_snapshots SET payload_sha256=? WHERE thread_id='source'",
            ("0" * 64,),
        )

    with pytest.raises(DurableCheckpointUnavailable, match="corrupt"):
        store.copy_thread_sync("source", "fork")

    assert store.load_head_sync("fork") is None
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_threads WHERE thread_id='fork'"
        ).fetchone()[0] == 0


def test_copy_thread_through_forks_exact_native_prefix_and_identity(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    state = _run_state("source", chat_id="source-chat")
    first = _commit(store, state)
    state["step"] = 1
    second = _commit(
        store,
        state,
        completed_node="prepare",
        next_node="loop_init",
        expected=first.sequence,
    )
    state["step"] = 2
    third = _commit(
        store,
        state,
        completed_node="loop_init",
        next_node="model_step",
        expected=second.sequence,
    )

    forked = store.copy_thread_through_sync(
        second,
        "fork-thread",
        target_run_id="fork-run",
        target_chat_id="fork-chat",
    )

    assert forked.thread_id == "fork-thread"
    assert forked.sequence == 2
    assert forked.snapshot_id not in {first.snapshot_id, second.snapshot_id}
    source_head = store.load_head_sync("source")
    target_head = store.load_head_sync("fork-thread")
    assert source_head is not None and source_head.cursor == third
    assert target_head is not None and target_head.cursor == forked
    assert target_head.state["thread_id"] == "fork-thread"
    assert target_head.state["run_id"] == "fork-run"
    assert target_head.state["chat_id"] == "fork-chat"
    assert target_head.state["step"] == 1
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT seq, snapshot_id, parent_snapshot_id FROM agent_snapshots "
            "WHERE thread_id='fork-thread' ORDER BY seq"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][2] == ""
    assert rows[1][2] == rows[0][1]


def test_copy_thread_through_rejects_inexact_terminal_and_tombstoned_targets(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    state = _run_state("source")
    cursor = _commit(store, state)

    wrong = SnapshotCursor(cursor.thread_id, cursor.sequence, "snap_wrong")
    with pytest.raises(DurableCheckpointUnavailable, match="exact boundary"):
        store.copy_thread_through_sync(
            wrong,
            "wrong-target",
            target_run_id="wrong-run",
            target_chat_id="wrong-chat",
        )
    assert store.load_head_sync("wrong-target") is None

    store.delete_thread_sync("deleted-target")
    with pytest.raises(SnapshotTombstonedError, match="deleted"):
        store.copy_thread_through_sync(
            cursor,
            "deleted-target",
            target_run_id="deleted-run",
            target_chat_id="deleted-chat",
        )

    terminal = _run_state("terminal", status="completed")
    terminal_cursor = _commit(
        store,
        terminal,
        completed_node="finalize",
        next_node="end",
    )
    with pytest.raises(DurableCheckpointUnavailable, match="terminal"):
        store.copy_thread_through_sync(
            terminal_cursor,
            "terminal-fork",
            target_run_id="terminal-fork-run",
            target_chat_id="terminal-fork-chat",
        )
    assert store.load_head_sync("terminal-fork") is None


def test_load_rejects_checksum_mismatch(tmp_path):
    db = tmp_path / "snapshots.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    _commit(store, _run_state("checksum"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE agent_snapshots SET payload_sha256=? WHERE thread_id='checksum'",
            ("f" * 64,),
        )

    with pytest.raises(DurableCheckpointUnavailable, match="checksum mismatch"):
        store.load_head_sync("checksum")


@pytest.mark.asyncio
async def test_truncated_worker_head_is_terminal_for_store_resume_and_fresh_generation(tmp_path):
    db = tmp_path / "truncated.sqlite3"
    store = SQLiteRunSnapshotStore(str(db))
    state = _run_state("truncated", source="subagent", chat_id="", status="truncated")
    state["worker"] = {
        "route": "finalize",
        "error": "The model reached its output limit repeatedly.",
        "terminal_reason": "model_output_limit",
        "length_recoveries": 2,
    }
    state["output"] = {
        "completion_status": "truncated",
        "terminal_reason": "model_output_limit",
        "length_recoveries": 2,
    }
    cursor = _commit(
        store,
        state,
        completed_node="worker_finalize",
        next_node="finalize",
    )
    head = store.load_head_sync("truncated")

    assert head is not None
    assert head.status == "truncated"
    assert not is_incomplete_run_state(head.state)
    with sqlite3.connect(db) as conn:
        resumable, terminal_at = conn.execute(
            "SELECT resumable, terminal_at FROM agent_threads WHERE thread_id=?",
            ("truncated",),
        ).fetchone()
    assert resumable == 0
    assert terminal_at is not None
    with pytest.raises(DurableCheckpointUnavailable, match="terminal native snapshot"):
        _validate_resume_boundary(head, worker=True)

    fresh = _run_state("truncated", source="subagent", chat_id="")
    fresh["run_id"] = "run-truncated-next"
    committer = await _managed_boundary_committer(
        config=subagent_v1(),
        state=fresh,
        is_resume=False,
        worker=True,
        snapshot_store=store,
    )
    assert committer.cursor == cursor


def test_state_size_limit_and_nonfinite_values_fail_before_commit(tmp_path, monkeypatch):
    store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    oversized = _run_state("oversized")
    oversized["messages"] = [{"role": "user", "content": "x" * 10_000}]
    monkeypatch.setenv("VARIANT1_MAX_SNAPSHOT_STATE_BYTES", "1024")

    with pytest.raises(ValueError, match="exceeds 1024 bytes"):
        _commit(store, oversized)

    nonfinite = _run_state("nonfinite")
    nonfinite["updated_at"] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        _commit(store, nonfinite)

    assert store.load_head_sync("oversized") is None
    assert store.load_head_sync("nonfinite") is None


def test_chat_lookup_and_head_filters_are_scoped_before_hydration(tmp_path):
    store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    _commit(store, _run_state("chat-old", chat_id="wanted", updated_at=10.0))
    _commit(store, _run_state("chat-new", chat_id="wanted", updated_at=20.0))
    _commit(
        store,
        _run_state(
            "automation",
            source="automation",
            chat_id="wanted",
            updated_at=30.0,
        ),
    )
    _commit(store, _run_state("foreign", chat_id="other", updated_at=40.0))

    latest = store.load_latest_for_chat_sync("wanted")
    heads = store.list_heads_sync(SnapshotHeadFilter(
        source="chat",
        chat_id="wanted",
        statuses=("running",),
        limit=1,
    ))

    assert latest is not None
    assert latest.cursor.thread_id == "chat-new"
    assert [head.cursor.thread_id for head in heads] == ["chat-new"]
    with pytest.raises(ValueError, match="requires chat_id"):
        store.load_latest_for_chat_sync("")


@pytest.mark.asyncio
async def test_async_store_contract_commits_and_loads_full_state(tmp_path):
    store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    state = _run_state("async-thread")

    cursor = await store.commit_boundary(
        state,
        completed_node="init",
        next_node="prepare",
        expected_head_sequence=None,
    )
    loaded = await store.load_head("async-thread")

    assert loaded is not None
    assert loaded.cursor == cursor
    assert loaded.state == state
