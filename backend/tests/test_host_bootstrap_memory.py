"""AppHost memory adapters use the canonical approved-memory store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app_host import AppHost
from memory_schema import MemoryRecord
from memory_store import MemoryStore


@pytest.mark.asyncio
async def test_bound_memory_mutations_are_awaitable(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    host = AppHost()
    host.install_runtime(
        SimpleNamespace(memory=SimpleNamespace(store=store))
    )

    first = await host.mem_add("Keep this", "fact", source="chat")
    second = await host.mem_add_record(MemoryRecord(content="Keep that too"))
    await host.mem_delete(first)

    assert first and second
    assert [row["content"] for row in store.list_items()] == ["Keep that too"]
