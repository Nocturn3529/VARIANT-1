"""Scoped external evidence remains retrievable without memory injection."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from artifacts.store import ContentAddressedArtifactStore
from external_context import SessionContextService, retire_fact_memory
from tests.support.conversation_sessions import open_sessions
from tools import ToolError


def fixture(tmp_path):
    sessions = open_sessions(tmp_path / "sessions", max_messages=1)
    chat = sessions.create_session()
    sessions.append_messages(chat, [{"role": "user", "text": "Straße: find the older plan"},
                                    {"role": "assistant", "text": "The plan is preserved."}])
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    cells = []
    kernel = SimpleNamespace(cell_ledger=SimpleNamespace(tail=lambda chat, limit: [SimpleNamespace(sequence=len(cells))] if cells else []),
                             execution_history=lambda chat, **kw: {"items": [row for row in cells if kw["after_sequence"] < row["sequence"] <= kw["through_sequence"]][:kw["limit"]],
                                "next_sequence": min(len(cells), kw["after_sequence"] + kw["limit"])})
    runtimes = SimpleNamespace(snapshot_store=None)
    service = SessionContextService(database_path=str(tmp_path / "context.sqlite3"), sessions=sessions,
                                    kernel=kernel, artifacts=artifacts, runtimes=runtimes)
    return service, sessions, chat, artifacts, cells, runtimes


def test_chronology_is_not_the_ui_cap_and_views_are_frozen(tmp_path):
    service, sessions, chat, *_ = fixture(tmp_path)
    view = service.capture(chat)
    assert len(sessions.get_session(chat)["messages"]) == 1
    page = service.read(chat, view, limit=1)
    assert page["has_more"] and page["items"][0]["role"] == "user"
    next_page = service.read(chat, view, after=page["next_cursor"])
    assert next_page["items"][0]["role"] == "assistant"
    sessions.append_messages(chat, [{"role": "user", "text": "newer commit"}])
    assert len(service.read(chat, view)["items"]) == 2
    assert len(service.read(chat, service.capture(chat))["items"]) == 3


def test_literal_unicode_search_and_exact_expansion_share_offsets(tmp_path):
    service, _, chat, *_ = fixture(tmp_path)
    view = service.capture(chat)
    hit = service.search(chat, view, query="STRASSE")["items"][0]
    exact = service.expand(chat, view, source_id=hit["source_id"], max_chars=8)
    assert exact["text"] == "Straße: " and exact["has_more"]
    assert service.expand(chat, view, source_id=hit["source_id"], offset=exact["next_offset"])["text"] == "find the older plan"
    assert service.search(chat, view, query="find")["items"][0]["match_offset"] == 8
    assert service.search(chat, view, query="%")["items"] == []


def test_views_rehydrate_and_deny_foreign_or_deleted_sessions(tmp_path):
    service, sessions, chat, artifacts, cells, runtimes = fixture(tmp_path)
    view = service.capture(chat)
    reopened = SessionContextService(database_path=service.path, sessions=sessions, artifacts=artifacts,
                                     kernel=service.kernel, runtimes=runtimes)
    assert reopened.status(chat, view)["counts"]["message"] == 2
    other = sessions.create_session()
    with pytest.raises(ToolError, match="outside"):
        reopened.read(other, view)
    with pytest.raises(ToolError, match="outside"):
        reopened.expand(chat, view, source_id="message:foreign")
    sessions.delete(chat)
    with pytest.raises(ToolError, match="deleted"):
        reopened.search(chat, view, query="plan")
    reopened.delete_chat(chat)
    with sqlite3.connect(service.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_source").fetchone()[0] == 0


def test_cell_source_is_plain_text_and_result_is_scoped_json(tmp_path):
    service, _, chat, artifacts, cells, _ = fixture(tmp_path)
    source = artifacts.put_text("print('older cell')", kind="kernel_cell_source", scope=chat)
    output = artifacts.put_json({"events": [{"kind": "stdout", "text": "retained full output"}],
                                 "dropped_bytes": 4}, kind="kernel_output_evidence", scope=chat)
    result = artifacts.put_json({"text": "older cell output", "error": "", "output_evidence": {
        "ref": output.ref, "sha256": output.sha256}}, kind="kernel_cell_result", scope=chat)
    cells.append({"sequence": 1, "execution_id": "exec-1", "source_ref": source.ref, "result_ref": result.ref})
    view = service.capture(chat)
    hit = service.search(chat, view, query="older cell", kind="cell")["items"][0]
    assert not hit.get("search_incomplete")
    assert service.expand(chat, view, source_id=hit["source_id"], part="source")["text"] == "print('older cell')"
    assert json.loads(service.expand(chat, view, source_id=hit["source_id"])["text"])["text"] == "older cell output"
    assert service.status(chat, view)["watermarks"]["ledger_through_sequence"] == 1
    evidence = json.loads(service.expand(chat, view, source_id=hit["source_id"], part="output")["text"])
    assert evidence["events"][0]["text"] == "retained full output" and evidence["dropped_bytes"] == 4
    from pathlib import Path
    Path(artifacts._path(source.sha256)).write_bytes(b"corrupted evidence")
    with pytest.raises(ToolError, match="integrity"):
        service.expand(chat, view, source_id=hit["source_id"], part="source")


def test_native_snapshot_projection_does_not_expose_opaque_reasoning(tmp_path):
    from external_context import _text
    assert _text({"type": "thinking", "thinking": "private", "signature": "opaque"}) == ""
    service, _, chat, _, _, runtimes = fixture(tmp_path)
    cursor = {"thread_id": "thread", "sequence": 3, "snapshot_id": "snap", "status": "running"}
    state = {"chat_id": chat, "messages": [{"role": "assistant", "content": [
        {"type": "text", "text": "visible"}, {"type": "reasoning", "text": "private reasoning"}],
        "reasoning": "opaque"}, {"role": "system", "content": "private contract"},
        {"role": "assistant", "content": {"type": "thinking", "thinking": "signed private reasoning"}}]}
    runtimes.snapshot_store = SimpleNamespace(context_cursors_sync=lambda sid: [cursor],
                                              load_cursor_sync=lambda c: SimpleNamespace(state=state))
    view = service.capture(chat)
    assert not service.search(chat, view, query="private")["items"]
    result = service.expand(chat, view, source_id="snapshot:snap")
    assert json.loads(result["text"]) == [{"role": "assistant", "content": "visible"}, {"role": "assistant", "content": ""}]
    state["chat_id"] = "foreign"
    with pytest.raises(ToolError, match="another session"):
        service.expand(chat, view, source_id="snapshot:snap")


def test_retirement_drops_only_fact_tables_in_shared_database(tmp_path):
    path = str(tmp_path / "shared.sqlite3")
    with sqlite3.connect(path) as conn:
        for name in ("session_memory_item", "session_memory_revision", "session_memory_proposal", "memory_fts", "astb_slots", "goals"):
            conn.execute(f"CREATE TABLE {name}(id TEXT)")
            conn.execute(f"INSERT INTO {name} VALUES('retained')")
    retire_fact_memory(path)
    retire_fact_memory(path)
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"astb_slots", "goals"}
        assert conn.execute("SELECT id FROM goals").fetchone()[0] == "retained"


def test_retirement_removes_fts_companion_tables(tmp_path):
    path = str(tmp_path / "fts.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(content)")
        conn.execute("INSERT INTO memory_fts VALUES('old fact')")
        conn.execute("CREATE TABLE kept(id TEXT)")
    retire_fact_memory(path)
    with sqlite3.connect(path) as conn:
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"kept"}


def test_canonical_chronology_uses_edges_despite_reversed_timestamps(tmp_path, monkeypatch):
    service, sessions, chat, *_ = fixture(tmp_path)
    with monkeypatch.context() as clock:
        clock.setattr("chat_sessions.service.time.time", lambda: 1.0)
        sessions.append_messages(chat, [{"role": "user", "text": "last despite old timestamp"}])
    rows = sessions.context_sources(chat)["items"]
    assert rows[-1]["created_at"] < rows[0]["created_at"]
    assert service.read(chat, service.capture(chat))["items"][-1]["preview"] == "last despite old timestamp"


def test_result_display_is_bounded_without_losing_mapping_content():
    from kernel_runtime.worker_bridge import _decode_host_result
    result = _decode_host_result({"schema": "variant1.session-context-result.v1", "text": "x" * 100_000,
                                  "view_id": "view", "has_more": True, "next_offset": 100_000}, None)
    assert len(repr(result)) < 300 and len(result["text"]) == 100_000


def test_all_committed_native_cursors_can_expand_after_later_compaction(tmp_path):
    from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
    from agent_engine.state import new_run_state
    service, _, chat, _, _, runtimes = fixture(tmp_path)
    store = SQLiteRunSnapshotStore(str(tmp_path / "snapshots.sqlite3"))
    runtimes.snapshot_store = store
    state = new_run_state(source="chat", title="History", goal="Retain evidence", thread_id="history-thread", run_id="run")
    state["chat_id"] = chat
    state["messages"] = [{"role": "user", "content": "original before compaction"}]
    first = store.commit_boundary_sync(state, completed_node="init", next_node="prepare", expected_head_sequence=None)
    state["messages"] = [{"role": "user", "content": "later projection"}]
    second = store.commit_boundary_sync(state, completed_node="init", next_node="prepare", expected_head_sequence=first.sequence)
    view = service.capture(chat)
    assert service.status(chat, view)["counts"]["snapshot"] == 2
    assert "original before compaction" in service.expand(chat, view, source_id="snapshot:" + first.snapshot_id)["text"]
    assert service.status(chat, view)["watermarks"]["snapshot_heads"]["history-thread"]["snapshot_id"] == second.snapshot_id


@pytest.mark.asyncio
async def test_retired_remember_command_never_claims_a_fact_write(monkeypatch):
    import chat_commands
    from unittest.mock import AsyncMock
    finish = AsyncMock()
    monkeypatch.setattr(chat_commands, "finish_chat_turn", finish)
    result = await chat_commands.CommandChatRoute().before_intent(
        SimpleNamespace(), None, None, "/remember prefer brief answers", is_resume=False,
        resume_state=None, reserved=False,
    )
    assert result.handled and chat_commands.is_direct_command("/remember prefer brief answers")
    assert "no longer saves facts" in finish.await_args.args[-1]


def test_hidden_child_history_and_explicit_parent_access_remain_scoped(tmp_path):
    service, sessions, parent, _, _, runtimes = fixture(tmp_path)
    child_chat = "hidden-child-runtime"
    record = SimpleNamespace(lifecycle_state="active", creation_saga_state="child:" + parent)
    runtimes.runtime = lambda sid: record if sid == child_chat else None
    def inspect(parent_id, child_id):
        if parent_id != parent or child_id != "child-1":
            raise LookupError("unknown child")
        return {"child_chat_id": child_chat, "deletion_state": ""}
    service.children = SimpleNamespace(inspect=inspect)
    runtimes.snapshot_store = SimpleNamespace(
        context_cursors_sync=lambda sid: [{"thread_id": "child-thread", "sequence": 1, "snapshot_id": "child-snap"}] if sid == child_chat else [],
        load_cursor_sync=lambda cursor: SimpleNamespace(state={"chat_id": child_chat, "messages": [
            {"role": "assistant", "content": "retained child work"}]}),
    )
    assert not sessions.has_session(child_chat)
    own = service.capture(child_chat)
    parent_view = service.capture(parent, child_id="child-1")
    for reader, view in ((child_chat, own), (parent, parent_view)):
        status = service.status(reader, view)
        assert status["watermarks"]["canonical_available"] is False
        assert status["watermarks"]["source_gaps"]
        assert "retained child work" in service.expand(reader, view, source_id="snapshot:child-snap")["text"]
    foreign = sessions.create_session()
    with pytest.raises(ToolError, match="outside"):
        service.capture(foreign, child_id="child-1")
    with pytest.raises(ToolError, match="outside"):
        service.read(child_chat, parent_view)
    record.lifecycle_state = "deleted"
    with pytest.raises(ToolError, match="deleted"):
        service.read(parent, parent_view)
    service.delete_chat(child_chat)
    with sqlite3.connect(service.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_view WHERE source_chat_id=?", (child_chat,)).fetchone()[0] == 0


def test_refresh_reuses_index_text_without_collapsing_distinct_messages(tmp_path):
    service, sessions, chat, *_ = fixture(tmp_path)
    first = service.capture(chat)
    sessions.append_messages(chat, [{"role": "user", "text": "Straße: find the older plan"}])
    second = service.capture(chat)
    assert len(service.read(chat, first)["items"]) == 2
    repeated = service.search(chat, second, query="older plan")["items"]
    assert len(repeated) == 2 and repeated[0]["source_id"] != repeated[1]["source_id"]
    with sqlite3.connect(service.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_record").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM context_member").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM context_text").fetchone()[0] == 2
    service.delete_chat(chat)
    with sqlite3.connect(service.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM context_text").fetchone()[0] == 0


def test_interrupted_index_commit_rolls_back_and_preserves_source(tmp_path, monkeypatch):
    service, sessions, chat, *_ = fixture(tmp_path)
    index_text = service._index_text
    calls = 0
    def interrupted(conn, text):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted index write")
        return index_text(conn, text)
    monkeypatch.setattr(service, "_index_text", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        service.capture(chat)
    with sqlite3.connect(service.path) as conn:
        for table in ("context_view", "context_source", "context_text"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert len(sessions.context_sources(chat)["items"]) == 2
    monkeypatch.setattr(service, "_index_text", index_text)
    assert len(service.read(chat, service.capture(chat))["items"]) == 2


def test_large_source_tail_offsets_remain_exact_and_searchable(tmp_path):
    service, _, chat, artifacts, cells, _ = fixture(tmp_path)
    source_text = "head " + "x" * 120_000 + " needle-tail 😀"
    source = artifacts.put_text(source_text, kind="kernel_cell_source", scope=chat)
    result = artifacts.put_json({"text": "done"}, kind="kernel_cell_result", scope=chat)
    cells.append({"sequence": 1, "execution_id": "large", "source_ref": source.ref, "result_ref": result.ref})
    view = service.capture(chat)
    found = service.search(chat, view, query="needle-tail")["items"][0]
    first = service.expand(chat, view, source_id=found["source_id"], part="source", max_chars=100_000)
    assert first["has_more"] and len(first["text"]) == 100_000
    tail = service.expand(chat, view, source_id=found["source_id"], part="source", offset=found["match_offset"])
    assert tail["text"] == "needle-tail 😀" and not tail["has_more"]


def test_later_correction_and_live_file_change_do_not_rewrite_evidence(tmp_path):
    service, sessions, chat, artifacts, cells, _ = fixture(tmp_path)
    sessions.append_messages(chat, [{"role": "user", "text": "Use draft A"},
                                   {"role": "user", "text": "Correction: use draft B"}])
    live_file = tmp_path / "changing.txt"
    live_file.write_text("old observation", encoding="utf-8")
    source = artifacts.put_text(f"read_file({str(live_file)!r})", kind="kernel_cell_source", scope=chat)
    result = artifacts.put_json({"text": live_file.read_text(encoding="utf-8")}, kind="kernel_cell_result", scope=chat)
    cells.append({"sequence": 1, "execution_id": "read-file", "source_ref": source.ref, "result_ref": result.ref})
    live_file.write_text("new current content", encoding="utf-8")
    view = service.capture(chat)
    corrections = service.search(chat, view, query="draft")["items"]
    assert [item["preview"] if "preview" in item else item["snippet"] for item in corrections] == ["Use draft A", "Correction: use draft B"]
    old = json.loads(service.expand(chat, view, source_id="cell:read-file")["text"])
    assert old["text"] == "old observation" and live_file.read_text(encoding="utf-8") == "new current content"


def test_sibling_branch_hits_cannot_enter_the_calling_view(tmp_path):
    service, sessions, chat, *_ = fixture(tmp_path)
    with sessions.repository._write() as conn:
        branch = dict(conn.execute("SELECT * FROM conversation_branch WHERE runtime_chat_id=?", (chat,)).fetchone())
        branch.update(branch_id="branch_sibling", runtime_chat_id="sibling-runtime", name="Sibling")
        columns = ",".join(branch)
        conn.execute(f"INSERT INTO conversation_branch({columns}) VALUES({','.join('?' for _ in branch)})", tuple(branch.values()))
    sessions.append_messages("sibling-runtime", [{"role": "user", "text": "private sibling-only evidence"}])
    sibling_source = sessions.context_sources("sibling-runtime")["items"][-1]["node_id"]
    view = service.capture(chat)
    assert service.search(chat, view, query="sibling-only", limit=1)["items"] == []
    with pytest.raises(ToolError, match="outside"):
        service.expand(chat, view, source_id="message:" + sibling_source)
    assert len(service.read(chat, view)["items"]) == 2
