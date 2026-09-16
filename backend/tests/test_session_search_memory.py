"""Conversation search and memory retrieval contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.conversation_sessions import open_sessions
from memory_tools import retrieve_memory
from memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return open_sessions(tmp_path / "sessions")


def _append_exchange(store, sid, user_text, assistant_text):
    return store.append_messages(sid, [
        {"role": "user", "text": user_text},
        {"role": "assistant", "text": assistant_text},
    ])


def test_search_finds_and_ranks_multi_term_matches(store):
    first = store.create_session()
    _append_exchange(store, first, "what was the invoice number again?", "It's 4381.")
    second = store.create_session()
    _append_exchange(
        store,
        second,
        "send the invoice 4381 to dana@example.com",
        "Sent the invoice to dana.",
    )

    hits = store.search("invoice dana")

    assert hits
    assert "dana" in hits[0]["snippet"].lower()
    assert hits[0]["session_id"] == second
    assert all(
        set(hit) >= {"session_id", "title", "ts", "role", "snippet", "score"}
        for hit in hits
    )


def test_search_empty_or_junk_query(store):
    assert store.search("") == []
    assert store.search("a") == []


def test_search_respects_limit(store):
    sid = store.create_session()
    for index in range(10):
        _append_exchange(
            store, sid, f"note about widgets {index}", f"noted widgets {index}",
        )
    assert len(store.search("widgets", limit=3)) == 3


@pytest.mark.asyncio
async def test_retrieve_memory_formats_dated_chat_snippets(store, tmp_path):
    sid = store.create_session()
    _append_exchange(store, sid, "the wifi password is hunter2", "Saved.")
    ports = SimpleNamespace(
        store=MemoryStore(str(tmp_path / "memory.sqlite3")),
        sessions=store,
    )

    output = await retrieve_memory(ports, {"query": "wifi password"})

    assert "wifi password" in output
    assert "hunter2" in output
    assert "20" in output


@pytest.mark.asyncio
async def test_retrieve_memory_requires_query(store):
    import tools

    with pytest.raises(tools.ToolError):
        await retrieve_memory(SimpleNamespace(), {})
