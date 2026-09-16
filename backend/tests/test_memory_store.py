"""Approved-memory lifecycle without the retired Context journal."""

from __future__ import annotations

import json

import pytest

from memory_store import MemoryConflict, MemoryStore


def test_consolidation_preserves_scope_type_expiry_and_provenance(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    identities = []
    for chat, scope, kind, expiry in [("a", "chat", "fact", 0), ("b", "chat", "fact", 0),
        ("a", "user", "fact", 0), ("a", "user", "profile", 0), ("a", "user", "fact", 1)]:
        proposal = store.propose(chat, kind="add", content="identical fact", scope=scope,
                                 metadata={"type": kind}, expires_at=expiry)
        identities.append(store.approve(proposal["proposal_id"], actor="user")["item_id"])
    proposal = store.propose("a", kind="add", content="identical fact", scope="chat", metadata={"type": "fact"})
    store.approve(proposal["proposal_id"], actor="user")
    assert store.consolidate_exact_duplicates() == 1
    assert len(store.retrieve("a", "identical")) == 3
    assert len(store.retrieve("b", "identical")) == 3
    assert store.count_items() == 5


def test_profile_context_uses_retrieval_scope_and_expiry_while_admin_keeps_all(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    for chat, scope, expiry, content in [("a", "chat", 0, "private-profile"),
        ("a", "user", 1, "expired-profile"), ("a", "user", 0, "global-profile")]:
        proposal = store.propose(chat, kind="add", content=content, scope=scope,
                                 metadata={"type": "profile"}, expires_at=expiry)
        store.approve(proposal["proposal_id"], actor="user")
    assert len(store.profile_items()) == 3
    assert store.render_profile() == "- global-profile"
    assert store.render_profile(chat_id="b") == "- global-profile"
    assert "private-profile" in store.render_profile(chat_id="a")
    assert "expired-profile" not in store.render_profile(chat_id="a")
    assert {row["content"] for row in store.retrieve("b", "profile")} == {"global-profile"}


def test_model_proposal_is_inert_until_approved_and_updates_use_cas(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    proposed = store.propose(
        "chat-a", kind="add", content="Use the blue deployment lane",
        confidence=0.9, provenance="user_message:turn-7",
    )
    assert store.retrieve("chat-a", "blue lane") == []

    added = store.approve(proposed["proposal_id"], actor="user")
    assert added["revision"] == 1
    rows = store.retrieve("chat-b", "blue lane")
    assert rows[0]["content"] == "Use the blue deployment lane"

    update = store.propose(
        "chat-a", kind="update", item_id=added["item_id"],
        expected_revision=1, content="Use the green deployment lane",
    )
    store.approve(update["proposal_id"], actor="user")
    with pytest.raises(MemoryConflict):
        store.propose(
            "chat-a", kind="update", item_id=added["item_id"],
            expected_revision=1, content="stale update",
        )


def test_delete_is_a_reversible_tombstone_with_revision_cas(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    proposal = store.propose("chat-a", kind="add", content="remember me")
    active = store.approve(proposal["proposal_id"], actor="user")
    deleted = store.tombstone(
        active["item_id"], expected_revision=1, actor="user", reason="cleanup"
    )
    assert deleted["state"] == "tombstoned"
    assert store.retrieve("chat-a", "remember") == []
    with pytest.raises((MemoryConflict, LookupError)):
        store.tombstone(active["item_id"], expected_revision=1, actor="user")
    restored = store.restore(active["item_id"], expected_revision=2, actor="user")
    assert restored["revision"] == 3


def test_chat_scoped_memory_and_rejected_proposal(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    proposal = store.propose(
        "chat-a", kind="add", content="chat-only detail", scope="chat"
    )
    store.approve(proposal["proposal_id"], actor="user")
    assert store.retrieve("chat-a", "chat-only")
    assert store.retrieve("chat-b", "chat-only") == []

    rejected = store.propose("chat-a", kind="add", content="do not retain")
    store.reject(rejected["proposal_id"], actor="user", note="private")
    assert all(
        row["content"] != "do not retain"
        for row in store.retrieve("chat-a", "retain")
    )


def test_explicit_memory_reopens_with_rebuildable_search(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = MemoryStore(str(path))
    store.remember_explicit(
        "chat-a",
        "The user's codename is Cedar",
        metadata={"type": "profile", "source": "manual"},
    )
    assert store.profile_items()[0]["text"] == "The user's codename is Cedar"
    assert MemoryStore(str(path)).retrieve("chat-a", "codename Cedar")


def test_bulk_export_and_clear_continue_past_internal_page_size(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    store._rebuild_search_index = lambda: None
    for index in range(505):
        store.remember_explicit("chat-a", f"fact {index}")

    destination = tmp_path / "memory.jsonl"
    assert store.export_jsonl(str(destination)) == 505
    assert len([
        json.loads(line)
        for line in destination.read_text(encoding="utf-8").splitlines()
    ]) == 505
    assert store.clear() == 505
    assert store.count_items() == 0
    assert store.count_items(include_tombstoned=True) == 505


def test_delete_chat_removes_pending_proposals_but_preserves_approved_memory(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    scoped = store.propose(
        "chat-a", kind="add", content="temporary chat detail", scope="chat"
    )
    store.approve(scoped["proposal_id"], actor="user")
    pending = store.propose(
        "chat-a", kind="add", content="unapproved detail", scope="chat"
    )

    store.delete_chat("chat-a")

    assert store.retrieve("chat-a", "temporary")
    proposals = store.status(chat_id="chat-a")["proposals"]
    assert all(row["proposal_id"] != pending["proposal_id"] for row in proposals)
